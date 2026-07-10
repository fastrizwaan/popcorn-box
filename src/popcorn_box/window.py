import sys
import threading
import gi
import json
from . import database

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango
import urllib.request
import os
import hashlib
import resource

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
except Exception:
    pass
from . import api
from . import player
from .player_widget import PlayerWidget, HAS_MPV


if os.environ.get("FLATPAK_ID"):
    cache_dir_base = os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache'))
    IMAGE_CACHE_DIR = os.path.join(cache_dir_base, 'popcorn-box', 'images')
else:
    IMAGE_CACHE_DIR = os.path.expanduser('~/.var/app/io.github.fastrizwaan.PopcornBox/cache/popcorn-box/images')
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

from concurrent.futures import ThreadPoolExecutor
_image_pool = ThreadPoolExecutor(max_workers=4)

def load_image_into_picture(url, picture_widget, width=None, height=None):
    if not url: return
    
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_file = os.path.join(IMAGE_CACHE_DIR, url_hash)
    
    def fetch_image():
        try:
            data = None
            if os.path.exists(cache_file):
                with open(cache_file, 'rb') as f:
                    data = f.read()
            else:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = response.read()
                with open(cache_file, 'wb') as f:
                    f.write(data)
            
            if not data:
                return
                
            # Decode pixbuf in worker thread — keeps glycin temp files
            # bounded to the pool size (4 max) instead of hundreds on main thread
            loader = GdkPixbuf.PixbufLoader()
            pixbuf = None
            try:
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
            except Exception as e:
                # If image is corrupt (e.g. HTML 429 page), write() fails.
                # MUST close loader to prevent leaking the glycin sub-process and FDs.
                try:
                    loader.close()
                except Exception:
                    pass
                print(f"Failed to decode image {url}: {e}")
                
            if pixbuf:
                if width and height:
                    pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
                GLib.idle_add(_apply_pixbuf, picture_widget, pixbuf)
        except Exception as e:
            print(f"Failed to load image: {e}")
    
    _image_pool.submit(fetch_image)

def _apply_pixbuf(picture_widget, pixbuf):
    """Apply a pre-decoded pixbuf to a widget. Runs on main GTK thread."""
    try:
        picture_widget.set_can_shrink(True)
        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture_widget.set_paintable(texture)
    except Exception:
        pass  # GPU texture allocation failed — skip silently
    return False


_WINDOW_DRAG_BLOCKED = (
    Gtk.Button, Gtk.Entry, Gtk.SearchEntry, Gtk.DropDown,
    Gtk.Scale, Gtk.Switch, Gtk.Spinner,
    Gtk.WindowControls, Gtk.Popover, Gtk.Scrollbar,
)

def _widget_blocks_window_drag(widget):
    while widget is not None:
        if isinstance(widget, _WINDOW_DRAG_BLOCKED):
            return True
        if isinstance(widget, (Gtk.Window, Adw.ApplicationWindow)):
            break
        widget = widget.get_parent()
    return False

def _install_window_drag(window):
    drag = Gtk.GestureDrag()
    drag.set_button(Gdk.BUTTON_PRIMARY)

    state = {"drag_started": False, "pending_drag": False}

    def on_drag_begin(gesture, start_x, start_y):
        state["drag_started"] = False
        state["pending_drag"] = False

        widget = gesture.get_widget()
        picked = widget.pick(start_x, start_y, Gtk.PickFlags.DEFAULT)
        if picked and _widget_blocks_window_drag(picked):
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return

        # Check if the picked widget is inside a Gtk.GLArea (the video player)
        # If so, check if the click is in the bottom 100 pixels of that GLArea
        gl_area = None
        temp = picked
        while temp is not None:
            if isinstance(temp, Gtk.GLArea):
                gl_area = temp
                break
            temp = temp.get_parent()

        if gl_area is not None:
            # Get start coordinates relative to the GLArea
            translated = widget.translate_coordinates(gl_area, start_x, start_y)
            if translated is not None:
                rx, ry = translated
                gl_height = gl_area.get_height()
                # Block drag if click is in the bottom 100 pixels (OSC controls area)
                if ry > gl_height - 100:
                    gesture.set_state(Gtk.EventSequenceState.DENIED)
                    return

        state["pending_drag"] = True

    def on_drag_update(gesture, offset_x, offset_y):
        if not state["pending_drag"] or state["drag_started"]:
            return

        dist = (offset_x ** 2 + offset_y ** 2) ** 0.5
        if dist > 8:
            state["drag_started"] = True

            # Release the click inside MPV to prevent stuck dragging state when dragging the window
            if hasattr(window, "global_player") and window.global_player:
                pw = window.global_player.player_widget
                if pw and hasattr(pw, "mpv") and pw.mpv:
                    try:
                        pw.mpv.command("keyup", "MBTN_LEFT")
                    except Exception:
                        pass

            widget = gesture.get_widget()
            native = widget.get_native()
            if native is None:
                return
            surface = native.get_surface()
            if surface is None:
                return

            event = gesture.get_current_event()
            if event is None:
                return

            pos = event.get_position()
            if len(pos) == 3:
                _, x, y = pos
            else:
                x, y = pos

            Gdk.Toplevel.begin_move(
                surface,
                event.get_device(),
                Gdk.BUTTON_PRIMARY,
                x,
                y,
                event.get_time(),
            )

    drag.connect("drag-begin", on_drag_begin)
    drag.connect("drag-update", on_drag_update)
    window.add_controller(drag)

