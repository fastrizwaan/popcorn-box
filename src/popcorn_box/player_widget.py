import gi
import ctypes
import os
import subprocess
import signal
from gi.repository import Gdk, Gio, GLib, Gtk

try:
    import mpv
    HAS_MPV = True
except ImportError:
    HAS_MPV = False
    print("Warning: python-mpv not installed")

libegl = ctypes.CDLL("libEGL.so.1")
egl_get_proc_address = libegl.eglGetProcAddress
egl_get_proc_address.restype = ctypes.c_void_p
egl_get_proc_address.argtypes = [ctypes.c_char_p]

GL_FRAMEBUFFER_BINDING = 0x8CA6
libgl = ctypes.CDLL("libGL.so.1")
glGetIntegerv = libgl.glGetIntegerv
glGetIntegerv.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]

# Map GDK keyvals to MPV key names
_GDK_TO_MPV = {
    Gdk.KEY_space:      "SPACE",
    Gdk.KEY_Return:     "ENTER",
    Gdk.KEY_KP_Enter:   "ENTER",
    Gdk.KEY_Left:       "LEFT",
    Gdk.KEY_Right:      "RIGHT",
    Gdk.KEY_Up:         "UP",
    Gdk.KEY_Down:       "DOWN",
    Gdk.KEY_BackSpace:  "BS",
    Gdk.KEY_Delete:     "DEL",
    Gdk.KEY_Home:       "HOME",
    Gdk.KEY_End:        "END",
    Gdk.KEY_Page_Up:    "PGUP",
    Gdk.KEY_Page_Down:  "PGDWN",
    Gdk.KEY_F1:  "F1",  Gdk.KEY_F2:  "F2",  Gdk.KEY_F3:  "F3",
    Gdk.KEY_F4:  "F4",  Gdk.KEY_F5:  "F5",  Gdk.KEY_F6:  "F6",
    Gdk.KEY_F7:  "F7",  Gdk.KEY_F8:  "F8",  Gdk.KEY_F9:  "F9",
    Gdk.KEY_F10: "F10", Gdk.KEY_F11: "F11", Gdk.KEY_F12: "F12",
    # Modifier combos are handled by unicode char below for single chars
}


