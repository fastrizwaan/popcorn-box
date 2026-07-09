import json
import os
import threading
from pathlib import Path

_db_lock = threading.RLock()

if os.environ.get("FLATPAK_ID"):
    _xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    CONFIG_DIR = Path(_xdg_config) / "popcorn-box"
else:
    CONFIG_DIR = Path.home() / ".var/app/io.github.fastrizwaan.PopcornBox/config/popcorn-box"

DB_FILE = CONFIG_DIR / "data.json"

HISTORY_LIMIT = 100

DEFAULT_ADDONS = [
    {
        "id": "cinemeta",
        "name": "Cinemeta",
        "version": "3.0.0",
        "description": "Provides movies and series catalogs from IMDb.",
        "manifest_url": "https://v3-cinemeta.strem.io/manifest.json",
        "enabled": True,
        "id_prefixes": ["tt"],
        "catalogs": [
            {"type": "movie", "id": "top", "name": "Popular"},
            {"type": "movie", "id": "imdbRating", "name": "IMDb Rating"},
            {"type": "series", "id": "top", "name": "Popular"},
            {"type": "series", "id": "imdbRating", "name": "IMDb Rating"}
        ]
    },
    {
        "id": "org.stremio.tmdb",
        "name": "The Movie Database Addon",
        "version": "1.0.0",
        "description": "Catalogs for Movies and Series from TMDB. Fast updates.",
        "manifest_url": "https://tmdb.strem.fun/manifest.json",
        "enabled": True,
        "id_prefixes": ["tmdb", "tt"],
        "catalogs": [
            {"type": "movie", "id": "tmdb.trending", "name": "Trending"},
            {"type": "movie", "id": "tmdb.popular", "name": "Popular"},
            {"type": "series", "id": "tmdb.trending", "name": "Trending"},
            {"type": "series", "id": "tmdb.popular", "name": "Popular"}
        ]
    }
]

