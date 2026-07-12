import subprocess
import os
import threading
import re
import atexit
import shutil
import time

from .libtorrent_stream import TorrentStreamEngine

# Global state for the streaming engines
_engines = {}
_streaming_hash = None
_engines_lock = threading.Lock()

_trailer_process = None
_trailer_lock = threading.Lock()

import os
DOWNLOAD_BASE = os.path.expanduser('~/io.github.fastrizwaan.PopcornBox/data/torrents')
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

def stop_player(keep_downloading=False):
    """Stop the currently streaming video, unless it's fully downloaded or keep_downloading is True."""
    global _streaming_hash, _trailer_process
    from . import database
    
    with _engines_lock:
        if _streaming_hash:
            engine = _engines.get(_streaming_hash)
            if engine and engine.is_alive():
                stats = engine.stats()
                prog = stats.get("progress", 0)
                if prog < 1.0 and not keep_downloading:
                    print(f"Pausing partially downloaded engine: {_streaming_hash}")
                    if hasattr(engine, 'pause'):
                        import threading
                        threading.Thread(target=engine.pause, daemon=True).start()
                    database.set_download_paused(_streaming_hash, True)
                else:
                    print(f"Leaving engine running: {_streaming_hash} (progress={prog:.2f}, keep_downloading={keep_downloading})")
                    if prog >= 1.0:
                        database.set_download_finished(_streaming_hash, True)
            _streaming_hash = None

    with _trailer_lock:
        if _trailer_process:
            print("Stopping trailer process...")
            try:
                _trailer_process.terminate()
                _trailer_process.wait(timeout=5)
            except Exception:
                try:
                    _trailer_process.kill()
                except Exception:
                    pass
            _trailer_process = None

def stop_engine_explicit(info_hash):
    """Explicitly stop a background download."""
    from . import database
    with _engines_lock:
        eng = _engines.pop(info_hash, None)
        if eng:
            print(f"Explicitly stopping engine: {info_hash}")
            threading.Thread(target=eng.stop, daemon=True).start()
    database.set_download_paused(info_hash, True)

def exit_player():
    from . import database
    database.flush_progress()
    with _engines_lock:
        for info_hash, engine in _engines.items():
            try:
                engine.stop()
            except Exception:
                pass
        _engines.clear()

atexit.register(exit_player)

def init_background_downloads():
    # Do not start background engines if the user is already streaming a video
    with _engines_lock:
        if _streaming_hash is not None:
            return
            
    from . import database
    downloads = database.get_downloads()
    
    # Start up to 10 finished downloads, and limit active downloading to 5
    active_count = 0
    max_auto_start = 5
    finished_count = 0
    max_seeding = 10
    
    import time
    for d in downloads:
        if not d.get("paused", False):
            if d.get("finished", False):
                if finished_count < max_seeding:
                    download_magnet_background(d["magnet"], d.get("file_index"), d.get("item_id"), d.get("media_type"), d.get("season"), d.get("episode"))
                    finished_count += 1
                    time.sleep(10.0)
            elif active_count < max_auto_start:
                download_magnet_background(d["magnet"], d.get("file_index"), d.get("item_id"), d.get("media_type"), d.get("season"), d.get("episode"))
                active_count += 1
                time.sleep(10.0)