class PlayerWidget(Gtk.Box):
    """Embedded MPV player using libmpv + OpenGL."""

    def __init__(self, on_close_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_close_callback = on_close_callback

        self.overlay = Gtk.Overlay()
        self.gl_area = Gtk.GLArea()
        self.gl_area.set_vexpand(True)
        self.gl_area.set_hexpand(True)
        self.gl_area.set_can_focus(True)
        
        self.window_handle = Gtk.WindowHandle()
        self.window_handle.set_hexpand(True)
        self.window_handle.set_vexpand(True)
        self.window_handle.set_child(self.gl_area)
        self.overlay.set_child(self.window_handle)
        self.append(self.overlay)
        
        top_left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        top_left_box.set_halign(Gtk.Align.START)
        top_left_box.set_valign(Gtk.Align.START)
        top_left_box.set_margin_top(16)
        top_left_box.set_margin_start(16)
        
        self.back_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.back_btn.set_css_classes(["osd", "circular"])
        self.back_btn.connect("clicked", lambda x: self._do_go_back())
        top_left_box.append(self.back_btn)
        
        self.audio_norm_btn = Gtk.Button(icon_name="audio-volume-high-symbolic")
        self.audio_norm_btn.set_css_classes(["osd", "circular"])
        self.audio_norm_btn.set_tooltip_text("Audio Normalization")
        self.audio_norm_btn.connect("clicked", self._toggle_audio_norm)
        top_left_box.append(self.audio_norm_btn)
        
        self.overlay.add_overlay(top_left_box)
        
        self.info_label = Gtk.Label(label="")
        self.info_label.set_visible(False)
        self.media_info_label = Gtk.Label(label="")
        self.media_info_label.set_visible(False)
        
        self._current_info_text = ""
        self._current_media_title = ""
        
        self._hide_timeout_id = None
        self.current_item_id = None  # track which media is playing
        self.on_go_back = None       # callback to navigate back to source
        self.on_play_next = None     # callback to trigger next episode
        self.next_episode_data = None
        self._up_next_triggered = False
        self._is_playing = False
        self.keep_downloading = False
        self.fetching_next_episode = False
        self._waiting_at_eof = False
        self._playback_started = False
        self._inhibit_cookie = 0
        self._dbus_inhibit_cookie = 0
        self._portal_inhibit_fd = None
        self._systemd_inhibit_proc = None
        self._inhibit_keepalive_id = None
        
        self.up_next_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.up_next_box.set_halign(Gtk.Align.END)
        self.up_next_box.set_valign(Gtk.Align.END)
        self.up_next_box.set_margin_bottom(64)
        self.up_next_box.set_margin_end(32)
        self.up_next_box.set_css_classes(["osd"])
        self.up_next_box.set_visible(False)
        
        self.up_next_label = Gtk.Label(label="Up Next...")
        self.up_next_label.set_css_classes(["title-2"])
        self.up_next_box.append(self.up_next_label)
        
        self.up_next_btn = Gtk.Button(label="Play Now")
        self.up_next_btn.set_css_classes(["suggested-action", "pill"])
        self.up_next_btn.connect("clicked", lambda x: self._trigger_next_episode())
        self.up_next_box.append(self.up_next_btn)
        
        self.overlay.add_overlay(self.up_next_box)

        if not HAS_MPV:
            return

        # NOTE: mpv_inhibit_gnome.so is NOT loaded here because it is a C plugin
        # designed for the standalone mpv binary and does not function correctly
        # when libmpv is embedded via python-mpv with vo=libmpv (no real mpv window).
        # Screensaver inhibition is handled entirely in Python below.
        self.mpv = mpv.MPV(
            vo="libmpv",
            osc=True,
            ytdl=True,
            ytdl_raw_options="yes-playlist=",
            loglevel="warn",
            hwdec="auto",
            slang="en,eng,English",
            alang="en,eng,English",
            subs_fallback="yes",
            input_default_bindings=True,
            input_vo_keyboard=True,
            osd_font_size=28,
            osd_align_x="center",
            osd_align_y="top",
        )

        self.gl_area.connect("realize",   self._on_realize)
        self.gl_area.connect("unrealize", self._on_unrealize)
        self.gl_area.connect("render",    self._on_render)
        self.gl_area.connect("resize",    self._on_resize)

        # --- Mouse/scroll events on the GL area ---
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.gl_area.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.gl_area.add_controller(scroll)

        click = Gtk.GestureClick()
        click.set_button(0)
        click.connect("pressed",  self._on_click_pressed)
        click.connect("released", self._on_click_released)
        self.gl_area.add_controller(click)

        self.mpv.observe_property("eof-reached", self._on_eof)
        self.mpv.observe_property("idle-active", self._on_idle_change)
        self.mpv.observe_property("time-pos", self._on_time_pos)
        self.mpv.observe_property("duration", self._on_duration)
        self.mpv.observe_property("fullscreen", self._on_mpv_fullscreen_change)
        self.mpv.observe_property("pause", self._on_pause_change)
        self._current_duration = 0

    # ------------------------------------------------------------------
    # GL callbacks
    # ------------------------------------------------------------------
    def _on_realize(self, area):
        area.make_current()
        if hasattr(self, 'mpv_ctx') and self.mpv_ctx:
            try:
                self.mpv_ctx.free()
            except Exception:
                pass
                
        proc_addr_fn = mpv.MpvGlGetProcAddressFn(
            lambda _inst, name: egl_get_proc_address(name)
        )
        self.mpv_ctx = mpv.MpvRenderContext(
            self.mpv, "opengl",
            opengl_init_params={"get_proc_address": proc_addr_fn},
        )
        self.mpv_ctx.update_cb = lambda: GLib.idle_add(
            self.gl_area.queue_render
        )
        self.fbo = ctypes.c_int()

        root = self.get_root()
        if root:
            self._fs_handler_id = root.connect("notify::fullscreened", self._on_window_fullscreen_changed)

    def _on_unrealize(self, area):
        area.make_current()
        if hasattr(self, 'mpv_ctx') and self.mpv_ctx:
            try:
                self.mpv_ctx.free()
            except Exception as e:
                print(f"Error freeing mpv context: {e}")
            self.mpv_ctx = None

    def _on_mpv_fullscreen_change(self, name, value):
        GLib.idle_add(self._sync_fullscreen, value)

    def _sync_fullscreen(self, value):
        root = self.get_root()
        if not root:
            return
        is_fs = root.props.fullscreened
        if value and not is_fs:
            root.fullscreen()
        elif not value and is_fs:
            root.unfullscreen()

    def _on_window_fullscreen_changed(self, window, pspec):
        if HAS_MPV and hasattr(self, 'mpv') and self.mpv:
            is_fs = window.props.fullscreened
            try:
                if self.mpv.fullscreen != is_fs:
                    self.mpv.fullscreen = is_fs
            except Exception:
                pass

    def _on_render(self, area, _ctx):
        try:
            w = int(area.get_width()  * area.props.scale_factor)
            h = int(area.get_height() * area.props.scale_factor)
            if w <= 0 or h <= 0:
                return
            glGetIntegerv(GL_FRAMEBUFFER_BINDING, self.fbo)
            self.mpv_ctx.render(
                flip_y=True,
                opengl_fbo={
                    "w":   w,
                    "h":   h,
                    "fbo": self.fbo.value,
                },
            )
        except Exception as e:
            print(f"MPV render error: {e}")

    def _on_resize(self, area, w, h):
        self.gl_area.queue_render()

    # ------------------------------------------------------------------
    # Mouse input → MPV
    # ------------------------------------------------------------------
    def _update_mpv_osd(self):
        if not HAS_MPV or not hasattr(self, 'mpv') or not self.mpv:
            return
            
        # Top-left OSD: Title
        if self.back_btn.get_visible() and self._current_media_title:
            self.mpv.osd_msg1 = self._current_media_title
        else:
            self.mpv.osd_msg1 = ""
            
        # Bottom OSC: Download Statistics (fallback to title if empty)
        bottom_text = self._current_info_text or self._current_media_title or ""
        self.mpv.force_media_title = bottom_text

    def _hide_back_btn(self):
        def hide_ui():
            self.back_btn.set_visible(False)
            if hasattr(self, 'audio_norm_btn'): self.audio_norm_btn.set_visible(False)
            self._update_mpv_osd()
            self.set_cursor(Gdk.Cursor.new_from_name("none"))
        GLib.idle_add(hide_ui)
        self._hide_timeout_id = None
        return False

    def _on_motion(self, ctrl, x, y):
        # Ignore fake motion events caused by UI re-layouts (e.g., info_label text updates)
        if hasattr(self, '_last_x') and abs(self._last_x - x) < 1.0 and abs(self._last_y - y) < 1.0:
            return
        self._last_x = x
        self._last_y = y

        sf = self.gl_area.get_scale_factor()
        if HAS_MPV:
            try:
                self.mpv.command("mouse", int(x * sf), int(y * sf))
            except Exception:
                pass
                
        if not self.back_btn.get_visible():
            def show_ui():
                self.set_cursor(Gdk.Cursor.new_from_name("default"))
                self.back_btn.set_visible(True)
                if hasattr(self, 'audio_norm_btn'): self.audio_norm_btn.set_visible(True)
                self._update_mpv_osd()
            GLib.idle_add(show_ui)
        if self._hide_timeout_id:
            GLib.source_remove(self._hide_timeout_id)
        self._hide_timeout_id = GLib.timeout_add(2000, self._hide_back_btn)

    def _on_click_pressed(self, gesture, n_press, x, y):
        if n_press == 2:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            root = self.get_root()
            if hasattr(root, "is_fullscreen"):
                if root.is_fullscreen():
                    root.unfullscreen()
                else:
                    root.fullscreen()
            return
            
        btn = gesture.get_current_button()
        if btn == 3:  # Right-click → pause/resume
            if HAS_MPV:
                self.mpv.pause = not self.mpv.pause
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            return
        if btn == 1:
            self.mpv.command("keydown", "MBTN_LEFT")

    def _on_click_released(self, gesture, n_press, x, y):
        btn = gesture.get_current_button()
        if btn == 1:
            self.mpv.command("keyup", "MBTN_LEFT")

    def _on_scroll(self, ctrl, dx, dy):
        if dy > 0:
            self.mpv.command("keypress", "WHEEL_DOWN")
        elif dy < 0:
            self.mpv.command("keypress", "WHEEL_UP")
        return True

    # ------------------------------------------------------------------
    # Keyboard input → MPV
    # Called by the window-level CAPTURE controller in window.py
    # ------------------------------------------------------------------
    def _do_go_back(self):
        root = self.get_root()
        if hasattr(root, "is_fullscreen") and root.is_fullscreen():
            root.unfullscreen()
        self.keep_downloading = True
        self._is_playing = False
        if HAS_MPV:
            try:
                self.mpv.stop()
            except Exception as e:
                print(f"mpv stop error on go back: {e}")
        if callable(self.on_go_back):
            GLib.idle_add(self.on_go_back)

    def send_key(self, keyval, keycode, state):
        """Route a GTK keyval to mpv. Returns True if consumed."""
        if not HAS_MPV:
            return False

        # ESC / q / Q: exit fullscreen → pause and go back to source
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_q, Gdk.KEY_Q):
            self._do_go_back()
            return True

        # 'f': toggle GTK window fullscreen — MPV can't manage the host window
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            root = self.get_root()
            if hasattr(root, "is_fullscreen"):
                if root.is_fullscreen():
                    root.unfullscreen()
                else:
                    root.fullscreen()
            return True
            
        # Prevent opening the mpv console which traps the user because ESC is overridden
        if keyval in (Gdk.KEY_grave, Gdk.KEY_asciitilde):
            return True

        # Look up in the static table first
        mpv_key = _GDK_TO_MPV.get(keyval)

        if mpv_key is None:
            # For printable characters, pass directly to MPV (e.g. 'm', 'q', '9', '0')
            ch = chr(keyval) if 0x20 <= keyval <= 0x7e else None
            if ch:
                mpv_key = ch

        if mpv_key:
            try:
                self.mpv.command("keypress", mpv_key)
                return True
            except Exception:
                return False
        return False

    def update_info(self, text):
        """Update the overlay info label with streaming status."""
        self._current_info_text = text
        self._update_mpv_osd()

    def set_media_title(self, title):
        """Set the media info overlay label."""
        import re
        if title:
            # Normalize series titles
            title = re.sub(r'\s*-\s*Season\s*(\d+),\s*Ep\s*(\d+)', r' Season \1 - Episode \2', title)
        self._current_media_title = title or ""
        self._update_mpv_osd()

    # ------------------------------------------------------------------
    # Position and EOF
    # ------------------------------------------------------------------
    def _on_duration(self, name, value):
        if value: self._current_duration = value

    def _on_time_pos(self, name, value):
        if value and value > 0:
            if not self._playback_started:
                self._playback_started = True
                GLib.idle_add(self._sync_inhibit)
            self._last_time_pos = value
            
        if not value or not self._current_duration:
            return
            
        if getattr(self, 'current_item_id', None):
            from . import database
            key = self.current_item_id
            if getattr(self, 'current_season', None) is not None and getattr(self, 'current_episode', None) is not None:
                key = f"{self.current_item_id}_S{self.current_season}_E{self.current_episode}"
            if self._current_duration - value > 5:
                database.save_progress(key, value)
            else:
                database.save_progress(key, 0)

        remaining = self._current_duration - value
        
        if remaining <= 20 and self.next_episode_data and not self._up_next_triggered:
            def show_overlay():
                self.up_next_label.set_text(f"Up Next: {self.next_episode_data.get('title')} in {int(remaining)}s")
                self.up_next_box.set_visible(True)
            GLib.idle_add(show_overlay)
            
            if remaining <= 1:
                self._trigger_next_episode()
                
        elif remaining > 20 and self.up_next_box.get_visible():
            GLib.idle_add(self.up_next_box.set_visible, False)
            
    def _trigger_next_episode(self):
        if self._up_next_triggered: return
        self._up_next_triggered = True
        GLib.idle_add(self.up_next_box.set_visible, False)
        if callable(self.on_play_next):
            GLib.idle_add(self.on_play_next)

    def handle_eof_or_idle(self):
        try:
            dur = getattr(self, '_current_duration', 0) or 0
            pos = getattr(self, '_last_time_pos', 0) or 0
            if dur > 0 and (dur - pos) > 60:
                print(f"Premature EOF! Pos {pos} Dur {dur}. Aborting auto-play.")
                if self.on_close_callback:
                    GLib.idle_add(self.on_close_callback)
                return
        except Exception as e:
            print(f"Error checking position on EOF: {e}")
            
        if hasattr(self, 'on_video_finished') and callable(self.on_video_finished):
            GLib.idle_add(self.on_video_finished)
            
        if getattr(self, 'fetching_next_episode', False):
            print("EOF reached but still fetching next episode torrents. Waiting...")
            self._waiting_at_eof = True
            return
        
        if self.next_episode_data and callable(self.on_play_next):
            self._trigger_next_episode()
        elif self.on_close_callback:
            GLib.idle_add(self.on_close_callback)

    def check_eof_waiting(self):
        if getattr(self, '_waiting_at_eof', False):
            self._waiting_at_eof = False
            print("Finished fetching next episode. Resuming EOF handling.")
            if self.next_episode_data and callable(self.on_play_next):
                self._trigger_next_episode()
            elif self.on_close_callback:
                GLib.idle_add(self.on_close_callback)

    def _on_eof(self, name, value):
        if value and getattr(self, '_is_playing', False) and getattr(self, '_playback_started', False):
            self._is_playing = False
            self.handle_eof_or_idle()

    def _on_idle_change(self, name, value):
        GLib.idle_add(self._sync_inhibit)
        if value:
            if getattr(self, '_is_playing', False):
                self._is_playing = False
                if getattr(self, '_playback_started', False):
                    self.handle_eof_or_idle()
                else:
                    if hasattr(self, 'on_playback_failed') and callable(self.on_playback_failed):
                        GLib.idle_add(self.on_playback_failed)

    def _on_pause_change(self, name, value):
        GLib.idle_add(self._sync_inhibit)

    def _sync_inhibit(self):
        """Screensaver inhibition matching Cine's proven GTK app.inhibit() logic."""
        try:
            should_inhibit = not self.mpv.pause and not self.mpv.idle_active
        except Exception:
            should_inhibit = False

        if should_inhibit and getattr(self, "_inhibit_cookie", 0) == 0:
            app = Gtk.Application.get_default()
            root = self.get_root()
            if app and root:
                try:
                    self._inhibit_cookie = app.inhibit(
                        root,
                        Gtk.ApplicationInhibitFlags.IDLE,
                        "Playing Media"
                    )
                    print(f"[Inhibit] GTK inhibit succeeded, cookie={self._inhibit_cookie}")
                except Exception as e:
                    print(f"[Inhibit] GTK inhibit failed: {e}")
        elif not should_inhibit and getattr(self, "_inhibit_cookie", 0) != 0:
            app = Gtk.Application.get_default()
            if app:
                try:
                    app.uninhibit(self._inhibit_cookie)
                    print(f"[Inhibit] GTK uninhibited, cookie={self._inhibit_cookie}")
                except Exception as e:
                    print(f"[Inhibit] GTK uninhibit failed: {e}")
            self._inhibit_cookie = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def apply_audio_norm(self):
        from . import database
        norm_level = database.get_setting("audio_normalize")
        if norm_level is None or isinstance(norm_level, bool):
            norm_level = 16 if norm_level else 0
            database.set_setting("audio_normalize", norm_level)
            
        if HAS_MPV and hasattr(self, 'mpv') and self.mpv:
            try:
                if norm_level == 16:
                    self.mpv.af = "lavfi=[loudnorm=I=-16]"
                    self.audio_norm_btn.set_icon_name("audio-volume-medium-symbolic")
                    self.audio_norm_btn.set_tooltip_text("Audio Normalization: Normal (-16 LUFS)")
                elif norm_level == 8:
                    self.mpv.af = "lavfi=[loudnorm=I=-8]"
                    self.audio_norm_btn.set_icon_name("audio-volume-high-symbolic")
                    self.audio_norm_btn.set_tooltip_text("Audio Normalization: Extreme (-8 LUFS)")
                else:
                    self.mpv.af = ""
                    self.audio_norm_btn.set_icon_name("audio-volume-low-symbolic")
                    self.audio_norm_btn.set_tooltip_text("Audio Normalization: OFF")
            except Exception as e:
                print(f"Failed to set audio filter: {e}")

    def _toggle_audio_norm(self, btn):
        from . import database
        norm_level = database.get_setting("audio_normalize")
        if norm_level is None or isinstance(norm_level, bool):
            norm_level = 16 if norm_level else 0
            
        if norm_level == 0:
            new_level = 16
        elif norm_level == 16:
            new_level = 8
        else:
            new_level = 0
            
        database.set_setting("audio_normalize", new_level)
        self.apply_audio_norm()

    def _try_initial_inhibit(self):
        """Called 500ms after play() to try inhibiting once the widget is likely realized."""
        if getattr(self, "_is_playing", False):
            self._sync_inhibit()
        return False  # Do not repeat

    def play(self, url, sub_file=None):
        self._is_playing = True
        self._playback_started = False
        self.keep_downloading = False
        self._up_next_triggered = False
        GLib.idle_add(self.up_next_box.set_visible, False)
        # Inhibition is deferred to _on_time_pos (when playback actually starts)
        # to guarantee the widget is realized and get_root() returns a valid window.
        # But try now as well — belt and suspenders.
        GLib.timeout_add(500, self._try_initial_inhibit)
        self.apply_audio_norm()
        
        start_pos = 0
        if getattr(self, 'current_item_id', None):
            from . import database
            key = self.current_item_id
            if getattr(self, 'current_season', None) is not None and getattr(self, 'current_episode', None) is not None:
                key = f"{self.current_item_id}_S{self.current_season}_E{self.current_episode}"
            start_pos = database.get_progress(key)

        if HAS_MPV:
            if start_pos >= 5:
                print(f"Resuming playback at position {start_pos}s")
                self.mpv.loadfile(url, start=str(int(start_pos)))
            else:
                self.mpv.loadfile(url)
            if sub_file:
                def add_sub(retries=30):
                    if not HAS_MPV or retries <= 0: return False
                    if getattr(self, '_playback_started', False):
                        try:
                            self.mpv.command("sub-add", sub_file)
                            return False
                        except Exception as e:
                            print(f"Failed to add sub: {e}")
                            return False
                    GLib.timeout_add(500, lambda: add_sub(retries - 1))
                    return False
                GLib.timeout_add(500, add_sub)
            self.mpv.pause = False
            self.gl_area.grab_focus()

    def stop(self):
        self._is_playing = False
        self._sync_inhibit()
        if HAS_MPV:
            self.mpv.stop()

    def destroy(self):
        self._sync_inhibit()
        if hasattr(self, '_fs_handler_id'):
            root = self.get_root()
            if root:
                try:
                    root.disconnect(self._fs_handler_id)
                except Exception:
                    pass
        if HAS_MPV:
            try:
                self.mpv.terminate()
            except Exception:
                pass
