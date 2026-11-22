#!/usr/bin/env python3
"""
RetroTV/IPTV Player - A 90s Television Experience
Requires: python-vlc, tkinter (standard library)
Install: pip install python-vlc
Also requires VLC media player installed on the system.
First Run / Setup Wizard:
  On the first launch (when no config files are present), a setup wizard appears
  offering two options:
    - Use the curated channel list (recommended) — channels are fetched from
      https://cdn.wedovids.com/tv/tv_config.json automatically.
    - Set up your own channels — opens the Channel Editor to add your own
      streams, playlists, or M3U sources.
Commercial Breaks:
  Place a file named  commercials.xspf  (or commercials.m3u / commercials.m3u8)
  in the same directory as this script (or the compiled .exe).
  RetroTV will automatically play a random number of commercials between tracks
  (default 2-4; configurable in Settings → Commercial Break settings).
  You can also place video files directly in a  commercials/  subdirectory and
  they will be scanned and added to the commercials playlist automatically.
Playlists:
  Both XSPF and M3U/M3U8 formats are supported for channel playlists and
  commercials.  Reference them via the  playlist  key in user_config.json,
  or use the built-in Channel Editor (✎ EDIT button).
Watermark / Network Bug:
  Each channel in user_config.json can optionally include a  "watermark"  key
  pointing to a transparent PNG file (absolute path, or relative to the script).
  The image will be scaled to ~8% of the screen width and displayed in the
  bottom-right corner of the video area, just like a real TV network bug.
  Example:
      { "name": "My Channel", "url": "...", "watermark": "logo.png" }
  Pillow (pip install Pillow) is recommended for proper RGBA transparency;
  plain tkinter PhotoImage works too but shows a black background behind
  semi-transparent pixels.
EPG / Program Guide:
  Program schedule data is loaded on startup from:
    https://cdn.wedovids.com/tv/guide.xml   (XMLTV format)
    epg.txt  (one EPG URL per line, loaded alongside guide.xml)
  Additional XMLTV sources (URLs or local file paths) can be added via the
  📡 EPG button, or by adding an  "epg_urls"  list to user_config.json.
  Each channel in user_config.json or tv_config.json can include a
  "channel-id" field matching the channel id in the EPG XML data.
  Example:
      { "name": "USA NETWORK", "url": "...", "channel-id": "USA Network - Pacific (125).us" }
  If no EPG data is available for a channel, the guide shows "No schedule available".
Keyboard shortcuts:  press  ?  at any time to see all bindings.
"""
import tkinter as tk
from tkinter import ttk, font, messagebox as tk_messagebox
import json
import os
import sys
import random
import re
import ctypes
import threading
import urllib.request
import webbrowser
import math
import queue
import xml.etree.ElementTree as ET
import datetime
import time as _time
import hashlib
import base64
import subprocess
import shutil
import uuid as _uuid_mod
try:
    import vlc
    VLC_AVAILABLE = True
except ImportError:
    VLC_AVAILABLE = False
    print("WARNING: python-vlc not installed. Run: pip install python-vlc")

# ─────────────────────────────────────────────
#  Windows System Media Transport Controls (SMTC)
#  Registers the app with the Windows media overlay so it shows the title,
#  play/pause/next/prev buttons AND a timeline (playhead) that can be
#  scrubbed. Requires `winsdk` (or `winrt`). Fails soft if unavailable.
#  WinRT events fire on a thread-pool thread, so callbacks only push into
#  a queue; the Tk main loop drains it.
# ─────────────────────────────────────────────
class _SMTCBridge:
    def __init__(self, event_queue):
        self._q = event_queue
        self.ok = False
        self._mp = None
        self._smtc = None
        self._last_title = None
        self._last_status = None
        self._timeline_on = False
        self._log = logging.getLogger("retrotv.smtc")
        if sys.platform != 'win32':
            return
        for prefix in ("winsdk", "winrt"):
            try:
                if prefix == "winsdk":
                    from winsdk.windows.media.playback import MediaPlayer
                    from winsdk.windows.media import (
                        MediaPlaybackStatus, MediaPlaybackType,
                        SystemMediaTransportControlsButton as Btn,
                        SystemMediaTransportControlsTimelineProperties as Timeline)
                else:
                    from winrt.windows.media.playback import MediaPlayer
                    from winrt.windows.media import (
                        MediaPlaybackStatus, MediaPlaybackType,
                        SystemMediaTransportControlsButton as Btn,
                        SystemMediaTransportControlsTimelineProperties as Timeline)
                self._Status, self._Type, self._Timeline = MediaPlaybackStatus, MediaPlaybackType, Timeline
                # A dummy MediaPlayer is the simplest way for a desktop app to
                # obtain an SMTC session; we drive it manually.
                self._mp = MediaPlayer()
                self._mp.command_manager.is_enabled = False
                s = self._mp.system_media_transport_controls
                s.is_enabled = True
                s.is_play_enabled = s.is_pause_enabled = True
                s.is_next_enabled = s.is_previous_enabled = True
                s.is_stop_enabled = True
                btn_map = {Btn.PLAY: 'play_pause', Btn.PAUSE: 'play_pause',
                           Btn.NEXT: 'next', Btn.PREVIOUS: 'prev', Btn.STOP: 'stop'}
                def _on_button(sender, args):
                    name = btn_map.get(args.button)
                    if name: self._q.put_nowait(('smtc', name))
                def _on_seek(sender, args):
                    pos = args.requested_playback_position
                    ms = int(pos.total_seconds() * 1000) if hasattr(pos, 'total_seconds') else int(pos) // 10_000
                    self._q.put_nowait(('smtc_seek', ms))
                s.add_button_pressed(_on_button)
                s.add_playback_position_change_requested(_on_seek)
                s.display_updater.type = MediaPlaybackType.VIDEO
                s.display_updater.update()
                self._smtc = s
                self.ok = True
                self._log.info("SMTC registered via %s", prefix)
                return
            except Exception as e:
                self._log.debug("SMTC via %s unavailable: %s", prefix, e)
        self._log.info("SMTC unavailable (pip install winsdk to enable)")

    def set_metadata(self, title, subtitle=""):
        if not self.ok or (title, subtitle) == self._last_title: return
        self._last_title = (title, subtitle)
        try:
            u = self._smtc.display_updater
            u.type = self._Type.VIDEO
            u.video_properties.title = title or ""
            u.video_properties.subtitle = subtitle or ""
            u.update()
        except Exception as e:
            self._log.debug("set_metadata failed: %s", e)

    def set_status(self, playing, paused=False):
        if not self.ok: return
        st = self._Status.PLAYING if playing else (self._Status.PAUSED if paused else self._Status.STOPPED)
        if st == self._last_status: return
        self._last_status = st
        try: self._smtc.playback_status = st
        except Exception as e: self._log.debug("set_status failed: %s", e)

    def set_timeline(self, pos_ms, length_ms, seekable=True):
        """Push the playhead. length_ms <= 0 clears the timeline (live TV)."""
        if not self.ok: return
        if length_ms <= 0 and not self._timeline_on: return
        try:
            td = datetime.timedelta
            tl = self._Timeline()
            if length_ms > 0:
                pos_ms = max(0, min(pos_ms, length_ms))
                tl.start_time = td(0)
                tl.end_time = td(milliseconds=length_ms)
                tl.min_seek_time = td(0)
                tl.max_seek_time = td(milliseconds=length_ms if seekable else pos_ms)
                tl.position = td(milliseconds=pos_ms)
                self._timeline_on = True
            else:
                for attr in ('start_time', 'end_time', 'min_seek_time', 'max_seek_time', 'position'):
                    setattr(tl, attr, td(0))
                self._timeline_on = False
            self._smtc.update_timeline_properties(tl)
        except Exception as e:
            self._log.debug("set_timeline failed: %s", e)

# ─────────────────────────────────────────────
#  PyInstaller-aware runtime directory
# ─────────────────────────────────────────────
def _exe_dir() -> str:
    if getattr(sys, 'frozen', False):
        if sys.platform.startswith('linux'):
            # Installed under /opt (read-only for normal users) — keep
            # config, settings and logs in the per-user data dir instead.
            base = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
            d = os.path.join(base, 'retrotv')
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                pass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────
#  Logging setup
#  - Rotating file log at  <exe_dir>/retrotv.log  (5 × 1 MB files)
#  - Mirrors to console
#  - Replaces the scattered print() calls; old prefixes like "[vlc]"
#    become logger names, so existing filtering habits still work.
# ─────────────────────────────────────────────
import logging
import logging.handlers

LOG_PATH = os.path.join(_exe_dir(), "retrotv.log")

def _setup_logging():
    root = logging.getLogger("retrotv")
    if root.handlers:  # idempotent — safe if called twice
        return root
    root.setLevel(logging.DEBUG)
    root.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s [%(name)s] %(threadName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_048_576, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as exc:  # disk full, permissions, etc.
        print(f"[log] could not open {LOG_PATH}: {exc}")
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # Route any stray warnings (e.g. DeprecationWarning from libs) through logging
    logging.captureWarnings(True)
    return root

log = _setup_logging()

def _log_uncaught(exctype, value, tb):
    log.critical("Uncaught exception", exc_info=(exctype, value, tb))
    # Still call the default hook so the interpreter behaves normally
    sys.__excepthook__(exctype, value, tb)
sys.excepthook = _log_uncaught

def _log_thread_exception(args):
    log.critical("Uncaught exception in thread %s",
                 getattr(args.thread, "name", "?"),
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
try:
    threading.excepthook = _log_thread_exception  # py3.8+
except Exception:
    pass

# Per-module child loggers. Using logger names that match the old bracket
# prefixes keeps log output readable and greppable.
_log_vlc    = log.getChild("vlc")
_log_live   = log.getChild("live")
_log_epg    = log.getChild("epg")
_log_xspf   = log.getChild("xspf")
_log_m3u    = log.getChild("m3u")
_log_libvlc = log.getChild("libvlc")

log.info("=" * 60)
log.info("RetroTV starting  (pid=%d  platform=%s  frozen=%s)",
         os.getpid(), sys.platform, bool(getattr(sys, "frozen", False)))
log.info("log file: %s", LOG_PATH)

# ─────────────────────────────────────────────
#  Windows screensaver / display sleep suppression
# ─────────────────────────────────────────────
_ES_CONTINUOUS       = 0x80000000
_ES_DISPLAY_REQUIRED = 0x00000002
def _suppress_screensaver(enable: bool):
    if sys.platform != 'win32':
        return
    try:
        flags = (_ES_CONTINUOUS | _ES_DISPLAY_REQUIRED) if enable else _ES_CONTINUOUS
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass

# ─────────────────────────────────────────────
#  XSPF Playlist parser
# ─────────────────────────────────────────────
def _xspf_find(element, tag, ns):
    result = element.find(f'xspf:{tag}', ns)
    if result is None:
        result = element.find(tag)
    return result
def _xspf_findall(element, tag, ns):
    result = element.findall(f'xspf:{tag}', ns)
    if not result:
        result = element.findall(tag)
    return result
def parse_xspf(path):
    import urllib.parse
    tracks = []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        ns   = {'xspf': 'http://xspf.org/ns/0/'}
        playlist_dir = os.path.dirname(os.path.abspath(path))
        tracklist = _xspf_find(root, 'trackList', ns)
        if tracklist is None:
            _log_xspf.warning("no <trackList> found in %s", path)
            return tracks
        for track in _xspf_findall(tracklist, 'track', ns):
            loc_el = _xspf_find(track, 'location', ns)
            if loc_el is None or not loc_el.text:
                continue
            location = loc_el.text.strip()
            location = urllib.parse.unquote(location)
            if location.startswith('file:///'):
                location = location[8:] if sys.platform.startswith('win') else location[7:]
                location = location.replace('/', os.sep)
            elif location.startswith('file://'):
                location = location[7:].replace('/', os.sep)
            if not os.path.isabs(location) and '://' not in location:
                location = os.path.join(playlist_dir, location)
            location = os.path.normpath(location)
            tracks.append(location)
    except ET.ParseError as e:
        _log_xspf.error("XML parse error in %s: %s", path, e)
    except Exception as e:
        _log_xspf.exception("unexpected error for %s: %s", path, e)
    _log_xspf.info("loaded %d track(s) from %s", len(tracks), os.path.basename(path))
    return tracks

# ─────────────────────────────────────────────
#  M3U Playlist parser
# ─────────────────────────────────────────────
def parse_m3u(path):
    """Parse an M3U or M3U8 file and return a list of local file paths / URLs."""
    import urllib.parse
    tracks = []
    playlist_dir = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('file:///'):
                line = urllib.parse.unquote(line)
                line = line[8:] if sys.platform.startswith('win') else line[7:]
                line = line.replace('/', os.sep)
            elif line.startswith('file://'):
                line = urllib.parse.unquote(line)
                line = line[7:].replace('/', os.sep)
            if not os.path.isabs(line) and '://' not in line:
                line = os.path.join(playlist_dir, line)
            if '://' not in line:
                line = os.path.normpath(line)
            tracks.append(line)
    except Exception as e:
        _log_m3u.exception("error parsing %s: %s", path, e)
    _log_m3u.info("loaded %d track(s) from %s", len(tracks), os.path.basename(path))
    return tracks

def parse_extm3u_channels(source):
    """
    Parse an Extended M3U playlist (EXTM3U format) and return a list of channel dicts.
    Each dict contains: name, url, and optionally channel-id (from tvg-id).
    Accepts a str, bytes, or a local file path / http(s) URL.
    """
    channels = []
    if isinstance(source, bytes):
        text = source.decode('utf-8', errors='replace')
    elif isinstance(source, str) and '\n' not in source and (
            source.startswith(('http://', 'https://')) or os.path.isfile(source)):
        # Treat as a path/URL — load it
        try:
            if source.startswith(('http://', 'https://')):
                req = urllib.request.Request(source, headers={"User-Agent": f"RetroTV/{APP_VERSION}"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    text = resp.read().decode('utf-8', errors='replace')
            else:
                with open(source, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
        except Exception as exc:
            _log_m3u.error("parse_extm3u_channels fetch error: %s", exc)
            return channels
    else:
        text = source
    current_extinf = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            current_extinf = line
        elif line and not line.startswith('#') and current_extinf is not None:
            meta = current_extinf
            m = re.search(r'tvg-id="([^"]*)"', meta)
            tvg_id = m.group(1).strip() if m else ''
            m = re.search(r'tvg-name="([^"]*)"', meta)
            tvg_name = m.group(1).strip() if m else ''
            display_name = ''
            if ',' in meta:
                display_name = meta.rsplit(',', 1)[1].strip()
            if not display_name:
                display_name = tvg_name or tvg_id or f"Channel {len(channels)+1}"
            entry = {'name': display_name, 'url': line}
            if tvg_id:
                entry['channel-id'] = tvg_id
            channels.append(entry)
            current_extinf = None
    _log_m3u.info("parse_extm3u_channels: found %d channel(s)", len(channels))
    return channels

def parse_playlist(path):
    """Dispatch to the correct parser based on file extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.m3u', '.m3u8'):
        return parse_m3u(path)
    return parse_xspf(path)

# ─────────────────────────────────────────────
#  EPG / XMLTV Parser
# ─────────────────────────────────────────────
EPG_URL  = "https://cdn.wedovids.com/tv/guide.xml"
EPG_TXT  = "epg.txt"
def _parse_xmltv_datetime(s):
    """
    Parse XMLTV datetime string like '20240328060000 +0000' or '20240328060000 +0500'
    Also handles the less-common ±HH:MM colon form (e.g. '+05:30').
    Returns a UTC-normalised naive datetime.
    """
    if not s:
        return None
    s = s.strip()
    parts = s.split()
    ts = parts[0]
    tz_offset_str = parts[1] if len(parts) > 1 else "+0000"
    try:
        dt = datetime.datetime(
            int(ts[0:4]), int(ts[4:6]), int(ts[6:8]),
            int(ts[8:10]), int(ts[10:12]), int(ts[12:14]) if len(ts) >= 14 else 0
        )
    except Exception:
        return None
    # Parse tz offset — accept both ±HHMM and ±HH:MM
    try:
        sign = 1 if tz_offset_str[0] != '-' else -1
        digits = tz_offset_str[1:].replace(':', '')   # strip optional colon
        tz_h = int(digits[0:2])
        tz_m = int(digits[2:4])
        offset_minutes = sign * (tz_h * 60 + tz_m)
        dt_utc = dt - datetime.timedelta(minutes=offset_minutes)
    except Exception:
        dt_utc = dt
    return dt_utc

def _maybe_gunzip(raw_bytes):
    """Decompress raw_bytes if it starts with the gzip magic bytes; else return as-is."""
    if raw_bytes[:2] == b'\x1f\x8b':
        import gzip
        return gzip.decompress(raw_bytes)
    return raw_bytes

def parse_epg_xml(xml_bytes, wanted=None):
    """
    Stream-parse XMLTV XML bytes.
    wanted: optional set of channel ids to keep; everything else is skipped and
            freed as it streams (None or empty = keep everything).
    Returns (epg_dict, icons_dict) where:
      epg_dict   = { channel_id: [ {start, stop, title, subtitle, desc, category}, ... ] }
      icons_dict = { channel_id: icon_url_str }
    Programmes are sorted by start time.
    """
    import io
    epg   = {}
    icons = {}
    try:
        for _ev, el in ET.iterparse(io.BytesIO(xml_bytes), events=('end',)):
            tag = el.tag
            if tag == 'channel':
                ch_id = el.get('id', '').strip()
                if ch_id and (not wanted or ch_id in wanted):
                    icon_el = el.find('icon')
                    if icon_el is not None:
                        src = (icon_el.get('src') or '').strip()
                        if src:
                            icons[ch_id] = src
                el.clear()
            elif tag == 'programme':
                ch_id = el.get('channel', '')
                if ch_id and (not wanted or ch_id in wanted):
                    start_dt = _parse_xmltv_datetime(el.get('start', ''))
                    if start_dt is not None:
                        stop_dt     = _parse_xmltv_datetime(el.get('stop', ''))
                        title_el    = el.find('title')
                        subtitle_el = el.find('sub-title')
                        desc_el     = el.find('desc')
                        cat_el      = el.find('category')
                        epg.setdefault(ch_id, []).append({
                            'start':    start_dt,
                            'stop':     stop_dt,
                            'title':    (title_el.text or '').strip()    if title_el    is not None else '',
                            'subtitle': (subtitle_el.text or '').strip() if subtitle_el is not None else '',
                            'desc':     (desc_el.text or '').strip()     if desc_el     is not None else '',
                            'category': (cat_el.text or '').strip()      if cat_el      is not None else '',
                        })
                el.clear()
    except ET.ParseError as e:
        print(f"[EPG] XML parse error: {e}")
        return epg, icons
    # Sort each channel's programmes by start time
    for ch_id in epg:
        epg[ch_id].sort(key=lambda p: p['start'])
    total_programmes = sum(len(v) for v in epg.values())
    print(f"[EPG] Parsed {total_programmes} programmes across {len(epg)} channels, "
          f"{len(icons)} channel icons")
    return epg, icons

def _get_epg_now_next(epg_data, channel_id):
    """
    Given epg_data dict and a channel_id, return (current_prog, next_prog) or (None, None).
    'current_prog' is the programme airing right now (UTC).
    """
    if not channel_id or channel_id not in epg_data:
        return None, None
    progs = epg_data[channel_id]
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    current = None
    next_prog = None
    for i, p in enumerate(progs):
        start = p['start']
        stop  = p.get('stop')
        if start <= now_utc:
            if stop is None or stop > now_utc:
                current = p
                if i + 1 < len(progs):
                    next_prog = progs[i + 1]
                break
        elif current is None:
            # Programme hasn't started yet — it's "next" if we have nothing current
            next_prog = p
            break
    return current, next_prog

def _format_prog_title(prog):
    """Return display title, using subtitle when title is generic like 'MOVIE'."""
    if prog is None:
        return ''
    title = prog.get('title', '')
    subtitle = prog.get('subtitle', '')
    if title.upper() in ('MOVIE', 'FILM', 'FEATURE', '') and subtitle:
        return subtitle
    if subtitle:
        return f"{title}: {subtitle}"
    return title

def _format_time_range(prog):
    """Return HH:MM - HH:MM string in local time."""
    if prog is None:
        return ''
    # Convert UTC to local
    def utc_to_local(dt_utc):
        try:
            import time as t
            ts = dt_utc.replace(tzinfo=datetime.timezone.utc).timestamp()
            local_ts = _time.localtime(ts)
            return datetime.datetime(*local_ts[:6])
        except Exception:
            return dt_utc
    start = prog.get('start')
    stop  = prog.get('stop')
    if start is None:
        return ''
    s = utc_to_local(start).strftime('%I:%M %p').lstrip('0')
    if stop:
        e = utc_to_local(stop).strftime('%I:%M %p').lstrip('0')
        return f"{s} – {e}"
    return s

def _get_progress_fraction(prog):
    """Return 0.0–1.0 progress through current programme, or None."""
    if prog is None:
        return None
    start = prog.get('start')
    stop  = prog.get('stop')
    if start is None or stop is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    total = (stop - start).total_seconds()
    if total <= 0:
        return None
    elapsed = (now - start).total_seconds()
    return max(0.0, min(1.0, elapsed / total))

# ─────────────────────────────────────────────
#  Channel model
# ─────────────────────────────────────────────
class Channel:
    def __init__(self, number, name, url=None, playlist=None, randomize=False,
                 description="", watermark=None, channel_id=None, commercials=True, color=None):
        self.number      = number
        self.name        = name
        self.url         = url
        self.playlist    = playlist
        self.randomize   = randomize
        self.description = description
        self.watermark   = watermark   # path to a transparent PNG for the network bug
        self.channel_id  = channel_id  # EPG channel id for matching guide.xml data
        self.commercials = commercials  # whether to play commercial breaks on this channel
        self.color       = color        # optional hex accent color e.g. "#ff6600"
        self.is_live     = (url is not None and playlist is None)
    @property
    def is_local(self):
        return self.playlist is not None

# ─────────────────────────────────────────────
#  Resume state helpers
# ─────────────────────────────────────────────
def _resume_file_path():
    return os.path.join(_exe_dir(), "retrotv_resume.json")
def _load_resume_state():
    path = _resume_file_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}
def _save_resume_state(state: dict):
    try:
        with open(_resume_file_path(), 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[resume] save error: {e}")

# ─────────────────────────────────────────────
#  News state helpers
# ─────────────────────────────────────────────
def _load_user_config_dict(path):
    """Load user_config.json as a dict, upgrading a bare list to {channels: [...]}."""
    if not os.path.exists(path):
        return {"channels": [], "epg_urls": []}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
        raw = re.sub(r',\s*(\]|\})', r'\1', raw)
        data = json.loads(raw)
    except Exception:
        return {"channels": [], "epg_urls": []}
    if isinstance(data, list):
        return {"channels": data, "epg_urls": []}
    if not isinstance(data, dict):
        return {"channels": [], "epg_urls": []}
    data.setdefault("channels", [])
    data.setdefault("epg_urls", [])
    return data

# ─────────────────────────────────────────────
#  App Settings
# ─────────────────────────────────────────────
SETTINGS_FILE = "retrotv_settings.json"
_DEFAULT_SETTINGS = {
    "commercials_min":       2,
    "commercials_max":       4,
    "epg_refresh_hours":     6,
    "clock_24h":             False,
    "hw_accel":              True,
    "buffer_ms":             1500,
    "start_last_channel":    True,
    "start_fullscreen":      False,
    "always_on_top":         False,
    "suppress_screensaver":  True,
    "use_curated_channels":  False,
    "remote_enabled":        True,    # LAN phone remote (see remote_control.py)
    "remote_port":           8765,
    "remote_token":          "",      # generated on first run
}

def _load_settings_file() -> dict:
    path = os.path.join(_exe_dir(), SETTINGS_FILE)
    out  = dict(_DEFAULT_SETTINGS)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k in _DEFAULT_SETTINGS:
                if k in data:
                    out[k] = data[k]
        except Exception:
            pass
    return out

def _save_settings_file(settings: dict):
    path = os.path.join(_exe_dir(), SETTINGS_FILE)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        log.warning("[settings] save error: %s", e)

def _news_state_file_path():
    return os.path.join(_exe_dir(), "retrotv_news.json")
def _load_news_state() -> dict:
    """Load the persisted news state (last seen hash + timestamp)."""
    path = _news_state_file_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}
def _save_news_state(state: dict):
    try:
        with open(_news_state_file_path(), 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[news] save error: {e}")

# ─────────────────────────────────────────────
#  Main Application
# ─────────────────────────────────────────────
APP_VERSION = "2.2.2"
class RetroTV(tk.Tk):
    def report_callback_exception(self, exc, val, tb):
        """Route Tk callback exceptions into retrotv.log.

        Tk callbacks — key bindings, button commands, after() timers — do NOT go
        through sys.excepthook; Tk prints them to stderr instead.  The frozen
        build is built with console=False, so stderr is discarded and a failure
        part-way through e.g. _exit_fullscreen would vanish without a trace,
        leaving the window in a half-restored state with nothing to debug from.
        """
        log.critical("Uncaught exception in Tk callback", exc_info=(exc, val, tb))

    # Colors — phosphor amber CRT palette
    BG        = "#0a0a0a"
    BEIGE     = "#c8b99a"
    AMBER     = "#ffb000"
    AMBER_DIM = "#7a5500"
    RED_GLOW  = "#cc2200"
    DARK_GRAY = "#1a1a1a"
    MID_GRAY  = "#2d2d2d"
    TEXT_CLR  = "#e8d5b0"
    # Guide specific colors
    GUIDE_BG        = "#000080"
    GUIDE_HEADER_BG = "#0000c0"
    GUIDE_ROW_A     = "#000070"
    GUIDE_ROW_B     = "#00004a"
    GUIDE_ACCENT    = "#00ffff"
    GUIDE_YELLOW    = "#ffff00"
    GUIDE_WHITE     = "#ffffff"
    GUIDE_GREEN     = "#00ff00"
    GUIDE_RED       = "#ff4444"
    GUIDE_GRAY      = "#aaaaaa"
    # Guide Livestream
    GUIDE_LIVESTREAM_URL = "https://amg00056-vevotv-vevo80saunz-samsungau-rp5e3.amagi.tv/playlist/amg00056-vevotv-vevo80saunz-samsungau/playlist.m3u8"
    DONATE_URL = (
        "https://www.paypal.com/cgi-bin/webscr?cmd=_donations"
        "&business=tnlshafe@gmail.com&lc=US"
        "&item_name=RTV-Donation+to+N5+Networks"
        "&no_note=0&currency_code=USD"
        "&bn=PP-DonationsBF:btn_donateCC_LG.gif:NonHostedGuest"
    )
    # News feed URL
    NEWS_URL = "https://cdn.wedovids.com/tv/news.txt"
    def __init__(self, config_path="tv_config.json"):
        super().__init__()
        self.config_path = config_path
        self.base_dir    = _exe_dir()
        self.channels    = []
        self.current_channel_idx = 0
        self.is_fullscreen = False
        self._last_fs_toggle = 0.0
        self._channel_display_after = None
        self._sidebar_collapsed = False
        self._sidebar_width = 155   # current expanded width, updated by drag
        self._last_tune_time = 0.0
        self._tuning = False
        self._advancing = False
        self._tuning_clear_after = None
        # FIX 1: timestamp debounce + stamp for stale-callback detection
        self._last_tune_start    = 0.0
        self._current_tune_stamp = None
        # ── Local playlist state ──────────────────────────────────────────
        self._local_track_idx        = 0
        self._local_track_order      = []
        self._channel_track_orders   = {}  # persists shuffled order per channel across re-tunes
        self._last_local_channel_idx = None
        self._playhead_dragging      = False
        self._playhead_seek_frac     = None
        self._resume_state = _load_resume_state()
        # ── Favorites ─────────────────────────────────────────────────────
        raw_favs = self._resume_state.get('__favorites__', [])
        self._favorites = set(raw_favs) if isinstance(raw_favs, list) else set()
        self._legacy_fav_numbers = set()
        if self._favorites and all(isinstance(f, int) for f in self._favorites):
            self._legacy_fav_numbers = self._favorites
            self._favorites = set()
        # ── Recently watched ──────────────────────────────────────────────
        raw_recent = self._resume_state.get('__recently_watched__', [])
        self._recently_watched     = raw_recent[:10] if isinstance(raw_recent, list) else []
        self._recently_watched_max = 10
        # ── EPG data ─────────────────────────────────────────────────────
        self._user_epg_sources = []    # extra XMLTV URLs/paths from user_config.json
        self._epg_data        = {}     # { channel_id: [ programme_dict, ... ] }
        self._epg_icons       = {}     # { channel_id: url_str }  — from <channel><icon src=...>
        self._epg_img_cache   = {}     # { channel_id: tk.PhotoImage | None }  None = failed/generic
        self._epg_img_loading = set()  # channel_ids currently being fetched
        self._epg_generic_img = None   # lazily-created generic TV icon PhotoImage
        self._epg_loaded      = False
        self._epg_loading     = False
        self._epg_load_error  = False
        self._epg_status_msg  = "Loading EPG data..."
        # FIX 8: generation counter + lock for icon cache thread-safety
        self._epg_gen         = 0
        self._epg_icon_lock   = threading.Lock()
        # ── Channel Guide (CH 0) state ────────────────────────────────────
        self._guide_active           = False
        self._guide_frame            = None
        self._guide_canvas           = None
        self._guide_video_frame      = None
        self._guide_scroll_y         = 0
        self._guide_after_id         = None
        self._guide_update_id        = None
        self._guide_player           = None
        self._guide_in_commercial    = False
        self._guide_comm_queue       = []
        self._guide_comm_poll_id     = None
        self._guide_offline_label    = None
        self._guide_player_err_count = 0
        # FIX 17: timestamp for guide stream startup grace period
        self._guide_open_time        = 0.0
        # EPG grid scroll state
        self._epg_scroll_y           = 0
        self._epg_scroll_target      = 0
        self._epg_scroll_anim_id     = None
        self._epg_selected_ch_idx    = None   # highlighted channel in EPG grid
        # ── Commercial break state ────────────────────────────────────────
        self._commercials        = []
        self._in_commercial      = False
        self._commercial_queue   = []
        self._post_commercial_fn = None
        # ── Watermark / network bug ───────────────────────────────────────
        self._watermark_image    = None
        self._watermark_label    = None
        self._watermark_resize_after = None
        # ── Scanline after-id ─────────────────────────────────────────────
        self._scanline_after_id  = None
        # ── Closing flag ──────────────────────────────────────────────────
        self._closing = False
        # ── EPG overlay (per-channel bottom bar) ──────────────────────────
        self._epg_overlay_active   = False
        self._epg_overlay_preview  = False
        self._epg_overlay_frame    = None
        self._epg_overlay_after_id = None
        # ── Fullscreen mouse-triggered control overlay ────────────────────
        self._fs_overlay_frame        = None
        self._fs_overlay_after_id     = None
        self._fs_overlay_visible      = False
        self._fs_overlay_seek_dragging = False
        self._fs_overlay_seek_slider  = None
        self._fs_pp_btn               = None
        self._fs_mute_btn             = None
        self._fs_poll_id              = None   # after() id for mouse-position poll loop
        self._fs_last_mouse_pos       = None   # last (x, y) seen by the poll loop
        self._fs_monitor_rect         = None   # (mx, my, mw, mh) saved on FS enter
        # ── News state ────────────────────────────────────────────────────
        self._news_state = _load_news_state()
        self._offline_font = font.Font(family="Courier New", size=10, weight="bold")
        # ── Local File Retry State (NEW) ──────────────────────────────────
        self._local_retry_count = 0
        self._max_retries = 3
        self._is_retrying = False
        self._retry_job_id = None
        self._last_local_seek_ms = 0
        # ── DVR state ─────────────────────────────────────────────────────
        self._dvr_process        = None    # active ffmpeg subprocess
        self._dvr_channel        = None    # Channel being recorded
        self._dvr_out_path       = None    # current output file path
        self._dvr_start_ts       = None    # datetime when recording started
        self._dvr_blink_id       = None    # after() id for ● REC indicator blink
        self._dvr_blink_on       = True
        self._dvr_recordings_dir = os.path.join(self.base_dir, "recordings")
        self._dvr_ffmpeg         = self._find_ffmpeg()
        self._dvr_schedules      = []   # list of schedule dicts
        self._dvr_timers         = {}   # sched_id → (start_timer | None, stop_timer | None)
        self._dvr_playing_path   = None # path of DVR recording currently playing in-app
        self._dvr_queue          = []   # paths queued for consecutive playback
        # ── Sleep timer ───────────────────────────────────────────────────
        self._sleep_timer_id  = None
        self._sleep_remaining = 0      # seconds remaining

        # VLC
        self.vlc_instance = None
        self.player       = None
        self._settings = _load_settings_file()
        # ── First-run detection ──────────────────────────────────────────────
        # Show the setup wizard when neither config file exists yet (fresh install
        # or the user deleted both files to reset).  We skip initial channel-tune
        # in that case so no video tries to start before the user has chosen a
        # channel source.
        _tv_cfg_path   = os.path.join(self.base_dir, "tv_config.json")
        _user_cfg_path = os.path.join(self.base_dir, "user_config.json")
        self._first_run = (not os.path.exists(_tv_cfg_path)
                           and not os.path.exists(_user_cfg_path))
        self._load_config()
        self._build_ui()
        self._rebuild_channel_list()
        self._init_vlc()
        if self._settings['always_on_top']:
            self.attributes('-topmost', True)
        if self.channels and not self._first_run:
            last_ch_name = (self._resume_state.get('__last_channel__', None)
                            if self._settings['start_last_channel'] else None)
            start_idx = 0
            if last_ch_name is not None:
                for i, ch in enumerate(self.channels):
                    if ch.name == last_ch_name:
                        start_idx = i
                        break
            self._tune_channel(start_idx, static=True)
        if self._settings['start_fullscreen'] and not self._first_run:
            self.after(400, self._enter_fullscreen)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # ── Keyboard bindings ───────────────────────────────────────────
        self.bind("<Escape>",  lambda e: self._exit_fullscreen())
        self.bind("<F11>",     lambda e: self._toggle_fullscreen())
        self.bind("<f>",       lambda e: self._toggle_favorite())
        self.bind("<F>",       lambda e: self._toggle_favorite())
        self.bind("<g>",       lambda e: self._toggle_epg_overlay())
        self.bind("<G>",       lambda e: self._toggle_epg_overlay())

        self.bind("<Left>",    lambda e: self._prev_channel())
        self.bind("<Right>",   lambda e: self._next_channel())
        self.bind("<Up>",      lambda e: self._volume_up())
        self.bind("<Down>",    lambda e: self._volume_down())
        self.bind("<XF86AudioPlay>",  lambda e: self._media_play_pause())
        self.bind("<XF86AudioPause>", lambda e: self._media_play_pause())
        self.bind("<XF86AudioStop>",  lambda e: self._media_stop())
        self.bind("<XF86AudioNext>",  lambda e: self._next_channel())
        self.bind("<XF86AudioPrev>",  lambda e: self._prev_channel())
        self.bind("<XF86AudioRaiseVolume>", lambda e: self._volume_up())
        self.bind("<XF86AudioLowerVolume>", lambda e: self._volume_down())
        self.bind("<XF86AudioMute>",        lambda e: self._toggle_mute())
        self.bind("<Control-l>", lambda e: self._open_log_file())
        self.bind("<Control-L>", lambda e: self._open_log_file())
        self.bind("<r>", lambda e: self._dvr_toggle())
        self.bind("<R>", lambda e: self._dvr_toggle())
        self.bind("<space>", lambda e: self._space_play_pause())
        self.bind("<Control-m>", lambda e: self._remote_show_dialog())
        self.bind("<Control-M>", lambda e: self._remote_show_dialog())
        if sys.platform == 'win32':
            self._media_key_queue = queue.Queue()
            self._smtc = _SMTCBridge(self._media_key_queue)
            self._setup_windows_media_keys()
            self._poll_media_keys()
        self._poll_vlc()
        self._remote_start()
        if self._settings['use_curated_channels']:
            self.after(800, self._check_remote_config)
        self.after(1200, self._check_software_update)
        self.after(2000, self._check_news)
        # ── Fetch EPG data on startup, then refresh per setting ───────────
        self._fetch_epg_async()
        self.after(int(self._settings['epg_refresh_hours'] * 3600 * 1000), self._epg_periodic_refresh)
        self._dvr_load_schedules()
        # ── Show the first-run setup wizard if this is a fresh install ───
        if self._first_run:
            self.after(250, self._show_first_run_dialog)

    # ══════════════════════════════════════════
    #  EPG Fetching and Parsing
    # ══════════════════════════════════════════
    def _epg_periodic_refresh(self):
        """Re-fetch EPG data periodically so long-running sessions stay current."""
        if self._closing:
            return
        hours = self._settings.get('epg_refresh_hours', 6)
        print(f"[EPG] {hours}-hour background refresh triggered")
        self._fetch_epg_async(silent=True)
        self.after(int(hours * 3600 * 1000), self._epg_periodic_refresh)

    def _fetch_epg_async(self, silent=False):
        """Start background EPG download (fetches built-in guides + any user-configured sources).
        When silent=True the existing data stays visible until the new data is ready."""
        if self._epg_loading:
            return
        self._epg_loading = True
        if not silent:
            self._epg_status_msg = "📡  Loading program guide..."
        epg_txt_path = os.path.join(self.base_dir, EPG_TXT)
        epg_txt_sources = []
        if os.path.exists(epg_txt_path):
            try:
                with open(epg_txt_path, 'r', encoding='utf-8') as _f:
                    for _line in _f:
                        _url = _line.strip()
                        if _url and not _url.startswith('#'):
                            epg_txt_sources.append((_url, _url if _url.startswith("http") else os.path.basename(_url)))
            except Exception as _e:
                print(f"[EPG] Could not read {EPG_TXT}: {_e}")
        sources = [
            (EPG_URL, "guide.xml"),
        ] + epg_txt_sources + [(s, os.path.basename(s) if not s.startswith("http") else s) for s in self._user_epg_sources]
        wanted = {ch.channel_id for ch in self.channels if ch.channel_id}
        print(f"[EPG] Starting background fetch ({len(sources)} source(s), "
              f"{len(wanted)} wanted channel id(s))")
        def _fetch():
            merged_epg   = {}
            merged_icons = {}
            errors       = []
            try:
                for src, label in sources:
                    try:
                        if src.startswith(("http://", "https://")):
                            # Built-in guide: prefer the gzipped copy, fall back to plain XML
                            urls = [src + ".gz", src] if src == EPG_URL else [src]
                            raw_bytes = None
                            for _u in urls:
                                try:
                                    req = urllib.request.Request(
                                        _u,
                                        headers={
                                            "User-Agent": f"RetroTV/{APP_VERSION}",
                                            "Cache-Control": "no-cache",
                                            "Pragma": "no-cache",
                                            "Accept-Encoding": "gzip",
                                        }
                                    )
                                    with urllib.request.urlopen(req, timeout=30) as resp:
                                        body = resp.read()
                                    raw_bytes = _maybe_gunzip(body)
                                    print(f"[EPG] Downloaded {len(body):,} bytes "
                                          f"({len(raw_bytes):,} unpacked) from {_u}")
                                    break
                                except Exception as _e:
                                    if _u is urls[-1]:
                                        raise
                                    print(f"[EPG] {_u} failed ({_e}); trying fallback")
                        else:
                            with open(src, 'rb') as f:
                                raw_bytes = _maybe_gunzip(f.read())
                            print(f"[EPG] Read {len(raw_bytes):,} bytes from local file: {label}")
                        epg_data, icons = parse_epg_xml(raw_bytes, wanted)
                        for ch_id, progs in epg_data.items():
                            if ch_id in merged_epg:
                                merged_epg[ch_id].extend(progs)
                            else:
                                merged_epg[ch_id] = list(progs)
                        for ch_id, icon_url in icons.items():
                            merged_icons.setdefault(ch_id, icon_url)
                    except Exception as exc:
                        print(f"[EPG] Fetch failed for {label}: {exc}")
                        errors.append(f"{label}: {exc}")
                for ch_id in merged_epg:
                    merged_epg[ch_id].sort(key=lambda p: p['start'])
                if merged_epg:
                    self.after(0, lambda: self._on_epg_loaded(merged_epg, merged_icons, silent))
                else:
                    self.after(0, lambda: self._on_epg_error("; ".join(errors) or "No data", silent))
            except Exception as exc:
                print(f"[EPG] Unexpected fetch error: {exc}")
                self.after(0, lambda: self._on_epg_error(str(exc), silent))
        threading.Thread(target=_fetch, daemon=True, name="epg-fetch").start()
    def _on_epg_loaded(self, data, icons, silent=False):
        """Called on main thread when EPG data is ready."""
        self._epg_data    = data
        self._epg_icons   = icons
        self._epg_loaded  = True
        self._epg_loading = False
        self._epg_load_error = False
        if not silent:
            self._epg_status_msg = f"✔  Guide data loaded  ({sum(len(v) for v in data.values())} programmes)"
        # FIX 8: bump generation so in-flight icon threads from the previous
        # load discard their stale results; clear caches under lock.
        self._epg_gen += 1
        with self._epg_icon_lock:
            self._epg_img_cache.clear()
            self._epg_img_loading.clear()
        print(f"[EPG] Data ready: {len(data)} channels, {len(icons)} channel icons "
              f"(gen={self._epg_gen})")
        # Kick off background icon pre-fetch for visible channels
        self._prefetch_epg_icons()
        # Refresh guide if it's currently open
        if self._guide_active and self._guide_canvas:
            self._draw_epg_grid()
    def _on_epg_error(self, msg, silent=False):
        self._epg_loading = False
        print(f"[EPG] Error: {msg}")
        if silent:
            # Keep existing data; don't surface the error to the user
            return
        self._epg_loaded     = False
        self._epg_load_error = True
        self._epg_status_msg = f"✖  EPG unavailable: {msg[:60]}"
        if self._guide_active and self._guide_canvas:
            self._draw_epg_grid()
    # ══════════════════════════════════════════
    #  EPG Channel Icon Cache
    # ══════════════════════════════════════════
    _EPG_ICON_W = 36   # target icon width in card
    _EPG_ICON_H = 28   # target icon height in card
    def _prefetch_epg_icons(self):
        """Kick off background downloads for all known channel icons."""
        for ch in self.channels:
            if ch.channel_id and ch.channel_id in self._epg_icons:
                self._fetch_channel_icon(ch.channel_id)
    def _fetch_channel_icon(self, channel_id):
        """Download and cache a single channel icon in a background thread."""
        # FIX 8: check membership and capture URL under lock; capture gen before spawn
        with self._epg_icon_lock:
            if channel_id in self._epg_img_cache or channel_id in self._epg_img_loading:
                return
            url = self._epg_icons.get(channel_id)
            if not url:
                self._epg_img_cache[channel_id] = None
                return
            self._epg_img_loading.add(channel_id)
        gen_at_spawn = self._epg_gen
        def _do_fetch(cid=channel_id, u=url, expected_gen=gen_at_spawn):
            photo = None
            try:
                req = urllib.request.Request(u, headers={"User-Agent": f"RetroTV/{APP_VERSION}"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    img_bytes = resp.read()
                import io
                try:
                    from PIL import Image, ImageTk
                    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                    img.thumbnail((self._EPG_ICON_W, self._EPG_ICON_H), Image.LANCZOS)
                    # Composite onto white background so transparent PNGs render correctly
                    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    photo = ImageTk.PhotoImage(bg.convert("RGB"))
                except ImportError:
                    # Pillow not available — try tk PhotoImage (PNG only, base64-encoded)
                    tmp = tk.PhotoImage(data=base64.b64encode(img_bytes))
                    # Subsample to fit
                    sw = max(1, tmp.width()  // self._EPG_ICON_W)
                    sh = max(1, tmp.height() // self._EPG_ICON_H)
                    s  = max(sw, sh)
                    photo = tmp.subsample(s, s) if s > 1 else tmp
            except Exception as e:
                print(f"[EPG icons] Failed to load icon for {cid}: {e}")
            self.after(0, lambda: self._on_icon_loaded(cid, photo, expected_gen))
        threading.Thread(target=_do_fetch, daemon=True,
                         name=f"icon-{channel_id[:20]}").start()
    def _on_icon_loaded(self, channel_id, photo, expected_gen=None):
        """Main-thread callback — store icon and refresh guide if open."""
        # FIX 8: discard results from a superseded EPG load
        if expected_gen is not None and expected_gen != self._epg_gen:
            with self._epg_icon_lock:
                self._epg_img_loading.discard(channel_id)
            return
        with self._epg_icon_lock:
            self._epg_img_loading.discard(channel_id)
            self._epg_img_cache[channel_id] = photo   # None means use generic
        if self._guide_active and self._guide_canvas:
            self._draw_epg_grid()
    def _get_epg_channel_image(self, channel_id):
        """Return cached PhotoImage for channel_id, or None for generic icon.
        Triggers a background fetch if not yet started."""
        if not channel_id:
            return None
        # FIX 8: read shared cache under lock
        with self._epg_icon_lock:
            if channel_id in self._epg_img_cache:
                return self._epg_img_cache[channel_id]
        # Not fetched yet — start fetch and return None for now
        if channel_id in self._epg_icons:
            self._fetch_channel_icon(channel_id)
        else:
            with self._epg_icon_lock:
                self._epg_img_cache[channel_id] = None
        return None
    def _draw_generic_tv_icon(self, c, cx, cy):
        """Draw a simple retro TV icon centered at (cx, cy) using canvas primitives."""
        w, h = 30, 22
        x0, y0 = cx - w // 2, cy - h // 2
        x1, y1 = x0 + w, y0 + h
        # TV body
        c.create_rectangle(x0, y0, x1, y1, fill="#222244", outline="#5555aa", width=1)
        # Screen
        sx0, sy0 = x0 + 3, y0 + 3
        sx1, sy1 = x1 - 3, y1 - 5
        c.create_rectangle(sx0, sy0, sx1, sy1, fill="#001133", outline="#3333aa", width=1)
        # Screen glow lines
        c.create_line(sx0 + 2, sy0 + 3, sx1 - 2, sy0 + 3, fill="#112244", width=1)
        c.create_line(sx0 + 2, sy0 + 6, sx1 - 2, sy0 + 6, fill="#112244", width=1)
        # Antenna left
        c.create_line(cx - 6, y0, cx - 2, y0 - 7, fill="#5555aa", width=1)
        # Antenna right
        c.create_line(cx + 6, y0, cx + 2, y0 - 7, fill="#5555aa", width=1)
        # Stand
        c.create_rectangle(cx - 4, y1, cx + 4, y1 + 3, fill="#222244", outline="#5555aa", width=1)
        # Power dot
        c.create_oval(x1 - 6, y1 - 4, x1 - 3, y1 - 1, fill="#00cc44", outline="")
    def _check_news(self):
        def fetch():
            try:
                req = urllib.request.Request(
                    self.NEWS_URL,
                    headers={"User-Agent": f"RetroTV/{APP_VERSION}"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read()
                text = raw.decode("utf-8", errors="replace").strip()
            except Exception as exc:
                print(f"[news] fetch failed: {exc}")
                return
            if not text:
                return
            content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
            last_hash = self._news_state.get("last_hash", "")
            if content_hash == last_hash:
                return
            # First time we've ever fetched news (no prior hash) — record it
            # silently so the dialog doesn't pop up on a fresh install.
            is_first_news = (last_hash == "")
            self._news_state["last_hash"] = content_hash
            self._news_state["last_seen"] = datetime.datetime.now().isoformat()
            _save_news_state(self._news_state)
            if not is_first_news:
                self.after(0, lambda: self._show_news_modal(text))
        threading.Thread(target=fetch, daemon=True).start()
    def _show_news_modal(self, news_text: str):
        if self._closing:
            return
        dialog = tk.Toplevel(self)
        self._apply_icon(dialog)
        dialog.title("RetroTV News")
        dialog.configure(bg="#1c1308")
        dialog.resizable(True, True)
        dialog.grab_set()
        dialog.focus_force()
        self._center_dialog(dialog, 560, 420)
        dialog.minsize(420, 320)
        border = tk.Frame(dialog, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        title_bar = tk.Frame(inner, bg=self.AMBER_DIM)
        title_bar.pack(fill=tk.X)
        signal_label = tk.Label(
            title_bar, text="📡",
            font=font.Font(family="Courier New", size=11),
            fg=self.AMBER, bg=self.AMBER_DIM, pady=5, padx=8
        )
        signal_label.pack(side=tk.LEFT)
        tk.Label(
            title_bar,
            text="RETROTV NEWS",
            font=font.Font(family="Courier New", size=13, weight="bold"),
            fg=self.AMBER, bg=self.AMBER_DIM,
            anchor="w", pady=5
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ts = datetime.datetime.now().strftime("%m/%d/%Y  %I:%M %p")
        tk.Label(
            title_bar,
            text=f"  {ts}  ",
            font=font.Font(family="Courier New", size=8),
            fg=self.AMBER_DIM, bg="#1c1308",
            anchor="e", pady=5, padx=8
        ).pack(side=tk.RIGHT)
        ticker_frame = tk.Frame(inner, bg="#000050",
                                highlightthickness=1,
                                highlightbackground=self.GUIDE_ACCENT)
        ticker_frame.pack(fill=tk.X, padx=0, pady=0)
        tk.Label(
            ticker_frame,
            text="  ◈  INCOMING TRANSMISSION  ◈  N5 NETWORKS  ◈  RETROTV  ◈  ",
            font=font.Font(family="Courier New", size=8),
            fg=self.GUIDE_ACCENT, bg="#000050",
            anchor="w", padx=6, pady=3
        ).pack(fill=tk.X)
        body_frame = tk.Frame(inner, bg="#0a0a14",
                              highlightthickness=1,
                              highlightbackground=self.AMBER_DIM)
        body_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))
        scrollbar = tk.Scrollbar(body_frame, bg="#2a1f10",
                                 troughcolor="#0a0a14", width=10,
                                 highlightthickness=0)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget = tk.Text(
            body_frame,
            bg="#0a0a14", fg=self.TEXT_CLR,
            font=font.Font(family="Courier New", size=10),
            relief=tk.FLAT, bd=0, wrap=tk.WORD,
            padx=12, pady=10, cursor="arrow",
            highlightthickness=0, yscrollcommand=scrollbar.set,
            state=tk.NORMAL, spacing1=2, spacing2=2, spacing3=4,
        )
        text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=text_widget.yview)
        text_widget.tag_configure("heading", font=font.Font(family="Courier New", size=11, weight="bold"),
            foreground=self.AMBER, spacing1=6, spacing3=4)
        text_widget.tag_configure("bullet", font=font.Font(family="Courier New", size=10),
            foreground=self.TEXT_CLR, lmargin1=18, lmargin2=18)
        text_widget.tag_configure("divider", foreground=self.AMBER_DIM, spacing1=4, spacing3=4)
        for line in news_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("###") or stripped.startswith("##") or stripped.startswith("#"):
                text_widget.insert(tk.END, stripped.lstrip("#").strip() + "\n", "heading")
            elif stripped.startswith("---") or stripped.startswith("==="):
                text_widget.insert(tk.END, "─" * 58 + "\n", "divider")
            elif stripped.startswith("- ") or stripped.startswith("* "):
                text_widget.insert(tk.END, "  •  " + stripped[2:] + "\n", "bullet")
            elif stripped == "":
                text_widget.insert(tk.END, "\n")
            else:
                text_widget.insert(tk.END, line + "\n")
        text_widget.config(state=tk.DISABLED)
        text_widget.yview_moveto(0)
        _blink_state = [True]
        _blink_job   = [None]
        def _blink():
            if not dialog.winfo_exists(): return
            _blink_state[0] = not _blink_state[0]
            signal_label.config(fg=self.AMBER if _blink_state[0] else self.AMBER_DIM)
            _blink_job[0] = dialog.after(700, _blink)
        _blink_job[0] = dialog.after(700, _blink)
        def _on_dialog_close():
            if _blink_job[0]:
                try: dialog.after_cancel(_blink_job[0])
                except Exception: pass
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", _on_dialog_close)
        dialog.bind("<Escape>", lambda e: _on_dialog_close())
        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=10, pady=(2, 10))
        close_btn = tk.Button(btn_row, text="  ✔  GOT IT  ", command=_on_dialog_close,
            bg=self.AMBER_DIM, fg=self.AMBER, activebackground=self.AMBER, activeforeground="#000000",
            font=font.Font(family="Courier New", size=10, weight="bold"),
            relief=tk.FLAT, padx=14, pady=6, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER)
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Enter>", lambda e: close_btn.config(bg=self.AMBER, fg="#000000"))
        close_btn.bind("<Leave>", lambda e: close_btn.config(bg=self.AMBER_DIM, fg=self.AMBER))
        tk.Label(btn_row, text="  This message will only appear once until the next update.",
            font=font.Font(family="Courier New", size=8),
            fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(side=tk.LEFT)

    # ══════════════════════════════════════════
    #  Windows media-key hook
    # ══════════════════════════════════════════
    def _setup_windows_media_keys(self):
        WM_APPCOMMAND = 0x0319
        WM_HOTKEY     = 0x0312
        appcommand_dispatch = {
            14: self._media_play_pause, 13: self._media_stop,
            11: self._next_channel, 12: self._prev_channel,
            10: self._volume_up, 9: self._volume_down, 8: self._toggle_mute,
        }
        hotkey_vk = {
            1: (0xB3, self._media_play_pause), 2: (0xB2, self._media_stop),
            3: (0xB0, self._next_channel), 4: (0xB1, self._prev_channel),
            5: (0xAF, self._volume_up), 6: (0xAE, self._volume_down),
            7: (0xAD, self._toggle_mute),
        }
        try:
            import ctypes.wintypes as wt
            user32 = ctypes.windll.user32
            hwnd   = self.winfo_id()
            self._registered_hotkeys = []
            for hk_id, (vk, _) in hotkey_vk.items():
                if user32.RegisterHotKey(hwnd, hk_id, 0, vk):
                    self._registered_hotkeys.append(hk_id)
            self._hotkey_dispatch = {hk_id: fn for hk_id, (_, fn) in hotkey_vk.items()}
            user32.SetWindowLongPtrW.restype  = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
            user32.GetWindowLongPtrW.restype  = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
            user32.CallWindowProcW.restype    = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes   = [ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t]
            GWLP_WNDPROC  = -4
            old_proc_addr = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
            WndProcType   = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t)
            def new_wndproc(hwnd_, msg, wparam, lparam):
                if msg == WM_HOTKEY:
                    self._media_key_queue.put_nowait(('hotkey', int(wparam)))
                    return 0
                if msg == WM_APPCOMMAND:
                    cmd = (lparam >> 16) & 0x0FFF
                    if cmd in appcommand_dispatch:
                        self._media_key_queue.put_nowait(('appcommand', cmd))
                    return 1
                return user32.CallWindowProcW(old_proc_addr, hwnd_, msg, wparam, lparam)
            self._wndproc_cb       = WndProcType(new_wndproc)
            self._old_wndproc_addr = old_proc_addr
            user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(self._wndproc_cb, ctypes.c_void_p).value)
        except Exception as exc:
            print(f"[media keys] setup failed: {exc}")
    def _poll_media_keys(self):
        appcommand_dispatch = {
            14: self._media_play_pause, 13: self._media_stop,
            11: self._next_channel, 12: self._prev_channel,
            10: self._volume_up, 9: self._volume_down, 8: self._toggle_mute,
        }
        try:
            while True:
                item = self._media_key_queue.get_nowait()
                kind, code = item
                if kind == 'hotkey':
                    action = getattr(self, '_hotkey_dispatch', {}).get(code)
                elif kind == 'smtc':
                    action = {'play_pause': self._media_play_pause, 'stop': self._media_stop,
                              'next': self._next_channel, 'prev': self._prev_channel}.get(code)
                elif kind == 'smtc_seek':
                    action = (lambda ms=code: self._smtc_seek(ms))
                elif kind == 'dblclick':
                    action = self._toggle_fullscreen
                else:
                    action = appcommand_dispatch.get(code)
                if action:
                    action()
        except queue.Empty:
            pass
        self.after(50, self._poll_media_keys)
    def _smtc_seek(self, ms):
        """Seek requested from the Windows media overlay's timeline."""
        total = getattr(self, '_vlc_len_ms', 0)
        if not self.player or total <= 0 or not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not (ch.is_local or getattr(self, '_dvr_playing_path', None)): return
        self._seek_to_frac(max(0.0, min(1.0, ms / total)))
    def _smtc_sync(self, ch, state, t_ms, total):
        """Push title / status / playhead to the Windows media overlay (1 Hz)."""
        smtc = getattr(self, '_smtc', None)
        if not smtc or not smtc.ok: return
        playing = state == vlc.State.Playing
        smtc.set_status(playing, paused=(state == vlc.State.Paused))
        dvr = getattr(self, '_dvr_playing_path', None)
        if dvr:
            title = os.path.splitext(os.path.basename(dvr))[0]
            subtitle = f"{ch.name} · DVR"
        elif ch.is_local and self._in_commercial:
            title, subtitle = "Commercial Break", ch.name
        elif ch.is_local and self._local_track_order and self._local_track_idx < len(self._local_track_order):
            actual_idx = self._local_track_order[self._local_track_idx]
            title = (os.path.splitext(os.path.basename(ch.playlist[actual_idx]))[0]
                     if actual_idx < len(ch.playlist) else ch.name)
            subtitle = f"CH {ch.number} · {ch.name}"
        else:
            title, subtitle = ch.name, (f"CH {ch.number} · LIVE" if ch.number != 0 else "")
        smtc.set_metadata(title, subtitle)
        if (dvr or ch.is_local) and total > 0 and t_ms >= 0 and state in (vlc.State.Playing, vlc.State.Paused):
            smtc.set_timeline(t_ms, total)
        elif not (dvr or ch.is_local):
            smtc.set_timeline(0, 0)
    # ══════════════════════════════════════════
    #  Config loading
    # ══════════════════════════════════════════
    def _load_config(self):
        if not os.path.exists(self.config_path):
            self._create_demo_config()
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = f.read()
            raw = re.sub(r',\s*(\]|\})', r'\1', raw)
            cfg = json.loads(raw)
        except Exception as e:
            print(f"Config error: {e}")
            cfg = {"channels": []}
        if isinstance(cfg, dict):
            if "guide_livestream_url" in cfg:
                self.GUIDE_LIVESTREAM_URL = cfg["guide_livestream_url"].strip()
        ch_num = 1
        self.channels.append(Channel(0, "CHANNEL GUIDE"))
        raw = cfg.get("channels", cfg) if isinstance(cfg, dict) else cfg
        flat_channels = []
        for item in (raw if isinstance(raw, list) else []):
            if isinstance(item, list):
                flat_channels.extend(item)
            elif isinstance(item, dict):
                flat_channels.append(item)
        for ch in flat_channels:
            if not isinstance(ch, dict):
                continue
            name       = ch.get("name", f"CH{ch_num}")
            url        = ch.get("url", "")
            desc       = ch.get("description", "").strip()
            channel_id = ch.get("channel-id", "").strip()  # EPG channel id
            if url:
                self.channels.append(Channel(ch_num, name, url=url, description=desc,
                                             channel_id=channel_id or None))
                ch_num += 1
        user_config_path = os.path.join(self.base_dir, "user_config.json")
        if os.path.exists(user_config_path):
            try:
                with open(user_config_path, 'r', encoding='utf-8') as f:
                    raw_user = f.read()
                raw_user = re.sub(r',\s*(\]|\})', r'\1', raw_user)
                user_cfg = json.loads(raw_user)
            except Exception as e:
                print(f"[user_config] parse error: {e}")
                user_cfg = []
            # ── Custom EPG sources ────────────────────────────────────────
            if isinstance(user_cfg, dict):
                for src in user_cfg.get("epg_urls", []):
                    src = src.strip()
                    if not src:
                        continue
                    # Resolve relative local paths against the config directory
                    if not src.startswith(("http://", "https://")) and not os.path.isabs(src):
                        src = os.path.join(self.base_dir, src)
                    self._user_epg_sources.append(src)
            entries = user_cfg if isinstance(user_cfg, list) else user_cfg.get("channels", [])
            for entry in entries:
                name      = entry.get("name", f"CH{ch_num}")
                url       = entry.get("url", "").strip()
                pl_file   = entry.get("playlist", "").strip()
                randomize = str(entry.get("random", "0")).strip() not in ("0", "false", "False", "")
                commercials = str(entry.get("commercials", "1")).strip() not in ("0", "false", "False", "")
                color     = entry.get("color", "").strip() or None
                desc      = entry.get("description", "").strip()
                channel_id = entry.get("channel-id", "").strip()
                watermark_path = entry.get("watermark", "").strip()
                if watermark_path:
                    if not os.path.isabs(watermark_path):
                        watermark_path = os.path.join(self.base_dir, watermark_path)
                    if not os.path.exists(watermark_path):
                        watermark_path = None
                else:
                    watermark_path = None
                if url:
                    self.channels.append(Channel(ch_num, name, url=url, description=desc,
                                                 watermark=watermark_path,
                                                 channel_id=channel_id or None))
                    ch_num += 1
                elif pl_file:
                    if not os.path.isabs(pl_file):
                        pl_file = os.path.join(self.base_dir, pl_file)
                    if not os.path.exists(pl_file):
                        continue
                    tracks = parse_playlist(pl_file)
                    if not tracks:
                        continue
                    self.channels.append(Channel(ch_num, name, playlist=tracks,
                                                 randomize=randomize, description=desc,
                                                 watermark=watermark_path,
                                                 channel_id=channel_id or None,
                                                 commercials=commercials,
                                                 color=color))
                    ch_num += 1
        # ── Load commercials ──────────────────────────────────────────────
        for comm_name in ('commercials.xspf', 'commercials.m3u', 'commercials.m3u8'):
            comm_path = os.path.join(self.base_dir, comm_name)
            if os.path.exists(comm_path):
                self._commercials = parse_playlist(comm_path)
                break
        VIDEO_EXTENSIONS = {
            '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m4v',
            '.mpg', '.mpeg', '.ts', '.m2ts', '.webm', '.ogv', '.3gp',
        }
        comm_dir = os.path.join(self.base_dir, 'commercials')
        if os.path.isdir(comm_dir):
            scanned = []
            for entry in sorted(os.scandir(comm_dir), key=lambda e: e.name.lower()):
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in VIDEO_EXTENSIONS:
                    scanned.append(entry.path)
            if scanned:
                existing = set(self._commercials)
                new_files = [p for p in scanned if p not in existing]
                self._commercials.extend(new_files)
        if self._legacy_fav_numbers:
            for ch in self.channels:
                if ch.number in self._legacy_fav_numbers:
                    self._favorites.add(ch.name)
            self._legacy_fav_numbers = set()
            self._save_favorites()
    def _create_demo_config(self):
        demo = {
            "channels": [
                {"name": "DW NEWS",    "url": "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8", "description": "News"},
                {"name": "AL JAZEERA", "url": "https://live-hls-apps-aje-fa.getaj.net/AJE/index.m3u8", "description": "News"},
            ]
        }
        with open(self.config_path, 'w') as f:
            json.dump(demo, f, indent=2)
    # ══════════════════════════════════════════
    #  VLC setup
    # ══════════════════════════════════════════
    def _init_vlc(self):
        if not VLC_AVAILABLE:
            _log_vlc.error("python-vlc not available — player disabled")
            return
        try:
            # NOTE: dropped the old '--quiet' flag. We now route libvlc's own
            # diagnostics into our Python logger via libvlc_log_set below, so
            # suppressing them meant we were flying blind on stream failures.
            buf = int(self._settings.get('buffer_ms', 1500))
            vlc_args = [
                '--no-video-title-show',
                f'--network-caching={buf}', f'--live-caching={buf}',
                '--clock-jitter=0', '--clock-synchro=0',
            ]
            if not self._settings.get('hw_accel', True):
                vlc_args.append('--no-hw-dec')
            self.vlc_instance = vlc.Instance(*vlc_args)
            self.player = self.vlc_instance.media_player_new()
            hwnd = self.video_frame.winfo_id()
            if sys.platform.startswith('win'):
                self.player.set_hwnd(hwnd)
            elif sys.platform.startswith('linux'):
                self.player.set_xwindow(hwnd)
            elif sys.platform == 'darwin':
                self.player.set_nsobject(hwnd)
            self._install_libvlc_log_hook()
            # Live-status overlay: libvlc reports buffer fill (0-100) via this
            # event. It fires on a libvlc thread; a plain float assignment is
            # safe to read from the Tk thread.
            self._vlc_buffer_pct = 0.0
            try:
                def _on_buffering(event):
                    try:
                        self._vlc_buffer_pct = float(event.u.new_cache)
                    except Exception:
                        pass
                self._vlc_buffering_cb = _on_buffering  # keep a reference
                self.player.event_manager().event_attach(
                    vlc.EventType.MediaPlayerBuffering, _on_buffering)
            except Exception as e:
                _log_vlc.debug("could not attach buffering event: %s", e)
        except Exception as e:
            _log_vlc.exception("VLC init error: %s", e)
            return
        self._vlc_queue   = queue.Queue()
        self._vlc_lock    = threading.Lock()
        self._vlc_thread  = threading.Thread(target=self._vlc_worker, daemon=True, name="vlc-cmd")
        self._vlc_thread.start()
        self._vlc_state   = vlc.State.NothingSpecial
        self._vlc_time_ms = 0
        self._vlc_len_ms  = 0
        _log_vlc.info("VLC initialised (libvlc %s)",
                      getattr(vlc, "libvlc_get_version", lambda: b"?")()
                      .decode(errors="replace") if hasattr(vlc, "libvlc_get_version") else "?")

    def _install_libvlc_log_hook(self):
        """Route libvlc's internal log stream (C-level) into our Python logger.

        libvlc fires its log callback from non-Python threads. We keep the
        handler small and defensive:
          - format the record with libc's vsnprintf so '%s' / '%d' / etc. are
            expanded (dramatically more useful than raw format strings);
          - override threadName in the log record so every libvlc thread shows
            up as 'libvlc' instead of each one minting a fresh 'Dummy-NNN'
            entry in Python's thread registry;
          - drop DEBUG-level messages from libvlc by default — they are
            extremely chatty on live streams (hundreds per second during
            buffering) and will flood the rotating log.
        We hold a reference to the CFUNCTYPE-wrapped callback on self so GC
        can't collect it mid-playback (that would cause a hard crash when
        libvlc next calls the now-freed function pointer)."""
        try:
            if not hasattr(vlc, "libvlc_log_set"):
                _log_vlc.warning("this python-vlc build has no libvlc_log_set; "
                                 "internal VLC errors will not be captured")
                return
            LEVEL_MAP = {0: logging.DEBUG,   # DEBUG
                         2: logging.INFO,    # NOTICE
                         3: logging.WARNING, # WARNING
                         4: logging.ERROR}   # ERROR

            # ── Locate a va_list-formatting function ─────────────────────
            # python-vlc gives us the raw va_list pointer; to stringify it we
            # need a C-level formatter. The export names differ by platform:
            #   Linux / macOS:  libc exports  vsnprintf  directly.
            #   Modern Windows (UCRT, Win10+):  ucrtbase exports
            #     __stdio_common_vsprintf  (with a leading 64-bit options arg).
            #   Legacy Windows (MSVCRT):  msvcrt exports  _vsnprintf.
            # We try each until one works; if all fail we fall back to raw
            # format strings (no regression from the previous behaviour).
            vsn_fn = None  # callable(buf, size, fmt, va_list) -> int
            try:
                if sys.platform == 'win32':
                    # Try modern UCRT first.
                    try:
                        ucrt = ctypes.cdll.ucrtbase
                        raw = ucrt.__stdio_common_vsprintf
                        raw.restype = ctypes.c_int
                        raw.argtypes = [ctypes.c_uint64,     # options
                                        ctypes.c_char_p,     # buf
                                        ctypes.c_size_t,     # size
                                        ctypes.c_char_p,     # fmt
                                        ctypes.c_void_p,     # locale (NULL ok)
                                        ctypes.c_void_p]     # va_list
                        # Options flag _CRT_INTERNAL_PRINTF_STANDARD_SNPRINTF_BEHAVIOR
                        # = 2; makes the function nul-terminate and return the
                        # would-be length on truncation (sane C99 behaviour).
                        _OPTS = ctypes.c_uint64(2)
                        def vsn_fn(buf, size, fmt, va, _raw=raw, _opts=_OPTS):
                            return _raw(_opts, buf, size, fmt, None, va)
                    except (OSError, AttributeError):
                        pass
                    # Fall back to legacy MSVCRT _vsnprintf.
                    if vsn_fn is None:
                        try:
                            msvcrt = ctypes.cdll.msvcrt
                            raw = msvcrt._vsnprintf
                            raw.restype = ctypes.c_int
                            raw.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                            ctypes.c_char_p, ctypes.c_void_p]
                            def vsn_fn(buf, size, fmt, va, _raw=raw):
                                return _raw(buf, size, fmt, va)
                        except (OSError, AttributeError):
                            pass
                else:
                    libc = ctypes.CDLL(None)  # main program resolves to libc
                    raw = libc.vsnprintf
                    raw.restype = ctypes.c_int
                    raw.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                                    ctypes.c_char_p, ctypes.c_void_p]
                    def vsn_fn(buf, size, fmt, va, _raw=raw):
                        return _raw(buf, size, fmt, va)
            except Exception as exc:
                _log_vlc.debug("vsnprintf lookup failed (%s); "
                               "libvlc messages will show raw format strings", exc)
                vsn_fn = None
            if vsn_fn is None:
                _log_vlc.debug("no C-level vsnprintf available on this platform; "
                               "libvlc messages will show raw format strings")

            # Drop DEBUG by default. Set RETROTV_LIBVLC_DEBUG=1 in the env to
            # re-enable detailed libvlc tracing when investigating a crash.
            want_debug = os.environ.get("RETROTV_LIBVLC_DEBUG") == "1"

            # Signature: void cb(void *data, int level, const libvlc_log_t *ctx,
            #                    const char *fmt, va_list args)
            CB_TYPE = ctypes.CFUNCTYPE(None,
                                       ctypes.c_void_p,
                                       ctypes.c_int,
                                       ctypes.c_void_p,
                                       ctypes.c_char_p,
                                       ctypes.c_void_p)

            BUF_SIZE = 1024
            def _cb(data, level, ctx, fmt, args):
                try:
                    if level == 0 and not want_debug:
                        return  # drop libvlc DEBUG by default
                    if fmt and vsn_fn is not None:
                        buf = ctypes.create_string_buffer(BUF_SIZE)
                        try:
                            vsn_fn(buf, BUF_SIZE, fmt, args)
                            msg = buf.value.decode("utf-8", "replace")
                        except Exception:
                            msg = fmt.decode("utf-8", "replace")
                    else:
                        msg = fmt.decode("utf-8", "replace") if fmt else ""
                    # Use LogRecord.extra to override threadName, avoiding the
                    # Dummy-N thread-object explosion.
                    py_level = LEVEL_MAP.get(level, logging.INFO)
                    if _log_libvlc.isEnabledFor(py_level):
                        rec = _log_libvlc.makeRecord(
                            _log_libvlc.name, py_level, "libvlc", 0,
                            "%s", (msg,), None)
                        rec.threadName = "libvlc"
                        _log_libvlc.handle(rec)
                except Exception:
                    pass  # never let a logging bug take down libvlc's thread

            wrapped = CB_TYPE(_cb)
            # Pin BOTH the Python function and the CFUNCTYPE wrapper. libvlc
            # keeps the wrapper's function pointer, so if Python GCs the
            # wrapper we'll segfault on the next log call.
            self._libvlc_log_cb_py = _cb
            self._libvlc_log_cb    = wrapped
            # python-vlc's module-level wrapper accepts the Instance object
            # directly and handles the ctypes marshalling for us.
            vlc.libvlc_log_set(self.vlc_instance, wrapped, None)
            _log_vlc.debug("libvlc log hook installed (vsnprintf=%s, debug=%s)",
                           vsn_fn is not None, want_debug)
        except Exception as exc:
            _log_vlc.warning("could not install libvlc log hook: %s", exc)
    def _vlc_worker(self):
        while True:
            try:
                item = self._vlc_queue.get(timeout=0.25)
            except queue.Empty:
                self._post_vlc_state()
                continue
            if item is None:
                break
            cmd, *args = item
            t0 = _time.monotonic()
            try:
                if cmd == 'play_media':
                    path_or_url, seek_ms, vol = args
                    is_url = '://' in path_or_url
                    _log_vlc.info("play_media: %s (seek=%dms vol=%d)",
                                  path_or_url, seek_ms, vol)

                    # FIX: Pre-warm local drives by touching the file.
                    # This triggers the OS to spin up the drive before VLC tries to open it.
                    if not is_url:
                        try:
                            # Read 1KB to wake the drive
                            with open(path_or_url, 'rb') as f:
                                f.read(1024)
                        except Exception as e:
                            _log_vlc.warning("warm-up failed for %s: %s", path_or_url, e)
                            # Proceed anyway, VLC will handle the error

                    # FIX 9: hold lock only for stop+set_media+play, not the seek wait loop
                    with self._vlc_lock:
                        # CRASH FIX: calling set_media() directly on some libvlc
                        # builds (seen on 3.0.18+ Windows) can segfault because
                        # the previous demuxer or HW decoder (D3D11VA/DXVA2)
                        # hasn't been cleanly torn down.  The documented recipe
                        # is: stop(), release the old media via set_media(None),
                        # THEN load the new media.  This applies to ALL previous
                        # states, not just Error — D3D11VA keeps GPU resources
                        # alive even from a Playing state and races against the
                        # new set_media() call, causing a hard crash on Windows.
                        try:
                            prev_state = self.player.get_state()
                        except Exception:
                            prev_state = vlc.State.NothingSpecial
                        if prev_state == vlc.State.Error:
                            _log_vlc.warning(
                                "player was in Error state before play_media; "
                                "clearing before loading new media")
                        try:
                            self.player.stop()
                            self.player.set_media(None)
                        except Exception as e:
                            _log_vlc.exception("error during media teardown: %s", e)
                        media = self.vlc_instance.media_new(path_or_url)
                        # For live HLS/DASH, request a fresh network session so
                        # we don't get stuck reading a stale, cached playlist
                        # that the origin has already rotated away from.
                        if is_url:
                            try:
                                media.add_option(":no-http-reconnect")
                                media.add_option(":network-caching=1500")
                            except Exception:
                                pass
                        self.player.set_media(media)
                        self.player.play()
                        self.player.audio_set_volume(vol)
                    # Wait for Playing state outside the lock so _post_vlc_state
                    # can still read player state from the main thread.
                    # We always wait (not just when seek_ms > 0) so we can re-apply
                    # volume after VLC's audio pipeline is fully ready — this fixes
                    # the silent-startup bug where audio_set_volume is ignored when
                    # called too soon after play().
                    deadline = _time.monotonic() + 5.0
                    reached_playing = False
                    terminal_state = None
                    _next_post = 0.0
                    while _time.monotonic() < deadline:
                        if self._closing:
                            return
                        # Keep the UI informed while we wait (the idle-loop
                        # state post doesn't run while we're in here).
                        if _time.monotonic() >= _next_post:
                            self._post_vlc_state()
                            _next_post = _time.monotonic() + 0.25
                        with self._vlc_lock:
                            st = self.player.get_state()
                        if st == vlc.State.Playing:
                            reached_playing = True
                            break
                        if st in (vlc.State.Error, vlc.State.Ended):
                            terminal_state = st
                            break
                        _time.sleep(0.05)
                    if reached_playing:
                        _log_vlc.info("play_media reached Playing in %.2fs",
                                      _time.monotonic() - t0)
                        # Retry until audio_get_volume() confirms the value was
                        # accepted.  VLC's audio output pipeline can take a few
                        # extra milliseconds to initialise after entering Playing
                        # state, during which audio_set_volume() is silently
                        # ignored and audio_get_volume() returns -1.
                        for _attempt in range(20):
                            if self._closing:
                                return
                            with self._vlc_lock:
                                self.player.audio_set_volume(vol)
                                actual_vol = self.player.audio_get_volume()
                            # audio_get_volume() returns -1 while the audio
                            # pipeline is still initialising; skip those.
                            if actual_vol >= 0 and actual_vol == vol:
                                break
                            _time.sleep(0.05)
                        else:
                            _log_vlc.warning(
                                "play_media: volume did not settle to %d "
                                "(last reported=%d)", vol, actual_vol)
                        with self._vlc_lock:
                            # Re-apply mute state — VLC resets this on new media.
                            if getattr(self, '_muted', False):
                                self.player.audio_set_mute(True)
                            # Re-apply subtitle state — VLC resets track selection on new media.
                            if getattr(self, '_subs_on', False):
                                self.player.video_set_spu(0)
                            else:
                                self.player.video_set_spu(-1)
                            if seek_ms > 0:
                                self.player.set_time(seek_ms)
                    else:
                        _log_vlc.warning(
                            "play_media did not reach Playing within 5s "
                            "(final state=%s, url=%s)",
                            terminal_state, path_or_url)
                elif cmd == 'stop':
                    _log_vlc.debug("stop")
                    with self._vlc_lock:
                        self.player.stop()
                elif cmd == 'pause':
                    _log_vlc.debug("pause")
                    with self._vlc_lock:
                        self.player.pause()
                elif cmd == 'set_volume':
                    with self._vlc_lock:
                        self.player.audio_set_volume(args[0])
                elif cmd == 'set_mute':
                    with self._vlc_lock:
                        self.player.audio_set_mute(args[0])
                elif cmd == 'seek_frac':
                    with self._vlc_lock:
                        total = self.player.get_length()
                        if total > 0:
                            self.player.set_time(int(args[0] * total))
                elif cmd == 'seek_ms':
                    with self._vlc_lock:
                        total = self.player.get_length()
                        ms = max(0, int(args[0]))
                        if total > 0:
                            ms = min(ms, max(0, total - 500))
                        self.player.set_time(ms)
                elif cmd == 'set_subs':
                    with self._vlc_lock:
                        enabled = args[0]
                        self.player.video_set_spu(0 if enabled else -1)
            except Exception as e:
                _log_vlc.exception("error executing %s: %s", cmd, e)
            finally:
                self._post_vlc_state()
                self._vlc_queue.task_done()
    def _post_vlc_state(self):
        try:
            with self._vlc_lock:
                s  = self.player.get_state()
                t  = self.player.get_time()
                ln = self.player.get_length()
            self.after(0, self._recv_vlc_state, s, t, ln)
        except Exception:
            pass
    def _recv_vlc_state(self, state, time_ms, length_ms):
        if self._closing:
            return
        prev_state        = getattr(self, '_vlc_state', None)
        self._vlc_state   = state
        self._vlc_time_ms = time_ms
        self._vlc_len_ms  = length_ms
        if state == vlc.State.Playing and prev_state != vlc.State.Playing:
            self._update_filename_label()
        if hasattr(self, 'btn_play_pause'):
            dvr_active = getattr(self, '_dvr_playing_path', None)
            is_local_ch = self.channels and self.channels[self.current_channel_idx].is_local
            if dvr_active or is_local_ch:
                if state == vlc.State.Paused:
                    self.btn_play_pause.config(text="▶ PLAY")
                elif state == vlc.State.Playing:
                    self.btn_play_pause.config(text="⏸ PAUSE")
        # Keep the fullscreen overlay play/pause button in sync
        if self._fs_pp_btn and self._fs_overlay_visible:
            try:
                dvr_active  = getattr(self, '_dvr_playing_path', None)
                is_local_ch = self.channels and self.channels[self.current_channel_idx].is_local
                if dvr_active or is_local_ch:
                    pp_text = "▶" if state == vlc.State.Paused else "⏸"
                    self._fs_pp_btn.config(text=pp_text)
            except Exception:
                pass
    def _vlc_cmd(self, cmd, *args):
        if hasattr(self, '_vlc_queue'):
            self._vlc_queue.put((cmd, *args))
    # ══════════════════════════════════════════
    #  UI Construction
    # ══════════════════════════════════════════
    def _build_ui(self):
        self.title(f"RetroTV v{APP_VERSION}")
        self.configure(bg=self.BG)
        self.geometry("960x640")
        saved_pos = self._resume_state.get('__window_pos__')
        if saved_pos:
            sx = saved_pos.get('x', 0) + 100
            sy = saved_pos.get('y', 0) + 100
            self.geometry(f"960x640+{sx}+{sy}")
        def _maximize():
            try:
                self.state('zoomed')  # Windows / macOS
            except tk.TclError:
                try:
                    self.attributes('-zoomed', True)  # X11 / Linux
                except tk.TclError:
                    pass
        self.after(50, _maximize)
        self.minsize(640, 480)
        self.resizable(True, True)
        self.after(100, self._set_window_icon)
        try:
            self.font_lcd    = font.Font(family="Courier New", size=28, weight="bold")
            self.font_small  = font.Font(family="Courier New", size=10, weight="bold")
            self.font_medium = font.Font(family="Courier New", size=12)
            self.font_ch     = font.Font(family="Courier New", size=9)
        except Exception:
            self.font_lcd    = font.Font(size=20, weight="bold")
            self.font_small  = font.Font(size=9,  weight="bold")
            self.font_medium = font.Font(size=11)
            self.font_ch     = font.Font(size=8)
        self.tv_outer = tk.Frame(self, bg="#3d2f1e", bd=0)
        self.tv_outer.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.tv_inner = tk.Frame(self.tv_outer, bg="#2a1f10", bd=0)
        self.tv_inner.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.main_area = tk.Frame(self.tv_inner, bg="#2a1f10")
        self.main_area.pack(fill=tk.BOTH, expand=True)
        self.screen_frame = tk.Frame(self.main_area, bg="#000000", bd=0,
                                      highlightthickness=4,
                                      highlightbackground="#1a1006",
                                      highlightcolor="#1a1006")
        self.screen_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)
        self.video_frame = tk.Frame(self.screen_frame, bg="#000000")
        self.video_frame.pack(fill=tk.BOTH, expand=True)
        self.scanline_canvas = tk.Canvas(self.video_frame, bg="#000000", highlightthickness=0, cursor="none")
        self.scanline_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._draw_scanlines()
        self._win32_overlay = None
        self.nosignal_label = tk.Label(self.scanline_canvas, text="NO SIGNAL",
            font=font.Font(family="Courier New", size=36, weight="bold"),
            fg="#444444", bg="#000000")
        self._nosignal_window = self.scanline_canvas.create_window(0, 0, anchor=tk.CENTER,
            window=self.nosignal_label, state='hidden', tags='nosignal')
        self._offline_frame = tk.Frame(self.scanline_canvas, bg="#000000")
        tk.Label(self._offline_frame,
                 text="📡  CHANNEL OFFLINE",
                 font=font.Font(family="Courier New", size=22, weight="bold"),
                 fg="#cc2200", bg="#000000").pack(pady=(0, 6))
        tk.Label(self._offline_frame,
                 text="This channel is not currently broadcasting.\nPlease try again later.",
                 font=font.Font(family="Courier New", size=11),
                 fg="#888888", bg="#000000", justify="center").pack()
        self._offline_window = self.scanline_canvas.create_window(0, 0, anchor=tk.CENTER,
            window=self._offline_frame, state='hidden', tags='offline')
        self._live_error_count = 0
        self._live_tune_time   = 0.0
        self._live_last_time_ms = -1      # last seen get_time() for freeze detection
        self._live_frozen_since = 0.0     # monotonic time when playhead stopped moving
        self._live_reconnecting = False   # True while a reconnect is in-flight
        self._live_reconnect_deadline = 0.0  # monotonic deadline; reconnect considered stuck past this
        self.scanline_canvas.bind("<Configure>", self._on_canvas_resize)
        self.video_frame.bind("<Configure>", self._on_watermark_resize)
        # OSD: two small Labels (shadow + text) placed over the top-right of screen_frame.
        # Being siblings of video_frame (not children) means they are separate OS windows
        # that the compositor draws on top of VLC's output — no transparency tricks needed.
        self._osd_shadow_label = tk.Label(
            self.screen_frame, text="", font=self.font_lcd,
            fg="#000000", bg="#000000",
            anchor="ne", justify="right",
            padx=0, pady=0, bd=0, highlightthickness=0)
        self._osd_text_label = tk.Label(
            self.screen_frame, text="", font=self.font_lcd,
            fg=self.AMBER, bg="#000000",
            anchor="ne", justify="right",
            padx=0, pady=0, bd=0, highlightthickness=0)
        # Labels start hidden; _show_osd places them, hide_osd calls place_forget
        # Live-stream loading status (CONNECTING / BUFFERING nn% / ...), centred
        # over the video.  Sibling of video_frame so it draws over VLC's window.
        self._live_status_label = tk.Label(
            self.screen_frame, text="",
            font=font.Font(family="Courier New", size=16, weight="bold"),
            fg=self.AMBER, bg="#000000", justify="center",
            padx=18, pady=10, bd=0,
            highlightthickness=2, highlightbackground="#3d2f1e")
        self._live_status_job   = None
        self._live_status_start = 0.0
        self._live_status_armed = False
        # DVR: blinking ● REC indicator anchored to top-left of video area
        self._dvr_rec_label = tk.Label(
            self.screen_frame, text="● REC",
            font=font.Font(family="Courier New", size=11, weight="bold"),
            fg="#cc0000", bg="#000000",
            anchor="nw", padx=0, pady=0, bd=0, highlightthickness=0)
        self.video_frame.bind("<Configure>", self._on_video_frame_resize)
        # Sidebar + resize sash
        self.sidebar = tk.Frame(self.main_area, bg="#1c1308", highlightthickness=1, highlightbackground="#3d2f1e")
        self.sidebar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)
        self.sidebar.pack_propagate(False)
        self.sidebar.configure(width=self._sidebar_width)
        # Sash: packing RIGHT after sidebar stacks it just to sidebar's LEFT
        self._resize_sash = tk.Frame(self.main_area, bg="#3d2f1e", width=6,
                                     cursor="sb_h_double_arrow")
        self._resize_sash.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self._resize_sash.pack_propagate(False)
        self._sash_drag_x = 0
        self._sash_drag_w = 0
        def _sash_press(e):
            self._sash_drag_x = e.x_root
            self._sash_drag_w = self.sidebar.winfo_width()
        def _sash_move(e):
            delta = self._sash_drag_x - e.x_root  # dragging left → positive → wider
            new_w = max(120, min(500, self._sash_drag_w + delta))
            self._sidebar_width = new_w
            self.sidebar.configure(width=new_w)
        self._resize_sash.bind("<Button-1>", _sash_press)
        self._resize_sash.bind("<B1-Motion>", _sash_move)
        self.collapse_btn = tk.Button(self.sidebar, text="◀ HIDE", command=self._toggle_sidebar_collapse,
            bg="#2a1f10", fg=self.AMBER_DIM, activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
            font=self.font_ch, relief=tk.FLAT, padx=4, pady=2, bd=0, cursor="hand2", highlightthickness=0)
        self.collapse_btn.pack(fill=tk.X, padx=4, pady=(4, 0))
        self._attach_tooltip(self.collapse_btn, "Collapse sidebar")
        self.sidebar_content = tk.Frame(self.sidebar, bg="#1c1308")
        self.sidebar_content.pack(fill=tk.BOTH, expand=True)
        ch_nav_frame = tk.Frame(self.sidebar_content, bg="#1c1308")
        ch_nav_frame.pack(fill=tk.X, padx=4, pady=(6, 4))
        self.knob_ch_label = tk.Label(ch_nav_frame, text="CH --",
            font=font.Font(family="Courier New", size=14, weight="bold"),
            fg=self.AMBER, bg="#0d0a06", relief=tk.FLAT, bd=0, padx=4, pady=3)
        self.knob_ch_label.pack(fill=tk.X, pady=(0, 4))
        ch_btn_frame = tk.Frame(ch_nav_frame, bg="#1c1308")
        ch_btn_frame.pack(fill=tk.X, pady=(2, 2))
        ch_btn_frame.columnconfigure(0, weight=1)
        ch_btn_frame.columnconfigure(1, weight=1)
        btn_prev = tk.Button(ch_btn_frame, text="◀ PREV",
            command=self._prev_channel,
            bg="#0d0a06", fg=self.AMBER,
            activebackground=self.AMBER_DIM, activeforeground="#000000",
            font=font.Font(family="Courier New", size=9, weight="bold"),
            relief=tk.FLAT, bd=0, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        btn_prev.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        btn_next = tk.Button(ch_btn_frame, text="NEXT ▶",
            command=self._next_channel,
            bg="#0d0a06", fg=self.AMBER,
            activebackground=self.AMBER_DIM, activeforeground="#000000",
            font=font.Font(family="Courier New", size=9, weight="bold"),
            relief=tk.FLAT, bd=0, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        btn_next.grid(row=0, column=1, sticky="ew", padx=(2, 0))
        for btn in (btn_prev, btn_next):
            btn.bind("<Enter>", lambda e, b=btn: b.config(bg=self.AMBER_DIM, fg="#000000"))
            btn.bind("<Leave>", lambda e, b=btn: b.config(bg="#0d0a06", fg=self.AMBER))
        self._attach_tooltip(btn_prev, "Previous channel  [←]")
        self._attach_tooltip(btn_next, "Next channel  [→]")
        btn_surf = tk.Button(ch_btn_frame, text="🎲 SURF",
            command=self._surf_channel,
            bg="#0d0a06", fg=self.AMBER,
            activebackground=self.AMBER_DIM, activeforeground="#000000",
            font=font.Font(family="Courier New", size=9, weight="bold"),
            relief=tk.FLAT, bd=0, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        btn_surf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        btn_surf.bind("<Enter>", lambda e: btn_surf.config(bg=self.AMBER_DIM, fg="#000000"))
        btn_surf.bind("<Leave>", lambda e: btn_surf.config(bg="#0d0a06", fg=self.AMBER))
        self._attach_tooltip(btn_surf, "Tune to a random channel")
        ttk.Separator(self.sidebar_content, orient='horizontal').pack(fill=tk.X, pady=(4, 2))
        fav_hdr = tk.Frame(self.sidebar_content, bg="#1c1308")
        fav_hdr.pack(fill=tk.X)
        ch_hdr_lbl = tk.Label(fav_hdr, text="CHANNELS", font=self.font_small, fg=self.AMBER, bg="#1c1308", pady=4, cursor="hand2")
        ch_hdr_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ch_hdr_lbl.bind("<Button-1>", lambda e: self._tune_channel(0))
        filter_row = tk.Frame(self.sidebar_content, bg="#1c1308")
        filter_row.pack(fill=tk.X, padx=4, pady=(0, 2))
        self._fav_filter_active = False
        self._fav_filter_btn = tk.Button(
            filter_row, text="★ FAV", font=self.font_small,
            fg=self.AMBER_DIM, bg="#1c1308",
            activebackground=self.AMBER_DIM, activeforeground="#000000",
            relief=tk.FLAT, bd=0, padx=4, pady=2,
            cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM,
            command=self._toggle_fav_filter,
        )
        self._fav_filter_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self._attach_tooltip(self._fav_filter_btn, "Show only favorites")
        self._recent_filter_active = False
        self._recent_filter_btn = tk.Button(
            filter_row, text="◷ HIST", font=self.font_small,
            fg=self.AMBER_DIM, bg="#1c1308",
            activebackground=self.AMBER_DIM, activeforeground="#000000",
            relief=tk.FLAT, bd=0, padx=4, pady=2,
            cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM,
            command=self._toggle_recent_filter,
        )
        self._recent_filter_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._attach_tooltip(self._recent_filter_btn, "Show recently watched")
        ttk.Separator(self.sidebar_content, orient='horizontal').pack(fill=tk.X)
        # ── Channel search box ───────────────────────────────────────────
        self._ch_search_frame = tk.Frame(self.sidebar_content, bg="#1c1308")
        self._ch_search_frame.pack(fill=tk.X, padx=4, pady=(4, 2))
        self._ch_search_var = tk.StringVar()
        self._ch_search_entry = tk.Entry(
            self._ch_search_frame,
            textvariable=self._ch_search_var,
            bg="#0d0a06", fg=self.BEIGE,
            insertbackground=self.AMBER,
            font=self.font_ch,
            relief=tk.FLAT, bd=0,
            highlightbackground=self.AMBER_DIM,
            highlightthickness=1,
        )
        self._ch_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        # Placeholder text behaviour
        self._ch_search_placeholder = "🔍 search..."
        self._ch_search_entry.insert(0, self._ch_search_placeholder)
        self._ch_search_entry.config(fg=self.AMBER_DIM)
        def _search_focus_in(e):
            if self._ch_search_var.get() == self._ch_search_placeholder:
                self._ch_search_entry.delete(0, tk.END)
                self._ch_search_entry.config(fg=self.BEIGE)
        def _search_focus_out(e):
            if not self._ch_search_var.get().strip():
                self._ch_search_entry.delete(0, tk.END)
                self._ch_search_entry.insert(0, self._ch_search_placeholder)
                self._ch_search_entry.config(fg=self.AMBER_DIM)
        self._ch_search_entry.bind("<FocusIn>",  _search_focus_in)
        self._ch_search_entry.bind("<FocusOut>", _search_focus_out)
        self._ch_search_var.trace_add("write", lambda *_: self._on_ch_search_changed())
        # Clear (✕) button — hidden until there's a real query
        self._ch_search_clear_btn = tk.Button(
            self._ch_search_frame,
            text="✕",
            command=self._clear_ch_search,
            bg="#0d0a06", fg=self.AMBER_DIM,
            activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
            font=self.font_ch,
            relief=tk.FLAT, bd=0,
            padx=4, pady=0,
            cursor="hand2",
            highlightthickness=0,
        )
        self._attach_tooltip(self._ch_search_clear_btn, "Clear search")
        self._ch_search_has_text = False
        self.ch_list_frame = tk.Frame(self.sidebar_content, bg="#1c1308")
        self.ch_list_frame.pack(fill=tk.BOTH, expand=True, pady=2)
        ch_scroll = tk.Scrollbar(self.ch_list_frame, bg="#2a1f10", troughcolor="#1c1308", width=8)
        ch_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.ch_listbox = tk.Listbox(self.ch_list_frame, bg="#0d0a06", fg=self.BEIGE,
            selectbackground=self.AMBER_DIM, selectforeground=self.AMBER, font=self.font_ch,
            relief=tk.FLAT, bd=0, highlightthickness=0, activestyle='none', yscrollcommand=ch_scroll.set)
        self.ch_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ch_scroll.config(command=self.ch_listbox.yview)
        self.ch_listbox.bind('<<ListboxSelect>>', self._on_listbox_select)
        self.ch_listbox.bind('<Button-3>', self._on_listbox_rightclick)
        self.ch_listbox.bind('<Motion>', self._on_listbox_hover)
        self.ch_listbox.bind('<Leave>', lambda e: self._ch_hover_label.config(text=""))
        self._listbox_ch_indices = []
        self._ch_hover_label = tk.Label(
            self.sidebar_content, text="",
            font=font.Font(family="Courier New", size=9),
            fg=self.AMBER, bg="#0d0a06",
            anchor="w", padx=6, pady=5,
            wraplength=145, justify="left",
        )
        self._ch_hover_label.pack(fill=tk.X, padx=4, pady=(0, 1))
        tk.Label(self.sidebar_content, text=f"v{APP_VERSION}", font=font.Font(family="Courier New", size=7),
            fg=self.AMBER_DIM, bg="#1c1308", anchor='e', padx=4).pack(fill=tk.X, pady=(2, 2))
        # Control Bar
        self.ctrl_bar = tk.Frame(self.tv_inner, bg="#1c1308", highlightthickness=1, highlightbackground="#3d2f1e")
        self.ctrl_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        row1 = tk.Frame(self.ctrl_bar, bg="#1c1308")
        row1.pack(fill=tk.X, padx=10, pady=(6, 2))
        self.live_label = tk.Label(row1, text="▶ LIVE", font=self.font_small, fg=self.AMBER_DIM, bg="#1c1308", width=12, anchor='w')
        self.live_label.pack(side=tk.LEFT)
        self.time_label = tk.Label(row1, text="LIVE", font=self.font_small, fg=self.AMBER_DIM, bg="#1c1308", anchor='e')
        self.time_label.pack(side=tk.RIGHT)
        self.filename_row = tk.Frame(self.ctrl_bar, bg="#1c1308")
        self.filename_label = tk.Label(self.filename_row, text="", font=self.font_ch, fg=self.AMBER, bg="#1c1308", anchor='center')
        self.filename_label.pack(fill=tk.X, expand=True, pady=(0, 1))
        row2 = tk.Frame(self.ctrl_bar, bg="#1c1308")
        row2.pack(fill=tk.X, padx=10, pady=(2, 6))
        tk.Label(row2, text="CH:", font=self.font_small, fg=self.AMBER_DIM, bg="#1c1308").pack(side=tk.LEFT, padx=(0, 2))
        self.ch_entry_var = tk.StringVar()
        self.ch_entry = tk.Entry(row2, textvariable=self.ch_entry_var, width=4, bg="#0d0a06", fg=self.AMBER,
            insertbackground=self.AMBER, font=self.font_medium, relief=tk.FLAT, bd=2,
            highlightbackground=self.AMBER_DIM, highlightthickness=1)
        self.ch_entry.pack(side=tk.LEFT, padx=(0, 4))
        self.ch_entry.bind("<Return>", self._on_channel_entry)
        self._make_btn(row2, "◀◀", self._prev_channel, side=tk.LEFT, tooltip="Previous channel  [←]")
        self._make_btn(row2, "▶▶", self._next_channel, side=tk.LEFT, tooltip="Next channel  [→]")
        self._lineup_btn = self._make_btn(row2, "↺ LINEUP", self._manual_lineup_refresh, side=tk.LEFT, padx=(8, 2), tooltip="Refresh channel lineup")
        self._edit_btn   = self._make_btn(row2, "✎ EDIT", self._open_channel_editor, side=tk.LEFT, padx=(2, 2), tooltip="Edit channels")
        self._make_btn(row2, "📡 EPG", self._open_epg_sources_editor, side=tk.LEFT, padx=(2, 2), tooltip="Manage EPG / guide sources")
        self._make_btn(row2, "⚙", self._open_settings_dialog, side=tk.LEFT, padx=(2, 2), tooltip="Settings")
        self._make_btn(row2, "📱", self._remote_show_dialog, side=tk.LEFT, padx=(2, 2), tooltip="Phone remote - address and pairing code  [Ctrl+M]")

        self.fav_btn  = self._make_btn(row2, "☆", self._toggle_favorite, side=tk.LEFT, padx=(2, 2), tooltip="Add / remove favorite  [F]")
        self.epg_btn  = self._make_btn(row2, "📋 GUIDE", self._toggle_epg_overlay, side=tk.LEFT, padx=(2, 2), tooltip="Program guide overlay  [G]")
        self.mute_btn = self._make_btn(row2, "🔊", self._toggle_mute, side=tk.RIGHT, tooltip="Mute / Unmute")
        self.vol_var = tk.IntVar(value=80)
        self.vol_slider = ttk.Scale(row2, from_=0, to=100, variable=self.vol_var, orient=tk.HORIZONTAL, command=self._on_volume, length=90)
        self.vol_slider.pack(side=tk.RIGHT, padx=(0, 4))
        tk.Label(row2, text="VOL", font=self.font_small, fg=self.AMBER_DIM, bg="#1c1308").pack(side=tk.RIGHT, padx=(8, 2))
        self._make_btn(row2, "⛶", self._toggle_fullscreen, side=tk.RIGHT, padx=(2, 4), tooltip="Toggle fullscreen  [F11]")
        self.sub_btn  = self._make_btn(row2, "CC", self._toggle_subtitles, side=tk.RIGHT, padx=(2, 2), tooltip="Toggle subtitles")
        self._make_btn(row2, "📅 DVR SCHED", self._dvr_open_schedule_manager, side=tk.RIGHT, padx=(2, 2), tooltip="DVR schedule manager")
        self._make_btn(row2, "📼 LIBRARY", self._dvr_open_manager, side=tk.RIGHT, padx=(2, 2), tooltip="DVR recording library")
        self._dvr_btn = self._make_btn(row2, "● REC", self._dvr_toggle, side=tk.RIGHT, padx=(2, 2), tooltip="Start / stop recording")
        self._dvr_btn.config(fg="#cc0000")
        self._sleep_btn = self._make_btn(row2, "⏾ SLEEP", self._show_sleep_dialog, side=tk.RIGHT, padx=(2, 2), tooltip="Sleep timer")
        donate_btn = tk.Label(row2, text="♥  Support This Project",
            font=font.Font(family="Courier New", size=9, underline=True), fg=self.AMBER_DIM, bg="#1c1308", cursor="hand2")
        donate_btn.pack(side=tk.LEFT, expand=True)
        donate_btn.bind("<Button-1>", lambda e: webbrowser.open(self.DONATE_URL))
        self.local_row = tk.Frame(self.ctrl_bar, bg="#1c1308")
        self.btn_prev_track = self._make_btn(self.local_row, "⏮ PREV", self._prev_track, side=tk.LEFT, padx=(2, 2), tooltip="Previous track")
        self.btn_play_pause = self._make_btn(self.local_row, "⏸ PAUSE", self._local_play_pause, side=tk.LEFT, padx=(2, 2), tooltip="Play / Pause  [Space]")
        self.btn_next_track = self._make_btn(self.local_row, "NEXT ⏭", self._next_track, side=tk.LEFT, padx=(2, 8), tooltip="Next track")
        tk.Label(self.local_row, text="POS", font=self.font_small, fg=self.AMBER_DIM, bg="#1c1308").pack(side=tk.LEFT, padx=(4, 2))
        self._playhead_dragging = False
        self.playhead_var = tk.DoubleVar(value=0.0)
        self.playhead_slider = ttk.Scale(self.local_row, from_=0.0, to=1.0, variable=self.playhead_var, orient=tk.HORIZONTAL, command=self._on_playhead_moved, length=200)
        self.playhead_slider.pack(side=tk.LEFT, padx=(0, 4), fill=tk.X, expand=True)
        self.playhead_slider.bind("<ButtonPress-1>", self._playhead_press)
        self.playhead_slider.bind("<ButtonRelease-1>", self._playhead_release)
        self._dvr_exit_btn = self._make_btn(self.local_row, "■ EXIT DVR", self._dvr_exit_playback, side=tk.RIGHT, padx=(4, 2), tooltip="Exit DVR playback")
        self.ch_strip = tk.Label(self.ctrl_bar, text="", font=self.font_lcd, fg=self.AMBER, bg="#080604", padx=10, pady=2)
        self.ch_strip.pack(side=tk.BOTTOM, fill=tk.X)
        self._style_sliders()
        self._update_channel_strip()
        self._update_knob_label()
        self._bind_dblclick_everywhere()
    # ══════════════════════════════════════════
    #  Channel Search
    # ══════════════════════════════════════════
    def _on_ch_search_changed(self):
        query = self._ch_search_var.get()
        is_placeholder = (query == self._ch_search_placeholder)
        has_real_text  = bool(query.strip()) and not is_placeholder
        # Show/hide the clear (✕) button
        if has_real_text and not self._ch_search_has_text:
            self._ch_search_entry.pack_forget()
            self._ch_search_clear_btn.pack(side=tk.RIGHT, padx=2)
            self._ch_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
            self._ch_search_has_text = True
        elif not has_real_text and self._ch_search_has_text:
            self._ch_search_clear_btn.pack_forget()
            self._ch_search_has_text = False
        self._rebuild_channel_list()
    def _clear_ch_search(self):
        self._ch_search_entry.delete(0, tk.END)
        self._ch_search_entry.insert(0, self._ch_search_placeholder)
        self._ch_search_entry.config(fg=self.AMBER_DIM)
        self._ch_search_clear_btn.pack_forget()
        self._ch_search_has_text = False
        self._rebuild_channel_list()
    # ══════════════════════════════════════════
    #  Per-Channel EPG Schedule Overlay
    # ══════════════════════════════════════════
    def _toggle_epg_overlay(self):
        """Toggle the bottom-of-screen EPG/playlist overlay for the current channel."""
        if not self.channels:
            return
        ch = self.channels[self.current_channel_idx]
        if ch.number == 0:
            return
        if self._epg_overlay_active and not getattr(self, '_epg_overlay_preview', False):
            self._hide_epg_overlay()
        else:
            # Not open, or open as a preview of another channel → show current channel's
            self._show_epg_overlay(ch)
    def _preview_channel_epg(self, ch_idx):
        """Show the schedule strip for another channel without tuning to it."""
        if not self.channels or ch_idx is None:
            return
        ch = self.channels[ch_idx]
        if ch.number == 0:
            return
        if ch_idx == self.current_channel_idx:
            self._show_epg_overlay(ch)
        else:
            self._show_epg_overlay(ch, preview_idx=ch_idx)
    def _show_epg_overlay(self, ch, preview_idx=None):
        """Build and show the EPG schedule strip across the bottom of the video.

        preview_idx: channel index when showing the guide for a channel other
        than the one currently playing (styled differently, with a TUNE button).
        """
        self._hide_epg_overlay()   # clean up any stale one
        preview = preview_idx is not None
        self._epg_overlay_active  = True
        self._epg_overlay_preview = preview
        if not preview:
            self.epg_btn.config(fg=self.AMBER, bg=self.AMBER_DIM)
        hdr_bg     = "#6a0060" if preview else "#0000aa"
        border_col = "#ff66ff" if preview else self.GUIDE_ACCENT
        overlay = tk.Frame(self.screen_frame, bg="#000000",
                           highlightthickness=2 if preview else 1,
                           highlightbackground=border_col)
        overlay.place(relx=0, rely=1.0, anchor="sw", relwidth=1.0, height=130)
        overlay.lift()
        self._epg_overlay_frame = overlay
        # Semi-transparent backing canvas
        bg_canvas = tk.Canvas(overlay, bg="#000011", highlightthickness=0)
        bg_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Header bar
        hdr = tk.Frame(overlay, bg=hdr_bg,
                       highlightthickness=0)
        hdr.place(relx=0, rely=0, relwidth=1, height=22)
        hdr_suffix = "PLAYLIST" if ch.is_local else "SCHEDULE"
        hdr_font = font.Font(family="Courier New", size=9, weight="bold")
        if preview:
            cur = self.channels[self.current_channel_idx]
            hdr_text = (f"  👁  PREVIEW  ·  CH {ch.number:02d}  —  {ch.name}  —  {hdr_suffix}"
                        f"   (not playing · now on CH {cur.number:02d})")
        else:
            hdr_text = f"  📺  CH {ch.number:02d}  —  {ch.name}  —  {hdr_suffix}"
        ch_label = tk.Label(hdr, text=hdr_text, font=hdr_font,
                            fg=self.GUIDE_YELLOW, bg=hdr_bg, anchor="w")
        ch_label.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=2)
        close_btn = tk.Label(hdr, text="  ✕  ", font=hdr_font,
                             fg=self.GUIDE_ACCENT, bg=hdr_bg, cursor="hand2", pady=2)
        close_btn.pack(side=tk.RIGHT)
        close_btn.bind("<Button-1>", lambda e: self._hide_epg_overlay())
        if preview:
            tune_btn = tk.Label(hdr, text=" ▶ TUNE ", font=hdr_font,
                                fg="#000000", bg=self.GUIDE_YELLOW, cursor="hand2")
            tune_btn.pack(side=tk.RIGHT, padx=(0, 4), pady=2)
            tune_btn.bind("<Button-1>", lambda e, i=preview_idx: self._tune_channel(i))
        # Programme cells canvas
        prog_canvas = tk.Canvas(overlay, bg="#000022", highlightthickness=0)
        prog_canvas.place(relx=0, rely=0, y=22, relwidth=1, relheight=1, height=-22)
        prog_canvas.bind("<Configure>", lambda e: self._draw_epg_overlay_canvas(prog_canvas, ch))
        self._epg_overlay_prog_canvas = prog_canvas
        self._epg_overlay_ch          = ch
        # Auto-refresh every 30 s
        self._epg_overlay_after_id = self.after(30000, self._refresh_epg_overlay)
    def _draw_epg_overlay_canvas(self, c, ch):
        """Render EPG programme blocks onto the overlay canvas."""
        c.delete("all")
        W = c.winfo_width()
        H = c.winfo_height()
        if W < 20 or H < 20:
            return

        # ── Local playlist: show now/next track cards ─────────────────────
        if ch.is_local:
            self._draw_local_overlay_canvas(c, ch, W, H)
            return

        # No EPG data cases
        if not self._epg_loaded:
            c.create_text(W // 2, H // 2, text="⌛  Loading schedule data...",
                          font=("Courier New", 9), fill=self.GUIDE_ACCENT, anchor="center")
            return
        if not ch.channel_id or ch.channel_id not in self._epg_data:
            label      = ch.description or "LIVE BROADCAST"
            labels     = [label, "CONTINUED"]
            fills      = ["#003070", "#004080"]
            borders    = ["#0044aa", "#0044aa"]
            pad        = 8
            cy0        = pad; cy1 = H - pad
            if cy1 - cy0 < 10:
                return
            mid_y      = (cy0 + cy1) // 2
            avail_w    = W - pad * 2
            min_half   = 40
            raw_mid    = W // 2
            mid_x      = max(pad + min_half, min(raw_mid, W - pad - min_half))
            segments   = [(pad, mid_x - pad // 2), (mid_x + pad // 2, W - pad)]
            # Fake time strings matching the two half-window slots
            now_local  = datetime.datetime.now()
            slot_mins  = self._EPG_SLOT_MINS
            half_delta = datetime.timedelta(minutes=slot_mins * self._EPG_SLOTS // 2)
            t1_start   = now_local.replace(second=0, microsecond=0) - \
                         datetime.timedelta(minutes=now_local.minute % slot_mins)
            t1_end     = t1_start + half_delta
            t2_end     = t1_end   + half_delta
            time_strs  = [
                f"{t1_start.strftime('%I:%M %p').lstrip('0')} – {t1_end.strftime('%I:%M %p').lstrip('0')}",
                f"{t1_end.strftime('%I:%M %p').lstrip('0')} – {t2_end.strftime('%I:%M %p').lstrip('0')}",
            ]
            for i, (cx0, cx1) in enumerate(segments):
                cw = cx1 - cx0
                if cw < 4:
                    continue
                c.create_rectangle(cx0, cy0, cx1, cy1,
                                   fill=fills[i], outline=borders[i], width=1)
                if cw > 30:
                    max_title = max(3, (cw - 16) // 6)
                    title_text = labels[i] if len(labels[i]) <= max_title else labels[i][:max_title - 1] + "…"
                    c.create_text(cx0 + 8, mid_y - 8, anchor="w",
                                  text=title_text, font=("Courier New", 9), fill="#ccddff")
                if cw > 70:
                    max_time = max(3, (cw - 16) // 5)
                    time_text = time_strs[i] if len(time_strs[i]) <= max_time else time_strs[i][:max_time]
                    c.create_text(cx0 + 8, mid_y + 8, anchor="w",
                                  text=time_text, font=("Courier New", 7), fill=self.GUIDE_GRAY)
            return
        progs = self._epg_data[ch.channel_id]
        win_start  = self._epg_window_start()
        total_secs = self._EPG_SLOTS * self._EPG_SLOT_MINS * 60
        win_end    = win_start + datetime.timedelta(seconds=total_secs)
        now_utc    = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        slot_w = max(60, W // self._EPG_SLOTS)
        total_slot_w = slot_w * self._EPG_SLOTS
        # Time ticks across the top of the overlay canvas
        tick_h = 16
        c.create_rectangle(0, 0, W, tick_h, fill="#0000aa", outline="")
        for slot_i in range(self._EPG_SLOTS):
            slot_time = win_start + datetime.timedelta(minutes=slot_i * self._EPG_SLOT_MINS)
            try:
                ts = slot_time.replace(tzinfo=datetime.timezone.utc).timestamp()
                local_slot = datetime.datetime(*_time.localtime(int(ts))[:6])
            except Exception:
                local_slot = slot_time
            time_str = local_slot.strftime('%I:%M %p').lstrip('0')
            tx = slot_i * slot_w
            c.create_line(tx, 0, tx, tick_h, fill="#0000dd", width=1)
            c.create_text(tx + 4, tick_h // 2, anchor="w", text=time_str,
                          font=("Courier New", 7, "bold"), fill=self.GUIDE_ACCENT)
        # Now-line
        elapsed_secs = (now_utc - win_start).total_seconds()
        if 0 <= elapsed_secs <= total_secs:
            now_x = int(elapsed_secs / total_secs * total_slot_w)
            c.create_line(now_x, tick_h, now_x, H, fill=self.GUIDE_RED, width=2, dash=(4, 3))
        cell_y0 = tick_h + 2
        cell_y1 = H - 2
        cell_colours = ["#003070", "#004080", "#003888", "#004470"]
        drawn_any = False
        for idx, prog in enumerate(progs):
            p_start = prog['start']
            p_stop  = prog.get('stop') or (p_start + datetime.timedelta(hours=1))
            if p_stop <= win_start or p_start >= win_end:
                continue
            drawn_any = True
            draw_start = max(p_start, win_start)
            draw_stop  = min(p_stop, win_end)
            x0 = int((draw_start - win_start).total_seconds() / total_secs * total_slot_w)
            x1 = int((draw_stop  - win_start).total_seconds() / total_secs * total_slot_w)
            x0 = max(0, x0); x1 = min(total_slot_w, x1)
            if x1 - x0 < 2:
                continue
            is_now     = (p_start <= now_utc < p_stop)
            cell_bg    = "#002255" if is_now else cell_colours[idx % len(cell_colours)]
            border_col = self.GUIDE_ACCENT if is_now else "#0044aa"
            c.create_rectangle(x0 + 1, cell_y0, x1 - 1, cell_y1,
                                fill=cell_bg, outline=border_col, width=1)
            # Progress bar
            if is_now:
                frac = _get_progress_fraction(prog)
                if frac is not None and frac > 0:
                    bar_x1 = x0 + 2 + int((x1 - x0 - 4) * frac)
                    c.create_rectangle(x0 + 2, cell_y1 - 5, bar_x1, cell_y1 - 2,
                                       fill=self.GUIDE_GREEN, outline="")
            cell_w = x1 - x0
            title  = _format_prog_title(prog)
            max_chars = max(3, (cell_w - 8) // 6)
            if len(title) > max_chars:
                title = title[:max_chars - 1] + "…"
            if cell_w > 24:
                title_col = self.GUIDE_WHITE if is_now else "#ccddff"
                mid_y = (cell_y0 + cell_y1) // 2
                c.create_text(x0 + 5, mid_y - 8, anchor="w",
                              text=title, font=("Courier New", 8), fill=title_col)
            if cell_w > 70:
                time_str = _format_time_range(prog)
                c.create_text(x0 + 5, (cell_y0 + cell_y1) // 2 + 6, anchor="w",
                              text=time_str, font=("Courier New", 7), fill=self.GUIDE_GRAY)
        if not drawn_any:
            c.create_text(W // 2, (cell_y0 + cell_y1) // 2,
                          text="No programmes found in current time window",
                          font=("Courier New", 8), fill="#445566", anchor="center")
    def _draw_local_overlay_canvas(self, c, ch, W, H):
        """Render a NOW / UP NEXT playlist strip for local channels."""
        now_playing = self._get_guide_now_playing(ch)
        up_next     = self._get_guide_up_next(ch)

        # Progress fraction through current track
        frac = None
        ch_idx = next((i for i, c2 in enumerate(self.channels) if c2 is ch), None)
        if (ch_idx is not None and ch_idx == self._last_local_channel_idx
                and not self._in_commercial):
            t_ms    = getattr(self, '_vlc_time_ms', 0)
            len_ms  = getattr(self, '_vlc_len_ms',  0)
            if len_ms and len_ms > 0 and t_ms >= 0:
                frac = max(0.0, min(1.0, t_ms / len_ms))

        pad   = 8
        mid_x = W // 2
        cy0   = pad
        cy1   = H - pad
        mid_y = (cy0 + cy1) // 2

        # ── NOW cell — EPG is_now colours ─────────────────────────────────
        now_x1 = mid_x - pad // 2
        c.create_rectangle(pad, cy0, now_x1, cy1,
                           fill="#002255", outline=self.GUIDE_ACCENT, width=1)
        c.create_text(pad + 10, cy0 + 10, anchor="w",
                      text="▶ NOW PLAYING", font=("Courier New", 7, "bold"),
                      fill=self.GUIDE_GRAY)
        max_now = max(4, (now_x1 - pad - 20) // 7)
        now_title = now_playing[:max_now] + ("…" if len(now_playing) > max_now else "")
        c.create_text(pad + 10, mid_y, anchor="w",
                      text=now_title, font=("Courier New", 10, "bold"),
                      fill=self.GUIDE_WHITE)
        # Track index label
        if (ch_idx is not None and ch_idx == self._last_local_channel_idx
                and self._local_track_order):
            track_n = self._local_track_idx + 1
            total_n = len(self._local_track_order)
            c.create_text(pad + 10, cy1 - 14, anchor="w",
                          text=f"Track {track_n} of {total_n}{'  (shuffle)' if ch.randomize else ''}",
                          font=("Courier New", 7), fill=self.GUIDE_GRAY)
        # Progress bar — GUIDE_GREEN, same as EPG
        if frac is not None and frac > 0:
            bar_x0 = pad + 2; bar_x1_max = now_x1 - 2
            bar_w  = bar_x1_max - bar_x0
            filled = bar_x0 + int(bar_w * frac)
            c.create_rectangle(bar_x0, cy1 - 6, bar_x1_max, cy1 - 3,
                               fill="#001133", outline="")
            c.create_rectangle(bar_x0, cy1 - 6, filled, cy1 - 3,
                               fill=self.GUIDE_GREEN, outline="")

        # ── UP NEXT cell — EPG non-now colours ────────────────────────────
        next_x0 = mid_x + pad // 2
        if up_next:
            c.create_rectangle(next_x0, cy0, W - pad, cy1,
                               fill="#003070", outline="#0044aa", width=1)
            c.create_text(next_x0 + 10, cy0 + 10, anchor="w",
                          text="NEXT ▶", font=("Courier New", 7, "bold"),
                          fill=self.GUIDE_GRAY)
            max_next = max(4, (W - pad - next_x0 - 20) // 7)
            next_title = up_next[:max_next] + ("…" if len(up_next) > max_next else "")
            c.create_text(next_x0 + 10, mid_y, anchor="w",
                          text=next_title, font=("Courier New", 10),
                          fill="#ccddff")
        else:
            c.create_rectangle(next_x0, cy0, W - pad, cy1,
                               fill="#00002a", outline="#000066", width=1)
            c.create_text(next_x0 + 10, mid_y, anchor="w",
                          text="—", font=("Courier New", 10), fill="#445566")

    def _refresh_epg_overlay(self):
        """Periodic refresh of the EPG overlay."""
        if not self._epg_overlay_active:
            return
        ch = getattr(self, '_epg_overlay_ch', None)
        pc = getattr(self, '_epg_overlay_prog_canvas', None)
        if ch and pc:
            try:
                self._draw_epg_overlay_canvas(pc, ch)
            except Exception:
                pass
        # Refresh more frequently for local playlists (track progress updates)
        interval = 2000 if (ch and ch.is_local) else 30000
        self._epg_overlay_after_id = self.after(interval, self._refresh_epg_overlay)
    def _hide_epg_overlay(self):
        """Tear down the EPG schedule overlay."""
        self._epg_overlay_active  = False
        self._epg_overlay_preview = False
        if self._epg_overlay_after_id:
            try:
                self.after_cancel(self._epg_overlay_after_id)
            except Exception:
                pass
            self._epg_overlay_after_id = None
        if self._epg_overlay_frame:
            try:
                self._epg_overlay_frame.destroy()
            except Exception:
                pass
            self._epg_overlay_frame = None
        self._epg_overlay_prog_canvas = None
        self._epg_overlay_ch          = None
        if hasattr(self, 'epg_btn'):
            self.epg_btn.config(fg=self.AMBER, bg="#2a1f10")
    # ══════════════════════════════════════════
    #  Fullscreen Mouse-Triggered Control Overlay
    #  Uses a separate Toplevel (alpha + topmost) so it always floats above
    #  hardware-accelerated VLC video surfaces which ignore normal z-order.
    # ══════════════════════════════════════════
    _FS_OVL_BG      = "#0f0f0f"
    _FS_OVL_BTN_BG  = "#1e1e1e"
    _FS_OVL_ALPHA   = 0.82        # 0 = transparent, 1 = opaque
    _FS_OVL_HIDE_MS = 3000        # auto-hide delay in ms

    # ── Mouse-position poll loop ──────────────────────────────────────────
    def _fs_start_mouse_poll(self):
        """Start the 100 ms cursor-position poll that drives overlay show/hide."""
        self._fs_stop_mouse_poll()
        self._fs_poll_id = self.after(100, self._fs_poll_mouse)

    def _fs_stop_mouse_poll(self):
        if self._fs_poll_id:
            try: self.after_cancel(self._fs_poll_id)
            except Exception: pass
            self._fs_poll_id = None
        self._fs_last_mouse_pos = None

    def _fs_poll_mouse(self):
        """Poll Win32 cursor position every 100 ms; show overlay on any movement."""
        if not self.is_fullscreen or self._closing:
            return
        try:
            import ctypes
            class _POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            pos = (pt.x, pt.y)
        except Exception:
            pos = None
        if pos and pos != self._fs_last_mouse_pos:
            self._fs_last_mouse_pos = pos
            self._fs_mouse_move()
        self._fs_poll_id = self.after(100, self._fs_poll_mouse)

    def _fs_mouse_move(self, event=None):
        """Called on any detected cursor movement while in fullscreen."""
        if not self.is_fullscreen or self._closing:
            return
        if not self._fs_overlay_visible:
            self._show_fs_overlay()
        else:
            self._fs_overlay_reset_timer()

    def _fs_overlay_reset_timer(self):
        """Reset (or start) the auto-hide countdown."""
        if self._fs_overlay_after_id:
            try: self.after_cancel(self._fs_overlay_after_id)
            except Exception: pass
        self._fs_overlay_after_id = self.after(self._FS_OVL_HIDE_MS, self._hide_fs_overlay)

    def _fs_overlay_refocus(self):
        """Give keyboard focus back to the main window after overlay interaction."""
        # Only meaningful while still in fullscreen.  The overlay's ⛶ button exits
        # fullscreen and this is scheduled 30 ms *after* that, so without this
        # guard the restored window force-grabs the foreground away from whatever
        # the user clicked next — other apps appear unable to come to the front.
        if not self.is_fullscreen or self._closing:
            return
        try:
            self.focus_force()
        except Exception:
            pass

    def _fs_overlay_btn(self, parent, text, cmd, side=tk.LEFT, padx=(4, 4)):
        """Styled button for the fullscreen overlay."""
        def _cmd():
            # Defer the real action to the next event-loop tick so the button's
            # own Tcl/Tk event handler has fully returned before we potentially
            # destroy the overlay Toplevel (e.g. via _hide_fs_overlay called from
            # _tune_channel).  Destroying a widget synchronously inside its own
            # command callback is undefined behaviour and hard-crashes on Windows.
            self.after(0, _deferred)
        def _deferred():
            cmd()
            if self._fs_overlay_visible:
                self._fs_overlay_reset_timer()
            self.after(30, self._fs_overlay_refocus)
        btn = tk.Button(
            parent, text=text, command=_cmd,
            bg=self._FS_OVL_BTN_BG, fg=self.AMBER,
            activebackground="#383838", activeforeground=self.AMBER,
            font=("Courier New", 12, "bold"),
            relief=tk.FLAT, bd=0, padx=10, pady=5,
            cursor="hand2", highlightthickness=0,
        )
        btn.pack(side=side, padx=padx)
        btn.bind("<Enter>",  lambda e: btn.config(bg="#333333"))
        btn.bind("<Leave>",  lambda e: btn.config(bg=self._FS_OVL_BTN_BG))
        btn.bind("<Motion>", lambda e: self._fs_overlay_reset_timer())
        return btn

    def _show_fs_overlay(self):
        """Build and display the fullscreen control bar as a floating Toplevel.

        A Toplevel with overrideredirect + topmost + alpha is used instead of
        a child Frame because VLC's hardware-accelerated D3D/GPU surface
        bypasses normal Win32 z-order and would cover a regular child widget.
        """
        if not self.is_fullscreen or self._closing:
            return
        self._hide_fs_overlay()          # tear down any stale one first
        self._fs_overlay_visible = True

        ch       = self.channels[self.current_channel_idx] if self.channels else None
        is_local = bool(ch and ch.is_local and not self._in_commercial)
        bar_h    = 80 if is_local else 60

        # Use the monitor rect saved at FS-enter time — winfo_* can be stale
        # because the resize hasn't committed to the OS yet when the first
        # mouse-move fires.
        if self._fs_monitor_rect:
            mx, my, mw, mh = self._fs_monitor_rect
        else:
            mx, my = self.winfo_x(), self.winfo_y()
            mw, mh = self.winfo_screenwidth(), self.winfo_screenheight()
        bar_x = mx
        bar_y = my + mh - bar_h

        # ── Floating Toplevel ─────────────────────────────────────────────
        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.geometry(f"{mw}x{bar_h}+{bar_x}+{bar_y}")
        overlay.config(bg=self._FS_OVL_BG)
        try:
            overlay.attributes('-alpha',   self._FS_OVL_ALPHA)
            overlay.attributes('-topmost', True)
        except Exception:
            pass
        self._fs_overlay_frame = overlay
        # Any motion on the overlay itself resets the hide timer
        overlay.bind("<Motion>", lambda e: self._fs_overlay_reset_timer())
        # Clicks on blank areas of the overlay return focus to the main window
        overlay.bind("<Button-1>", lambda e: self.after(30, self._fs_overlay_refocus))

        ch_name = ch.name if ch else ""
        ch_num  = f"CH {ch.number:02d}" if ch else ""

        # ── Shared helper: build the guide/exit buttons, mute + volume slider ──
        def _add_vol_controls(parent):
            self._fs_overlay_btn(parent, "⛶", self._exit_fullscreen,
                                 side=tk.RIGHT, padx=(6, 4))
            self._fs_overlay_btn(parent, "📋", self._toggle_epg_overlay,
                                 side=tk.RIGHT, padx=(2, 2))
            muted = getattr(self, '_muted', False)
            mute_icon = "🔇" if muted else "🔊"
            mute_btn = self._fs_overlay_btn(parent, mute_icon, self._toggle_mute,
                                            side=tk.RIGHT, padx=(4, 4))
            self._fs_mute_btn = mute_btn
            vol_slider = ttk.Scale(
                parent, from_=0, to=100,
                variable=self.vol_var,           # shared IntVar — already synced
                orient=tk.HORIZONTAL,
                command=self._fs_overlay_vol_moved,
                length=130,
            )
            vol_slider.pack(side=tk.RIGHT, padx=(0, 2))
            vol_slider.bind("<Motion>",          lambda e: self._fs_overlay_reset_timer())
            vol_slider.bind("<ButtonRelease-1>", lambda e: self.after(30, self._fs_overlay_refocus))
            tk.Label(parent, text="VOL", font=("Courier New", 9),
                     fg="#666666", bg=self._FS_OVL_BG).pack(side=tk.RIGHT, padx=(10, 2))

        if is_local:
            # ── Row 1: seek / position slider ─────────────────────────────
            row1 = tk.Frame(overlay, bg=self._FS_OVL_BG)
            row1.pack(fill=tk.X, padx=14, pady=(7, 0))
            tk.Label(
                row1, text="POS",
                font=("Courier New", 9), fg="#666666", bg=self._FS_OVL_BG,
            ).pack(side=tk.LEFT, padx=(0, 6))
            seek = ttk.Scale(
                row1, from_=0.0, to=1.0,
                variable=self.playhead_var,      # shared DoubleVar — auto-synced by poll loop
                orient=tk.HORIZONTAL,
                command=self._fs_overlay_seek_moved,
            )
            seek.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
            seek.bind("<ButtonPress-1>",   self._fs_overlay_seek_press)
            seek.bind("<ButtonRelease-1>", self._fs_overlay_seek_release)
            seek.bind("<Motion>",          lambda e: self._fs_overlay_reset_timer())
            self._fs_overlay_seek_slider = seek

            # ── Row 2: transport buttons  |  channel name  |  VOL ─────────
            row2 = tk.Frame(overlay, bg=self._FS_OVL_BG)
            row2.pack(fill=tk.X, padx=10, pady=(2, 6))

            self._fs_overlay_btn(row2, "◀◀ CH",  self._prev_channel,       side=tk.LEFT,  padx=(0,  2))
            self._fs_overlay_btn(row2, "CH ▶▶",  self._next_channel,       side=tk.LEFT,  padx=(2, 14))
            self._fs_overlay_btn(row2, "⏮",      self._prev_track,         side=tk.LEFT,  padx=(2,  2))
            state   = getattr(self, '_vlc_state', vlc.State.NothingSpecial)
            pp_text = "▶" if state == vlc.State.Paused else "⏸"
            self._fs_pp_btn = self._fs_overlay_btn(row2, pp_text, self._fs_overlay_play_pause,
                                                   side=tk.LEFT, padx=(2, 2))
            self._fs_overlay_btn(row2, "⏭",      self._next_track,         side=tk.LEFT,  padx=(2,  2))
            # Volume controls on the right; channel name fills the middle
            _add_vol_controls(row2)
            tk.Label(
                row2, text=f"  {ch_num}  {ch_name}",
                font=("Courier New", 11, "bold"), fg=self.AMBER, bg=self._FS_OVL_BG,
                anchor='center',
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        else:
            # ── Single row: ◀◀  channel name  ▶▶  |  VOL ────────────────
            row = tk.Frame(overlay, bg=self._FS_OVL_BG)
            row.pack(fill=tk.BOTH, expand=True, padx=10)
            self._fs_overlay_btn(row, "◀◀",  self._prev_channel, side=tk.LEFT,  padx=(0, 6))
            tk.Label(
                row, text=f"  {ch_num}  {ch_name}",
                font=("Courier New", 14, "bold"), fg=self.AMBER, bg=self._FS_OVL_BG,
                anchor='center',
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._fs_overlay_btn(row, "▶▶",  self._next_channel, side=tk.LEFT, padx=(6, 16))
            # Volume controls pinned to the right
            _add_vol_controls(row)

        self._fs_overlay_reset_timer()

    def _fs_overlay_play_pause(self):
        """Play/pause action from the fullscreen overlay button."""
        self._local_play_pause()
        # Flip the button label optimistically — will be corrected on next poll tick
        if self._fs_pp_btn:
            try:
                state   = getattr(self, '_vlc_state', vlc.State.NothingSpecial)
                pp_text = "▶" if state == vlc.State.Playing else "⏸"
                self._fs_pp_btn.config(text=pp_text)
            except Exception:
                pass

    def _fs_overlay_seek_press(self, event):
        self._fs_overlay_seek_dragging = True
        self._playhead_dragging = True
        frac = self._scale_frac_at(event.widget, event)
        if frac is not None:
            self._playhead_seek_frac = frac
            self.playhead_var.set(frac)

    def _fs_overlay_seek_release(self, event):
        self._fs_overlay_seek_dragging = False
        self._playhead_dragging = False
        frac = self._scale_frac_at(event.widget, event)
        if frac is None:
            frac = self.playhead_var.get()
        self._seek_to_frac(frac)
        self._playhead_seek_frac = None
        self._fs_overlay_reset_timer()
        self.after(30, self._fs_overlay_refocus)

    def _fs_overlay_seek_moved(self, val):
        if self._fs_overlay_seek_dragging:
            self._playhead_seek_frac = float(val)
        self._fs_overlay_reset_timer()

    def _fs_overlay_vol_moved(self, val):
        """Volume slider in the FS overlay moved — apply immediately."""
        self._on_volume(val)
        self._fs_overlay_reset_timer()

    def _hide_fs_overlay(self):
        """Destroy the fullscreen control overlay Toplevel."""
        self._fs_overlay_visible = False
        if self._fs_overlay_after_id:
            try: self.after_cancel(self._fs_overlay_after_id)
            except Exception: pass
            self._fs_overlay_after_id = None
        if self._fs_overlay_frame:
            try: self._fs_overlay_frame.destroy()
            except Exception: pass
            self._fs_overlay_frame = None
        self._fs_overlay_seek_slider = None
        self._fs_pp_btn  = None
        self._fs_mute_btn = None

    # ══════════════════════════════════════════
    #  Window / Taskbar Icon
    # ══════════════════════════════════════════
    def _set_window_icon(self):
        icon_ico = os.path.join(self.base_dir, "retrotv.ico")
        icon_png = os.path.join(self.base_dir, "retrotv.png")
        self._icon_ico_path = icon_ico if os.path.exists(icon_ico) else None
        self._icon_photo    = None
        try:
            if self._icon_ico_path:
                self.wm_iconbitmap(self._icon_ico_path)
            elif os.path.exists(icon_png):
                photo = tk.PhotoImage(file=icon_png)
                self.wm_iconphoto(True, photo)
                self._icon_photo = photo
        except Exception as e:
            print(f"[icon] tkinter icon error: {e}")

    def _apply_icon(self, win):
        try:
            if getattr(self, '_icon_ico_path', None):
                win.wm_iconbitmap(self._icon_ico_path)
            elif getattr(self, '_icon_photo', None):
                win.wm_iconphoto(False, self._icon_photo)
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                import ctypes.wintypes as wt
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("N5Networks.RetroTV")
                WM_SETICON = 0x0080; ICON_SMALL = 0; ICON_BIG = 1; IMAGE_ICON = 1; LR_LOADFROMFILE = 0x0010
                user32 = ctypes.windll.user32; hwnd = self.winfo_id()
                ico_path = self._icon_ico_path
                if ico_path:
                    hicon_big   = user32.LoadImageW(None, ico_path, IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
                    hicon_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
                    if hicon_big:   user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                    if hicon_small: user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            except Exception as e:
                print(f"[icon] Windows taskbar icon error: {e}")
    # ══════════════════════════════════════════
    #  UI Helpers & Controls
    # ══════════════════════════════════════════
    def _update_local_controls(self):
        if getattr(self, '_dvr_playing_path', None):
            self.filename_row.pack_forget()
            self.local_row.pack(fill=tk.X, padx=10, pady=(0, 4), before=self.ch_strip)
            self.btn_prev_track.config(state=tk.DISABLED)
            self.btn_next_track.config(state=tk.DISABLED)
            self.btn_play_pause.config(state=tk.NORMAL)
            self._dvr_exit_btn.pack(side=tk.RIGHT, padx=(4, 2))
            return
        self._dvr_exit_btn.pack_forget()
        if not self.channels:
            self.local_row.pack_forget()
            self.filename_row.pack_forget()
            return
        ch = self.channels[self.current_channel_idx]
        if ch.number == 0 or not ch.is_local:
            self.local_row.pack_forget()
            self.filename_row.pack_forget()
        else:
            self.filename_row.pack(fill=tk.X, padx=10, pady=(0, 0), before=self.ch_strip)
            self.local_row.pack(fill=tk.X, padx=10, pady=(0, 4), before=self.ch_strip)
            self.btn_prev_track.config(state=tk.NORMAL)
            self.btn_next_track.config(state=tk.NORMAL)
            self.btn_play_pause.config(state=tk.NORMAL)
        # EPG guide button: shown for live channels and local playlists (not guide ch)
        if hasattr(self, 'epg_btn'):
            if ch.number != 0:
                self.epg_btn.config(state=tk.NORMAL)
            else:
                self.epg_btn.config(state=tk.DISABLED)
    def _prev_track(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not ch.is_local: return
        if self._tuning: return
        # Reset retry state on user action
        self._local_retry_count = 0
        self._is_retrying = False
        if self._retry_job_id:
            try:
                self.after_cancel(self._retry_job_id)
            except Exception:
                pass
            self._retry_job_id = None

        self._in_commercial = False; self._commercial_queue = []; self._post_commercial_fn = None
        if not self._local_track_order:
            self._build_track_order(ch)
            self._local_track_idx = 0
        prev_idx = self._local_track_idx - 1
        if prev_idx < 0: prev_idx = max(0, len(self._local_track_order) - 1)
        self._local_track_idx = prev_idx
        self._skip_to_track(ch, prev_idx)
    def _next_track(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not ch.is_local: return
        if self._tuning: return
        
        # Reset retry state on user action
        self._local_retry_count = 0
        self._is_retrying = False
        if self._retry_job_id:
            try:
                self.after_cancel(self._retry_job_id)
            except Exception:
                pass
            self._retry_job_id = None

        self._in_commercial = False; self._commercial_queue = []; self._post_commercial_fn = None
        if not self._local_track_order:
            self._build_track_order(ch)
            self._local_track_idx = 0
            self._skip_to_track(ch, 0)
            return
        next_idx = self._local_track_idx + 1
        if next_idx >= len(self._local_track_order):
            avoid = self._local_track_order[self._local_track_idx]
            self._build_track_order(ch, avoid_first=avoid)
            next_idx = 0
        self._local_track_idx = next_idx
        self._skip_to_track(ch, next_idx)
    def _local_play_pause(self):
        if not self.player: return
        state = getattr(self, '_vlc_state', vlc.State.NothingSpecial)
        if state == vlc.State.Paused:
            self._vlc_cmd('pause')
            self.btn_play_pause.config(text="⏸ PAUSE")
        elif state == vlc.State.Playing:
            self._vlc_cmd('pause')
            self.btn_play_pause.config(text="▶ PLAY")
    def _space_play_pause(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if ch.is_local:
            self._local_play_pause()
    def _skip_to_track(self, ch, track_idx):
        if not self.player: return
        if not self._local_track_order or track_idx >= len(self._local_track_order): return
        actual_idx = self._local_track_order[track_idx]
        if actual_idx >= len(ch.playlist): return
        file_path  = ch.playlist[actual_idx]
        vol = self.vol_var.get()
        self.playhead_var.set(0.0)
        self._vlc_cmd('play_media', file_path, 0, vol)
        self._update_filename_label()
    def _skip_to_track_by_ch_idx(self, ch_idx, track_idx):
        if not self.channels or ch_idx >= len(self.channels): return
        if ch_idx != self.current_channel_idx: return
        self._skip_to_track(self.channels[ch_idx], track_idx)
    def _update_filename_label(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not ch.is_local: return
        if not self._local_track_order: return
        if self._in_commercial:
            self.filename_label.config(text="— COMMERCIAL BREAK —", fg=self.AMBER)
            return
        if self._local_track_idx >= len(self._local_track_order): return
        actual_idx = self._local_track_order[self._local_track_idx]
        if actual_idx >= len(ch.playlist): return
        name = os.path.splitext(os.path.basename(ch.playlist[actual_idx]))[0]
        self.filename_label.config(text=name, fg=self.AMBER)
    @staticmethod
    def _scale_frac_at(widget, event):
        """Map a mouse event to the 0..1 value of a ttk.Scale.

        ttk.Scale.get(x, y) accounts for the thumb width and trough insets, so
        the result matches where the thumb is drawn.  Dividing event.x by the
        widget width puts clicks off target, worst near the ends.
        """
        try:
            return max(0.0, min(1.0, float(widget.get(event.x, event.y))))
        except Exception:
            w = widget.winfo_width()
            return max(0.0, min(1.0, event.x / w)) if w > 0 else None
    def _seek_to_frac(self, frac):
        """Seek to frac and keep the poll loop from snapping the playhead back
        to the old position while VLC is still finishing the seek."""
        self.playhead_var.set(frac)
        self._seek_hold_until = _time.monotonic() + 1.5
        if self.player:
            self._vlc_cmd('seek_frac', frac)
    def _playhead_hold(self):
        return self._playhead_dragging or _time.monotonic() < getattr(self, '_seek_hold_until', 0.0)
    def _playhead_press(self, event):
        self._playhead_dragging = True
        frac = self._scale_frac_at(self.playhead_slider, event)
        self._playhead_seek_frac = frac
        if frac is not None:
            self.playhead_var.set(frac)
    def _playhead_release(self, event):
        self._playhead_dragging = False
        frac = self._scale_frac_at(self.playhead_slider, event)
        if frac is None:
            frac = self._playhead_seek_frac or self.playhead_var.get()
        self._seek_to_frac(frac)
        self._playhead_seek_frac = None
    def _on_playhead_moved(self, val):
        if self._playhead_dragging:
            self._playhead_seek_frac = float(val)
    def _start_commercial_break(self, after_fn):
        if not self._commercials:
            after_fn()
            return
        count = random.randint(
            int(self._settings.get('commercials_min', 2)),
            int(self._settings.get('commercials_max', 4))
        )
        pool = self._commercials[:]
        random.shuffle(pool)
        if len(pool) < count:
            extra = self._commercials[:]
            random.shuffle(extra)
            pool += extra
        self._commercial_queue = pool[:count]
        self._post_commercial_fn = after_fn
        # Snapshot playhead time so _save_local_position can record where
        # the real content was when the break started (not the ad position).
        self._pre_commercial_time_ms = max(0, getattr(self, '_vlc_time_ms', 0))
        self._in_commercial = True
        self._play_next_commercial()
    def _play_next_commercial(self):
        if not self._commercial_queue:
            self._in_commercial = False
            fn = self._post_commercial_fn
            self._post_commercial_fn = None
            self._update_filename_label()
            # Guard _advancing so the poll loop can't double-advance while VLC
            # transitions from the last commercial's Ended state to Playing.
            self._advancing = True
            if fn: fn()
            return
        spot = self._commercial_queue.pop(0)
        vol  = self.vol_var.get()
        self._vlc_cmd('play_media', spot, 0, vol)
    # ══════════════════════════════════════════
    #  Windows double-click VLC subclass
    # ══════════════════════════════════════════
    def _install_vlc_dblclick_hook(self):
        if sys.platform != 'win32' or not self.player:
            return
        try:
            import ctypes.wintypes as wt
            user32 = ctypes.windll.user32
            parent_hwnd = self.video_frame.winfo_id()
            found = []
            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def _enum_cb(child_hwnd, lparam):
                found.append(child_hwnd)
                return True
            user32.EnumChildWindows(parent_hwnd, EnumProc(_enum_cb), 0)
            if not found:
                self.after(400, self._install_vlc_dblclick_hook)
                return
            if getattr(self, '_vlc_hooked_hwnds', None) == found:
                return
            self._uninstall_vlc_dblclick_hook()
            WM_LBUTTONDOWN   = 0x0201
            WM_LBUTTONDBLCLK = 0x0203
            GWLP_WNDPROC     = -4
            dblclick_ms      = user32.GetDoubleClickTime()
            user32.GetWindowLongPtrW.restype  = ctypes.c_ssize_t
            user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype  = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
            user32.CallWindowProcW.restype    = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes   = [ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t]
            WndProcType = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_ssize_t)
            monotonic = _time.monotonic
            # Shared across all hooked windows — tracks last WM_LBUTTONDOWN time.
            # Needed because VLC windows often lack CS_DBLCLKS, so the OS never
            # generates WM_LBUTTONDBLCLK; we synthesise it from two rapid downs.
            _last_down_ms = [0]
            self._vlc_hooked_hwnds = []
            self._vlc_orig_procs   = {}
            self._vlc_wndproc_cbs  = []
            self._win32_user32     = user32
            def _make_proc(orig_proc):
                # THREAD SAFETY: callbacks run on VLC's rendering thread, not
                # the Tkinter main thread. self.after() is NOT safe here.
                # Route events through _media_key_queue (plain queue.Queue).
                def _proc(hwnd, msg, wparam, lparam):
                    if msg == WM_LBUTTONDBLCLK:
                        q = getattr(self, '_media_key_queue', None)
                        if q is not None:
                            try: q.put_nowait(('dblclick', 0))
                            except Exception: pass
                        return 0
                    if msg == WM_LBUTTONDOWN:
                        now_ms = int(monotonic() * 1000)
                        if now_ms - _last_down_ms[0] <= dblclick_ms:
                            q = getattr(self, '_media_key_queue', None)
                            if q is not None:
                                try: q.put_nowait(('dblclick', 0))
                                except Exception: pass
                            _last_down_ms[0] = 0
                        else:
                            _last_down_ms[0] = now_ms
                    return user32.CallWindowProcW(orig_proc, hwnd, msg, wparam, lparam)
                return WndProcType(_proc)
            for hwnd in found:
                orig = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)
                cb   = _make_proc(orig)
                self._vlc_wndproc_cbs.append(cb)   # pin to prevent GC
                self._vlc_orig_procs[hwnd] = orig
                self._vlc_hooked_hwnds.append(hwnd)
                user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                    ctypes.cast(cb, ctypes.c_void_p).value)
        except Exception as exc:
            print(f"[dblclick] hook failed: {exc}")
    def _uninstall_vlc_dblclick_hook(self):
        user32 = getattr(self, '_win32_user32', None)
        # Multi-window path (current)
        hwnds = getattr(self, '_vlc_hooked_hwnds', None)
        procs = getattr(self, '_vlc_orig_procs', {})
        if hwnds and user32:
            for hwnd in hwnds:
                orig = procs.get(hwnd)
                if orig:
                    try: user32.SetWindowLongPtrW(hwnd, -4, orig)
                    except Exception: pass
        self._vlc_hooked_hwnds = []
        self._vlc_orig_procs   = {}
        self._vlc_wndproc_cbs  = []
        # Legacy single-window path (old state that may exist at startup)
        legacy = getattr(self, '_vlc_hooked_hwnd', None)
        if legacy and user32:
            try: user32.SetWindowLongPtrW(legacy, -4, getattr(self, '_vlc_orig_proc', 0))
            except Exception: pass
            self._vlc_hooked_hwnd = None
    def _toggle_sidebar_collapse(self):
        if self._sidebar_collapsed:
            self._sidebar_collapsed = False
            self.sidebar_content.pack(fill=tk.BOTH, expand=True)
            self.sidebar.configure(width=self._sidebar_width)
            self._resize_sash.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
            self.collapse_btn.config(text="◀ HIDE")
        else:
            self._sidebar_collapsed = True
            self.sidebar_content.pack_forget()
            self._resize_sash.pack_forget()
            self.sidebar.configure(width=30)
            self.collapse_btn.config(text="▶")
    def _update_knob_label(self):
        if self.channels:
            ch = self.channels[self.current_channel_idx]
            self.knob_ch_label.config(text=f"CH {ch.number:02d}")
        else:
            self.knob_ch_label.config(text="CH --")
    def _make_btn(self, parent, text, cmd, side=tk.LEFT, padx=(2, 2), tooltip=None):
        btn = tk.Button(parent, text=text, command=cmd, bg="#2a1f10", fg=self.AMBER,
            activebackground=self.AMBER_DIM, activeforeground=self.AMBER, font=self.font_small, relief=tk.FLAT,
            padx=6, pady=3, bd=0, cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM)
        btn.pack(side=side, padx=padx)
        btn.bind("<Enter>", lambda e: btn.config(bg=self.AMBER_DIM))
        btn.bind("<Leave>", lambda e: btn.config(bg="#2a1f10"))
        if tooltip:
            self._attach_tooltip(btn, tooltip)
        return btn
    def _attach_tooltip(self, widget, text):
        """Attach a themed hover tooltip to any widget."""
        _after_id = [None]
        _tip_win  = [None]
        def _show():
            if _tip_win[0] or not text:
                return
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            w = tk.Toplevel(widget)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=text, font=self.font_small,
                     bg="#0d0a06", fg=self.AMBER, relief=tk.FLAT, bd=0,
                     padx=7, pady=4,
                     highlightthickness=1, highlightbackground=self.AMBER_DIM
                     ).pack()
            w.geometry(f"+{x}+{y}")
            _tip_win[0] = w
        def _enter(e):
            _after_id[0] = widget.after(600, _show)
        def _leave(e):
            if _after_id[0]:
                try: widget.after_cancel(_after_id[0])
                except Exception: pass
                _after_id[0] = None
            if _tip_win[0]:
                try: _tip_win[0].destroy()
                except Exception: pass
                _tip_win[0] = None
        widget.bind("<Enter>", _enter, add='+')
        widget.bind("<Leave>", _leave, add='+')
    def _style_sliders(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Horizontal.TScale", background="#1c1308", troughcolor="#0d0a06", sliderlength=16, sliderrelief='flat')
    def _draw_scanlines(self):
        if self._scanline_after_id is not None:
            self.after_cancel(self._scanline_after_id)
            self._scanline_after_id = None
        self.scanline_canvas.delete("scanline")
        h = self.scanline_canvas.winfo_height(); w = self.scanline_canvas.winfo_width()
        if h < 10:
            self._scanline_after_id = self.after(500, self._draw_scanlines)
            return
        for y in range(0, h, 4):
            self.scanline_canvas.create_line(0, y, w, y, fill="#ffffff", stipple="gray12", tags="scanline")
        self._scanline_after_id = self.after(2000, self._draw_scanlines)
    def _save_local_position(self, ch):
        if not ch.is_local or not self._local_track_order: return
        if self._local_track_idx >= len(self._local_track_order): return
        # During a commercial, use the saved pre-commercial position if available
        if self._in_commercial:
            timestamp_ms = getattr(self, '_pre_commercial_time_ms', 0)
        else:
            timestamp_ms = max(0, getattr(self, '_vlc_time_ms', 0))
        actual_idx = self._local_track_order[self._local_track_idx]
        if actual_idx >= len(ch.playlist): return
        filename = ch.playlist[actual_idx]
        self._resume_state[ch.name] = {"filename": filename, "timestamp_ms": timestamp_ms}
        _save_resume_state(self._resume_state)
    def _get_resume_position(self, ch):
        saved = self._resume_state.get(ch.name)
        if not saved: return 0, 0
        timestamp_ms = saved.get("timestamp_ms", 0); filename = saved.get("filename", "")
        if not filename: return 0, 0
        for order_pos, actual_idx in enumerate(self._local_track_order):
            if ch.playlist[actual_idx] == filename: return order_pos, timestamp_ms
        return 0, 0
    def _build_track_order(self, ch, avoid_first=None):
        indices = list(range(len(ch.playlist)))
        if ch.randomize:
            random.shuffle(indices)
            if (avoid_first is not None and len(indices) > 1 and indices[0] == avoid_first):
                swap_pos = random.randint(1, len(indices) - 1)
                indices[0], indices[swap_pos] = indices[swap_pos], indices[0]
        self._local_track_order = indices
        self._channel_track_orders[ch.name] = indices[:]
    def _clear_tuning(self):
        self._tuning_clear_after = None
        self._tuning = False
    # ══════════════════════════════════════════
    #  Channel tuning
    # ══════════════════════════════════════════
    def _tune_channel(self, idx, static=True):
        if not self.channels: return
        # FIX 1: replace boolean latch with monotonic timestamp debounce
        now = _time.monotonic()
        if now - self._last_tune_start < 0.35:
            return
        self._last_tune_start = now
        # Stamp this tune so stale static-animation callbacks can detect
        # they belong to a superseded tune and bail out.
        tune_stamp = now
        self._current_tune_stamp = tune_stamp
        
        # Cancel any pending retries from the previous channel
        if self._retry_job_id:
            try:
                self.after_cancel(self._retry_job_id)
            except Exception:
                pass
            self._retry_job_id = None
        self._local_retry_count = 0
        self._is_retrying = False
        
        if getattr(self, '_tuning_clear_after', None):
            self.after_cancel(self._tuning_clear_after)
            self._tuning_clear_after = None
        self._tuning = True
        self._advancing = False
        self._live_status_hide()
        if self._guide_active: self._hide_guide()
        self._in_commercial = False; self._commercial_queue = []; self._post_commercial_fn = None
        # Hide per-channel EPG overlay when changing channels
        if self._epg_overlay_active:
            self._hide_epg_overlay()
        # Dismiss fullscreen overlay (it will re-appear on next mouse move)
        self._hide_fs_overlay()
        # Remove the Win32 wndproc hook before VLC transitions media so the
        # hook is never called on an HWND that VLC is in the process of
        # destroying.  _play_local_track / _play_url reschedule the install.
        self._uninstall_vlc_dblclick_hook()
        new_idx = idx % len(self.channels)
        if self.channels:
            leaving_ch = self.channels[self.current_channel_idx]
            player_active = getattr(self, '_vlc_state', vlc.State.NothingSpecial) not in (
                vlc.State.NothingSpecial, vlc.State.Stopped)
            if leaving_ch.is_local and new_idx != self.current_channel_idx and player_active:
                self._save_local_position(leaving_ch)
                self._vlc_cmd('pause')
        self.current_channel_idx = new_idx
        self._local_track_idx = 0; self._local_track_order = []
        self._last_local_channel_idx = new_idx
        self._dvr_playing_path = None
        ch = self.channels[self.current_channel_idx]
        if ch.number != 0:
            self._add_to_recently_watched(ch.name)
        self.ch_listbox.selection_clear(0, tk.END)
        for row, ci in enumerate(self._listbox_ch_indices):
            if ci == self.current_channel_idx:
                self.ch_listbox.selection_set(row); self.ch_listbox.see(row); break
        self._update_channel_strip(); self._update_knob_label()
        self._update_fav_btn(); self._update_watermark()
        if ch.number == 0: self.live_label.config(text="▶ GUIDE")
        elif ch.is_local: self.live_label.config(text=f"▶ {'VHS RANDOM' if ch.randomize else 'VHS'}")
        else: self.live_label.config(text="▶ LIVE"); self.filename_row.pack_forget()
        self._update_local_controls()
        if hasattr(self, 'btn_play_pause'):
            self.btn_play_pause.config(text="⏸ PAUSE")
        # Show channel name OSD — delayed to appear after static clears
        osd_text = f"CH {ch.number:02d}  {ch.name}" if ch.number != 0 else "CH 00  GUIDE"
        self.after(420, lambda t=osd_text: self._show_osd(t))
        if static: self._show_static(duration_ms=400, then=lambda: self._start_channel_playback(tune_stamp))
        else: self._start_channel_playback(tune_stamp)
    def _start_channel_playback(self, tune_stamp=None):
        # FIX 1: discard stale callbacks from superseded tunes
        if tune_stamp is not None and tune_stamp != getattr(self, '_current_tune_stamp', None):
            return
        ch = self.channels[self.current_channel_idx]
        self.scanline_canvas.itemconfigure(self._nosignal_window, state='hidden')
        self.scanline_canvas.itemconfigure(self._offline_window,  state='hidden')
        self._live_error_count = 0; self._live_tune_time = _time.monotonic()
        self._live_last_time_ms = -1
        self._live_frozen_since = 0.0
        self._live_reconnecting = False
        self._live_reconnect_deadline = 0.0
        # Always restore normal label style when tuning — clears any red offline state
        self.time_label.config(fg=self.AMBER_DIM, font=self.font_small)
        if ch.number == 0:
            if self.player: self._vlc_cmd('stop')
            self._show_guide()
            self._tuning = False
            return
        if ch.is_local:
            if ch.name in self._channel_track_orders:
                self._local_track_order = self._channel_track_orders[ch.name][:]
            else:
                self._build_track_order(ch)
            track_idx, timestamp_ms = self._get_resume_position(ch)
            track_idx = max(0, min(track_idx, len(self._local_track_order) - 1))
            self._local_track_idx = track_idx
            self._play_local_track(ch, track_idx, seek_ms=timestamp_ms)
        elif ch.url:
            self._play_url(ch.url)
        else:
            w = self.scanline_canvas.winfo_width(); h = self.scanline_canvas.winfo_height()
            self.scanline_canvas.coords(self._nosignal_window, max(w // 2, 10), max(h // 2, 10))
            self.scanline_canvas.itemconfigure(self._nosignal_window, state='normal')
            self._tuning = False
    def _play_local_track(self, ch, track_idx, seek_ms=0):
        if not self.player: self._tuning = False; return
        if not self._local_track_order or track_idx >= len(self._local_track_order):
            self._tuning = False; return
        actual_idx = self._local_track_order[track_idx]
        if not (0 <= actual_idx < len(ch.playlist)): self._tuning = False; return
        file_path = ch.playlist[actual_idx]
        vol = self.vol_var.get()
        self.playhead_var.set(0.0)
        # Store seek time for retries
        self._last_local_seek_ms = seek_ms
        self._vlc_cmd('play_media', file_path, seek_ms, vol)
        # Don't eagerly delete the resume entry here — if VLC fails to seek,
        # we still want the position recorded. It will be overwritten on the
        # next successful _poll_vlc tick or cleared when the track finishes.
        self._advancing = False; self._update_filename_label()
        self._tuning_clear_after = self.after(600, self._clear_tuning)
        self.after(800, self._install_vlc_dblclick_hook)
    def _advance_local_track(self):
        if not self.channels: return
        ch_idx = self.current_channel_idx; ch = self.channels[ch_idx]
        if not ch.is_local: return
        if self._tuning: return
        
        # Reset retry state since we are advancing
        self._local_retry_count = 0
        self._is_retrying = False
        
        if not self._local_track_order:
            self._build_track_order(ch); self._local_track_idx = 0
        next_idx = self._local_track_idx + 1
        if next_idx >= len(self._local_track_order):
            avoid = self._local_track_order[self._local_track_idx]
            self._build_track_order(ch, avoid_first=avoid); next_idx = 0
        self._local_track_idx = next_idx
        if self._commercials and ch.commercials:
            self._start_commercial_break(
                lambda idx=next_idx, cidx=ch_idx: self._skip_to_track_by_ch_idx(cidx, idx))
        else:
            self._skip_to_track(ch, next_idx)
    def _play_url(self, url):
        if not self.player: self._tuning = False; return
        vol = self.vol_var.get()
        self._vlc_cmd('play_media', url, 0, vol)
        self._live_status_begin()
        self._tuning_clear_after = self.after(600, self._clear_tuning)
        self.after(800, self._install_vlc_dblclick_hook)
    def _dvr_play_recording(self, path):
        self._dvr_playing_path = path
        self.playhead_var.set(0.0)
        self._play_url(path)
        self._update_local_controls()
        self._update_dvr_strip()
        self.live_label.config(text="▶ DVR")
        if hasattr(self, 'btn_play_pause'):
            self.btn_play_pause.config(text="⏸ PAUSE")
    def _dvr_exit_playback(self):
        """Stop DVR recording playback. Advances the queue if one is active."""
        if not getattr(self, '_dvr_playing_path', None):
            return
        self._dvr_playing_path = None
        if self._dvr_queue:
            next_path = self._dvr_queue.pop(0)
            if os.path.exists(next_path):
                self._dvr_play_recording(next_path)
                return
            # Skip missing files and try the next one
            self.after(0, self._dvr_exit_playback)
            return
        self._tune_channel(self.current_channel_idx, static=False)

    def _dvr_play_all(self, paths):
        """Play a list of recordings consecutively."""
        if not paths:
            return
        self._dvr_queue = list(paths[1:])
        self._dvr_play_recording(paths[0])
    def _show_static(self, duration_ms=400, then=None):
        frames = 8; delay = duration_ms // frames
        def draw_frame(n):
            self.scanline_canvas.delete("static")
            w = max(self.scanline_canvas.winfo_width(), 1); h = max(self.scanline_canvas.winfo_height(), 1)
            for _ in range(200):
                x, y = random.randint(0, w), random.randint(0, h); size = random.randint(2, 12)
                gray = random.choice(["#ffffff", "#aaaaaa", "#555555", "#000000", "#888888", "#cccccc", "#222222"])
                self.scanline_canvas.create_rectangle(x, y, x + size, y + size, fill=gray, outline="", tags="static")
            if n < frames: self.after(delay, lambda: draw_frame(n + 1))
            else:
                self.scanline_canvas.delete("static")
                # FIX 11: clear _tuning now so no subsequent tune is blocked
                # by the animation's remaining lifetime; combined with FIX 1's
                # stamp check, stale _start_channel_playback calls are no-ops.
                self._tuning = False
                if then: then()
        draw_frame(0)
    # OSD
    def _on_canvas_resize(self, event=None):
        w = self.scanline_canvas.winfo_width(); h = self.scanline_canvas.winfo_height()
        if w > 1 and h > 1:
            self.scanline_canvas.coords(self._nosignal_window, w // 2, h // 2)
            self.scanline_canvas.coords(self._offline_window,  w // 2, h // 2)
    def _on_video_frame_resize(self, event=None):
        """Reposition OSD labels and watermark when the video area changes size."""
        self._on_watermark_resize(event)
        self._reposition_osd()
    def _reposition_osd(self):
        """Re-place OSD labels at the top-right of the video area (if visible)."""
        try:
            if not self._osd_text_label.winfo_ismapped():
                return
            vx = self.video_frame.winfo_x()
            vw = self.video_frame.winfo_width()
            right_x = vx + vw - 10
            self._osd_shadow_label.place(x=right_x + 2, y=14, anchor="ne")
            self._osd_text_label.place(  x=right_x,     y=12, anchor="ne")
        except Exception:
            pass
    def _on_osd_canvas_resize(self, event=None):
        self._reposition_osd()
    # ── Live-stream loading status overlay ──
    def _place_live_status(self):
        try:
            vx = self.video_frame.winfo_x(); vy = self.video_frame.winfo_y()
            vw = self.video_frame.winfo_width(); vh = self.video_frame.winfo_height()
            self._live_status_label.place(x=vx + vw // 2, y=vy + vh // 2, anchor="center")
            tk.Widget.lift(self._live_status_label)
        except Exception:
            pass
    def _live_status_begin(self):
        """Called whenever a live stream (re)starts loading."""
        if getattr(self, '_dvr_playing_path', None) or not self.channels:
            return
        ch = self.channels[self.current_channel_idx]
        if ch.is_local or ch.number == 0:
            return
        # Keep the original start time across reconnects so the elapsed
        # counter reflects how long the viewer has actually been waiting.
        if not self._live_status_job:
            self._live_status_start = _time.monotonic()
        self._live_status_armed = False
        self._vlc_buffer_pct = 0.0
        if not self._live_status_job:
            self._live_status_tick()
    def _live_status_hide(self):
        if self._live_status_job:
            try: self.after_cancel(self._live_status_job)
            except Exception: pass
            self._live_status_job = None
        try: self._live_status_label.place_forget()
        except Exception: pass
    def _live_status_tick(self):
        self._live_status_job = None
        if self._closing or not self.channels:
            return
        ch = self.channels[self.current_channel_idx]
        if (ch.is_local or ch.number == 0 or self._guide_active
                or getattr(self, '_dvr_playing_path', None)):
            self._live_status_hide(); return
        state = getattr(self, '_vlc_state', vlc.State.NothingSpecial)
        t_ms  = getattr(self, '_vlc_time_ms', 0)
        pct   = getattr(self, '_vlc_buffer_pct', 0.0)
        elapsed = _time.monotonic() - self._live_status_start
        # Ignore a stale 'Playing' left over from the previous stream until
        # the new media has been seen leaving Playing (or time has passed).
        if state != vlc.State.Playing or elapsed > 2.0:
            self._live_status_armed = True
        if self._live_status_armed and state == vlc.State.Playing and (t_ms > 0 or pct >= 100):
            self._live_status_hide(); return
        dots = "." * (1 + int(elapsed * 2) % 3)
        if state == vlc.State.Buffering or (state == vlc.State.Playing and 0 < pct < 100):
            line = f"BUFFERING {int(pct):3d}%"
        elif state == vlc.State.Playing:
            line = f"STARTING{dots:<3}"
        elif state == vlc.State.Opening:
            line = f"CONNECTING{dots:<3}"
        else:
            line = f"TUNING{dots:<3}"
        lines = [line]
        if self._live_reconnecting and self._live_error_count > 0:
            lines.insert(0, f"RECONNECTING ({self._live_error_count}/3)")
        if elapsed >= 1.0:
            lines.append(f"{int(elapsed)}s")
        if elapsed >= 8.0:
            lines.append("slow stream - please wait")
        self._live_status_label.config(text="\n".join(lines))
        # Short delay before first showing so fast channels don't flash it.
        if elapsed >= 0.7:
            self._place_live_status()
        self._live_status_job = self.after(250, self._live_status_tick)
    def _show_osd(self, text):
        try:
            vx = self.video_frame.winfo_x()
            vw = self.video_frame.winfo_width()
            right_x = vx + vw - 10
        except Exception:
            right_x = 200
        self._osd_shadow_label.config(text=text)
        self._osd_text_label.config(text=text)
        self._osd_shadow_label.place(x=right_x + 2, y=14, anchor="ne")
        self._osd_text_label.place(  x=right_x,     y=12, anchor="ne")
        tk.Widget.lift(self._osd_shadow_label)
        tk.Widget.lift(self._osd_text_label)
        if self._channel_display_after: self.after_cancel(self._channel_display_after)
        timeout_ms = random.randint(5000, 7000)
        def _hide_osd():
            self._osd_shadow_label.place_forget()
            self._osd_text_label.place_forget()
        self._channel_display_after = self.after(timeout_ms, _hide_osd)
    def _update_channel_strip(self):
        if getattr(self, '_dvr_playing_path', None):
            self._update_dvr_strip()
            return
        if not self.channels: self.ch_strip.config(text="NO CHANNELS LOADED"); return
        ch = self.channels[self.current_channel_idx]
        if ch.number == 0: self.ch_strip.config(text="  CH 00  ·  CABLE GUIDE  ·  INFO  "); return
        self.ch_strip.config(text=f"  CH {ch.number:02d}  ·  {ch.name}  ·  {'VHS' if ch.is_local else 'LIVE'}  ")
    def _update_dvr_strip(self):
        path = getattr(self, '_dvr_playing_path', None)
        if not path:
            return
        basename = os.path.splitext(os.path.basename(path))[0]
        parts = basename.rsplit('_', 2)
        def _trunc(s, n=35):
            return s if len(s) <= n else s[:n - 1] + "…"
        if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6:
            try:
                dt = datetime.datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y%m%d_%H%M%S")
                ch_name = _trunc(parts[0].replace('_', ' '))
                self.ch_strip.config(text=f"  DVR  ·  {ch_name}  ·  {dt.strftime('%Y-%m-%d %H:%M')}  ")
                return
            except ValueError:
                pass
        self.ch_strip.config(text=f"  DVR  ·  {_trunc(basename)}  ·  REC  ")
    # Controls
    def _prev_channel(self):
        import time; now = time.monotonic()
        if now - self._last_tune_time < 0.6: return
        self._last_tune_time = now; self._tune_channel(self.current_channel_idx - 1)
    def _next_channel(self):
        import time; now = time.monotonic()
        if now - self._last_tune_time < 0.6: return
        self._last_tune_time = now; self._tune_channel(self.current_channel_idx + 1)
    def _on_channel_entry(self, event=None):
        val = self.ch_entry_var.get().strip(); self.ch_entry_var.set("")
        try:
            num = int(val)
            for i, ch in enumerate(self.channels):
                if ch.number == num: self._tune_channel(i); return
            self._show_osd(f"CH {num} NOT FOUND")
        except ValueError: pass
    def _on_listbox_select(self, event):
        sel = self.ch_listbox.curselection()
        if not sel: return
        row = sel[0]
        ch_idx = self._listbox_ch_indices[row] if row < len(self._listbox_ch_indices) else None
        if ch_idx is not None and ch_idx != self.current_channel_idx: self._tune_channel(ch_idx)
    def _media_play_pause(self):
        if self.player: self._vlc_cmd('pause')
    def _media_stop(self):
        if self.player: self._vlc_cmd('stop')
    def _toggle_mute(self):
        if not self.player and not self._guide_player: return
        self._muted = not getattr(self, '_muted', False)
        if self.player: self._vlc_cmd('set_mute', self._muted)
        if self._guide_player:
            try: self._guide_player.audio_set_mute(self._muted)
            except Exception: pass
        icon = "🔇" if self._muted else "🔊"
        self.mute_btn.config(text=icon)
        # Keep the FS overlay mute button in sync
        if self._fs_mute_btn:
            try: self._fs_mute_btn.config(text=icon)
            except Exception: pass
        self._show_volume_osd(self.vol_var.get())
    def _toggle_subtitles(self):
        if not self.player: return
        self._subs_on = not getattr(self, '_subs_on', False)
        self._vlc_cmd('set_subs', self._subs_on)
        self.sub_btn.config(fg=self.AMBER if self._subs_on else self.AMBER_DIM)
    def _on_volume(self, val):
        self._set_volume(int(float(val))); self._show_volume_osd(int(float(val)))
    def _set_volume(self, vol):
        if self.player: self._vlc_cmd('set_volume', vol)
        if self._guide_player:
            try: self._guide_player.audio_set_volume(vol)
            except Exception: pass
    def _volume_up(self):
        new_vol = min(100, self.vol_var.get() + 5)
        self.vol_var.set(new_vol); self._set_volume(new_vol); self._show_volume_osd(new_vol)
    def _volume_down(self):
        new_vol = max(0, self.vol_var.get() - 5)
        self.vol_var.set(new_vol); self._set_volume(new_vol); self._show_volume_osd(new_vol)
    def _show_volume_osd(self, vol):
        muted = getattr(self, '_muted', False)
        if getattr(self, '_vol_osd_after', None):
            try:
                self.after_cancel(self._vol_osd_after)
            except Exception:
                pass
            self._vol_osd_after = None
        if getattr(self, '_vol_osd_frame', None):
            try: self._vol_osd_frame.destroy()
            except Exception: pass
            self._vol_osd_frame = None
        box_h = 54
        frame = tk.Frame(self.screen_frame, bg="#000000", highlightthickness=0)
        # Stretch full width of the video area, pinned to the bottom
        frame.place(relx=0, rely=1.0, y=-(box_h), relwidth=1.0, height=box_h)
        tk.Widget.lift(frame)
        self._vol_osd_frame = frame
        c = tk.Canvas(frame, bg="#000000", highlightthickness=0)
        c.pack(fill=tk.BOTH, expand=True)
        def _draw(event=None):
            c.delete("all")
            W = c.winfo_width() or frame.winfo_width() or 600
            H = c.winfo_height() or box_h
            # ── Dark CRT background panel ─────────────────────────────────
            c.create_rectangle(0, 0, W, H, fill="#0a0800", outline="")
            # Top amber rule line
            c.create_line(0, 0, W, 0, fill=self.AMBER, width=2)
            # Subtle scanline texture
            for sy in range(4, H, 4):
                c.create_line(0, sy, W, sy, fill="#1a1200", stipple="gray50")
            # ── Layout constants ──────────────────────────────────────────
            pad_x   = 16
            icon_x  = pad_x
            pct_x   = icon_x + 130
            bar_x0  = pct_x + 62
            bar_x1  = W - pad_x
            bar_w   = max(bar_x1 - bar_x0, 10)
            cy      = H // 2
            bar_h   = 16
            bar_y0  = cy - bar_h // 2
            bar_y1  = bar_y0 + bar_h
            # ── Icon ─────────────────────────────────────────────────────
            icon = "MUTE" if muted else "VOL"
            icon_col = self.GUIDE_RED if muted else self.AMBER_DIM
            c.create_text(icon_x, cy, anchor="w", text=icon,
                          font=("Courier New", 8, "bold"), fill=icon_col)
            # ── Speaker glyph (ASCII art bars) ───────────────────────────
            spk_x = icon_x + 34
            if muted:
                c.create_text(spk_x, cy, anchor="w", text="▐░▌",
                              font=("Courier New", 11), fill=self.GUIDE_RED)
            elif vol < 30:
                c.create_text(spk_x, cy, anchor="w", text="▐█▌",
                              font=("Courier New", 11), fill=self.AMBER_DIM)
            elif vol < 70:
                c.create_text(spk_x, cy, anchor="w", text="▐█▌▌",
                              font=("Courier New", 11), fill=self.AMBER)
            else:
                c.create_text(spk_x, cy, anchor="w", text="▐█▌▌▌",
                              font=("Courier New", 11), fill=self.AMBER)
            # ── Percentage / MUTED ────────────────────────────────────────
            pct_text = "MUTED" if muted else f"{vol:3d}%"
            pct_col  = self.GUIDE_RED if muted else self.AMBER
            c.create_text(pct_x, cy, anchor="e",
                          text=pct_text,
                          font=("Courier New", 18, "bold"), fill=pct_col)
            # ── Bar trough ────────────────────────────────────────────────
            c.create_rectangle(bar_x0, bar_y0, bar_x1, bar_y1,
                               fill="#0a0800", outline=self.AMBER_DIM, width=1)
            # ── Filled segments (amber → yellow → red) ────────────────────
            if not muted and vol > 0:
                fill_px = max(2, int(bar_w * vol / 100))
                segments = (
                    (0,  70, self.AMBER),
                    (70, 90, self.GUIDE_YELLOW),
                    (90, 100, self.GUIDE_RED),
                )
                for seg_lo, seg_hi, colour in segments:
                    if vol <= seg_lo:
                        break
                    sx = int(bar_w * seg_lo  / 100)
                    ex = min(fill_px, int(bar_w * seg_hi / 100))
                    if ex <= sx:
                        continue
                    # Main fill
                    c.create_rectangle(bar_x0 + sx + 1, bar_y0 + 1,
                                       bar_x0 + ex,     bar_y1 - 1,
                                       fill=colour, outline="")
                    # Shine strip along top
                    shine = "#ffffff" if colour == self.AMBER else colour
                    c.create_line(bar_x0 + sx + 1, bar_y0 + 1,
                                  bar_x0 + ex,     bar_y0 + 1,
                                  fill=shine, stipple="gray25")
            # ── Segment tick marks every 10% ──────────────────────────────
            for pct in range(0, 101, 10):
                tx = bar_x0 + int(bar_w * pct / 100)
                # Tall tick at 0, 50, 100; short otherwise
                tick_h = 6 if pct in (0, 50, 100) else 3
                c.create_line(tx, bar_y1 - tick_h, tx, bar_y1,
                              fill=self.AMBER_DIM, width=1)
                if pct in (0, 50, 100):
                    c.create_text(tx, bar_y1 + 5, anchor="n",
                                  text=str(pct), font=("Courier New", 6),
                                  fill=self.AMBER_DIM)
            # ── Bottom rule ───────────────────────────────────────────────
            c.create_line(0, H - 1, W, H - 1, fill=self.AMBER_DIM, width=1)
        c.bind("<Configure>", _draw)
        # Initial draw after geometry is resolved
        frame.after(10, _draw)
        def _hide():
            try: frame.destroy()
            except Exception: pass
            if getattr(self, '_vol_osd_frame', None) is frame:
                self._vol_osd_frame = None
        self._vol_osd_after = self.after(2500, _hide)

    # ══════════════════════════════════════════
    #  Fullscreen
    # ══════════════════════════════════════════
    _FS_TOGGLE_DEBOUNCE = 0.3
    def _toggle_fullscreen(self):
        # ONE timestamp guards both directions.  Separate enter/exit stamps did
        # not debounce anything: a single physical double-click can arrive twice
        # (the tkinter <Double-Button-1> binding *and* the VLC wndproc hook at
        # _install_vlc_dblclick_hook both queue a toggle), and because the first
        # call flips is_fullscreen, the second consulted the *opposite* — stale —
        # stamp, sailed past the guard, and immediately undid the first.
        # _enter/_exit stamp this themselves, so Escape and the overlay ⛶ button
        # (which call them directly) are debounced on the same clock.
        if _time.monotonic() - self._last_fs_toggle < self._FS_TOGGLE_DEBOUNCE:
            return
        if self.is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()
    def _get_current_monitor_geometry(self):
        # ── Win32: use MonitorFromWindow (HWND) as primary path ───────────
        # MonitorFromPoint relies on winfo_x()/winfo_y() which return the
        # *restore* position when the window is maximised, potentially pointing
        # at the wrong monitor.  MonitorFromWindow queries the OS directly and
        # is always correct regardless of window state.
        if sys.platform == "win32":
            try:
                import ctypes.wintypes as _wt
                class _RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
                class _MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_ulong),
                                ("rcMonitor", _RECT), ("rcWork", _RECT),
                                ("dwFlags", ctypes.c_ulong)]
                _user32 = ctypes.windll.user32
                _hmon = _user32.MonitorFromWindow(
                    self.winfo_id(), 2)            # MONITOR_DEFAULTTONEAREST
                _mi = _MONITORINFO(); _mi.cbSize = ctypes.sizeof(_MONITORINFO)
                _user32.GetMonitorInfoW(_hmon, ctypes.byref(_mi))
                r = _mi.rcMonitor
                return (r.left, r.top, r.right - r.left, r.bottom - r.top)
            except Exception:
                pass
        # ── screeninfo fallback (cross-platform) ──────────────────────────
        win_x = self.winfo_x() + self.winfo_width() // 2
        win_y = self.winfo_y() + self.winfo_height() // 2
        try:
            from screeninfo import get_monitors
            for m in get_monitors():
                if m.x <= win_x < m.x + m.width and m.y <= win_y < m.y + m.height:
                    return (m.x, m.y, m.width, m.height)
            m = get_monitors()[0]; return (m.x, m.y, m.width, m.height)
        except ImportError:
            pass
        return (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
    def _center_dialog(self, dialog, dw=None, dh=None):
        self.update_idletasks()
        mx, my, mw, mh = self._get_current_monitor_geometry()
        if dw is not None and dh is not None:
            x = mx + max(0, (mw - dw) // 2)
            y = my + max(0, (mh - dh) // 2)
            dialog.geometry(f"{dw}x{dh}+{x}+{y}")
        else:
            dialog.update_idletasks()
            dw = dialog.winfo_reqwidth()
            dh = dialog.winfo_reqheight()
            x = mx + max(0, (mw - dw) // 2)
            y = my + max(0, (mh - dh) // 2)
            dialog.geometry(f"+{x}+{y}")
    def _bind_dblclick_everywhere(self):
        handler = lambda e: self._toggle_fullscreen()
        for widget in (self.video_frame, self.scanline_canvas):
            try: widget.bind("<Double-Button-1>", handler)
            except Exception: pass
    def _enter_fullscreen(self):
        if self._closing: return
        # Idempotent: a second entry while already fullscreen would overwrite
        # _pre_fs_geometry with the *fullscreen* rect, permanently destroying the
        # geometry we need to restore.  Reachable via the start_fullscreen timer
        # racing a double-click.
        if self.is_fullscreen: return
        self.is_fullscreen = True
        self._last_fs_toggle = _time.monotonic()
        self._pre_fs_geometry = self.geometry()
        mx, my, mw, mh = self._get_current_monitor_geometry()
        self._fs_monitor_rect = (mx, my, mw, mh)   # save for overlay positioning
        self.ctrl_bar.pack_forget(); self.sidebar.pack_forget(); self._resize_sash.pack_forget()
        # Strip decorative bezel frames and screen border
        self.tv_outer.pack_configure(padx=0, pady=0)
        self.tv_inner.pack_configure(padx=0, pady=0)
        self.screen_frame.config(highlightthickness=0)
        self.screen_frame.pack_configure(padx=0, pady=0)
        try:
            self.overrideredirect(True)
        except Exception:
            pass
        self.geometry(f"{mw}x{mh}+{mx}+{my}")
        self.lift(); self.focus_force()
        self._bind_dblclick_everywhere()
        self.bind("<Escape>", lambda e: self._exit_fullscreen())
        self.bind("<F11>",    lambda e: self._toggle_fullscreen())
        self.bind("<g>",      lambda e: self._toggle_epg_overlay())
        self.bind("<G>",      lambda e: self._toggle_epg_overlay())
        # Bind mouse motion on tkinter widgets (catches cursor over non-VLC areas)
        for _w in (self, self.screen_frame, self.video_frame, self.scanline_canvas):
            try: _w.bind("<Motion>", self._fs_mouse_move, add='+')
            except Exception: pass
        # Primary detection: poll the OS cursor position every 100 ms.
        # This works even when VLC's native window absorbs all Win32 mouse messages
        # before tkinter or the wndproc hook can see them.
        self._fs_last_mouse_pos = None
        self._fs_start_mouse_poll()
        self._uninstall_vlc_dblclick_hook()
        self.after(300, self._install_vlc_dblclick_hook)
        if self._settings.get('suppress_screensaver', True):
            _suppress_screensaver(True)
    def _exit_fullscreen(self):
        if not self.is_fullscreen: return
        if self._closing: return
        self.is_fullscreen = False
        self._last_fs_toggle = _time.monotonic()
        # Dismiss fullscreen overlay, stop poll loop, unbind motion hooks
        self._hide_fs_overlay()
        self._fs_stop_mouse_poll()
        for _w in (self, self.screen_frame, self.video_frame, self.scanline_canvas):
            try: _w.unbind("<Motion>")
            except Exception: pass
        # Uninstall the VLC wndproc hook BEFORE overrideredirect so no messages
        # can fire through a stale ctypes callback during the window transition.
        self._uninstall_vlc_dblclick_hook()
        try:
            self.overrideredirect(False)
        except Exception:
            pass
        # Let the window manager re-decorate and settle before restoring geometry.
        self.update_idletasks()
        try:
            if hasattr(self, '_pre_fs_geometry') and self._pre_fs_geometry:
                self.geometry(self._pre_fs_geometry)
        except Exception:
            pass
        # Restore decorative bezel frames and screen border
        self.tv_outer.pack_configure(padx=6, pady=6)
        self.tv_inner.pack_configure(padx=10, pady=10)
        self.screen_frame.config(highlightthickness=4)
        self.main_area.pack_forget(); self.main_area.pack(fill=tk.BOTH, expand=True)
        self.sidebar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=8)
        if not self._sidebar_collapsed:
            self._resize_sash.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        self.screen_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)
        self.ctrl_bar.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.lift(); self._bind_dblclick_everywhere()
        # Defer hook reinstall until the layout has fully settled so VLC's child
        # window has a stable HWND to subclass.
        self.after(500, self._install_vlc_dblclick_hook)
        if self._settings.get('suppress_screensaver', True):
            _suppress_screensaver(False)

    # ══════════════════════════════════════════
    #  Polling & Retry Logic
    # ══════════════════════════════════════════
    def _poll_vlc(self):
        if self._closing: return
        if self.player and self.channels:
            state = getattr(self, '_vlc_state', vlc.State.NothingSpecial)
            t_ms = getattr(self, '_vlc_time_ms', 0); total = getattr(self, '_vlc_len_ms', 0)
            ch = self.channels[self.current_channel_idx]
            
            # --- Error Handling & Retries ---
            if getattr(self, '_dvr_playing_path', None) and state == vlc.State.Ended:
                self._dvr_exit_playback()
                return
            if state == vlc.State.Ended or (state == vlc.State.Error and ch.is_local):
                if ch.is_local:
                    if self._in_commercial:
                        self._play_next_commercial()
                    elif not getattr(self, '_advancing', False):
                        # On a clean end, save position before advancing so the
                        # resume file reflects the completed track (timestamp 0
                        # means start-of-track, which is correct for a finished file).
                        if state == vlc.State.Ended:
                            self._save_local_position(ch)
                        # Check retry count before advancing
                        if state == vlc.State.Error and self._local_retry_count < self._max_retries:
                            self._local_retry_count += 1
                            # Show buffering state
                            self.filename_label.config(text=f"BUFFERING (RETRY {self._local_retry_count})...", fg=self.RED_GLOW)
                            self._is_retrying = True
                            self._vlc_cmd('stop') # Clear error state
                            
                            # Exponential backoff: 2s, 4s, 6s
                            delay_ms = 2000 * self._local_retry_count
                            _log_vlc.warning(
                                "local file error on '%s'; retry %d/%d in %dms",
                                ch.name, self._local_retry_count, self._max_retries,
                                delay_ms)
                            
                            # Cancel existing job if any
                            if self._retry_job_id: self.after_cancel(self._retry_job_id)
                            
                            # Schedule retry
                            self._retry_job_id = self.after(delay_ms, self._retry_local_playback)
                        else:
                            # Max retries reached or clean end
                            self._local_retry_count = 0
                            self._is_retrying = False
                            self._advancing = True
                            self._advance_local_track()
                elif not self._live_reconnecting:
                    # Stream ended cleanly — reconnect once
                    _log_live.info("stream ended on '%s', reconnecting", ch.name)
                    self._live_reconnecting = True
                    self._live_reconnect_deadline = _time.monotonic() + 10.0
                    self._live_last_time_ms = -1
                    self._live_frozen_since = 0.0
                    self._live_tune_time = _time.monotonic()
                    self._play_url(ch.url)
            elif state == vlc.State.Playing:
                # Reset retry counter on successful playback
                if self._local_retry_count > 0:
                    _log_vlc.info("local playback recovered on '%s'", ch.name)
                self._local_retry_count = 0
                self._is_retrying = False
                self._advancing = False

            # --- UI Updates ---
            if not ch.is_local and ch.number != 0:
                if state == vlc.State.Playing:
                    # Declare recovery as soon as Playing — don't wait for t_ms to
                    # advance, because some streams always report t_ms=0 or -1 and
                    # would never reset the error count, eventually hitting the
                    # offline threshold even while the stream is alive.
                    if self._live_error_count != 0 or self._live_reconnecting:
                        _log_live.info("stream recovered on '%s'", ch.name)
                        self._live_error_count = 0
                        self._live_reconnecting = False
                        self.scanline_canvas.itemconfigure(self._offline_window, state='hidden')
                        self.time_label.config(text="LIVE", fg=self.AMBER_DIM,
                            font=self.font_small)
                    # ── Freeze detection: check if playhead has stopped moving ──
                    if t_ms > 0 and t_ms != self._live_last_time_ms:
                        self._live_last_time_ms = t_ms
                        self._live_frozen_since = 0.0
                    elif t_ms == self._live_last_time_ms and t_ms >= 0:
                        # Playhead not moving while state says Playing — potential freeze
                        now = _time.monotonic()
                        if self._live_frozen_since == 0.0:
                            self._live_frozen_since = now
                        elif now - self._live_frozen_since >= 8.0 and not self._live_reconnecting:
                            _log_live.warning("stream frozen for 8s on '%s', reconnecting",
                                              ch.name)
                            self._live_reconnecting = True
                            self._live_reconnect_deadline = now + 10.0  # safety valve
                            self._live_frozen_since = 0.0
                            self._live_last_time_ms = -1
                            self._live_tune_time = now
                            self._play_url(ch.url)
                elif state == vlc.State.Error:
                    # STUCK-STATE FIX: previously, once _live_reconnecting was
                    # set True here it was only cleared on successful playback.
                    # If the retry itself failed fast into Error again, the
                    # guard below blocked any further handling and the channel
                    # stayed silently broken forever. We now use a deadline:
                    # if the reconnect doesn't reach Playing within 10s, we
                    # let another retry fire, counting it toward the limit.
                    now = _time.monotonic()
                    deadline_passed = (
                        self._live_reconnecting
                        and getattr(self, '_live_reconnect_deadline', 0) > 0
                        and now >= self._live_reconnect_deadline
                    )
                    if not self._live_reconnecting or deadline_passed:
                        self._live_reconnecting = True
                        self._live_reconnect_deadline = now + 10.0
                        self._live_last_time_ms = -1
                        self._live_frozen_since = 0.0
                        self._live_error_count += 1
                        _log_live.warning(
                            "stream error on '%s' (attempt %d/3), reconnecting",
                            ch.name, self._live_error_count)
                        if self._live_error_count >= 3:
                            _log_live.error("stream '%s' failed 3x, marking offline",
                                            ch.name)
                            self._show_live_offline()
                            # Stop hammering a dead stream — fully reset the
                            # reconnect state so the user tuning away and back
                            # starts a fresh attempt sequence.
                            self._live_reconnecting = False
                            self._live_reconnect_deadline = 0
                            self._live_tune_time = 0
                        else:
                            self._live_tune_time = now
                            self._play_url(ch.url)
                elif state in (vlc.State.Opening, vlc.State.Buffering):
                    _log_live.debug("state=%s on '%s'", state, ch.name)
                    # Don't reset _live_reconnecting here — let the Playing
                    # branch above do it once we've actually recovered.
                    self._live_tune_time = _time.monotonic()
                elif state not in (vlc.State.Paused,):
                    # NothingSpecial / Stopped / Ended with no time progress
                    now = _time.monotonic()
                    elapsed = now - self._live_tune_time
                    deadline_passed = (
                        self._live_reconnecting
                        and self._live_reconnect_deadline > 0
                        and now >= self._live_reconnect_deadline
                    )
                    # Also trigger on deadline_passed: if VLC settles into
                    # Stopped/NothingSpecial after a reconnect attempt the Error
                    # branch never runs, so the deadline must be checked here too.
                    if deadline_passed or (elapsed >= 12.0 and self._live_tune_time > 0 and not self._live_reconnecting):
                        self._live_error_count += 1
                        _log_live.warning(
                            "stream '%s' stuck in state=%s for %.0fs (attempt %d/3)",
                            ch.name, state, elapsed, self._live_error_count)
                        # RESET fix: zero tune_time so this branch doesn't
                        # re-fire every 1s forever. We either reconnect (which
                        # sets a fresh tune_time) or mark offline (and stop).
                        if self._live_error_count >= 3:
                            _log_live.error("stream '%s' offline after timeouts",
                                            ch.name)
                            self._show_live_offline()
                            self._live_tune_time = 0
                        else:
                            self._live_reconnecting = True
                            self._live_reconnect_deadline = _time.monotonic() + 10.0
                            self._live_tune_time = _time.monotonic()
                            self._play_url(ch.url)

            if getattr(self, '_dvr_playing_path', None) and state == vlc.State.Playing:
                if t_ms >= 0:
                    def fmt(ms):
                        s = ms // 1000
                        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60:02d}:{s%60:02d}"
                    t_str = fmt(t_ms); tot_str = fmt(total) if total > 0 else "--:--"
                    self.time_label.config(text=f"{t_str}/{tot_str}  [DVR]")
                    if total > 0 and not self._playhead_hold(): self.playhead_var.set(t_ms / total)
                else: self.time_label.config(text="")
            elif ch.is_local and state == vlc.State.Playing and self._local_track_order:
                if t_ms >= 0:
                    def fmt(ms):
                        s = ms // 1000
                        return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60:02d}:{s%60:02d}"
                    t_str = fmt(t_ms); tot_str = fmt(total) if total > 0 else "--:--"
                    if self._in_commercial: self.time_label.config(text=f"{t_str}/{tot_str}  [AD]")
                    else:
                        track_disp = min(self._local_track_idx + 1, len(self._local_track_order))
                        self.time_label.config(text=f"{t_str}/{tot_str}  [{track_disp}/{len(self._local_track_order)}]")
                    if total > 0 and not self._playhead_hold(): self.playhead_var.set(t_ms / total)
                else: self.time_label.config(text="")
            elif not ch.is_local and not getattr(self, '_dvr_playing_path', None):
                if self._live_error_count == 0: self.time_label.config(text="LIVE")
            try: self._smtc_sync(ch, state, t_ms, total)
            except Exception as e: log.debug("smtc sync failed: %s", e)
        self.after(1000, self._poll_vlc)

    def _retry_local_playback(self):
        """Helper to retry playing the current local track after a backoff delay."""
        if self._closing: return
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not ch.is_local: return
        # Use the furthest known position: prefer the live playhead if it advanced
        # beyond the original seek point, so we don't rewind on retry.
        live_ms  = max(0, getattr(self, '_vlc_time_ms', 0))
        start_ms = max(0, getattr(self, '_last_local_seek_ms', 0))
        seek = live_ms if live_ms > start_ms else start_ms
        self._play_local_track(ch, self._local_track_idx, seek_ms=seek)
        self._retry_job_id = None

    def _show_live_offline(self):
        self._live_status_hide()
        w = self.scanline_canvas.winfo_width(); h = self.scanline_canvas.winfo_height()
        self.scanline_canvas.coords(self._offline_window, max(w // 2, 10), max(h // 2, 10))
        self.scanline_canvas.itemconfigure(self._offline_window, state='normal')
        self.time_label.config(
            text="⬤ NOT BROADCASTING",
            fg=self.RED_GLOW,
            font=self._offline_font)

    def _open_log_file(self):
        """Ctrl+L: open the rotating log file in the OS's default text viewer."""
        try:
            path = LOG_PATH
            if not os.path.exists(path):
                _log_vlc.info("log file requested but does not exist yet: %s", path)
                return
            _log_vlc.info("user opened log file via Ctrl+L")
            if sys.platform == 'win32':
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', path])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', path])
        except Exception as exc:
            _log_vlc.exception("failed to open log file: %s", exc)

    # Remote Config
    REMOTE_CONFIG_URL = "https://cdn.wedovids.com/tv/tv_config.json"
    def _check_remote_config(self):
        def fetch():
            try:
                req = urllib.request.Request(self.REMOTE_CONFIG_URL, headers={"User-Agent": "RetroTV/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp: remote_raw = resp.read().decode("utf-8")
            except Exception as exc: print(f"[config update] fetch failed: {exc}"); return
            try:
                remote_cfg = json.loads(re.sub(r',\s*(\]|\})', r'\1', remote_raw))
                remote_normalised = json.dumps(remote_cfg, sort_keys=True)
            except Exception as exc: print(f"[config update] remote JSON invalid: {exc}"); return
            local_normalised = None
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f: local_raw = f.read()
                    local_cfg = json.loads(re.sub(r',\s*(\]|\})', r'\1', local_raw))
                    local_normalised = json.dumps(local_cfg, sort_keys=True)
                except Exception: pass
            if remote_normalised == local_normalised: return
            try:
                with open(self.config_path, "w", encoding="utf-8") as f: f.write(remote_raw)
            except Exception as exc: return
            self.after(0, self._apply_updated_config)
        threading.Thread(target=fetch, daemon=True).start()
    def _manual_lineup_refresh(self):
        # ── Non-curated users: reload their own user_config.json only ────
        if not self._settings.get('use_curated_channels', False):
            self._show_update_banner("↺  REFRESHING YOUR CHANNELS...")
            current_name = (self.channels[self.current_channel_idx].name
                            if self.channels else None)
            self.channels = []
            self._user_epg_sources = []
            self._load_config()
            self._rebuild_channel_list()
            self._fetch_epg_async(silent=True)
            # Try to stay on the same channel
            if current_name:
                for i, ch in enumerate(self.channels):
                    if ch.name == current_name:
                        self._tune_channel(i, static=False)
                        break
            self._show_update_banner("✔  CHANNELS RELOADED")
            return
        # ── Curated users: fetch latest lineup from remote ────────────────
        self._show_update_banner("↺  CHECKING FOR NEW LINEUP...")
        def fetch_and_apply():
            try:
                req = urllib.request.Request(self.REMOTE_CONFIG_URL, headers={"User-Agent": "RetroTV/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp: remote_raw = resp.read().decode("utf-8")
            except Exception:
                self.after(0, lambda: self._show_update_banner("✖  LINEUP FETCH FAILED")); return
            try:
                remote_cfg = json.loads(re.sub(r',\s*(\]|\})', r'\1', remote_raw))
                remote_normalised = json.dumps(remote_cfg, sort_keys=True)
            except Exception:
                self.after(0, lambda: self._show_update_banner("✖  INVALID LINEUP DATA")); return
            local_normalised = None
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f: local_raw = f.read()
                    local_cfg = json.loads(re.sub(r',\s*(\]|\})', r'\1', local_raw))
                    local_normalised = json.dumps(local_cfg, sort_keys=True)
                except Exception: pass
            if remote_normalised == local_normalised:
                self.after(0, lambda: self._show_update_banner("✔  LINEUP IS ALREADY UP TO DATE"))
                self.after(0, self._fetch_epg_async)
                return
            try:
                with open(self.config_path, "w", encoding="utf-8") as f: f.write(remote_raw)
            except Exception:
                self.after(0, lambda: self._show_update_banner("✖  COULD NOT SAVE LINEUP")); return
            self.after(0, self._apply_updated_config)
        threading.Thread(target=fetch_and_apply, daemon=True).start()
    def _apply_updated_config(self):
        if self.channels:
            current_ch = self.channels[self.current_channel_idx]
            if current_ch.is_local:
                # Save position regardless of player state — covers retrying and paused cases
                self._save_local_position(current_ch)
            self._resume_state['__last_channel__'] = current_ch.name
            _save_resume_state(self._resume_state)
        self.channels = []
        self._load_config()
        # Keep all favorites — _is_favorite() returns False for channels not
        # currently in self.channels, so orphaned names are harmless.  Filtering
        # here caused stars to be silently dropped whenever the lineup shifted
        # channel numbers or a playlist file was temporarily unavailable.
        self._save_favorites()
        self._rebuild_channel_list()
        self._fetch_epg_async()
        self._show_update_banner("✦ NEW CHANNEL LINEUP INSTALLED ✦")
        if self.channels:
            last_name = self._resume_state.get('__last_channel__', None)
            restore_idx = 0
            if last_name:
                for i, ch in enumerate(self.channels):
                    if ch.name == last_name:
                        restore_idx = i; break
            self._tune_channel(restore_idx, static=True)
    def _show_update_banner(self, message):
        banner = tk.Label(self.scanline_canvas, text=message, font=self.font_small, fg="#000000", bg=self.AMBER, padx=12, pady=6, relief=tk.FLAT, bd=0)
        w = self.scanline_canvas.winfo_width()
        banner_win = self.scanline_canvas.create_window(max(w // 2, 10), 0, anchor=tk.N, window=banner, state='normal', tags='banner')
        def fade_out():
            self.scanline_canvas.itemconfigure(banner_win, state='hidden')
            self.scanline_canvas.delete(banner_win); banner.destroy()
        self.after(6000, fade_out)

    # Software Update
    SOFTWARE_VERSION_URL  = "https://cdn.wedovids.com/tv/version.json"
    SOFTWARE_DOWNLOAD_URL = "https://cdn.wedovids.com/tv/download"
    def _check_software_update(self):
        def fetch():
            try:
                req = urllib.request.Request(self.SOFTWARE_VERSION_URL, headers={"User-Agent": f"RetroTV/{APP_VERSION}"})
                with urllib.request.urlopen(req, timeout=10) as resp: data = json.loads(resp.read().decode("utf-8"))
            except Exception as exc: return
            remote_version = data.get("version", "").strip()
            download_url = data.get("download_url", self.SOFTWARE_DOWNLOAD_URL).strip()
            if remote_version and self._version_newer(remote_version, APP_VERSION):
                self.after(0, lambda: self._prompt_software_update(remote_version, download_url))
        threading.Thread(target=fetch, daemon=True).start()
    @staticmethod
    def _version_newer(remote: str, local: str) -> bool:
        def parse(v):
            try: return tuple(int(x) for x in v.lstrip("v").split("."))
            except ValueError: return (0,)
        return parse(remote) > parse(local)
    def _prompt_software_update(self, new_version: str, download_url: str):
        dialog = tk.Toplevel(self); self._apply_icon(dialog); dialog.title("RetroTV Update"); dialog.configure(bg="#1c1308")
        dialog.resizable(False, False); dialog.grab_set(); dialog.focus_force()
        self._center_dialog(dialog, 420, 260)
        border = tk.Frame(dialog, bg=self.AMBER, bd=0); border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg="#1c1308"); inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        title_bar = tk.Frame(inner, bg=self.AMBER_DIM); title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="  ● SOFTWARE UPDATE AVAILABLE", font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM, anchor="w", pady=5).pack(fill=tk.X)
        body = tk.Frame(inner, bg="#1c1308"); body.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)
        tk.Label(body, text="NEW VERSION DETECTED", font=font.Font(family="Courier New", size=11, weight="bold"), fg=self.AMBER, bg="#1c1308").pack(pady=(0, 6))
        tk.Label(body, text=f"Installed :  v{APP_VERSION}", font=self.font_ch, fg=self.BEIGE, bg="#1c1308").pack(anchor="w")
        tk.Label(body, text=f"Available :  v{new_version}", font=self.font_ch, fg=self.AMBER, bg="#1c1308").pack(anchor="w")
        tk.Label(body, text="Open your browser to download the update?", font=self.font_ch, fg=self.TEXT_CLR, bg="#1c1308", wraplength=360, justify="left").pack(anchor="w")
        btn_row = tk.Frame(inner, bg="#1c1308"); btn_row.pack(fill=tk.X, padx=16, pady=(0, 14))
        def do_download():
            webbrowser.open(download_url); dialog.destroy(); self.after(500, self._on_close)
        def do_skip(): dialog.destroy()
        dl_btn = tk.Button(btn_row, text="▶  DOWNLOAD NOW", command=do_download, bg=self.AMBER_DIM, fg=self.AMBER,
            activebackground=self.AMBER, activeforeground="#000000", font=self.font_small, relief=tk.FLAT, padx=10, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER)
        dl_btn.pack(side=tk.LEFT, padx=(0, 8))
        skip_btn = tk.Button(btn_row, text="SKIP", command=do_skip, bg="#2a1f10", fg=self.AMBER_DIM,
            activebackground="#2a1f10", activeforeground=self.AMBER, font=self.font_small, relief=tk.FLAT, padx=10, pady=5, cursor="hand2",
            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        skip_btn.pack(side=tk.LEFT)
        dialog.bind("<Escape>", lambda e: do_skip())

    # ══════════════════════════════════════════
    #  Favorites
    # ══════════════════════════════════════════
    def _save_favorites(self):
        self._resume_state['__favorites__'] = list(self._favorites)
        _save_resume_state(self._resume_state)
    def _is_favorite(self, ch_number):
        ch = next((c for c in self.channels if c.number == ch_number), None)
        return ch.name in self._favorites if ch else False
    def _toggle_favorite(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if ch.number == 0: return
        if ch.name in self._favorites:
            self._favorites.discard(ch.name); self._show_osd("☆ REMOVED FROM FAVORITES")
        else:
            self._favorites.add(ch.name); self._show_osd("★ ADDED TO FAVORITES")
        self._save_favorites(); self._rebuild_channel_list(); self._update_fav_btn()
    def _update_fav_btn(self):
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if ch.number != 0 and self._is_favorite(ch.number): self.fav_btn.config(text="★", fg=self.AMBER)
        else: self.fav_btn.config(text="☆", fg=self.AMBER_DIM)
    def _rebuild_channel_list(self):
        raw_query = getattr(self, '_ch_search_var', None)
        query = ""
        if raw_query is not None:
            v = raw_query.get()
            if v != getattr(self, '_ch_search_placeholder', ''):
                query = v.strip().lower()
        self.ch_listbox.delete(0, tk.END)
        self._listbox_ch_indices = []
        fav_filter    = getattr(self, '_fav_filter_active',    False)
        recent_filter = getattr(self, '_recent_filter_active', False)
        if recent_filter:
            recently_watched = getattr(self, '_recently_watched', [])
            name_to_idx = {ch.name: i for i, ch in enumerate(self.channels)}
            channel_pairs = [(name_to_idx[name], self.channels[name_to_idx[name]])
                             for name in recently_watched if name in name_to_idx]
        else:
            channel_pairs = list(enumerate(self.channels))
        for i, ch in channel_pairs:
            if query and query not in ch.name.lower() and query not in str(ch.number):
                continue
            if fav_filter and not (ch.number != 0 and self._is_favorite(ch.number)):
                continue
            star = self._is_favorite(ch.number) and ch.number != 0
            prefix = "★ " if star else ("◷ " if recent_filter else "  ")
            self.ch_listbox.insert(tk.END, f"{prefix}{ch.number:02d}  {ch.name}")
            self._listbox_ch_indices.append(i)
        for row, ch_idx in enumerate(self._listbox_ch_indices):
            if ch_idx == self.current_channel_idx:
                self.ch_listbox.selection_clear(0, tk.END)
                self.ch_listbox.selection_set(row)
                self.ch_listbox.see(row)
                break
        for row, ch_idx in enumerate(self._listbox_ch_indices):
            ch = self.channels[ch_idx]
            if ch.number != 0 and self._is_favorite(ch.number):
                self.ch_listbox.itemconfig(row, fg=self.AMBER)
    def _on_listbox_rightclick(self, event):
        idx = self.ch_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._listbox_ch_indices): return
        ch_idx = self._listbox_ch_indices[idx]
        if ch_idx is None: return
        ch = self.channels[ch_idx]
        if ch.number == 0: return
        menu = tk.Menu(self, tearoff=0, bg="#1c1308", fg=self.AMBER, activebackground=self.AMBER_DIM, activeforeground=self.AMBER, font=self.font_ch, relief=tk.FLAT, bd=1)
        if self._is_favorite(ch.number):
            menu.add_command(label=f"☆  Remove '{ch.name}' from Favorites", command=lambda: self._set_favorite(ch.number, False))
        else:
            menu.add_command(label=f"★  Add '{ch.name}' to Favorites", command=lambda: self._set_favorite(ch.number, True))
        menu.add_separator()
        menu.add_command(label=f"▶  Tune to CH {ch.number:02d} {ch.name}", command=lambda: self._tune_channel(ch_idx))
        if ch_idx == self.current_channel_idx:
            guide_label = f"📋  View Guide for CH {ch.number:02d} (playing now)"
        else:
            guide_label = f"👁  Preview Guide for CH {ch.number:02d} (keep watching current)"
        menu.add_command(label=guide_label, command=lambda: self._preview_channel_epg(ch_idx))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()
    def _set_favorite(self, ch_number, state: bool):
        ch = next((c for c in self.channels if c.number == ch_number), None)
        if not ch: return
        if state: self._favorites.add(ch.name)
        else: self._favorites.discard(ch.name)
        self._save_favorites(); self._rebuild_channel_list(); self._update_fav_btn()
    def _toggle_fav_filter(self):
        self._fav_filter_active = not self._fav_filter_active
        if self._fav_filter_active:
            self._recent_filter_active = False
            self._recent_filter_btn.config(fg=self.AMBER_DIM, bg="#1c1308", activebackground=self.AMBER_DIM, activeforeground="#000000")
            self._fav_filter_btn.config(fg=self.AMBER, bg=self.AMBER_DIM, activebackground=self.AMBER, activeforeground="#000000")
        else:
            self._fav_filter_btn.config(fg=self.AMBER_DIM, bg="#1c1308", activebackground=self.AMBER_DIM, activeforeground="#000000")
        self._rebuild_channel_list()
    def _toggle_recent_filter(self):
        self._recent_filter_active = not self._recent_filter_active
        if self._recent_filter_active:
            self._fav_filter_active = False
            self._fav_filter_btn.config(fg=self.AMBER_DIM, bg="#1c1308", activebackground=self.AMBER_DIM, activeforeground="#000000")
            self._recent_filter_btn.config(fg=self.AMBER, bg=self.AMBER_DIM, activebackground=self.AMBER, activeforeground="#000000")
        else:
            self._recent_filter_btn.config(fg=self.AMBER_DIM, bg="#1c1308", activebackground=self.AMBER_DIM, activeforeground="#000000")
        self._rebuild_channel_list()
    def _add_to_recently_watched(self, ch_name):
        if not ch_name:
            return
        if ch_name in self._recently_watched:
            self._recently_watched.remove(ch_name)
        self._recently_watched.insert(0, ch_name)
        self._recently_watched = self._recently_watched[:self._recently_watched_max]
        self._resume_state['__recently_watched__'] = self._recently_watched
        _save_resume_state(self._resume_state)
    def _on_listbox_hover(self, event):
        if not self.channels:
            return
        idx = self.ch_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._listbox_ch_indices):
            self._ch_hover_label.config(text="")
            return
        ch_idx = self._listbox_ch_indices[idx]
        if ch_idx is None:
            return
        ch = self.channels[ch_idx]
        if ch.number == 0:
            self._ch_hover_label.config(text="Cable TV Guide")
            return
        lines = []
        if ch.description:
            lines.append(ch.description)
        if ch.channel_id and self._epg_loaded:
            current, _ = _get_epg_now_next(self._epg_data, ch.channel_id)
            if current:
                lines.append(f"▶ {_format_prog_title(current)[:44]}")
        if not lines:
            lines.append("PLAYLIST" if ch.is_local else "LIVE")
        self._ch_hover_label.config(text="\n".join(lines))
    def _surf_channel(self):
        if not self.channels:
            return
        recent_names = set(getattr(self, '_recently_watched', []))
        candidates = [i for i, ch in enumerate(self.channels)
                      if ch.number != 0 and ch.name not in recent_names]
        if not candidates:
            candidates = [i for i, ch in enumerate(self.channels) if ch.number != 0]
        if not candidates:
            return
        self._tune_channel(random.choice(candidates))

    # ══════════════════════════════════════════
    #  CHANNEL 0 — Retro Cable TV Guide
    # ══════════════════════════════════════════
    def _local_now_next_actual_idx(self, ch):
        """Return (now_idx, next_idx) actual playlist indices for a local channel,
        consistent whether or not it's the currently tuned channel."""
        ch_idx = next((i for i, c in enumerate(self.channels) if c is ch), None)
        if ch_idx is not None and ch_idx == self._last_local_channel_idx and self._local_track_order:
            # Currently playing — use the live play order/position
            order = self._local_track_order
            pos   = self._local_track_idx
        else:
            # Not tuned in — reconstruct from the last-known play order (if any)
            # and whatever track resume-state says was playing.
            order = self._channel_track_orders.get(ch.name) or list(range(len(ch.playlist)))
            pos = 0
            resume = self._resume_state.get(ch.name)
            if resume:
                fname = resume.get("filename")
                try:
                    now_actual_idx = ch.playlist.index(fname)
                    pos = order.index(now_actual_idx)
                except ValueError:
                    pos = 0
        if not order:
            return 0, 0
        now_idx  = order[pos % len(order)]
        next_idx = order[(pos + 1) % len(order)]
        return now_idx, next_idx
    def _get_guide_now_playing(self, ch):
        """Return program title for a channel — EPG data if available, else fallback."""
        # Try EPG first
        if ch.channel_id and self._epg_loaded:
            current, _ = _get_epg_now_next(self._epg_data, ch.channel_id)
            if current:
                return _format_prog_title(current)[:50]
        # Local playlist fallback
        if not ch.is_local:
            return "LIVE BROADCAST"
        if not ch.playlist:
            return "—"
        try:
            now_idx, _next_idx = self._local_now_next_actual_idx(ch)
            fname = ch.playlist[now_idx]
            name = os.path.splitext(os.path.basename(fname))[0]
            return name[:38] if len(name) > 38 else name
        except Exception:
            return "—"
    def _get_guide_up_next(self, ch):
        """Return the next track name for a local playlist channel, or '' if unavailable."""
        if not ch.is_local or not ch.playlist:
            return ''
        try:
            _now_idx, next_idx = self._local_now_next_actual_idx(ch)
            if next_idx >= len(ch.playlist):
                return ''
            fname = ch.playlist[next_idx]
            name = os.path.splitext(os.path.basename(fname))[0]
            return name[:38] if len(name) > 38 else name
        except Exception:
            return ''

    def _show_guide(self):
        """Create and display the split-screen guide overlay."""
        if self._guide_active: return
        self._guide_active = True
        self._guide_scroll_y = 0
        self._epg_scroll_y = 0
        self._epg_scroll_target = 0
        self._epg_selected_ch_idx = self.current_channel_idx
        self._guide_frame = tk.Frame(self.video_frame, bg=self.GUIDE_BG, highlightthickness=0)
        self._guide_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._guide_frame.tk.call('raise', self._guide_frame._w)
        # ── TOP THIRD: header with livestream preview ──────────
        self._guide_top_frame = tk.Frame(self._guide_frame, bg=self.GUIDE_HEADER_BG)
        self._guide_top_frame.place(relx=0, rely=0, relwidth=1, relheight=0.33)
        # ── BOTTOM TWO-THIRDS: EPG grid canvas ───────────────────────────
        self._guide_canvas = tk.Canvas(self._guide_frame, bg=self.GUIDE_BG, highlightthickness=0)
        self._guide_canvas.place(relx=0, rely=0.33, relwidth=1, relheight=0.67)
        self._guide_canvas.bind("<Configure>",  lambda e: self._draw_epg_grid())
        self._guide_canvas.bind("<MouseWheel>", self._epg_on_mousewheel)
        self._guide_canvas.bind("<Button-4>",   self._epg_on_mousewheel)
        self._guide_canvas.bind("<Button-5>",   self._epg_on_mousewheel)
        self._guide_canvas.bind("<Button-1>",   self._epg_on_click)
        # ── Info pane (left side of top third) ───────────────────────────
        self._guide_info_frame = tk.Frame(self._guide_top_frame, bg=self.GUIDE_HEADER_BG)
        self._guide_info_frame.place(relx=0, rely=0, relwidth=0.60, relheight=1.0)
        # ── Livestream preview (right side of top third) ──────────────────
        self._guide_video_frame = tk.Frame(self._guide_top_frame, bg="black",
                                           highlightthickness=2,
                                           highlightbackground=self.GUIDE_ACCENT)
        self._guide_video_frame.place(relx=0.62, rely=0.04, relwidth=0.36, relheight=0.92)
        try: title_font     = font.Font(family="Courier New", size=22, weight="bold")
        except: title_font  = font.Font(size=22, weight="bold")
        try: clock_font     = font.Font(family="Courier New", size=16, weight="bold")
        except: clock_font  = font.Font(size=16, weight="bold")
        try: cap_font       = font.Font(family="Courier New", size=12)
        except: cap_font    = font.Font(size=12)
        try: donate_font    = font.Font(family="Courier New", size=12)
        except: donate_font = font.Font(size=12)
        try: donate_btn_font    = font.Font(family="Courier New", size=12, weight="bold", underline=True)
        except: donate_btn_font = font.Font(size=12, weight="bold", underline=True)
        title_row = tk.Frame(self._guide_info_frame, bg=self.GUIDE_HEADER_BG)
        title_row.pack(fill=tk.X, padx=14, pady=(10, 0))
        tk.Label(title_row, text="📺  RETRO TV / IPTV Player", font=title_font,
                 fg=self.GUIDE_YELLOW, bg=self.GUIDE_HEADER_BG, anchor="nw").pack(side=tk.LEFT)
        tk.Label(title_row, text=f"v{APP_VERSION}", font=cap_font,
                 fg=self.GUIDE_ACCENT, bg=self.GUIDE_HEADER_BG, anchor="nw", padx=8).pack(side=tk.LEFT, pady=(8, 0))
        self._guide_time_label = tk.Label(self._guide_info_frame, text="",
                 font=clock_font, fg=self.GUIDE_WHITE, bg=self.GUIDE_HEADER_BG, anchor="nw")
        self._guide_time_label.pack(fill=tk.X, padx=14)
        caps_frame = tk.Frame(self._guide_info_frame, bg=self.GUIDE_HEADER_BG)
        caps_frame.pack(fill=tk.X, padx=14, pady=(4, 0))
        caps = [
            ("◉ LIVE STREAMS",    "Internet TV — always on"),
            ("◉ CUSTOM CHANNELS", "Play your own Playlists"),
            ("◉ COMMERCIAL BRK",  "Auto-inserts ads from commercials/ folder or commercials.xspf"),
            ("◉ CHANNEL GUIDE","Your Looking At It"),
            ("◉ OPEN SOURCE",     "Help make RetroTV Better"),
        ]
        for cap, detail in caps:
            row_frame = tk.Frame(caps_frame, bg=self.GUIDE_HEADER_BG)
            row_frame.pack(fill=tk.X, pady=0)
            tk.Label(row_frame, text=cap, font=cap_font,
                     fg=self.GUIDE_ACCENT, bg=self.GUIDE_HEADER_BG,
                     anchor="w", width=22).pack(side=tk.LEFT)
            tk.Label(row_frame, text=detail, font=cap_font,
                     fg=self.GUIDE_GRAY, bg=self.GUIDE_HEADER_BG,
                     anchor="w", wraplength=380, justify="left").pack(side=tk.LEFT, fill=tk.X, expand=True)
        donate_strip = tk.Frame(self._guide_info_frame, bg="#00004a",
                                highlightthickness=1, highlightbackground=self.GUIDE_ACCENT)
        donate_strip.pack(fill=tk.X, padx=14, pady=(8, 0))
        tk.Label(donate_strip,
                 text="♥  ENJOY RETRO TV?  EVERY DONATION KEEPS THE SIGNAL ALIVE!",
                 font=donate_font, fg=self.GUIDE_YELLOW, bg="#00004a",
                 padx=8, pady=5).pack(side=tk.LEFT, fill=tk.X, expand=True)
        donate_btn = tk.Label(donate_strip, text="[ DONATE NOW ]",
                 font=donate_btn_font, fg=self.GUIDE_GREEN, bg="#00004a",
                 padx=10, pady=5, cursor="hand2")
        donate_btn.pack(side=tk.RIGHT)
        donate_btn.bind("<Button-1>",   lambda e: webbrowser.open(self.DONATE_URL))
        donate_strip.bind("<Button-1>", lambda e: webbrowser.open(self.DONATE_URL))
        # ── Livestream player in the top-right preview pane ───────────────
        if VLC_AVAILABLE and self.vlc_instance:
            try:
                # Resolve preview URL: use configured URL or fall back to a random channel
                preview_url = self.GUIDE_LIVESTREAM_URL.strip()
                preview_ch_name = None   # set when we pick a random channel
                preview_ch_number = None  # channel number for click-to-tune
                if not preview_url:
                    live_chs  = [c for c in self.channels if c.number != 0 and c.url and not c.is_local]
                    local_chs = [c for c in self.channels if c.number != 0 and c.is_local and c.playlist]
                    if live_chs:
                        picked = random.choice(live_chs)
                        preview_url = picked.url
                        preview_ch_name   = f"CH {picked.number:02d}  {picked.name}"
                        preview_ch_number = picked.number
                    elif local_chs:
                        chosen = random.choice(local_chs)
                        preview_url = random.choice(chosen.playlist)
                        preview_ch_name   = f"CH {chosen.number:02d}  {chosen.name}"
                        preview_ch_number = chosen.number
                self._guide_player = self.vlc_instance.media_player_new()
                media = self.vlc_instance.media_new(preview_url)
                self._guide_player.set_media(media)
                self.update_idletasks()
                if sys.platform.startswith('win'):
                    self._guide_player.set_hwnd(self._guide_video_frame.winfo_id())
                elif sys.platform.startswith('linux'):
                    self._guide_player.set_xwindow(self._guide_video_frame.winfo_id())
                elif sys.platform == 'darwin':
                    self._guide_player.set_nsobject(self._guide_video_frame.winfo_id())
                self._guide_player.play()
                self._guide_player.audio_set_volume(self.vol_var.get())
                if getattr(self, '_muted', False):
                    self._guide_player.audio_set_mute(True)
                # Show which channel is playing in the preview pane if it was randomly chosen
                if preview_ch_name:
                    # Place on the parent frame (outside VLC's render surface)
                    # anchored to the bottom of the video pane position.
                    ch_ind = tk.Label(
                        self._guide_top_frame,
                        text=f"  {preview_ch_name}  ▶",
                        font=font.Font(family="Courier New", size=8, weight="bold"),
                        fg=self.GUIDE_YELLOW, bg="#000044",
                        anchor="w", pady=2, cursor="hand2")
                    ch_ind.place(relx=0.62, rely=0.96, anchor="sw", relwidth=0.36)
                    ch_ind.lift()
                    if preview_ch_number is not None:
                        preview_ch_idx = next((i for i, c in enumerate(self.channels) if c.number == preview_ch_number), None)
                        if preview_ch_idx is not None:
                            ch_ind.bind("<Button-1>", lambda e, n=preview_ch_idx: self._tune_channel(n))
            except Exception as e:
                print(f"[guide video] Error initializing player: {e}")
        self._guide_offline_label = tk.Label(
            self._guide_video_frame,
            text="📡\nSTREAM\nOFFLINE",
            font=font.Font(family="Courier New", size=9, weight="bold"),
            fg=self.GUIDE_RED, bg="black", justify="center"
        )
        self._guide_offline_label.place_forget()
        self._guide_player_err_count = 0
        self._guide_open_time = _time.monotonic()
        self.after(4000, self._guide_check_stream)
        # Initial draw and refresh loop
        self.after(150, self._draw_epg_grid)
        self._guide_refresh()
        self.live_label.config(text="▶ GUIDE")
        self.time_label.config(text="CH 00")
        self.ch_strip.config(text="  CH 00  ·  CABLE GUIDE  ·  INFO  ")
        self.local_row.pack_forget()
        self.filename_row.pack_forget()
    def _hide_guide(self):
        """Tear down the guide overlay."""
        if not self._guide_active: return
        self._guide_active = False
        for attr in ('_guide_after_id', '_guide_update_id', '_guide_comm_poll_id', '_epg_scroll_anim_id'):
            val = getattr(self, attr, None)
            if val:
                try: self.after_cancel(val)
                except Exception: pass
                setattr(self, attr, None)
        self._guide_in_commercial = False; self._guide_comm_queue = []
        self._guide_offline_label    = None
        self._guide_player_err_count = 0
        if self._guide_player:
            _gp = self._guide_player
            self._guide_player = None
            def _stop_guide_player(p=_gp):
                try: p.stop()
                except Exception: pass
                try: p.release()
                except Exception: pass
            threading.Thread(target=_stop_guide_player, daemon=True, name="guide-stop").start()
        if self._guide_frame:
            try: self._guide_frame.destroy()
            except Exception: pass
            self._guide_frame = None
        self._guide_canvas = None
    # ══════════════════════════════════════════
    #  EPG Grid Renderer
    # ══════════════════════════════════════════
    _EPG_ROW_H      = 52
    _EPG_CH_COL_W   = 160
    _EPG_TIME_HDR_H = 24
    _EPG_SLOT_MINS  = 30
    _EPG_SLOTS      = 6
    def _epg_slot_width(self, canvas_w):
        avail = max(1, canvas_w - self._EPG_CH_COL_W - 12)
        return max(80, avail // self._EPG_SLOTS)
    def _epg_window_start(self):
        now_local = datetime.datetime.now()
        snapped_local = now_local.replace(second=0, microsecond=0)
        snapped_local = snapped_local - datetime.timedelta(minutes=snapped_local.minute % 30)
        try:
            local_ts  = _time.mktime(snapped_local.timetuple())
            snapped_utc = datetime.datetime.fromtimestamp(local_ts, datetime.timezone.utc).replace(tzinfo=None)
        except Exception:
            utc_offset  = datetime.datetime.now() - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            snapped_utc = snapped_local - utc_offset
        return snapped_utc
    def _draw_epg_grid(self):
        c = self._guide_canvas
        if c is None or not self._guide_active: return
        try: c.tk.call('raise', c._w)
        except Exception: pass
        c.delete("all")
        W = c.winfo_width(); H = c.winfo_height()
        if W < 20 or H < 20: return
        if hasattr(self, '_guide_time_label') and self._guide_time_label.winfo_exists():
            now = datetime.datetime.now()
            if self._settings.get('clock_24h', False):
                self._guide_time_label.config(text=now.strftime("%H:%M:%S  —  %A, %B %d %Y"))
            else:
                self._guide_time_label.config(text=now.strftime("%I:%M:%S %p  —  %A, %B %d %Y"))
        slot_w   = self._epg_slot_width(W)
        ch_col_w = self._EPG_CH_COL_W
        hdr_h    = self._EPG_TIME_HDR_H
        row_h    = self._EPG_ROW_H
        sb_w     = 10
        win_start = self._epg_window_start()
        total_slot_w = slot_w * self._EPG_SLOTS
        c.create_rectangle(0, 0, W, H, fill=self.GUIDE_BG, outline="")
        c.create_rectangle(0, 0, W, hdr_h, fill="#0000aa", outline="")
        c.create_rectangle(0, 0, ch_col_w, hdr_h, fill="#0000cc", outline="")
        c.create_text(ch_col_w // 2, hdr_h // 2, text="CHANNEL",
                      font=("Courier New", 8, "bold"), fill=self.GUIDE_YELLOW, anchor="center")
        for slot_i in range(self._EPG_SLOTS):
            slot_time = win_start + datetime.timedelta(minutes=slot_i * self._EPG_SLOT_MINS)
            try:
                ts = slot_time.replace(tzinfo=datetime.timezone.utc).timestamp()
                local_slot = datetime.datetime(*_time.localtime(int(ts))[:6])
            except Exception:
                local_slot = slot_time
            time_str = local_slot.strftime('%I:%M %p').lstrip('0')
            sx = ch_col_w + slot_i * slot_w
            c.create_rectangle(sx, 0, sx + slot_w, hdr_h, fill="#0000aa",
                                outline="#000055", width=1)
            c.create_text(sx + 6, hdr_h // 2, text=time_str,
                          font=("Courier New", 8, "bold"), fill=self.GUIDE_ACCENT, anchor="w")
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        elapsed_secs = (now_utc - win_start).total_seconds()
        total_secs = self._EPG_SLOTS * self._EPG_SLOT_MINS * 60
        if 0 <= elapsed_secs <= total_secs:
            now_x = ch_col_w + int(elapsed_secs / total_secs * total_slot_w)
            c.create_line(now_x, hdr_h, now_x, H, fill=self.GUIDE_RED, width=2, dash=(4, 3))
        if not self._epg_loaded:
            status_y = hdr_h + 6
            status_col = self.GUIDE_YELLOW if self._epg_loading else self.GUIDE_RED
            c.create_text(ch_col_w + 10, status_y + 8,
                          text=self._epg_status_msg,
                          font=("Courier New", 9), fill=status_col, anchor="w")
        content_top = hdr_h
        visible_h   = H - content_top
        total_ch_h  = len(self.channels) * row_h
        max_scroll  = max(0, total_ch_h - visible_h)
        self._epg_scroll_y = max(0, min(self._epg_scroll_y, max_scroll))
        start_row = max(0, int(self._epg_scroll_y // row_h) - 1)
        end_row   = min(len(self.channels), int((self._epg_scroll_y + visible_h) // row_h) + 2)
        fn_ch   = ("Courier New", 9,  "bold")
        fn_prog = ("Courier New", 8)
        fn_time = ("Courier New", 7)
        for i in range(start_row, end_row):
            ch    = self.channels[i]
            row_y = content_top + i * row_h - int(self._epg_scroll_y)
            if row_y + row_h < content_top or row_y > H: continue
            is_current  = (i == self.current_channel_idx)
            is_selected = (i == self._epg_selected_ch_idx)
            row_bg = ("#003366" if is_current else
                      "#001a44" if is_selected else
                      (self.GUIDE_ROW_A if i % 2 == 0 else self.GUIDE_ROW_B))
            c.create_rectangle(0, row_y, W - sb_w, row_y + row_h,
                                fill=row_bg, outline="")
            pad_t, pad_b, pad_l, pad_r = 2, 2, 2, 4
            bx0 = pad_l; by0 = row_y + pad_t
            bx1 = ch_col_w - pad_r; by1 = row_y + row_h - pad_b
            if is_current:
                box_fill    = "#0000c8"; box_outline = self.GUIDE_YELLOW; shine_col = "#4444ff"
            elif is_selected:
                box_fill    = "#00008a"; box_outline = self.GUIDE_ACCENT; shine_col = "#2222cc"
            else:
                box_fill    = "#00006a"; box_outline = "#0000aa"; shine_col = "#1111aa"
            c.create_rectangle(bx0 + 1, by0 + 1, bx1 + 1, by1 + 1, fill="#00001a", outline="")
            c.create_rectangle(bx0, by0, bx1, by1, fill=box_fill, outline=box_outline, width=1)
            c.create_line(bx0 + 1, by0 + 1, bx1 - 1, by0 + 1, fill=shine_col, width=1)
            c.create_line(ch_col_w, row_y, ch_col_w, row_y + row_h, fill="#000033", width=2)
            badge_x = bx0 + 5
            badge_col = self.GUIDE_YELLOW if is_current else self.GUIDE_ACCENT
            c.create_text(badge_x, by0 + 10, anchor="w", text=f"{ch.number:02d}",
                          font=("Courier New", 9, "bold"), fill=badge_col)
            if self._is_favorite(ch.number):
                c.create_text(badge_x + 22, by0 + 10, anchor="w", text="★", font=fn_ch, fill=self.GUIDE_YELLOW)
            if is_current:
                c.create_text(badge_x, by0 + 24, anchor="w", text="▶ ON AIR",
                              font=("Courier New", 7, "bold"), fill=self.GUIDE_GREEN)
            ch_name = ch.name
            max_name_chars = max(6, (ch_col_w - 14) // 6)
            name_y = by1 - 18
            if len(ch_name) > max_name_chars:
                line1 = ch_name[:max_name_chars]
                line2 = ch_name[max_name_chars:max_name_chars*2]
                c.create_text(badge_x, name_y, anchor="w", text=line1, font=fn_ch,
                              fill=self.GUIDE_WHITE if is_current else self.BEIGE)
                c.create_text(badge_x, name_y + 12, anchor="w", text=line2, font=fn_ch,
                              fill=self.GUIDE_WHITE if is_current else self.BEIGE)
            else:
                c.create_text(badge_x, name_y + 6, anchor="w", text=ch_name, font=fn_ch,
                              fill=self.GUIDE_WHITE if is_current else self.BEIGE)
            icon_w, icon_h = self._EPG_ICON_W, self._EPG_ICON_H
            icon_cx = bx1 - icon_w // 2 - 2
            icon_cy = (by0 + by1) // 2
            icon_x0 = icon_cx - icon_w // 2; icon_y0 = icon_cy - icon_h // 2
            img = self._get_epg_channel_image(ch.channel_id) if ch.channel_id else None
            if img is not None:
                c.create_rectangle(icon_x0 - 1, icon_y0 - 1, icon_x0 + icon_w + 1, icon_y0 + icon_h + 1,
                                   fill="white", outline="#aaaaaa", width=1)
                c.create_image(icon_cx, icon_cy, image=img, anchor="center")
            else:
                self._draw_generic_tv_icon(c, icon_cx, icon_cy)
            if ch.is_local:
                self._draw_epg_local_cell(c, ch, ch_col_w, row_y, total_slot_w, row_h, fn_prog, fn_time)
            elif ch.number == 0:
                self._draw_epg_info_cell(c, "CHANNEL GUIDE", "Interactive TV Guide", ch_col_w, row_y, total_slot_w, row_h)
            elif not self._epg_loaded:
                self._draw_epg_loading_cell(c, ch_col_w, row_y, total_slot_w, row_h)
            elif not ch.channel_id or ch.channel_id not in self._epg_data:
                self._draw_epg_no_data_cell(c, ch_col_w, row_y, total_slot_w, row_h,
                                            description=ch.description)
            else:
                progs = self._epg_data[ch.channel_id]
                self._draw_epg_programme_cells(c, progs, win_start, total_secs,
                                               ch_col_w, total_slot_w, row_y, row_h,
                                               slot_w, fn_prog, fn_time,
                                               description=ch.description,
                                               channel_id=ch.channel_id or "")
        if total_ch_h > visible_h:
            sb_x = W - sb_w
            thumb_h = max(20, int(visible_h * visible_h / total_ch_h))
            thumb_y = (content_top + int((self._epg_scroll_y / max_scroll) * (visible_h - thumb_h))
                       if max_scroll > 0 else content_top)
            c.create_rectangle(sb_x, content_top, sb_x + sb_w, H, fill="#000044", outline="")
            c.create_rectangle(sb_x + 1, thumb_y, sb_x + sb_w - 1, thumb_y + thumb_h, fill=self.GUIDE_ACCENT, outline="")
        c.create_rectangle(0, 0, 0, H, fill="", outline="")
    def _draw_epg_programme_cells(self, c, progs, win_start, total_secs,
                                   ch_col_w, total_slot_w, row_y, row_h,
                                   slot_w, fn_prog, fn_time, description="", channel_id=""):
        grid_x0 = ch_col_w; grid_x1 = ch_col_w + total_slot_w
        win_end  = win_start + datetime.timedelta(seconds=total_secs)
        cell_colours = ["#003070", "#004080", "#003888", "#004470"]
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        drawn_any = False
        for idx, prog in enumerate(progs):
            p_start = prog['start']
            p_stop  = prog.get('stop') or (p_start + datetime.timedelta(hours=1))
            if p_stop <= win_start or p_start >= win_end: continue
            drawn_any = True
            draw_start = max(p_start, win_start); draw_stop  = min(p_stop,  win_end)
            x0 = grid_x0 + int((draw_start - win_start).total_seconds() / total_secs * total_slot_w)
            x1 = grid_x0 + int((draw_stop  - win_start).total_seconds() / total_secs * total_slot_w)
            x0 = max(grid_x0, x0); x1 = min(grid_x1, x1)
            if x1 - x0 < 2: continue
            is_now = (p_start <= now_utc < p_stop)
            cell_bg = "#002255" if is_now else cell_colours[idx % len(cell_colours)]
            border_col = self.GUIDE_ACCENT if is_now else "#0044aa"
            c.create_rectangle(x0 + 1, row_y + 2, x1 - 1, row_y + row_h - 3,
                                fill=cell_bg, outline=border_col, width=1)
            if is_now:
                frac = _get_progress_fraction(prog)
                if frac is not None and frac > 0:
                    bar_x1 = x0 + 2 + int((x1 - x0 - 4) * frac)
                    c.create_rectangle(x0 + 2, row_y + row_h - 6, bar_x1, row_y + row_h - 3,
                                       fill=self.GUIDE_GREEN, outline="")
            cell_w = x1 - x0; title  = _format_prog_title(prog)
            max_chars = max(3, (cell_w - 8) // 6)
            if len(title) > max_chars: title = title[:max_chars - 1] + "…"
            if cell_w > 30:
                title_col = self.GUIDE_WHITE if is_now else "#ccddff"
                c.create_text(x0 + 5, row_y + 12, anchor="w", text=title, font=fn_prog, fill=title_col)
            if cell_w > 70:
                time_str = _format_time_range(prog)
                if len(time_str) > (cell_w - 8) // 5: time_str = time_str[:max(0, (cell_w - 8) // 5)]
                c.create_text(x0 + 5, row_y + 26, anchor="w", text=time_str, font=fn_time, fill=self.GUIDE_GRAY)
            if cell_w > 130 and prog.get('category'):
                cat = prog['category'][:12].upper()
                c.create_text(x0 + 5, row_y + 38, anchor="w", text=f"[{cat}]", font=fn_time, fill="#6688cc")
            # Scheduled-recording indicator: red dot in top-right corner of cell
            if channel_id and prog.get('start'):
                try:
                    if self._dvr_is_scheduled(channel_id, prog['start'].isoformat()):
                        dot_r = 5
                        dot_cx = max(x0 + dot_r + 2, x1 - dot_r - 4)
                        dot_cy = row_y + dot_r + 4
                        c.create_oval(dot_cx - dot_r, dot_cy - dot_r,
                                      dot_cx + dot_r, dot_cy + dot_r,
                                      fill="#cc0000", outline="#ff4444", width=1)
                except Exception:
                    pass
        if not drawn_any: self._draw_epg_no_data_cell(c, ch_col_w, row_y, total_slot_w, row_h, description=description)
    def _draw_epg_local_cell(self, c, ch, ch_col_w, row_y, total_slot_w, row_h, fn_prog, fn_time):
        cell_x0 = ch_col_w + 2
        cell_x1 = ch_col_w + total_slot_w - 2
        cell_w  = cell_x1 - cell_x0

        now_playing = self._get_guide_now_playing(ch)
        up_next     = self._get_guide_up_next(ch)

        # Decide split: if we have an "up next" title and there's enough room, show two cells
        show_split = bool(up_next) and cell_w >= 120
        now_x1 = (cell_x0 + cell_w // 2) if show_split else cell_x1

        # ── NOW cell — same colours as EPG is_now ─────────────────────────
        c.create_rectangle(cell_x0, row_y + 2, now_x1, row_y + row_h - 3,
                           fill="#002255", outline=self.GUIDE_ACCENT, width=1)
        now_w     = now_x1 - cell_x0
        max_now   = max(3, (now_w - 16) // 6)
        now_title = now_playing[:max_now] + ("…" if len(now_playing) > max_now else "")
        c.create_text(cell_x0 + 5, row_y + 12, anchor="w",
                      text=now_title, font=fn_prog, fill=self.GUIDE_WHITE)
        now_label = "▶ NOW  (shuffle)" if ch.randomize else "▶ NOW"
        c.create_text(cell_x0 + 5, row_y + 26, anchor="w",
                      text=now_label, font=fn_time, fill=self.GUIDE_GRAY)
        # Progress bar using live VLC playhead (same style as EPG progress bar)
        ch_idx = next((i for i, c2 in enumerate(self.channels) if c2 is ch), None)
        if (ch_idx is not None and ch_idx == self._last_local_channel_idx
                and not self._in_commercial):
            t_ms   = getattr(self, '_vlc_time_ms', 0)
            len_ms = getattr(self, '_vlc_len_ms', 0)
            if len_ms and len_ms > 0 and t_ms >= 0:
                frac = max(0.0, min(1.0, t_ms / len_ms))
                if frac > 0:
                    bar_x1 = cell_x0 + 2 + int((now_x1 - cell_x0 - 4) * frac)
                    c.create_rectangle(cell_x0 + 2, row_y + row_h - 6,
                                       bar_x1, row_y + row_h - 3,
                                       fill=self.GUIDE_GREEN, outline="")

        # ── UP NEXT cell — same colours as EPG non-now ────────────────────
        if show_split:
            next_x0    = now_x1 + 2
            next_bg    = "#003070"   # first entry in cell_colours palette
            next_border = "#0044aa"
            c.create_rectangle(next_x0, row_y + 2, cell_x1, row_y + row_h - 3,
                               fill=next_bg, outline=next_border, width=1)
            next_w     = cell_x1 - next_x0
            max_next   = max(3, (next_w - 16) // 6)
            next_title = up_next[:max_next] + ("…" if len(up_next) > max_next else "")
            c.create_text(next_x0 + 5, row_y + 12, anchor="w",
                          text=next_title, font=fn_prog, fill="#ccddff")
            c.create_text(next_x0 + 5, row_y + 26, anchor="w",
                          text="NEXT ▶", font=fn_time, fill=self.GUIDE_GRAY)
    def _draw_epg_info_cell(self, c, title, subtitle, ch_col_w, row_y, total_slot_w, row_h):
        x0 = ch_col_w + 2; x1 = ch_col_w + total_slot_w - 2
        c.create_rectangle(x0, row_y + 2, x1, row_y + row_h - 3, fill="#000055", outline=self.GUIDE_ACCENT, width=1)
        c.create_text(x0 + 8, row_y + 14, anchor="w", text=title, font=("Courier New", 9, "bold"), fill=self.GUIDE_YELLOW)
        c.create_text(x0 + 8, row_y + 30, anchor="w", text=subtitle, font=("Courier New", 7), fill=self.GUIDE_GRAY)
    def _draw_epg_loading_cell(self, c, ch_col_w, row_y, total_slot_w, row_h):
        x0 = ch_col_w + 2; x1 = ch_col_w + total_slot_w - 2
        c.create_rectangle(x0, row_y + 2, x1, row_y + row_h - 3, fill="#00003a", outline="#000088", width=1)
        c.create_text(x0 + 8, row_y + row_h // 2, anchor="w", text="⌛  Loading schedule...", font=("Courier New", 8), fill="#4466aa")
    def _draw_epg_no_data_cell(self, c, ch_col_w, row_y, total_slot_w, row_h, description=""):
        x0 = ch_col_w + 2; x1 = ch_col_w + total_slot_w - 2
        cell_w = x1 - x0
        # Need at least enough room for two cells to be meaningful
        if cell_w < 40:
            return
        label      = description or "LIVE BROADCAST"
        # Second cell uses a continuation label so the two blocks look distinct
        labels     = [label, "CONTINUED"]
        fills      = ["#003070", "#004080"]
        borders    = ["#0044aa", "#0044aa"]
        # Clamp mid so each half is at least 40px wide
        min_half   = 40
        mid        = max(x0 + min_half, min(x0 + cell_w // 2, x1 - min_half))
        segments   = [(x0, mid - 1), (mid + 1, x1)]
        # Build fake time strings to match real EPG two-row layout
        now_local  = datetime.datetime.now()
        slot_mins  = self._EPG_SLOT_MINS
        half_delta = datetime.timedelta(minutes=slot_mins * self._EPG_SLOTS // 2)
        t1_start   = now_local.replace(second=0, microsecond=0) - \
                     datetime.timedelta(minutes=now_local.minute % slot_mins)
        t1_end     = t1_start + half_delta
        t2_end     = t1_end   + half_delta
        time_strs  = [
            f"{t1_start.strftime('%I:%M %p').lstrip('0')} – {t1_end.strftime('%I:%M %p').lstrip('0')}",
            f"{t1_end.strftime('%I:%M %p').lstrip('0')} – {t2_end.strftime('%I:%M %p').lstrip('0')}",
        ]
        for i, (cx0, cx1) in enumerate(segments):
            cw = cx1 - cx0
            if cw < 4:
                continue
            c.create_rectangle(cx0, row_y + 2, cx1, row_y + row_h - 3,
                               fill=fills[i], outline=borders[i], width=1)
            if cw > 30:
                max_title = max(3, (cw - 10) // 6)
                title_text = labels[i] if len(labels[i]) <= max_title else labels[i][:max_title - 1] + "…"
                c.create_text(cx0 + 5, row_y + 12, anchor="w",
                              text=title_text, font=("Courier New", 8), fill="#ccddff")
            if cw > 70:
                max_time = max(3, (cw - 10) // 5)
                time_text = time_strs[i] if len(time_strs[i]) <= max_time else time_strs[i][:max_time]
                c.create_text(cx0 + 5, row_y + 26, anchor="w",
                              text=time_text, font=("Courier New", 7), fill=self.GUIDE_GRAY)
    # ── EPG scroll / interaction ───────────────────────────────────────────
    def _epg_on_mousewheel(self, event):
        if event.num == 4 or (hasattr(event, 'delta') and event.delta > 0): delta = -self._EPG_ROW_H
        else: delta = self._EPG_ROW_H
        self._epg_scroll_y = max(0, self._epg_scroll_y + delta)
        self._draw_epg_grid()
    def _epg_on_click(self, event):
        if self._guide_canvas is None: return
        W = self._guide_canvas.winfo_width(); sb_w = 10
        if event.x >= W - sb_w:
            self._epg_on_scrollbar_click(event)
            return
        hdr_h = self._EPG_TIME_HDR_H; row_h = self._EPG_ROW_H; y = event.y
        if y < hdr_h: return
        row = int((y - hdr_h + self._epg_scroll_y) // row_h)
        if 0 <= row < len(self.channels):
            ch = self.channels[row]
            self._epg_selected_ch_idx = row
            # Click in the programme area on a live EPG channel → show detail popup
            if (event.x > self._EPG_CH_COL_W
                    and not ch.is_local and ch.number != 0
                    and self._epg_loaded and ch.url):
                prog = self._epg_prog_at_click(ch, event.x)
                if prog:
                    self._draw_epg_grid()
                    self._epg_show_prog_popup(ch, prog)
                    return
            self._draw_epg_grid()
            if row != self.current_channel_idx:
                self._tune_channel(row)
    def _epg_on_scrollbar_click(self, event):
        if self._guide_canvas is None: return
        H = self._guide_canvas.winfo_height(); hdr_h = self._EPG_TIME_HDR_H
        visible_h   = H - hdr_h
        total_ch_h  = len(self.channels) * self._EPG_ROW_H
        max_scroll  = max(0, total_ch_h - visible_h)
        if max_scroll <= 0: return
        click_y = max(hdr_h, event.y)
        frac = (click_y - hdr_h) / max(1, visible_h)
        self._epg_scroll_y = max(0, min(max_scroll, int(frac * max_scroll)))
        self._draw_epg_grid()
    def _guide_refresh(self):
        if not self._guide_active: return
        self._draw_epg_grid()
        self._guide_update_id = self.after(10000, self._guide_refresh)
    def _guide_check_stream(self):
        if not self._guide_active: return
        if self._guide_player is None:
            self.after(2000, self._guide_check_stream); return
        try: state = self._guide_player.get_state()
        except Exception:
            self.after(2000, self._guide_check_stream); return
        healthy_states = {vlc.State.Playing, vlc.State.Opening, vlc.State.Buffering}
        startup_states = {vlc.State.Stopped, vlc.State.NothingSpecial}
        elapsed_since_open = _time.monotonic() - getattr(self, '_guide_open_time', 0.0)
        grace_period = 6.0
        if state in healthy_states:
            self._guide_player_err_count = 0
        elif state == vlc.State.Error:
            self._guide_player_err_count += 1
        elif state in startup_states and elapsed_since_open < grace_period:
            pass
        else:
            self._guide_player_err_count += 1
        if self._guide_player_err_count >= 2 and self._guide_offline_label:
            self._guide_offline_label.place(relx=0.5, rely=0.5, anchor="center")
            self._guide_offline_label.lift()
        elif self._guide_player_err_count == 0 and self._guide_offline_label:
            self._guide_offline_label.place_forget()
        self.after(2000, self._guide_check_stream)
    # ══════════════════════════════════════════
    #  Watermark / Network Bug
    # ══════════════════════════════════════════
    def _update_watermark(self):
        if self._watermark_label is not None:
            try:
                self._watermark_label.place_forget()
                self._watermark_label.destroy()
            except Exception: pass
            self._watermark_label = None; self._watermark_image = None
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if not ch.watermark: return
        screen_w = self.video_frame.winfo_width()
        if screen_w < 20: screen_w = 960
        target_w = max(40, min(120, int(screen_w * 0.08)))
        try:
            from PIL import Image, ImageTk
            img = Image.open(ch.watermark).convert("RGBA")
            ratio = target_w / img.width; target_h = max(1, int(img.height * ratio))
            img   = img.resize((target_w, target_h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img); bg_colour = "black"
        except ImportError:
            try:
                photo = tk.PhotoImage(file=ch.watermark)
                orig_w = photo.width()
                if orig_w > 0 and orig_w > target_w:
                    factor = max(1, round(orig_w / target_w))
                    photo  = photo.subsample(factor, factor)
                bg_colour = "black"
            except Exception as e:
                print(f"[watermark] failed to load '{ch.watermark}': {e}"); return
        except Exception as e:
            print(f"[watermark] failed to load '{ch.watermark}': {e}"); return
        self._watermark_image = photo
        lbl = tk.Label(self.video_frame, image=photo, bg=bg_colour, bd=0,
                       highlightthickness=0, cursor="none")
        lbl.place(relx=1.0, rely=1.0, anchor="se", x=-8, y=-8)
        lbl.lift()
        self._watermark_label = lbl
    def _on_watermark_resize(self, event=None):
        if self._watermark_resize_after is not None:
            self.after_cancel(self._watermark_resize_after)
            self._watermark_resize_after = None
        if not self.channels: return
        ch = self.channels[self.current_channel_idx]
        if ch.watermark:
            self._watermark_resize_after = self.after(150, self._update_watermark)
    # ══════════════════════════════════════════
    #  Keyboard Shortcut Overlay
    # ══════════════════════════════════════════
    # ══════════════════════════════════════════
    #  Settings
    # ══════════════════════════════════════════
    def _apply_settings(self):
        """Apply settings that can take effect without a restart."""
        self.attributes('-topmost', self._settings.get('always_on_top', False))

    def _open_settings_dialog(self):
        if getattr(self, '_settings_open', False):
            return
        self._settings_open = True

        BG       = '#1a1008'
        FG       = '#ffb000'
        FG_DIM   = '#c07800'
        ENTRY_BG = '#120b04'
        BTN_BG   = '#2a1a04'
        FONT_LG  = ('Courier New', 11, 'bold')
        FONT_SM  = ('Courier New', 10)

        d = tk.Toplevel(self)
        d.title("⚙  RetroTV Settings")
        d.configure(bg=BG)
        d.resizable(False, False)
        d.grab_set()

        def on_close():
            self._settings_open = False
            d.destroy()
        d.protocol("WM_DELETE_WINDOW", on_close)

        s = dict(self._settings)

        # ── helpers ───────────────────────────────────────────────────────
        def section(parent, label):
            tk.Label(parent, text=f"── {label} ──", font=FONT_LG,
                     bg=BG, fg=FG, anchor='w').pack(fill=tk.X, padx=12, pady=(14, 2))

        def row(parent):
            f = tk.Frame(parent, bg=BG)
            f.pack(fill=tk.X, padx=20, pady=2)
            return f

        def lbl(parent, text, width=32):
            tk.Label(parent, text=text, font=FONT_SM, bg=BG, fg=FG_DIM,
                     anchor='w', width=width).pack(side=tk.LEFT)

        def int_entry(parent, key, width=6):
            var = tk.StringVar(value=str(s[key]))
            e = tk.Entry(parent, textvariable=var, width=width, font=FONT_SM,
                         bg=ENTRY_BG, fg=FG, insertbackground=FG,
                         relief='flat', bd=1, highlightthickness=1,
                         highlightbackground=FG_DIM, highlightcolor=FG)
            e.pack(side=tk.LEFT, padx=4)
            return var

        def check(parent, key, text=''):
            var = tk.BooleanVar(value=bool(s[key]))
            cb = tk.Checkbutton(parent, text=text, variable=var, font=FONT_SM,
                                bg=BG, fg=FG, selectcolor=ENTRY_BG,
                                activebackground=BG, activeforeground=FG,
                                relief='flat', bd=0)
            cb.pack(side=tk.LEFT)
            return var

        outer = tk.Frame(d, bg=BG)
        outer.pack(fill=tk.BOTH, padx=8, pady=8)

        # ── Commercials ───────────────────────────────────────────────────
        section(outer, "COMMERCIALS PER BREAK")

        r = row(outer)
        lbl(r, "Regular channels  min / max:")
        v_cmin = int_entry(r, 'commercials_min', 4)
        tk.Label(r, text='/', bg=BG, fg=FG_DIM, font=FONT_SM).pack(side=tk.LEFT)
        v_cmax = int_entry(r, 'commercials_max', 4)

        # ── EPG ───────────────────────────────────────────────────────────
        section(outer, "GUIDE / EPG")

        r = row(outer)
        lbl(r, "EPG refresh interval (hours):")
        v_epg = int_entry(r, 'epg_refresh_hours', 4)

        r = row(outer)
        lbl(r, "24-hour clock in guide:")
        v_24h = check(r, 'clock_24h')

        # ── Playback ──────────────────────────────────────────────────────
        section(outer, "PLAYBACK  (restart required)")

        r = row(outer)
        lbl(r, "Hardware acceleration:")
        v_hw = check(r, 'hw_accel')

        r = row(outer)
        lbl(r, "Network / live buffer (ms):")
        v_buf = int_entry(r, 'buffer_ms', 6)

        # ── Startup ───────────────────────────────────────────────────────
        section(outer, "STARTUP")

        r = row(outer)
        lbl(r, "Resume last-watched channel:")
        v_last = check(r, 'start_last_channel')

        r = row(outer)
        lbl(r, "Start fullscreen:")
        v_fs = check(r, 'start_fullscreen')

        # ── Window ────────────────────────────────────────────────────────
        section(outer, "WINDOW")

        r = row(outer)
        lbl(r, "Always on top:")
        v_top = check(r, 'always_on_top')

        r = row(outer)
        lbl(r, "Suppress screensaver (fullscreen):")
        v_ss = check(r, 'suppress_screensaver')

        # ── Channels ──────────────────────────────────────────────────────
        section(outer, "CHANNELS")

        r = row(outer)
        lbl(r, "Use curated channel list:")
        v_curated = check(r, 'use_curated_channels')
        tk.Label(r, text='(auto-updates lineup from remote)', font=FONT_SM,
                 bg=BG, fg=FG_DIM).pack(side=tk.LEFT, padx=(6, 0))

        r = row(outer)
        lbl(r, "Import M3U playlist:")
        def _settings_import_m3u():
            def _on_imported(nc):
                user_config_path = os.path.join(self.base_dir, "user_config.json")
                try:
                    existing = _load_user_config_dict(user_config_path)
                    existing["channels"].extend(nc)
                    with open(user_config_path, 'w', encoding='utf-8') as f:
                        json.dump(existing, f, indent=2, ensure_ascii=False)
                    self.channels = []
                    self._load_config()
                    self._rebuild_channel_list()
                    self._show_update_banner(f"✦ IMPORTED {len(nc)} CHANNEL(S) ✦")
                except Exception as e:
                    log.error("Settings M3U import error: %s", e)
            self._show_m3u_import_dialog(d, _on_imported)
        tk.Button(r, text="⬇ Import M3U…", command=_settings_import_m3u,
                  font=FONT_SM, bg=BTN_BG, fg=FG,
                  activebackground='#3a2a08', activeforeground=FG,
                  relief='flat', bd=1, highlightthickness=1,
                  highlightbackground=FG_DIM, cursor='hand2',
                  padx=10, pady=4).pack(side=tk.LEFT, padx=4)

        # ── Buttons ───────────────────────────────────────────────────────
        tk.Frame(outer, bg=FG_DIM, height=1).pack(fill=tk.X, padx=12, pady=(14, 0))
        btn_row = tk.Frame(outer, bg=BG)
        btn_row.pack(pady=10)

        def _save():
            try:
                self._settings['commercials_min']   = max(0, int(v_cmin.get()))
                self._settings['commercials_max']   = max(0, int(v_cmax.get()))
                self._settings['epg_refresh_hours'] = max(1, int(v_epg.get()))
                self._settings['buffer_ms']             = max(100, int(v_buf.get()))
            except ValueError:
                tk.messagebox.showerror("Invalid value",
                    "Min/max counts and buffer size must be whole numbers.",
                    parent=d)
                return
            self._settings['clock_24h']            = v_24h.get()
            self._settings['hw_accel']             = v_hw.get()
            self._settings['start_last_channel']   = v_last.get()
            self._settings['start_fullscreen']     = v_fs.get()
            self._settings['always_on_top']        = v_top.get()
            self._settings['suppress_screensaver'] = v_ss.get()
            self._settings['use_curated_channels'] = v_curated.get()
            _save_settings_file(self._settings)
            self._apply_settings()
            on_close()

        def _cancel():
            on_close()

        btn_kw = dict(font=FONT_LG, bg=BTN_BG, fg=FG, activebackground='#3a2a08',
                      activeforeground=FG, relief='flat', bd=1,
                      highlightthickness=1, highlightbackground=FG_DIM,
                      cursor='hand2', padx=14, pady=4)
        tk.Button(btn_row, text="✔  SAVE", command=_save,   **btn_kw).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="✘  CANCEL", command=_cancel, **btn_kw).pack(side=tk.LEFT, padx=8)
        self._center_dialog(d)

    # ══════════════════════════════════════════
    #  M3U Import Dialog (shared)
    # ══════════════════════════════════════════
    def _show_m3u_import_dialog(self, parent, on_success):
        """Open the M3U import dialog. on_success(channels) is called with the imported list."""
        imp = tk.Toplevel(parent)
        self._apply_icon(imp)
        imp.title("Import M3U Playlist")
        imp.configure(bg="#1c1308")
        imp.resizable(True, False)
        imp.grab_set()
        imp.focus_force()
        parent.update_idletasks()
        iw, ih = 580, 230
        imp.geometry(f"{iw}x{ih}+{parent.winfo_rootx()+(parent.winfo_width()-iw)//2}+{parent.winfo_rooty()+(parent.winfo_height()-ih)//2}")
        imp.minsize(460, 210)
        border2 = tk.Frame(imp, bg=self.AMBER)
        border2.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner2 = tk.Frame(border2, bg="#1c1308")
        inner2.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        title2 = tk.Frame(inner2, bg=self.AMBER_DIM)
        title2.pack(fill=tk.X)
        tk.Label(title2, text="  ⬇  IMPORT M3U PLAYLIST", font=self.font_small,
                 fg=self.AMBER, bg=self.AMBER_DIM, anchor="w", pady=6).pack(fill=tk.X)
        body2 = tk.Frame(inner2, bg="#1c1308")
        body2.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        src_row = tk.Frame(body2, bg="#1c1308")
        src_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(src_row, text="Source:", font=self.font_ch, fg=self.AMBER_DIM, bg="#1c1308",
                 width=9, anchor="w").pack(side=tk.LEFT)
        var_isrc = tk.StringVar(value="file")
        for val, lbl in (("file", "  Local File  "), ("url", "  Remote URL  ")):
            tk.Radiobutton(src_row, text=lbl, variable=var_isrc, value=val,
                           bg="#1c1308", fg=self.AMBER, activebackground="#1c1308",
                           activeforeground=self.AMBER, indicatoron=False, relief=tk.FLAT, bd=1,
                           font=self.font_ch, padx=8, pady=3, cursor="hand2",
                           highlightthickness=1, highlightbackground=self.AMBER_DIM,
                           selectcolor=self.AMBER_DIM).pack(side=tk.LEFT, padx=(0, 4))
        path_row = tk.Frame(body2, bg="#1c1308")
        path_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(path_row, text="Path/URL:", font=self.font_ch, fg=self.AMBER_DIM, bg="#1c1308",
                 width=9, anchor="w").pack(side=tk.LEFT)
        var_ipath = tk.StringVar()
        ent_ipath = tk.Entry(path_row, textvariable=var_ipath, bg="#0d0a06", fg=self.AMBER,
                             insertbackground=self.AMBER, font=self.font_ch, relief=tk.FLAT, bd=0,
                             highlightbackground=self.AMBER_DIM, highlightthickness=1)
        ent_ipath.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        def browse_m3u():
            from tkinter import filedialog
            p = filedialog.askopenfilename(parent=imp, title="Select M3U Playlist",
                filetypes=[("M3U Playlist", "*.m3u *.m3u8"), ("All files", "*.*")])
            if p:
                var_ipath.set(p)
                ent_ipath.focus_set()
        btn_browse_m3u = tk.Button(path_row, text="Browse…", command=browse_m3u,
                                   bg="#2a1f10", fg=self.AMBER, activebackground=self.AMBER_DIM,
                                   font=self.font_ch, relief=tk.FLAT, padx=6, pady=2,
                                   cursor="hand2", bd=0)
        btn_browse_m3u.pack(side=tk.LEFT)
        def on_isrc_change(*_):
            if var_isrc.get() == "url":
                btn_browse_m3u.pack_forget()
            else:
                btn_browse_m3u.pack(side=tk.LEFT)
        var_isrc.trace_add("write", on_isrc_change)
        imp_status = tk.Label(body2, text="", font=self.font_ch, fg=self.GUIDE_GREEN,
                              bg="#0d0a06", anchor="w", padx=6, pady=4)
        imp_status.pack(fill=tk.X, pady=(4, 0))
        btn_row2 = tk.Frame(inner2, bg="#1c1308", highlightthickness=1, highlightbackground="#3d2f1e")
        btn_row2.pack(fill=tk.X, padx=8, pady=(4, 8))
        def do_import():
            src_path = var_ipath.get().strip()
            if not src_path:
                imp_status.config(text="✖  Enter a file path or URL.", fg=self.GUIDE_RED)
                return
            imp_status.config(text="⟳  Fetching…", fg=self.AMBER)
            imp.update_idletasks()
            def _fetch(src=src_path, is_url=var_isrc.get() == "url"):
                try:
                    if is_url or src.startswith(("http://", "https://")):
                        req = urllib.request.Request(src,
                            headers={"User-Agent": f"RetroTV/{APP_VERSION}"})
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            raw = resp.read()
                        new_channels = parse_extm3u_channels(raw)
                    else:
                        with open(src, 'r', encoding='utf-8', errors='replace') as f:
                            raw = f.read()
                        new_channels = parse_extm3u_channels(raw)
                except Exception as exc:
                    def _err(e=exc):
                        try: imp_status.config(text=f"✖  {e}", fg=self.GUIDE_RED)
                        except Exception: pass
                    self.after(0, _err)
                    return
                if not new_channels:
                    def _empty():
                        try: imp_status.config(text="✖  No channels found in playlist.", fg=self.GUIDE_RED)
                        except Exception: pass
                    self.after(0, _empty)
                    return
                def _apply(nc=new_channels):
                    try:
                        imp.destroy()
                        on_success(nc)
                    except Exception:
                        pass
                self.after(0, _apply)
            threading.Thread(target=_fetch, daemon=True, name="m3u-import").start()
        tk.Button(btn_row2, text="✔  IMPORT", command=do_import,
                  bg=self.AMBER_DIM, fg=self.AMBER, activebackground=self.AMBER,
                  activeforeground="#000000", font=self.font_small, relief=tk.FLAT,
                  padx=12, pady=6, cursor="hand2").pack(side=tk.RIGHT, padx=(6, 6), pady=6)
        tk.Button(btn_row2, text="CANCEL", command=imp.destroy,
                  bg="#2a1f10", fg=self.AMBER_DIM, activebackground="#2a1f10",
                  activeforeground=self.AMBER, font=self.font_small, relief=tk.FLAT,
                  padx=12, pady=6, cursor="hand2").pack(side=tk.RIGHT, pady=6)
        ent_ipath.focus_set()

    # ══════════════════════════════════════════
    #  First-Run Setup Wizard
    # ══════════════════════════════════════════
    def _show_first_run_dialog(self):
        """
        Modal setup wizard shown on the very first launch (or when both
        tv_config.json and user_config.json are absent).  The user picks one of:
          • Curated channel list from cdn.wedovids.com (default/recommended)
          • Import a local M3U/M3U8 playlist file
          • Import a remote M3U/M3U8 playlist via URL
        """
        BG       = '#1a1008'
        FG       = '#ffb000'
        FG_DIM   = '#7a5500'
        ENTRY_BG = '#0d0a06'
        BTN_BG   = '#2a1a04'
        BTN_HOV  = '#3a2808'
        FONT_XL  = ('Courier New', 13, 'bold')
        FONT_LG  = ('Courier New', 11, 'bold')
        FONT_SM  = ('Courier New', 10)
        FONT_TY  = ('Courier New',  9)

        d = tk.Toplevel(self)
        self._apply_icon(d)
        d.title("Welcome to RetroTV — Channel Setup")
        d.configure(bg=BG)
        d.resizable(False, False)
        d.grab_set()
        d.focus_force()
        # Don't let the user dismiss without making a choice
        d.protocol("WM_DELETE_WINDOW", lambda: None)

        # ── outer border ─────────────────────────────────────────────────
        border = tk.Frame(d, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg=BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # ── title bar ────────────────────────────────────────────────────
        title_bar = tk.Frame(inner, bg=self.AMBER_DIM)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="  ✦  WELCOME TO RETROTV  ✦",
                 font=FONT_XL, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor='w', pady=8).pack(fill=tk.X)

        # ── subtitle ─────────────────────────────────────────────────────
        body = tk.Frame(inner, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        tk.Label(body, text="Choose your channel source to get started:",
                 font=FONT_LG, bg=BG, fg=FG, anchor='w').pack(fill=tk.X, pady=(0, 14))

        # ────────────────────────────────────────────────────────────────
        #  Option A: Curated list
        # ────────────────────────────────────────────────────────────────
        box_a = tk.Frame(body, bg=BTN_BG, highlightthickness=1,
                         highlightbackground=FG_DIM, bd=0)
        box_a.pack(fill=tk.X, pady=(0, 10))

        tk.Label(box_a, text="  📡  CURATED CHANNEL LIST  (Recommended)",
                 font=FONT_LG, bg=BTN_BG, fg=FG, anchor='w',
                 pady=6).pack(fill=tk.X, padx=10)
        tk.Label(box_a,
                 text="  Automatically updated channel lineup provided by N5 Networks\n"
                      "  New channels are pushed whenever the lineup changes.",
                 font=FONT_TY, bg=BTN_BG, fg=FG_DIM, anchor='w',
                 justify='left').pack(fill=tk.X, padx=10)

        btn_curated = tk.Button(
            box_a, text="► USE CURATED LIST",
            font=FONT_LG, bg=FG, fg='#1a0800',
            activebackground='#ffc830', activeforeground='#1a0800',
            relief='flat', bd=0, padx=20, pady=11, cursor='hand2',
            highlightthickness=0)
        btn_curated.pack(fill=tk.X, padx=10, pady=(10, 12))
        btn_curated.bind("<Enter>", lambda e: btn_curated.config(bg='#ffc830'))
        btn_curated.bind("<Leave>", lambda e: btn_curated.config(bg=FG))

        # ────────────────────────────────────────────────────────────────
        #  Divider
        # ────────────────────────────────────────────────────────────────
        div = tk.Frame(body, bg=BG)
        div.pack(fill=tk.X, pady=4)
        tk.Frame(div, bg=FG_DIM, height=1).pack(fill=tk.X, side=tk.LEFT, expand=True, pady=7)
        tk.Label(div, text="  or load your own channels from a playlist  ",
                 font=('Courier New', 9, 'bold'), bg=BG, fg=FG_DIM).pack(side=tk.LEFT)
        tk.Frame(div, bg=FG_DIM, height=1).pack(fill=tk.X, side=tk.LEFT, expand=True, pady=7)

        # ────────────────────────────────────────────────────────────────
        #  Option B: M3U import
        # ────────────────────────────────────────────────────────────────
        box_b = tk.Frame(body, bg=BTN_BG, highlightthickness=1,
                         highlightbackground=FG_DIM, bd=0)
        box_b.pack(fill=tk.X, pady=(0, 4))

        tk.Label(box_b, text="  ⬇  M3U CHANNEL PLAYLIST",
                 font=FONT_LG, bg=BTN_BG, fg=FG, anchor='w',
                 pady=6).pack(fill=tk.X, padx=10)

        # Source selector radio buttons
        src_row = tk.Frame(box_b, bg=BTN_BG)
        src_row.pack(fill=tk.X, padx=10)
        tk.Label(src_row, text="Source:", font=FONT_SM, fg=FG_DIM,
                 bg=BTN_BG, width=8, anchor='w').pack(side=tk.LEFT)
        var_src = tk.StringVar(value="file")
        for val, lbl in (("file", "  Local File  "), ("url", "  Remote URL  ")):
            tk.Radiobutton(
                src_row, text=lbl, variable=var_src, value=val,
                bg=BTN_BG, fg=FG, activebackground=BTN_BG,
                activeforeground=FG, indicatoron=False, relief=tk.FLAT, bd=1,
                font=FONT_SM, padx=8, pady=3, cursor='hand2',
                highlightthickness=1, highlightbackground=FG_DIM,
                selectcolor=FG_DIM
            ).pack(side=tk.LEFT, padx=(0, 4))

        # Path / URL entry row
        path_row = tk.Frame(box_b, bg=BTN_BG)
        path_row.pack(fill=tk.X, padx=10, pady=(6, 0))
        tk.Label(path_row, text="Playlist:", font=FONT_SM, fg=FG_DIM,
                 bg=BTN_BG, width=8, anchor='w').pack(side=tk.LEFT)
        var_path = tk.StringVar()
        ent_path = tk.Entry(
            path_row, textvariable=var_path,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=FONT_SM, relief=tk.FLAT, bd=0,
            highlightbackground=FG_DIM, highlightthickness=1)
        ent_path.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        def _browse_m3u():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                parent=d, title="Select M3U Playlist",
                filetypes=[("M3U Playlist", "*.m3u *.m3u8"), ("All files", "*.*")])
            if p:
                var_path.set(p)
                ent_path.focus_set()

        btn_browse = tk.Button(
            path_row, text="Browse…", command=_browse_m3u,
            bg=BTN_BG, fg=FG, activebackground=BTN_HOV,
            font=FONT_SM, relief=tk.FLAT, padx=6, pady=2,
            cursor='hand2', bd=0)
        btn_browse.pack(side=tk.LEFT)

        def _on_src_change(*_):
            if var_src.get() == "url":
                btn_browse.pack_forget()
            else:
                btn_browse.pack(side=tk.LEFT)
        var_src.trace_add("write", _on_src_change)

        # ── Optional EPG source ───────────────────────────────────────────
        tk.Frame(box_b, bg=FG_DIM, height=1).pack(fill=tk.X, padx=10, pady=(8, 0))
        epg_hdr_row = tk.Frame(box_b, bg=BTN_BG)
        epg_hdr_row.pack(fill=tk.X, padx=10, pady=(4, 0))
        tk.Label(epg_hdr_row, text="📺  EPG / PROGRAM GUIDE",
                 font=FONT_SM, fg=FG, bg=BTN_BG, anchor='w').pack(side=tk.LEFT)
        tk.Label(epg_hdr_row, text=" optional",
                 font=FONT_TY, fg=FG_DIM, bg=BTN_BG, anchor='w').pack(side=tk.LEFT)

        epg_row = tk.Frame(box_b, bg=BTN_BG)
        epg_row.pack(fill=tk.X, padx=10, pady=(4, 0))
        tk.Label(epg_row, text="Source:", font=FONT_SM, fg=FG_DIM,
                 bg=BTN_BG, width=8, anchor='w').pack(side=tk.LEFT)
        var_epg = tk.StringVar()
        ent_epg = tk.Entry(
            epg_row, textvariable=var_epg,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            font=FONT_SM, relief=tk.FLAT, bd=0,
            highlightbackground=FG_DIM, highlightthickness=1)
        ent_epg.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        def _browse_epg():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                parent=d, title="Select XMLTV File",
                filetypes=[("XMLTV files", "*.xml"), ("All files", "*.*")])
            if p:
                var_epg.set(p)
                ent_epg.focus_set()

        tk.Button(epg_row, text="Browse…", command=_browse_epg,
                  bg=BTN_BG, fg=FG, activebackground=BTN_HOV,
                  font=FONT_SM, relief=tk.FLAT, padx=6, pady=2,
                  cursor='hand2', bd=0).pack(side=tk.LEFT)

        tk.Label(box_b,
                 text="  XMLTV URL or local .xml file — merged with the built-in guide.",
                 font=FONT_TY, fg=FG_DIM, bg=BTN_BG, anchor='w').pack(fill=tk.X, padx=10)

        # Status label (shows fetch progress / errors)
        status_lbl = tk.Label(
            box_b, text="", font=FONT_TY, fg=self.GUIDE_GREEN,
            bg=BTN_BG, anchor='w', padx=10, pady=2)
        status_lbl.pack(fill=tk.X, padx=10)

        btn_import = tk.Button(
            box_b, text="► IMPORT M3U",
            font=FONT_LG, bg='#1a0c02', fg=FG,
            activebackground='#3a2808', activeforeground='#ffc830',
            relief='flat', bd=0, padx=20, pady=11, cursor='hand2',
            highlightthickness=2, highlightbackground=FG)
        btn_import.pack(fill=tk.X, padx=10, pady=(8, 12))
        btn_import.bind("<Enter>", lambda e: btn_import.config(bg='#3a2808', fg='#ffc830'))
        btn_import.bind("<Leave>", lambda e: btn_import.config(bg='#1a0c02', fg=FG))

        # ────────────────────────────────────────────────────────────────
        #  Button callbacks
        # ────────────────────────────────────────────────────────────────
        def _choose_curated():
            self._settings['use_curated_channels'] = True
            _save_settings_file(self._settings)
            d.destroy()
            self._after_first_run_setup(curated=True)

        btn_curated.config(command=_choose_curated)

        def _do_import_m3u():
            src_path = var_path.get().strip()
            if not src_path:
                status_lbl.config(text="✖  Enter a file path or URL.", fg=self.GUIDE_RED)
                return
            status_lbl.config(text="⟳  Fetching playlist…", fg=FG)
            btn_import.config(state=tk.DISABLED)
            d.update_idletasks()

            def _fetch(src=src_path, is_url=var_src.get() == "url"):
                try:
                    if is_url or src.startswith(("http://", "https://")):
                        req = urllib.request.Request(
                            src, headers={"User-Agent": f"RetroTV/{APP_VERSION}"})
                        with urllib.request.urlopen(req, timeout=20) as resp:
                            raw_bytes = resp.read()
                        new_channels = parse_extm3u_channels(raw_bytes)
                    else:
                        with open(src, 'r', encoding='utf-8', errors='replace') as f:
                            raw_text = f.read()
                        new_channels = parse_extm3u_channels(raw_text)
                except Exception as exc:
                    def _show_err(e=exc):
                        try:
                            status_lbl.config(text=f"✖  {e}", fg=self.GUIDE_RED)
                            btn_import.config(state=tk.NORMAL)
                        except Exception:
                            pass
                    self.after(0, _show_err)
                    return
                if not new_channels:
                    def _show_empty():
                        try:
                            status_lbl.config(
                                text="✖  No channels found in that playlist.",
                                fg=self.GUIDE_RED)
                            btn_import.config(state=tk.NORMAL)
                        except Exception:
                            pass
                    self.after(0, _show_empty)
                    return

                def _apply(nc=new_channels):
                    try:
                        user_cfg_path = os.path.join(self.base_dir, "user_config.json")
                        existing = _load_user_config_dict(user_cfg_path)
                        existing["channels"].extend(nc)
                        epg_val = var_epg.get().strip()
                        if epg_val and epg_val not in existing["epg_urls"]:
                            existing["epg_urls"].append(epg_val)
                        with open(user_cfg_path, 'w', encoding='utf-8') as fp:
                            json.dump(existing, fp, indent=2, ensure_ascii=False)
                        d.destroy()
                        self._after_first_run_setup(curated=False)
                    except Exception as e:
                        log.error("First-run M3U import apply error: %s", e)
                self.after(0, _apply)

            threading.Thread(target=_fetch, daemon=True,
                             name="first-run-m3u-import").start()

        btn_import.config(command=_do_import_m3u)
        ent_path.bind("<Return>", lambda _e: _do_import_m3u())

        # ────────────────────────────────────────────────────────────────
        #  Divider 2
        # ────────────────────────────────────────────────────────────────
        div2 = tk.Frame(body, bg=BG)
        div2.pack(fill=tk.X, pady=4)
        tk.Frame(div2, bg=FG_DIM, height=1).pack(fill=tk.X, side=tk.LEFT, expand=True, pady=7)
        tk.Label(div2, text="  or build from scratch  ",
                 font=('Courier New', 9, 'bold'), bg=BG, fg=FG_DIM).pack(side=tk.LEFT)
        tk.Frame(div2, bg=FG_DIM, height=1).pack(fill=tk.X, side=tk.LEFT, expand=True, pady=7)

        # ────────────────────────────────────────────────────────────────
        #  Option C: Create your own channel
        # ────────────────────────────────────────────────────────────────
        box_c = tk.Frame(body, bg=BTN_BG, highlightthickness=1,
                         highlightbackground=FG_DIM, bd=0)
        box_c.pack(fill=tk.X, pady=(0, 10))

        tk.Label(box_c, text="  ✎  CREATE YOUR OWN CHANNEL",
                 font=FONT_LG, bg=BTN_BG, fg=FG, anchor='w',
                 pady=6).pack(fill=tk.X, padx=10)
        tk.Label(box_c,
                 text="  Build custom channels from scratch using the Channel Editor.\n"
                      "  Add your own streams, playlists, or M3U sources manually.",
                 font=FONT_TY, bg=BTN_BG, fg=FG_DIM, anchor='w',
                 justify='left').pack(fill=tk.X, padx=10)

        btn_create = tk.Button(
            box_c, text="► OPEN CHANNEL EDITOR",
            font=FONT_LG, bg='#1a0c02', fg=FG,
            activebackground='#3a2808', activeforeground='#ffc830',
            relief='flat', bd=0, padx=20, pady=11, cursor='hand2',
            highlightthickness=2, highlightbackground=FG)
        btn_create.pack(fill=tk.X, padx=10, pady=(10, 12))
        btn_create.bind("<Enter>", lambda e: btn_create.config(bg='#3a2808', fg='#ffc830'))
        btn_create.bind("<Leave>", lambda e: btn_create.config(bg='#1a0c02', fg=FG))

        def _choose_create_channel():
            d.destroy()
            self._after_first_run_setup(curated=False)
            self.after(300, self._open_channel_editor)

        btn_create.config(command=_choose_create_channel)

        self._center_dialog(d)

    def _after_first_run_setup(self, curated: bool = False):
        """
        Called when the first-run wizard completes.  Reloads the channel list
        from whatever the user just configured and starts playback.
        """
        self._first_run = False
        self.channels = []
        self._user_epg_sources = []
        self._load_config()
        self._rebuild_channel_list()
        if curated:
            # Pull the latest lineup immediately (don't wait 800 ms)
            self._check_remote_config()
        if self.channels:
            self._tune_channel(0, static=True)
        if self._settings.get('start_fullscreen'):
            self.after(400, self._enter_fullscreen)
        self._show_update_banner("✦ WELCOME TO RETROTV ✦")

    # ══════════════════════════════════════════
    #  Channel Editor
    # ══════════════════════════════════════════
    def _open_channel_editor(self):
        if getattr(self, '_editor_open', False): return
        self._editor_open = True
        user_config_path = os.path.join(self.base_dir, "user_config.json")
        entries = list(_load_user_config_dict(user_config_path).get("channels", []))
        import copy
        entries = copy.deepcopy(entries)
        d = tk.Toplevel(self)
        self._apply_icon(d)
        d.title("Channel Editor"); d.configure(bg="#1c1308")
        d.resizable(True, True); d.grab_set(); d.focus_force()
        self._center_dialog(d, 780, 620)
        d.minsize(660, 500)
        _dirty = [False]; selected_idx = [None]
        def on_close():
            self._editor_open = False; d.destroy()
        d.protocol("WM_DELETE_WINDOW", on_close)
        d.bind("<Escape>", lambda e: on_close())
        border = tk.Frame(d, bg=self.AMBER, bd=0); border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg="#1c1308"); inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        title_bar = tk.Frame(inner, bg=self.AMBER_DIM); title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="  ✎  CHANNEL EDITOR", font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor="w", pady=6).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(title_bar, text="  Changes are saved when you click  ✔ APPLY & CLOSE  ",
                 font=font.Font(family="Courier New", size=8), fg=self.AMBER_DIM, bg=self.AMBER_DIM, anchor="e").pack(side=tk.RIGHT)
        split = tk.Frame(inner, bg="#1c1308"); split.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 0))
        left = tk.Frame(split, bg="#1c1308", width=210); left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        left.pack_propagate(False)
        tk.Label(left, text="YOUR CHANNELS", font=self.font_small, fg=self.AMBER, bg="#1c1308", pady=4, anchor="w").pack(fill=tk.X)
        list_frame = tk.Frame(left, bg="#0d0a06", highlightthickness=1, highlightbackground=self.AMBER_DIM)
        list_frame.pack(fill=tk.BOTH, expand=True)
        list_scroll = tk.Scrollbar(list_frame, bg="#2a1f10", troughcolor="#0d0a06", width=8)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        ch_listbox = tk.Listbox(list_frame, bg="#0d0a06", fg=self.BEIGE,
                                selectbackground=self.AMBER_DIM, selectforeground=self.AMBER,
                                font=self.font_ch, relief=tk.FLAT, bd=0,
                                highlightthickness=0, activestyle='none', yscrollcommand=list_scroll.set)
        ch_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scroll.config(command=ch_listbox.yview)
        list_btns = tk.Frame(left, bg="#1c1308"); list_btns.pack(fill=tk.X, pady=(6, 0))
        def _mlb(parent, text, cmd, row, col):
            b = tk.Button(parent, text=text, command=cmd, bg="#2a1f10", fg=self.AMBER,
                          activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                          font=self.font_ch, relief=tk.FLAT, padx=4, pady=3, bd=0,
                          cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM)
            b.grid(row=row, column=col, padx=(0, 3), pady=(0, 3), sticky="ew"); return b
        list_btns.columnconfigure(0, weight=1); list_btns.columnconfigure(1, weight=1)
        btn_add  = _mlb(list_btns, "＋ Add Channel",  lambda: add_new(),           0, 0)
        btn_dupe = _mlb(list_btns, "⧉ Duplicate",     lambda: duplicate_selected(), 0, 1)
        btn_up   = _mlb(list_btns, "▲ Move Up",       lambda: move_up(),            1, 0)
        btn_down = _mlb(list_btns, "▼ Move Down",     lambda: move_down(),          1, 1)
        btn_del    = _mlb(list_btns, "✖ Delete",        lambda: delete_selected(),    2, 0)
        btn_import = _mlb(list_btns, "⬇ Import M3U",  lambda: import_m3u(),         2, 1)
        btn_tracks = _mlb(list_btns, "✎ Edit Tracks…", lambda: edit_tracks_selected(), 3, 0)
        btn_tracks.grid(columnspan=2)
        right = tk.Frame(split, bg="#1c1308"); right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        form_title = tk.Label(right, text="— no channel selected —", font=self.font_small,
                              fg=self.AMBER_DIM, bg="#1c1308", pady=4, anchor="w")
        form_title.pack(fill=tk.X)
        form = tk.Frame(right, bg="#1c1308"); form.pack(fill=tk.BOTH, expand=True)
        lbl_kw = dict(font=self.font_ch, fg=self.AMBER_DIM, bg="#1c1308", anchor="w", width=13)
        ent_kw = dict(bg="#0d0a06", fg=self.AMBER, insertbackground=self.AMBER, font=self.font_ch,
                      relief=tk.FLAT, bd=0, highlightbackground=self.AMBER_DIM, highlightthickness=1)
        def _field_row(label, hint=None):
            outer = tk.Frame(form, bg="#1c1308"); outer.pack(fill=tk.X, pady=(0, 6))
            tk.Label(outer, text=label, **lbl_kw).grid(row=0, column=0, sticky="w", padx=(0, 6))
            entry_frame = tk.Frame(outer, bg="#1c1308"); entry_frame.grid(row=0, column=1, sticky="ew")
            outer.columnconfigure(1, weight=1)
            if hint:
                tk.Label(outer, text=hint, font=font.Font(family="Courier New", size=7),
                         fg="#555544", bg="#1c1308", anchor="w").grid(row=1, column=1, sticky="w")
            return entry_frame
        r = _field_row("Channel Name"); var_name = tk.StringVar()
        ent_name = tk.Entry(r, textvariable=var_name, **ent_kw); ent_name.pack(fill=tk.X, expand=True)
        r = _field_row("Description", hint="Optional — shown in the channel guide")
        var_desc = tk.StringVar(); tk.Entry(r, textvariable=var_desc, **ent_kw).pack(fill=tk.X, expand=True)
        r = _field_row("EPG Channel ID", hint="Matches channel-id in guide.xml")
        var_cid = tk.StringVar(); tk.Entry(r, textvariable=var_cid, **ent_kw).pack(fill=tk.X, expand=True)
        ttk.Separator(form, orient='horizontal').pack(fill=tk.X, pady=(2, 8))
        src_frame = tk.Frame(form, bg="#1c1308"); src_frame.pack(fill=tk.X, pady=(0, 6))
        tk.Label(src_frame, text="Source Type", **lbl_kw).pack(side=tk.LEFT)
        var_type = tk.StringVar(value="url")
        for val, label in (("url", "  Live URL  "), ("playlist", "  Playlist File  ")):
            tk.Radiobutton(src_frame, text=label, variable=var_type, value=val,
                           bg="#1c1308", fg=self.AMBER, activebackground="#1c1308", activeforeground=self.AMBER,
                           indicatoron=False, relief=tk.FLAT, bd=1, font=self.font_ch, padx=8, pady=3,
                           cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM,
                           selectcolor=self.AMBER_DIM).pack(side=tk.LEFT, padx=(0, 4))
        r_url = tk.Frame(form, bg="#1c1308"); r_url.pack(fill=tk.X, pady=(0, 6))
        tk.Label(r_url, text="Stream URL", **lbl_kw).grid(row=0, column=0, sticky="w", padx=(0, 6))
        url_inner = tk.Frame(r_url, bg="#1c1308"); url_inner.grid(row=0, column=1, sticky="ew")
        r_url.columnconfigure(1, weight=1)
        tk.Label(r_url, text="e.g.  https://example.com/stream.m3u8",
                 font=font.Font(family="Courier New", size=7), fg="#555544", bg="#1c1308", anchor="w").grid(row=1, column=1, sticky="w")
        var_url = tk.StringVar(); tk.Entry(url_inner, textvariable=var_url, **ent_kw).pack(fill=tk.X, expand=True)
        r_pl = tk.Frame(form, bg="#1c1308")
        tk.Label(r_pl, text="Playlist File", **lbl_kw).grid(row=0, column=0, sticky="w", padx=(0, 6))
        pl_inner = tk.Frame(r_pl, bg="#1c1308"); pl_inner.grid(row=0, column=1, sticky="ew")
        r_pl.columnconfigure(1, weight=1)
        tk.Label(r_pl, text="XSPF, M3U or M3U8 — or drag a folder of video files",
                 font=font.Font(family="Courier New", size=7), fg="#555544", bg="#1c1308", anchor="w").grid(row=1, column=1, sticky="w", columnspan=2)
        var_pl = tk.StringVar()
        tk.Entry(pl_inner, textvariable=var_pl, **ent_kw).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        def browse_playlist():
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=d, title="Select Playlist File",
                filetypes=[("Playlist files", "*.xspf *.m3u *.m3u8"), ("All files", "*.*")])
            if path: var_pl.set(path)
        tk.Button(pl_inner, text="Browse…", command=browse_playlist,
                  bg="#2a1f10", fg=self.AMBER, activebackground=self.AMBER_DIM,
                  font=self.font_ch, relief=tk.FLAT, padx=6, pady=2, cursor="hand2", bd=0).pack(side=tk.LEFT)
        def _open_playlist_builder():
            pl_path = var_pl.get().strip()
            abs_path = None
            if pl_path:
                abs_path = pl_path if os.path.isabs(pl_path) else os.path.join(self.base_dir, pl_path)
            exists = bool(abs_path and os.path.exists(abs_path))
            existing_files = parse_playlist(abs_path) if exists else []
            def _on_builder_commit(name, rel, shuffle, comm):
                if name: var_name.set(name)
                var_pl.set(rel)
                var_random.set(shuffle)
                var_commercials.set(comm)
                _dirty[0] = True
                if not commit_current(silent=True): return
                _set_status(f"✔  Tracks saved for '{name}'.  Click  ✔ APPLY & CLOSE  to reload channels.")
            self._open_local_playlist_builder(
                d, _on_builder_commit,
                initial_name=var_name.get().strip(),
                initial_files=existing_files,
                edit_m3u_path=(abs_path if exists else None),
                initial_shuffle=var_random.get(),
                initial_comm=var_commercials.get())
        btn_builder = tk.Button(pl_inner, text="✎ Edit Tracks…", command=_open_playlist_builder,
                  bg="#2a1f10", fg=self.AMBER, activebackground=self.AMBER_DIM,
                  font=self.font_ch, relief=tk.FLAT, padx=6, pady=2, cursor="hand2", bd=0)
        btn_builder.pack(side=tk.LEFT, padx=(4, 0))
        def _update_builder_label(*_):
            btn_builder.config(text="✎ Edit Tracks…" if var_pl.get().strip() else "📁 Build…")
        var_pl.trace_add("write", _update_builder_label)
        shuffle_frame = tk.Frame(form, bg="#1c1308")
        var_random = tk.BooleanVar(value=False)
        tk.Checkbutton(shuffle_frame, text="Shuffle playlist order", variable=var_random,
                       bg="#1c1308", fg=self.AMBER, selectcolor="#0d0a06",
                       activebackground="#1c1308", activeforeground=self.AMBER, font=self.font_ch).pack(side=tk.LEFT)
        var_commercials = tk.BooleanVar(value=True)
        tk.Checkbutton(shuffle_frame, text="Play commercial breaks", variable=var_commercials,
                       bg="#1c1308", fg=self.AMBER, selectcolor="#0d0a06",
                       activebackground="#1c1308", activeforeground=self.AMBER, font=self.font_ch).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Separator(form, orient='horizontal').pack(fill=tk.X, pady=(2, 8))
        r_wm = _field_row("Watermark PNG", hint="Transparent PNG shown bottom-right (Pillow recommended)")
        var_watermark = tk.StringVar()
        tk.Entry(r_wm, textvariable=var_watermark, **ent_kw).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        def browse_watermark():
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=d, title="Select Watermark PNG",
                filetypes=[("PNG images", "*.png"), ("All images", "*.png *.gif *.ppm *.pgm"), ("All files", "*.*")])
            if path: var_watermark.set(path)
        tk.Button(r_wm, text="Browse…", command=browse_watermark,
                  bg="#2a1f10", fg=self.AMBER, activebackground=self.AMBER_DIM,
                  font=self.font_ch, relief=tk.FLAT, padx=6, pady=2, cursor="hand2", bd=0).pack(side=tk.LEFT)
        status_bar = tk.Label(form, text="", font=self.font_ch, fg=self.GUIDE_GREEN, bg="#0d0a06",
                              anchor="w", padx=6, pady=4)
        status_bar.pack(fill=tk.X, pady=(8, 0))
        def _set_status(msg, colour=None):
            colour = colour or self.GUIDE_GREEN; status_bar.config(text=msg, fg=colour)
        def on_type_change(*_):
            t = var_type.get()
            if t == "url":
                r_url.pack(fill=tk.X, pady=(0, 6)); r_pl.pack_forget(); shuffle_frame.pack_forget()
            else:
                r_url.pack_forget(); r_pl.pack(fill=tk.X, pady=(0, 6)); shuffle_frame.pack(fill=tk.X, pady=(0, 6))
        var_type.trace_add("write", on_type_change); on_type_change()
        def _mark_dirty(*_):
            if selected_idx[0] is not None: _dirty[0] = True
        for v in (var_name, var_desc, var_cid, var_type, var_url, var_pl, var_random, var_commercials, var_watermark):
            v.trace_add("write", _mark_dirty)
        def commit_current(silent=False):
            idx = selected_idx[0]
            if idx is None or not _dirty[0]: return True
            name = var_name.get().strip()
            if not name:
                _set_status("✖  Channel name is required.", self.GUIDE_RED)
                ent_name.focus_set(); return False
            entry = {"name": name, "description": var_desc.get().strip()}
            cid = var_cid.get().strip()
            if cid: entry["channel-id"] = cid
            if var_type.get() == "url":
                url = var_url.get().strip()
                if not url:
                    _set_status("✖  Stream URL is required.", self.GUIDE_RED); return False
                entry["url"] = url
            else:
                pl = var_pl.get().strip()
                if not pl:
                    _set_status("✖  Playlist file path is required.", self.GUIDE_RED); return False
                entry["playlist"] = pl
                entry["random"] = "1" if var_random.get() else "0"
                entry["commercials"] = "1" if var_commercials.get() else "0"
            wm = var_watermark.get().strip()
            if wm: entry["watermark"] = wm
            entries[idx] = entry; _dirty[0] = False
            refresh_list(idx)
            if not silent: _set_status(f"✔  '{name}' updated.")
            return True
        def load_entry(idx):
            selected_idx[0] = idx; _dirty[0] = False
            if idx is None or idx >= len(entries):
                form_title.config(text="— no channel selected —", fg=self.AMBER_DIM)
                var_name.set(""); var_desc.set(""); var_cid.set("")
                var_url.set(""); var_pl.set(""); var_type.set("url")
                var_random.set(False); var_commercials.set(True); var_watermark.set(""); _set_status(""); return
            e = entries[idx]
            form_title.config(text=f"Editing  ·  {e.get('name', '(unnamed)')}", fg=self.AMBER)
            var_name.set(e.get("name", "")); var_desc.set(e.get("description", ""))
            var_cid.set(e.get("channel-id", ""))
            if e.get("url"):
                var_type.set("url"); var_url.set(e.get("url", ""))
            else:
                var_type.set("playlist"); var_pl.set(e.get("playlist", ""))
            var_random.set(str(e.get("random", "0")).strip() not in ("0", "false", "False", ""))
            var_commercials.set(str(e.get("commercials", "1")).strip() not in ("0", "false", "False", ""))
            var_watermark.set(e.get("watermark", "")); _dirty[0] = False; _set_status("")
        def on_list_select(event):
            ch_listbox.focus_set()
            sel = ch_listbox.curselection()
            if not sel: return
            new_idx = sel[0]
            if new_idx == selected_idx[0]: return
            if not commit_current(silent=True):
                ch_listbox.selection_clear(0, tk.END)
                if selected_idx[0] is not None: ch_listbox.selection_set(selected_idx[0])
                return
            load_entry(new_idx)
        ch_listbox.bind("<<ListboxSelect>>", on_list_select)
        def edit_tracks_selected():
            idx = selected_idx[0]
            if idx is None:
                _set_status("✖  Select a channel first.", self.GUIDE_RED); return
            if var_type.get() != "playlist":
                _set_status("✖  Only playlist channels have tracks to edit (this one is a live URL).",
                            self.GUIDE_RED); return
            _open_playlist_builder()
        def on_list_double(event):
            idx = ch_listbox.nearest(event.y)
            if idx == selected_idx[0] and var_type.get() == "playlist":
                _open_playlist_builder()
        ch_listbox.bind("<Double-Button-1>", on_list_double)
        def refresh_list(select_idx=None):
            ch_listbox.delete(0, tk.END)
            for i, e in enumerate(entries):
                name = e.get('name', '(unnamed)'); src = "URL" if e.get("url") else "PLAYLIST"
                ch_listbox.insert(tk.END, f"  {name}  [{src}]")
            if select_idx is not None and 0 <= select_idx < len(entries):
                ch_listbox.selection_clear(0, tk.END); ch_listbox.selection_set(select_idx); ch_listbox.see(select_idx)
            if not entries:
                form_title.config(text="— no channels yet —", fg=self.AMBER_DIM)
                _set_status("Click  ＋ Add Channel  to create your first channel.")
        def add_new():
            commit_current(silent=True)
            # ── Type-picker dialog ────────────────────────────────────────
            picker = tk.Toplevel(d)
            self._apply_icon(picker)
            picker.title("Add Channel")
            picker.configure(bg="#1c1308")
            picker.resizable(False, False)
            picker.grab_set()
            picker.focus_force()
            self._center_dialog(picker, 460, 310)
            border_p = tk.Frame(picker, bg=self.AMBER, bd=0)
            border_p.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
            inner_p = tk.Frame(border_p, bg="#1c1308")
            inner_p.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
            tbar = tk.Frame(inner_p, bg=self.AMBER_DIM)
            tbar.pack(fill=tk.X)
            tk.Label(tbar, text="  ＋  ADD CHANNEL",
                     font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                     anchor='w', pady=6).pack(fill=tk.X)
            body_p = tk.Frame(inner_p, bg="#1c1308")
            body_p.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
            tk.Label(body_p, text="What kind of channel do you want to add?",
                     font=self.font_ch, fg=self.AMBER_DIM, bg="#1c1308",
                     anchor='w').pack(fill=tk.X, pady=(0, 10))
            # Card A – Live Stream
            card_a = tk.Frame(body_p, bg="#2a1f10",
                               highlightthickness=1, highlightbackground=self.AMBER_DIM)
            card_a.pack(fill=tk.X, pady=(0, 8))
            tk.Label(card_a, text="  🌐  PLAYLIST / LIVE STREAM URL",
                     font=self.font_small, bg="#2a1f10", fg=self.AMBER,
                     anchor='w', pady=4).pack(fill=tk.X, padx=10)
            tk.Label(card_a,
                     text="  Use an existing M3U playlist or paste a direct stream URL",
                     font=font.Font(family="Courier New", size=8),
                     bg="#2a1f10", fg=self.AMBER_DIM, anchor='w').pack(fill=tk.X, padx=10)
            btn_live = tk.Button(card_a, text="► USE EXISTING PLAYLIST",
                                  font=self.font_small, bg=self.AMBER, fg='#1a0800',
                                  activebackground='#ffc830', activeforeground='#1a0800',
                                  relief='flat', bd=0, padx=20, pady=8,
                                  cursor='hand2', highlightthickness=0)
            btn_live.pack(fill=tk.X, padx=10, pady=(8, 10))
            btn_live.bind("<Enter>", lambda e: btn_live.config(bg='#ffc830'))
            btn_live.bind("<Leave>", lambda e: btn_live.config(bg=self.AMBER))
            # Card B – Local files
            card_b = tk.Frame(body_p, bg="#2a1f10",
                               highlightthickness=1, highlightbackground=self.AMBER_DIM)
            card_b.pack(fill=tk.X)
            tk.Label(card_b, text="  📁  CHANNEL BUILDER",
                     font=self.font_small, bg="#2a1f10", fg=self.AMBER,
                     anchor='w', pady=4).pack(fill=tk.X, padx=10)
            tk.Label(card_b,
                     text="  Build a custom channel from video files on your computer",
                     font=font.Font(family="Courier New", size=8),
                     bg="#2a1f10", fg=self.AMBER_DIM, anchor='w').pack(fill=tk.X, padx=10)
            btn_local = tk.Button(card_b, text="► OPEN CHANNEL BUILDER",
                                   font=self.font_small, bg='#1a0c02', fg=self.AMBER,
                                   activebackground='#3a2808', activeforeground='#ffc830',
                                   relief='flat', bd=0, padx=20, pady=8,
                                   cursor='hand2',
                                   highlightthickness=2, highlightbackground=self.AMBER)
            btn_local.pack(fill=tk.X, padx=10, pady=(8, 10))
            btn_local.bind("<Enter>",
                            lambda e: btn_local.config(bg='#3a2808', fg='#ffc830'))
            btn_local.bind("<Leave>",
                            lambda e: btn_local.config(bg='#1a0c02', fg=self.AMBER))
            # Cancel
            tk.Button(inner_p, text="CANCEL", command=picker.destroy,
                      bg="#2a1f10", fg=self.AMBER_DIM,
                      activebackground="#2a1f10", activeforeground=self.AMBER,
                      font=self.font_ch, relief=tk.FLAT, padx=8, pady=4,
                      cursor='hand2').pack(side=tk.RIGHT, padx=10, pady=(0, 8))
            picker.bind("<Escape>", lambda e: picker.destroy())
            def _pick_live():
                picker.destroy()
                new_entry = {"name": "New Channel", "url": ""}
                entries.append(new_entry)
                refresh_list(len(entries) - 1)
                load_entry(len(entries) - 1)
                ent_name.focus_set()
                ent_name.select_range(0, tk.END)
                _set_status("Fill in the channel name and stream URL below.")
            def _pick_local():
                picker.destroy()
                def _on_built(name, rel_path, shuffle, commercials):
                    new_entry = {
                        "name": name,
                        "playlist": rel_path,
                        "random": "1" if shuffle else "0",
                        "commercials": "1" if commercials else "0",
                    }
                    entries.append(new_entry)
                    refresh_list(len(entries) - 1)
                    load_entry(len(entries) - 1)
                    _set_status(f"✔  '{name}' created — add a description if you like.")
                self._open_local_playlist_builder(d, _on_built)
            btn_live.config(command=_pick_live)
            btn_local.config(command=_pick_local)
        def duplicate_selected():
            if not commit_current(silent=True): return
            idx = selected_idx[0]
            if idx is None: return
            import copy as _copy
            new_entry = _copy.deepcopy(entries[idx]); new_entry["name"] = new_entry.get("name", "") + " (copy)"
            entries.insert(idx + 1, new_entry); refresh_list(idx + 1); load_entry(idx + 1)
            _set_status(f"Duplicated  '{entries[idx].get('name', '')}'.")
        def delete_selected():
            idx = selected_idx[0]
            if idx is None:
                sel = ch_listbox.curselection()
                if not sel: _set_status("✖  Select a channel first.", self.GUIDE_RED); return
                idx = sel[0]; load_entry(idx)
            if idx is None: return
            name = entries[idx].get("name", "(unnamed)")
            _set_status(f"Delete  '{name}'?  Click  ✖ Delete  again to confirm.", self.GUIDE_RED)
            btn_del._confirm_pending = True
            def _do_delete(captured_idx=idx, captured_name=name):
                btn_del._confirm_pending = False
                btn_del.config(text="✖ Delete", command=delete_selected, fg=self.AMBER)
                selected_idx[0] = None; _dirty[0] = False; entries.pop(captured_idx)
                new_sel = min(captured_idx, len(entries) - 1) if entries else None
                refresh_list(new_sel); load_entry(new_sel); _set_status(f"Deleted  '{captured_name}'.")
            def _cancel_delete(*_):
                btn_del._confirm_pending = False
                btn_del.config(text="✖ Delete", command=delete_selected, fg=self.AMBER); _set_status("")
            btn_del.config(text="⚠ Confirm Delete", command=_do_delete, fg="#ff6666")
            d.after(4000, lambda: _cancel_delete() if getattr(btn_del, '_confirm_pending', False) else None)
        def move_up():
            if not commit_current(silent=True): return
            idx = selected_idx[0]
            if idx is None or idx == 0: return
            entries[idx], entries[idx-1] = entries[idx-1], entries[idx]; refresh_list(idx - 1); load_entry(idx - 1)
        def move_down():
            if not commit_current(silent=True): return
            idx = selected_idx[0]
            if idx is None or idx >= len(entries) - 1: return
            entries[idx], entries[idx+1] = entries[idx+1], entries[idx]; refresh_list(idx + 1); load_entry(idx + 1)
        def import_m3u():
            commit_current(silent=True)
            def _on_imported(nc):
                entries.extend(nc)
                refresh_list(len(entries) - 1)
                load_entry(len(entries) - 1)
                _set_status(f"✔  Imported {len(nc)} channel(s) from M3U playlist.")
            self._show_m3u_import_dialog(d, _on_imported)
        bottom = tk.Frame(inner, bg="#1c1308", highlightthickness=1, highlightbackground="#3d2f1e")
        bottom.pack(fill=tk.X, padx=8, pady=(4, 8))
        def apply_and_close():
            if not commit_current(): return
            try:
                existing = _load_user_config_dict(user_config_path)
                existing["channels"] = entries
                with open(user_config_path, 'w', encoding='utf-8') as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                self.channels = []; self._load_config()
                valid_names = {ch.name for ch in self.channels}
                self._favorites = {n for n in self._favorites if n in valid_names}
                self._rebuild_channel_list(); self._show_update_banner("✦ CHANNEL LIST UPDATED ✦"); on_close()
            except Exception as e:
                _set_status(f"✖  Could not save: {e}", self.GUIDE_RED)
        tk.Button(bottom, text="✔  APPLY & CLOSE", command=apply_and_close,
                  bg=self.AMBER_DIM, fg=self.AMBER, activebackground=self.AMBER, activeforeground="#000000",
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6, cursor="hand2").pack(side=tk.RIGHT, padx=(6, 6), pady=6)
        tk.Button(bottom, text="CANCEL", command=on_close,
                  bg="#2a1f10", fg=self.AMBER_DIM, activebackground="#2a1f10", activeforeground=self.AMBER,
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6, cursor="hand2").pack(side=tk.RIGHT, pady=6)
        tk.Label(bottom, text="  All edits are held in memory until you click  ✔ APPLY & CLOSE",
                 font=font.Font(family="Courier New", size=8), fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(side=tk.LEFT, padx=6)
        refresh_list()
        if entries: ch_listbox.selection_set(0); load_entry(0)
        else: _set_status("Click  ＋ Add Channel  to get started.")

    # ══════════════════════════════════════════
    #  Local Playlist Builder
    # ══════════════════════════════════════════
    def _open_local_playlist_builder(self, parent, on_commit,
                                      initial_name="", initial_files=None,
                                      edit_m3u_path=None,
                                      initial_shuffle=False, initial_comm=True):
        """
        Dialog for building or editing a local-file channel playlist.
        Lets the user pick video files, reorder them, then saves them as
        an M3U file in the  playlists/  folder next to the app.

        on_commit(name, rel_m3u_path, shuffle, commercials) is called on success.
        edit_m3u_path: absolute path to an existing playlist (edit mode).
            M3U/M3U8 files are overwritten in place; other formats (e.g. XSPF)
            are left untouched and the edited list is saved as a new M3U.
        """
        VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m4v',
                     '.mpg', '.mpeg', '.ts', '.m2ts', '.webm', '.ogv', '.3gp'}
        BG       = "#1c1308"; FG = self.AMBER; FG_DIM = self.AMBER_DIM
        BTN_BG   = "#2a1f10"; BTN_HOV = "#3a2808"; ENTRY_BG = "#0d0a06"
        edit_mode = (edit_m3u_path is not None)
        files = list(initial_files or [])

        dlg = tk.Toplevel(parent)
        self._apply_icon(dlg)
        dlg.title("Edit Local Channel" if edit_mode else "Build Local Channel")
        dlg.configure(bg=BG)
        dlg.resizable(True, True)
        dlg.grab_set()
        dlg.focus_force()
        self._center_dialog(dlg, 680, 660)
        dlg.minsize(560, 600)

        border = tk.Frame(dlg, bg=FG, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg=BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        hdr_text = "✎  EDIT LOCAL CHANNEL" if edit_mode else "📁  BUILD LOCAL CHANNEL"
        title_bar = tk.Frame(inner, bg=FG_DIM)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text=f"  {hdr_text}", font=self.font_small,
                 fg=FG, bg=FG_DIM, anchor='w', pady=6).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(title_bar,
                 text="  Files are saved as an M3U playlist in the  playlists/  folder  ",
                 font=font.Font(family="Courier New", size=8),
                 fg=FG_DIM, bg=FG_DIM, anchor='e').pack(side=tk.RIGHT)

        body = tk.Frame(inner, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        # ── Channel name ──────────────────────────────────────────────────
        name_row = tk.Frame(body, bg=BG)
        name_row.pack(fill=tk.X, pady=(0, 10))
        tk.Label(name_row, text="Channel Name:", font=self.font_ch,
                 fg=FG_DIM, bg=BG, width=14, anchor='w').pack(side=tk.LEFT)
        var_name = tk.StringVar(value=initial_name)
        ent_name = tk.Entry(name_row, textvariable=var_name,
                            bg=ENTRY_BG, fg=FG, insertbackground=FG,
                            font=self.font_ch, relief=tk.FLAT, bd=0,
                            highlightbackground=FG_DIM, highlightthickness=1)
        ent_name.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── File list header ──────────────────────────────────────────────
        hdr_row = tk.Frame(body, bg=BG)
        hdr_row.pack(fill=tk.X, pady=(0, 4))
        tk.Label(hdr_row, text="VIDEO FILES", font=self.font_ch,
                 fg=FG_DIM, bg=BG, anchor='w').pack(side=tk.LEFT)
        count_lbl = tk.Label(hdr_row, text="", font=self.font_ch, fg=FG_DIM, bg=BG)
        count_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # ── File list + side buttons ──────────────────────────────────────
        list_outer = tk.Frame(body, bg=BG)
        list_outer.pack(fill=tk.BOTH, expand=True)

        lf = tk.Frame(list_outer, bg=ENTRY_BG,
                      highlightthickness=1, highlightbackground=FG_DIM)
        lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_scroll = tk.Scrollbar(lf, bg=BTN_BG, troughcolor=ENTRY_BG, width=8)
        lb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        file_lb = tk.Listbox(lf, bg=ENTRY_BG, fg=self.BEIGE,
                              selectbackground=FG_DIM, selectforeground=FG,
                              font=self.font_ch, relief=tk.FLAT, bd=0,
                              highlightthickness=0, activestyle='none',
                              yscrollcommand=lb_scroll.set,
                              selectmode=tk.EXTENDED)
        file_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_scroll.config(command=file_lb.yview)

        btn_col = tk.Frame(list_outer, bg=BG)
        btn_col.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))

        def _sb(text, cmd):
            b = tk.Button(btn_col, text=text, command=cmd,
                          bg=BTN_BG, fg=FG, activebackground=BTN_HOV, activeforeground=FG,
                          font=self.font_ch, relief=tk.FLAT, padx=8, pady=5, cursor='hand2',
                          highlightthickness=1, highlightbackground=FG_DIM)
            b.pack(fill=tk.X, pady=(0, 4))
            return b

        def _divider():
            tk.Frame(btn_col, bg=FG_DIM, height=1).pack(fill=tk.X, pady=(0, 4))

        def _is_missing(f):
            return '://' not in f and not os.path.exists(f)

        def refresh_lb(select=None):
            """Redraw the list; `select` is an iterable of indices to re-select."""
            top = file_lb.yview()[0]
            file_lb.delete(0, tk.END)
            missing = 0
            for i, f in enumerate(files):
                gone = _is_missing(f)
                missing += gone
                file_lb.insert(tk.END, f"{'⚠' if gone else ' '} {i + 1:>3}.  {os.path.basename(f)}")
                if gone:
                    file_lb.itemconfig(i, fg=self.GUIDE_RED)
            n = len(files)
            txt = f"({n} file{'s' if n != 1 else ''})"
            if missing:
                txt += f"  ⚠ {missing} missing"
            count_lbl.config(text=txt)
            file_lb.yview_moveto(top)
            for i in (select or ()):
                if 0 <= i < len(files):
                    file_lb.selection_set(i)
            sel = list(select or ())
            if sel:
                file_lb.see(sel[0])

        def _insert_files(new_paths, what="file(s)"):
            """Insert after the last selected row, or append if nothing selected."""
            existing = set(files)
            added = [p for p in new_paths if p not in existing]
            if not added:
                status_lbl.config(text=f"  No new {what} to add.", fg=FG_DIM)
                return
            sel = file_lb.curselection()
            pos = (sel[-1] + 1) if sel else len(files)
            files[pos:pos] = added
            refresh_lb(range(pos, pos + len(added)))
            status_lbl.config(text=f"✔  Added {len(added)} {what}.", fg=self.GUIDE_GREEN)

        def add_files():
            from tkinter import filedialog
            ext_str = " ".join(sorted(f"*{e}" for e in VIDEO_EXT))
            paths = filedialog.askopenfilenames(
                parent=dlg, title="Add Video Files",
                filetypes=[("Video files", ext_str), ("All files", "*.*")])
            if paths:
                _insert_files([os.path.normpath(p) for p in paths])

        def add_folder():
            from tkinter import filedialog
            folder = filedialog.askdirectory(parent=dlg,
                                              title="Add All Videos from Folder")
            if not folder:
                return
            found = [os.path.normpath(e.path)
                     for e in sorted(os.scandir(folder), key=lambda e: e.name.lower())
                     if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXT]
            _insert_files(found, "file(s) from folder")

        def remove_sel():
            sel = list(file_lb.curselection())
            if not sel:
                return
            for idx in reversed(sel):
                files.pop(idx)
            refresh_lb([min(sel[0], len(files) - 1)] if files else None)
            status_lbl.config(text=f"  Removed {len(sel)} file(s).", fg=FG_DIM)

        def remove_missing():
            before = len(files)
            files[:] = [f for f in files if not _is_missing(f)]
            refresh_lb()
            status_lbl.config(text=f"  Removed {before - len(files)} missing file(s).", fg=FG_DIM)

        def _move(delta):
            """Move the whole selection (may be non-contiguous) by one step."""
            sel = list(file_lb.curselection())
            if not sel:
                return
            if (delta < 0 and sel[0] == 0) or (delta > 0 and sel[-1] >= len(files) - 1):
                return
            order = sel if delta < 0 else list(reversed(sel))
            for i in order:
                files[i], files[i + delta] = files[i + delta], files[i]
            refresh_lb([i + delta for i in sel])

        def _move_to_end(top):
            sel = list(file_lb.curselection())
            if not sel:
                return
            picked = [files[i] for i in sel]
            rest = [f for i, f in enumerate(files) if i not in set(sel)]
            files[:] = picked + rest if top else rest + picked
            start = 0 if top else len(rest)
            refresh_lb(range(start, start + len(picked)))

        def sort_az():
            sel_paths = {files[i] for i in file_lb.curselection()}
            files.sort(key=lambda p: os.path.basename(p).lower())
            refresh_lb([i for i, f in enumerate(files) if f in sel_paths])
            status_lbl.config(text="  Sorted A–Z by filename.", fg=FG_DIM)

        # ── Drag-to-reorder (plain click-drag; Shift/Ctrl keep normal selection) ──
        _drag = {"idx": None}

        def _on_press(e):
            if e.state & 0x0005:            # Shift or Control held
                _drag["idx"] = None
                return
            _drag["idx"] = file_lb.nearest(e.y) if files else None

        def _on_drag(e):
            src = _drag["idx"]
            if src is None:
                return
            dst = file_lb.nearest(e.y)
            if dst == src or not (0 <= dst < len(files)):
                return "break"
            files.insert(dst, files.pop(src))
            _drag["idx"] = dst
            file_lb.selection_clear(0, tk.END)
            refresh_lb([dst])
            return "break"

        file_lb.bind("<ButtonPress-1>", _on_press, add="+")
        file_lb.bind("<B1-Motion>", _on_drag)
        file_lb.bind("<Delete>", lambda e: remove_sel())
        file_lb.bind("<Alt-Up>", lambda e: (_move(-1), "break")[1])
        file_lb.bind("<Alt-Down>", lambda e: (_move(1), "break")[1])

        _sb("＋ Add Files",  add_files)
        _sb("📁 Add Folder", add_folder)
        _divider()
        _sb("⤒ To Top",      lambda: _move_to_end(True))
        _sb("▲ Move Up",     lambda: _move(-1))
        _sb("▼ Move Down",   lambda: _move(1))
        _sb("⤓ To Bottom",   lambda: _move_to_end(False))
        _sb("A–Z Sort",      sort_az)
        _divider()
        _sb("✖ Remove",      remove_sel)
        _sb("⚠ Drop Missing", remove_missing)

        tk.Label(body, text="Tip: drag rows to reorder · Shift/Ctrl-click to select several · "
                            "new files are inserted after the selection · Del removes",
                 font=font.Font(family="Courier New", size=7),
                 fg="#555544", bg=BG, anchor='w').pack(fill=tk.X, pady=(4, 0))

        # ── Options ───────────────────────────────────────────────────────
        opts_row = tk.Frame(body, bg=BG)
        opts_row.pack(fill=tk.X, pady=(10, 0))
        var_shuffle = tk.BooleanVar(value=bool(initial_shuffle))
        var_comm    = tk.BooleanVar(value=bool(initial_comm))
        ck_kw = dict(bg=BG, fg=FG, selectcolor=ENTRY_BG,
                     activebackground=BG, activeforeground=FG, font=self.font_ch)
        tk.Checkbutton(opts_row, text="Shuffle playback order",
                       variable=var_shuffle, **ck_kw).pack(side=tk.LEFT)
        tk.Checkbutton(opts_row, text="Include commercial breaks",
                       variable=var_comm, **ck_kw).pack(side=tk.LEFT, padx=(16, 0))

        # ── Status label ─────────────────────────────────────────────────
        status_lbl = tk.Label(body, text="", font=self.font_ch,
                               fg=self.GUIDE_GREEN, bg=BG, anchor='w')
        status_lbl.pack(fill=tk.X, pady=(8, 0))

        # ── Bottom bar ────────────────────────────────────────────────────
        bottom = tk.Frame(inner, bg=BG, highlightthickness=1,
                          highlightbackground="#3d2f1e")
        # Pack before `body` so the Save/Cancel bar always keeps its space;
        # otherwise a tall button column squeezes it off the bottom of the window.
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 8), before=body)

        def do_commit():
            name = var_name.get().strip()
            if not name:
                status_lbl.config(text="✖  Channel name is required.",
                                   fg=self.GUIDE_RED)
                ent_name.focus_set()
                return
            if not files:
                status_lbl.config(text="✖  Add at least one video file.",
                                   fg=self.GUIDE_RED)
                return
            # Resolve target M3U path
            playlists_dir = os.path.join(self.base_dir, "playlists")
            os.makedirs(playlists_dir, exist_ok=True)
            edit_abs = None
            if edit_m3u_path:
                edit_abs = (edit_m3u_path if os.path.isabs(edit_m3u_path)
                            else os.path.join(self.base_dir, edit_m3u_path))
                # Only overwrite M3U files in place — never write M3U text
                # into an XSPF (or other) file, which would corrupt it.
                if os.path.splitext(edit_abs)[1].lower() not in ('.m3u', '.m3u8'):
                    edit_abs = None
            if edit_abs:
                m3u_abs = edit_abs
            else:
                safe = re.sub(r'[^\w\- ]', '_', name).strip().replace(' ', '_')
                m3u_abs = os.path.join(playlists_dir, f"{safe}.m3u")
                n = 1
                while os.path.exists(m3u_abs):
                    m3u_abs = os.path.join(playlists_dir, f"{safe}_{n}.m3u")
                    n += 1
            try:
                with open(m3u_abs, 'w', encoding='utf-8') as fh:
                    fh.write("#EXTM3U\n")
                    for path in files:
                        title = os.path.splitext(os.path.basename(path))[0]
                        fh.write(f"#EXTINF:-1,{title}\n{path}\n")
            except Exception as exc:
                status_lbl.config(text=f"✖  Could not save playlist: {exc}",
                                   fg=self.GUIDE_RED)
                return
            try:
                rel = os.path.relpath(m3u_abs, self.base_dir)
            except ValueError:
                rel = m3u_abs  # different drive on Windows — keep absolute
            on_commit(name, rel, var_shuffle.get(), var_comm.get())
            dlg.destroy()

        btn_label = "✔  SAVE CHANGES" if edit_mode else "✔  CREATE CHANNEL"
        tk.Button(bottom, text=btn_label, command=do_commit,
                  bg=self.AMBER_DIM, fg=FG, activebackground=FG,
                  activeforeground="#000000", font=self.font_small,
                  relief=tk.FLAT, padx=12, pady=6,
                  cursor='hand2').pack(side=tk.RIGHT, padx=(6, 6), pady=6)
        tk.Button(bottom, text="CANCEL", command=dlg.destroy,
                  bg=BTN_BG, fg=FG_DIM, activebackground=BTN_BG, activeforeground=FG,
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6,
                  cursor='hand2').pack(side=tk.RIGHT, pady=6)
        tk.Label(bottom,
                 text=f"  Playlist saved to  playlists/  next to the app",
                 font=font.Font(family="Courier New", size=8),
                 fg=FG_DIM, bg=BG, anchor='w').pack(side=tk.LEFT, padx=6)

        dlg.bind("<Control-s>", lambda e: do_commit())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        refresh_lb()
        ent_name.focus_set()

    # ══════════════════════════════════════════
    #  EPG Sources Editor
    # ══════════════════════════════════════════
    def _open_epg_sources_editor(self):
        if getattr(self, '_epg_editor_open', False): return
        self._epg_editor_open = True
        user_config_path = os.path.join(self.base_dir, "user_config.json")

        d = tk.Toplevel(self)
        self._apply_icon(d)
        d.title("EPG Sources"); d.configure(bg="#1c1308")
        d.resizable(True, True); d.grab_set(); d.focus_force()
        self._center_dialog(d, 620, 420)
        d.minsize(480, 340)

        def on_close():
            self._epg_editor_open = False; d.destroy()
        d.protocol("WM_DELETE_WINDOW", on_close)
        d.bind("<Escape>", lambda e: on_close())

        border = tk.Frame(d, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        title_bar = tk.Frame(inner, bg=self.AMBER_DIM)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="  📡  EPG SOURCES", font=self.font_small,
                 fg=self.AMBER, bg=self.AMBER_DIM, anchor="w", pady=6).pack(side=tk.LEFT)
        tk.Label(title_bar,
                 text="  Add XMLTV URLs or local .xml file paths  ",
                 font=font.Font(family="Courier New", size=8),
                 fg=self.AMBER_DIM, bg=self.AMBER_DIM, anchor="e").pack(side=tk.RIGHT)

        # ── Source list ───────────────────────────────────────────────────
        list_outer = tk.Frame(inner, bg="#1c1308")
        list_outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))
        tk.Label(list_outer, text="CUSTOM EPG SOURCES  (fetched on startup, merged with built-in guide)",
                 font=font.Font(family="Courier New", size=8),
                 fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(fill=tk.X, pady=(0, 4))
        lf = tk.Frame(list_outer, bg="#0d0a06",
                      highlightthickness=1, highlightbackground=self.AMBER_DIM)
        lf.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(lf, bg="#2a1f10", troughcolor="#0d0a06", width=8)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        src_listbox = tk.Listbox(lf, bg="#0d0a06", fg=self.BEIGE,
                                 selectbackground=self.AMBER_DIM, selectforeground=self.AMBER,
                                 font=font.Font(family="Courier New", size=9),
                                 relief=tk.FLAT, bd=0, highlightthickness=0,
                                 activestyle='none', yscrollcommand=sb.set)
        src_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=src_listbox.yview)

        # Populate from current in-memory list
        sources = list(self._user_epg_sources)
        def refresh_list(select_idx=None):
            src_listbox.delete(0, tk.END)
            for s in sources:
                src_listbox.insert(tk.END, f"  {s}")
            if select_idx is not None and 0 <= select_idx < len(sources):
                src_listbox.selection_set(select_idx)
                src_listbox.see(select_idx)
        refresh_list()

        # ── Entry row for adding/editing ──────────────────────────────────
        entry_row = tk.Frame(inner, bg="#1c1308")
        entry_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        ent_kw = dict(bg="#0d0a06", fg=self.AMBER, insertbackground=self.AMBER,
                      font=font.Font(family="Courier New", size=9),
                      relief=tk.FLAT, bd=0,
                      highlightbackground=self.AMBER_DIM, highlightthickness=1)
        var_src = tk.StringVar()
        src_entry = tk.Entry(entry_row, textvariable=var_src, **ent_kw)
        src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        tk.Label(entry_row, text="URL or file path",
                 font=font.Font(family="Courier New", size=7),
                 fg="#555544", bg="#1c1308").pack(side=tk.LEFT)

        def browse_file():
            from tkinter import filedialog
            path = filedialog.askopenfilename(
                parent=d, title="Select XMLTV File",
                filetypes=[("XMLTV files", "*.xml"), ("All files", "*.*")])
            if path:
                var_src.set(path)
                src_entry.focus_set()

        def add_source():
            val = var_src.get().strip()
            if not val: return
            if val not in sources:
                sources.append(val)
                refresh_list(len(sources) - 1)
            var_src.set("")

        def remove_selected():
            sel = src_listbox.curselection()
            if not sel: return
            idx = sel[0]
            sources.pop(idx)
            refresh_list(min(idx, len(sources) - 1) if sources else None)

        def move_up():
            sel = src_listbox.curselection()
            if not sel or sel[0] == 0: return
            i = sel[0]
            sources[i-1], sources[i] = sources[i], sources[i-1]
            refresh_list(i - 1)

        def move_down():
            sel = src_listbox.curselection()
            if not sel or sel[0] >= len(sources) - 1: return
            i = sel[0]
            sources[i], sources[i+1] = sources[i+1], sources[i]
            refresh_list(i + 1)

        # Clicking a row populates the entry for easy editing
        def on_select(e):
            sel = src_listbox.curselection()
            if sel:
                var_src.set(sources[sel[0]])
        src_listbox.bind("<<ListboxSelect>>", on_select)

        # ── Button row ────────────────────────────────────────────────────
        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        btn_kw = dict(bg="#2a1f10", fg=self.AMBER,
                      activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                      font=self.font_ch, relief=tk.FLAT, padx=8, pady=4, bd=0,
                      cursor="hand2",
                      highlightthickness=1, highlightbackground=self.AMBER_DIM)
        tk.Button(btn_row, text="📂 Browse…",  command=browse_file,    **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="＋ Add",       command=add_source,     **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="✖ Remove",    command=remove_selected, **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="▲",           command=move_up,         **btn_kw).pack(side=tk.LEFT, padx=(0, 2))
        tk.Button(btn_row, text="▼",           command=move_down,       **btn_kw).pack(side=tk.LEFT)
        src_entry.bind("<Return>", lambda e: add_source())

        # ── Bottom bar ────────────────────────────────────────────────────
        status_var = tk.StringVar()
        bottom = tk.Frame(inner, bg="#1c1308",
                          highlightthickness=1, highlightbackground="#3d2f1e")
        bottom.pack(fill=tk.X, padx=8, pady=(4, 8))
        tk.Label(bottom, textvariable=status_var,
                 font=font.Font(family="Courier New", size=8),
                 fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(side=tk.LEFT, padx=6)

        def apply_and_close():
            try:
                existing = _load_user_config_dict(user_config_path)
                existing["epg_urls"] = sources
                with open(user_config_path, 'w', encoding='utf-8') as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)
                # Resolve paths and update in-memory list
                resolved = []
                for s in sources:
                    if not s.startswith(("http://", "https://")) and not os.path.isabs(s):
                        s = os.path.join(self.base_dir, s)
                    resolved.append(s)
                self._user_epg_sources = resolved
                # Re-fetch EPG with the new sources
                self._epg_loaded  = False
                self._epg_loading = False
                self._fetch_epg_async()
                on_close()
            except Exception as e:
                status_var.set(f"✖  Could not save: {e}")

        tk.Button(bottom, text="✔  APPLY & CLOSE", command=apply_and_close,
                  bg=self.AMBER_DIM, fg=self.AMBER,
                  activebackground=self.AMBER, activeforeground="#000000",
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT, padx=(6, 6), pady=6)
        tk.Button(bottom, text="CANCEL", command=on_close,
                  bg="#2a1f10", fg=self.AMBER_DIM,
                  activebackground="#2a1f10", activeforeground=self.AMBER,
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT, pady=6)

    # ══════════════════════════════════════════
    #  DVR — Digital Video Recording
    # ══════════════════════════════════════════
    def _find_ffmpeg(self):
        """Return the path to ffmpeg executable, or None if not found."""
        found = shutil.which("ffmpeg")
        if found:
            return found
        candidates = [
            os.path.join(self.base_dir, "ffmpeg.exe"),
            os.path.join(self.base_dir, "ffmpeg", "bin", "ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def _dvr_toggle(self):
        """Toggle DVR recording on/off for the current channel."""
        if self._dvr_process and self._dvr_process.poll() is None:
            self._dvr_stop()
        else:
            self._dvr_start()

    def _dvr_start(self):
        """Start recording the current channel with ffmpeg."""
        if not self.channels:
            return
        ch = self.channels[self.current_channel_idx]
        if ch.number == 0:
            self._show_osd("No recording\nfor guide ch")
            return
        if not self._dvr_ffmpeg:
            self._dvr_show_no_ffmpeg()
            return

        os.makedirs(self._dvr_recordings_dir, exist_ok=True)
        safe_name = re.sub(r'[^\w\-]', '_', ch.name)
        ts_str    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"{safe_name}_{ts_str}.ts"
        out_path  = os.path.join(self._dvr_recordings_dir, filename)

        if ch.is_local and self._local_track_order:
            actual_idx = self._local_track_order[self._local_track_idx % len(self._local_track_order)]
            url = ch.playlist[actual_idx]
        elif ch.url:
            url = ch.url
        else:
            self._show_osd("Nothing\nto record")
            return

        cmd = [
            self._dvr_ffmpeg, "-y",
            "-loglevel", "warning",
            "-i", url,
            "-c", "copy",
            "-fflags", "+genpts",
            out_path,
        ]
        try:
            create_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self._dvr_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=create_flags,
            )
        except Exception as e:
            log.error("DVR start failed: %s", e)
            self._show_osd(f"REC FAILED\n{str(e)[:22]}")
            return

        self._dvr_channel  = ch
        self._dvr_out_path = out_path
        self._dvr_start_ts = datetime.datetime.now()
        log.info("DVR started: %s → %s", url, out_path)
        self._dvr_update_button()
        self._dvr_blink_start()
        self._show_osd(f"● REC STARTED\n{ch.name}")

    def _dvr_stop(self):
        """Stop an active DVR recording."""
        proc = self._dvr_process
        if proc and proc.poll() is None:
            try:
                # Send 'q' to ffmpeg's stdin for a graceful shutdown that
                # flushes write buffers and finalises the output file properly.
                proc.stdin.write(b'q')
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=8)
            except Exception:
                try: proc.terminate()
                except Exception: pass
                try: proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        self._dvr_process  = None
        self._dvr_blink_stop()
        self._dvr_update_button()
        out = self._dvr_out_path
        self._dvr_out_path = None
        self._dvr_channel  = None
        self._dvr_start_ts = None
        if out and os.path.exists(out):
            sz = os.path.getsize(out)
            sz_str = f"{sz // 1_048_576} MB" if sz >= 1_048_576 else f"{sz // 1024} KB"
            self._show_osd(f"■ REC SAVED\n{sz_str}")
            log.info("DVR stopped: %s  (%s)", out, sz_str)
        else:
            self._show_osd("■ REC STOPPED")

    def _dvr_update_button(self):
        """Sync the ● REC button label/colour to current recording state."""
        if not hasattr(self, '_dvr_btn'):
            return
        recording = bool(self._dvr_process and self._dvr_process.poll() is None)
        if recording:
            self._dvr_btn.config(text="■ STOP REC", fg="#ff4444", bg="#2a0000",
                                 highlightbackground="#cc0000")
            self._dvr_btn.unbind("<Leave>")
            self._dvr_btn.bind("<Leave>", lambda e: self._dvr_btn.config(bg="#2a0000"))
        else:
            self._dvr_btn.config(text="● REC", fg="#cc0000", bg="#2a1f10",
                                 highlightbackground=self.AMBER_DIM)
            self._dvr_btn.unbind("<Leave>")
            self._dvr_btn.bind("<Leave>", lambda e: self._dvr_btn.config(bg="#2a1f10"))

    def _dvr_blink_start(self):
        """Place the on-screen ● REC indicator and start blinking."""
        self._dvr_blink_on = True
        try:
            vx = self.video_frame.winfo_x()
            vy = self.video_frame.winfo_y()
            self._dvr_rec_label.place(x=vx + 10, y=vy + 10, anchor="nw")
            tk.Widget.lift(self._dvr_rec_label)
        except Exception:
            pass
        self._dvr_blink()

    def _dvr_blink(self):
        """Toggle the ● REC indicator colour every 700 ms."""
        if not (self._dvr_process and self._dvr_process.poll() is None):
            self._dvr_blink_stop()
            # Process exited on its own — clean up state so the button reflects reality.
            self._dvr_process  = None
            self._dvr_channel  = None
            self._dvr_out_path = None
            self._dvr_start_ts = None
            self._dvr_update_button()
            return
        try:
            self._dvr_rec_label.config(fg="#cc0000" if self._dvr_blink_on else "#330000")
        except Exception:
            pass
        self._dvr_blink_on  = not self._dvr_blink_on
        self._dvr_blink_id  = self.after(700, self._dvr_blink)

    def _dvr_blink_stop(self):
        """Cancel blink timer and hide the on-screen indicator."""
        if self._dvr_blink_id:
            try: self.after_cancel(self._dvr_blink_id)
            except Exception: pass
            self._dvr_blink_id = None
        try:
            self._dvr_rec_label.place_forget()
        except Exception:
            pass

    def _dvr_show_no_ffmpeg(self):
        """Show a dialog explaining that ffmpeg is required."""
        dialog = tk.Toplevel(self)
        self._apply_icon(dialog)
        dialog.title("DVR — ffmpeg Required")
        dialog.configure(bg="#1c1308")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.focus_force()
        self._center_dialog(dialog, 480, 220)

        border = tk.Frame(dialog, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner  = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        tk.Label(inner, text="  ● DVR — ffmpeg Not Found",
                 font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor="w", pady=6).pack(fill=tk.X)
        msg = (
            "DVR recording requires ffmpeg.\n\n"
            "1.  Download ffmpeg from  ffmpeg.org\n"
            "2.  Place  ffmpeg.exe  next to  tv.py  (or  tv.exe)\n"
            "       — OR —\n"
            "    Add ffmpeg to your system PATH\n\n"
            "Then restart RetroTV."
        )
        tk.Label(inner, text=msg,
                 font=font.Font(family="Courier New", size=9),
                 fg=self.TEXT_CLR, bg="#1c1308",
                 justify="left", anchor="w", padx=16, pady=10).pack(fill=tk.BOTH, expand=True)
        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(btn_row, text="  OK  ", command=dialog.destroy,
                  bg=self.AMBER_DIM, fg=self.AMBER,
                  activebackground=self.AMBER, activeforeground="#000000",
                  font=self.font_small, relief=tk.FLAT, padx=14, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT)
        dialog.bind("<Escape>", lambda e: dialog.destroy())
        dialog.bind("<Return>", lambda e: dialog.destroy())

    # ── DVR Schedule (EPG-based recording) ───────────────────────────────────

    def _dvr_load_schedules(self):
        """Load dvr_schedule.json, re-arm timers for pending entries."""
        path = os.path.join(self.base_dir, "dvr_schedule.json")
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._dvr_schedules = data.get('schedules', [])
        except Exception as e:
            log.error("DVR load schedules: %s", e)
            self._dvr_schedules = []
        now_utc = datetime.datetime.utcnow()
        changed = False
        for sched in self._dvr_schedules:
            if sched.get('status') == 'recording':
                sched['status'] = 'failed'
                changed = True
            elif sched.get('status') == 'pending':
                try:
                    stop_utc = datetime.datetime.fromisoformat(sched['stop_utc'])
                except Exception:
                    stop_utc = None
                if stop_utc and stop_utc < now_utc:
                    sched['status'] = 'missed'
                    changed = True
                else:
                    self._dvr_arm_schedule(sched)
        if changed:
            self._dvr_save_schedules()
        log.info("DVR: loaded %d scheduled entries", len(self._dvr_schedules))

    def _dvr_save_schedules(self):
        """Write schedule list to dvr_schedule.json (strips non-serialisable _proc field)."""
        path = os.path.join(self.base_dir, "dvr_schedule.json")
        try:
            clean = [{k: v for k, v in s.items() if k != '_proc'}
                     for s in self._dvr_schedules]
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({'schedules': clean}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.error("DVR save schedules: %s", e)

    def _dvr_is_scheduled(self, channel_id, start_utc_iso):
        """Return True if a pending/recording schedule exists for this channel+start."""
        if not channel_id:
            return False
        for s in self._dvr_schedules:
            if (s.get('channel_id') == channel_id
                    and s.get('start_utc') == start_utc_iso
                    and s.get('status') in ('pending', 'recording')):
                return True
        return False

    def _dvr_schedule_program(self, ch, prog):
        """Add a DVR schedule for prog on ch, persist, and arm the start timer."""
        if not self._dvr_ffmpeg:
            self._dvr_show_no_ffmpeg()
            return False
        start_dt = prog.get('start')
        stop_dt  = prog.get('stop')
        if start_dt is None or not (ch.url or ch.is_local):
            return False
        start_iso = start_dt.isoformat()
        if self._dvr_is_scheduled(ch.channel_id, start_iso):
            return False
        stop_iso = stop_dt.isoformat() if stop_dt else ''
        url = ch.url if ch.url else (ch.playlist[0] if ch.playlist else None)
        if not url:
            return False
        sched = {
            'id':           str(_uuid_mod.uuid4()),
            'channel_name': ch.name,
            'channel_id':   ch.channel_id or '',
            'url':          url,
            'title':        _format_prog_title(prog),
            'start_utc':    start_iso,
            'stop_utc':     stop_iso,
            'status':       'pending',
            'out_path':     None,
        }
        self._dvr_schedules.append(sched)
        self._dvr_save_schedules()
        self._dvr_arm_schedule(sched)
        log.info("DVR scheduled: '%s' on %s @ %s", sched['title'], ch.name, start_iso)
        if self._guide_active and self._guide_canvas:
            self._draw_epg_grid()
        return True

    def _dvr_arm_schedule(self, sched):
        """Fire a threading.Timer to start the recording at sched['start_utc']."""
        sched_id = sched['id']
        try:
            start_utc = datetime.datetime.fromisoformat(sched['start_utc'])
        except Exception:
            return
        delay = max(0.0, (start_utc - datetime.datetime.utcnow()).total_seconds())
        t = threading.Timer(delay,
                            lambda sid=sched_id: self.after(0, self._dvr_start_scheduled, sid))
        t.daemon = True
        t.name   = f"dvr-start-{sched_id[:8]}"
        t.start()
        self._dvr_timers[sched_id] = (t, None)
        log.info("DVR timer armed: '%s' in %.0fs", sched.get('title', '?'), delay)

    def _dvr_start_scheduled(self, sched_id):
        """Main-thread: start ffmpeg for a scheduled entry."""
        if self._closing:
            return
        sched = next((s for s in self._dvr_schedules if s['id'] == sched_id), None)
        if sched is None or sched.get('status') != 'pending':
            return
        if not self._dvr_ffmpeg:
            sched['status'] = 'failed'
            self._dvr_save_schedules()
            return
        os.makedirs(self._dvr_recordings_dir, exist_ok=True)
        safe_ch    = re.sub(r'[^\w\-]', '_', sched.get('channel_name', 'CH'))
        safe_title = re.sub(r'[^\w\-]', '_', sched.get('title', 'rec'))[:40]
        try:
            start_dt = datetime.datetime.fromisoformat(sched['start_utc'])
            ts_str   = start_dt.strftime("%Y%m%d_%H%M")
        except Exception:
            ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(self._dvr_recordings_dir,
                                f"{safe_ch}_{safe_title}_{ts_str}.ts")
        cmd = [
            self._dvr_ffmpeg, "-y", "-loglevel", "warning",
            "-i", sched['url'],
            "-c", "copy", "-fflags", "+genpts",
            out_path,
        ]
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            proc  = subprocess.Popen(cmd,
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE,
                                     creationflags=flags)
        except Exception as e:
            log.error("DVR scheduled start failed: %s", e)
            sched['status'] = 'failed'
            self._dvr_save_schedules()
            return
        sched['status']   = 'recording'
        sched['out_path'] = out_path
        sched['_proc']    = proc
        self._dvr_save_schedules()
        log.info("DVR recording started: '%s' → %s", sched.get('title'), out_path)
        self._show_osd(f"● SCHED REC\n{sched.get('title','')[:22]}")
        # Arm the stop timer
        try:
            stop_utc = datetime.datetime.fromisoformat(sched['stop_utc'])
            stop_delay = max(0.0, (stop_utc - datetime.datetime.utcnow()).total_seconds())
        except Exception:
            stop_delay = 3600.0
        t_stop = threading.Timer(
            stop_delay,
            lambda sid=sched_id, p=proc: self.after(0, self._dvr_stop_scheduled, sid, p))
        t_stop.daemon = True
        t_stop.name   = f"dvr-stop-{sched_id[:8]}"
        t_stop.start()
        t_start = self._dvr_timers.get(sched_id, (None, None))[0]
        self._dvr_timers[sched_id] = (t_start, t_stop)

    def _dvr_stop_scheduled(self, sched_id, proc=None):
        """Main-thread: stop a scheduled recording process and mark it done."""
        sched = next((s for s in self._dvr_schedules if s['id'] == sched_id), None)
        if proc is None and sched:
            proc = sched.get('_proc')
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b'q')
                proc.stdin.flush()
                proc.stdin.close()
                proc.wait(timeout=8)
            except Exception:
                try: proc.terminate()
                except Exception: pass
                try: proc.wait(timeout=3)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
        if sched:
            sched['status'] = 'done'
            sched.pop('_proc', None)
            out = sched.get('out_path', '')
            self._dvr_save_schedules()
            if out and os.path.exists(out):
                sz     = os.path.getsize(out)
                sz_str = f"{sz // 1_048_576} MB" if sz >= 1_048_576 else f"{sz // 1024} KB"
                log.info("DVR scheduled done: %s (%s)", out, sz_str)
                self._show_osd(f"■ SCHED DONE\n{sz_str}")
        self._dvr_timers.pop(sched_id, None)

    def _dvr_cancel_schedule(self, sched_id):
        """Cancel a pending or active scheduled recording."""
        for t in self._dvr_timers.pop(sched_id, (None, None)):
            if t:
                try: t.cancel()
                except Exception: pass
        sched = next((s for s in self._dvr_schedules if s['id'] == sched_id), None)
        if sched:
            if sched.get('status') == 'recording':
                # _dvr_stop_scheduled sets status='done' and saves; don't overwrite it.
                self._dvr_stop_scheduled(sched_id)
            else:
                sched['status'] = 'cancelled'
                sched.pop('_proc', None)
                self._dvr_save_schedules()
        if self._guide_active and self._guide_canvas:
            self._draw_epg_grid()

    def _dvr_purge_old_schedules(self):
        """Remove done/failed/cancelled/missed entries older than 7 days."""
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        def _keep(s):
            if s.get('status') in ('pending', 'recording'):
                return True
            try:
                return datetime.datetime.fromisoformat(s['stop_utc']) > cutoff
            except Exception:
                return False
        before = len(self._dvr_schedules)
        self._dvr_schedules = [s for s in self._dvr_schedules if _keep(s)]
        if len(self._dvr_schedules) != before:
            self._dvr_save_schedules()

    def _epg_prog_at_click(self, ch, click_x):
        """Return the EPG programme dict at click_x in the guide canvas, or None."""
        if not ch.channel_id or ch.channel_id not in self._epg_data:
            return None
        c = self._guide_canvas
        if c is None:
            return None
        W            = c.winfo_width()
        ch_col_w     = self._EPG_CH_COL_W
        slot_w       = self._epg_slot_width(W)
        total_slot_w = slot_w * self._EPG_SLOTS
        total_secs   = self._EPG_SLOTS * self._EPG_SLOT_MINS * 60
        win_start    = self._epg_window_start()
        win_end      = win_start + datetime.timedelta(seconds=total_secs)
        for prog in self._epg_data[ch.channel_id]:
            p_start = prog['start']
            p_stop  = prog.get('stop') or (p_start + datetime.timedelta(hours=1))
            if p_stop <= win_start or p_start >= win_end:
                continue
            draw_start = max(p_start, win_start)
            draw_stop  = min(p_stop,  win_end)
            x0 = ch_col_w + int((draw_start - win_start).total_seconds() / total_secs * total_slot_w)
            x1 = ch_col_w + int((draw_stop  - win_start).total_seconds() / total_secs * total_slot_w)
            if x0 <= click_x <= x1:
                return prog
        return None

    def _epg_show_prog_popup(self, ch, prog):
        """Floating programme-detail popup with a Schedule Recording button."""
        existing = getattr(self, '_epg_prog_popup', None)
        if existing:
            try: existing.destroy()
            except Exception: pass
        title     = _format_prog_title(prog)
        time_str  = _format_time_range(prog)
        desc      = prog.get('desc', '')
        cat       = prog.get('category', '')
        start_iso = prog['start'].isoformat() if prog.get('start') else ''
        already   = self._dvr_is_scheduled(ch.channel_id, start_iso)

        popup = tk.Toplevel(self)
        self._apply_icon(popup)
        popup.title("Programme Detail")
        popup.configure(bg="#1c1308")
        popup.overrideredirect(True)
        popup.lift()
        self._epg_prog_popup = popup

        try:
            c  = self._guide_canvas
            gx = c.winfo_rootx() + c.winfo_width()  // 2
            gy = c.winfo_rooty() + c.winfo_height() // 2
        except Exception:
            gx, gy = 500, 350
        pw, ph = 400, 230
        popup.geometry(f"{pw}x{ph}+{gx - pw // 2}+{gy - ph // 2}")

        border = tk.Frame(popup, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        inner  = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        hdr = tk.Frame(inner, bg=self.AMBER_DIM)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text=f"  📺  {ch.name}",
                 font=font.Font(family="Courier New", size=9, weight="bold"),
                 fg=self.AMBER, bg=self.AMBER_DIM, anchor="w", pady=4).pack(side=tk.LEFT)
        close_lbl = tk.Label(hdr, text="  ✕  ",
                             font=font.Font(family="Courier New", size=9),
                             fg=self.GUIDE_ACCENT, bg=self.AMBER_DIM,
                             cursor="hand2", pady=4)
        close_lbl.pack(side=tk.RIGHT)
        close_lbl.bind("<Button-1>", lambda e: popup.destroy())

        body = tk.Frame(inner, bg="#1c1308")
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        tk.Label(body, text=title,
                 font=font.Font(family="Courier New", size=11, weight="bold"),
                 fg=self.AMBER, bg="#1c1308", anchor="w",
                 wraplength=370, justify="left").pack(fill=tk.X, pady=(0, 2))
        tk.Label(body, text=time_str,
                 font=font.Font(family="Courier New", size=9),
                 fg=self.GUIDE_ACCENT, bg="#1c1308", anchor="w").pack(fill=tk.X)
        if cat:
            tk.Label(body, text=f"[{cat.upper()}]",
                     font=font.Font(family="Courier New", size=8),
                     fg="#6688cc", bg="#1c1308", anchor="w").pack(fill=tk.X)
        if desc:
            tk.Label(body, text=desc[:160] + ('…' if len(desc) > 160 else ''),
                     font=font.Font(family="Courier New", size=8),
                     fg=self.TEXT_CLR, bg="#1c1308",
                     anchor="nw", wraplength=370, justify="left").pack(fill=tk.X, pady=(4, 0))

        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=10, pady=(4, 8))

        def _do_schedule():
            if self._dvr_schedule_program(ch, prog):
                rec_btn.config(text="✓ SCHEDULED", state=tk.DISABLED, fg=self.GUIDE_GREEN,
                               bg="#002200", highlightbackground=self.GUIDE_GREEN)
            else:
                rec_btn.config(text="✓ ALREADY SCHEDULED", state=tk.DISABLED,
                               fg=self.GUIDE_GREEN, bg="#002200")
            self.after(1000, popup.destroy)

        if already:
            rec_btn = tk.Button(btn_row, text="✓ ALREADY SCHEDULED",
                                state=tk.DISABLED,
                                bg="#002200", fg=self.GUIDE_GREEN,
                                font=self.font_ch, relief=tk.FLAT, padx=10, pady=5,
                                cursor="arrow", highlightthickness=1,
                                highlightbackground=self.GUIDE_GREEN)
        else:
            rec_btn = tk.Button(btn_row, text="● SCHEDULE RECORDING",
                                command=_do_schedule,
                                bg="#2a0000", fg="#ff6666",
                                activebackground="#440000", activeforeground="#ffaaaa",
                                font=self.font_ch, relief=tk.FLAT, padx=10, pady=5,
                                cursor="hand2", highlightthickness=1,
                                highlightbackground="#cc0000")
        rec_btn.pack(side=tk.LEFT)
        tk.Button(btn_row, text="✕ CLOSE", command=popup.destroy,
                  bg="#2a1f10", fg=self.AMBER_DIM,
                  activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                  font=self.font_ch, relief=tk.FLAT, padx=10, pady=5,
                  cursor="hand2", highlightthickness=1,
                  highlightbackground=self.AMBER_DIM).pack(side=tk.RIGHT)

        popup.bind("<Escape>", lambda e: popup.destroy())
        popup.grab_set()
        popup.focus_force()

    def _dvr_open_schedule_manager(self):
        """Dialog listing all scheduled, active, and recent DVR recordings."""
        if getattr(self, '_dvr_sched_mgr_open', False):
            return
        self._dvr_sched_mgr_open = True

        dialog = tk.Toplevel(self)
        self._apply_icon(dialog)
        dialog.title("DVR Schedule Manager")
        dialog.configure(bg="#1c1308")
        dialog.resizable(True, True)
        dialog.grab_set()
        dialog.focus_force()
        self._center_dialog(dialog, 700, 460)
        dialog.minsize(520, 360)

        def on_close():
            self._dvr_sched_mgr_open = False
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", on_close)
        dialog.bind("<Escape>", lambda e: on_close())

        border = tk.Frame(dialog, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner  = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        tk.Label(inner, text="  📅  DVR SCHEDULE MANAGER",
                 font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor="w", pady=6).pack(fill=tk.X)
        tk.Label(inner,
                 text="  Open the guide (CH 0), then click any programme cell to schedule a recording.",
                 font=font.Font(family="Courier New", size=8),
                 fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(fill=tk.X, padx=6, pady=(0, 4))

        lb_outer = tk.Frame(inner, bg="#0d0a06",
                            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        lb_outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 2))
        sb = tk.Scrollbar(lb_outer, bg="#2a1f10", troughcolor="#0d0a06", width=8)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb = tk.Listbox(lb_outer, bg="#0d0a06", fg=self.BEIGE,
                        selectbackground=self.AMBER_DIM, selectforeground=self.AMBER,
                        font=font.Font(family="Courier New", size=9),
                        relief=tk.FLAT, bd=0, highlightthickness=0,
                        activestyle='none', yscrollcommand=sb.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=lb.yview)
        visible_scheds = []

        STATUS_ICON = {
            'pending':   '⏳', 'recording': '● ',
            'done':      '✓ ', 'failed':    '✗ ',
            'cancelled': '—  ', 'missed':   '✗ ',
        }
        STATUS_COLOR = {
            'recording': "#ff6666",
            'done':      self.GUIDE_GREEN,
            'failed':    self.GUIDE_RED,
            'missed':    self.GUIDE_RED,
            'cancelled': self.GUIDE_GRAY,
        }

        def _refresh():
            sel = lb.curselection()
            sel_id = visible_scheds[sel[0]]['id'] if sel and sel[0] < len(visible_scheds) else None
            lb.delete(0, tk.END)
            visible_scheds.clear()
            for s in sorted(self._dvr_schedules,
                            key=lambda x: x.get('start_utc', ''), reverse=True):
                icon    = STATUS_ICON.get(s.get('status', ''), '? ')
                status  = s.get('status', '?').upper()
                try:
                    utc_ts = datetime.datetime.fromisoformat(s['start_utc'])
                    local  = datetime.datetime(*_time.localtime(
                        int(utc_ts.replace(tzinfo=datetime.timezone.utc).timestamp()))[:6])
                    tstr   = local.strftime("%m/%d  %I:%M %p")
                except Exception:
                    tstr   = s.get('start_utc', '')[:16]
                ch_name = s.get('channel_name', '?')[:14]
                title   = s.get('title', '?')[:38]
                lb.insert(tk.END, f"  {icon} {status:<10}  {tstr}  {ch_name:<14}  {title}")
                lb.itemconfig(tk.END, fg=STATUS_COLOR.get(s.get('status', ''), self.BEIGE))
                visible_scheds.append(s)
            if not visible_scheds:
                lb.insert(tk.END, "  (no scheduled recordings)")
            if sel_id:
                for i, s in enumerate(visible_scheds):
                    if s['id'] == sel_id:
                        lb.selection_set(i)
                        break

        _refresh()

        _refresh_job = [None]
        def _auto_refresh():
            if not dialog.winfo_exists(): return
            _refresh()
            _refresh_job[0] = dialog.after(3000, _auto_refresh)
        _refresh_job[0] = dialog.after(3000, _auto_refresh)

        def _cancel_refresh():
            if _refresh_job[0]:
                try: dialog.after_cancel(_refresh_job[0])
                except Exception: pass
        dialog.protocol("WM_DELETE_WINDOW", lambda: [_cancel_refresh(), on_close()])

        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=8, pady=(4, 8))
        btn_kw = dict(bg="#2a1f10", fg=self.AMBER,
                      activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                      font=self.font_ch, relief=tk.FLAT, padx=8, pady=5, bd=0,
                      cursor="hand2",
                      highlightthickness=1, highlightbackground=self.AMBER_DIM)

        def _cancel_selected():
            sel = lb.curselection()
            if not sel or sel[0] >= len(visible_scheds): return
            s = visible_scheds[sel[0]]
            if s.get('status') not in ('pending', 'recording'): return
            if tk_messagebox.askyesno(
                    "Cancel Recording",
                    f"Cancel this scheduled recording?\n\n"
                    f"{s.get('title','?')}\n{s.get('channel_name','?')}",
                    parent=dialog):
                self._dvr_cancel_schedule(s['id'])
                _refresh()

        def _delete_selected():
            sel = lb.curselection()
            if not sel or sel[0] >= len(visible_scheds): return
            s = visible_scheds[sel[0]]
            if not tk_messagebox.askyesno(
                    "Delete Entry",
                    f"Remove this entry from the schedule list?\n\n"
                    f"{s.get('title','?')}\n{s.get('channel_name','?')}",
                    parent=dialog):
                return
            if s.get('status') in ('pending', 'recording'):
                self._dvr_cancel_schedule(s['id'])
            self._dvr_schedules = [x for x in self._dvr_schedules if x['id'] != s['id']]
            self._dvr_save_schedules()
            _refresh()

        def _purge():
            self._dvr_purge_old_schedules()
            _refresh()

        tk.Button(btn_row, text="✖ Cancel Selected", command=_cancel_selected, **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="🗑 Delete",          command=_delete_selected, **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="➕ Manual",          command=lambda: self._dvr_open_manual_schedule_dialog(dialog, _refresh), **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="🗑 Purge Old",       command=_purge,            **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="↺ Refresh",          command=_refresh,          **btn_kw).pack(side=tk.LEFT)
        tk.Button(btn_row, text="✔ Close", command=on_close,
                  bg=self.AMBER_DIM, fg=self.AMBER,
                  activebackground=self.AMBER, activeforeground="#000000",
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT)

    def _dvr_open_manual_schedule_dialog(self, parent, refresh_fn):
        """Dialog to manually schedule a DVR recording without EPG data."""
        if not self._dvr_ffmpeg:
            self._dvr_show_no_ffmpeg()
            return
        ch_list = [ch for ch in (self.channels or []) if ch.number != 0 and ch.url]
        if not ch_list:
            tk_messagebox.showwarning("No Channels", "No recordable channels found.", parent=parent)
            return

        dlg = tk.Toplevel(parent)
        self._apply_icon(dlg)
        dlg.title("Manual Schedule")
        dlg.configure(bg="#1c1308")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.focus_force()
        dlg.transient(parent)
        self._center_dialog(dlg, 440, 320)
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        border = tk.Frame(dlg, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        tk.Label(inner, text="  📅  MANUAL SCHEDULE RECORDING",
                 font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor="w", pady=6).pack(fill=tk.X)

        form = tk.Frame(inner, bg="#1c1308")
        form.pack(fill=tk.X, padx=14, pady=8)

        lbl_kw = dict(font=self.font_ch, fg=self.AMBER_DIM, bg="#1c1308", anchor="e", width=7)
        ent_kw = dict(font=self.font_ch, bg="#0d0a06", fg=self.AMBER,
                      insertbackground=self.AMBER, relief=tk.FLAT,
                      highlightthickness=1, highlightbackground=self.AMBER_DIM)

        tk.Label(form, text="Channel", **lbl_kw).grid(row=0, column=0, padx=(0, 6), pady=5, sticky="e")
        ch_names = [f"CH {c.number:02d}  {c.name}" for c in ch_list]
        ch_var = tk.StringVar(value=ch_names[0])
        ch_combo = ttk.Combobox(form, textvariable=ch_var, values=ch_names,
                                 state="readonly", font=self.font_ch, width=28)
        ch_combo.current(0)
        ch_combo.grid(row=0, column=1, columnspan=2, pady=5, sticky="w")

        tk.Label(form, text="Title", **lbl_kw).grid(row=1, column=0, padx=(0, 6), pady=5, sticky="e")
        title_var = tk.StringVar(value="Manual Recording")
        tk.Entry(form, textvariable=title_var, width=30, **ent_kw).grid(row=1, column=1, columnspan=2, pady=5, sticky="w")

        _now   = datetime.datetime.now().replace(second=0, microsecond=0)
        _start = _now + datetime.timedelta(minutes=5 - (_now.minute % 5) or 5)
        _stop  = _start + datetime.timedelta(hours=1)

        tk.Label(form, text="Date", **lbl_kw).grid(row=2, column=0, padx=(0, 6), pady=5, sticky="e")
        date_var = tk.StringVar(value=_start.strftime("%m/%d/%Y"))
        tk.Entry(form, textvariable=date_var, width=12, **ent_kw).grid(row=2, column=1, pady=5, sticky="w")
        tk.Label(form, text="MM/DD/YYYY", font=self.font_ch, fg=self.AMBER_DIM,
                 bg="#1c1308").grid(row=2, column=2, padx=(6, 0), sticky="w")

        def _make_time_row(row, label, default_dt):
            tk.Label(form, text=label, **lbl_kw).grid(row=row, column=0, padx=(0, 6), pady=5, sticky="e")
            hh_var = tk.StringVar(value=default_dt.strftime("%H"))
            mm_var = tk.StringVar(value=default_dt.strftime("%M"))
            tf = tk.Frame(form, bg="#1c1308")
            tf.grid(row=row, column=1, columnspan=2, pady=5, sticky="w")
            ttk.Spinbox(tf, from_=0, to=23, textvariable=hh_var, format="%02.0f",
                        width=3, wrap=True, font=self.font_ch).pack(side=tk.LEFT)
            tk.Label(tf, text=":", font=self.font_ch, fg=self.AMBER,
                     bg="#1c1308").pack(side=tk.LEFT, padx=1)
            ttk.Spinbox(tf, from_=0, to=59, textvariable=mm_var, format="%02.0f",
                        width=3, wrap=True, font=self.font_ch).pack(side=tk.LEFT)
            tk.Label(tf, text=" local", font=self.font_ch, fg=self.AMBER_DIM,
                     bg="#1c1308").pack(side=tk.LEFT, padx=(6, 0))
            return hh_var, mm_var

        start_hh_var, start_mm_var = _make_time_row(3, "Start", _start)
        stop_hh_var,  stop_mm_var  = _make_time_row(4, "Stop",  _stop)

        err_var = tk.StringVar()
        tk.Label(inner, textvariable=err_var, font=self.font_ch, fg=self.GUIDE_RED,
                 bg="#1c1308", anchor="w").pack(fill=tk.X, padx=14)

        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(fill=tk.X, padx=14, pady=(4, 8))
        btn_kw = dict(bg="#2a1f10", fg=self.AMBER,
                      activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                      font=self.font_ch, relief=tk.FLAT, padx=10, pady=5, bd=0,
                      cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM)

        def _do_schedule():
            sel_idx = ch_combo.current()
            if sel_idx < 0:
                err_var.set("  Select a channel.")
                return
            ch = ch_list[sel_idx]
            title  = title_var.get().strip() or "Manual Recording"
            date_s = date_var.get().strip()
            try:
                base_date = datetime.datetime.strptime(date_s, "%m/%d/%Y")
            except ValueError:
                err_var.set("  Invalid date — use MM/DD/YYYY.")
                return
            try:
                start_t = datetime.time(int(start_hh_var.get()), int(start_mm_var.get()))
            except (ValueError, TypeError):
                err_var.set("  Invalid start time.")
                return
            try:
                stop_t = datetime.time(int(stop_hh_var.get()), int(stop_mm_var.get()))
            except (ValueError, TypeError):
                err_var.set("  Invalid stop time.")
                return
            dt_start_local = datetime.datetime.combine(base_date.date(), start_t)
            dt_stop_local  = datetime.datetime.combine(base_date.date(), stop_t)
            if dt_stop_local <= dt_start_local:
                dt_stop_local += datetime.timedelta(days=1)
            now_local = datetime.datetime.now()
            if dt_start_local < now_local:
                err_var.set("  Start time is already in the past.")
                return
            if dt_stop_local <= now_local:
                err_var.set("  Stop time is already in the past.")
                return
            import time as _tm
            def _local_to_utc(dt):
                # Two-pass: first guess DST via mktime, then confirm it to get a consistent offset.
                ts = _tm.mktime(dt.timetuple())
                isdst = _tm.localtime(ts).tm_isdst
                ts2 = _tm.mktime(dt.timetuple()[:8] + (isdst,))
                return datetime.datetime.utcfromtimestamp(ts2)
            dt_start_utc = _local_to_utc(dt_start_local)
            dt_stop_utc  = _local_to_utc(dt_stop_local)
            sched = {
                'id':           str(_uuid_mod.uuid4()),
                'channel_name': ch.name,
                'channel_id':   ch.channel_id or '',
                'url':          ch.url,
                'title':        title,
                'start_utc':    dt_start_utc.isoformat(),
                'stop_utc':     dt_stop_utc.isoformat(),
                'status':       'pending',
                'out_path':     None,
            }
            self._dvr_schedules.append(sched)
            self._dvr_save_schedules()
            self._dvr_arm_schedule(sched)
            log.info("DVR manual scheduled: '%s' on %s @ %s", title, ch.name, dt_start_utc.isoformat())
            dlg.destroy()
            refresh_fn()

        tk.Button(btn_row, text="📅 Schedule", command=_do_schedule, **btn_kw).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_row, text="✖ Cancel",    command=dlg.destroy,  **btn_kw).pack(side=tk.LEFT)

    def _dvr_open_manager(self):
        """Open the DVR recordings library dialog."""
        if hasattr(self, '_dvr_manager_open') and self._dvr_manager_open:
            return
        self._dvr_manager_open = True

        dialog = tk.Toplevel(self)
        self._apply_icon(dialog)
        dialog.title("DVR Recordings Library")
        dialog.configure(bg="#1c1308")
        dialog.resizable(True, True)
        dialog.grab_set()
        dialog.focus_force()
        self._center_dialog(dialog, 620, 400)
        dialog.minsize(480, 300)

        def on_close():
            self._dvr_manager_open = False
            dialog.destroy()
        dialog.protocol("WM_DELETE_WINDOW", on_close)
        dialog.bind("<Escape>", lambda e: on_close())

        border = tk.Frame(dialog, bg=self.AMBER, bd=0)
        border.pack(fill=tk.BOTH, expand=True, padx=3, pady=3)
        inner  = tk.Frame(border, bg="#1c1308")
        inner.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Title bar
        title_bar = tk.Frame(inner, bg=self.AMBER_DIM)
        title_bar.pack(fill=tk.X)
        tk.Label(title_bar, text="  📼  DVR RECORDINGS LIBRARY",
                 font=self.font_small, fg=self.AMBER, bg=self.AMBER_DIM,
                 anchor="w", pady=6).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Status row: active recording info
        status_var = tk.StringVar(value="No active recording.")
        status_bar = tk.Frame(inner, bg="#0d0a06",
                              highlightthickness=1, highlightbackground=self.AMBER_DIM)
        status_bar.pack(fill=tk.X, padx=8, pady=(6, 2))
        self._dvr_status_lbl = tk.Label(status_bar, textvariable=status_var,
                 font=font.Font(family="Courier New", size=9),
                 fg=self.AMBER, bg="#0d0a06", anchor="w", padx=8, pady=4)
        self._dvr_status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        rec_btn_mgr = tk.Button(status_bar,
                                text="■ STOP REC" if (self._dvr_process and self._dvr_process.poll() is None) else "● REC NOW",
                                command=lambda: [self._dvr_toggle(), _refresh()],
                                bg="#2a0000" if (self._dvr_process and self._dvr_process.poll() is None) else "#2a1f10",
                                fg="#ff4444" if (self._dvr_process and self._dvr_process.poll() is None) else "#cc0000",
                                activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                                font=self.font_ch, relief=tk.FLAT, padx=8, pady=4,
                                cursor="hand2", highlightthickness=1, highlightbackground=self.AMBER_DIM)
        rec_btn_mgr.pack(side=tk.RIGHT, padx=(4, 6), pady=4)

        # Button bar — packed before list so expand=True on list never squeezes it out
        btn_row = tk.Frame(inner, bg="#1c1308")
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(4, 8))

        # Recordings list
        list_frame = tk.Frame(inner, bg="#1c1308")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 2))
        tk.Label(list_frame,
                 text=f"Recordings folder:  {self._dvr_recordings_dir}",
                 font=font.Font(family="Courier New", size=7),
                 fg=self.AMBER_DIM, bg="#1c1308", anchor="w").pack(fill=tk.X)

        lb_outer = tk.Frame(list_frame, bg="#0d0a06",
                            highlightthickness=1, highlightbackground=self.AMBER_DIM)
        lb_outer.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        sb = tk.Scrollbar(lb_outer, bg="#2a1f10", troughcolor="#0d0a06", width=8)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb = tk.Listbox(lb_outer, bg="#0d0a06", fg=self.BEIGE,
                        selectbackground=self.AMBER_DIM, selectforeground=self.AMBER,
                        font=font.Font(family="Courier New", size=9),
                        relief=tk.FLAT, bd=0, highlightthickness=0,
                        activestyle='none', yscrollcommand=sb.set)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=lb.yview)
        recordings = []

        VIDEO_EXTS = {'.ts', '.mkv', '.mp4', '.avi', '.mov', '.wmv'}

        def _refresh():
            sel = lb.curselection()
            prev_sel = sel[0] if sel else None
            lb.delete(0, tk.END)
            recordings.clear()
            if not os.path.isdir(self._dvr_recordings_dir):
                lb.insert(tk.END, "  (no recordings folder yet)")
                status_var.set("No active recording.")
                rec_btn_mgr.config(text="● REC NOW", fg="#cc0000", bg="#2a1f10")
                return
            files = sorted(
                (e for e in os.scandir(self._dvr_recordings_dir)
                 if e.is_file() and os.path.splitext(e.name)[1].lower() in VIDEO_EXTS),
                key=lambda e: e.stat().st_mtime, reverse=True
            )
            if not files:
                lb.insert(tk.END, "  (no recordings yet)")
            for entry in files:
                sz  = entry.stat().st_size
                sz_str = f"{sz // 1_048_576} MB" if sz >= 1_048_576 else f"{sz // 1024} KB"
                mtime  = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
                label  = f"  {mtime.strftime('%Y-%m-%d %H:%M')}  {sz_str:>8}  {entry.name}"
                lb.insert(tk.END, label)
                recordings.append(entry.path)
            if prev_sel is not None and prev_sel < lb.size():
                lb.selection_set(prev_sel)
                lb.see(prev_sel)
            # Update status
            if self._dvr_process and self._dvr_process.poll() is None:
                ch_name = self._dvr_channel.name if self._dvr_channel else "?"
                elapsed = ""
                if self._dvr_start_ts:
                    secs = int((datetime.datetime.now() - self._dvr_start_ts).total_seconds())
                    elapsed = f"  [{secs//3600:02d}:{(secs%3600)//60:02d}:{secs%60:02d}]"
                status_var.set(f"● RECORDING  {ch_name}{elapsed}  →  {os.path.basename(self._dvr_out_path or '')}")
                rec_btn_mgr.config(text="■ STOP REC", fg="#ff4444", bg="#2a0000")
            else:
                status_var.set("No active recording.")
                rec_btn_mgr.config(text="● REC NOW", fg="#cc0000", bg="#2a1f10")

        _refresh()

        # Auto-refresh while dialog is open
        _refresh_job = [None]
        def _auto_refresh():
            if not dialog.winfo_exists():
                return
            _refresh()
            _refresh_job[0] = dialog.after(2000, _auto_refresh)
        _refresh_job[0] = dialog.after(2000, _auto_refresh)

        def _cancel_refresh():
            if _refresh_job[0]:
                try: dialog.after_cancel(_refresh_job[0])
                except Exception: pass
        dialog.protocol("WM_DELETE_WINDOW", lambda: [_cancel_refresh(), on_close()])

        btn_kw = dict(bg="#2a1f10", fg=self.AMBER,
                      activebackground=self.AMBER_DIM, activeforeground=self.AMBER,
                      font=self.font_ch, relief=tk.FLAT, padx=8, pady=5, bd=0,
                      cursor="hand2",
                      highlightthickness=1, highlightbackground=self.AMBER_DIM)

        def _play_selected():
            sel = lb.curselection()
            if not sel or sel[0] >= len(recordings):
                return
            path = recordings[sel[0]]
            if not os.path.exists(path):
                return
            on_close()
            self._dvr_play_recording(path)

        def _delete_selected():
            sel = lb.curselection()
            if not sel or sel[0] >= len(recordings):
                return
            path = recordings[sel[0]]
            if not os.path.exists(path):
                _refresh(); return
            confirm = tk_messagebox.askyesno(
                "Delete Recording",
                f"Delete this file?\n\n{os.path.basename(path)}",
                parent=dialog)
            if confirm:
                try:
                    os.remove(path)
                except Exception as e:
                    log.error("DVR delete error: %s", e)
                _refresh()

        def _open_folder():
            folder = self._dvr_recordings_dir
            os.makedirs(folder, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception as e:
                log.error("DVR open folder error: %s", e)

        def _play_all():
            if not recordings:
                return
            on_close()
            self._dvr_play_all(list(recordings))

        tk.Button(btn_row, text="▶ Play",       command=_play_selected,  **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="▶▶ Play All",  command=_play_all,       **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="🗑 Delete",    command=_delete_selected, **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="📂 Open Folder", command=_open_folder,  **btn_kw).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(btn_row, text="↺ Refresh",    command=_refresh,        **btn_kw).pack(side=tk.LEFT)
        tk.Button(btn_row, text="✔ Close",      command=on_close,
                  bg=self.AMBER_DIM, fg=self.AMBER,
                  activebackground=self.AMBER, activeforeground="#000000",
                  font=self.font_small, relief=tk.FLAT, padx=12, pady=6,
                  cursor="hand2").pack(side=tk.RIGHT)

    # ══════════════════════════════════════════
    #  Sleep Timer
    # ══════════════════════════════════════════
    def _show_sleep_dialog(self):
        if self._sleep_timer_id is not None:
            self._cancel_sleep_timer()
            return
        dlg = tk.Toplevel(self)
        self._apply_icon(dlg)
        dlg.title("Sleep Timer")
        dlg.resizable(False, False)
        dlg.configure(bg="#1c1308")
        dlg.grab_set()
        dlg.transient(self)
        tk.Label(dlg, text="Auto-shutdown after:", font=self.font_medium,
                 fg=self.AMBER, bg="#1c1308").pack(padx=20, pady=(16, 8))
        row1 = tk.Frame(dlg, bg="#1c1308")
        row1.pack(padx=20, pady=(0, 4))
        for minutes in (15, 30, 45):
            def _pick(m=minutes):
                dlg.destroy()
                self._start_sleep_timer(m)
            self._make_btn(row1, f"{minutes} min", _pick, side=tk.LEFT, padx=(4, 4))
        row2 = tk.Frame(dlg, bg="#1c1308")
        row2.pack(padx=20, pady=(0, 12))
        for minutes in (60, 90, 120):
            def _pick(m=minutes):
                dlg.destroy()
                self._start_sleep_timer(m)
            self._make_btn(row2, f"{minutes} min", _pick, side=tk.LEFT, padx=(4, 4))
        custom_frame = tk.Frame(dlg, bg="#1c1308")
        custom_frame.pack(padx=20, pady=(0, 12))
        tk.Label(custom_frame, text="Custom:", font=self.font_small,
                 fg=self.AMBER_DIM, bg="#1c1308").pack(side=tk.LEFT, padx=(0, 6))
        custom_var = tk.StringVar()
        custom_entry = tk.Entry(custom_frame, textvariable=custom_var, width=5,
                                bg="#2a1f10", fg=self.AMBER, insertbackground=self.AMBER,
                                font=self.font_small, relief=tk.FLAT, bd=2,
                                highlightthickness=1, highlightbackground=self.AMBER_DIM,
                                justify=tk.CENTER)
        custom_entry.pack(side=tk.LEFT, padx=(0, 4))
        tk.Label(custom_frame, text="min", font=self.font_small,
                 fg=self.AMBER_DIM, bg="#1c1308").pack(side=tk.LEFT, padx=(0, 8))
        def _pick_custom():
            try:
                m = int(custom_var.get())
            except ValueError:
                custom_entry.config(highlightbackground="#aa3333")
                return
            if m < 1:
                custom_entry.config(highlightbackground="#aa3333")
                return
            dlg.destroy()
            self._start_sleep_timer(m)
        self._make_btn(custom_frame, "Set", _pick_custom, side=tk.LEFT, padx=(0, 0))
        custom_entry.bind("<Return>", lambda e: _pick_custom())
        tk.Button(dlg, text="Cancel", command=dlg.destroy,
                  bg="#2a1f10", fg=self.AMBER_DIM, activebackground=self.AMBER_DIM,
                  activeforeground=self.AMBER, font=self.font_small, relief=tk.FLAT,
                  padx=8, pady=3, bd=0, cursor="hand2").pack(pady=(0, 14))
        dlg.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _start_sleep_timer(self, minutes):
        self._sleep_remaining = minutes * 60
        self._sleep_tick()

    def _sleep_tick(self):
        if self._closing:
            return
        if self._sleep_remaining <= 0:
            self._sleep_timer_id = None
            dvr_active = (
                (self._dvr_process and self._dvr_process.poll() is None)
                or any(s.get('status') == 'recording' for s in self._dvr_schedules)
            )
            if dvr_active:
                if not getattr(self, '_muted', False):
                    self._toggle_mute()
                self.iconify()
                self._sleep_btn.config(text="⏾ SLEEP")
            else:
                self._on_close()
            return
        m, s = divmod(self._sleep_remaining, 60)
        self._sleep_btn.config(text=f"⏾ {m}:{s:02d}")
        self._sleep_remaining -= 1
        self._sleep_timer_id = self.after(1000, self._sleep_tick)

    def _cancel_sleep_timer(self):
        if self._sleep_timer_id is not None:
            try: self.after_cancel(self._sleep_timer_id)
            except Exception: pass
            self._sleep_timer_id = None
        self._sleep_remaining = 0
        self._sleep_btn.config(text="⏾ SLEEP")

    # ══════════════════════════════════════════
    #  LAN remote control (phone web remote)
    # ══════════════════════════════════════════
    def _remote_start(self):
        self._remote = None
        self._remote_tick = 0
        if not self._settings.get('remote_enabled', True):
            return
        try:
            import secrets, string
            from remote_control import RemoteServer
            if not self._settings.get('remote_token'):
                alphabet = string.ascii_lowercase + string.digits
                self._settings['remote_token'] = ''.join(secrets.choice(alphabet) for _ in range(8))
                _save_settings_file(self._settings)
            srv = RemoteServer(self._settings.get('remote_port', 8765),
                               self._settings['remote_token'], log=log)
            srv.support_url = self.DONATE_URL        # same link as the PC's "Support This Project"
            if not srv.start():
                return
            self._remote = srv
            self._remote_guide_ts = 0.0
            self._remote_pump()
        except Exception:
            log.exception("[remote] failed to start")

    def _remote_pump(self):
        """Main-thread loop: run queued phone commands, refresh the snapshot."""
        if self._closing or not self._remote:
            return
        try:
            for action, value in self._remote.drain_commands():
                self._remote_dispatch(action, value)
            if self._remote_tick % 10 == 0:          # ~1 s
                self._remote_snapshot(with_channels=(self._remote_tick % 300 == 0))  # ~30 s
            # Build the phone's channel guide only while a phone is looking at it
            if (self._remote.guide_wanted()
                    and _time.monotonic() - self._remote_guide_ts > 30):
                self._remote_guide_ts = _time.monotonic()
                self._remote_build_guide()
            self._remote_tick += 1
        except Exception:
            log.exception("[remote] pump error")
        self.after(100, self._remote_pump)

    def _remote_dispatch(self, action, value):
        ch = self.channels[self.current_channel_idx] if self.channels else None
        if action == "next":          self._next_channel()
        elif action == "prev":        self._prev_channel()
        elif action == "vol_up":      self._volume_up()
        elif action == "vol_down":    self._volume_down()
        elif action == "mute":        self._toggle_mute()
        elif action == "stop":        self._media_stop()
        elif action == "fullscreen":  self._toggle_fullscreen()
        elif action == "favorite":    self._toggle_favorite()
        elif action == "guide":       self._toggle_epg_overlay()
        elif action == "record":      self._dvr_toggle()
        elif action == "prev_track":  self._prev_track()    # no-op on non-local channels
        elif action == "next_track":  self._next_track()
        elif action == "play_pause":
            if ch and ch.is_local: self._local_play_pause()
            else:                  self._media_play_pause()
        elif action == "seek":                # value = position in 0..1000 (permille)
            if self.player and ch and (ch.is_local or getattr(self, '_dvr_playing_path', None)):
                frac = max(0, min(1000, value)) / 1000.0
                self._seek_to_frac(frac)
        elif action == "seek_ms":             # value = absolute position in milliseconds
            if self.player and ch and (ch.is_local or getattr(self, '_dvr_playing_path', None)):
                total = int(getattr(self, '_vlc_len_ms', 0) or 0)
                ms = max(0, value)
                if total > 0:
                    ms = min(ms, total)
                    self.playhead_var.set(ms / total)
                self._seek_hold_until = _time.monotonic() + 1.5
                self._vlc_cmd('seek_ms', ms)
        elif action == "set_volume":
            vol = max(0, min(100, value))
            self.vol_var.set(vol); self._set_volume(vol); self._show_volume_osd(vol)
        elif action == "tune":
            for i, c in enumerate(self.channels):
                if c.number == value:
                    self._tune_channel(i); break

    def _remote_snapshot(self, with_channels=False):
        status = {"ready": bool(self.channels)}
        if self.channels:
            ch = self.channels[self.current_channel_idx]
            cur, nxt = _get_epg_now_next(self._epg_data, ch.channel_id)
            state = getattr(self, '_vlc_state', None)
            if not VLC_AVAILABLE or state is None:           label = "STOPPED"
            elif state == vlc.State.Playing:                 label = "PLAYING"
            elif state == vlc.State.Paused:                  label = "PAUSED"
            elif state in (vlc.State.Opening, vlc.State.Buffering): label = "LOADING"
            else:                                            label = "STOPPED"
            status.update({
                "channel_number": ch.number,
                "channel_name":   ch.name,
                "now":            _format_prog_title(cur) or ch.description,
                "next":           _format_prog_title(nxt),
                "progress":       _get_progress_fraction(cur) or 0,
                "state":          label,
                "volume":         int(self.vol_var.get()),
                "muted":          bool(getattr(self, '_muted', False)),
                "fullscreen":     bool(self.is_fullscreen),
                "favorite":       ch.name in self._favorites,
                "recording":      bool(self._dvr_process and self._dvr_process.poll() is None),
                "dvr":            bool(getattr(self, '_dvr_playing_path', None)),
            })
            is_dvr = status["dvr"]
            status["local"] = bool(ch.is_local and not is_dvr and ch.number != 0)
            if status["local"]:
                order = self._local_track_order
                pos = self._local_track_idx
                name = ""
                if order and 0 <= pos < len(order) and order[pos] < len(ch.playlist):
                    name = os.path.splitext(os.path.basename(str(ch.playlist[order[pos]])))[0]
                status.update({"track": name, "track_pos": pos + 1,
                               "track_total": len(order) or len(ch.playlist)})
                # Show what's playing under the channel name, like live channels do
                if self._in_commercial:
                    status["now"], status["next"] = "Commercial Break", ""
                elif name:
                    status["now"], status["next"] = name, ""
            elif is_dvr:
                status["now"] = os.path.splitext(os.path.basename(self._dvr_playing_path))[0]
                status["next"] = ""
            # Playhead (local files and DVR recordings are seekable)
            total = int(getattr(self, '_vlc_len_ms', 0) or 0)
            t_ms  = int(getattr(self, '_vlc_time_ms', 0) or 0)
            seekable = bool((status["local"] or is_dvr) and total > 0)
            status["seekable"]  = seekable
            status["length_ms"] = total if seekable else 0
            status["time_ms"]   = max(0, min(t_ms, total)) if seekable else 0
            status["position"]  = (status["time_ms"] / total) if seekable else 0
            # Info panel (what the PC's Info/Guide overlay shows) + whether that overlay is open
            status["info_open"] = bool(self._epg_overlay_active
                                       and not getattr(self, '_epg_overlay_preview', False))
            try:
                status["info"] = self._remote_info(ch, status)
            except Exception:
                log.exception("[remote] info error")
                status["info"] = None
        channels = None
        if with_channels or not self._remote._channels:
            channels = []
            for c in self.channels:
                p, _n = _get_epg_now_next(self._epg_data, c.channel_id)
                now_t = _format_prog_title(p)
                if not now_t and c.is_local and c.playlist and c.number != 0:
                    ln, _ = self._remote_local_now_next(c)
                    now_t = "" if ln == "-" else ln
                channels.append({
                    "number": c.number, "name": c.name,
                    "description": c.description or "",
                    "now": now_t,
                    "favorite": c.name in self._favorites,
                })
        self._remote.set_snapshot(status, channels)

    def _remote_local_now_next(self, ch):
        """(now, next) file names for a local channel, untruncated."""
        def _name(i):
            return os.path.splitext(os.path.basename(str(ch.playlist[i])))[0]
        try:
            now_idx, next_idx = self._local_now_next_actual_idx(ch)
            now_t = _name(now_idx) if 0 <= now_idx < len(ch.playlist) else ""
            next_t = _name(next_idx) if (0 <= next_idx < len(ch.playlist) and next_idx != now_idx) else ""
            return now_t or "-", next_t
        except Exception:
            return "-", ""

    def _remote_build_guide(self):
        """Schedule grid for the phone: same rows/cells as channel 0, as data.
        Times are seconds from the window start (the phone only does layout;
        all clock text is formatted here so time zones can't disagree)."""
        SLOT = self._EPG_SLOT_MINS * 60
        SLOTS = 8                                   # 4 hours; phones scroll sideways
        total = SLOT * SLOTS
        win_start = self._epg_window_start()
        win_end = win_start + datetime.timedelta(seconds=total)
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ticks = []
        for i in range(SLOTS):
            st = win_start + datetime.timedelta(seconds=i * SLOT)
            try:
                ts = st.replace(tzinfo=datetime.timezone.utc).timestamp()
                loc = datetime.datetime(*_time.localtime(int(ts))[:6])
            except Exception:
                loc = st
            ticks.append(loc.strftime('%I:%M %p').lstrip('0'))
        rows = []
        for ch in self.channels:
            if ch.number == 0:
                continue
            cells = []
            if ch.is_local:
                now_t, next_t = self._remote_local_now_next(ch)
                half = total // 2
                cells.append({"s": 0, "e": half if next_t else total, "t": now_t, "now": 1,
                              "l": "Now playing" + (" (shuffle)" if ch.randomize else "")})
                if next_t:
                    cells.append({"s": half, "e": total, "t": next_t, "l": "Up next"})
            elif not self._epg_loaded:
                cells.append({"s": 0, "e": total, "t": "Loading schedule...", "l": ""})
            else:
                progs = self._epg_data.get(ch.channel_id) if ch.channel_id else None
                for p in (progs or []):
                    if p['start'] >= win_end:
                        break                                   # sorted by start
                    p_stop = p.get('stop') or (p['start'] + datetime.timedelta(hours=1))
                    if p_stop <= win_start:
                        continue
                    s_off = max(0, (p['start'] - win_start).total_seconds())
                    e_off = min(total, (p_stop - win_start).total_seconds())
                    if e_off - s_off < 60:
                        continue
                    is_now = p['start'] <= now_utc < p_stop
                    cell = {"s": int(s_off), "e": int(e_off),
                            "t": _format_prog_title(p) or ch.name,
                            "l": _format_time_range(p)}
                    if is_now:
                        cell["now"] = 1
                        frac = _get_progress_fraction(p)
                        if frac:
                            cell["p"] = round(frac, 2)
                    if p.get('category'):
                        cell["c"] = p['category']
                    if p.get('desc'):
                        cell["d"] = p['desc'][:200]
                    if ch.channel_id and self._dvr_is_scheduled(ch.channel_id, p['start'].isoformat()):
                        cell["rec"] = 1
                    cells.append(cell)
                if not cells:
                    cells.append({"s": 0, "e": total, "t": ch.description or "Live broadcast", "l": "Live"})
            rows.append({"n": ch.number, "name": ch.name,
                         "fav": ch.name in self._favorites, "cells": cells})
        self._remote.set_guide({
            "ready": True, "slot": SLOT, "slots": SLOTS, "ticks": ticks,
            "now": int(max(0, (now_utc - win_start).total_seconds())), "rows": rows})

    def _remote_info(self, ch, status):
        """Data behind the phone's Info panel - the same things the PC's Info
        overlay shows (live: upcoming programmes; local: now playing / up next),
        with full, untruncated text.  Called on the Tk thread."""
        if ch.number == 0 or getattr(self, '_dvr_playing_path', None):
            return None
        items = []
        if ch.is_local:
            if self._in_commercial:
                now_title = "Commercial break"
            else:
                now_title = status.get("track") or "-"
            meta = ""
            if status.get("track_pos"):
                meta = f"Track {status['track_pos']} of {status['track_total']}"
                if ch.randomize:
                    meta += " (shuffle)"
            frac = None if self._in_commercial else status.get("position")
            items.append({"label": "Now playing", "title": now_title, "now": True,
                          "progress": round(frac, 2) if frac else None, "meta": meta})
            try:
                _n, next_idx = self._local_now_next_actual_idx(ch)
                if 0 <= next_idx < len(ch.playlist):
                    nxt = os.path.splitext(os.path.basename(str(ch.playlist[next_idx])))[0]
                    items.append({"label": "Up next", "title": nxt})
            except Exception:
                pass
            return {"heading": "Playlist", "items": items}
        # Live channel
        note = ""
        progs = self._epg_data.get(ch.channel_id) if (ch.channel_id and self._epg_loaded) else None
        if progs:
            now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            horizon = now_utc + datetime.timedelta(hours=4)
            for p in progs:
                p_stop = p.get('stop') or (p['start'] + datetime.timedelta(hours=1))
                if p_stop <= now_utc or p['start'] >= horizon:
                    continue
                is_now = p['start'] <= now_utc < p_stop
                frac = _get_progress_fraction(p) if is_now else None
                items.append({
                    "label": "On now" if is_now else "",
                    "title": _format_prog_title(p) or ch.name,
                    "time":  _format_time_range(p),
                    "now":   is_now,
                    "progress": round(frac, 2) if frac else None,
                    "meta":  p.get('category', '') or "",
                    "desc":  (p.get('desc', '') or "")[:400],
                })
                if len(items) >= 8:
                    break
        if not items:
            items.append({"label": "On now", "title": ch.description or "Live broadcast", "now": True})
            note = ("Schedule data is still loading." if not self._epg_loaded
                    else "No schedule is available for this channel.")
        return {"heading": "Schedule", "items": items, "note": note}

    def _remote_show_dialog(self):
        """Show the phone-remote address and pairing code (Ctrl+M)."""
        if not self._remote:
            tk_messagebox.showinfo("Phone Remote",
                "The phone remote is not running.\n\nSet \"remote_enabled\": true in "
                "retrotv_settings.json and restart, or check the log if the port is in use.",
                parent=self)
            return
        existing = getattr(self, '_remote_dlg', None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift(); existing.focus_force(); return
            except Exception:
                pass
        base = self._remote.url(with_token=False)
        full = self._remote.url(with_token=True)
        BG, FG, DIM = '#1a1008', '#ffb000', '#c07800'
        dlg = tk.Toplevel(self)
        self._remote_dlg = dlg
        dlg.title("Phone Remote")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        mono = ("Courier New", 10)
        tk.Label(dlg, text="SCAN WITH YOUR PHONE CAMERA", bg=BG, fg=FG,
                 font=("Courier New", 11, "bold")).pack(padx=20, pady=(16, 6))
        # ── QR code (matrix drawn on a canvas; no Pillow needed) ──
        try:
            import qrcode
            qr = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(full); qr.make(fit=True)
            matrix = qr.get_matrix()
            quiet = 3                                   # quiet-zone modules
            n = len(matrix) + quiet * 2
            cell = max(4, 240 // n)
            size = n * cell
            cv = tk.Canvas(dlg, width=size, height=size, bg="#ffffff", highlightthickness=0)
            for y, row in enumerate(matrix):
                for x, on in enumerate(row):
                    if on:
                        px, py = (x + quiet) * cell, (y + quiet) * cell
                        cv.create_rectangle(px, py, px + cell, py + cell, fill="#000000", outline="")
            cv.pack(pady=4)
        except Exception:
            log.warning("[remote] qrcode unavailable - run: pip install qrcode")
            tk.Label(dlg, text="(QR unavailable - run: pip install qrcode)",
                     bg=BG, fg=DIM, font=mono).pack(pady=8)
        tk.Label(dlg, text="Or open this address in the phone's browser:",
                 bg=BG, fg=DIM, font=mono).pack(pady=(10, 0))
        tk.Label(dlg, text=base, bg=BG, fg=FG, font=("Courier New", 12, "bold")).pack()
        tk.Label(dlg, text=f"Pairing code:  {self._remote.token}", bg=BG, fg=FG,
                 font=("Courier New", 12, "bold")).pack(pady=(6, 0))
        tk.Label(dlg, text="Phone and PC must be on the same Wi-Fi.\n"
                 "If Windows asks, allow RetroTV through the firewall (Private networks).",
                 bg=BG, fg=DIM, font=("Courier New", 9), justify="center").pack(padx=20, pady=(10, 4))
        btns = tk.Frame(dlg, bg=BG); btns.pack(pady=(6, 16))
        def _copy():
            try:
                self.clipboard_clear(); self.clipboard_append(full)
                copy_btn.config(text="COPIED")
            except Exception:
                pass
        copy_btn = tk.Button(btns, text="COPY LINK", command=_copy, bg="#2a1c0a", fg=FG,
                             activebackground=FG, activeforeground="#000000", relief="flat",
                             font=mono, padx=10, pady=4)
        copy_btn.pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="CLOSE", command=dlg.destroy, bg="#2a1c0a", fg=FG,
                  activebackground=FG, activeforeground="#000000", relief="flat",
                  font=mono, padx=10, pady=4).pack(side=tk.LEFT, padx=4)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.focus_force()

    # ══════════════════════════════════════════
    #  Cleanup
    # ══════════════════════════════════════════
    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        if self.is_fullscreen:
            self.is_fullscreen = False
            try:
                self.overrideredirect(False)
            except Exception:
                pass
        _suppress_screensaver(False)
        if getattr(self, '_remote', None):
            self._remote.stop()
        self._fs_stop_mouse_poll()
        for attr in ('_scanline_after_id', '_watermark_resize_after', '_channel_display_after',
                     '_tuning_clear_after', '_epg_scroll_anim_id', '_epg_overlay_after_id',
                     '_fs_overlay_after_id', '_sleep_timer_id'):
            val = getattr(self, attr, None)
            if val:
                try: self.after_cancel(val)
                except Exception: pass
                setattr(self, attr, None)
        if getattr(self, '_win32_overlay', None):
            try: self._win32_user32.DestroyWindow(self._win32_overlay)
            except Exception: pass
            self._win32_overlay = None
        self._hide_guide()
        if sys.platform == 'win32' and hasattr(self, '_registered_hotkeys'):
            try:
                hwnd = self.winfo_id()
                for hk_id in self._registered_hotkeys:
                    ctypes.windll.user32.UnregisterHotKey(hwnd, hk_id)
            except Exception: pass
        if self.channels:
            ch = self.channels[self.current_channel_idx]
            if ch.is_local: self._save_local_position(ch)
        try:
            self._resume_state['__window_pos__'] = {
                'x': self.winfo_x(),
                'y': self.winfo_y(),
            }
        except Exception:
            pass
        if self.channels:
            self._resume_state['__last_channel__'] = self.channels[self.current_channel_idx].name
            _save_resume_state(self._resume_state)
        for sched_id, timer_pair in list(getattr(self, '_dvr_timers', {}).items()):
            for t in timer_pair:
                if t:
                    try: t.cancel()
                    except Exception: pass
        if getattr(self, '_dvr_blink_id', None):
            try: self.after_cancel(self._dvr_blink_id)
            except Exception: pass
        # Uninstall the VLC Win32 hook now, on the main thread, before handing off.
        self._uninstall_vlc_dblclick_hook()
        # Hide the window immediately so it appears closed to the user.
        try: self.withdraw()
        except Exception: pass
        # All remaining cleanup (DVR process wait, VLC thread join, player.release)
        # can block for several seconds and will deadlock if run on the main thread
        # because VLC's Win32 HWND pipeline needs the main-thread message pump to
        # keep running during stop/release.  Offload to a daemon thread and post
        # destroy() back to the main thread when done.
        threading.Thread(target=self._blocking_cleanup, daemon=True,
                         name="retrotv-shutdown").start()

    def _blocking_cleanup(self):
        if getattr(self, '_dvr_process', None) and self._dvr_process.poll() is None:
            try:
                self._dvr_process.terminate()
                self._dvr_process.wait(timeout=5)
            except Exception:
                try: self._dvr_process.kill()
                except Exception: pass
        if hasattr(self, '_vlc_queue'):
            self._vlc_queue.put(None)
            self._vlc_thread.join(timeout=3.0)
            if self._vlc_thread.is_alive():
                _log_vlc.warning("VLC worker thread did not stop within 3s")
        self.after(0, self._final_cleanup)
        _time.sleep(2)
        os._exit(0)

    def _final_cleanup(self):
        if self.player:
            try: self.player.stop(); self.player.release()
            except Exception: pass
        if self.vlc_instance:
            try: self.vlc_instance.release()
            except Exception: pass
        try:
            self.destroy()
        except Exception:
            pass

if __name__ == "__main__":
    config = os.path.join(_exe_dir(), "tv_config.json")
    if len(sys.argv) > 1: config = sys.argv[1]
    if not VLC_AVAILABLE:
        print("\n" + "=" * 60)
        print("ERROR: python-vlc is not installed.")
        print("Run:  pip install python-vlc")
        print("=" * 60 + "\n")
    app = RetroTV(config_path=config)
    app.mainloop()