# CastBridge

**Turn any laptop into a powerful IPTV media server. Browse, download, and cast movies to your TV — with Sonos surround sound.**

CastBridge solves real problems that millions of IPTV users face every day: slow buffering, unsupported devices, terrible audio, and zero control over playback. It works by downloading content first, then casting it — giving you buffer-free playback, full seeking, and audiophile-quality sound through your Sonos speakers.

---

## Why This Exists

I built this because I wanted to watch movies on my projector with great sound, and nothing on the market could do it properly.

**The setup**: An XGIMI MoGo 2 projector (with Chromecast built-in) and two Sonos One speakers. That's it. No soundbar, no AV receiver, no HDMI-ARC, no complicated wiring. Just WiFi.

**The problem**: IPTV apps on Android TV are unreliable. They buffer constantly, the UI is terrible on a projector remote, and there's no way to route audio to Sonos. Chromecast doesn't run IPTV apps natively. And Sonos? Sonos only plays what Sonos wants to play.

**The solution**: CastBridge runs on your laptop. You browse and control everything from your phone. It downloads movies over your local network, splits the video and audio, sends video to your Chromecast and audio to your Sonos. You get buffer-free 1080p video on your big screen with rich, room-filling sound from your Sonos speakers.

One evening of building. A lifetime of movie nights.

---

## What It Can Do

### Split Cast Mode (Video + Audio separated)
Your TV gets the picture. Your Sonos gets the sound. Both perfectly synced.

- Video streams to any Chromecast device (TV, projector, dongle)
- Audio streams to any Sonos speaker on your network
- Chromecast volume is muted — all sound comes from Sonos
- Live sync adjustment from your phone — nudge video forward or back in 200ms increments
- Works with any IPTV service that supports Xtream Codes API

### Single Cast Mode (Everything to Chromecast)
Don't have Sonos? No problem. Send video and audio together to your Chromecast. Classic casting, but with the download-first advantage — no buffering, full seeking, pause/resume.

### Sonos Audio Mirror (Android App)
A standalone Android app that captures any audio playing on your phone and streams it to your Sonos speakers. Playing Spotify but want it on Sonos without Spotify Connect? Playing a voice note? A YouTube video? This app mirrors everything.

