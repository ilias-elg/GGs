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
    ROBLOX_PROXY_URL,
)

logger = logging.getLogger('discord')

class RobloxClient:
    def __init__(self):
        self.session = None
        self._presence_blocked_until = 0.0

    @property
    def _request_options(self) -> dict:
        return {"proxy": ROBLOX_PROXY_URL} if ROBLOX_PROXY_URL else {}

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "FireNationBot/1.0"}
            )
        return self.session

    async def fetch_group_members(self, group_id):
        """Fetches all members of a group handling pagination."""
        session = await self.get_session()
        users = []
        cursor = ""
        url = f"https://groups.roblox.com/v1/groups/{group_id}/users"
        
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
                
            try:
                async with session.get(url, params=params, **self._request_options) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                        
                    elif resp.status != 200:
                        logger.error(f"Failed to fetch group {group_id}: HTTP {resp.status}")
                        return None
                        
                    data = await resp.json()
                    
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
                logger.error(f"Error fetching group members for {group_id}: {e}")
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
        session = await self.get_session()
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
                    async with session.post(url, json=payload, **self._request_options) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results.extend(data.get("userPresences", []))
                            completed = True
                            break
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                        else:
                            logger.error(
                                "Failed to fetch presence batch %s/%s: HTTP %s",
                                batch_number,
                                len(batches),
                                resp.status,
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
                    logger.error(f"Error fetching presence batch {batch_number}: {e}")
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
            
        session = await self.get_session()
        url = "https://games.roblox.com/v1/games"
        
        id_str = ",".join(map(str, universe_ids))
        params = {"universeIds": id_str}
        
        names = {}
        try:
            async with session.get(url, params=params, **self._request_options) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get('data', []):
                        names[item['id']] = item['name']
        except Exception as e:
            logger.error(f"Error fetching game names: {e}")
            
        return names

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
