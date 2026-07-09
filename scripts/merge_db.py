import json
from pathlib import Path

host_db = Path.home() / ".config/popcorn-box/data.json"
sandbox_db = Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/config/popcorn-box/data.json"

if host_db.exists() and sandbox_db.exists():
    with open(host_db, "r") as f:
        host_data = json.load(f)
    with open(sandbox_db, "r") as f:
        sandbox_data = json.load(f)
        
    for key in ["favorites", "watched", "history", "downloads"]:
        host_items = host_data.get(key, [])
        sandbox_items = sandbox_data.get(key, [])
        
        # Merge, preferring sandbox items if they are newer (just prepend them, then deduplicate)
        # Deduplication logic:
        merged = []
        seen = set()
        
        # for downloads, unique by info_hash
        if key == "downloads":
            for item in sandbox_items + host_items:
                h = item.get("info_hash")
                if h == "testhash": continue # Remove the dummy
                if h not in seen:
                    seen.add(h)
                    merged.append(item)
        else:
            # for history, watched, favorites, unique by id
            for item in sandbox_items + host_items:
                i = item.get("id")
                if i not in seen:
                    seen.add(i)
                    merged.append(item)
                    
        host_data[key] = merged
        
    with open(host_db, "w") as f:
        json.dump(host_data, f, indent=4)
    print("Successfully merged sandbox data into host data.json!")
else:
    print("Could not find both files to merge.")