def _ensure_db():
    if not CONFIG_DIR.exists():
        CONFIG_DIR.mkdir(parents=True)
    if not DB_FILE.exists():
        with open(DB_FILE, "w") as f:
            json.dump({"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}, f, indent=4)

def _read_db():
    with _db_lock:
        _ensure_db()
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
            # Migrate older databases
            if "history" not in data:
                data["history"] = []
            if "downloads" not in data:
                data["downloads"] = []
            if "settings" not in data:
                data["settings"] = {}
            if "addons" not in data:
                data["addons"] = DEFAULT_ADDONS
            else:
                # Ensure all default/bundled addons are present in the user database
                migrated = False
                for default_addon in DEFAULT_ADDONS:
                    found = False
                    for a in data["addons"]:
                        if a.get("id") == default_addon["id"]:
                            found = True
                            # Migrate missing catalogs/id_prefixes to existing addons
                            if "catalogs" in default_addon and "catalogs" not in a:
                                a["catalogs"] = default_addon["catalogs"]
                                migrated = True
                            if "id_prefixes" in default_addon and "id_prefixes" not in a:
                                a["id_prefixes"] = default_addon["id_prefixes"]
                                migrated = True
                            break
                    if not found:
                        data["addons"].append(default_addon)
                        migrated = True
                if migrated:
                    _write_db(data)
            return data
        except Exception:
            return {"favorites": [], "watched": [], "history": [], "downloads": [], "settings": {}, "addons": DEFAULT_ADDONS}

def _write_db(data):
    with _db_lock:
        _ensure_db()
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)

# --- Favorites ---

def get_favorites():
    return _read_db().get("favorites", [])

def add_favorite(item):
    db = _read_db()
    if not any(f.get("id") == item.get("id") for f in db.get("favorites", [])):
        db.setdefault("favorites", []).insert(0, item)
        _write_db(db)

def remove_favorite(item_id):
    db = _read_db()
    db["favorites"] = [f for f in db.get("favorites", []) if f.get("id") != item_id]
    _write_db(db)

def is_favorite(item_id):
    return any(f.get("id") == item_id for f in _read_db().get("favorites", []))

# --- Watched ---

def get_watched():
    return _read_db().get("watched", [])

def add_watched(item):
    db = _read_db()
    watched = db.setdefault("watched", [])
    watched = [w for w in watched if w.get("id") != item.get("id")]
    watched.insert(0, item)
    db["watched"] = watched
    _write_db(db)

def remove_watched(item_id):
    db = _read_db()
    db["watched"] = [f for f in db.get("watched", []) if f.get("id") != item_id]
    _write_db(db)

def is_watched(item_id):
    return any(f.get("id") == item_id for f in _read_db().get("watched", []))

# --- History ---

def get_history():
    return _read_db().get("history", [])

def add_history(item):
    """Add item to top of history, moving it if already present. Capped at HISTORY_LIMIT."""
    db = _read_db()
    history = db.setdefault("history", [])
    # Remove existing entry if present (so it moves to the top)
    history = [h for h in history if h.get("id") != item.get("id")]
    history.insert(0, item)
    # Enforce cap
    db["history"] = history[:HISTORY_LIMIT]
    _write_db(db)

def clear_history():
    db = _read_db()
    db["history"] = []
    _write_db(db)

def remove_history(item_id):
    db = _read_db()
    db["history"] = [h for h in db.get("history", []) if h.get("id") != item_id]
    _write_db(db)

# --- Downloads ---

def get_downloads():
    return _read_db().get("downloads", [])

def add_download(info_hash, name, magnet, file_index=None, item_id=None, media_type=None, season=None, episode=None):
    db = _read_db()
    downloads = db.setdefault("downloads", [])
    
    for d in downloads:
        if d.get("info_hash") == info_hash:
            if file_index is not None:
                d["file_index"] = file_index
            if name and name != "Fetching metadata...":
                d["name"] = name
            if item_id is not None:
                d["item_id"] = item_id
            if media_type is not None:
                d["media_type"] = media_type
            if season is not None:
                d["season"] = season
            if episode is not None:
                d["episode"] = episode
            _write_db(db)
            return

    downloads.insert(0, {
        "info_hash": info_hash,
        "name": name,
        "magnet": magnet,
        "paused": False,
        "file_index": file_index,
        "item_id": item_id,
        "media_type": media_type,
        "season": season,
        "episode": episode
    })
    _write_db(db)

def set_download_paused(info_hash, paused):
    db = _read_db()
    for d in db.get("downloads", []):
        if d.get("info_hash") == info_hash:
            d["paused"] = paused
    _write_db(db)

def set_download_finished(info_hash, finished):
    db = _read_db()
    for d in db.get("downloads", []):
        if d.get("info_hash") == info_hash:
            d["finished"] = finished
    _write_db(db)

def remove_download(info_hash):
    db = _read_db()
    db["downloads"] = [d for d in db.get("downloads", []) if d.get("info_hash") != info_hash]
    _write_db(db)

# --- Settings ---

def get_setting(key, default=None):
    return _read_db().get("settings", {}).get(key, default)

def set_setting(key, value):
    db = _read_db()
    settings = db.setdefault("settings", {})
    settings[key] = value
    _write_db(db)

# --- Progress ---

def save_progress(key, position):
    db = _read_db()
    progress = db.setdefault("progress", {})
    progress[key] = position
    _write_db(db)

def get_progress(key):
    return _read_db().get("progress", {}).get(key, 0)


# --- Addons ---

def get_addons():
    return _read_db().get("addons", [])

def add_addon(addon):
    db = _read_db()
    addons = db.setdefault("addons", [])
    # Remove existing addon with same ID or manifest_url
    addons = [a for a in addons if a.get("id") != addon.get("id") and a.get("manifest_url") != addon.get("manifest_url")]
    addons.append(addon)
    db["addons"] = addons
    _write_db(db)

def remove_addon(addon_id):
    db = _read_db()
    db["addons"] = [a for a in db.get("addons", []) if a.get("id") != addon_id]
    _write_db(db)

def set_addon_enabled(addon_id, enabled):
    db = _read_db()
    for a in db.get("addons", []):
        if a.get("id") == addon_id:
            a["enabled"] = enabled
    _write_db(db)

