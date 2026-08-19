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
        users = set()
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
                        
                    if resp.status != 200:
                        logger.error(f"Failed to fetch group {group_id}: HTTP {resp.status}")
                        break
                        
                    data = await resp.json()
                    
                    if 'data' in data:
                        for item in data['data']:
                            users.add(item['user']['userId'])
                            
                    cursor = data.get('nextPageCursor')
                    if not cursor:
                        break
                        
            except Exception as e:
                logger.error(f"Error fetching group members for {group_id}: {e}")
                break
                
        return users

    async def fetch_presence(self, user_ids):
        """Fetches presence for a list of user IDs in batches."""
        session = await self.get_session()
        results = []
        url = "https://presence.roblox.com/v1/presence/users"
        
        # Roblox API limits batch size to 50 for presence
        batch_size = 50
        for i in range(0, len(user_ids), batch_size):
            batch = user_ids[i:i+batch_size]
            payload = {"userIds": batch}
            
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(3)
                        # Retry once
                        async with session.post(url, json=payload) as retry_resp:
                            if retry_resp.status == 200:
                                data = await retry_resp.json()
                                results.extend(data.get('userPresences', []))
                    elif resp.status == 200:
                        data = await resp.json()
                        results.extend(data.get('userPresences', []))
                    else:
                        logger.error(f"Failed to fetch presence: HTTP {resp.status}")
            except Exception as e:
                logger.error(f"Error fetching presence: {e}")
                
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
