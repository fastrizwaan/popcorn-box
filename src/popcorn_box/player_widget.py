import gi
import ctypes
from gi.repository import Gdk, GLib, Gtk

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
        self.gl_area.set_hexpand(True)
        self.gl_area.set_vexpand(True)
        self.gl_area.set_focusable(True)
        self.overlay.set_child(self.gl_area)
        self.append(self.overlay)
        
        self.back_btn = Gtk.Button(icon_name="window-close-symbolic")
        self.back_btn.set_halign(Gtk.Align.END)
        self.back_btn.set_valign(Gtk.Align.START)
        self.back_btn.set_margin_top(16)
        self.back_btn.set_margin_end(16)
        # Style as a semi-transparent floating circle
        self.back_btn.set_css_classes(["osd", "circular"])
        self.back_btn.connect("clicked", lambda x: self._do_go_back())
        self.overlay.add_overlay(self.back_btn)
        
        self.info_label = Gtk.Label(label="")
        self.info_label.set_halign(Gtk.Align.CENTER)
        self.info_label.set_valign(Gtk.Align.START)
        self.info_label.set_margin_top(22)
        self.info_label.set_css_classes(["osd"])
        self.info_label.set_visible(False)
        self.overlay.add_overlay(self.info_label)
        
        self.media_info_label = Gtk.Label(label="")
        self.media_info_label.set_halign(Gtk.Align.CENTER)
        self.media_info_label.set_valign(Gtk.Align.START)
        self.media_info_label.set_margin_top(62)
        self.media_info_label.set_css_classes(["osd"])
        self.media_info_label.set_visible(False)
        self.overlay.add_overlay(self.media_info_label)
        
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

        self.mpv = mpv.MPV(
            vo="libmpv",
            osc=True,
            ytdl=True,
            ytdl_raw_options="yes-playlist=",
            loglevel="warn",
            hwdec="auto",
            slang="en,eng,English",
            subs_fallback="yes",
            input_default_bindings=True,
            input_vo_keyboard=True,
        )

        self.gl_area.connect("realize",  self._on_realize)
        self.gl_area.connect("render",   self._on_render)
        self.gl_area.connect("resize",   self._on_resize)

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
        proc_addr_fn = mpv.MpvGlGetProcAddressFn(
            lambda _inst, name: egl_get_proc_address(name)
        )
        self.mpv_ctx = mpv.MpvRenderContext(
            self.mpv, "opengl",
            opengl_init_params={"get_proc_address": proc_addr_fn},
        )
        self.mpv_ctx.update_cb = lambda: GLib.idle_add(
            self.gl_area.queue_render,
            priority=GLib.PRIORITY_HIGH_IDLE,
        )
        self.fbo = ctypes.c_int()

        root = self.get_root()
        if root:
            self._fs_handler_id = root.connect("notify::fullscreened", self._on_window_fullscreen_changed)

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
            glGetIntegerv(GL_FRAMEBUFFER_BINDING, self.fbo)
            self.mpv_ctx.render(
                flip_y=True,
                opengl_fbo={
                    "w":   int(area.get_width()  * area.props.scale_factor),
                    "h":   int(area.get_height() * area.props.scale_factor),
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
    def _hide_back_btn(self):
        self.back_btn.set_visible(False)
        self.info_label.set_visible(False)
        self.media_info_label.set_visible(False)
        self.set_cursor(Gdk.Cursor.new_from_name("none"))
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
                
        self.set_cursor(Gdk.Cursor.new_from_name("default"))
        self.back_btn.set_visible(True)
        if self.info_label.get_text():
            self.info_label.set_visible(True)
        if self.media_info_label.get_text():
            self.media_info_label.set_visible(True)
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
        self.info_label.set_text(text)
        if self.back_btn.get_visible() and text:
            self.info_label.set_visible(True)
        elif not text:
            self.info_label.set_visible(False)

    def set_media_title(self, title):
        """Set the media info overlay label."""
        import re
        if title:
            # Normalize series titles: "Name - Season X, Ep Y" -> "Name Season X - Episode Y"
            title = re.sub(r'\s*-\s*Season\s*(\d+),\s*Ep\s*(\d+)', r' Season \1 - Episode \2', title)
        self.media_info_label.set_text(title or "")
        if self.back_btn.get_visible() and title:
            self.media_info_label.set_visible(True)
        else:
            self.media_info_label.set_visible(False)

    # ------------------------------------------------------------------
    # Position and EOF
    # ------------------------------------------------------------------
    def _on_duration(self, name, value):
        if value: self._current_duration = value

    def _on_time_pos(self, name, value):
        if value and value > 0:
            self._playback_started = True
            
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
        if value:
            self._uninhibit_screensaver()
            if getattr(self, '_is_playing', False):
                self._is_playing = False
                if getattr(self, '_playback_started', False):
                    self.handle_eof_or_idle()
                else:
                    if hasattr(self, 'on_playback_failed') and callable(self.on_playback_failed):
                        GLib.idle_add(self.on_playback_failed)

    def _on_pause_change(self, name, value):
        # value is True if paused, False if playing
        if value:
            self._uninhibit_screensaver()
        else:
            if getattr(self, '_is_playing', False):
                self._inhibit_screensaver()

    def _inhibit_screensaver(self):
        if not getattr(self, "_inhibit_cookie", 0):
            app = Gtk.Application.get_default()
            if app:
                root = self.get_root()
                if not root:
                    # Defer inhibition to when the widget is mapped/realized if not yet attached
                    GLib.idle_add(self._inhibit_screensaver)
                    return
                try:
                    self._inhibit_cookie = app.inhibit(root, Gtk.ApplicationInhibitFlags.IDLE, "Playing media")
                    print(f"[PlayerWidget] Screensaver inhibited, cookie: {self._inhibit_cookie}")
                except Exception as e:
                    print(f"[PlayerWidget] Failed to inhibit screensaver: {e}")

    def _uninhibit_screensaver(self):
        cookie = getattr(self, "_inhibit_cookie", 0)
        if cookie:
            app = Gtk.Application.get_default()
            if app:
                try:
                    app.uninhibit(cookie)
                    print(f"[PlayerWidget] Screensaver uninhibited, cookie: {cookie}")
                except Exception as e:
                    print(f"[PlayerWidget] Failed to uninhibit screensaver: {e}")
            self._inhibit_cookie = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def play(self, url, sub_file=None):
        self._is_playing = True
        self._playback_started = False
        self.keep_downloading = False
        self._up_next_triggered = False
        GLib.idle_add(self.up_next_box.set_visible, False)
        self._inhibit_screensaver()
        
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
                # MPV requires the file to be loaded before adding subtitles. Retry for up to 5 seconds.
                def add_sub(retries=10):
                    if not HAS_MPV or retries <= 0: return False
                    try:
                        self.mpv.command("sub-add", sub_file)
                        return False
                    except Exception:
                        GLib.timeout_add(500, lambda: add_sub(retries - 1))
                        return False
                GLib.timeout_add(500, add_sub)
            self.mpv.pause = False
            self.gl_area.grab_focus()

    def stop(self):
        self._is_playing = False
        self._uninhibit_screensaver()
        if HAS_MPV:
            self.mpv.stop()

    def destroy(self):
        self._uninhibit_screensaver()
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
