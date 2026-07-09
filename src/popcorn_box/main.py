import sys
import os
import gi
import logging

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

# Suppress noisy Vulkan swapchain resize warnings (harmless artefacts)
os.environ.setdefault("GDK_DEBUG", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, Gdk

from popcorn_box.window import PopcornBoxWindow

class PopcornBoxApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.fastrizwaan.PopcornBox',
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        # Clean up legacy UUID-style session dirs from old versions
        import glob, shutil
        for d in glob.glob('/var/tmp/popcorn-box-????????'):  # old UUID format
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

        # Force Dark Mode
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        
        # Load custom CSS
        css_provider = Gtk.CssProvider()
        css_data = b"""
        window { background-color: #17181b; }
        .topbar { background-color: #111215; border-bottom: 1px solid #1a1b1f; padding-top: 6px;padding-bottom: 0px; padding-left: 0px; padding-right: 6px; transition: background-color 300ms ease; }
        .topbar.transparent-topbar { background-color: rgba(17, 18, 21, 0.1); border-bottom-color: rgba(26, 27, 31, 0.1); }
        .topbar-item { padding-top: 0px; padding-bottom: 6px; padding-left: 18px; padding-right: 18px; border-radius: 0; margin-top: 6px; margin-bottom: 0px; transition: all 200ms ease; font-weight: bold; color: rgba(255, 255, 255, 0.6); background: transparent; border: none; border-bottom: 3px solid transparent; outline: none; box-shadow: none; }
        .topbar-item:hover, .topbar-item:focus, .topbar-item:focus-visible { color: #ffffff; background: transparent; outline: none; box-shadow: none; }
        .topbar-item:active { background: transparent; border-bottom: 3px solid transparent; outline: none; box-shadow: none; }
        .topbar-item.selected { border-bottom: 3px solid #2f79c3; color: #ffffff; }
        
        button.suggested-action { background-color: #25405b; color: #ffffff; font-weight: bold; border: none; }
        button.suggested-action:hover { background-color: #2e5175; }
        
        flowboxchild { background: none; outline: none; box-shadow: none; padding: 0; margin: 0; }
        flowboxchild:hover, flowboxchild:focus, flowboxchild:active, flowboxchild:selected { background: none; outline: none; box-shadow: none; }
        
        .pt-card { background-color: transparent; border-radius: 8px; transition: all 200ms ease; }
        .pt-card:hover { transform: scale(1.05); }
        .dl-row { background-color: transparent; border-radius: 8px; transition: background-color 200ms ease; }
        .dl-row:hover { background-color: rgba(255, 255, 255, 0.05); }
        .pt-card-title { font-weight: bold; font-size: 13px; margin-top: 8px; color: #fff; }
        .pt-card-year { font-size: 11px; color: rgba(255, 255, 255, 0.5); }
        
        .backdrop-overlay { background: linear-gradient(to top, #17181b 10%, rgba(23, 24, 27, 0.5) 100%); }
        
        button.flat.g-button { color: #4285f4; font-weight: 900; font-size: 15px; padding: 0; min-width: 28px; min-height: 28px; border-radius: 9999px; }
        button.flat.g-button:hover { background-color: rgba(66, 133, 244, 0.15); color: #ffffff; }
        button.flat.imdb-link-btn { color: #f5c518; padding: 0px 4px; font-weight: bold; border-bottom: 1px dashed #f5c518; border-radius: 0; background: transparent; }
        button.flat.imdb-link-btn:hover { color: #ffffff; border-bottom-style: solid; background: transparent; }
        button.flat.circular { padding: 4px; min-width: 28px; min-height: 28px; border-radius: 9999px; }
        """
        css_provider.load_from_data(css_data)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), 
            css_provider, 
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        win = self.props.active_window
        if not win:
            win = PopcornBoxWindow(application=self)
        win.present()

def main(args):
    app = PopcornBoxApp()
    return app.run(args)

if __name__ == '__main__':
    sys.exit(main(sys.argv))
