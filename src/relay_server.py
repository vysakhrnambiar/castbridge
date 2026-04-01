"""
IPTV Relay Server — Download & Play (v2)
=========================================
Browse IPTV -> Download movie -> Play: Video on Chromecast (muted), Audio on Sonos
Features: seek, pause, volume, auto-sync, downloaded movies list

Usage: python _scripts/iptv_relay.py
"""

import os, sys, json, socket, time, re, shutil, tempfile
import threading, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import urlopen, Request

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ─── Config ───────────────────────────────────────────────
# Find ffmpeg: same folder > PATH > common locations
def _find_ffmpeg():
    # Next to this script
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
    if os.path.exists(local):
        return local
    # In PATH
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Common Windows locations
    for p in [
        os.path.expanduser("~/AppData/Local/FFmpeg/ffmpeg.exe"),
        "C:/FFmpeg/bin/ffmpeg.exe",
        "C:/Program Files/FFmpeg/bin/ffmpeg.exe",
    ]:
        if os.path.exists(p):
            return p
    return "ffmpeg"  # Hope it's in PATH

FFMPEG = _find_ffmpeg()
WEB_PORT = 8080
AUDIO_PORT = 8766
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "iptv_cache")
DOWNLOAD_DIR = os.path.join(SCRIPT_DIR, "iptv_downloads")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Config File ─────────────────────────────────────────
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