- Works with any app on your phone
- Discovers Sonos speakers automatically
- One-tap streaming
- No root required (uses Android's AudioPlaybackCapture API, Android 10+)

---

## Who This Is For

**You have a Chromecast but your TV can't run IPTV apps.** Many smart TVs have Chromecast built in but lack the storage, RAM, or app compatibility to run IPTV players properly. CastBridge turns your laptop into the brain and your TV into just a screen.

**You have Sonos speakers but can't use them for movies.** Sonos doesn't play nicely with most video sources. CastBridge splits the audio out and sends it directly to your Sonos — any model, any generation. Those old Sonos Ones that sound incredible? Now they're your movie speakers.

**Your internet is slow or unreliable.** IPTV streaming requires consistent bandwidth. If you're on satellite internet (Starlink in rural areas), mobile hotspots, or shared connections — buffering ruins the experience. CastBridge downloads first, then plays locally. Zero buffering. Ever.

**You want to watch IPTV on your phone and hear it on Sonos.** The Android app captures your phone's audio output and streams it to any Sonos speaker. Simple as that.

---

## How It Works

```
                        Your Laptop (CastBridge)
                              |
            +-----------------+-----------------+
            |                                   |
     IPTV Server                          Your Phone
     (downloads movie)                    (web browser = remote control)
            |
            v
     [Downloaded Movie File]
            |
      ffmpeg converts
            |
     +------+------+
     |             |
   HLS Video    MP3 Audio
     |             |
     v             v
  Chromecast     Sonos
  (your TV)    (your speakers)
```

**The key insight**: Streaming IPTV directly to Chromecast doesn't work well — connections drop, formats are incompatible, and there's no way to split audio. By downloading first and converting locally, everything becomes reliable. The conversion is cached, so replaying a movie starts instantly.

---

## Quick Start

### Option 1: Download the EXE (Windows)
1. Download `CastBridge.exe` from [Releases](https://github.com/vysakhrnambiar/castbridge/releases)
2. Double-click to run — a system tray icon appears
3. Open `http://localhost:8080` in your browser (or from your phone on the same WiFi)
4. Go to Settings, enter your IPTV credentials
5. Browse movies, download one, hit Play

### Option 2: Run from source
```bash
# Clone the repo
git clone https://github.com/user/castbridge.git
cd castbridge

# Install dependencies
pip install -r requirements.txt

# Install ffmpeg (required)
# Windows: Download from https://www.gyan.dev/ffmpeg/builds/
# Linux: sudo apt install ffmpeg
# macOS: brew install ffmpeg

# Run
python src/relay_server.py
```

Open `http://localhost:8080` in your browser. That's it.

### Option 3: Android App (Sonos Audio Mirror)
1. Download `SonosAudioMirror.apk` from [Releases](https://github.com/vysakhrnambiar/castbridge/releases)
2. Install on your Android phone (Android 10+)
3. Open the app, it discovers your Sonos speakers
4. Select a speaker, tap "Stream"
5. Any audio on your phone now plays through Sonos

---

## Features

### Browsing & Downloads
- Browse IPTV channels by category (Live TV, Movies, Series)
- Quick access buttons for your favorite categories
- Search across all channels
- Paginated browsing with movie posters
- Background download queue — download movies while watching another
- Channel list cached locally for instant loading

### Playback Controls
- **Play / Pause / Stop** — controls both video and audio devices together
- **Seek bar** — click anywhere to jump (seeks both devices)
- **Skip buttons** — -30s, -10s, +10s, +30s
- **Volume slider** — controls Sonos volume
- **Resume** — remembers where you stopped, offers "Resume from X:XX"
- **Preprocess** — convert movies in advance for instant playback

### Audio-Video Sync
- Live sync display showing video and audio positions
- **Freeze buttons** — pause video or audio briefly to adjust lip sync
  - Audio ahead? Freeze video by 200ms/500ms/1s/2s/5s
  - Video ahead? Freeze audio by the same amounts
- Sync persists once set — no drift during playback

### Device Management
- Auto-discovers Sonos speakers and Chromecast devices on your network
- Select which speaker and which screen from Settings
- Remembers your selection

### Phone as Remote
- Full web UI optimized for mobile
- Access from any device on the same WiFi: `http://<your-laptop-ip>:8080`
- Browse, download, play, pause, seek, adjust volume — all from your phone

---

## Technical Details

### Requirements
- **Python 3.10+**
- **ffmpeg** — for video/audio conversion
- **pychromecast** — Chromecast discovery and control
- **soco** — Sonos discovery and control
- A Chromecast-enabled device on your network
- (Optional) Sonos speakers on your network
- An IPTV subscription with Xtream Codes API support

### How the Split Works
1. **Download**: Movie file downloaded from IPTV server to local disk
2. **Convert**: ffmpeg splits into HLS video segments (.ts files + .m3u8 playlist) and a separate MP3 audio file
3. **Serve**: Local HTTP server serves both HLS and MP3
4. **Cast**: pychromecast tells your Chromecast to play the HLS URL. soco tells your Sonos to play the MP3 URL
5. **Sync**: Both devices start together. Fine-tune with freeze buttons if needed

### Why Download First?
We tried live streaming — it doesn't work reliably:
- IPTV connections drop and reconnect, causing stutters on Chromecast
- ffmpeg can't reliably split a live stream into two outputs without blocking
- Chromecast needs complete HLS segments, not a growing stream
- Sonos has a ~10 second internal buffer that makes live sync impossible

Downloading first solves all of these. The conversion is cached next to the video file, so you only convert once. Replay is instant.

### Xtream Codes API
CastBridge works with any IPTV provider that uses the Xtream Codes panel (most do). You need:
- Server URL (e.g., `http://yourprovider.com`)
- Username
- Password

These are stored locally on your machine and never transmitted anywhere except to your IPTV provider.

---

## Configuration

On first run, go to **Settings** in the web UI to configure:
- IPTV server URL, username, password
- Default Sonos speaker
- Default Chromecast device

Settings are stored in `config.json` in the application directory.

---

## Future Plans

### Live Audio Translation
We're planning an extension that can translate movie audio on the fly:

1. Download movie and extract audio
2. Diarize speakers (identify who's talking when)
3. Clone each speaker's voice characteristics
4. Translate speech to target language using voice-preserving translation
5. Separate background sounds (music, effects) from dialogue
6. Combine translated dialogue with original background audio
7. Play the translated track through Sonos while video plays on Chromecast

This means: a movie in English, heard in Hindi — with the original actors' voices preserved. The technology exists today (speaker diarization, voice cloning, neural machine translation, source separation). We just need to connect the pieces.

### Live Channel Support
Currently optimized for VOD (movies/series). Live channel streaming is on the roadmap — it requires a different approach since you can't download-first.

### Multi-Room Audio
Sonos supports grouping speakers. CastBridge could send audio to a group — surround sound in every room.

---

## Project Structure

```
castbridge/
├── README.md               # This file
├── LICENSE                  # MIT License
├── requirements.txt         # Python dependencies
├── config.example.json      # Template configuration (no credentials)
├── .gitignore              # Excludes credentials, downloads, cache
├── src/
│   ├── relay_server.py     # Main server — web UI + API + HLS serving
│   ├── iptv_client.py      # Xtream Codes API wrapper + local cache
│   ├── chromecast.py       # Chromecast discovery + casting
│   ├── sonos.py            # Sonos discovery + control
│   ├── converter.py        # ffmpeg HLS + MP3 conversion
│   └── web_ui.py           # Embedded HTML/JS for the web interface
├── tray/
│   └── tray_app.py         # Windows system tray application
├── android/                # Sonos Audio Mirror app source
│   ├── app/
│   └── build.gradle.kts
├── releases/               # Pre-built EXE and APK
├── screenshots/            # UI screenshots for documentation
└── docs/
    └── ARCHITECTURE.md     # Detailed technical documentation
```

---

## Disclaimer

CastBridge is a media relay tool. It does not provide, host, or distribute any media content. You must have your own IPTV subscription. Please respect the terms of service of your IPTV provider and the copyright laws of your jurisdiction.

---

## Contributing

This is an open-source project. If you find it useful, you can:
- Report bugs and suggest features via GitHub Issues
- Submit pull requests
- Share it with others who might benefit
- Star the repo if you like it

### Ideas for Contributors
- Linux/macOS support for the tray app
- Subtitle support (SRT/SSA extraction and display)
- EPG (Electronic Program Guide) integration
- Multiple audio track selection
- Chromecast groups support
- Web-based subtitle editor
- Live audio translation (see Future Plans)

---

## License

MIT License. Use it, modify it, share it. See [LICENSE](LICENSE) for details.

---

Built because movie nights should be simple. Your laptop does the hard work. Your Chromecast shows the picture. Your Sonos fills the room with sound. Your phone is the remote. That's it.
