import aiohttp
import asyncio
import logging
import re
import time

from .config import (
    PRESENCE_BATCH_DELAY_SECONDS,
    PRESENCE_BATCH_SIZE,
    PRESENCE_429_COOLDOWN_SECONDS,
    PRESENCE_MAX_RETRIES,
    ROBLOX_PROXY_LIST,
)
import random

logger = logging.getLogger('discord')

# A proxy that refuses requests (out of credit, bad login, unreachable) is
# left alone for this long before being tried again.
PROXY_COOLDOWN_SECONDS = 15 * 60


def _redact(text) -> str:
    """Strips user:password@ from proxy URLs so credentials never reach the logs."""
    return re.sub(r"//[^/@\s]+@", "//", str(text))


class RobloxClient:
    def __init__(self):
        self.session = None
        self._presence_blocked_until = 0.0
        self._bad_proxies: dict[str, float] = {}

    def _pick_proxy(self) -> str | None:
        """A random proxy that isn't cooling down, or None to connect directly."""
        now = time.monotonic()
        working = [p for p in ROBLOX_PROXY_LIST if self._bad_proxies.get(p, 0) <= now]
        return random.choice(working) if working else None

    async def _request(self, method: str, url: str, **kwargs):
        """
        One Roblox request. Returns (status, headers, json-or-None).

        If the chosen proxy itself refuses the request, it is benched and the
        request is retried through another proxy — and directly, once none are
        left — instead of failing the whole sync or scan.
        """
        session = await self.get_session()
        for _ in range(len(ROBLOX_PROXY_LIST) + 1):
            proxy = self._pick_proxy()
            try:
                async with session.request(method, url, proxy=proxy, **kwargs) as resp:
                    data = await resp.json() if resp.status == 200 else None
                    return resp.status, resp.headers, data
            except (aiohttp.ClientHttpProxyError, aiohttp.ClientProxyConnectionError) as e:
                if proxy is None:
                    raise
                self._bad_proxies[proxy] = time.monotonic() + PROXY_COOLDOWN_SECONDS
                reason = (
                    f"{e.status} {e.message}" if isinstance(e, aiohttp.ClientHttpProxyError)
                    else "could not connect"
                )
                now = time.monotonic()
                remaining = sum(1 for p in ROBLOX_PROXY_LIST if self._bad_proxies.get(p, 0) <= now)
                logger.warning(
                    "Roblox proxy %s refused the request (%s); skipping it for %s minutes. %s",
                    _redact(proxy),
                    reason,
                    PROXY_COOLDOWN_SECONDS // 60,
                    f"{remaining} other proxies still in use." if remaining
                    else "No working proxies left — connecting directly.",
                )
        raise RuntimeError("every Roblox proxy refused the request")

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "FireNationBot/1.0"}
            )
        return self.session

    async def fetch_group_members(self, group_id):
        """Fetches all members of a group handling pagination."""
        users = []
        cursor = ""
        url = f"https://groups.roblox.com/v1/groups/{group_id}/users"

        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor

            try:
                status, _, data = await self._request("GET", url, params=params)
                if status == 429:
                    await asyncio.sleep(5)
                    continue

                elif status != 200:
                    logger.error(f"Failed to fetch group {group_id}: HTTP {status}")
                    return None

                if 'data' in data:
                    for item in data['data']:
                        users.append({
                            'user_id': item['user']['userId'],
                            'username': item['user'].get('username', ''),
                            'rank': item['role']['rank'],
                            'role': item['role']['name']
                        })

                cursor = data.get('nextPageCursor')
                if not cursor:
                    break

            except Exception as e:
                logger.error(f"Error fetching group members for {group_id}: {_redact(e)}")
                return None  # Return None so we don't wipe the DB with a partial list

        return users

    @staticmethod
    def _retry_after_seconds(value: str | None, attempt: int) -> float:
        """Return a bounded retry delay from Roblox's header or backoff."""
        if value:
            match = re.search(r"\d+(?:\.\d+)?", value)
            if match:
                return min(max(float(match.group(0)), 1.0), 60.0)
        return min(5.0 * (2 ** attempt), 60.0)

    async def fetch_presence(self, user_ids):
        """Fetch presence sequentially, retrying only the batch Roblox throttles."""
        url = "https://presence.roblox.com/v1/presence/users"
        remaining_cooldown = self._presence_blocked_until - time.monotonic()
        if remaining_cooldown > 0:
            logger.info(
                "Skipping presence scan; Roblox cooldown has %.0fs remaining.",
                remaining_cooldown,
            )
            return None
        batches = [
            user_ids[index:index + PRESENCE_BATCH_SIZE]
            for index in range(0, len(user_ids), PRESENCE_BATCH_SIZE)
        ]
        results = []

        for batch_number, batch in enumerate(batches, start=1):
            payload = {"userIds": batch}
            completed = False
            for attempt in range(PRESENCE_MAX_RETRIES + 1):
                try:
                    status, headers, data = await self._request("POST", url, json=payload)
                    if status == 200:
                        results.extend(data.get("userPresences", []))
                        completed = True
                        break
                    if status == 429:
                        retry_after = headers.get("Retry-After")
                    else:
                        logger.error(
                            "Failed to fetch presence batch %s/%s: HTTP %s",
                            batch_number,
                            len(batches),
                            status,
                        )
                        return None
                    if attempt >= PRESENCE_MAX_RETRIES:
                        self._presence_blocked_until = (
                            time.monotonic() + PRESENCE_429_COOLDOWN_SECONDS
                        )
                        logger.warning(
                            "Roblox blocked presence batch %s/%s; skipping this scan and "
                            "cooling down for %ss. If this repeats, configure ROBLOX_PROXY_URL "
                            "or use a host with a dedicated outbound IP.",
                            batch_number,
                            len(batches),
                            PRESENCE_429_COOLDOWN_SECONDS,
                        )
                        return None
                    if not retry_after:
                        self._presence_blocked_until = (
                            time.monotonic() + PRESENCE_429_COOLDOWN_SECONDS
                        )
                        logger.warning(
                            "Roblox returned a bare 429 for presence batch %s/%s; skipping "
                            "this scan instead of waiting. Cooldown: %ss.",
                            batch_number,
                            len(batches),
                            PRESENCE_429_COOLDOWN_SECONDS,
                        )
                        return None
                    delay = self._retry_after_seconds(retry_after, attempt)
                    logger.warning(
                        "Roblox throttled presence batch %s/%s; retrying in %.1fs (%s/%s).",
                        batch_number,
                        len(batches),
                        delay,
                        attempt + 1,
                        PRESENCE_MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                except Exception as e:
                    logger.error(f"Error fetching presence batch {batch_number}: {_redact(e)}")
                    return None

            if not completed:
                return None
            if batch_number < len(batches):
                await asyncio.sleep(PRESENCE_BATCH_DELAY_SECONDS)

        return results

    async def fetch_game_names(self, universe_ids):
        """Fetches names for a list of universe IDs."""
        if not universe_ids:
            return {}

        url = "https://games.roblox.com/v1/games"

        id_str = ",".join(map(str, universe_ids))
        params = {"universeIds": id_str}

        names = {}
        try:
            status, _, data = await self._request("GET", url, params=params)
            if status == 200:
                for item in data.get('data', []):
                    names[item['id']] = item['name']
        except Exception as e:
            logger.error(f"Error fetching game names: {_redact(e)}")

        return names

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
