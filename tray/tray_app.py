"""
CastBridge Tray App
====================
System tray icon for the IPTV Relay server.
- Green icon = server running
- Red icon = server stopped
- Right-click menu: Start/Stop, Open Web UI, Auto-start, Quit
"""

import os, sys, threading, subprocess, time, webbrowser, winreg
from PIL import Image, ImageDraw

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# When running as EXE, look in the EXE's folder; otherwise script folder
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
RELAY_SCRIPT = os.path.join(SCRIPT_DIR, "iptv_relay.py")
LOGO_PATH = os.path.join(SCRIPT_DIR, "logo.png")
if not os.path.exists(LOGO_PATH):
    LOGO_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "iptv-relay", "logo.png")
WEB_PORT = 8080
APP_NAME = "CastBridge"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class CastBridgeTray:
    def __init__(self):
        self.server_proc = None
        self.running = False
        self.tray = None

    def create_icon(self, color):
        """Create a simple colored circle icon."""
        if os.path.exists(LOGO_PATH):
            try:
                img = Image.open(LOGO_PATH).resize((64, 64))
                return img
            except Exception:
                pass
        # Fallback: colored circle
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=color)
        return img

    def start_server(self):
        if self.running:
            return
        if getattr(sys, 'frozen', False):
            # Running as EXE: import relay and run in thread
            sys.path.insert(0, SCRIPT_DIR)
            import iptv_relay
            self.server_thread = threading.Thread(target=iptv_relay.main, daemon=True)
            self.server_thread.start()
        else:
            # Running as script: subprocess
            self.server_proc = subprocess.Popen(
                [sys.executable, RELAY_SCRIPT],
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=SCRIPT_DIR,
            )
        self.running = True
        self.update_icon()
        print(f"[TRAY] Server started")

    def stop_server(self):
        if not self.running:
            return
        if self.server_proc:
            self.server_proc.kill()
            try:
                self.server_proc.wait(timeout=5)
            except Exception:
                pass
            self.server_proc = None
        # Thread-based server can't be stopped cleanly, but will die with process
        self.running = False
        self.update_icon()
        print("[TRAY] Server stopped")

    def toggle_server(self, icon, item):
        if self.running:
            self.stop_server()
        else:
            self.start_server()

    def open_webui(self, icon, item):
        webbrowser.open(f"http://localhost:{WEB_PORT}")

    def is_autostart(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False

    def toggle_autostart(self, icon, item):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_ALL_ACCESS)
            if self.is_autostart():
                winreg.DeleteValue(key, APP_NAME)
                print("[TRAY] Auto-start disabled")
            else:
                exe_path = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
                print("[TRAY] Auto-start enabled")
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[TRAY] Auto-start error: {e}")

    def quit_app(self, icon, item):
        self.stop_server()
        icon.stop()

    def update_icon(self):
        if self.tray:
            color = (29, 185, 84) if self.running else (229, 57, 53)
            self.tray.icon = self.create_icon(color)
            self.tray.title = f"{APP_NAME} - {'Running' if self.running else 'Stopped'}"

    def monitor_server(self):
        """Watch server process, restart if crashed."""
        while True:
            time.sleep(5)
            if self.running and self.server_proc:
                if self.server_proc.poll() is not None:
                    print("[TRAY] Server crashed, restarting...")
                    self.running = False
                    self.start_server()

    def run(self):
        import pystray
        from pystray import MenuItem as item

        menu = pystray.Menu(
            item("Open Web UI", self.open_webui, default=True),
            item(
                lambda text: f"{'Stop' if self.running else 'Start'} Server",
                self.toggle_server,
            ),
            pystray.Menu.SEPARATOR,
            item(
                "Start on Login",
                self.toggle_autostart,
                checked=lambda item: self.is_autostart(),
            ),
            pystray.Menu.SEPARATOR,
            item("Quit", self.quit_app),
        )

        self.tray = pystray.Icon(
            APP_NAME,
            self.create_icon((29, 185, 84)),
            f"{APP_NAME} - Starting...",
            menu,
        )

        # Start server automatically
        self.start_server()

        # Monitor thread
        threading.Thread(target=self.monitor_server, daemon=True).start()

        # Run tray (blocks)
        self.tray.run()


if __name__ == "__main__":
    app = CastBridgeTray()
    app.run()
