# RetroTV

A Python desktop application that simulates the experience of watching a 1990s CRT television. RetroTV plays live internet streams and local video playlists through a nostalgic amber-glow interface complete with scanlines, static noise, and commercial breaks.

**Current version: 2.2.1**

**Website: [https://retrotv.n5net.com](https://retrotv.n5net.com)**

---

## Documentation & Downloads

- Website & installer download: **[https://retrotv.n5net.com](https://retrotv.n5net.com)**
- Full documentation: **[https://retrotv.n5net.com/download.html](https://retrotv.n5net.com/download.html)**
- GitHub releases (Windows installer): **[https://github.com/q3shafe/RetroTV/releases](https://github.com/q3shafe/RetroTV/releases)**
- Release history: [`changelog.txt`](changelog.txt)

---

## Windows Installer

If you don't want to deal with Python, a self-contained Windows installer is available at:

**[https://retrotv.n5net.com](https://retrotv.n5net.com)**

The installer (`retrotv_setup.exe`) includes everything you need — no Python or separate ffmpeg download required:

- `RetroTV.exe` — the standalone application (no Python needed)
- `ffmpeg.exe`, `ffplay.exe`, `ffprobe.exe` — bundled for DVR recording support
- Optional: a pack of 100+ classic commercials (~2 GB) that can be downloaded during setup

The installer targets 64-bit Windows and installs to `C:\RetroTV` by default. A Start Menu shortcut is created automatically, with an optional desktop icon.

---

## Features

- **CRT Interface** — Realistic phosphor amber palette, animated scanlines, static noise, and a "No Signal" screen.
- **Live Streaming** — Tune into internet TV channels (HLS, M3U8, RTMP, and more). A loading status (TUNING / CONNECTING / BUFFERING / RECONNECTING) is shown while a stream starts.
- **Local Playlists** — Play your own video libraries via `.xspf`, `.m3u`, or `.m3u8` playlists. Supports shuffle mode and per-channel resume (remembers where you left off).
- **Local Channel Editor** — Edit a local channel's tracks in-app ("Edit Tracks" or double-click the channel): drag to reorder, sort A–Z, insert, remove, and flag missing files.
- **Commercial Breaks** — Automatically plays 2–4 random commercials between playlist tracks. Drop video files into a `commercials/` folder or point to a `commercials.xspf` playlist.
- **Channel Guide (CH 00)** — Built-in scrolling cable-style guide showing all channels.
- **EPG (Electronic Program Guide)** — Live program schedule data loaded from XMLTV sources. Press `G` on any live channel to see an overlay with what's on now and what's coming up. Right-click a channel → **Preview Guide** to see its schedule without changing what you're watching.
- **Phone Remote** — Control RetroTV from any phone browser on your local network, no app needed. See [Phone Remote](#phone-remote).
- **DVR Recording** — Record any live channel on demand. Press `R` to start/stop. A blinking red dot appears while recording. Recordings are saved as `.ts` files in the `recordings/` folder.
- **DVR Scheduler** — Schedule recordings by time using the built-in schedule manager (`DVR SCHED` button). Run `dvr_scheduler.py` in the background to capture scheduled shows even when the main app is closed. Supports simultaneous recording of multiple channels.
- **Network Watermarks** — Per-channel transparent PNG watermarks (network bug) displayed in the bottom-right corner of the video area.
- **Favorites** — Star any channel for quick filtering.
- **Channel Search** — Filter the channel list by name in real time.
- **Channel Editor** — Add, edit, and remove channels directly inside the app without editing config files manually.
- **Windows Media Keys & Media Overlay** — Play/Pause, Stop, Next, Previous, Volume Up/Down, and Mute media keys work out of the box, and the Windows media overlay shows what's playing.
- **Multi-Monitor** — Proper fullscreen handling on multi-monitor setups.
- **Pause & Seek** — Pause button and playhead scrubber for local file channels.
- **Offline Detection** — Shows an on-screen notice when a live channel is unreachable.
- **Auto Update Check** — Notifies you when a new version is available.

---

## Requirements

- **Python 3.10+**
- **VLC Media Player** installed on your system — [Download VLC](https://www.videolan.org/vlc/)
  - The Python architecture (32-bit or 64-bit) must match your VLC installation.
- **ffmpeg** — required only for DVR recording.
  - Place `ffmpeg.exe` in the same folder as `tv.py`, or install it so it is on your system `PATH`.

---

## Installation

1. Install dependencies:
   ```
   pip install python-vlc Pillow qrcode
   ```
   - `python-vlc` — VLC media playback bindings (required)
   - `Pillow` — proper RGBA transparency for channel watermarks (recommended)
   - `qrcode` — QR code in the phone-remote pairing dialog (optional)
   - `winsdk` — Windows media overlay integration (optional, Windows only)

2. Run the application:
   ```
   python tv.py
   ```

### Building the Windows executable

```
pyinstaller retrotv.spec
```

This produces `dist/RetroTV.exe`. The installer script `retrotv.iss` (Inno Setup) expects the exe plus `ffmpeg.exe`, `ffplay.exe` and `ffprobe.exe` in `dist/`, and uses absolute paths — edit them to match your checkout before compiling.

---

## Configuration

### `retrotv_settings.json` — App Settings

Created automatically next to the app. Notable options:

| Setting | Default | Description |
|---|---|---|
| `commercials_min` / `commercials_max` | `2` / `4` | Number of commercials per break |
| `epg_refresh_hours` | `6` | How often EPG data is refreshed |
| `clock_24h` | `false` | 24-hour clock |
| `hw_accel` | `true` | Hardware-accelerated decoding |
| `buffer_ms` | `1500` | Network buffer size |
| `start_last_channel` | `true` | Resume the last channel on launch |
| `start_fullscreen` | `false` | Launch in fullscreen |
| `always_on_top` | `false` | Keep the window on top |
| `suppress_screensaver` | `true` | Prevent the screensaver while playing |
| `remote_enabled` | `true` | Enable the phone remote |
| `remote_port` | `8765` | Phone remote port |
| `remote_token` | *(generated)* | Pairing code for the phone remote |

### `tv_config.json` — Default Channel Lineup

The built-in channel list (not stored in this repository). The app fetches the current lineup from the RetroTV server on startup. You generally do not need to edit this file directly — use `user_config.json` for your own channels.

### `user_config.json` — Custom Channels

Create this file next to `tv.py` to add your own channels. The app merges these with the default lineup. You can also manage channels from inside the app with the channel editor.

```json
{
  "channels": [
    {
      "name": "My Live Channel",
      "url": "https://example.com/stream.m3u8",
      "description": "News",
      "watermark": "logos/my_logo.png",
      "channel-id": "my.xmltv.id"
    },
    {
      "name": "80s Movies",
      "playlist": "playlists/80s.xspf",
      "random": "1",
      "description": "Movies"
    }
  ],
  "epg_urls": ["https://example.com/guide.xml"]
}
```

**Channel fields:**

| Field | Description |
|---|---|
| `name` | Display name shown in the channel list |
| `url` | URL to a live stream (HLS, RTMP, etc.) |
| `playlist` | Path to a local `.xspf`, `.m3u`, or `.m3u8` file |
| `random` | `"1"` to shuffle the playlist |
| `description` | Category label shown in the guide |
| `watermark` | Path to a transparent PNG to use as a network bug |
| `channel-id` | XMLTV channel ID for EPG data matching |

### EPG Data

Program guide data is loaded automatically from XMLTV sources on startup and refreshed periodically (see `epg_refresh_hours`). Only programme data for channels in your lineup is kept, which keeps memory use low. To add EPG data for your custom channels, give each channel a `channel-id` that matches the channel ID in your XMLTV source, then add the source in either of these ways:

- Use the **📡 EPG** button in the app, **or**
- Add an `epg_urls` list to `user_config.json` (URLs or local file paths), **or**
- Create an `epg.txt` next to `tv.py` with one EPG URL per line (loaded alongside the built-in guide).

### Commercials

To enable automatic commercial breaks between playlist tracks:

1. Create a `commercials/` folder next to `tv.py` and drop video files inside, **or**
2. Create a `commercials.xspf` or `commercials.m3u` file listing your ad videos.

---

## Phone Remote

RetroTV runs a small web server on your local network so you can control it from any phone browser — no app to install.

1. In RetroTV, click the **📱** button in the control bar (or press `Ctrl+M`).
2. Scan the QR code with your phone camera (or open the shown address and enter the 8-character pairing code). The phone remembers the pairing.
3. The first run may trigger a Windows Firewall prompt — allow it on **Private** networks. The phone and PC must be on the same network.

The remote offers channel up/down, play/pause, volume and mute, fullscreen, favorite, info, and record. It has a searchable channel list (All / Favorites), a "now playing" card, a scrolling TV **Guide** tab (next 4 hours), and track prev/next plus a seek slider on local-file channels.

To disable it, set `"remote_enabled": false` in `retrotv_settings.json`. If the port is already in use (for example by a second RetroTV instance), the app logs it and runs without the remote.

---

## Controls

| Key / Action | Function |
|---|---|
| `Left` / `Right` | Previous / Next channel |
| `Up` / `Down` | Volume up / down |
| `Space` | Play / Pause (local file channels) |
| `F` | Toggle favorite |
| `G` | Toggle EPG program guide overlay |
| `R` | Toggle DVR recording on/off |
| `Ctrl+M` | Phone remote pairing dialog |
| `F11` | Toggle fullscreen |
| `Esc` | Exit fullscreen |
| `Ctrl+L` | Open log file |
| Mouse wheel (over knob) | Change channels |
| Double-click | Toggle fullscreen |
| Right-click a channel | Preview Guide / channel options |
| Media keys | Play/Pause, Stop, Next, Previous, Volume, Mute |

---

## DVR

### On-demand recording

Press `R` while watching any live or playlist channel to start recording. A blinking red dot appears in the top-left of the video area while active. Press `R` again to stop. Recordings are saved to the `recordings/` folder as `.ts` files named `ChannelName_YYYYMMDD_HHMMSS.ts`.

ffmpeg must be available. If it is not found, the app shows an instruction dialog.

### Scheduled recording

Click **DVR SCHED** to open the schedule manager. Set a channel, start time, and stop time. Schedules are stored in `dvr_schedule.json`. The scheduler polls it every 30 seconds and launches a separate ffmpeg process for each due recording. Multiple channels can record simultaneously.

To run the scheduler as a standalone background process (so recordings fire even when the main app is closed):

```
python dvr_scheduler.py
```

On Windows, you can register this as a Task Scheduler task to start automatically at login:
- **Program:** `pythonw.exe`
- **Arguments:** `C:\path\to\dvr_scheduler.py`
- **Trigger:** At log on

> **Note:** Do not run `dvr_scheduler.py` and `tv.py` at the same time — they both manage `dvr_schedule.json` and may conflict.

---

## Android App (experimental)

The `android/` folder contains an early (v0.1.0) Android client written in Kotlin with Jetpack Compose (min SDK 26). Open the `android/` folder in Android Studio to build it. It bundles a copy of the channel lineup in `app/src/main/assets/tv_config.json`.

---

## Repository Layout

```
tv.py                  — Main application
remote_control.py      — Phone remote web server
retrotv_epg.py         — EPG / XMLTV parsing module
dvr_scheduler.py       — Standalone background DVR scheduler
version.json           — Version info used by the update check
retrotv.spec           — PyInstaller build spec
retrotv.iss            — Inno Setup installer script
fonts/                 — Press Start 2P font (OFL licensed)
android/               — Android client (Kotlin / Compose)
website/               — Project website (retrotv.n5net.com)
changelog.txt          — Release history
```

Files created at runtime next to the app:

```
user_config.json       — Your custom channels (create this yourself)
retrotv_settings.json  — App settings
retrotv_resume.json    — Per-channel resume positions
dvr_schedule.json      — DVR schedule
recordings/            — DVR output folder
playlists/             — Playlists created by the channel editor
commercials/           — Drop commercial video files here
commercials.xspf       — Or use a playlist for commercials
retrotv.log            — Application log (rotating, 5 x 1 MB)
```

---

## Built With

- [Python](https://www.python.org/) 3.10+
- [Tkinter](https://docs.python.org/3/library/tkinter.html) — GUI
- [python-vlc](https://wiki.videolan.org/Python_Bindings/) — Media playback
- [ffmpeg](https://ffmpeg.org/) — DVR recording
- [Pillow](https://python-pillow.org/) — Watermark rendering
- [qrcode](https://pypi.org/project/qrcode/) — Phone remote pairing QR code
- [PyInstaller](https://pyinstaller.org/) / [Inno Setup](https://jrsoftware.org/isinfo.php) — Windows packaging
