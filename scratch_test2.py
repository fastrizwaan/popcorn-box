import urllib.request
import json

urls = [
    "https://tmdb-addon.strem.io/meta/series/tmdb:series:93289.json",
    "https://tmdb-addon.strem.io/meta/series/tmdb:93289.json",
    "https://tmdb-addon.strem.io/meta/series/93289.json"
]

for url in urls:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            print(url)
            print("SUCCESS. Meta ID:", data.get("meta", {}).get("id"))
            print("IMDB ID:", data.get("meta", {}).get("imdb_id"))
    except Exception as e:
        print(url, "FAILED:", e)