class MovieDetailsPage(Gtk.Overlay):
    def __init__(self, movie, on_back):
        super().__init__()
        self.movie_stub = movie
        self.media_type = movie.get("type", "movie")
        self.selected_season = None
        self.selected_episode = None
        self.torrents = []
        self._restoring_state = False
        self._destroyed = False
        self._last_played_magnet = None
        self._last_played_file_index = None
        
        self.backdrop_pic = Gtk.Picture()
        self.backdrop_pic.set_can_shrink(True)
        self.backdrop_pic.set_opacity(0.3)
        self.backdrop_pic.set_content_fit(Gtk.ContentFit.COVER)
        self.backdrop_pic.set_hexpand(True)
        self.backdrop_pic.set_vexpand(True)
        self.set_child(self.backdrop_pic)
        
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_box.set_css_classes(['backdrop-overlay'])
        self.add_overlay(self.main_box)
        
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header_box.set_margin_start(16)
        header_box.set_margin_top(64)
        
        back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        back_btn.set_tooltip_text("Back")
        back_btn.set_css_classes(['circular', 'flat'])
        def on_back_clicked(btn):
            if hasattr(self, 'stop_all'):
                self.stop_all()
            on_back()
            
        back_btn.connect("clicked", on_back_clicked)
        header_box.append(back_btn)
        
        self.main_box.append(header_box)
        
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.content_box.set_margin_start(48)
        self.content_box.set_margin_end(48)
        self.content_box.set_margin_top(24)
        self.content_box.set_margin_bottom(24)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_child(self.content_box)
        scrolled.add_css_class("transparent")
        
        self.main_box.append(scrolled)
        
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        self.spinner.set_halign(Gtk.Align.CENTER)
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.spinner.set_vexpand(True)
        self.content_box.append(self.spinner)
        
        self.progress_label = Gtk.Label(label="")
        self.progress_label.set_halign(Gtk.Align.START)
        
        self.load_details_async()
        
    def load_details_async(self):
        def fetch():
            details = api.fetch_movie_details(self.movie_stub.get("id"), self.media_type)
            GLib.idle_add(self.build_ui, details)
        threading.Thread(target=fetch, daemon=True).start()
        
    def toggle_favorite(self, details):
        item_id = details.get("id")
        if database.is_favorite(item_id):
            database.remove_favorite(item_id)
            self.detail_fav_btn.set_label("♡ Add to Favorites")
        else:
            database.add_favorite({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_fav_btn.set_label("♥ Remove from Favorites")
            
    def toggle_watched(self, details):
        item_id = details.get("id")
        if database.is_watched(item_id):
            database.remove_watched(item_id)
            self.detail_seen_btn.set_label("👁 Not Seen")
        else:
            database.add_watched({
                "id": item_id,
                "title": details.get("title"),
                "year": details.get("year"),
                "medium_cover_image": details.get("medium_cover_image"),
                "type": self.media_type
            })
            self.detail_seen_btn.set_label("👁 Seen")
        
    def build_ui(self, details, torrents=None):
        self.content_box.remove(self.spinner)
        if not details:
            self.content_box.append(Gtk.Label(label="Failed to load details."))
            return
            
        if details.get("background"):
            load_image_into_picture(details.get("background"), self.backdrop_pic)
            
        top_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32)
        
        poster = Gtk.Picture()
        poster.set_can_shrink(True)
        poster.set_size_request(250, 375)
        poster.set_valign(Gtk.Align.START)
        poster.set_content_fit(Gtk.ContentFit.COVER)
        top_hbox.append(poster)
        load_image_into_picture(details.get("medium_cover_image"), poster)
        
        info_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        
        title_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_hbox.set_valign(Gtk.Align.CENTER)
        
        title = Gtk.Label(label=details.get("title", ""))
        title.set_css_classes(['title-1'])
        title.set_halign(Gtk.Align.START)
        title.set_wrap(True)
        title_hbox.append(title)
        
        # Copy button
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy Title")
        copy_btn.set_css_classes(['flat', 'circular'])
        copy_btn.set_valign(Gtk.Align.CENTER)
        def on_copy_clicked(btn):
            try:
                clipboard = Gdk.Display.get_default().get_clipboard()
                clipboard.set(details.get("title", ""))
            except Exception as e:
                print(f"Failed to copy to clipboard: {e}")
        copy_btn.connect("clicked", on_copy_clicked)
        title_hbox.append(copy_btn)
        
        # G button
        g_btn = Gtk.Button(label="G")
        g_btn.set_tooltip_text("Search Google")
        g_btn.set_css_classes(['flat', 'circular', 'g-button'])
        g_btn.set_valign(Gtk.Align.CENTER)
        def on_g_clicked(btn):
            import urllib.parse
            import subprocess
            q = urllib.parse.quote(details.get("title", ""))
            subprocess.Popen(["xdg-open", f"https://www.google.com/search?q={q}"])
        g_btn.connect("clicked", on_g_clicked)
        title_hbox.append(g_btn)
        
        info_vbox.append(title_hbox)
        
        meta_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        meta_hbox.set_valign(Gtk.Align.CENTER)
        
        meta_str = f"{details.get('year', '')} • {details.get('runtime', '')} • {details.get('genre', '')} • "
        meta = Gtk.Label(label=meta_str)
        meta.set_halign(Gtk.Align.START)
        meta.set_css_classes(['dim-label'])
        meta_hbox.append(meta)
        
        # IMDb Link Button
        imdb_id = details.get("imdb_id") or details.get("id")
        imdb_rating = details.get("imdbRating", "")
        if imdb_id:
            imdb_btn = Gtk.Button(label=f"IMDb {imdb_rating}")
            imdb_btn.set_css_classes(['flat', 'imdb-link-btn'])
            imdb_btn.set_valign(Gtk.Align.CENTER)
            def on_imdb_clicked(btn):
                import subprocess
                subprocess.Popen(["xdg-open", f"https://www.imdb.com/title/{imdb_id}/"])
            imdb_btn.connect("clicked", on_imdb_clicked)
            meta_hbox.append(imdb_btn)
        else:
            meta_no_link = Gtk.Label(label=f"IMDb {imdb_rating}")
            meta_no_link.set_css_classes(['dim-label'])
            meta_hbox.append(meta_no_link)
            
        info_vbox.append(meta_hbox)
        
        desc = Gtk.Label(label=details.get("description", ""))
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        desc.set_max_width_chars(80)
        desc.set_margin_top(16)
        desc.set_margin_bottom(16)
        info_vbox.append(desc)
        
        # Row 1: Actions (Fav, Seen, Trailer)
        self.row1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row1_box.set_margin_top(16)
        
        item_id = details.get("id")
        self.detail_fav_btn = Gtk.Button(label="♥ Remove from Favorites" if database.is_favorite(item_id) else "♡ Add to Favorites")
        self.detail_fav_btn.set_css_classes(['pill'])
        self.detail_fav_btn.connect("clicked", lambda x: self.toggle_favorite(details))
        self.row1_box.append(self.detail_fav_btn)
        
        self.detail_seen_btn = Gtk.Button(label="👁 Seen" if database.is_watched(item_id) else "👁 Not Seen")
        self.detail_seen_btn.set_css_classes(['pill'])
        self.detail_seen_btn.connect("clicked", lambda x: self.toggle_watched(details))
        self.row1_box.append(self.detail_seen_btn)
        
        trailer_btn = Gtk.Button()
        trailer_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        trailer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        trailer_box.append(trailer_icon)
        trailer_box.append(Gtk.Label(label="Watch Trailer"))
        trailer_btn.set_child(trailer_box)
        trailer_btn.set_css_classes(['pill'])
        trailer_btn.set_valign(Gtk.Align.CENTER)
        trailer_btn.connect("clicked", lambda x: self.on_trailer_clicked(details.get("trailer")))
        if not details.get("trailer"): trailer_btn.set_sensitive(False)
        self.row1_box.append(trailer_btn)
        
        info_vbox.append(self.row1_box)
        
        # Row 2: Series Selection (if applicable) & Qualities
        self.row2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row2_box.set_margin_top(16)
        
        self.is_resume = False
        if self.media_type in ["series", "anime"] and details.get("videos"):
            videos = details.get("videos")
            seasons = sorted(list(set([v.get("season", 1) for v in videos])))
            
            watched_item = next((w for w in database.get_watched() if w.get("id") == self.movie_stub.get("id")), None)
            
            # Prioritize active downloads for this item
            active_download = None
            from . import player
            with player._engines_lock:
                for eng in player._engines.values():
                    if eng.is_alive() and eng.item_id == self.movie_stub.get("id") and getattr(eng, "season", None) is not None:
                        active_download = eng
                        break
            
            pending_s = None
            pending_e = None
            
            if active_download and getattr(active_download, "season", None) is not None and getattr(active_download, "episode", None) is not None:
                if any(v for v in videos if v.get("season") == active_download.season and v.get("episode") == active_download.episode):
                    pending_s = active_download.season
                    pending_e = active_download.episode
                    self.is_resume = True
            elif watched_item and "season" in watched_item and "episode" in watched_item:
                if any(v for v in videos if v.get("season") == watched_item["season"] and v.get("episode") == watched_item["episode"]):
                    pending_s = watched_item["season"]
                    pending_e = watched_item["episode"]
                    self.is_resume = True
            
            if not pending_s and seasons:
                pending_s = seasons[0]
                
            self.season_dropdown = Gtk.DropDown.new_from_strings([f"Season {s}" for s in seasons])
            self.season_dropdown.set_valign(Gtk.Align.CENTER)
            self.row2_box.append(self.season_dropdown)
            
            self.episode_dropdown = Gtk.DropDown.new_from_strings([])
            self.episode_dropdown.set_valign(Gtk.Align.CENTER)
            self.row2_box.append(self.episode_dropdown)
            
            self.autoplay_check = Gtk.CheckButton(label="Auto Play Next Episodes")
            self.autoplay_check.set_valign(Gtk.Align.CENTER)
            self.autoplay_check.set_margin_start(8)
            self.autoplay_check.set_active(database.get_setting("autoplay_next", True))
            self.autoplay_check.connect("toggled", lambda cb: database.set_setting("autoplay_next", cb.get_active()))
            self.row2_box.append(self.autoplay_check)
            
            def on_season_changed(dropdown, *args):
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                s = seasons[idx]
                
                self.selected_season = s
                eps = [v for v in videos if v.get("season") == s]
                eps.sort(key=lambda x: x.get("episode", 0))
                self.current_episodes = eps
                ep_strings = [f"Ep {e.get('episode')}: {e.get('title') or e.get('name', '')}" for e in eps]
                self.episode_dropdown.set_model(Gtk.StringList.new(ep_strings))
                
                e_idx = 0
                if getattr(self, '_pending_e', None):
                    for i, e in enumerate(eps):
                        if e.get("episode") == self._pending_e:
                            e_idx = i
                            break
                self._pending_e = None
                
                if self.episode_dropdown.get_selected() == e_idx:
                    on_episode_changed(self.episode_dropdown)
                else:
                    self.episode_dropdown.set_selected(e_idx)
                
            self.season_dropdown.connect("notify::selected", on_season_changed)
            
            def on_episode_changed(dropdown, *args):
                idx = dropdown.get_selected()
                if idx == Gtk.INVALID_LIST_POSITION: return
                ep = self.current_episodes[idx].get("episode")
                if getattr(self, 'selected_episode', None) == ep and getattr(self, '_initial_fetch_done', False):
                    return
                self.selected_episode = ep
                self._initial_fetch_done = True
                self.fetch_torrents_async()
                self._check_continue_watching(self.movie_stub.get("id"))
                
            self.episode_dropdown.connect("notify::selected", on_episode_changed)
            
            if seasons:
                self._pending_e = pending_e
                s_idx = seasons.index(pending_s) if pending_s in seasons else 0
                if self.season_dropdown.get_selected() == s_idx:
                    on_season_changed(self.season_dropdown)
                else:
                    self.season_dropdown.set_selected(s_idx)
            
        self.quality_button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.quality_button_box.set_valign(Gtk.Align.CENTER)
        
        if self.media_type in ["series", "anime"] and details.get("videos"):
            self.quality_button_box.set_margin_top(12)
            info_vbox.append(self.row2_box)
        else:
            self.quality_button_box.set_margin_top(16)
            
        info_vbox.append(self.quality_button_box)
        
        # Row 3: Dropdown
        self.row3_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row3_box.set_margin_top(12)
        
        self.file_dropdown = Gtk.DropDown.new_from_strings([])
        self.file_dropdown.set_valign(Gtk.Align.CENTER)
        
        def on_dropdown_changed(dropdown, pspec):
            if self._restoring_state:
                return
            idx = dropdown.get_selected()
            if hasattr(self, 'current_t_list') and idx != Gtk.INVALID_LIST_POSITION and idx < len(self.current_t_list):
                self.selected_torrent = self.current_t_list[idx]
                
        self.file_dropdown.connect("notify::selected", on_dropdown_changed)
        self.row3_box.append(self.file_dropdown)
        
        info_vbox.append(self.row3_box)
        
        # Row 4: Watch/Download/Subtitle buttons
        self.row4_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.row4_box.set_margin_top(12)
        
        watch_label = "Continue Episode" if getattr(self, 'is_resume', False) else "WATCH IT NOW"
        self.watch_btn = Gtk.Button(label=watch_label)
        self.watch_btn.set_css_classes(['suggested-action', 'pill'])
        self.watch_btn.set_size_request(150, 40)
        self.watch_btn.connect("clicked", self.on_watch_clicked)
        self.row4_box.append(self.watch_btn)
        
        self.stop_btn = Gtk.Button(label="■ Stop")
        self.stop_btn.set_css_classes(['destructive-action', 'pill'])
        self.stop_btn.set_valign(Gtk.Align.CENTER)
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        self.stop_btn.set_visible(False)
        self.row4_box.append(self.stop_btn)
        
        # Check if this item is currently paused in the global player → "Continue Watching"
        self._check_continue_watching(details.get("id"))
        
        self.download_btn = Gtk.Button(label="Download")
        self.download_btn.set_css_classes(['pill'])
        self.download_btn.set_valign(Gtk.Align.CENTER)
        self.download_btn.connect("clicked", self.on_download_clicked)
        self.row4_box.append(self.download_btn)
        
        self.download_sub_btn = Gtk.Button(label="Subtitle (EN)")
        self.download_sub_btn.set_css_classes(['pill'])
        self.download_sub_btn.set_valign(Gtk.Align.CENTER)
        self.download_sub_btn.connect("clicked", self.on_download_sub_clicked)
        self.row4_box.append(self.download_sub_btn)
        
        info_vbox.append(self.row4_box)
        info_vbox.append(self.progress_label)
        
        top_hbox.append(info_vbox)
        self.content_box.append(top_hbox)
        
        if self.media_type != "series":
            self.fetch_torrents_async()
            
    def _check_continue_watching(self, item_id):
        """Update watch button to 'Continue Watching' if this item is actively downloading in the background."""
        if not hasattr(self, 'watch_btn'):
            return
        from . import player
        widget = self.get_root()
        active_engine = None
        if hasattr(widget, 'global_player'):
            # Find the active engine in player._engines
            with player._engines_lock:
                for eng in player._engines.values():
                    is_alive = eng.is_alive()
                    eng_season = getattr(eng, 'season', None)
                    eng_episode = getattr(eng, 'episode', None)
                    sel_season = getattr(self, 'selected_season', None)
                    sel_episode = getattr(self, 'selected_episode', None)
                    if is_alive and eng.item_id == item_id:
                        if self.media_type in ["series", "anime"]:
                            # Match season and episode too!
                            if eng_season == sel_season and eng_episode == sel_episode:
                                active_engine = eng
                                break
                        else:
                            active_engine = eng
                            break

        has_progress = False
        sel_season = getattr(self, 'selected_season', None)
        sel_episode = getattr(self, 'selected_episode', None)
        key = item_id
        if self.media_type in ["series", "anime"] and sel_season is not None and sel_episode is not None:
            key = f"{item_id}_S{sel_season}_E{sel_episode}"
        
        from . import database
        if database.get_progress(key) >= 5:
            has_progress = True

        if active_engine or has_progress:
            self.watch_btn.set_label("▶ Continue Watching")
            self.watch_btn.set_css_classes(['pill', 'suggested-action'])
            if hasattr(self, 'stop_btn'): self.stop_btn.set_visible(bool(active_engine))
            self._is_continuing = True
        else:
            self.watch_btn.set_label("WATCH IT NOW")
            if hasattr(self, 'stop_btn'): self.stop_btn.set_visible(False)
            self._is_continuing = False

    def fetch_torrents_async(self):
        self._last_played_magnet = None
        self._last_played_file_index = None
        if hasattr(self, 'watch_btn') and self.watch_btn:
            self.watch_btn.set_sensitive(False)
        if hasattr(self, 'download_btn') and self.download_btn:
            self.download_btn.set_sensitive(False)
        if hasattr(self, 'progress_label') and self.progress_label:
            self.progress_label.set_text("Loading streams...")
        
        if hasattr(self, 'quality_button_box') and self.quality_button_box:
            while child := self.quality_button_box.get_first_child():
                self.quality_button_box.remove(child)
        if hasattr(self, 'file_dropdown') and self.file_dropdown:
            self.file_dropdown.set_model(Gtk.StringList.new(["Loading..."]))
        
        def fetch():
            torrents = api.get_torrents(self.movie_stub.get("id"), self.media_type, self.selected_season, self.selected_episode)
            GLib.idle_add(self.on_torrents_fetched, torrents)
        threading.Thread(target=fetch, daemon=True).start()
        
    def on_torrents_fetched(self, torrents):
        self.progress_label.set_text("")
        self.torrents = torrents
        self.update_quality_dropdown()
        
    def update_quality_dropdown(self):
        while child := self.quality_button_box.get_first_child():
            self.quality_button_box.remove(child)
            
        if not self.torrents:
            self.watch_btn.set_sensitive(False)
            self.download_btn.set_sensitive(False)
            self.file_dropdown.set_model(Gtk.StringList.new(["No streams"]))
            return
            
        self.watch_btn.set_sensitive(True)
        self.download_btn.set_sensitive(True)
        
        quality_groups = {"4K": [], "2160p": [], "1080p": [], "720p": [], "More": []}
        
        for t in self.torrents:
            q = t.get('quality', 'Unknown').upper()
            if "4K" in q: quality_groups["4K"].append(t)
            elif "2160" in q: quality_groups["2160p"].append(t)
            elif "1080" in q: quality_groups["1080p"].append(t)
            elif "720" in q: quality_groups["720p"].append(t)
            else: quality_groups["More"].append(t)
            
        self.selected_torrent = None
        self.quality_buttons = []
        self.current_t_list = []
        
        def on_quality_btn_clicked(btn, t_list):
            for b in self.quality_buttons:
                b.set_css_classes(['pill'])
            btn.set_css_classes(['pill', 'suggested-action'])
            
            self.current_t_list = t_list
            self.selected_torrent = t_list[0]
            strings = []
            for t in t_list:
                addons_str = ", ".join(t.get("addon_names", []))
                addons_suffix = f" [{addons_str}]" if addons_str else ""
                strings.append(f"{t.get('size', 'Unknown')} ({t.get('seeders', 0)} seeds){addons_suffix}")
            self.file_dropdown.set_model(Gtk.StringList.new(strings))
            self.file_dropdown.set_selected(0)
            
        priority = {"1080p": 1, "720p": 2, "4K": 3, "2160p": 4, "More": 5}
        best_priority = 99
        default_btn = None
        default_t_list = None
        
        for q_label in ["4K", "2160p", "1080p", "720p", "More"]:
            t_list = quality_groups[q_label]
            if t_list:
                btn = Gtk.Button(label=q_label)
                btn.set_css_classes(['pill'])
                btn.connect("clicked", on_quality_btn_clicked, t_list)
                self.quality_buttons.append(btn)
                self.quality_button_box.append(btn)
                
                cur_priority = priority.get(q_label, 99)
                if cur_priority < best_priority:
                    best_priority = cur_priority
                    default_btn = btn
                    default_t_list = t_list
                    
        if default_btn:
            on_quality_btn_clicked(default_btn, default_t_list)
            
    def on_download_clicked(self, btn):
        if hasattr(self, 'file_dropdown') and hasattr(self, 'current_t_list') and self.current_t_list:
            idx = self.file_dropdown.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION and idx < len(self.current_t_list):
                self.selected_torrent = self.current_t_list[idx]
                
        if not hasattr(self, 'selected_torrent') or not self.selected_torrent:
            return
        magnet = self.selected_torrent.get("url") or self.selected_torrent.get("magnet")
        if not magnet and self.selected_torrent.get("hash"):
            magnet = api.build_magnet(self.selected_torrent.get("hash"), self.movie_stub.get("title", ""))
        if magnet:
            import subprocess
            subprocess.Popen(['xdg-open', magnet])
            
    def on_download_sub_clicked(self, btn):
        if not getattr(self, 'selected_torrent', None):
            self.progress_label.set_text("Please select a stream first.")
            return
            
        self.download_sub_btn.set_sensitive(False)
        self.progress_label.set_text("Fetching subtitles...")
        
        def fetch_and_download():
            subs = api.get_subtitles(self.movie_stub.get("id"), self.media_type, self.selected_season, self.selected_episode)
            if not subs:
                GLib.idle_add(self._on_sub_downloaded, None)
                return
                
            best_sub = subs[0]
            url = best_sub.get("url")
            
            info_hash = self.selected_torrent.get("hash")
            dest_dir = f"/var/tmp/popcorn-box/{info_hash}"
            
            import os.path
            import glob
            
            sub_dir = dest_dir
            target_filename = "subtitle.srt"
            
            # Try to get relative path from active engine
            active_hash = player.get_active_info_hash()
            if active_hash == info_hash:
                stats = player.get_engine_stats(active_hash)
                if stats and stats.get("filePath"):
                    rel_path = stats.get("filePath")
                    abs_path = os.path.join(dest_dir, rel_path)
                    sub_dir = os.path.dirname(abs_path)
                    target_filename = os.path.splitext(os.path.basename(abs_path))[0] + ".srt"
            
            # If not running or no metadata, scan disk for existing video
            if sub_dir == dest_dir and os.path.exists(dest_dir):
                video_files = []
                for ext in ('*.mp4', '*.mkv', '*.avi', '*.webm'):
                    video_files.extend(glob.glob(os.path.join(dest_dir, '**', ext), recursive=True))
                if video_files:
                    video_files.sort(key=os.path.getsize, reverse=True)
                    largest_video = video_files[0]
                    sub_dir = os.path.dirname(largest_video)
                    target_filename = os.path.splitext(os.path.basename(largest_video))[0] + ".srt"
                    
            # Fallback if engine is off and no file on disk: Use torrent stream hint
            if sub_dir == dest_dir and target_filename == "subtitle.srt":
                video_filename = self.selected_torrent.get("filename")
                if video_filename:
                    target_filename = os.path.splitext(video_filename)[0] + ".srt"
                
            saved_path = api.download_subtitle_to_path(url, os.path.join(sub_dir, target_filename))
            GLib.idle_add(self._on_sub_downloaded, saved_path)
            
        threading.Thread(target=fetch_and_download, daemon=True).start()
        
    def _on_sub_downloaded(self, saved_path):
        self.download_sub_btn.set_sensitive(True)
        if saved_path:
            self.progress_label.set_text(f"Subtitle saved to {saved_path}")
        else:
            self.progress_label.set_text("Failed to download subtitle or no English subtitle found.")
        
    def on_stop_clicked(self, btn):
        # Stop mpv so the global player is no longer playing this item
        widget = self.get_root()
        if hasattr(widget, 'global_player'):
            gp = widget.global_player
            if hasattr(gp, 'player_widget') and HAS_MPV and hasattr(gp.player_widget, 'mpv'):
                try:
                    gp.player_widget.mpv.command("stop")
                except Exception:
                    pass
            gp.current_item_id = None
                
        # Find and stop the active background engine for this item
        from . import player
        item_id = self.movie_stub.get("id")
        active_hash = None
        with player._engines_lock:
            for h, eng in player._engines.items():
                if eng.is_alive() and eng.item_id == item_id:
                    if self.media_type in ["series", "anime"]:
                        if getattr(eng, 'season', None) == self.selected_season and getattr(eng, 'episode', None) == self.selected_episode:
                            active_hash = h
                            break
                    else:
                        active_hash = h
                        break
        if active_hash:
            player.stop_engine_explicit(active_hash)
                    
        self._check_continue_watching(item_id)
        self.fetch_torrents_async()
        
    def on_watch_clicked(self, btn):
        # "Continue Watching" — resume streaming from the active engine
        if getattr(self, '_is_continuing', False):
            from . import player
            active_engine = None
            with player._engines_lock:
                for eng in player._engines.values():
                    if eng.is_alive() and eng.item_id == self.movie_stub.get("id"):
                        if self.media_type in ["series", "anime"]:
                            if getattr(eng, 'season', None) == self.selected_season and getattr(eng, 'episode', None) == self.selected_episode:
                                active_engine = eng
                                break
                        else:
                            active_engine = eng
                            break
            if active_engine:
                f_idx = active_engine.file_index
                if self.media_type in ["series", "anime"]:
                    f_idx = None
                self._start_streaming(active_engine.magnet_link, f_idx)
                return

        if not getattr(self, 'selected_torrent', None):
            # If we have a previously played magnet, resume with that
            if self._last_played_magnet:
                self._start_streaming(self._last_played_magnet, self._last_played_file_index)
                return
            if hasattr(self, 'progress_label') and self.progress_label:
                self.progress_label.set_text("No streams available. Select a quality first.")
            return
            
        import urllib.parse
        torrent = self.selected_torrent
        magnet = torrent.get("url") or torrent.get("magnet")
        if not magnet and torrent.get("hash"):
            magnet = api.build_magnet(torrent.get("hash"), self.movie_stub.get("title", ""))
            
        if magnet and magnet.startswith("magnet:?"):
            trackers = [
                "udp://tracker.opentrackr.org:1337/announce",
                "udp://tracker.openbittorrent.com:80/announce",
                "udp://tracker.torrent.eu.org:451/announce",
                "udp://exodus.desync.com:6969/announce",
                "udp://explodie.org:6969/announce",
                "udp://p4p.arenabg.com:1337/announce",
                "udp://tracker.internetwarriors.net:1337/announce",
                "udp://tracker.cyberia.is:6969/announce",
                "http://tracker.openbittorrent.com:80/announce",
                "udp://open.stealth.si:80/announce"
            ]
            for tr in trackers:
                encoded_tr = urllib.parse.quote(tr, safe="")
                if encoded_tr not in magnet:
                    magnet += f"&tr={encoded_tr}"
        
        file_index = torrent.get("file_index")
        self._start_streaming(magnet, file_index)
        
    def _start_streaming(self, magnet, file_index):
        """Send the stream request to the global player and switch tabs."""
        if not magnet:
            return
            
        self._last_played_magnet = magnet
        self._last_played_file_index = file_index
        
        # Mark as watched when playback starts
        watch_data = {
            "id": self.movie_stub.get("id"),
            "title": self.movie_stub.get("title", ""),
            "type": self.movie_stub.get("type", "movie"),
            "medium_cover_image": self.movie_stub.get("medium_cover_image", "")
        }
        if self.media_type in ["series", "anime"]:
            watch_data["season"] = self.selected_season
            watch_data["episode"] = self.selected_episode
            
        database.add_watched(watch_data)
        if hasattr(self, 'detail_seen_btn'):
            self.detail_seen_btn.set_label("👁 Seen")
            
        title_str = self.movie_stub.get("title", "")
        next_episode_data = None
        
        if self.media_type in ["series", "anime"]:
            title_str = f"{self.movie_stub.get('title', '')} Season {self.selected_season} - Episode {self.selected_episode}"
            
            # Resolve next episode if Auto Play is enabled
            if hasattr(self, 'autoplay_check') and self.autoplay_check.get_active():
                current_idx = -1
                for i, ep in enumerate(self.current_episodes):
                    if ep.get("episode") == self.selected_episode:
                        current_idx = i
                        break
                        
                next_ep = None
                if current_idx >= 0 and current_idx + 1 < len(self.current_episodes):
                    next_ep = self.current_episodes[current_idx + 1]
                elif current_idx >= 0:
                    videos = self.movie_stub.get("videos", [])
                    next_season_eps = [v for v in videos if v.get("season") == self.selected_season + 1]
                    if next_season_eps:
                        next_season_eps.sort(key=lambda x: x.get("episode", 0))
                        next_ep = next_season_eps[0]
                        
                if next_ep:
                    # Mark fetching_next_episode as True on the player widget
                    widget = self.get_root()
                    if hasattr(widget, 'global_player'):
                        widget.global_player.player_widget.fetching_next_episode = True
                    
                    # Torrents for series are not embedded in the initial API response, fetch them in background!
                    import threading
                    def fetch_next_torrents():
                        try:
                            torrents = api.get_torrents(self.movie_stub.get("id"), self.media_type, next_ep.get("season"), next_ep.get("episode"))
                            if not torrents:
                                def clear_fetching():
                                    try:
                                        root = self.get_root()
                                        if hasattr(root, 'global_player'):
                                            root.global_player.player_widget.fetching_next_episode = False
                                            root.global_player.player_widget.check_eof_waiting()
                                    except Exception: pass
                                GLib.idle_add(clear_fetching)
                                return
                            
                            best_torrent = None
                            for p_q in ["1080p", "720p", "4K", "480p"]:
                                for t in torrents:
                                    if t.get("quality") == p_q:
                                        best_torrent = t
                                        break
                                if best_torrent:
                                    break
                            
                            if not best_torrent:
                                best_torrent = torrents[0]
                            n_magnet = best_torrent.get("url") or best_torrent.get("magnet")
                            if not n_magnet and best_torrent.get("hash"):
                                n_magnet = api.build_magnet(best_torrent.get("hash"), self.movie_stub.get("title", ""))
                                
                            if n_magnet and n_magnet.startswith("magnet:?"):
                                import urllib.parse
                                trackers = [
                                    "udp://tracker.opentrackr.org:1337/announce",
                                    "udp://tracker.openbittorrent.com:80/announce",
                                    "udp://tracker.torrent.eu.org:451/announce",
                                    "udp://exodus.desync.com:6969/announce",
                                    "udp://explodie.org:6969/announce",
                                    "udp://p4p.arenabg.com:1337/announce",
                                    "udp://tracker.internetwarriors.net:1337/announce",
                                    "udp://tracker.cyberia.is:6969/announce",
                                    "http://tracker.openbittorrent.com:80/announce",
                                    "udp://open.stealth.si:80/announce"
                                ]
                                for tr in trackers:
                                    encoded_tr = urllib.parse.quote(tr, safe="")
                                    if encoded_tr not in n_magnet:
                                        n_magnet += f"&tr={encoded_tr}"
                                        
                            if n_magnet:
                                player.download_magnet_background(
                                    n_magnet,
                                    file_index=best_torrent.get("file_index"),
                                    item_id=self.movie_stub.get("id"),
                                    media_type=self.media_type,
                                    season=next_ep.get("season"),
                                    episode=next_ep.get("episode")
                                )
                                
                                def trigger_next():
                                    self.selected_season = next_ep.get("season")
                                    self.selected_episode = next_ep.get("episode")
                                    self._start_streaming(n_magnet, best_torrent.get("file_index"))
                                    
                                next_data = {
                                    "title": f"{self.movie_stub.get('title', '')} - Season {next_ep.get('season')}, Ep {next_ep.get('episode')}",
                                    "callback": trigger_next
                                }
                                
                                def update_ui():
                                    try:
                                        root = self.get_root()
                                        if hasattr(root, 'global_player') and root.global_player.current_item_id == self.movie_stub.get("id"):
                                            root.global_player.player_widget.next_episode_data = next_data
                                            root.global_player.player_widget.fetching_next_episode = False
                                            root.global_player.player_widget.check_eof_waiting()
                                    except Exception:
                                        pass
                                GLib.idle_add(update_ui)
                            else:
                                def clear_fetching():
                                    try:
                                        root = self.get_root()
                                        if hasattr(root, 'global_player'):
                                            root.global_player.player_widget.fetching_next_episode = False
                                            root.global_player.player_widget.check_eof_waiting()
                                    except Exception:
                                        pass
                                GLib.idle_add(clear_fetching)
                        except Exception as e:
                            print(f"Failed to fetch next episode torrents: {e}")
                            def clear_fetching():
                                try:
                                    root = self.get_root()
                                    if hasattr(root, 'global_player'):
                                        root.global_player.player_widget.fetching_next_episode = False
                                        root.global_player.player_widget.check_eof_waiting()
                                except Exception:
                                    pass
                            GLib.idle_add(clear_fetching)
                            
                    threading.Thread(target=fetch_next_torrents, daemon=True).start()
            
        item_id = self.movie_stub.get("id")
        
        widget = self.get_root()
        if hasattr(widget, 'global_player'):
            season = self.selected_season if self.media_type in ["series", "anime"] else None
            episode = self.selected_episode if self.media_type in ["series", "anime"] else None
            widget.global_player.start_magnet(magnet, file_index, item_id, self.media_type, title_str, next_episode_data=next_episode_data, season=season, episode=episode)
            widget.switch_category("player", "All", widget.player_btn)
        
    def on_trailer_clicked(self, trailer_id):
        title_str = self.movie_stub.get("title", "") + " (Trailer)"
        
        widget = self.get_root()
        if hasattr(widget, 'global_player'):
            widget.global_player.start_trailer(trailer_id, title_str)
            widget.switch_category("player", "All", widget.player_btn)
class MovieWidget(Gtk.Box):
    def __init__(self, movie, on_card_clicked, on_remove_clicked=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.movie = movie
        self.on_card_clicked_cb = on_card_clicked
        self.remove_btn_ref = None
        
        # Match WineCharm icon view logic but make them slightly larger to fit ~15 per row
        self.set_size_request(130, 195)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.set_css_classes(['pt-card'])
        
        # Click gesture to handle mouse activation
        click = Gtk.GestureClick()
        click.connect("released", self._on_card_released)
        self.add_controller(click)
        
        icon_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        icon_container.set_hexpand(True)
        icon_container.set_halign(Gtk.Align.CENTER)
        
        self.overlay = Gtk.Overlay()
        self.poster_image = Gtk.Picture()
        self.poster_image.set_can_shrink(True)
        self.poster_image.set_size_request(130, 195)
        self.poster_image.set_content_fit(Gtk.ContentFit.COVER)
        
        self.overlay.set_child(self.poster_image)
        
        if on_remove_clicked:
            remove_btn = Gtk.Button(icon_name="window-close-symbolic")
            remove_btn.set_can_focus(False)
            remove_btn.set_css_classes(["osd", "circular"])
            remove_btn.set_halign(Gtk.Align.END)
            remove_btn.set_valign(Gtk.Align.START)
            remove_btn.set_margin_top(4)
            remove_btn.set_margin_end(4)
            remove_btn.set_tooltip_text("Remove")
            remove_btn.set_visible(False)
            remove_btn.connect("clicked", lambda btn: on_remove_clicked(self.movie, self))
            self.overlay.add_overlay(remove_btn)
            self.remove_btn_ref = remove_btn
            
            hover = Gtk.EventControllerMotion()
            hover.connect("enter", lambda *args: remove_btn.set_visible(True))
            hover.connect("leave", lambda *args: remove_btn.set_visible(False))
            icon_container.add_controller(hover)
            
        icon_container.append(self.overlay)
        self.append(icon_container)
        load_image_into_picture(movie.get("medium_cover_image"), self.poster_image, width=130, height=195)
        
        title_label = Gtk.Label(label=movie.get("title", "Unknown"))
        title_label.set_lines(1)
        import gi
        gi.require_version('Pango', '1.0')
        from gi.repository import Pango
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        title_label.set_max_width_chars(1)
        title_label.set_hexpand(True)
        title_label.set_halign(Gtk.Align.FILL)
        title_label.set_xalign(0.0)
        title_label.set_css_classes(['pt-card-title'])
        self.append(title_label)
        
        year_label = Gtk.Label(label=str(movie.get("year", "")))
        year_label.set_halign(Gtk.Align.START)
        year_label.set_css_classes(['pt-card-year'])
        self.append(year_label)

    def _on_card_released(self, gesture, n_press, x, y):
        # Claim the sequence to prevent GtkFlowBox child-activated from double firing
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        target = picked
        while target is not None:
            if target == self.remove_btn_ref:
                return
            if target == self:
                break
            target = target.get_parent()
            
        self.on_card_clicked_cb(self.movie)

class DownloadItemWidget(Gtk.Box):
    def __init__(self, download):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_css_classes(['dl-row'])
        
        self.download = download
        self.info_hash = download.get("info_hash") or ""
        self.magnet = download.get("magnet") or ""
        self.file_index = download.get("file_index")
        
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_hexpand(True)
        
        title = Gtk.Label(label=download.get("name") or "Unknown")
        title.set_halign(Gtk.Align.START)
        title.set_css_classes(['title-2'])
        title.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(title)
        
        self.progress_bar = Gtk.ProgressBar()
        vbox.append(self.progress_bar)
        
        self.status_label = Gtk.Label(label="Checking...")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_css_classes(['dim-label'])
        vbox.append(self.status_label)
        
        self.append(vbox)
        
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_valign(Gtk.Align.CENTER)
        
        self.popover = Gtk.Popover()
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pop_box.set_margin_top(8)
        pop_box.set_margin_bottom(8)
        pop_box.set_margin_start(8)
        pop_box.set_margin_end(8)
        
        self.play_btn = Gtk.Button(label="Resume Download", icon_name="go-down-symbolic")
        self.play_btn.set_tooltip_text("Resume Download")
        self.play_btn.set_has_frame(False)
        self.play_btn.set_halign(Gtk.Align.START)
        self.play_btn.connect("clicked", self.on_play_clicked)
        pop_box.append(self.play_btn)

        self.stop_btn = Gtk.Button(label="Pause Download", icon_name="media-playback-pause-symbolic")
        self.stop_btn.set_tooltip_text("Pause Download")
        self.stop_btn.set_has_frame(False)
        self.stop_btn.set_halign(Gtk.Align.START)
        self.stop_btn.connect("clicked", self.on_stop_clicked)
        pop_box.append(self.stop_btn)
        
        self.watch_btn = Gtk.Button(label="Play Video File", icon_name="media-playback-start-symbolic")
        self.watch_btn.set_tooltip_text("Play Video File")
        self.watch_btn.set_has_frame(False)
        self.watch_btn.set_halign(Gtk.Align.START)
        self.watch_btn.connect("clicked", self.on_watch_clicked)
        pop_box.append(self.watch_btn)
        
        folder_btn = Gtk.Button(label="Open Folder", icon_name="folder-symbolic")
        folder_btn.set_tooltip_text("Open Folder")
        folder_btn.set_has_frame(False)
        folder_btn.set_halign(Gtk.Align.START)
        folder_btn.connect("clicked", self.on_folder_clicked)
        pop_box.append(folder_btn)
        
        copy_btn = Gtk.Button(label="Copy Magnet Link", icon_name="edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy Magnet Link")
        copy_btn.set_has_frame(False)
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.connect("clicked", self.on_copy_clicked)
        pop_box.append(copy_btn)
        
        source_btn = Gtk.Button(label="Go to Source", icon_name="go-home-symbolic")
        source_btn.set_tooltip_text("Go to Source")
        source_btn.set_has_frame(False)
        source_btn.set_halign(Gtk.Align.START)
        item_id = download.get("item_id")
        media_type = download.get("media_type")
        if not item_id or not media_type:
            source_btn.set_sensitive(False)
            source_btn.set_tooltip_text("Source information missing for this download.")
        else:
            source_btn.connect("clicked", lambda btn: self.on_source_clicked(item_id, media_type))
        pop_box.append(source_btn)
        
        del_btn = Gtk.Button(label="Delete", icon_name="user-trash-symbolic")
        del_btn.set_tooltip_text("Delete")
        del_btn.set_has_frame(False)
        del_btn.set_halign(Gtk.Align.START)
        del_btn.set_css_classes(['destructive-action'])
        del_btn.connect("clicked", self.on_delete_clicked)
        pop_box.append(del_btn)
        
        self.popover.set_child(pop_box)
        
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("view-more-symbolic")
        menu_btn.set_popover(self.popover)
        menu_btn.set_tooltip_text("Options")
        action_box.append(menu_btn)
        
        self.append(action_box)
        
        self.is_active = True
        self.poll_id = GLib.timeout_add(1000, self.update_status)
        self.update_status()
        
    def destroy(self):
        self.is_active = False
        if hasattr(self, 'poll_id'):
            GLib.source_remove(self.poll_id)
            
    def update_status(self):
        if not self.is_active:
            return False
            
        stats = player.get_engine_stats(self.info_hash)
        if stats:
            dl = stats.get("downloaded", 0)
            tot = stats.get("totalLength", 0)
            prog = stats.get("progress", 0)
            self.progress_bar.set_fraction(prog)
            speed_dl = stats.get("downloadSpeed", 0) / 1024
            speed_ul = stats.get("uploadSpeed", 0) / 1024
            
            status_text = f"{prog*100:.1f}%"
            if speed_dl > 0 or speed_ul > 0:
                status_text += f" - D: {speed_dl:.1f} KiB/s | U: {speed_ul:.1f} KiB/s"
            elif prog < 1.0:
                status_text += f" - Connecting..."
                
            if tot > 0:
                status_text += f" ({dl/(1024*1024):.1f} MB / {tot/(1024*1024*1024):.2f} GB)"
                
            status_desc = stats.get("status", "")
            if "seeding" in status_desc.lower() or "finished" in status_desc.lower():
                status_text = f"Seeding - {status_text}"
                
            ratio = stats.get("ratio", 0)
            peers = stats.get("activePeers", 0)
            seeds = stats.get("seeds", 0)
            
            if ratio > 0:
                status_text += f" | Ratio: {ratio:.2f}"
            if peers > 0 or seeds > 0:
                status_text += f" | Peers: {peers} / Seeds: {seeds}"
                
            self.status_label.set_text(status_text)
            
            self.play_btn.set_visible(False)
            self.stop_btn.set_visible(True)
        else:
            self.play_btn.set_visible(True)
            self.stop_btn.set_visible(False)
            
            path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
            if os.path.exists(path):
                self.status_label.set_text("Paused")
            else:
                self.status_label.set_text("Not Downloaded")
                self.progress_bar.set_fraction(0.0)
                
        return True
        
    def on_play_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.download_magnet_background(
            self.magnet,
            file_index=self.file_index,
            item_id=self.download.get("item_id"),
            media_type=self.download.get("media_type")
        )
        self.update_status()
        
    def on_watch_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        
        widget = self.get_root()
        if hasattr(widget, 'global_player'):
            widget.global_player.start_magnet(
                self.magnet, 
                self.file_index, 
                self.download.get("item_id"), 
                self.download.get("media_type", "movie"), 
                self.download.get("title", "")
            )
            widget.switch_category("player", "All", widget.player_btn)
            
            def go_back_to_downloads():
                widget.switch_category("downloads", "All", widget.downloads_btn)
            
            widget.global_player.player_widget.on_go_back = go_back_to_downloads
        
    def on_stop_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.stop_engine_explicit(self.info_hash)
        self.update_status()
        
    def on_folder_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
        os.makedirs(path, exist_ok=True)
        import subprocess
        subprocess.Popen(['xdg-open', path])
            
    def on_copy_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(self.magnet)
        
    def on_delete_clicked(self, btn):
        if hasattr(self, 'popover'): self.popover.popdown()
        player.stop_engine_explicit(self.info_hash)
        database.remove_download(self.info_hash)
        path = os.path.join(player.DOWNLOAD_BASE, self.info_hash)
        import shutil
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        
        parent = self.get_parent()
        if parent:
            parent.remove(self)
        self.destroy()

    def on_source_clicked(self, item_id, media_type):
        if hasattr(self, 'popover'): self.popover.popdown()
        # Find the main app window
        widget = self
        while widget.get_parent():
            widget = widget.get_parent()
            if isinstance(widget, Gtk.Window):
                break
        
        if hasattr(widget, 'show_movie_details'):
            stub = {"id": item_id, "type": media_type, "title": "Loading...", "medium_cover_image": ""}
            widget.show_movie_details(stub)

class GlobalPlayerView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(True)
        self.set_vexpand(True)
        
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.append(self.stack)
        
        empty = Gtk.Label(label="Nothing is playing.")
        empty.set_css_classes(["dim-label", "title-2"])
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_vexpand(True)
        self.stack.add_named(empty, "empty")
        
        self.build_download_ui()
        self.build_player_ui()
        self.stack.set_visible_child_name("empty")
        
    def build_download_ui(self):
        self.dl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.dl_box.set_valign(Gtk.Align.CENTER)
        self.dl_box.set_halign(Gtk.Align.CENTER)
        self.dl_box.set_vexpand(True)
        
        self.dl_title = Gtk.Label(label="")
        self.dl_title.set_css_classes(['title-1'])
        self.dl_title.set_wrap(True)
        self.dl_title.set_halign(Gtk.Align.CENTER)
        self.dl_box.append(self.dl_title)
        
        self.dl_progress = Gtk.ProgressBar()
        self.dl_progress.set_size_request(500, -1)
        self.dl_box.append(self.dl_progress)
        
        self.dl_status = Gtk.Label(label="Initializing...")
        self.dl_status.set_css_classes(['title-2'])
        self.dl_box.append(self.dl_status)
        
        self.dl_stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.dl_stats_box.set_css_classes(['dl-row'])
        
        self.dl_percent = Gtk.Label(label="0%")
        self.dl_percent.set_css_classes(['title-3'])
        self.dl_stats_box.append(self.dl_percent)
        
        self.dl_speed = Gtk.Label(label="")
        self.dl_stats_box.append(self.dl_speed)
        
        self.dl_peers = Gtk.Label(label="")
        self.dl_stats_box.append(self.dl_peers)
        
        self.dl_box.append(self.dl_stats_box)
        
        cancel_btn = Gtk.Button(label="Stop Stream")
        cancel_btn.set_css_classes(['pill', 'destructive-action'])
        cancel_btn.set_size_request(140, -1)
        cancel_btn.set_halign(Gtk.Align.CENTER)
        cancel_btn.connect("clicked", self.on_stop_stream)
        self.dl_box.append(cancel_btn)
        
        self.stack.add_named(self.dl_box, "download")
        
    def build_player_ui(self):
        def on_player_close():
            keep = getattr(self.player_widget, 'keep_downloading', False)
            self.stop_all(keep_downloading=keep)
            self.player_widget.keep_downloading = False
            if hasattr(self, 'player_widget') and callable(self.player_widget.on_go_back):
                self.player_widget.on_go_back()
            
        self.player_widget = PlayerWidget(on_close_callback=on_player_close)
        
        def on_play_next_episode():
            data = self.player_widget.next_episode_data
            if data and "callback" in data:
                data["callback"]()
                
        self.player_widget.on_play_next = on_play_next_episode
        self.stack.add_named(self.player_widget, "player")

    def on_stop_stream(self, btn):
        h = player._streaming_hash
        self.stop_all()
        if h:
            player.stop_engine_explicit(h)
        if hasattr(self, 'player_widget') and callable(self.player_widget.on_go_back):
            self.player_widget.on_go_back()

    def stop_all(self, keep_downloading=False):
        if not keep_downloading:
            self.current_item_id = None
        player.stop_player(keep_downloading=keep_downloading)
        if hasattr(self, 'player_widget'):
            self.player_widget.stop()
        self.stack.set_visible_child_name("empty")
        
    def _create_progress_cb(self):
        def progress_cb(status_data):
            if type(status_data) == dict:
                if status_data.get("closed"):
                    self.stop_all()
                    return
                if status_data.get("url"):
                    if self.stack.get_visible_child_name() == "download":
                        self.stack.set_visible_child_name("player")
                        self.player_widget.play(status_data["url"], status_data.get("sub_file"))
                    return
                if status_data.get("status"):
                    self.dl_status.set_text(status_data["status"])
                    
                torrent_progress = status_data.get("progress", 0)
                dl  = status_data.get("downloaded", 0)
                tot = status_data.get("totalLength", 0)

                if torrent_progress > 0:
                    self.dl_progress.set_fraction(torrent_progress)
                    session_mb = f" (+{dl/(1024*1024):.1f} MiB)" if dl > 0 else ""
                    self.dl_percent.set_text(f"{torrent_progress*100:.1f}%{session_mb}")
                elif tot > 0 and dl > 0:
                    frac = dl / tot
                    self.dl_progress.set_fraction(frac)
                    self.dl_percent.set_text(f"{frac*100:.1f}% ({dl/(1024*1024):.1f} MiB / {tot/(1024*1024*1024):.2f} GiB)")
                elif dl > 0:
                    self.dl_progress.pulse()
                    self.dl_percent.set_text(f"Buffered: {dl/(1024*1024):.1f} MiB")
                        
                if "downloadSpeed" in status_data:
                    speed = status_data["downloadSpeed"] / 1024
                    self.dl_speed.set_text(f"↓ {speed:.1f} KiB/s")
                    
                if "activePeers" in status_data:
                    self.dl_peers.set_text(f"Peers: {status_data['activePeers']} / {status_data.get('totalPeers', 0)}")
                    
                # Update player overlay
                if hasattr(self, 'player_widget'):
                    pct_str = self.dl_percent.get_text()
                    spd_str = self.dl_speed.get_text()
                    peer_str = self.dl_peers.get_text()
                    parts = []
                    if pct_str: parts.append(pct_str)
                    if spd_str: parts.append(spd_str)
                    if peer_str: parts.append(peer_str)
                    self.player_widget.update_info(" — ".join(parts))
            elif type(status_data) == str:
                self.dl_status.set_text(status_data)
            return False
        return progress_cb

    def start_magnet(self, magnet, file_index, item_id, media_type, title, next_episode_data=None, season=None, episode=None):
        self.stop_all() 
        self.current_item_id = item_id
        self.stack.set_visible_child_name("download")
        self.dl_title.set_text(title)
        self.dl_status.set_text("Starting stream...")
        self.dl_progress.set_fraction(0.0)
        self.dl_percent.set_text("0%")
        self.dl_speed.set_text("")
        self.dl_peers.set_text("")
        
        self.player_widget.next_episode_data = next_episode_data
        if hasattr(self, 'player_widget'):
            self.player_widget.set_media_title(title)
            self.player_widget.current_item_id = item_id
            self.player_widget.current_season = season
            self.player_widget.current_episode = episode
            
        # ESC will navigate back to this item's details page and keep downloading
        def go_back_to_source():
            self.stop_all(keep_downloading=True)
            root = self.get_root()
            if root:
                orig_media = getattr(root, 'current_media_type', 'movie')
                btn = root.get_tab_button(orig_media)
                for b in [root.movies_btn, root.tv_btn, root.anime_btn, root.fav_btn, 
                          root.watched_btn, root.history_btn, root.downloads_btn, root.addons_btn, root.player_btn]:
                    b.remove_css_class("selected")
                btn.add_css_class("selected")
                
                details_widget = root.stack.get_child_by_name("details")
                if details_widget:
                    if hasattr(root, 'topbar_box'):
                        root.topbar_box.add_css_class("transparent-topbar")
                    root.stack.set_visible_child_name("details")
                    if hasattr(details_widget, '_check_continue_watching') and hasattr(details_widget, 'movie_stub'):
                        details_widget._check_continue_watching(details_widget.movie_stub.get("id"))
                else:
                    if hasattr(root, 'topbar_box'):
                        root.topbar_box.remove_css_class("transparent-topbar")
                    root.stack.set_visible_child_name(f"grid_{orig_media}")
        self.player_widget.on_go_back = go_back_to_source
        
        player.play_magnet(magnet, "mpv", progress_callback=self._create_progress_cb(), file_index=file_index, item_id=item_id, media_type=media_type, season=season, episode=episode)
        
    def start_trailer(self, trailer_id, title):
        self.stop_all()
        if hasattr(self, 'player_widget'):
            self.player_widget.next_episode_data = None
            self.player_widget.current_item_id = None
            self.player_widget.current_season = None
            self.player_widget.current_episode = None
            self.player_widget.set_media_title(title)
        self.stack.set_visible_child_name("download")
        self.dl_title.set_text(title)
        self.dl_status.set_text("Loading Trailer via YouTube...")
        self.dl_progress.set_fraction(0.0)
        self.dl_percent.set_text("")
        self.dl_speed.set_text("")
        self.dl_peers.set_text("")
        
        # ESC will navigate back to the details page
        root = self.get_root()
        def go_back_to_source():
            if root:
                orig_media = getattr(root, 'current_media_type', 'movie')
                btn = root.get_tab_button(orig_media)
                for b in [root.movies_btn, root.tv_btn, root.anime_btn, root.fav_btn, 
                          root.watched_btn, root.history_btn, root.downloads_btn, root.addons_btn, root.player_btn]:
                    b.remove_css_class("selected")
                btn.add_css_class("selected")
                
                if root.stack.get_child_by_name("details"):
                    if hasattr(root, 'topbar_box'):
                        root.topbar_box.add_css_class("transparent-topbar")
                    root.stack.set_visible_child_name("details")
                else:
                    if hasattr(root, 'topbar_box'):
                        root.topbar_box.remove_css_class("transparent-topbar")
                    root.stack.set_visible_child_name(f"grid_{orig_media}")
        self.player_widget.on_go_back = go_back_to_source
        
        player.play_trailer(trailer_id, self._create_progress_cb())

class CategoryGridPage(Gtk.Box):
    def __init__(self, media_type, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_margin_top(54)
        self.media_type = media_type
        self.window = window
        
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_vexpand(True)
        vadjust = self.scrolled.get_vadjustment()
        vadjust.connect("value-changed", self.on_scroll)
        self.append(self.scrolled)
        
        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_valign(Gtk.Align.START)
        self.flowbox.set_max_children_per_line(100)
        self.flowbox.set_homogeneous(True)
        self.flowbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flowbox.set_column_spacing(4)
        self.flowbox.set_row_spacing(12)
        self.flowbox.set_margin_start(12)
        self.flowbox.set_margin_end(12)
        self.flowbox.set_margin_top(12)
        self.flowbox.connect("child-activated", window._on_flowbox_child_activated)
        self.scrolled.set_child(self.flowbox)

        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_size_request(32, 32)
        self.loading_spinner.set_margin_top(16)
        self.loading_spinner.set_margin_bottom(16)
        self.loading_spinner.set_halign(Gtk.Align.CENTER)
        self.loading_spinner.set_visible(False)
        self.append(self.loading_spinner)

        # State per page
        self.current_page = 1
        self.loaded_genre = "All"
        self.loaded_catalog_id = "top"
        self.loaded_catalog_url = "https://v3-cinemeta.strem.io/manifest.json"
        self.loaded_query = ""
        self.is_fetching = False
        self.saved_scroll_pos = 0.0
        self.has_loaded_once = False
        self.seen_ids = set()

    def on_scroll(self, adj):
        if self.is_fetching: return
        if self.media_type in ("history", "favorites", "watched", "downloads", "addons"):
            return
        if adj.get_value() > 0 and adj.get_value() >= adj.get_upper() - adj.get_page_size() - 400:
            self.window.load_category_movies(self, page=self.current_page + 1)

class PopcornBoxWindow(Adw.ApplicationWindow):
    def is_fullscreen(self):
        return self.props.fullscreened

    def get_tab_button(self, media_type):
        mapping = {
            "movie": self.movies_btn,
            "series": self.tv_btn,
            "anime": self.anime_btn,
            "catalogs": getattr(self, "catalogs_btn", self.movies_btn),
            "favorites": self.fav_btn,
            "watched": self.watched_btn,
            "history": self.history_btn,
            "downloads": self.downloads_btn,
            "addons": self.addons_btn,
        }
        return mapping.get(media_type, self.movies_btn)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Popcorn Box")
        self.set_default_size(1100, 800)
        self.connect("close-request", self.on_close_request)
        
        # Global key controller to route MPV shortcuts
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_global_key_pressed)
        self.add_controller(key_ctrl)
        
        player.init_background_downloads()
        

        self.current_media_type = "movie"
        self.current_genre = "All"
        self.current_catalog_id = "top"
        self.current_query = ""
        self.current_page = 1
        self.is_fetching = False

        toolbar_view = Adw.ToolbarView()
        toolbar_view.set_extend_content_to_top_edge(True)
        self.toolbar_view = toolbar_view
        self.set_content(toolbar_view)
        
        # Auto-hide topbar when maximized/fullscreened on player tab
        self.connect("notify::maximized",    self._on_window_state_changed)
        self.connect("notify::fullscreened", self._on_window_state_changed)
        
        # Topbar
        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        topbar.set_css_classes(['topbar'])
        self.topbar_box = topbar
        
        # Left side
        left_topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        
        self.movies_btn = Gtk.Button(label="Movies")
        self.movies_btn.set_css_classes(['topbar-item', 'selected'])
        self.movies_btn.connect("clicked", lambda x: self.switch_category("movie", "All", self.movies_btn))
        left_topbar.append(self.movies_btn)
        
        self.tv_btn = Gtk.Button(label="Series")
        self.tv_btn.set_css_classes(['topbar-item'])
        self.tv_btn.connect("clicked", lambda x: self.switch_category("series", "All", self.tv_btn))
        left_topbar.append(self.tv_btn)
        
        self.anime_btn = Gtk.Button(label="Anime")
        self.anime_btn.set_css_classes(['topbar-item'])
        self.anime_btn.connect("clicked", lambda x: self.switch_category("anime", "All", self.anime_btn))
        left_topbar.append(self.anime_btn)
        
        self.catalogs_btn = Gtk.Button(label="Catalogs")
        self.catalogs_btn.set_css_classes(['topbar-item'])
        self.catalogs_btn.connect("clicked", lambda x: self.switch_category("catalogs", "All", self.catalogs_btn))
        left_topbar.append(self.catalogs_btn)
        
        self.fav_btn = Gtk.Button(icon_name="starred-symbolic")
        self.fav_btn.set_css_classes(['topbar-item'])
        self.fav_btn.set_tooltip_text("Favorites")
        self.fav_btn.connect("clicked", lambda x: self.switch_category("favorites", "All", self.fav_btn))
        left_topbar.append(self.fav_btn)
        
        self.watched_btn = Gtk.Button(icon_name="view-reveal-symbolic")
        self.watched_btn.set_css_classes(['topbar-item'])
        self.watched_btn.set_tooltip_text("Watched")
        self.watched_btn.connect("clicked", lambda x: self.switch_category("watched", "All", self.watched_btn))
        left_topbar.append(self.watched_btn)
        
        self.history_btn = Gtk.Button(icon_name="document-open-recent-symbolic")
        self.history_btn.set_css_classes(['topbar-item'])
        self.history_btn.set_tooltip_text("History")
        self.history_btn.connect("clicked", lambda x: self.switch_category("history", "All", self.history_btn))
        left_topbar.append(self.history_btn)
        
        self.downloads_btn = Gtk.Button(icon_name="folder-download-symbolic")
        self.downloads_btn.set_css_classes(['topbar-item'])
        self.downloads_btn.set_tooltip_text("Downloads")
        self.downloads_btn.connect("clicked", lambda x: self.switch_category("downloads", "All", self.downloads_btn))
        left_topbar.append(self.downloads_btn)
        
        self.addons_btn = Gtk.Button(icon_name="application-x-addon-symbolic")
        self.addons_btn.set_css_classes(['topbar-item'])
        self.addons_btn.set_tooltip_text("Addons")
        self.addons_btn.connect("clicked", lambda x: self.switch_category("addons", "All", self.addons_btn))
        left_topbar.append(self.addons_btn)
        
        self.player_btn = Gtk.Button(icon_name="multimedia-player-symbolic")
        self.player_btn.set_css_classes(['topbar-item'])
        self.player_btn.set_tooltip_text("Player")
        self.player_btn.connect("clicked", lambda x: self.switch_category("player", "All", self.player_btn))
        left_topbar.append(self.player_btn)
        
        topbar.append(left_topbar)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        topbar.append(spacer)
        
        # Middle controls
        mid_topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mid_topbar.set_valign(Gtk.Align.CENTER)
        
        self.genre_label = Gtk.Label(label="Genre")
        self.genre_label.set_css_classes(['dim-label'])
        mid_topbar.append(self.genre_label)
        self.movie_genres = ["All", "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy", "Film-Noir", "History", "Horror", "Music", "Musical", "Mystery", "Romance", "Sci-Fi", "Short", "Sport", "Thriller", "War", "Western"]
        self.series_genres = ["All", "Action & Adventure", "Animation", "Family", "Kids", "Comedy", "Drama", "Crime", "Mystery", "Sci-Fi & Fantasy", "Western", "War & Politics", "Reality", "Documentary", "Talk", "News", "Soap", "Romance", "Music", "Musical", "History"]
        self.anime_genres = ["All"]
        self.genre_counts_cache = {}
        
        self.genre_dropdown = Gtk.DropDown.new_from_strings(self.movie_genres)
        self.genre_dropdown.set_valign(Gtk.Align.CENTER)
        self.genre_dropdown.connect("notify::selected", self.on_genre_changed)
        mid_topbar.append(self.genre_dropdown)
        
        self.sort_label = Gtk.Label(label="Sort by")
        self.sort_label.set_css_classes(['dim-label'])
        mid_topbar.append(self.sort_label)
        
        self.standard_sorts = ["Trending", "Popularity", "Last Added", "Year", "Title", "Rating"]
        self.series_sorts = ["Trending", "Popularity", "Updated", "Year", "Name", "Rating"]
        self.anime_sorts = ["Trending", "Popularity", "Updated", "Year", "Name", "Rating"]
        self.sort_dropdown = Gtk.DropDown.new_from_strings(self.standard_sorts)
        self.sort_dropdown.set_valign(Gtk.Align.CENTER)
        self.sort_dropdown.connect("notify::selected", self.on_sort_changed)
        mid_topbar.append(self.sort_dropdown)
        
        topbar.append(mid_topbar)
        
        # Spacer
        spacer2 = Gtk.Box()
        spacer2.set_hexpand(True)
        topbar.append(spacer2)
        
        # Right controls
        right_topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        right_topbar.set_valign(Gtk.Align.CENTER)
        right_topbar.set_margin_end(16)
        
        self.search_btn = Gtk.Button(icon_name="system-search-symbolic")
        self.search_btn.set_css_classes(['flat', 'circular'])
        self.search_btn.set_tooltip_text("Search")
        
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search...")
        self.search_entry.connect("search-changed", self.on_search_changed)
        
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.search_revealer.set_transition_duration(250)
        self.search_revealer.set_child(self.search_entry)
        
        right_topbar.append(self.search_revealer)
        right_topbar.append(self.search_btn)
        
        def toggle_search(btn):
            is_revealed = self.search_revealer.get_reveal_child()
            self.search_revealer.set_reveal_child(not is_revealed)
            if not is_revealed:
                self.search_entry.grab_focus()
            else:
                if self.search_entry.get_text():
                    self.search_entry.set_text("")
                    
        self.search_btn.connect("clicked", toggle_search)
        
        # Escape key collapses and clears the search entry
        search_key_ctrl = Gtk.EventControllerKey()
        def on_search_key_pressed(controller, keyval, keycode, state):
            if keyval == Gdk.KEY_Escape:
                self.search_entry.set_text("")
                self.search_revealer.set_reveal_child(False)
                self.search_btn.grab_focus()
                return True
            return False
        search_key_ctrl.connect("key-pressed", on_search_key_pressed)
        self.search_entry.add_controller(search_key_ctrl)
        
        topbar.append(right_topbar)
        
        window_controls = Gtk.WindowControls(side=Gtk.PackType.END)
        topbar.append(window_controls)
        
        toolbar_view.add_top_bar(topbar)
        self.topbar_widget = topbar
        
        # Content Area (Stack)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        toolbar_view.set_content(self.stack)
        
        # Initialize Category pages
        self.category_pages = {}
        for m in ["movie", "series", "anime", "catalogs", "favorites", "watched", "history", "downloads", "addons"]:
            page = CategoryGridPage(m, self)
            self.category_pages[m] = page
            self.stack.add_named(page, f"grid_{m}")
        
        self.global_player = GlobalPlayerView()
        self.stack.add_named(self.global_player, "global_player")
        
        self.stack.connect("notify::visible-child", lambda *_: self._update_topbar_visibility())
        self.global_player.stack.connect("notify::visible-child", lambda *_: self._update_topbar_visibility())
        
        self.stack.set_visible_child_name("grid_movie")
        
        self.fetch_and_update_genre_counts("movie")
        self.load_category_movies(self.category_pages["movie"])

        _install_window_drag(self)
        
    def fetch_and_update_genre_counts(self, media_type):
        if media_type in ["favorites", "watched", "history", "downloads"]: return
        if media_type in self.genre_counts_cache:
            self.apply_genre_counts(media_type, self.genre_counts_cache[media_type])
            return
            
        def fetch():
            counts = api.fetch_genre_counts(media_type)
            self.genre_counts_cache[media_type] = counts
            GLib.idle_add(self.apply_genre_counts, media_type, counts)
        threading.Thread(target=fetch, daemon=True).start()
        
    def apply_genre_counts(self, media_type, counts):
        if self.current_media_type != media_type: return
        
        if media_type == "anime":
            base_genres = self.anime_genres
        elif media_type == "series":
            base_genres = self.series_genres
        else:
            base_genres = self.movie_genres
            
        new_strings = []
        for g in base_genres:
            if g == "All":
                new_strings.append("All")
            else:
                c = counts.get(g.lower(), counts.get(g, 0))
                if c > 0:
                    new_strings.append(f"{g} ({c})")
                else:
                    new_strings.append(g)
                    
        current_actual = "All"
        if hasattr(self, 'category_pages') and self.current_media_type in self.category_pages:
            current_actual = self.category_pages[self.current_media_type].loaded_genre
            
        self._ignore_genre_change = True
        self.genre_dropdown.set_model(Gtk.StringList.new(new_strings))
        
        for i, ns in enumerate(new_strings):
            if ns.split(' (')[0] == current_actual:
                self.genre_dropdown.set_selected(i)
                break
        self._ignore_genre_change = False
        
    def get_sorts_for_media(self, media_type):
        actual_type = "series" if media_type == "anime" else media_type
        catalogs = []
        for addon in database.get_addons():
            if addon.get("enabled", True):
                for catalog in addon.get("catalogs", []):
                    if catalog.get("type", "movie") == actual_type:
                        catalogs.append({
                            "id": catalog.get("id"),
                            "name": f"{addon.get('name')} - {catalog.get('name', catalog.get('id'))}",
                            "url": addon.get("manifest_url"),
                            "type": actual_type
                        })
        if not catalogs:
            catalogs.append({"id": "top", "name": "Top", "url": "https://v3-cinemeta.strem.io/manifest.json", "type": actual_type})
        return catalogs

    def get_all_addon_catalogs(self):
        catalogs = []
        for addon in database.get_addons():
            if addon.get("enabled", True):
                for catalog in addon.get("catalogs", []):
                    catalogs.append({
                        "id": catalog.get("id"),
                        "name": f"{addon.get('name')} - {catalog.get('name', catalog.get('id'))}",
                        "url": addon.get("manifest_url"),
                        "type": catalog.get("type", "movie")
                    })
        return catalogs

    def switch_category(self, media_type, genre, btn):
        if hasattr(self, 'topbar_box'):
            self.topbar_box.remove_css_class("transparent-topbar")
            
        self.movies_btn.remove_css_class("selected")
        self.tv_btn.remove_css_class("selected")
        self.anime_btn.remove_css_class("selected")
        self.fav_btn.remove_css_class("selected")
        self.watched_btn.remove_css_class("selected")
        self.history_btn.remove_css_class("selected")
        self.downloads_btn.remove_css_class("selected")
        self.addons_btn.remove_css_class("selected")
        self.catalogs_btn.remove_css_class("selected")
        self.player_btn.remove_css_class("selected")
        btn.add_css_class("selected")

        if media_type == "player":
            self.stack.set_visible_child_name("global_player")
            if hasattr(self, 'search_revealer'):
                self.search_revealer.set_reveal_child(False)
                if self.search_entry.get_text():
                    self.search_entry.set_text("")
            return

        target_page = self.category_pages[media_type]

        if hasattr(self, 'current_media_type') and self.current_media_type == media_type:
            # If clicked on already active category tab, reset its search if active
            if self.search_entry.get_text():
                self.search_entry.set_text("")
                if hasattr(self, 'search_revealer'):
                    self.search_revealer.set_reveal_child(False)
                self.stack.set_visible_child_name(f"grid_{media_type}")
                return
                
            self.stack.set_visible_child_name(f"grid_{media_type}")
            GLib.idle_add(lambda: target_page.scrolled.get_vadjustment().set_value(target_page.saved_scroll_pos) or False)
            return

        # Collapse/clear search ONLY when switching to a non-grid/local tab (unless both are movie/series/anime)
        is_main_media = media_type in ("movie", "series", "anime")
        was_main_media = getattr(self, 'current_media_type', None) in ("movie", "series", "anime")
        
        if not is_main_media or not was_main_media:
            # Clear search and collapse when leaving or entering local tabs/player
            if hasattr(self, 'search_revealer'):
                self.search_revealer.set_reveal_child(False)
                if self.search_entry.get_text():
                    self.search_entry.set_text("")

        # Restore topbar when leaving the player tab
        self._update_topbar_visibility()
        
        self.stack.set_visible_child_name(f"grid_{media_type}")
        
        self.current_media_type = media_type

        # Update dropdown models based on category
        self._ignore_genre_change = True
        
        self.genre_label.set_visible(media_type != "catalogs")
        self.genre_dropdown.set_visible(media_type != "catalogs")
        self.sort_label.set_text("Select Catalog" if media_type == "catalogs" else "Sort by")
        
        if media_type in ["favorites", "watched", "history", "downloads", "addons"]:
            self.genre_dropdown.set_sensitive(False)
            self.sort_dropdown.set_sensitive(False)
        elif media_type == "catalogs":
            self.current_sort_map = self.get_all_addon_catalogs()
            sort_names = [s["name"] for s in self.current_sort_map]
            self.sort_dropdown.set_model(Gtk.StringList.new(sort_names))
            self.genre_dropdown.set_sensitive(False)
            self.sort_dropdown.set_sensitive(True)
        else:
            self.current_sort_map = self.get_sorts_for_media(media_type)
            sort_names = [s["name"] for s in self.current_sort_map]
            self.sort_dropdown.set_model(Gtk.StringList.new(sort_names))
            
            if media_type == "anime":
                self.genre_dropdown.set_model(Gtk.StringList.new(self.anime_genres))
            elif media_type == "series":
                self.genre_dropdown.set_model(Gtk.StringList.new(self.series_genres))
            else: # movie
                self.genre_dropdown.set_model(Gtk.StringList.new(self.movie_genres))
                
            self.genre_dropdown.set_sensitive(True)
            self.sort_dropdown.set_sensitive(True)
            
        # Restore target page's genre selection
        if self.genre_dropdown.get_sensitive():
            model = self.genre_dropdown.get_model()
            found = False
            for idx in range(model.get_n_items()):
                item_str = model.get_string(idx)
                actual_genre = item_str.split(' (')[0]
                if actual_genre == target_page.loaded_genre:
                    self.genre_dropdown.set_selected(idx)
                    found = True
                    break
            if not found:
                self.genre_dropdown.set_selected(0)
                target_page.loaded_genre = "All"
                
        # Restore target page's sort selection
        if self.sort_dropdown.get_sensitive():
            found = False
            for idx, smap in enumerate(self.current_sort_map):
                if smap["id"] == target_page.loaded_catalog_id and smap["url"] == target_page.loaded_catalog_url:
                    self.sort_dropdown.set_selected(idx)
                    found = True
                    break
            if not found:
                self.sort_dropdown.set_selected(0)
                if self.current_sort_map:
                    first = self.current_sort_map[0]
                    target_page.loaded_catalog_id = first["id"]
                    target_page.loaded_catalog_url = first["url"]
                    if media_type == "catalogs":
                        target_page.catalog_type = first.get("type", "movie")
                else:
                    target_page.loaded_catalog_id = "trending"
                    target_page.loaded_catalog_url = None
                
        self._ignore_genre_change = False

        self.fetch_and_update_genre_counts(media_type)
        
        # If the target page has never loaded, OR if its loaded state doesn't match the current query
        if not target_page.has_loaded_once or target_page.loaded_query != self.current_query:
            self.load_category_movies(target_page, query=self.current_query)
        
    def on_genre_changed(self, dropdown, *args):
        if getattr(self, '_ignore_genre_change', False): return
        idx = dropdown.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION: return
        item = dropdown.get_model().get_string(idx)
        actual_genre = item.split(' (')[0]
        
        active_page = self.category_pages[self.current_media_type]
        if active_page.loaded_genre == actual_genre:
            return
            
        active_page.loaded_genre = actual_genre
        active_page.current_page = 1
        self.load_category_movies(active_page)
        
    def on_sort_changed(self, dropdown, *args):
        if getattr(self, '_ignore_genre_change', False): return
        idx = dropdown.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION: return
        
        active_page = self.category_pages[self.current_media_type]
        if not hasattr(self, 'current_sort_map') or idx >= len(self.current_sort_map):
            return
            
        selected_sort = self.current_sort_map[idx]
        new_catalog_id = selected_sort["id"]
        new_catalog_url = selected_sort["url"]
            
        if active_page.loaded_catalog_id == new_catalog_id and active_page.loaded_catalog_url == new_catalog_url:
            return
            
        active_page.loaded_catalog_id = new_catalog_id
        active_page.loaded_catalog_url = new_catalog_url
        if self.current_media_type == "catalogs":
            active_page.catalog_type = selected_sort.get("type", "movie")
            
        active_page.current_page = 1
        self.load_category_movies(active_page)
            
    def load_category_movies(self, category_page, query=None, page=1):
        media_type = category_page.media_type
        is_new_query = False
        
        if query is not None and query != category_page.loaded_query:
            category_page.loaded_query = query
            category_page.current_page = 1
            is_new_query = True
        else:
            if category_page.is_fetching: return
            
        category_page.is_fetching = True
        category_page.loading_spinner.set_visible(True)
        category_page.loading_spinner.start()
        
        request_query = category_page.loaded_query
        
        def _do_load():
            is_local = media_type in ("favorites", "watched", "history", "downloads", "addons")
            if page == 1:
                if not is_local:
                    while child := category_page.flowbox.get_first_child():
                        category_page.flowbox.remove(child)
                        
            def fetch():
                if media_type == "favorites":
                    movies = database.get_favorites() if page == 1 else []
                elif media_type == "watched":
                    movies = database.get_watched() if page == 1 else []
                elif media_type == "history":
                    movies = database.get_history() if page == 1 else []
                elif media_type == "downloads":
                    movies = database.get_downloads() if page == 1 else []
                elif media_type == "addons":
                    movies = database.get_addons() if page == 1 else []
                else:
                    actual_type = getattr(category_page, "catalog_type", "movie") if media_type == "catalogs" else media_type
                    movies = api.fetch_items(media_type=actual_type, query=request_query, genre=category_page.loaded_genre, catalog_id=category_page.loaded_catalog_id, catalog_url=category_page.loaded_catalog_url, page=page)
                GLib.idle_add(self.populate_category_movies, category_page, movies, page, request_query)
                
            threading.Thread(target=fetch, daemon=True).start()
            return False
            
        if is_new_query:
            GLib.timeout_add(300, _do_load)
        else:
            _do_load()
            
    def build_separated_view_for_page(self, category_page, items):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        category_page.scrolled.set_child(outer)

        buckets = {
            "Movies":  [i for i in items if i.get("type", "movie") == "movie"],
            "Series":  [i for i in items if i.get("type") == "series"],
            "Anime":   [i for i in items if i.get("type") == "anime"],
        }

        has_any = False
        for section_title, section_items in buckets.items():
            if not section_items:
                continue
            has_any = True

            header = Gtk.Label(label=section_title)
            header.set_halign(Gtk.Align.START)
            header.set_css_classes(["title-2"])
            header.set_margin_top(12)
            header.set_margin_bottom(8)
            outer.append(header)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_bottom(12)
            outer.append(sep)

            fb = Gtk.FlowBox()
            fb.set_valign(Gtk.Align.START)
            fb.set_max_children_per_line(100)
            fb.set_homogeneous(True)
            fb.set_selection_mode(Gtk.SelectionMode.NONE)
            fb.set_column_spacing(8)
            fb.set_row_spacing(12)
            fb.connect("child-activated", self._on_flowbox_child_activated)
            for item in section_items:
                def on_remove(movie_item, widget, m_type=category_page.media_type):
                    if m_type == "favorites":
                        database.remove_favorite(movie_item.get("id"))
                    elif m_type == "watched":
                        database.remove_watched(movie_item.get("id"))
                    elif m_type == "history":
                        database.remove_history(movie_item.get("id"))
                    
                    parent = widget.get_parent()
                    if parent:
                        flowbox = parent.get_parent()
                        if flowbox:
                            flowbox.remove(parent)
                        
                remove_cb = on_remove if category_page.media_type in ("favorites", "watched", "history") else None
                fb.append(MovieWidget(item, self.show_movie_details, on_remove_clicked=remove_cb))
            outer.append(fb)

        if not has_any:
            empty = Gtk.Label(label="Nothing here yet.")
            empty.set_css_classes(["dim-label"])
            empty.set_valign(Gtk.Align.CENTER)
            empty.set_vexpand(True)
            outer.append(empty)

    def build_downloads_view_for_page(self, category_page, items):
        listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        listbox.set_margin_start(12)
        listbox.set_margin_end(12)
        listbox.set_margin_top(12)
        listbox.set_margin_bottom(12)
        category_page.scrolled.set_child(listbox)
        
        if not items:
            empty = Gtk.Label(label="No downloads yet.")
            empty.set_css_classes(["dim-label"])
            empty.set_valign(Gtk.Align.CENTER)
            empty.set_vexpand(True)
            listbox.append(empty)
            return
            
        for dl in items:
            listbox.append(DownloadItemWidget(dl))

    def build_addons_view_for_page(self, category_page, addons):
        outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer_box.set_margin_start(24)
        outer_box.set_margin_end(24)
        outer_box.set_margin_top(24)
        outer_box.set_margin_bottom(24)
        
        # Header/Input section
        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        entry = Gtk.Entry()
        entry.set_placeholder_text("Search or paste Stremio addon manifest URL...")
        entry.set_hexpand(True)
        entry.connect("activate", lambda e: self.install_addon(e.get_text(), entry))
        input_box.append(entry)
        
        install_btn = Gtk.Button(label="Install")
        install_btn.set_css_classes(["suggested-action"])
        install_btn.connect("clicked", lambda b: self.install_addon(entry.get_text(), entry))
        input_box.append(install_btn)
        
        import_btn = Gtk.Button(label="Import")
        import_btn.connect("clicked", self._on_import_clicked)
        input_box.append(import_btn)
        
        export_btn = Gtk.Button(label="Export")
        export_btn.connect("clicked", self._on_export_clicked)
        input_box.append(export_btn)
        
        outer_box.append(input_box)
        
        # Addons List
        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.set_css_classes(["boxed-list"])
        
        if not addons:
            empty_label = Gtk.Label(label="No addons installed.")
            empty_label.set_css_classes(["dim-label"])
            empty_label.set_margin_top(24)
            outer_box.append(empty_label)
        else:
            for addon in addons:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                row.set_margin_top(8)
                row.set_margin_bottom(8)
                row.set_margin_start(12)
                row.set_margin_end(12)
                
                icon_image = Gtk.Image.new_from_icon_name("application-x-addon")
                icon_image.set_pixel_size(32)
                row.append(icon_image)
                
                info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                info_box.set_hexpand(True)
                
                name_version_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                name_label = Gtk.Label(label=addon.get("name", "Unknown Addon"))
                name_label.set_css_classes(["heading"])
                name_label.set_halign(Gtk.Align.START)
                name_version_box.append(name_label)
                
                version_label = Gtk.Label(label=f"v{addon.get('version', '0.0.1')}")
                version_label.set_css_classes(["dim-label", "caption"])
                version_label.set_halign(Gtk.Align.START)
                name_version_box.append(version_label)
                
                info_box.append(name_version_box)
                
                desc_label = Gtk.Label(label=addon.get("description", "No description provided."))
                desc_label.set_css_classes(["dim-label", "body"])
                desc_label.set_halign(Gtk.Align.START)
                desc_label.set_wrap(True)
                desc_label.set_max_width_chars(60)
                info_box.append(desc_label)
                
                row.append(info_box)
                
                switch = Gtk.Switch()
                switch.set_active(addon.get("enabled", True))
                switch.set_valign(Gtk.Align.CENTER)
                switch.connect("notify::active", lambda sw, pspec, a_id=addon.get("id"): self.toggle_addon(a_id, sw.get_active()))
                row.append(switch)
                
                copy_btn = Gtk.Button()
                copy_btn.set_icon_name("edit-copy-symbolic")
                copy_btn.set_css_classes(["flat"])
                copy_btn.set_valign(Gtk.Align.CENTER)
                copy_btn.set_tooltip_text("Copy Addon Metadata")
                copy_btn.connect("clicked", lambda b, a=addon: Gdk.Display.get_default().get_clipboard().set(a.get("manifest_url", "")))
                row.append(copy_btn)
                
                delete_btn = Gtk.Button()
                delete_btn.set_icon_name("user-trash-symbolic")
                delete_btn.set_css_classes(["destructive-action", "flat"])
                delete_btn.set_valign(Gtk.Align.CENTER)
                delete_btn.connect("clicked", lambda b, a_id=addon.get("id"): self.uninstall_addon(a_id))
                row.append(delete_btn)
                
                list_box.append(row)
            
            outer_box.append(list_box)
            
        category_page.scrolled.set_child(outer_box)

    def _on_export_clicked(self, btn):
        dialog = Gtk.FileDialog.new()
        dialog.set_initial_name("popcorn_addons.json")
        dialog.save(self, None, self._on_export_saved)
        
    def _on_export_saved(self, dialog, result):
        try:
            file = dialog.save_finish(result)
            if file:
                import json
                addons = database.get_addons()
                with open(file.get_path(), "w") as f:
                    json.dump(addons, f, indent=4)
                print(f"Exported addons to {file.get_path()}")
        except Exception as e:
            print(f"Export cancelled or failed: {e}")

    def _on_import_clicked(self, btn):
        dialog = Gtk.FileDialog.new()
        dialog.open(self, None, self._on_import_opened)
        
    def _on_import_opened(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                import json
                with open(file.get_path(), "r") as f:
                    imported = json.load(f)
                
                if isinstance(imported, list):
                    current_addons = database.get_addons()
                    existing_ids = {a.get("id") for a in current_addons if a.get("id")}
                    added = False
                    for addon in imported:
                        a_id = addon.get("id")
                        if a_id and a_id not in existing_ids:
                            current_addons.append(addon)
                            existing_ids.add(a_id)
                            added = True
                    
                    if added:
                        data = database._read_db()
                        data["addons"] = current_addons
                        database._write_db(data)
                        # Refresh UI
                        self.switch_category("addons", "All", self.addons_btn)
        except Exception as e:
            print(f"Import cancelled or failed: {e}")

    def populate_category_movies(self, category_page, movies, page, request_query=None):
        if request_query is not None and request_query != category_page.loaded_query:
            return
            
        category_page.is_fetching = False
        category_page.loading_spinner.stop()
        category_page.loading_spinner.set_visible(False)
        category_page.has_loaded_once = True
        
        media_type = category_page.media_type
        is_local = media_type in ("favorites", "watched", "history", "downloads", "addons")

        if media_type == "downloads":
            self.build_downloads_view_for_page(category_page, movies or [])
            return

        if media_type == "addons":
            self.build_addons_view_for_page(category_page, movies or [])
            return

        if is_local:
            self.build_separated_view_for_page(category_page, movies or [])
            return

        if not movies:
            return

        if category_page.scrolled.get_child() is not category_page.flowbox:
            category_page.scrolled.set_child(category_page.flowbox)

        if page == 1:
            category_page.seen_ids = set()

        added_count = 0
        for movie in movies:
            m_id = movie.get("id")
            if m_id:
                if m_id in category_page.seen_ids:
                    continue
                category_page.seen_ids.add(m_id)
            category_page.flowbox.append(MovieWidget(movie, self.show_movie_details))
            added_count += 1
            
        category_page.current_page = page

        if page > 1 and added_count == 0:
            return

        if page == 1 and not category_page.loaded_query:
            self.load_category_movies(category_page, page=2)
            return
            
        def check_overflow():
            adj = category_page.scrolled.get_vadjustment()
            if adj.get_upper() <= adj.get_page_size() + 50:
                self.load_category_movies(category_page, page=category_page.current_page + 1)
        GLib.timeout_add(100, check_overflow)
            
    def on_search_changed(self, entry):
        query = entry.get_text()
        self.current_query = query
        active_page = self.category_pages[self.current_media_type]
        self.load_category_movies(active_page, query=query, page=1)
        
    def show_movie_details(self, movie):
        imdb_id = movie.get("imdb_id") or movie.get("id")
        media_type = movie.get("type", "movie")
        print(f"{media_type}: {imdb_id}")

        if movie.get("medium_cover_image"):
            database.add_history({
                "id": movie.get("id"),
                "title": movie.get("title"),
                "year": movie.get("year"),
                "medium_cover_image": movie.get("medium_cover_image"),
                "type": movie.get("type", "movie"),
            })
        active_page = self.category_pages[self.current_media_type]
        active_page.saved_scroll_pos = active_page.scrolled.get_vadjustment().get_value()
        
        old_details = self.stack.get_child_by_name("details")
        if old_details and hasattr(old_details, "movie_stub") and old_details.movie_stub.get("id") == movie.get("id"):
            old_details._check_continue_watching(movie.get("id"))
            if hasattr(self, 'topbar_box'): self.topbar_box.add_css_class("transparent-topbar")
            self.stack.set_visible_child_name("details")
            return
            
        if old_details:
            leftover = self.stack.get_child_by_name("details_old")
            if leftover:
                self.stack.remove(leftover)
            
            page = self.stack.get_page(old_details)
            page.set_property("name", "details_old")
            
            GLib.timeout_add(1000, lambda: self.stack.remove(old_details) or False)
            
        details_page = MovieDetailsPage(movie, self.hide_movie_details)
        self.stack.add_named(details_page, "details")
        if hasattr(self, 'topbar_box'): self.topbar_box.add_css_class("transparent-topbar")
        self.stack.set_visible_child_name("details")

    def hide_movie_details(self):
        if hasattr(self, 'topbar_box'): self.topbar_box.remove_css_class("transparent-topbar")
        self.stack.set_visible_child_name(f"grid_{self.current_media_type}")
        active_page = self.category_pages[self.current_media_type]
        GLib.idle_add(lambda: active_page.scrolled.get_vadjustment().set_value(active_page.saved_scroll_pos) or False)

    def on_close_request(self, *args):
        player.stop_player()
        import os
        os._exit(0)
        return False

    def _is_player_visible(self):
        """True when the actual mpv video player tab is active."""
        return (
            hasattr(self, 'global_player') and
            self.stack.get_visible_child_name() == "global_player" and
            self.global_player.stack.get_visible_child_name() == "player"
        )

    def _on_global_key_pressed(self, controller, keyval, keycode, state):
        """CAPTURE-phase handler: forward ALL keys to MPV when player tab is showing."""
        if not self._is_player_visible():
            return False
        pw = self.global_player.player_widget
        if pw is None:
            return False
        return pw.send_key(keyval, keycode, state)

    def _update_topbar_visibility(self):
        """Show/hide the topbar based on window state and active tab."""
        if not hasattr(self, 'topbar_widget'):
            return
        hide = self._is_player_visible()
        self.topbar_widget.set_visible(not hide)

    def _on_window_state_changed(self, window, param):
        self._update_topbar_visibility()

    def _on_flowbox_child_activated(self, box, child):
        widget = child.get_child()
        if isinstance(widget, MovieWidget):
            widget.on_card_clicked_cb(widget.movie)



    def install_addon(self, url, entry_widget):
        url = url.strip()
        if not url:
            return
        
        if not (url.startswith("http://") or url.startswith("https://")):
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Invalid URL",
                body="Please enter a valid HTTP or HTTPS URL."
            )
            dialog.add_response("ok", "OK")
            dialog.connect("response", lambda d, r: d.destroy())
            dialog.present()
            return
            
        if "manifest.json" not in url:
            if url.endswith("/"):
                url += "manifest.json"
            else:
                url += "/manifest.json"
                
        entry_widget.set_sensitive(False)
        
        def fetch_thread():
            try:
                import urllib.request
                import json
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    manifest_data = json.loads(response.read().decode('utf-8'))
                    
                if not isinstance(manifest_data, dict) or "name" not in manifest_data:
                    raise ValueError("Invalid manifest format. Must contain a 'name' field.")
                
                addon_id = manifest_data.get("id", url)
                addon = {
                    "id": addon_id,
                    "name": manifest_data.get("name"),
                    "version": manifest_data.get("version", "1.0.0"),
                    "description": manifest_data.get("description", "No description."),
                    "icon": manifest_data.get("icon"),
                    "manifest_url": url,
                    "enabled": True,
                    "catalogs": manifest_data.get("catalogs", []),
                    "resources": manifest_data.get("resources", [])
                }
                
                database.add_addon(addon)
                
                def on_success():
                    entry_widget.set_text("")
                    entry_widget.set_sensitive(True)
                    self.load_category_movies(self.category_pages["addons"])
                    
                GLib.idle_add(on_success)
                
            except Exception as e:
                def on_error(err_msg=str(e)):
                    entry_widget.set_sensitive(True)
                    dialog = Adw.MessageDialog(
                        transient_for=self,
                        heading="Failed to install addon",
                        body=f"Error: {err_msg}"
                    )
                    dialog.add_response("ok", "OK")
                    dialog.connect("response", lambda d, r: d.destroy())
                    dialog.present()
                GLib.idle_add(on_error)
                
        threading.Thread(target=fetch_thread, daemon=True).start()

    def toggle_addon(self, addon_id, enabled):
        database.set_addon_enabled(addon_id, enabled)

    def uninstall_addon(self, addon_id):
        database.remove_addon(addon_id)
        self.load_category_movies(self.category_pages["addons"])