def get_player_cmd():
    import shutil
    import subprocess
    import os
    
    # Priority 1: Bundled Flatpak MPV
    if os.path.exists("/app/bin/mpv"):
        return ["/app/bin/mpv", "--force-window=immediate"]
        
    # Priority 2 & 3: Native System Players
    if shutil.which("mpv"):
        return ["mpv", "--force-window=immediate"]
    if shutil.which("vlc"):
        return ["vlc"]
        
    # Priority 4 & 5: External Flatpak Players
    if shutil.which("flatpak"):
        if subprocess.run(["flatpak", "info", "io.mpv.Mpv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return ["flatpak", "run", "io.mpv.Mpv", "--force-window=immediate"]
            
    return ["flatpak", "run", "org.videolan.VLC"]

def get_active_info_hash():
    """Return the info hash of the currently playing engine, or None."""
    with _engines_lock:
        return _streaming_hash

def get_engine_stats(info_hash):
    """Return the stats of the engine for the given info hash, or None."""
    with _engines_lock:
        engine = _engines.get(info_hash)
        if engine and engine.is_alive():
            return engine.stats()
    return None

def download_magnet_background(magnet_link, file_index=None, item_id=None, media_type=None, season=None, episode=None):
    """Launch libtorrent with the given magnet link in the background."""
    from .libtorrent_stream import info_hash_from_magnet
    from . import database
    
    info_hash = info_hash_from_magnet(magnet_link)
    if not info_hash: return
    
    with _engines_lock:
        engine = _engines.get(info_hash)
        if engine and engine.is_alive():
            database.set_download_paused(info_hash, False)
            if hasattr(engine, 'resume'):
                engine.resume()
            if item_id: engine.item_id = item_id
            if media_type: engine.media_type = media_type
            if season is not None: engine.season = season
            if episode is not None: engine.episode = episode
            if engine.file_index != file_index:
                if hasattr(engine, 'prefetch_additional_file'):
                    engine.prefetch_additional_file(file_index)
            return
            
    def launch():
        try:
            engine = TorrentStreamEngine(magnet_link, DOWNLOAD_BASE, file_index, item_id, media_type, season, episode)
            engine.start()
            
            # Throttle the background download to 2MB/s so it doesn't freeze the active stream
            if hasattr(engine, 'set_download_limit'):
                engine.set_download_limit(2 * 1024 * 1024)
                
            with _engines_lock:
                _engines[info_hash] = engine
            database.set_download_paused(info_hash, False)
            import time
            time.sleep(5.0) # Wait for fast-resume data to load
            if engine.is_alive():
                stats = engine.stats()
                if stats.get("progress", 0) >= 1.0 and stats.get("ratio", 0) >= 1.5:
                    print(f"Background engine {info_hash} already reached 1.5x ratio. Pausing.")
                    engine.stop()
                    database.set_download_paused(info_hash, True)
                
        except Exception as e:
            print(f"Error launching background download: {e}")

    threading.Thread(target=launch, daemon=True).start()

def play_magnet(magnet_link, player="mpv", progress_callback=None, file_index=None, item_id=None, media_type=None, season=None, episode=None):
    global _streaming_hash
    import gi
    from gi.repository import GLib
    from .libtorrent_stream import info_hash_from_magnet
    from . import database
    
    info_hash = info_hash_from_magnet(magnet_link)
    if not info_hash: return
    
    def launch_player_only(engine):
        if progress_callback: GLib.idle_add(progress_callback, {"status": "Launching media player..."})
        if not engine or not engine.is_alive():
            if progress_callback: GLib.idle_add(lambda: progress_callback({"status": "Engine died. Retrying...", "closed": True}))
            return
            
        # Remove any bandwidth limits when moving to foreground
        if hasattr(engine, 'set_download_limit'):
            engine.set_download_limit(0)
            
        media_url = engine.media_url()
        import glob, os.path
        stats = engine.stats()
        hash_dir = os.path.join(DOWNLOAD_BASE, engine.info_hash)
        sub_file = None
        
        if stats and stats.get("filePath"):
            target_path = os.path.join(hash_dir, stats.get("filePath"))
            expected_sub = os.path.splitext(target_path)[0] + ".srt"
            if os.path.exists(expected_sub):
                sub_file = expected_sub
                
            total_size = stats.get("totalLength", 0)
            if total_size > 0 and engine._target_downloaded() == total_size:
                media_url = target_path
                print(f"File is fully downloaded, playing local file directly: {media_url}")
                
        if not sub_file and os.path.exists(hash_dir):
            srts = glob.glob(os.path.join(glob.escape(hash_dir), '**', '*.srt'), recursive=True)
            if srts: sub_file = srts[0]
                
        if progress_callback:
            GLib.idle_add(progress_callback, {"status": "Playing!", "url": media_url, "sub_file": sub_file})
        
        def poll():
            while True:
                with _engines_lock:
                    eng = _engines.get(info_hash)
                if not eng or not eng.is_alive():
                    break
                if _streaming_hash != info_hash:
                    break
                stats = eng.stats()
                stats["status"] = "Downloading"
                if progress_callback: GLib.idle_add(progress_callback, stats)
                time.sleep(1)
                    
        threading.Thread(target=poll, daemon=True).start()

    stop_player()

    with _engines_lock:
        to_stop = [h for h in _engines if h != info_hash]
        for h in to_stop:
            eng = _engines[h]
            try:
                stats = eng.stats()
                
                # We want a max of 10 seeding background engines, otherwise we kill them to save memory
                active_count = sum(1 for e in _engines.values() if e.is_alive())
                if stats.get("progress", 0) >= 1.0 and active_count <= 10:
                    print(f"Leaving 100% seeded engine running: {h}")
                    continue
                
                if stats.get("progress", 0) >= 1.0:
                    print(f"Stopping 100% seeded engine to save resources (limit reached): {h}")
                
                print(f"Stopping background engine: {h}")
                threading.Thread(target=eng.stop, daemon=True).start()
                database.set_download_paused(h, True)
            except Exception:
                pass
            if h in _engines:
                del _engines[h]

        global _streaming_hash
        _streaming_hash = info_hash

    with _engines_lock:
        engine = _engines.get(info_hash)
        if engine and engine.is_alive():
            if file_index is None and season is not None and episode is not None:
                try:
                    if engine.ready_event.is_set() and hasattr(engine, '_files'):
                        from . import api
                        files_data = [{"name": f["path"], "size": f["size"]} for f in engine._files()]
                        found = api.find_episode_file_index(files_data, season, episode)
                        if found is not None:
                            file_index = found
                except Exception as e:
                    print(f"Error resolving file_index: {e}")

            if file_index is not None:
                file_index = int(file_index)
            if engine.file_index == file_index or file_index is None:
                print("Reusing existing libtorrent engine")
                engine.item_id = item_id
                engine.media_type = media_type
                engine.season = season
                engine.episode = episode
                _streaming_hash = info_hash
                database.set_download_paused(info_hash, False)
                if hasattr(engine, 'resume'):
                    engine.resume()
                def resume_stream(): launch_player_only(engine)
                threading.Thread(target=resume_stream, daemon=True).start()
                if progress_callback: GLib.idle_add(progress_callback, {"status": "Resuming stream..."})
                return
            else:
                if hasattr(engine, 'switch_target_file'):
                    print(f"Switching active file index to {file_index} in existing engine")
                    engine.item_id = item_id
                    engine.media_type = media_type
                    engine.season = season
                    engine.episode = episode
                    engine.switch_target_file(file_index)
                    _streaming_hash = info_hash
                    database.set_download_paused(info_hash, False)
                    if hasattr(engine, 'resume'):
                        engine.resume()
                    def resume_stream():
                        print("Phase 3: Waiting for playable buffer (existing engine)...")
                        for i in range(300):
                            with _engines_lock:
                                if _streaming_hash != info_hash: return
                            if engine.is_buffering_finished():
                                break
                            if progress_callback:
                                stats = engine.stats()
                                buffered = stats.get("bufferedFromStart", 0)
                                stats["status"] = f"Buffering... {buffered/(1024*1024):.1f} MB"
                                GLib.idle_add(progress_callback, stats)
                            time.sleep(1)
                        launch_player_only(engine)
                    threading.Thread(target=resume_stream, daemon=True).start()
                    if progress_callback: GLib.idle_add(progress_callback, {"status": "Resuming stream..."})
                    return
                else:
                    engine.stop()
                    del _engines[info_hash]
                    engine = None

    def launch(attempt=1):
        global _streaming_hash
        try:
            if progress_callback: GLib.idle_add(progress_callback, {"status": f"Initializing stream engine (Attempt {attempt})..."})
            engine = TorrentStreamEngine(magnet_link, DOWNLOAD_BASE, file_index, item_id, media_type, season, episode)
            engine.start()
            
            with _engines_lock:
                _engines[info_hash] = engine
                _streaming_hash = info_hash
            database.set_download_paused(info_hash, False)
                
            def monitor_ready():
                if progress_callback: GLib.idle_add(progress_callback, {"status": "Fetching metadata..."})
                while engine.is_alive() and not engine.wait_ready(timeout=1.0):
                    with _engines_lock:
                        if _engines.get(info_hash) != engine: return
                    if _streaming_hash != info_hash: return
                    stats = engine.stats()
                    if progress_callback: GLib.idle_add(progress_callback, stats)
                        
                with _engines_lock:
                    if _engines.get(info_hash) != engine or not engine.is_alive(): return
                if _streaming_hash != info_hash: return
                    
                print("Phase 3: Waiting for playable buffer...")
                last_nonzero_time = time.time()
                for i in range(300):
                    with _engines_lock:
                        if _engines.get(info_hash) != engine or not engine.is_alive(): return
                    if _streaming_hash != info_hash: return
                    
                    stats = engine.stats()
                    downloaded = stats.get("downloaded", 0)
                    buffered = stats.get("bufferedFromStart", downloaded)
                    
                    stats["status"] = "Verifying / Buffering..."
                    if progress_callback: GLib.idle_add(progress_callback, stats)
                        
                    if downloaded > 0 or buffered > 0: last_nonzero_time = time.time()
                    
                    if engine.is_buffering_finished():
                        break
                    if (time.time() - last_nonzero_time) > 30: break
                    time.sleep(1)
                    
                with _engines_lock:
                    if _engines.get(info_hash) != engine or not engine.is_alive(): return
                if _streaming_hash != info_hash: return
                launch_player_only(engine)
                
            threading.Thread(target=monitor_ready, daemon=True).start()
            
        except Exception as e:
            print(f"Error launching player: {e}")
            if progress_callback: GLib.idle_add(progress_callback, {"status": f"Error: {e}"})

    threading.Thread(target=launch, daemon=True).start()
    return None

def play_trailer(youtube_id, progress_callback=None):
    """Yield trailer URL for embedded playback."""
    stop_player()
    
    if youtube_id.startswith("http://") or youtube_id.startswith("https://"):
        url = youtube_id
    else:
        url = f"https://www.youtube.com/watch?v={youtube_id}"
    
    def launch():
        try:
            import gi
            from gi.repository import GLib
            if progress_callback:
                GLib.idle_add(lambda: progress_callback({"status": "Resolving YouTube link..."}))
                GLib.idle_add(lambda: progress_callback({"status": "Playing Trailer!", "url": url}))
            
        except Exception as e:
            print(f"Error launching trailer: {e}")
            if progress_callback:
                import gi
                from gi.repository import GLib
                GLib.idle_add(lambda: progress_callback({"status": f"Error: {e}", "closed": True}))
                
    threading.Thread(target=launch, daemon=True).start()
