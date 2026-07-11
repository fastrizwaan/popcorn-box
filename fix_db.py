import json, re

db_path = "/var/home/rizvan/.config/popcorn-box/data.json"
with open(db_path, "r") as f:
    text = f.read()

# Try to extract the top-level keys up to "addons"
import ast
recovered = {}

for key in ["favorites", "watched", "history", "downloads", "settings"]:
    match = re.search(f'"{key}"\s*:\s*(\[.*?\]|{{.*?}})', text, flags=re.DOTALL)
    if match:
        try:
            val_str = match.group(1)
            # Some manual cleanup if the regex grabbed too much, but since the JSON is formatted nicely it should be fine.
            # Actually, regex parsing JSON is dangerous. Let's just strip everything from '"addons":' onwards.
            pass
        except:
            pass

# Simpler way: find '"addons":' and truncate the string there, then add empty addons list and close braces!
addons_idx = text.rfind('"addons":')
if addons_idx != -1:
    fixed_text = text[:addons_idx] + '"addons": []\n}'
    try:
        data = json.loads(fixed_text)
        with open(db_path, "w") as f:
            json.dump(data, f, indent=4)
        print("Database recovered successfully!")
    except Exception as e:
        print("Failed to parse fixed JSON:", e)
else:
    print("Could not find addons key.")

