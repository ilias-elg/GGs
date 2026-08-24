import aiohttp
import asyncio
import logging

logger = logging.getLogger('discord')

class RobloxClient:
    def __init__(self):
        self.session = None

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
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
                async with session.get(url, params=params) as resp:
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

    async def fetch_presence(self, user_ids):
        """Fetches presence for a list of user IDs in sequential batches to avoid rate limits."""
        session = await self.get_session()
        url = "https://presence.roblox.com/v1/presence/users"
        batch_size = 50
        
        batches = [user_ids[i:i+batch_size] for i in range(0, len(user_ids), batch_size)]
        results = []
        
        for batch in batches:
            payload = {"userIds": batch}
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results.extend(data.get('userPresences', []))
                    elif resp.status == 429:
                        logger.warning("429 rate limit hit on presence API, aborting scan to avoid partial data.")
                        return None
                    else:
                        logger.error(f"Failed to fetch presence: HTTP {resp.status}")
                        return None
            except Exception as e:
                logger.error(f"Error fetching presence batch: {e}")
                return None
                
            # Sleep 1 second between batches to avoid burst limits
            await asyncio.sleep(1)
            
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
            async with session.get(url, params=params) as resp:
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
