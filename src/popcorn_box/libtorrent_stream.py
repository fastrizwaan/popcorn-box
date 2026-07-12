import json
import os
import re
import socket
import threading
import time
import urllib.parse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import database


EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "http://tracker.openbittorrent.com:80/announce",
]


def info_hash_from_magnet(magnet):
    match = re.search(r"urn:btih:([a-fA-F0-9]{40})", magnet, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    match = re.search(r"urn:btih:([A-Z2-7]{32})", magnet)
    return match.group(1).lower() if match else None


def add_trackers_to_magnet(magnet):
    if not magnet.startswith("magnet:?"):
        return magnet
    for tracker in EXTRA_TRACKERS:
        encoded = urllib.parse.quote(tracker, safe="")
        if encoded not in magnet:
            magnet += f"&tr={encoded}"
    return magnet


class TorrentStreamEngine:
    def __init__(self, magnet_link, download_base, file_index=None, item_id=None, media_type=None, season=None, episode=None):
        self.magnet_link = magnet_link
        self.info_hash = info_hash_from_magnet(magnet_link)
        self.download_base = download_base
        self.file_index = file_index
        self.item_id = item_id
        self.media_type = media_type
        self.season = season
        self.episode = episode
        self.session_dir = os.path.join(download_base, self.info_hash or "unknown")
        self.resume_path = os.path.join(self.session_dir, ".popcorn-box.fastresume")
        self.torrent_path = os.path.join(self.session_dir, f"{self.info_hash}.torrent")

        self.lt = None
        self.session = None
        self.handle = None
        self.httpd = None
        self.http_thread = None
        self.stream_url = None

        self.lock = threading.RLock()
        self.ready_event = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.target = None
        self.last_priority_window = []
        self._stream_gen = 0  # incremented on each new /stream request

    def start(self):
        os.makedirs(self.session_dir, exist_ok=True)
        try:
            import libtorrent as lt
        except Exception as exc:
            raise RuntimeError("python-libtorrent is not installed") from exc

        self.lt = lt
        self.session = self._create_session()
        self.handle = self._add_torrent()
        
        # Try to parse name from magnet for immediate display
        parsed = urllib.parse.urlparse(self.magnet_link)
        qs = urllib.parse.parse_qs(parsed.query)
        initial_name = qs.get("dn", ["Fetching metadata..."])[0]
        database.add_download(self.info_hash, initial_name, self.magnet_link, self.file_index, self.item_id, self.media_type, getattr(self, 'season', None), getattr(self, 'episode', None))
        
        self._start_http_server()
        threading.Thread(target=self._metadata_worker, daemon=True).start()
        self._start_monitor()
        return self.stream_url

    def stop(self):
        self.stopped.set()
        self._save_resume_data(timeout=6)
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        if self.session:
            try:
                self.session.pause()
            except Exception:
                pass
            self.session = None
        self.lt = None

    def pause(self):
        if self.handle:
            try:
                self.handle.pause()
            except Exception:
                pass

    def resume(self):
        if self.handle:
            try:
                self.handle.resume()
            except Exception:
                pass

    def is_alive(self):
        return not self.stopped.is_set() and self.http_thread is not None and self.http_thread.is_alive()

    def stats(self):
        with self.lock:
            if self.error:
                return {"ready": False, "status": f"Error: {self.error}"}

            if not self.handle:
                return {"ready": False, "status": "Initializing torrent engine..."}

            status = self._status()
            has_metadata = bool(getattr(status, "has_metadata", False))
            dl = getattr(status, "all_time_download", 0)
            ul = getattr(status, "all_time_upload", 0)
            
            data = {
                "name": getattr(status, "name", "") or "",
                "infoHash": self.info_hash or "",
                "filePath": self.target["path"] if self.target else "",
                "ready": self.ready_event.is_set(),
                "progress": 0,
                "downloaded": 0,
                "bufferedFromStart": 0,
                "uploadSpeed": float(getattr(status, "upload_rate", 0) or 0),
                "downloadSpeed": float(getattr(status, "download_rate", 0) or 0),
                "totalLength": self.target["size"] if self.target else 0,
                "activePeers": int(getattr(status, "num_peers", 0) or 0),
                "totalPeers": int(getattr(status, "num_peers", 0) or 0),
                "seeds": int(getattr(status, "num_seeds", 0) or 0),
                "ratio": float(ul / dl) if dl > 0 else 0.0,
                "status": self._state_text(status, has_metadata),
            }

            if self.target:
                downloaded = self._target_downloaded()
                
                # Force 100% if libtorrent state indicates completion
                state_str = str(getattr(status, "state", "")).lower()
                if "finished" in state_str or "seeding" in state_str:
                    downloaded = self.target["size"]
                    
                data["downloaded"] = downloaded
                data["progress"] = downloaded / self.target["size"] if self.target["size"] else 0
                data["bufferedFromStart"] = self._buffered_from_start()
            else:
                data["progress"] = getattr(status, "progress", 0)

            return data

    def wait_ready(self, timeout=None):
        return self.ready_event.wait(timeout)

    def media_url(self):
        return f"{self.stream_url}/stream?idx={self.file_index}" if self.stream_url else None

    def _create_session(self):
        lt = self.lt
        try:
            settings = {
                "listen_interfaces": "0.0.0.0:0",
                "user_agent": "Transmission 4.1.2",
                "enable_outgoing_tcp": True,
                "enable_outgoing_utp": True,
                "enable_incoming_tcp": True,
                "enable_incoming_utp": True,
                "enable_dht": True,
                "enable_lsd": True,
                "enable_upnp": True,
                "enable_natpmp": True,
                "dht_bootstrap_nodes": "router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881",
                "connections_limit": 500,
                "active_downloads": 20,
            }
            return lt.session(settings)
        except Exception:
            session = lt.session()
            try:
                # Fallback for older libtorrent versions
                session.listen_on(0, 0)
                if hasattr(session, 'start_dht'):
                    session.start_dht()
                if hasattr(session, 'start_lsd'):
                    session.start_lsd()
                if hasattr(session, 'start_upnp'):
                    session.start_upnp()
                if hasattr(session, 'start_natpmp'):
                    session.start_natpmp()
                if hasattr(session, 'add_dht_router'):
                    session.add_dht_router("router.bittorrent.com", 6881)
                    session.add_dht_router("router.utorrent.com", 6881)
                    session.add_dht_router("dht.transmissionbt.com", 6881)
            except Exception:
                pass
            return session

    def _add_torrent(self):
        lt = self.lt
        magnet = add_trackers_to_magnet(self.magnet_link)
        atp = None
        if os.path.exists(self.resume_path):
            try:
                with open(self.resume_path, "rb") as f:
                    atp = lt.read_resume_data(f.read())
            except Exception:
                atp = None

        if atp is None:
            if os.path.exists(self.torrent_path):
                try:
                    info = lt.torrent_info(self.torrent_path)
                    atp = lt.add_torrent_params()
                    atp.ti = info
                    atp.save_path = self.session_dir
                except Exception:
                    atp = None
                    
        if atp is None:
            atp = lt.parse_magnet_uri(magnet)
            atp.save_path = self.session_dir

        try:
            if hasattr(lt, "storage_mode_t"):
                atp.storage_mode = lt.storage_mode_t.storage_mode_sparse
        except Exception:
            pass

        handle = self.session.add_torrent(atp)
        try:
            handle.set_sequential_download(True)
        except Exception:
            pass
        return handle
        
    def set_download_limit(self, limit_bytes):
        """Limit download speed (0 for unlimited)"""
        if hasattr(self, 'handle') and self.handle:
            try:
                self.handle.set_download_limit(int(limit_bytes))
            except Exception:
                pass

    def _start_http_server(self):
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_HEAD(self):
                self._handle_request(head_only=True)

            def do_GET(self):
                self._handle_request(head_only=False)

            def log_message(self, fmt, *args):
                return

            def _handle_request(self, head_only=False):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path in ("/.json", "/_stats"):
                    self._send_json(engine.stats())
                elif parsed.path in ("", "/"):
                    self.send_response(302)
                    self.send_header("Location", "/stream")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif parsed.path == "/stream":
                    engine.serve_stream(self, head_only=head_only)
                else:
                    self.send_error(404)

            def _send_json(self, data):
                payload = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self.httpd.server_address[1]
        self.stream_url = f"http://127.0.0.1:{port}"
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.http_thread.start()

    def _metadata_worker(self):
        try:
            while not self.stopped.is_set():
                status = self._status()
                if getattr(status, "has_metadata", False):
                    if not os.path.exists(self.torrent_path):
                        try:
                            info = self.handle.torrent_file()
                            if info:
                                with open(self.torrent_path, "wb") as f:
                                    f.write(self.lt.bencode(self.lt.create_torrent(info).generate()))
                        except Exception:
                            pass
                            
                    # We have metadata, so we can select the target file immediately, even if we are verifying existing files!
                    if True:
                        self._select_target_file()
                        try:
                            self.handle.set_sequential_download(True)
                        except Exception:
                            pass
                        self.ready_event.set()
                        
                        # Prioritize the first 5MB and last 5MB for fast player probing/parsing and sequential start
                        if self.target["size"] > 10 * 1024 * 1024:
                            self.prioritize_range(0, 5 * 1024 * 1024, clear_old=False)
                            self.prioritize_range(self.target["size"] - (5 * 1024 * 1024), self.target["size"] - 1, clear_old=False)
                        else:
                            self.prioritize_range(0, self.target["size"] - 1, clear_old=False)
                            
                        # Start strict sequential prefetcher for the front buffer
                        threading.Thread(target=self._background_prefetcher, daemon=True).start()
                        
                        # Record this download in the database
                        name = getattr(status, "name", "Unknown")
                        if isinstance(name, bytes):
                            name = name.decode('utf-8', 'replace')
                            
                        if hasattr(self, 'target') and "path" in self.target:
                            name = os.path.basename(self.target["path"])
                            
                        from . import database
                        database.add_download(self.info_hash, name, self.magnet_link, self.file_index, self.item_id, self.media_type, getattr(self, 'season', None), getattr(self, 'episode', None))
                        return
                time.sleep(0.25)
        except Exception as exc:
            self.error = exc
 
    def _background_prefetcher(self):
        """Forces strict sequential downloading of the first 50MB by sliding a tiny priority window."""
        target_size = min(50 * 1024 * 1024, self.target["size"])
        start_piece, end_piece = self._piece_range_for_file_bytes(0, target_size - 1)
        
        while not self.stopped.is_set():
            # If the HTTP stream is active, mpv is playing this file, so stop background prefetching.
            if getattr(self, '_stream_gen', 0) > 0:
                break
                
            first_missing = -1
            for p in range(start_piece, end_piece + 1):
                try:
                    if not self.handle.have_piece(p):
                        first_missing = p
                        break
                except Exception:
                    pass
                    
            if first_missing != -1:
                # Prioritize next 4 pieces (~1MB to 4MB) to max, everything else to normal
                for p in range(start_piece, end_piece + 1):
                    try:
                        if first_missing <= p <= first_missing + 3:
                            self.handle.piece_priority(p, 7)
                        else:
                            if not self.handle.have_piece(p):
                                self.handle.piece_priority(p, 4)
                    except Exception:
                        pass
            else:
                break # First 50MB is fully downloaded, prefetch complete!
                
            time.sleep(1.0)

    def _start_monitor(self):
        def monitor_worker():
            from . import database
            while not self.stopped.is_set():
                try:
                    stats = self.stats()
                    if stats.get("progress", 0) >= 1.0:
                        database.set_download_finished(self.info_hash, True)
                    # We no longer stop engines based on seed ratio as requested.
                except Exception:
                    pass
                time.sleep(10)
        threading.Thread(target=monitor_worker, daemon=True).start()

    def _status(self):
        try:
            return self.handle.status()
        except Exception:
            return type("Status", (), {})()

    def _state_text(self, status, has_metadata):
        if not has_metadata:
            return "Fetching metadata..."
        if not self.ready_event.is_set():
            return "Preparing selected file..."
        state = getattr(status, "state", None)
        if state is None:
            return "Downloading"
        return str(state).split(".")[-1].replace("_", " ").title()

    def _select_target_file(self):
        with self.lock:
            files = self._files()
            if not files:
                raise RuntimeError("Torrent metadata has no files")

            idx = self.file_index if self.file_index is not None else None
            
            if idx is None and getattr(self, 'season', None) is not None and getattr(self, 'episode', None) is not None:
                try:
                    from . import api
                    files_data = [{"name": f["path"], "size": f["size"]} for f in files]
                    found = api.find_episode_file_index(files_data, self.season, self.episode)
                    if found is not None:
                        idx = found
                except Exception:
                    pass

            if idx is None or idx < 0 or idx >= len(files):
                idx = max(range(len(files)), key=lambda i: files[i]["size"])
            self.file_index = idx
            self.target = files[idx]

            priorities = [0] * len(files)
            for i, f in enumerate(files):
                if f["size"] < 20 * 1024 * 1024:  # Download small files (<20MB) to prevent partfile hash issues
                    priorities[i] = 1
            priorities[idx] = 4
            try:
                self.handle.prioritize_files(priorities)
            except Exception:
                for file_idx, priority in enumerate(priorities):
                    try:
                        self.handle.file_priority(file_idx, priority)
                    except Exception:
                        pass
                        
    def switch_target_file(self, new_file_index):
        if new_file_index is not None:
            new_file_index = int(new_file_index)
        with self.lock:
            if self.file_index == new_file_index:
                return
            self.file_index = new_file_index
            if self.ready_event.is_set():
                self._select_target_file()
                if self.target and self.target.get("size", 0) > 0:
                    if self.target["size"] > 10 * 1024 * 1024:
                        self.prioritize_range(0, 5 * 1024 * 1024, clear_old=True)
                        self.prioritize_range(self.target["size"] - (5 * 1024 * 1024), self.target["size"] - 1, clear_old=False)
                    else:
                        self.prioritize_range(0, self.target["size"] - 1, clear_old=True)
            self._stream_gen += 1
            
    def prefetch_additional_file(self, file_index):
        if not self.handle: return
        try:
            files = self._files()
            if file_index < 0 or file_index >= len(files): return
            f = files[file_index]
            try:
                self.handle.file_priority(file_index, 1) # Start with low priority!
            except Exception:
                pass
            threading.Thread(target=self._additional_file_prefetcher, args=(f,), daemon=True).start()
        except Exception:
            pass
            
    def _additional_file_prefetcher(self, target_dict):
        """Forces strict sequential downloading of the first 50MB and last 2MB for an additional file."""
        target_size = target_dict["size"]
        
        # Prioritize last 2MB once
        if target_size > 2 * 1024 * 1024:
            piece_length = self._piece_length()
            absolute_start = target_dict["offset"] + (target_size - 2 * 1024 * 1024)
            absolute_end = target_dict["offset"] + target_size - 1
            start_p = absolute_start // piece_length
            end_p = absolute_end // piece_length
            for p in range(start_p, end_p + 1):
                try: self.handle.piece_priority(p, 7)
                except: pass
                
        # Slide window for first 50MB
        front_target_size = min(50 * 1024 * 1024, target_size)
        start_piece = target_dict["offset"] // self._piece_length()
        end_piece = (target_dict["offset"] + front_target_size - 1) // self._piece_length()
        
        upgraded = False
        
        while not self.stopped.is_set():
            if self.file_index == target_dict["index"] and getattr(self, '_stream_gen', 0) > 0:
                break # foreground player took over this exact file
                
            main_finished = False
            if getattr(self, 'target', None) and self.target.get("size", 0) > 0:
                if self._target_downloaded() >= self.target["size"]:
                    main_finished = True
                    
            if main_finished and not upgraded:
                upgraded = True
                try:
                    self.handle.file_priority(target_dict["index"], 4)
                except Exception:
                    pass
                    
            base_prio = 4 if main_finished else 1
            high_prio = 7 if main_finished else 2
                
            first_missing = -1
            for p in range(start_piece, end_piece + 1):
                try:
                    if not self.handle.have_piece(p):
                        first_missing = p
                        break
                except Exception: pass
                    
            if first_missing != -1:
                for p in range(start_piece, end_piece + 1):
                    try:
                        if first_missing <= p <= first_missing + 3:
                            self.handle.piece_priority(p, high_prio)
                        else:
                            if not self.handle.have_piece(p):
                                self.handle.piece_priority(p, base_prio)
                    except Exception: pass
            else:
                if main_finished:
                    break
            time.sleep(1.0)

    def _files(self):
        info = self.handle.torrent_file()
        storage = info.files()
        count = self._num_files(storage, info)
        files = []
        for idx in range(count):
            path = self._file_path(storage, info, idx)
            size = int(self._file_size(storage, info, idx))
            offset = int(self._file_offset(storage, info, idx))
            files.append({"index": idx, "path": path.replace("\\", "/"), "size": size, "offset": offset})
        return files

    def _num_files(self, storage, info):
        for obj in (storage, info):
            for name in ("num_files", "files"):
                attr = getattr(obj, name, None)
                if callable(attr):
                    try:
                        value = attr()
                        if isinstance(value, int):
                            return value
                    except Exception:
                        pass
        return len(storage)

    def _file_path(self, storage, info, idx):
        for obj in (storage, info):
            attr = getattr(obj, "file_path", None)
            if callable(attr):
                try:
                    return attr(idx)
                except Exception:
                    pass
        entry = storage.at(idx) if hasattr(storage, "at") else storage[idx]
        return entry.path

    def _file_size(self, storage, info, idx):
        attr = getattr(storage, "file_size", None)
        if callable(attr):
            return attr(idx)
        entry = storage.at(idx) if hasattr(storage, "at") else storage[idx]
        return entry.size

    def _file_offset(self, storage, info, idx):
        attr = getattr(storage, "file_offset", None)
        if callable(attr):
            return attr(idx)
        entry = storage.at(idx) if hasattr(storage, "at") else storage[idx]
        return entry.offset

    def _piece_length(self):
        info = self.handle.torrent_file()
        return int(info.piece_length())

    def _piece_range_for_file_bytes(self, start, end):
        piece_length = self._piece_length()
        absolute_start = self.target["offset"] + start
        absolute_end = self.target["offset"] + end
        return absolute_start // piece_length, absolute_end // piece_length

    def prioritize_range(self, start, end, clear_old=True):
        if not self.target or end < start:
            return
        start_piece, end_piece = self._piece_range_for_file_bytes(start, end)
        deadline = 0
        selected = []
        
        # Clear old deadlines so libtorrent doesn't stay stuck on the old seek position
        if clear_old:
            for old_piece in self.last_priority_window:
                if old_piece < start_piece or old_piece > end_piece:
                    try:
                        self.handle.reset_piece_deadline(old_piece)
                    except Exception:
                        pass
                    try:
                        self.handle.piece_priority(old_piece, 4) # Reset to target file's base priority
                    except Exception:
                        pass
            self.last_priority_window = []

        for piece in range(start_piece, end_piece + 1):
            try:
                if self.handle.have_piece(piece):
                    continue
            except Exception:
                pass
                
            # The closer the piece is to start_piece, the higher its priority (7 to 1)
            # This ensures absolute strict sequential downloading for the requested window!
            dist = piece - start_piece
            prio = max(4, 7 - dist)
            
            try:
                self.handle.piece_priority(piece, prio)
            except Exception:
                pass
            try:
                self.handle.set_piece_deadline(piece, deadline)
            except Exception:
                pass
            selected.append(piece)
            deadline += 50
        self.last_priority_window.extend(selected)

    def _target_downloaded(self):
        if not self.target or not self.handle:
            return 0
            
        try:
            progress = self.handle.file_progress()
            downloaded = int(progress[self.target["index"]])
            if downloaded >= self.target["size"]:
                return self.target["size"]
        except Exception:
            pass
            
        try:
            status = self.handle.status()
            state_str = str(getattr(status, "state", "")).lower()
            
            if "finished" in state_str or "seeding" in state_str:
                return self.target["size"]
                
            # Only use physical block validation during 'checking' phase.
            # For new active downloads, pre-allocation can cause st_blocks to equal total size, 
            # leading to a false 100% read.
            if "checking" in state_str:
                path = self._target_path()
                if os.path.exists(path):
                    st = os.stat(path)
                    if hasattr(st, 'st_blocks'):
                        physical_size = st.st_blocks * 512
                        if physical_size >= self.target["size"]:
                            return self.target["size"]
        except Exception as e:
            pass
            
        return self._downloaded_by_piece_scan()

    def _downloaded_by_piece_scan(self):
        start_piece, end_piece = self._piece_range_for_file_bytes(0, self.target["size"] - 1)
        piece_length = self._piece_length()
        total = 0
        for piece in range(start_piece, end_piece + 1):
            try:
                if not self.handle.have_piece(piece):
                    continue
            except Exception:
                continue
            piece_start = piece * piece_length
            piece_end = piece_start + piece_length - 1
            file_start = self.target["offset"]
            file_end = self.target["offset"] + self.target["size"] - 1
            total += max(0, min(piece_end, file_end) - max(piece_start, file_start) + 1)
        return min(total, self.target["size"])

    def _buffered_from_start(self):
        if not self.target:
            return 0
        piece_length = self._piece_length()
        file_start = self.target["offset"]
        file_end = self.target["offset"] + self.target["size"] - 1
        start_piece = file_start // piece_length
        end_piece = file_end // piece_length
        buffered = 0
        for piece in range(start_piece, end_piece + 1):
            try:
                if not self.handle.have_piece(piece):
                    break
            except Exception:
                break
            piece_start = piece * piece_length
            piece_end = piece_start + piece_length - 1
            buffered += max(0, min(piece_end, file_end) - max(piece_start, file_start) + 1)
        return min(buffered, self.target["size"])

    def _has_range_downloaded(self, start, end):
        if not self.target or not self.handle:
            return False
        try:
            start_piece, end_piece = self._piece_range_for_file_bytes(start, end)
            for piece in range(start_piece, end_piece + 1):
                if not self.handle.have_piece(piece):
                    return False
            return True
        except Exception:
            return False

    def is_buffering_finished(self):
        if not self.target or not self.handle:
            return False
        total_size = self.target["size"]
        if total_size == 0:
            return False
        if self._target_downloaded() == total_size:
            return True
        # Small files: wait until fully downloaded
        if total_size <= 15 * 1024 * 1024:
            return self._target_downloaded() == total_size
        
        # Check first 5MB and last 5MB
        first_parts_done = self._has_range_downloaded(0, 5 * 1024 * 1024)
        last_parts_done = self._has_range_downloaded(total_size - 5 * 1024 * 1024, total_size - 1)
        
        # Check sequential buffer (at least 15MB or 1% of total size, capped at 50MB)
        min_seq = min(50 * 1024 * 1024, max(15 * 1024 * 1024, total_size * 0.01))
        sequential_done = self._buffered_from_start() >= min_seq
        
        return first_parts_done and last_parts_done and sequential_done

    def _target_path(self):
        rel_path = os.path.normpath(self.target["path"])
        abs_path = os.path.abspath(os.path.join(self.session_dir, rel_path))
        root = os.path.abspath(self.session_dir)
        if not abs_path.startswith(root + os.sep):
            raise RuntimeError("Unsafe torrent file path")
        return abs_path

    def serve_stream(self, request, head_only=False):
        try:
            if self.error:
                request.send_error(500, str(self.error))
                return
            if not self.ready_event.wait(120):
                request.send_error(503, "Torrent metadata is not ready")
                return

            # Bump generation: any older _write_range thread will see the
            # mismatch and exit on its next loop iteration.
            with self.lock:
                self._stream_gen += 1
                my_gen = self._stream_gen

            size = self.target["size"]
            range_header = request.headers.get("Range", "")
            start, end, status = self._parse_range(range_header, size)
            if start is None:
                request.send_response(416)
                request.send_header("Content-Range", f"bytes */{size}")
                request.send_header("Content-Length", "0")
                request.end_headers()
                return

            headers = {
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Content-Length": str(end - start + 1),
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(os.path.basename(self.target['path']))}",
            }
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"

            request.send_response(status)
            request.send_header("Access-Control-Allow-Origin", "*")
            for name, value in headers.items():
                request.send_header(name, value)
            request.end_headers()
            if head_only:
                return

            self._write_range(request, start, end, my_gen)
        except (BrokenPipeError, ConnectionResetError, socket.error):
            pass  # client disconnected, normal during seek
        except Exception as e:
            import traceback
            print(f"serve_stream error: {e}\n{traceback.format_exc()}")

    def _parse_range(self, range_header, size):
        if not range_header:
            return 0, size - 1, 200
        
        # Clean up the header string
        range_header = range_header.strip()
        match = re.search(r"bytes=(\d*)-(\d*)", range_header)
        
        if not match:
            return 0, size - 1, 200
            
        start_str, end_str = match.groups()
        
        if not start_str:
            suffix = int(end_str) if end_str else 0
            if suffix <= 0:
                return None, None, None
            return max(size - suffix, 0), size - 1, 206
            
        start = int(start_str)
        end = int(end_str) if end_str else size - 1
        
        if start > end or start >= size:
            return None, None, None
            
        return start, min(end, size - 1), 206

    def _write_range(self, request, start, end, gen):
        path = self._target_path()
        pos = start
        chunk_size = 256 * 1024
        while pos <= end and not self.stopped.is_set():
            # If a newer request arrived (mpv seeked), abort immediately
            if self._stream_gen != gen:
                return

            seek_window_end = min(end, pos + (16 * 1024 * 1024) - 1)
            self.prioritize_range(pos, seek_window_end)

            chunk_end = min(end, pos + chunk_size - 1)
            if not self._wait_for_range(pos, chunk_end, timeout=60, gen=gen):
                return

            try:
                with open(path, "rb") as stream_file:
                    stream_file.seek(pos)
                    data = stream_file.read(chunk_end - pos + 1)
            except FileNotFoundError:
                if self._stream_gen != gen:
                    return
                time.sleep(0.2)
                continue

            if not data:
                if self._stream_gen != gen:
                    return
                time.sleep(0.2)
                continue

            try:
                request.wfile.write(data)
                request.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.error):
                return
            pos += len(data)

    def _wait_for_range(self, start, end, timeout, gen=None):
        deadline = time.time() + timeout
        start_piece, end_piece = self._piece_range_for_file_bytes(start, end)
        self.prioritize_range(start, end, clear_old=False)
        last_prioritize = time.time()
        
        while time.time() < deadline and not self.stopped.is_set():
            # Abort if a newer stream request superseded us
            if gen is not None and self._stream_gen != gen:
                return False
            ready = True
            for piece in range(start_piece, end_piece + 1):
                try:
                    if not self.handle.have_piece(piece):
                        ready = False
                        break
                except Exception:
                    ready = False
                    break
            if ready:
                return True
                
            now = time.time()
            if now - last_prioritize >= 1.0:
                self.prioritize_range(start, end, clear_old=False)
                last_prioritize = now
                
            time.sleep(0.05)
        return False

    def _save_resume_data(self, timeout=6):
        if not self.handle or not self.lt:
            return
        try:
            flags = getattr(self.lt.resume_data_flags_t, "save_info_dict", 1)
            self.handle.save_resume_data(flags)
        except Exception:
            return

        end_time = time.time() + timeout
        while time.time() < end_time:
            alerts = []
            try:
                alert = self.session.wait_for_alert(500)
                if alert is not None:
                    alerts = self.session.pop_alerts()
            except Exception:
                try:
                    alerts = self.session.pop_alerts()
                except Exception:
                    pass

            for item in alerts:
                if item.__class__.__name__ == "save_resume_data_alert":
                    self._write_resume_alert(item)
                    return
                if item.__class__.__name__ == "save_resume_data_failed_alert":
                    return
            time.sleep(0.05)

    def _write_resume_alert(self, alert):
        try:
            if hasattr(self.lt, "write_resume_data_buf"):
                data = bytes(self.lt.write_resume_data_buf(alert.params))
            else:
                data = self.lt.bencode(self.lt.write_resume_data(alert.params))
            tmp_path = self.resume_path + ".tmp"
            with open(tmp_path, "wb") as resume_file:
                resume_file.write(data)
            os.replace(tmp_path, self.resume_path)
        except Exception as exc:
            print(f"Failed to save fast-resume data: {exc}")
