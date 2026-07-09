import json
import urllib.request
import urllib.parse
import os
import time
import hashlib
import logging
from . import database
import concurrent.futures
import threading

# Global semaphore: cap ALL outbound HTTP connections across the entire app
_net_sema = threading.Semaphore(8)

if os.environ.get("FLATPAK_ID"):
    cache_dir_base = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
    CACHE_DIR = os.path.join(cache_dir_base, 'popcorn-box', 'api')
else:
    CACHE_DIR = os.path.expanduser('~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box/api')
os.makedirs(CACHE_DIR, exist_ok=True)

def _get_cached_request(url, max_age_hours=2, headers=None, cache_only=False):
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(CACHE_DIR, url_hash)
    
    if headers is None:
        headers = {'User-Agent': 'Mozilla/5.0'}
    
    # Check if cache exists and is fresh
    if os.path.exists(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < max_age_hours:
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.debug(f"Cache corrupted, falling back to fetch: {e}")
                
    if cache_only:
        return None
        
    # Fetch from network
    try:
        with _net_sema:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                data_str = response.read().decode('utf-8')
        data = json.loads(data_str)
        # Save to cache
        with open(cache_file, 'w', encoding='utf-8') as f:
            f.write(data_str)
        return data
    except Exception as e:
        print(f"Error fetching items from {url}: {e}")
        # Return stale cache if network fails
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logging.debug(f"Failed to read stale cache: {e}")
    return None

def fetch_genre_counts(media_type="movie"):
    return {}

def fetch_items(media_type="movie", query="", genre="", catalog_id="top", catalog_url=None, limit=50, page=1, cache_only=False):
    c_type = "series" if media_type in ["series", "anime"] else "movie"
    skip = (page - 1) * 100

    if query:
        # Search all enabled addons for a search catalog
        items = []
        seen_ids = set()
        for addon in database.get_addons():
            if not addon.get("enabled", True): continue
            m_url = addon.get("manifest_url", "")
            if not m_url or m_url.startswith("builtin:"): continue
            
            base_url = m_url.rsplit("manifest.json", 1)[0]
            if not base_url.endswith("/"): base_url += "/"
            
            # Simple fallback search using top catalog if addon supports search
            search_url = f"{base_url}catalog/{c_type}/top/search={urllib.parse.quote(query)}.json"
            data = _get_cached_request(search_url, max_age_hours=2, cache_only=cache_only)
            
            if data and "metas" in data:
                for m in data["metas"]:
                    imdb_id = m.get("imdb_id") or m.get("id")
                    if imdb_id and imdb_id not in seen_ids:
                        seen_ids.add(imdb_id)
                        items.append({
                            "id": imdb_id,
                            "title": m.get("name", ""),
                            "year": str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else "",
                            "medium_cover_image": m.get("poster", ""),
                            "type": media_type
                        })
        return items

    if catalog_url:
        base_url = catalog_url
        if "manifest.json" in base_url:
            base_url = base_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"):
            base_url += "/"
            
        url = f"{base_url}catalog/{c_type}/{catalog_id}"
        
        extras = []
        if genre and genre != "All":
            extras.append(f"genre={urllib.parse.quote(genre)}")
        if skip > 0:
            extras.append(f"skip={skip}")
            
        if extras:
            url += "/" + "&".join(extras) + ".json"
        else:
            url += ".json"
            
        data = _get_cached_request(url, max_age_hours=2, cache_only=cache_only)
        if data and "metas" in data:
            movies = []
            for m in data["metas"]:
                imdb_id = m.get("imdb_id") or m.get("id")
                poster = m.get("poster", "")
                title = m.get("name", "")
                year = str(m.get("releaseInfo", "")).split("-")[0] if m.get("releaseInfo") else ""
                movies.append({
                    "id": imdb_id,
                    "title": title,
                    "year": year,
                    "medium_cover_image": poster,
                    "type": media_type
                })
            return movies

    return []

def fetch_movie_details(imdb_id, media_type="movie"):
    c_type = "series" if media_type in ["series", "anime"] else "movie"
    
    for addon in database.get_addons():
        if not addon.get("enabled", True): continue
        m_url = addon.get("manifest_url", "")
        if not m_url or m_url.startswith("builtin:"): continue
        
        base_url = m_url.rsplit("manifest.json", 1)[0]
        if not base_url.endswith("/"): base_url += "/"
        
        meta_url = f"{base_url}meta/{c_type}/{imdb_id}.json"
        data = _get_cached_request(meta_url, max_age_hours=168)
        
        if data and data.get("meta"):
            cm = data["meta"]
            videos = []
            for v in cm.get("videos", []):
                videos.append({
                    "season": v.get("season", 1),
                    "episode": v.get("episode", 1),
                    "title": v.get("title", ""),
                    "overview": v.get("overview", "")
                })
            
            return {
                "id": imdb_id,
                "title": cm.get("name", ""),
                "year": str(cm.get("releaseInfo", "")).split("-")[0] if cm.get("releaseInfo") else "",
                "medium_cover_image": cm.get("poster", ""),
                "background": cm.get("background", ""),
                "description": cm.get("description", "No synopsis available."),
                "runtime": cm.get("runtime", ""),
                "genre": ", ".join(cm.get("genres", [])),
                "imdbRating": str(cm.get("imdbRating", "")),
                "trailer": cm.get("trailers", [{"source": ""}])[0].get("source") if cm.get("trailers") else None,
                "videos": videos
            }
            
    return {}

def find_episode_file_index(files, season, episode):
    import re
    patterns = [
        rf"s{season:02d}e{episode:02d}",
        rf"s{season}e{episode}",
        rf"{season}x{episode:02d}",
        rf"{season}x{episode}",
        rf"ep(?:isode)?\s*{episode:02d}\b",
        rf"ep(?:isode)?\s*{episode}\b",
        rf"\b{episode:02d}\b"
    ]
    
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if not any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']): continue
        
        for p in patterns[:4]:
            if re.search(p, fname): return idx
                
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if not any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']): continue
            
        for p in patterns[4:]:
            if re.search(p, fname): return idx
                
    video_files = []
    for idx, f in enumerate(files):
        fname_list = f.get("name")
        if not fname_list: continue
        fname = fname_list[0].lower() if isinstance(fname_list, list) else str(fname_list).lower()
        if any(fname.endswith(ext) for ext in ['.mkv', '.mp4', '.avi', '.m4v']):
            size_list = f.get("size")
            size = size_list[0] if isinstance(size_list, list) else (int(size_list) if size_list is not None else 0)
            video_files.append((idx, size))
    if video_files:
        return max(video_files, key=lambda x: x[1])[0]
        
    return None

def get_torrents(imdb_id, media_type="movie", season=None, episode=None):
    if not imdb_id:
        return []
        
    actual_media = "series" if media_type in ["series", "anime"] else media_type
    
    addons = [a for a in database.get_addons() if a.get("enabled", True)]
    if not addons:
        return []
        
    stremio_addons = [a for a in addons if not a.get("manifest_url", "").startswith("builtin://")]
    
    def fetch_from_addon(addon):
        manifest_url = addon.get("manifest_url", "")
        if "manifest.json" in manifest_url:
            base_url = manifest_url.rsplit('manifest.json', 1)[0]
        else:
            base_url = manifest_url
        if not base_url.endswith('/'):
            base_url += '/'
            
        if actual_media == "series" and season is not None and episode is not None:
            url = f"{base_url}stream/series/{imdb_id}:{season}:{episode}.json"
        else:
            url = f"{base_url}stream/movie/{imdb_id}.json"
            
        try:
            with _net_sema:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=8) as response:
                    data = json.loads(response.read().decode('utf-8'))
            return addon.get("name", "Unknown"), data.get("streams", [])
        except Exception as e:
            print(f"Error fetching from addon {addon.get('name')}: {e}")
            return addon.get("name", "Unknown"), []
            
    all_streams = []
    num_workers = len(stremio_addons)
    if num_workers > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_addon = {executor.submit(fetch_from_addon, addon): addon for addon in stremio_addons}
                
            try:
                for future in concurrent.futures.as_completed(future_to_addon, timeout=10):
                    try:
                        addon_name, streams = future.result()
                        if streams:
                            for s in streams:
                                s["addon_name"] = addon_name
                                all_streams.append(s)
                    except Exception as e:
                        print(f"Error in addon future: {e}")
            except concurrent.futures.TimeoutError:
                print("Timeout fetching streams from some addons")
            
    if not all_streams:
        return []
        
    import re
    valid_streams = []
    seen_hashes = set()
    for s in all_streams:
        if not s.get("infoHash"):
            continue
            
        info_hash = s["infoHash"].lower()
        if info_hash in seen_hashes:
            for vs in valid_streams:
                if vs["hash"].lower() == info_hash:
                    if s.get("addon_name") and s["addon_name"] not in vs["addon_names"]:
                        vs["addon_names"].append(s["addon_name"])
                    break
            continue
        seen_hashes.add(info_hash)
        
        title_str = s.get("title", "")
        name_and_title = (s.get("name", "") + " " + title_str)
        
        quality = "Unknown"
        q_val = 0
        lower_name = name_and_title.lower()
        
        if "2160p" in lower_name:
            quality = "4K"
            q_val = 4
        elif "1080p" in lower_name: 
            quality = "1080p"
            q_val = 3
        elif "720p" in lower_name: 
            quality = "720p"
            q_val = 2
        elif "480p" in lower_name:
            quality = "480p"
            q_val = 1
        elif re.search(r'\b4k\b', lower_name): 
            quality = "4K"
            q_val = 4
        
        size = ""
        size_match = re.search(r'([\d.]+)\s*(GB|MB)', title_str, re.IGNORECASE)
        if size_match:
            size = f"{size_match.group(1)} {size_match.group(2).upper()}"
            
        seeders = 0
        seed_match = re.search(r'👤\s*(\d+)', title_str)
        if seed_match:
            try:
                seeders = int(seed_match.group(1))
            except ValueError:
                pass
            
        behavior_hints = s.get("behaviorHints", {})
        filename = behavior_hints.get("filename") or behavior_hints.get("videoFilename")
        if not filename:
            filename = title_str.split('\n')[0] if '\n' in title_str else ""
        if not filename:
            filename = name_and_title.replace('/', '_')
            
        valid_streams.append({
            "hash": s["infoHash"],
            "quality": quality,
            "q_val": q_val,
            "size": size,
            "seeders": seeders,
            "title": s.get("name", ""),
            "file_index": s.get("fileIdx"),
            "filename": filename,
            "addon_names": [s.get("addon_name")] if s.get("addon_name") else []
        })
    
    valid_streams.sort(key=lambda x: (x["q_val"], x["seeders"]), reverse=True)
    return valid_streams

