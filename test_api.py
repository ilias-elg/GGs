import asyncio
import aiosqlite
import aiohttp

async def main():
    async with aiosqlite.connect('roblox_monitor.db') as db:
        async with db.execute('SELECT DISTINCT user_id FROM group_members') as cursor:
            rows = await cursor.fetchall()
            user_ids = [r[0] for r in rows]
            
    print(f"Total users: {len(user_ids)}")
    print(f"Any None? {None in user_ids}")
    
    async with aiohttp.ClientSession() as session:
        url = "https://presence.roblox.com/v1/presence/users"
        batch_size = 50
        for i in range(0, len(user_ids), batch_size):
            batch = user_ids[i:i+batch_size]
            payload = {"userIds": batch}
            async with session.post(url, json=payload) as resp:
                print(f"Batch {i//batch_size}: {resp.status}")
                if resp.status != 200:
                    print(await resp.text())

asyncio.run(main())
