import urllib.request
import json
url = "https://streaming-catalogs.strem.fun/manifest.json"
try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        print("Resources:", data.get("resources"))
except Exception as e:
    print("Error:", e)