def get_subtitles(imdb_id, media_type="movie", season=None, episode=None):
    if not imdb_id:
        return []
        
    actual_media = "series" if media_type in ["series", "anime"] else media_type
    if actual_media == "series" and season is not None and episode is not None:
        url = f"https://opensubtitles-v3.strem.io/subtitles/series/{imdb_id}:{season}:{episode}.json"
    else:
        url = f"https://opensubtitles-v3.strem.io/subtitles/movie/{imdb_id}.json"
        
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            subs = data.get("subtitles", [])
            
            eng_subs = [s for s in subs if s.get("lang", "").lower() in ["eng", "en", "english"]]
            return eng_subs
    except Exception as e:
        print(f"Error fetching subtitles: {e}")
        
    return []

def download_subtitle(sub_url, filename):
    import gi
    gi.require_version('GLib', '2.0')
    from gi.repository import GLib
    download_dir = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    if not download_dir:
        download_dir = os.path.expanduser("~/Downloads")
        os.makedirs(download_dir, exist_ok=True)
        
    if not filename.endswith('.srt'):
        filename += '.srt'
        
    filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
    file_path = os.path.join(download_dir, filename)
    
    try:
        req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            with open(file_path, 'wb') as f:
                f.write(response.read())
        return file_path
    except Exception as e:
        print(f"Error downloading subtitle: {e}")
        return None

def download_subtitle_to_path(sub_url, file_path):
    dir_name = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    base_name = "".join([c for c in base_name if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.', '(', ')', '[', ']')]).rstrip()
    
    file_path = os.path.join(dir_name, base_name)
    os.makedirs(dir_name, exist_ok=True)
    
    try:
        req = urllib.request.Request(sub_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            with open(file_path, 'wb') as f:
                f.write(response.read())
        return file_path
    except Exception as e:
        print(f"Error downloading subtitle: {e}")
        return None

def build_magnet(hash_string, title):
    title = title or ""
    encoded_title = urllib.parse.quote(title)
    trackers = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://tracker.openbittorrent.com:80/announce",
        "udp://tracker.torrent.eu.org:451/announce"
    ]
    tracker_str = "&tr=".join([urllib.parse.quote(t) for t in trackers])
    return f"magnet:?xt=urn:btih:{hash_string}&dn={encoded_title}&tr={tracker_str}"
