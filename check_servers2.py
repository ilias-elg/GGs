import urllib.request, json
req = urllib.request.urlopen('https://games.roblox.com/v1/games/79669834155516/servers/Public?limit=100')
data = json.loads(req.read())
for s in data['data']:
    print(f"ID: {s['id'][:4]}-{s['id'][4:8]} Playing: {s['playing']}/{s['maxPlayers']}")