def load_config():
    defaults = {
        "iptv_server": "",
        "iptv_username": "",
        "iptv_password": "",
        "sonos_ip": "",
        "sonos_name": "",
        "chromecast_name": "",
        "cast_mode": "split",
        "default_volume": 60,
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

# ─── State ────────────────────────────────────────────────
state = {
    "iptv_server": config["iptv_server"],
    "iptv_user": config["iptv_username"],
    "iptv_pass": config["iptv_password"],
    "playing": False,
    "channel_name": "",
    "phase": "idle",
    "download_pct": 0,
    "elapsed": 0,
    "duration": 0,
    "play_start": 0,
    "paused": False,
    "sync_diff": 0,
}

hls_dir = None
cast_obj = None
cast_browser = None
pc_ip = None
sync_thread = None


# ─── Utilities ────────────────────────────────────────────
def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def iptv_api(action=None, **params):
    base = state["iptv_server"].rstrip("/")
    url = f"{base}/player_api.php?username={state['iptv_user']}&password={state['iptv_pass']}"
    if action:
        url += f"&action={action}"
    for k, v in params.items():
        url += f"&{k}={v}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def stream_url(stream_id, ext="ts", stream_type="live"):
    base = state["iptv_server"].rstrip("/")
    u, p = state["iptv_user"], state["iptv_pass"]
    if stream_type == "vod":
        return f"{base}/movie/{u}/{p}/{stream_id}.{ext}"
    if stream_type == "series":
        return f"{base}/series/{u}/{p}/{stream_id}.{ext}"
    return f"{base}/live/{u}/{p}/{stream_id}.m3u8"


# ─── Cache ────────────────────────────────────────────────
def cache_path(key):
    return os.path.join(CACHE_DIR, re.sub(r'[^\w\-]', '_', key) + ".json")

def cache_read(key):
    p = cache_path(key)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def cache_write(key, data):
    with open(cache_path(key), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

def iptv_api_cached(action, refresh=False, **params):
    key = action + ("_" + "_".join(f"{k}_{v}" for k, v in sorted(params.items())) if params else "")
    if not refresh:
        cached = cache_read(key)
        if cached is not None:
            return cached
    data = iptv_api(action, **params)
    cache_write(key, data)
    return data


# ─── Download ────────────────────────────────────────────
def download_stream(url, dest_path):
    print(f"[DL] Downloading: {url}")
    state["phase"] = "downloading"
    state["download_pct"] = 0
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urlopen(req, timeout=30)
    total = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    with open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                state["download_pct"] = int(downloaded * 100 / total)
            if downloaded % (1024 * 1024) < 65536:
                print(f"[DL] {downloaded//(1024*1024)}MB / {total//(1024*1024) if total else '?'}MB ({state['download_pct']}%)")
    print(f"[DL] Done: {os.path.getsize(dest_path)//(1024*1024)}MB")
    state["download_pct"] = 100


# ─── Convert ─────────────────────────────────────────────
def convert_to_hls_and_mp3(video_path):
    """Convert video to HLS + MP3. Reuses existing conversion if available."""
    global hls_dir

    # Use a persistent dir next to the video file (not temp)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    hls_dir = os.path.join(DOWNLOAD_DIR, base_name + "_hls")
    m3u8 = os.path.join(hls_dir, "stream.m3u8")
    mp3 = os.path.join(hls_dir, "audio.mp3")

    # Check if already converted
    if os.path.exists(m3u8) and os.path.exists(mp3):
        segs = len([f for f in os.listdir(hls_dir) if f.endswith(".ts")])
        if segs > 0:
            print(f"[CONV] Already converted: {segs} segs, reusing!")
            # Get duration from m3u8
            dur = 0
            with open(m3u8, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#EXTINF:"):
                        dur += float(line.split(":")[1].split(",")[0])
            state["duration"] = int(dur)
            return True

    # Need to convert
    state["phase"] = "converting"
    os.makedirs(hls_dir, exist_ok=True)
    seg_pat = os.path.join(hls_dir, "seg%05d.ts")

    print(f"[CONV] Converting...")
    t0 = time.time()
    proc = subprocess.run([
        FFMPEG, "-hide_banner", "-y", "-i", video_path,
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "32k", "-ac", "1",
        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
        "-hls_segment_filename", seg_pat, m3u8,
        "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-ac", "2", mp3,
    ], capture_output=True, text=True)

    if proc.returncode != 0:
        print(f"[CONV] Error: {proc.stderr[:500]}")
        return False

    segs = len([f for f in os.listdir(hls_dir) if f.endswith(".ts")])
    print(f"[CONV] Done: {segs} segs, {os.path.getsize(mp3)//1024}KB audio, {time.time()-t0:.1f}s")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)", proc.stderr)
    if m:
        state["duration"] = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
    return True


# ─── Playback ────────────────────────────────────────────
def start_playback():
    global cast_obj, cast_browser
    state["phase"] = "casting"
    cast_name = get_cast_name()
    print(f"[PLAY] Casting to {cast_name}...")

    import pychromecast
    casts, browser = pychromecast.get_listed_chromecasts(friendly_names=[cast_name])
    cast_browser = browser
    if not casts:
        print("[PLAY] Chromecast not found!")
        state["phase"] = "error"
        browser.stop_discovery()
        return False

    cast = casts[0]
    cast.wait()
    cast_obj = cast
    is_single = state.get("cast_mode", "split") == "single"
    cast.set_volume(1.0 if is_single else 0.01)
    print(f"[PLAY] Mode: {'Single (CC audio)' if is_single else 'Split (Sonos audio)'}")

    hls_url = f"http://{pc_ip}:{WEB_PORT}/stream.m3u8"
    mc = cast.media_controller
    mc.play_media(hls_url, "application/x-mpegurl", stream_type="BUFFERED",
                  title=state.get("channel_name", "Movie"))

    print("[PLAY] Waiting for video...")
    time.sleep(5)
    for i in range(60):
        time.sleep(1)
        try:
            mc.update_status()
        except Exception:
            pass
        st = mc.status.player_state if mc.status else "?"
        if st == "PLAYING":
            print(f"[PLAY] Video playing after {i+5}s!")
            break
        if st == "IDLE" and i > 15:
            mc.play_media(hls_url, "application/x-mpegurl", stream_type="BUFFERED",
                          title=state.get("channel_name", "Movie"))
            time.sleep(5)

    # Resume from saved position if available
    resume_pos = state.get("resume_from", 0)
    if resume_pos > 0:
        print(f"[PLAY] Seeking to resume position: {resume_pos}s")
        try:
            mc.seek(resume_pos)
            time.sleep(2)
        except Exception as e:
            print(f"[PLAY] Resume seek error: {e}")

    # Start Sonos audio (skip in single cast mode)
    if not is_single:
        print("[PLAY] Starting Sonos audio...")
        audio_url = f"http://{pc_ip}:{AUDIO_PORT}/audio.mp3"
        try:
            import soco
            sp = soco.SoCo(get_sonos_ip())
            sp.play_uri(audio_url, title=state.get("channel_name", "Movie"))
            sp.volume = 60
            if resume_pos > 0:
                time.sleep(2)
                h = resume_pos // 3600
                m = (resume_pos % 3600) // 60
                s = resume_pos % 60
                try:
                    sp.seek(f"{h:02d}:{m:02d}:{s:02d}")
                    print(f"[PLAY] Sonos seeked to {h:02d}:{m:02d}:{s:02d}")
                except Exception:
                    pass
            print("[PLAY] Sonos playing!")
        except Exception as e:
            print(f"[PLAY] Sonos error: {e}")
    else:
        print("[PLAY] Single cast mode - audio on Chromecast")

    state["phase"] = "playing"
    state["playing"] = True
    state["play_start"] = time.time()
    state["paused"] = False
    state["resume_from"] = 0
    return True


def stop_playback():
    global cast_obj, cast_browser, hls_dir, sync_thread
    # Save resume position before stopping
    if state.get("playing") and state.get("channel_name"):
        try:
            cc, _ = get_positions()
            if cc > 10:  # Only save if played more than 10s
                save_resume_position(state["channel_name"], cc)
                print(f"[RESUME] Saved position: {cc:.0f}s for {state['channel_name']}")
        except Exception:
            pass

    state["playing"] = False
    state["phase"] = "idle"
    state["paused"] = False
    sync_thread = None

    try:
        import soco
        soco.SoCo(get_sonos_ip()).stop()
    except Exception:
        pass
    if cast_obj:
        try:
            cast_obj.quit_app()
        except Exception:
            pass
        cast_obj = None
    if cast_browser:
        try:
            cast_browser.stop_discovery()
        except Exception:
            pass
        cast_browser = None
    # Keep hls_dir for instant replay (persistent conversion)
    hls_dir = None


def pause_playback(paused):
    state["paused"] = paused
    state["phase"] = "paused" if paused else "playing"
    if cast_obj:
        try:
            mc = cast_obj.media_controller
            if paused:
                mc.pause()
            else:
                mc.play()
        except Exception:
            pass
    try:
        import soco
        sp = soco.SoCo(get_sonos_ip())
        if paused:
            sp.pause()
        else:
            sp.play()
    except Exception:
        pass


def set_volume(vol):
    try:
        import soco
        soco.SoCo(get_sonos_ip()).volume = vol
    except Exception:
        pass


def seek_to(seconds, auto=False):
    """Seek Chromecast. If manual seek, also seek Sonos and pause auto-sync."""
    if not auto:
        # Manual seek: pause auto-sync for 30s
        state["sync_paused_until"] = time.time() + 30
        # Seek Sonos too
        try:
            import soco
            sp = soco.SoCo(get_sonos_ip())
            h = int(seconds) // 3600
            m = (int(seconds) % 3600) // 60
            s = int(seconds) % 60
            sp.seek(f"{h:02d}:{m:02d}:{s:02d}")
            print(f"[SEEK] Sonos -> {h:02d}:{m:02d}:{s:02d}")
        except Exception as e:
            print(f"[SEEK] Sonos seek error: {e}")
    if cast_obj:
        try:
            mc = cast_obj.media_controller
            offset = state.get("sync_offset", 0)
            target = seconds + offset if not auto else seconds
            mc.seek(max(0, target))
            print(f"[SEEK] Chromecast -> {target:.0f}s {'(auto)' if auto else ''}")
        except Exception as e:
            print(f"[SEEK] Error: {e}")


def get_positions():
    """Get current playback positions of both devices."""
    cc_time = 0
    sonos_time = 0
    try:
        if cast_obj:
            mc = cast_obj.media_controller
            mc.update_status()
            cc_time = mc.status.current_time if mc.status else 0
    except Exception:
        pass
    try:
        import soco
        sp = soco.SoCo(get_sonos_ip())
        info = sp.get_current_track_info()
        pos = info.get("position", "0:00:00")
        parts = pos.split(":")
        sonos_time = int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
    except Exception:
        pass
    return cc_time, sonos_time


def start_sync_thread():
    """Auto-sync: every 15s check drift, adjust Chromecast if > 2s."""
    global sync_thread

    def sync_loop():
        while state.get("playing") and not state.get("paused"):
            time.sleep(15)
            if not state.get("playing") or state.get("paused"):
                break
            cc, sonos = get_positions()
            offset = state.get("sync_offset", 0)
            target = sonos + offset  # Where Chromecast should be
            diff = cc - target
            state["sync_diff"] = round(diff, 1)
            state["elapsed"] = int(sonos) if sonos > 0 else int(cc)
            if abs(diff) > 3 and time.time() > state.get("sync_paused_until", 0):
                print(f"[SYNC] Drift: {diff:+.1f}s (offset:{offset}) -> seeking CC to {target}")
                seek_to(max(0, target), auto=True)
            elif abs(diff) > 1:
                print(f"[SYNC] Minor drift: {diff:+.1f}s (ok)")

    sync_thread = threading.Thread(target=sync_loop, daemon=True)
    sync_thread.start()


# ─── Downloaded Movies List ──────────────────────────────
def handle_discover():
    sonos_list = []
    cast_list = []
    try:
        import soco
        for sp in soco.discover(timeout=5) or []:
            sonos_list.append({"name": sp.player_name, "ip": sp.ip_address})
    except Exception as e:
        print(f"[DISCOVER] Sonos error: {e}")
    try:
        import pychromecast
        casts, browser = pychromecast.get_chromecasts(timeout=8)
        for c in casts:
            cast_list.append({"name": c.name, "model": c.model_name, "ip": str(c.cast_info.host)})
        browser.stop_discovery()
    except Exception as e:
        print(f"[DISCOVER] Cast error: {e}")
    return {"ok": True, "sonos": sonos_list, "chromecast": cast_list}


def get_sonos_ip():
    return state.get("sonos_ip", config.get("sonos_ip", ""))

def get_cast_name():
    return state.get("cast_name", config.get("chromecast_name", ""))


# ─── Resume Positions ─────────────────────────────────────
RESUME_FILE = os.path.join(DOWNLOAD_DIR, "_resume.json")

def load_resume_positions():
    if os.path.exists(RESUME_FILE):
        with open(RESUME_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_resume_position(filename, seconds):
    data = load_resume_positions()
    data[filename] = int(seconds)
    with open(RESUME_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

def get_resume_position(filename):
    return load_resume_positions().get(filename, 0)


# ─── Download Queue ──────────────────────────────────────
download_queue = []  # list of {name, url, ext, status, pct}
download_lock = threading.Lock()

def queue_download(name, url, ext):
    """Add a movie to the download queue."""
    safe_name = re.sub(r'[^\w\-\s]', '', name)[:50].strip()
    dest = os.path.join(DOWNLOAD_DIR, f"{safe_name}.{ext}")
    if os.path.exists(dest):
        return {"ok": False, "error": "Already downloaded"}
    item = {"name": name, "safe_name": safe_name, "url": url, "ext": ext,
            "dest": dest, "status": "queued", "pct": 0}
    with download_lock:
        # Don't add duplicates
        for q in download_queue:
            if q["dest"] == dest:
                return {"ok": False, "error": "Already in queue"}
        download_queue.append(item)
    return {"ok": True}

def download_worker():
    """Background thread that processes the download queue."""
    while True:
        time.sleep(2)
        item = None
        with download_lock:
            for q in download_queue:
                if q["status"] == "queued":
                    q["status"] = "downloading"
                    item = q
                    break
        if not item:
            continue
        try:
            print(f"[DL-Q] Starting: {item['name']}")
            req = Request(item["url"], headers={"User-Agent": "Mozilla/5.0"})
            resp = urlopen(req, timeout=30)
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(item["dest"], "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        item["pct"] = int(downloaded * 100 / total)
            item["status"] = "done"
            item["pct"] = 100
            print(f"[DL-Q] Done: {item['name']} ({downloaded//(1024*1024)}MB)")
        except Exception as e:
            item["status"] = "error"
            print(f"[DL-Q] Error: {item['name']} - {e}")


def list_downloads():
    files = []
    for f in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, f)
        if os.path.isfile(fpath) and not f.startswith("_"):
            size_mb = os.path.getsize(fpath) // (1024 * 1024)
            resume = get_resume_position(f)
            # Check if HLS is already preprocessed
            base = os.path.splitext(f)[0]
            hls_path = os.path.join(DOWNLOAD_DIR, base + "_hls")
            preprocessed = (os.path.exists(os.path.join(hls_path, "stream.m3u8"))
                           and os.path.exists(os.path.join(hls_path, "audio.mp3")))
            files.append({"name": f, "size_mb": size_mb, "path": fpath,
                          "resume": resume, "ready": preprocessed})
    files.sort(key=lambda x: x["name"])
    return files


preprocess_status = {}  # filename -> "processing" / "done" / "error"

def preprocess_file(filename):
    """Convert a downloaded file to HLS+MP3 in background. Does NOT touch global hls_dir."""
    fpath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(fpath):
        return {"ok": False, "error": "File not found"}
    if preprocess_status.get(filename) == "processing":
        return {"ok": False, "error": "Already processing"}

    def worker():
        preprocess_status[filename] = "processing"
        try:
            base_name = os.path.splitext(filename)[0]
            prep_dir = os.path.join(DOWNLOAD_DIR, base_name + "_hls")
            m3u8 = os.path.join(prep_dir, "stream.m3u8")
            mp3 = os.path.join(prep_dir, "audio.mp3")

            # Skip if already done
            if os.path.exists(m3u8) and os.path.exists(mp3):
                preprocess_status[filename] = "done"
                return

            os.makedirs(prep_dir, exist_ok=True)
            seg_pat = os.path.join(prep_dir, "seg%05d.ts")

            print(f"[PREP] Converting {filename}...")
            t0 = time.time()
            proc = subprocess.run([
                FFMPEG, "-hide_banner", "-y", "-i", fpath,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "32k", "-ac", "1",
                "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                "-hls_segment_filename", seg_pat, m3u8,
                "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "128k", "-ac", "2", mp3,
            ], capture_output=True, text=True)

            if proc.returncode != 0:
                print(f"[PREP] Error: {proc.stderr[:300]}")
                preprocess_status[filename] = "error"
                return

            segs = len([f for f in os.listdir(prep_dir) if f.endswith(".ts")])
            print(f"[PREP] Done: {filename} - {segs} segs, {time.time()-t0:.1f}s")
            preprocess_status[filename] = "done"
        except Exception as e:
            preprocess_status[filename] = "error"
            print(f"[PREP] Error: {filename} - {e}")

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


def delete_download(filename):
    fpath = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(fpath):
        os.remove(fpath)
    # Also delete HLS cache
    hls_path = os.path.join(DOWNLOAD_DIR, os.path.splitext(filename)[0] + "_hls")
    if os.path.exists(hls_path):
        shutil.rmtree(hls_path, ignore_errors=True)
        return True
    return False


# ─── Audio Server ─────────────────────────────────────────
class AudioHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not hls_dir:
            self.send_error(503)
            return
        mp3_path = os.path.join(hls_dir, "audio.mp3")
        if not os.path.exists(mp3_path):
            self.send_error(404)
            return
        # Support Range requests for Sonos seeking
        file_size = os.path.getsize(mp3_path)
        range_hdr = self.headers.get("Range")
        if range_hdr:
            m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else file_size - 1
                with open(mp3_path, "rb") as f:
                    f.seek(start)
                    data = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(data)
                return
        with open(mp3_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ─── Web UI ──────────────────────────────────────────────
WEB_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>IPTV Relay</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,sans-serif;background:#0a0a0a;color:#eee;padding:16px;max-width:600px;margin:0 auto}
  h1{text-align:center;margin-bottom:16px;color:#1DB954}
  .section{background:#1a1a1a;border-radius:8px;padding:16px;margin-bottom:12px}
  .section h2{font-size:14px;color:#888;margin-bottom:8px;text-transform:uppercase}
  select,input[type=text]{width:100%;padding:10px;border:1px solid #333;background:#222;color:#eee;border-radius:4px;margin-bottom:8px;font-size:14px}
  button{padding:10px 16px;border:none;border-radius:6px;font-size:14px;font-weight:bold;cursor:pointer;margin-bottom:4px}
  .btn-green{background:#1DB954;color:#fff} .btn-red{background:#e53935;color:#fff}
  .btn-blue{background:#1976d2;color:#fff} .btn-gray{background:#333;color:#ccc}
  .btn-orange{background:#E65100;color:#fff}
  button:disabled{opacity:0.4}
  .channel-list{max-height:400px;overflow-y:auto}
  .channel{padding:12px;border-bottom:1px solid #222;cursor:pointer;display:flex;align-items:center}
  .channel:hover{background:#252525}
  .channel img{width:80px;height:110px;margin-right:12px;border-radius:6px;object-fit:cover;background:#333}
  .status{text-align:center;padding:8px;color:#888;font-size:13px}
  .tabs{display:flex;gap:4px;margin-bottom:8px}
  .tab{flex:1;padding:8px;text-align:center;background:#222;border-radius:4px;cursor:pointer;font-size:13px}
  .tab.active{background:#1DB954;color:#fff}
  .pagination{display:flex;justify-content:center;align-items:center;gap:8px;margin-top:8px}
  .pagination button{padding:8px 16px;border:1px solid #444;background:#222;color:#eee;border-radius:4px;font-size:14px}
  .bar{background:#333;border-radius:4px;height:8px;overflow:hidden;margin:6px 0;cursor:pointer}
  .bar-fill{height:100%;border-radius:4px;transition:width 1s}
  .ctrl-row{display:flex;gap:6px;margin-bottom:8px;align-items:center}
  .ctrl-row button{flex:1}
  .slider-row{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .slider-row input[type=range]{flex:1}
  .slider-row span{min-width:35px;text-align:center;font-size:13px;color:#aaa}
  .dl-item{display:flex;align-items:center;padding:10px;border-bottom:1px solid #222;gap:8px}
  .dl-item .name{flex:1;font-size:14px}
  .dl-item .size{color:#666;font-size:12px;margin-right:8px}
  .dl-item button{padding:6px 12px;font-size:12px}
  .sync-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;margin-left:8px}
</style>
</head><body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <h1 style="margin:0">IPTV Relay</h1>
  <div id="connBadge" style="padding:4px 10px;border-radius:12px;background:#b71c1c;font-size:11px;align-self:center">Connecting...</div>
  <button class="btn-gray" onclick="toggleSettings()" style="padding:6px 12px;width:auto;font-size:12px">Settings</button>
</div>
<div class="section" id="settingsSection" style="display:none">
  <h2>IPTV Account</h2>
  <input type="text" id="cfgServer" placeholder="Server URL (e.g. http://your-provider.com)">
  <input type="text" id="cfgUser" placeholder="Username">
  <input type="password" id="cfgPass" placeholder="Password">
  <button class="btn-blue" onclick="saveCredentials()" style="margin-bottom:4px">Save & Connect</button>
  <div id="accountInfo" style="color:#888;font-size:13px"></div>
  <h2 style="margin-top:12px">Devices</h2>
  <button class="btn-blue" onclick="discoverDevices()" style="margin-bottom:8px">Discover Devices</button>
  <div id="deviceStatus" style="color:#888;font-size:12px;margin-bottom:6px"></div>
  <div id="sonosDevices"></div>
  <div id="castDevices" style="margin-top:6px"></div>
  <div style="margin-top:8px;font-size:12px;color:#666">
    Selected: <b id="selectedSonos">--</b> | <b id="selectedCast">--</b>
  </div>
  <h2 style="margin-top:12px">Favorite Categories</h2>
  <div id="favList" style="margin-bottom:6px"></div>
  <div style="display:flex;gap:4px">
    <select id="favTypeSelect" style="width:auto;flex:0">
      <option value="live">Live</option>
      <option value="vod">Movies</option>
      <option value="series">Series</option>
    </select>
    <select id="favCatSelect" style="flex:1"><option>Load categories first...</option></select>
    <button class="btn-green" onclick="addFavorite()" style="width:auto;padding:6px 12px;font-size:12px">Pin</button>
  </div>
  <button class="btn-gray" onclick="loadFavCats()" style="margin-top:4px;font-size:11px">Load Categories</button>
  <h2 style="margin-top:12px">Cast Mode</h2>
  <div style="display:flex;gap:6px">
    <button id="modeSplit" class="btn-green" onclick="setCastMode('split')" style="flex:1;font-size:13px">Split (Sonos + Chromecast)</button>
    <button id="modeSingle" class="btn-gray" onclick="setCastMode('single')" style="flex:1;font-size:13px">Single (Chromecast only)</button>
  </div>
  <div style="font-size:11px;color:#666;margin-top:4px">Split = video on TV, audio on Sonos. Single = everything on TV.</div>
</div>

<!-- Player -->
<div class="section" id="playerSection" style="display:none">
  <h2 id="nowPlaying" style="font-size:16px;color:#eee">-</h2>
  <div id="phaseText" style="color:#1DB954;font-size:18px;text-align:center;margin:10px 0">-</div>
  <div class="bar" id="seekBar" onclick="doSeek(event)">
    <div class="bar-fill" id="progBar" style="width:0%;background:#1DB954"></div>
  </div>
  <div style="display:flex;justify-content:space-between;font-size:13px;color:#888;margin-bottom:4px">
    <span id="timeElapsed">00:00</span>
    <span id="timeTotal">--:--</span>
  </div>
  <!-- Live Sync Display -->
  <div id="syncBox" style="background:#111;border-radius:6px;padding:10px;margin-bottom:8px;text-align:center">
    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">
      <span>Video: <b id="ccPos">--</b></span>
      <span id="syncDiff" style="font-size:16px;font-weight:bold">--</span>
      <span>Audio: <b id="sonosPos">--</b></span>
    </div>
    <div style="font-size:11px;color:#888;text-align:center;margin-top:4px">Audio ahead? Freeze video to let audio catch up:</div>
    <div style="display:flex;gap:4px;margin-top:4px">
      <button class="btn-gray" onclick="freezeVideo(200)" style="flex:1;font-size:12px">+200ms</button>
      <button class="btn-gray" onclick="freezeVideo(500)" style="flex:1;font-size:12px">+500ms</button>
      <button class="btn-gray" onclick="freezeVideo(1000)" style="flex:1;font-size:12px">+1s</button>
      <button class="btn-gray" onclick="freezeVideo(2000)" style="flex:1;font-size:12px">+2s</button>
      <button class="btn-gray" onclick="freezeVideo(5000)" style="flex:1;font-size:12px">+5s</button>
    </div>
    <div style="font-size:11px;color:#888;text-align:center;margin-top:6px">Video ahead? Freeze audio to let video catch up:</div>
    <div style="display:flex;gap:4px;margin-top:4px">
      <button class="btn-gray" onclick="freezeAudio(200)" style="flex:1;font-size:12px">+200ms</button>
      <button class="btn-gray" onclick="freezeAudio(500)" style="flex:1;font-size:12px">+500ms</button>
      <button class="btn-gray" onclick="freezeAudio(1000)" style="flex:1;font-size:12px">+1s</button>
      <button class="btn-gray" onclick="freezeAudio(2000)" style="flex:1;font-size:12px">+2s</button>
      <button class="btn-gray" onclick="freezeAudio(5000)" style="flex:1;font-size:12px">+5s</button>
    </div>
  </div>
  <div class="ctrl-row">
    <button class="btn-gray" onclick="doSkip(-30)">-30s</button>
    <button class="btn-gray" onclick="doSkip(-10)">-10s</button>
    <button class="btn-blue" onclick="doPause()" id="pauseBtn">Pause</button>
    <button class="btn-gray" onclick="doSkip(10)">+10s</button>
    <button class="btn-gray" onclick="doSkip(30)">+30s</button>
  </div>
  <div class="ctrl-row">
    <button class="btn-red" onclick="doStop()" style="flex:1">Stop</button>
  </div>
  <div class="slider-row">
    <span>Vol</span>
    <input type="range" id="volSlider" min="0" max="100" value="60" oninput="doVolume(this.value)">
    <span id="volVal">60</span>
  </div>
  <div class="slider-row">
    <span>A/V Sync</span>
    <input type="number" id="syncInput" value="0" step="0.5" style="width:80px;padding:8px;border:1px solid #333;background:#222;color:#eee;border-radius:4px;text-align:center;font-size:16px" onchange="doSyncOffset(this.value)">
    <span style="color:#666;font-size:12px">sec</span>
    <button class="btn-gray" onclick="nudgeSync(-0.5)" style="width:auto;padding:6px 10px">-0.5</button>
    <button class="btn-gray" onclick="nudgeSync(0.5)" style="width:auto;padding:6px 10px">+0.5</button>
  </div>
  <div style="text-align:center;font-size:11px;color:#666">(-) = video behind audio &nbsp; (+) = video ahead of audio</div>
</div>

<!-- Downloaded Movies -->
<div class="section" id="dlSection">
  <h2>Downloaded Movies</h2>
  <div id="dlList"><div class="status">Loading...</div></div>
</div>

<!-- Channel Browser -->
<div class="section" id="browseSection" style="opacity:0.3;pointer-events:none">
  <h2>Browse IPTV</h2>
  <div class="tabs">
    <div id="favButtons"></div>
    <div class="tab" onclick="refreshMalayalam()" style="background:#b71c1c">Refresh List</div>
  </div>
  <div class="tabs" id="typeTabs">
    <div class="tab active" onclick="switchType('live')">Live TV</div>
    <div class="tab" onclick="switchType('vod')">Movies</div>
    <div class="tab" onclick="switchType('series')">Series</div>
  </div>
  <select id="categorySelect" onchange="loadChannels()">
    <option value="">All categories...</option>
  </select>
  <input type="text" id="searchBox" placeholder="Search..." oninput="filterChannels()">
  <div class="channel-list" id="channelList"></div>
  <div class="pagination" id="pagination"></div>
</div>

<script>
let allChannels=[], filteredList=[], currentPage=0, contentType='live';
const PER_PAGE=10;
let statusTimer=null, paused=false, currentDuration=0;

async function api(path, body) {
    try {
        const opts = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {};
        const r = await fetch('/api/' + path, opts);
        return await r.json();
    } catch(e) { return {ok:false, error:e.message}; }
}

// Auto-login + load downloads on page load
(async function(){
    // Load saved config and auto-login
    const cfg = await api('get_config', {});
    if (cfg.ok) {
        document.getElementById('cfgServer').value = cfg.iptv_server || '';
        document.getElementById('cfgUser').value = cfg.iptv_username || '';
        document.getElementById('cfgPass').value = cfg.iptv_password || '';
    }
    const r = await api('auto_login', {});
    if (r.ok && r.info) {
        document.getElementById('accountInfo').textContent = r.info;
        document.getElementById('connBadge').style.background = '#1DB954';
        document.getElementById('connBadge').textContent = 'Connected';
        document.getElementById('browseSection').style.opacity = '1';
        document.getElementById('browseSection').style.pointerEvents = 'auto';
        loadFavorites();
    } else {
        document.getElementById('connBadge').textContent = 'Not Connected';
        document.getElementById('accountInfo').textContent = 'Enter credentials in Settings';
    }
    loadDownloads();
})();

let castMode = 'split'; // 'split' or 'single'

function toggleSettings() {
    const s = document.getElementById('settingsSection');
    s.style.display = s.style.display === 'none' ? '' : 'none';
    if (s.style.display !== 'none') updateSelectedDisplay();
}

async function saveCredentials() {
    const s = document.getElementById('cfgServer').value.trim();
    const u = document.getElementById('cfgUser').value.trim();
    const p = document.getElementById('cfgPass').value.trim();
    if (!s || !u || !p) { alert('Fill all fields'); return; }
    document.getElementById('accountInfo').textContent = 'Connecting...';
    const r = await api('save_config', {iptv_server: s, iptv_username: u, iptv_password: p});
    if (r.ok) {
        document.getElementById('accountInfo').textContent = r.info || 'Connected!';
        document.getElementById('connBadge').style.background = '#1DB954';
        document.getElementById('connBadge').textContent = 'Connected';
        document.getElementById('browseSection').style.opacity = '1';
        document.getElementById('browseSection').style.pointerEvents = 'auto';
    } else {
        document.getElementById('accountInfo').textContent = 'Error: ' + (r.error || 'Unknown');
        document.getElementById('connBadge').textContent = 'Failed';
    }
}

async function loadFavCats() {
    const t = document.getElementById('favTypeSelect').value;
    const r = await api('categories', {type: t});
    const sel = document.getElementById('favCatSelect');
    sel.innerHTML = '';
    (r.categories || []).forEach(c => {
        sel.innerHTML += '<option value="'+c.category_id+'">'+c.category_name+'</option>';
    });
}

async function addFavorite() {
    const t = document.getElementById('favTypeSelect').value;
    const sel = document.getElementById('favCatSelect');
    const catId = sel.value;
    const catName = sel.options[sel.selectedIndex].text;
    if (!catId) return;
    await api('add_favorite', {type: t, category_id: catId, name: catName});
    renderFavSettings();
    loadFavorites();
}

async function removeFavorite(idx) {
    await api('remove_favorite', {index: idx});
    renderFavSettings();
    loadFavorites();
}

async function renderFavSettings() {
    const r = await api('get_config', {});
    const favs = (r.ok && r.favorites) ? r.favorites : [];
    const div = document.getElementById('favList');
    div.innerHTML = '';
    favs.forEach((f, i) => {
        const el = document.createElement('div');
        el.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #333';
        el.innerHTML = '<span style="flex:1;font-size:13px">'+f.name+' <small style="color:#666">('+f.type+')</small></span>';
        const del = document.createElement('button');
        del.className = 'btn-red';
        del.style.cssText = 'padding:2px 8px;font-size:11px;width:auto';
        del.textContent = 'X';
        del.onclick = () => removeFavorite(i);
        el.appendChild(del);
        div.appendChild(el);
    });
    if (!favs.length) div.innerHTML = '<div style="color:#666;font-size:12px">No favorites</div>';
}

function setCastMode(mode) {
    castMode = mode;
    document.getElementById('modeSplit').className = mode === 'split' ? 'btn-green' : 'btn-gray';
    document.getElementById('modeSingle').className = mode === 'single' ? 'btn-green' : 'btn-gray';
    api('set_cast_mode', {mode: mode});
}

async function discoverDevices() {
    document.getElementById('deviceStatus').textContent = 'Scanning... (~10s)';
    const r = await api('discover', {});
    if (!r.ok) { document.getElementById('deviceStatus').textContent = 'Error: ' + r.error; return; }
    // Sonos
    const sd = document.getElementById('sonosDevices');
    sd.innerHTML = '<b style="color:#888;font-size:12px">Sonos Speakers:</b>';
    if (!(r.sonos||[]).length) { sd.innerHTML += '<div class="status">None found</div>'; }
    (r.sonos || []).forEach(s => {
        const el = document.createElement('div');
        el.className = 'dl-item'; el.style.cursor = 'pointer';
        el.innerHTML = '<span class="name">'+s.name+'</span><span class="size">'+s.ip+'</span>';
        el.onclick = () => selectSonos(s.ip, s.name);
        sd.appendChild(el);
    });
    // Chromecast
    const cd = document.getElementById('castDevices');
    cd.innerHTML = '<b style="color:#888;font-size:12px">Chromecast:</b>';
    if (!(r.chromecast||[]).length) { cd.innerHTML += '<div class="status">None found</div>'; }
    (r.chromecast || []).forEach(c => {
        const el = document.createElement('div');
        el.className = 'dl-item'; el.style.cursor = 'pointer';
        el.innerHTML = '<span class="name">'+c.name+'</span><span class="size">'+c.model+'</span>';
        el.onclick = () => selectCast(c.name);
        cd.appendChild(el);
    });
    document.getElementById('deviceStatus').textContent = 'Found ' + (r.sonos||[]).length + ' Sonos, ' + (r.chromecast||[]).length + ' Chromecast';
}

async function selectSonos(ip, name) {
    await api('set_device', {sonos_ip: ip, sonos_name: name});
    updateSelectedDisplay();
}
async function selectCast(name) {
    await api('set_device', {cast_name: name});
    updateSelectedDisplay();
}
async function updateSelectedDisplay() {
    const r = await api('get_devices', {});
    if (r.ok) {
        document.getElementById('selectedSonos').textContent = r.sonos_name + ' (' + r.sonos_ip + ')';
        document.getElementById('selectedCast').textContent = r.cast_name;
    }
}

async function refreshMalayalam() {
    document.getElementById('channelList').innerHTML = '<div class="status">Refreshing from server...</div>';
    await api('categories', {type: 'live', refresh: true});
    await api('categories', {type: 'vod', refresh: true});
    await api('channels', {type: 'live', category_id: '299', refresh: true});
    await api('channels', {type: 'vod', category_id: '575', refresh: true});
    loadFavorites();
    alert('Malayalam list refreshed!');
}

async function loadDownloads() {
    const r = await api('downloads', {});
    const div = document.getElementById('dlList');
    div.innerHTML = '';

    // Show download queue
    if (r.queue && r.queue.length) {
        r.queue.forEach(q => {
            const el = document.createElement('div');
            el.className = 'dl-item';
            el.innerHTML = '<span class="name">' + q.name + '</span>' +
                '<span class="size" style="color:#1976d2">' +
                (q.status === 'downloading' ? q.pct + '%' : q.status) + '</span>';
            div.appendChild(el);
        });
    }

    if (!r.files || !r.files.length) {
        if (!r.queue || !r.queue.length) div.innerHTML = '<div class="status">No downloads yet</div>';
        return;
    }

    r.files.forEach(f => {
        const el = document.createElement('div');
        el.className = 'dl-item';
        el.style.flexWrap = 'wrap';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'name';
        nameSpan.textContent = f.name;

        const sizeSpan = document.createElement('span');
        sizeSpan.className = 'size';
        sizeSpan.textContent = f.size_mb + 'MB';

        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;gap:4px';

        const playBtn = document.createElement('button');
        playBtn.className = 'btn-green';
        playBtn.textContent = f.ready ? 'Play' : 'Play';
        playBtn.style.fontSize = '12px';
        playBtn.onclick = () => playLocal(f.name, 0);

        if (f.resume > 0) {
            const resumeBtn = document.createElement('button');
            resumeBtn.className = 'btn-blue';
            const rm = Math.floor(f.resume / 60), rs = f.resume % 60;
            resumeBtn.textContent = 'Resume ' + rm + ':' + String(rs).padStart(2, '0');
            resumeBtn.style.fontSize = '12px';
            resumeBtn.onclick = () => playLocal(f.name, -1);
            btnRow.appendChild(resumeBtn);
        }

        btnRow.appendChild(playBtn);

        if (!f.ready) {
            const prepBtn = document.createElement('button');
            prepBtn.className = 'btn-gray';
            prepBtn.textContent = 'Prep';
            prepBtn.style.fontSize = '12px';
            prepBtn.onclick = async () => {
                prepBtn.textContent = '...';
                prepBtn.disabled = true;
                await api('preprocess', {filename: f.name});
                // Poll until done
                const poll = setInterval(async () => {
                    const s = await api('preprocess_status', {});
                    const st = s.status && s.status[f.name];
                    if (st === 'done') { clearInterval(poll); loadDownloads(); }
                    else if (st === 'error') { clearInterval(poll); prepBtn.textContent = 'ERR'; }
                    else { prepBtn.textContent = 'Converting...'; }
                }, 3000);
            };
            btnRow.appendChild(prepBtn);
        } else {
            const readyBadge = document.createElement('span');
            readyBadge.style.cssText = 'color:#4CAF50;font-size:11px;align-self:center';
            readyBadge.textContent = 'READY';
            btnRow.appendChild(readyBadge);
            const reprepBtn = document.createElement('button');
            reprepBtn.className = 'btn-gray';
            reprepBtn.textContent = 'Re-prep';
            reprepBtn.style.cssText = 'font-size:10px;padding:4px 6px';
            reprepBtn.onclick = async () => {
                if (!confirm('Delete existing conversion and re-convert?')) return;
                reprepBtn.textContent = '...';
                await api('reprep', {filename: f.name});
                const poll = setInterval(async () => {
                    const s = await api('preprocess_status', {});
                    const st = s.status && s.status[f.name];
                    if (st === 'done') { clearInterval(poll); loadDownloads(); }
                    else if (st === 'error') { clearInterval(poll); reprepBtn.textContent = 'ERR'; }
                    else { reprepBtn.textContent = 'Converting...'; }
                }, 3000);
            };
            btnRow.appendChild(reprepBtn);
        }

        const delBtn = document.createElement('button');
        delBtn.className = 'btn-red';
        delBtn.textContent = 'X';
        delBtn.style.fontSize = '12px';
        delBtn.onclick = () => deleteLocal(f.name);
        btnRow.appendChild(delBtn);

        el.appendChild(nameSpan);
        el.appendChild(sizeSpan);
        el.appendChild(btnRow);
        div.appendChild(el);
    });
}

// Refresh downloads list every 5s if queue is active
setInterval(() => {
    if (download_queue_active) loadDownloads();
}, 5000);
let download_queue_active = false;

async function playLocal(name, resume) {
    document.getElementById('playerSection').style.display = '';
    document.getElementById('nowPlaying').textContent = name;
    paused = false;
    document.getElementById('pauseBtn').textContent = 'Pause';
    const r = await api('play_local', {filename: name, resume: resume || 0});
    if (!r.ok) alert('Error: ' + r.error);
    startStatusPolling();
}

async function deleteLocal(name) {
    if (!confirm('Delete ' + name + '?')) return;
    await api('delete_download', {filename: name});
    loadDownloads();
}

function switchType(t) {
    contentType = t;
    document.querySelectorAll('#typeTabs .tab').forEach(tb => tb.classList.remove('active'));
    event.target.classList.add('active');
    loadCategories();
}

async function loadFavorites() {
    const r = await api('get_config', {});
    const favs = (r.ok && r.favorites) ? r.favorites : [];
    const div = document.getElementById('favButtons');
    div.innerHTML = '';
    if (!favs.length) {
        div.innerHTML = '<div style="color:#666;font-size:12px;text-align:center;padding:4px">No favorites yet - browse categories and pin them from Settings</div>';
        return;
    }
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px';
    favs.forEach(fav => {
        const btn = document.createElement('button');
        btn.className = 'btn-orange';
        btn.style.cssText = 'padding:6px 12px;font-size:12px';
        btn.textContent = fav.name;
        btn.onclick = () => goFavorite(fav);
        row.appendChild(btn);
    });
    div.appendChild(row);
    // Auto-load first favorite
    if (favs.length) goFavorite(favs[0]);
}

async function goFavorite(fav) {
    contentType = fav.type;
    document.querySelectorAll('#typeTabs .tab').forEach(tb => tb.classList.remove('active'));
    document.getElementById('channelList').innerHTML = '<div class="status">Loading...</div>';
    const r = await api('channels', {type: fav.type, category_id: fav.category_id});
    allChannels = r.channels || [];
    renderChannels(allChannels);
}

async function loadCategories(refresh) {
    const r = await api('categories', {type: contentType, refresh: !!refresh});
    const sel = document.getElementById('categorySelect');
    sel.innerHTML = '<option value="">All categories...</option>';
    (r.categories || []).forEach(c => {
        sel.innerHTML += '<option value="'+c.category_id+'">'+c.category_name+'</option>';
    });
    loadChannels(refresh);
}

async function loadChannels(refresh) {
    const catId = document.getElementById('categorySelect').value;
    const r = await api('channels', {type: contentType, category_id: catId, refresh: !!refresh});
    allChannels = r.channels || [];
    renderChannels(allChannels);
}

function filterChannels() {
    const q = document.getElementById('searchBox').value.toLowerCase();
    renderChannels(allChannels.filter(c => c.name.toLowerCase().includes(q)));
}

function renderChannels(list) { filteredList = list; currentPage = 0; showPage(); }

function showPage() {
    const div = document.getElementById('channelList');
    const pgDiv = document.getElementById('pagination');
    div.innerHTML = '';
    if (!filteredList.length) { div.innerHTML='<div class="status">No channels</div>'; pgDiv.innerHTML=''; return; }
    const tp = Math.ceil(filteredList.length / PER_PAGE);
    const start = currentPage * PER_PAGE, end = Math.min(start + PER_PAGE, filteredList.length);
    for (let i = start; i < end; i++) {
        const ch = filteredList[i];
        const el = document.createElement('div');
        el.className = 'channel';
        const nameSpan = document.createElement('span');
        nameSpan.style.flex = '1';
        nameSpan.textContent = ch.name;
        nameSpan.onclick = () => playChannel(ch);
        el.appendChild(nameSpan);
        // Download button (queue download without playing)
        const dlBtn = document.createElement('button');
        dlBtn.className = 'btn-blue';
        dlBtn.textContent = 'DL';
        dlBtn.style.cssText = 'padding:4px 8px;font-size:11px;width:auto;margin:0';
        dlBtn.onclick = (e) => { e.stopPropagation(); queueDL(ch); };
        el.appendChild(dlBtn);
        div.appendChild(el);
        if (ch.stream_icon) {
            const img = new Image(); img.loading='lazy';
            img.style.cssText='width:80px;height:110px;margin-right:12px;border-radius:6px;object-fit:cover;background:#333';
            img.onerror=()=>img.remove(); img.src=ch.stream_icon; el.prepend(img);
        }
    }
    pgDiv.innerHTML='<button onclick="prevPage()" '+(currentPage===0?'disabled':'')+'>Prev</button>'+
        '<span>'+(currentPage+1)+'/'+tp+' ('+filteredList.length+')</span>'+
        '<button onclick="nextPage()" '+(currentPage>=tp-1?'disabled':'')+'>Next</button>';
}
function prevPage(){if(currentPage>0){currentPage--;showPage()}}
function nextPage(){if(currentPage<Math.ceil(filteredList.length/PER_PAGE)-1){currentPage++;showPage()}}

async function playChannel(ch) {
    document.getElementById('playerSection').style.display = '';
    document.getElementById('nowPlaying').textContent = ch.name;
    paused = false;
    document.getElementById('pauseBtn').textContent = 'Pause';
    const r = await api('play', {stream_id:ch.stream_id, name:ch.name, type:contentType, ext:ch.container_extension||'ts'});
    if (!r.ok) alert('Error: ' + r.error);
    startStatusPolling();
}

function startStatusPolling() {
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(async () => {
        const r = await api('status', {});
        if (!r.ok) return;
        const p = r.phase;
        document.getElementById('phaseText').textContent = r.phase_text || p;
        const e = r.elapsed || 0; currentDuration = r.duration || 0;
        const fmt = s => {const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=Math.floor(s%60); return h>0?h+':'+String(m).padStart(2,'0')+':'+String(ss).padStart(2,'0'):String(m).padStart(2,'0')+':'+String(ss).padStart(2,'0');}
        document.getElementById('timeElapsed').textContent = fmt(e);
        document.getElementById('timeTotal').textContent = currentDuration ? fmt(currentDuration) : '--:--';
        const pct = p==='downloading' ? r.download_pct : (currentDuration>0 ? Math.min(100,e/currentDuration*100) : 0);
        document.getElementById('progBar').style.width = pct+'%';
        document.getElementById('progBar').style.background = p==='downloading'?'#1976d2':'#1DB954';
        // Live sync display with ms precision
        if (p==='playing'||p==='paused') {
            const cc=r.cc_time||0, sn=r.sonos_time||0, diff=r.sync_diff||0;
            const fmtMs = s => fmt(Math.floor(s)) + '.' + String(Math.round((s%1)*1000)).padStart(3,'0');
            document.getElementById('ccPos').textContent = fmtMs(cc);
            document.getElementById('sonosPos').textContent = fmtMs(sn);
            const absDiff = Math.abs(diff);
            const color = absDiff<2?'#4CAF50':absDiff<5?'#FF9800':'#f44336';
            const label = diff>0 ? 'video ahead' : 'audio ahead';
            document.getElementById('syncDiff').innerHTML = '<span style="color:'+color+'">'+diff.toFixed(3)+'s<br><small>'+label+'</small></span>';
            document.getElementById('syncBox').style.display='';
        } else {
            document.getElementById('syncBox').style.display='none';
        }
        if (p==='idle') {
            document.getElementById('playerSection').style.display='none';
            clearInterval(statusTimer); loadDownloads();
        }
    }, 1500);
}

async function doPause() {
    paused=!paused;
    await api('pause',{paused});
    document.getElementById('pauseBtn').textContent=paused?'Resume':'Pause';
}
async function doStop() {
    await api('stop',{});
    document.getElementById('playerSection').style.display='none';
    if(statusTimer)clearInterval(statusTimer);
    loadDownloads();
}
async function doVolume(v) {
    document.getElementById('volVal').textContent=v;
    await api('volume',{volume:parseInt(v)});
}
async function doSkip(sec) {
    await api('skip',{seconds:sec});
}
function doSeek(evt) {
    if (!currentDuration) return;
    const bar = document.getElementById('seekBar');
    const pct = (evt.clientX - bar.getBoundingClientRect().left) / bar.offsetWidth;
    const sec = Math.floor(pct * currentDuration);
    api('seek',{seconds:sec});
}
async function doSyncOffset(val) {
    document.getElementById('syncInput').value = val;
    await api('sync_offset', {offset: parseFloat(val)});
}
function nudgeSync(delta) {
    const inp = document.getElementById('syncInput');
    const newVal = Math.round((parseFloat(inp.value) + delta) * 10) / 10;
    inp.value = newVal;
    doSyncOffset(newVal);
}
async function doSyncNow() {
    const r = await api('sync_now', {});
}
async function queueDL(ch) {
    const r = await api('queue_download', {
        stream_id: ch.stream_id, name: ch.name, type: contentType,
        ext: ch.container_extension || 'ts'
    });
    if (r.ok) { download_queue_active = true; loadDownloads(); }
    else alert(r.error || 'Download failed');
}

async function freezeVideo(ms) {
    // Pause video for N ms, audio keeps playing -> audio catches up
    await api('freeze_video', {ms: ms});
}
async function freezeAudio(ms) {
    // Pause audio for N ms, video keeps playing -> video catches up
    await api('freeze_audio', {ms: ms});
}
</script>
</body></html>"""


# ─── Web Handler ─────────────────────────────────────────
class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(WEB_HTML.encode("utf-8"))
        elif path == "/stream.m3u8" or path.endswith(".ts"):
            self._serve_hls(path)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        path = self.path.replace("/api/", "")
        result = {"ok": False}
        try:
            if path == "login":
                result = handle_login(body)
            elif path == "auto_login":
                # Login using saved config
                if state["iptv_server"] and state["iptv_user"] and state["iptv_pass"]:
                    result = handle_login({"server": state["iptv_server"],
                                           "username": state["iptv_user"],
                                           "password": state["iptv_pass"]})
                else:
                    result = {"ok": False, "error": "No credentials configured"}
            elif path == "get_config":
                result = {"ok": True,
                          "iptv_server": config.get("iptv_server", ""),
                          "iptv_username": config.get("iptv_username", ""),
                          "iptv_password": config.get("iptv_password", ""),
                          "cast_mode": config.get("cast_mode", "split"),
                          "favorites": config.get("favorites", [])}
            elif path == "save_config":
                config["iptv_server"] = body.get("iptv_server", "")
                config["iptv_username"] = body.get("iptv_username", "")
                config["iptv_password"] = body.get("iptv_password", "")
                save_config(config)
                state["iptv_server"] = config["iptv_server"]
                state["iptv_user"] = config["iptv_username"]
                state["iptv_pass"] = config["iptv_password"]
                # Try to login with new credentials
                result = handle_login({"server": config["iptv_server"],
                                       "username": config["iptv_username"],
                                       "password": config["iptv_password"]})
            elif path == "categories":
                result = handle_categories(body)
            elif path == "channels":
                result = handle_channels(body)
            elif path == "play":
                result = handle_play(body)
            elif path == "play_local":
                result = handle_play_local(body)
            elif path == "stop":
                stop_playback(); result = {"ok": True}
            elif path == "pause":
                pause_playback(body.get("paused", False)); result = {"ok": True}
            elif path == "volume":
                set_volume(body.get("volume", 60)); result = {"ok": True}
            elif path == "seek":
                seek_to(body.get("seconds", 0)); result = {"ok": True}
            elif path == "skip":
                _, sonos = get_positions()
                seek_to(max(0, sonos + body.get("seconds", 0))); result = {"ok": True}
            elif path == "sync_offset":
                offset = body.get("offset", 0)
                state["sync_offset"] = offset
                # Re-generate MP3 with offset baked in
                result = apply_audio_offset(offset)
            elif path == "sync_now":
                result = {"ok": True}
            elif path == "freeze_video":
                # Pause Chromecast for N ms, audio keeps playing
                ms = body.get("ms", 500)
                def do_freeze_video():
                    if cast_obj:
                        try:
                            mc = cast_obj.media_controller
                            mc.pause()
                            time.sleep(ms / 1000.0)
                            mc.play()
                            print(f"[SYNC] Froze video {ms}ms")
                        except Exception as e:
                            print(f"[SYNC] Freeze video error: {e}")
                threading.Thread(target=do_freeze_video, daemon=True).start()
                result = {"ok": True}
            elif path == "freeze_audio":
                # Pause Sonos for N ms, video keeps playing
                ms = body.get("ms", 500)
                def do_freeze_audio():
                    try:
                        import soco
                        sp = soco.SoCo(get_sonos_ip())
                        sp.pause()
                        time.sleep(ms / 1000.0)
                        sp.play()
                        print(f"[SYNC] Froze audio {ms}ms")
                    except Exception as e:
                        print(f"[SYNC] Freeze audio error: {e}")
                threading.Thread(target=do_freeze_audio, daemon=True).start()
                result = {"ok": True}
            elif path == "status":
                result = handle_status()
            elif path == "downloads":
                result = {"ok": True, "files": list_downloads(),
                          "queue": [{"name":q["name"],"status":q["status"],"pct":q["pct"]} for q in download_queue]}
            elif path == "delete_download":
                delete_download(body.get("filename", "")); result = {"ok": True}
            elif path == "queue_download":
                sid = body["stream_id"]
                stype = body.get("type", "vod")
                ext = body.get("ext", "mkv")
                url = stream_url(sid, ext, stype)
                result = queue_download(body["name"], url, ext)
            elif path == "preprocess":
                result = preprocess_file(body.get("filename", ""))
            elif path == "preprocess_status":
                result = {"ok": True, "status": preprocess_status}
            elif path == "reprep":
                fn = body.get("filename", "")
                base_name = os.path.splitext(fn)[0]
                hls_path = os.path.join(DOWNLOAD_DIR, base_name + "_hls")
                if os.path.exists(hls_path):
                    shutil.rmtree(hls_path, ignore_errors=True)
                    print(f"[REPREP] Deleted {hls_path}")
                result = preprocess_file(fn)
            elif path == "discover":
                result = handle_discover()
            elif path == "set_device":
                if "sonos_ip" in body:
                    state["sonos_ip"] = body["sonos_ip"]
                    state["sonos_name"] = body.get("sonos_name", "Sonos")
                    config["sonos_ip"] = body["sonos_ip"]
                    config["sonos_name"] = body.get("sonos_name", "Sonos")
                if "cast_name" in body:
                    state["cast_name"] = body["cast_name"]
                    config["chromecast_name"] = body["cast_name"]
                save_config(config)
                result = {"ok": True}
            elif path == "add_favorite":
                favs = config.get("favorites", [])
                favs.append({"type": body["type"], "category_id": body["category_id"], "name": body["name"]})
                config["favorites"] = favs
                save_config(config)
                result = {"ok": True}
            elif path == "remove_favorite":
                favs = config.get("favorites", [])
                idx = body.get("index", -1)
                if 0 <= idx < len(favs):
                    favs.pop(idx)
                config["favorites"] = favs
                save_config(config)
                result = {"ok": True}
            elif path == "set_cast_mode":
                state["cast_mode"] = body.get("mode", "split")
                config["cast_mode"] = state["cast_mode"]
                save_config(config)
                print(f"[MODE] Cast mode: {state['cast_mode']}")
                result = {"ok": True}
            elif path == "get_devices":
                result = {"ok": True, "sonos_ip": state.get("sonos_ip", config.get("sonos_ip", "")),
                          "sonos_name": state.get("sonos_name", config.get("sonos_name", "")),
                          "cast_name": state.get("cast_name", config.get("chromecast_name", ""))}
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except Exception:
            pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _serve_hls(self, path):
        if not hls_dir:
            self.send_error(503); return
        fpath = os.path.join(hls_dir, "stream.m3u8") if path == "/stream.m3u8" else os.path.join(hls_dir, path.lstrip("/"))
        if not os.path.exists(fpath):
            self.send_error(404); return
        for _ in range(5):
            with open(fpath, "rb") as f:
                data = f.read()
            if data:
                break
            time.sleep(0.2)
        if not data:
            self.send_error(503); return
        ct = "application/vnd.apple.mpegurl" if fpath.endswith(".m3u8") else "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ─── API Handlers ────────────────────────────────────────
def handle_login(body):
    state["iptv_server"] = body["server"].rstrip("/")
    state["iptv_user"] = body["username"]
    state["iptv_pass"] = body["password"]
    try:
        info = iptv_api()
        ui = info.get("user_info", {})
        exp = ui.get("exp_date", "")
        if exp and exp.isdigit():
            from datetime import datetime
            exp = datetime.fromtimestamp(int(exp)).strftime("%Y-%m-%d")
        return {"ok": True, "info": f"Status: {ui.get('status','?')}, Expires: {exp}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def handle_categories(body):
    t = body.get("type", "live")
    action_map = {"live": "get_live_categories", "vod": "get_vod_categories", "series": "get_series_categories"}
    cats = iptv_api_cached(action_map.get(t, "get_live_categories"), refresh=body.get("refresh", False))
    return {"ok": True, "categories": cats}

def handle_channels(body):
    t = body.get("type", "live")
    cat_id = body.get("category_id", "")
    action_map = {"live": "get_live_streams", "vod": "get_vod_streams", "series": "get_series"}
    params = {"category_id": cat_id} if cat_id else {}
    channels = iptv_api_cached(action_map.get(t, "get_live_streams"), refresh=body.get("refresh", False), **params)
    return {"ok": True, "channels": channels}

def handle_play(body):
    stop_playback()
    sid = body["stream_id"]
    name = body["name"]
    stype = body.get("type", "live")
    ext = body.get("ext", "ts")
    state["channel_name"] = name
    url = stream_url(sid, ext, stype)
    safe_name = re.sub(r'[^\w\-\s]', '', name)[:50].strip()
    dest = os.path.join(DOWNLOAD_DIR, f"{safe_name}.{ext}")

    def worker():
        try:
            if not os.path.exists(dest):
                download_stream(url, dest)
            else:
                print(f"[PLAY] Already downloaded: {dest}")
                state["download_pct"] = 100
            if not convert_to_hls_and_mp3(dest):
                state["phase"] = "error"; return
            start_playback()
            monitor_loop()
        except Exception as e:
            print(f"[PLAY] Error: {e}")
            state["phase"] = "error"
    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": f"Starting {name}..."}

def handle_play_local(body):
    stop_playback()
    filename = body["filename"]
    fpath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(fpath):
        return {"ok": False, "error": "File not found"}
    state["channel_name"] = filename
    state["download_pct"] = 100
    # Resume from saved position or start from 0
    resume = body.get("resume", 0)
    if resume == -1:  # -1 means auto-resume
        resume = get_resume_position(filename)
    state["resume_from"] = resume

    def worker():
        try:
            if not convert_to_hls_and_mp3(fpath):
                state["phase"] = "error"; return
            start_playback()
            monitor_loop()
        except Exception as e:
            print(f"[PLAY] Error: {e}")
            state["phase"] = "error"
    threading.Thread(target=worker, daemon=True).start()
    msg = f"Starting {filename}..."
    if resume > 0:
        msg += f" (resuming from {resume//60}:{resume%60:02d})"
    return {"ok": True, "message": msg}

def monitor_loop():
    while state["playing"]:
        time.sleep(5)
        if cast_obj:
            try:
                mc = cast_obj.media_controller
                mc.update_status()
                if mc.status and mc.status.player_state == "IDLE" and state.get("elapsed", 0) > 30:
                    print("[PLAY] Movie ended")
                    stop_playback()
                    break
            except Exception:
                pass

def handle_status():
    phase = state["phase"]
    texts = {
        "idle": "Ready", "downloading": f"Downloading... {state.get('download_pct',0)}%",
        "converting": "Converting...", "casting": "Casting to projector...",
        "playing": "Playing", "paused": "Paused", "error": "Error",
    }
    cc_time = 0
    sonos_time = 0
    if phase == "playing":
        try:
            cc_time, sonos_time = get_positions()
            state["last_cc"] = cc_time
            state["last_sonos"] = sonos_time
        except Exception:
            pass
    elif phase == "paused":
        cc_time = state.get("last_cc", 0)
        sonos_time = state.get("last_sonos", 0)
    return {
        "ok": True, "phase": phase, "phase_text": texts.get(phase, phase),
        "download_pct": state.get("download_pct", 0),
        "elapsed": int(cc_time) if cc_time > 0 else state.get("elapsed", 0),
        "duration": state.get("duration", 0),
        "cc_time": round(cc_time, 3),
        "sonos_time": sonos_time,
        "sync_diff": round(cc_time - sonos_time, 3) if cc_time > 0 else 0,
    }


# ─── Logging ─────────────────────────────────────────────
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "iptv_relay.log")

logger = logging.getLogger("iptv_relay")
logger.setLevel(logging.INFO)
_rfh = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
_rfh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_rfh)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_sh)

import builtins
_orig_print = builtins.print
def _log_print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    try:
        logger.info(msg)
    except Exception:
        pass
builtins.print = _log_print


# ─── Main ────────────────────────────────────────────────
def main():
    global pc_ip
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write("=== IPTV Relay v2 ===\n")
    pc_ip = get_ip()
    print(f"IPTV Relay v2")
    print(f"  Web:   http://{pc_ip}:{WEB_PORT}/")
    print(f"  Audio: http://{pc_ip}:{AUDIO_PORT}/")

    audio_srv = ThreadedHTTPServer(("0.0.0.0", AUDIO_PORT), AudioHandler)
    threading.Thread(target=audio_srv.serve_forever, daemon=True).start()

    # Start background download worker
    threading.Thread(target=download_worker, daemon=True).start()
    print("[DL] Download worker started")

    web_srv = ThreadedHTTPServer(("0.0.0.0", WEB_PORT), WebHandler)
    try:
        web_srv.serve_forever()
    except KeyboardInterrupt:
        stop_playback()
        print("Stopped.")

if __name__ == "__main__":
    main()
