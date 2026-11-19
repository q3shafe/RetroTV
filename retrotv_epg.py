"""
RetroTV EPG (Electronic Program Guide) Integration
====================================================
Parses XMLTV-format EPG data files and integrates program schedules
into the RetroTV channel guide.

XMLTV Format Support:
  Place an XMLTV file (e.g.  guide.xml  or  epg.xml) in the same
  directory as retrotv.py.  The path can also be set in user_config.json:
      { "epg_file": "path/to/guide.xml" }

Channel Matching:
  XMLTV channels are matched to RetroTV channels using a flexible
  strategy:
    1. Exact name match (case-insensitive)
    2. Partial / substring match
    3. Manual mapping via  epg_map  in user_config.json:
         "epg_map": {
           "My Channel Name": "xmltv.channel.id",
           "CNN HD":          "cnn.us"
         }

Usage (standalone):
  epg = EPGManager()
  epg.load("guide.xml")
  program = epg.get_current_program("CNN")
  programs = epg.get_schedule("CNN", hours=4)
"""

import xml.etree.ElementTree as ET
import datetime
import threading
import os
import re
from typing import Optional


# ─────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────

class EPGProgram:
    """Represents a single programme entry from XMLTV data."""

    __slots__ = (
        "channel_id", "title", "subtitle", "description",
        "start", "stop", "category", "episode", "rating",
        "icon_url",
    )

    def __init__(self, channel_id: str, title: str, start: datetime.datetime,
                 stop: datetime.datetime, subtitle: str = "",
                 description: str = "", category: str = "",
                 episode: str = "", rating: str = "", icon_url: str = ""):
        self.channel_id  = channel_id
        self.title       = title
        self.subtitle    = subtitle
        self.description = description
        self.start       = start
        self.stop        = stop
        self.category    = category
        self.episode     = episode
        self.rating      = rating
        self.icon_url    = icon_url

    # ── Computed helpers ──────────────────────────────────────────────────

    @property
    def duration_minutes(self) -> int:
        delta = self.stop - self.start
        return max(0, int(delta.total_seconds() / 60))

    def progress_pct(self, now: Optional[datetime.datetime] = None) -> float:
        """Return 0.0–1.0 representing how far through the programme we are."""
        now = now or datetime.datetime.now(tz=self.start.tzinfo)
        total = (self.stop - self.start).total_seconds()
        if total <= 0:
            return 0.0
        elapsed = (now - self.start).total_seconds()
        return max(0.0, min(1.0, elapsed / total))

    def time_remaining(self, now: Optional[datetime.datetime] = None) -> int:
        """Return whole minutes remaining in the programme."""
        now = now or datetime.datetime.now(tz=self.start.tzinfo)
        remaining = (self.stop - now).total_seconds()
        return max(0, int(remaining / 60))

    def format_time_range(self) -> str:
        """Return  HH:MM – HH:MM  string."""
        return (f"{self.start.strftime('%I:%M %p').lstrip('0')} – "
                f"{self.stop.strftime('%I:%M %p').lstrip('0')}")

    def __repr__(self) -> str:
        return (f"<EPGProgram channel={self.channel_id!r} "
                f"title={self.title!r} "
                f"start={self.start.strftime('%H:%M')}>")


class EPGChannel:
    """Represents an XMLTV <channel> element."""

    __slots__ = ("id", "display_names", "icon_url")

    def __init__(self, channel_id: str, display_names: list, icon_url: str = ""):
        self.id            = channel_id
        self.display_names = display_names   # list of strings, primary first
        self.icon_url      = icon_url

    @property
    def name(self) -> str:
        return self.display_names[0] if self.display_names else self.id

    def __repr__(self) -> str:
        return f"<EPGChannel id={self.id!r} name={self.name!r}>"


# ─────────────────────────────────────────────
#  XMLTV timestamp parser
# ─────────────────────────────────────────────

def _parse_xmltv_time(raw: str) -> Optional[datetime.datetime]:
    """
    Parse XMLTV timestamps.  Common formats:
      20240101123000 +0000
      20240101123000 +0530
      20240101123000
    Returns a timezone-aware datetime (UTC if no offset), or None on failure.
    """
    raw = raw.strip()
    # Split off optional timezone offset: +HHMM or -HHMM
    tz_match = re.search(r'([+-]\d{4})$', raw)
    tz_offset = None
    if tz_match:
        tz_str  = tz_match.group(1)
        raw     = raw[:tz_match.start()].strip()
        sign    = 1 if tz_str[0] == '+' else -1
        hours   = int(tz_str[1:3])
        minutes = int(tz_str[3:5])
        tz_offset = datetime.timezone(
            datetime.timedelta(hours=sign * hours, minutes=sign * minutes))
    else:
        tz_offset = datetime.timezone.utc

    # Strip to exactly 14 digits (YYYYMMDDHHmmss)
    digits = re.sub(r'\D', '', raw)[:14].ljust(14, '0')
    try:
        naive = datetime.datetime.strptime(digits, '%Y%m%d%H%M%S')
        return naive.replace(tzinfo=tz_offset)
    except ValueError:
        return None


def _to_local(dt: datetime.datetime) -> datetime.datetime:
    """Convert any timezone-aware datetime to local wall-clock time."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(tz=None).replace(tzinfo=None)


# ─────────────────────────────────────────────
#  Core EPG manager
# ─────────────────────────────────────────────

class EPGManager:
    """
    Loads and queries XMLTV EPG data.

    Thread safety: load() acquires an internal lock; all read methods
    are safe to call from any thread after load() completes.
    """

    def __init__(self):
        self._lock        = threading.RLock()
        self._channels    : dict[str, EPGChannel]       = {}   # id → EPGChannel
        self._programs    : dict[str, list[EPGProgram]] = {}   # id → sorted list
        self._loaded      = False
        self._load_error  : Optional[str] = None
        self._source_path : Optional[str] = None

        # Channel mapping: RetroTV channel name → XMLTV channel id
        # Populated by set_manual_map() or auto-detected via fuzzy matching.
        self._manual_map  : dict[str, str] = {}

        # Cache: retrotv_name → xmltv_channel_id (or None if no match found)
        self._match_cache : dict[str, Optional[str]] = {}

    # ── Loading ───────────────────────────────────────────────────────────

    def load(self, path: str) -> bool:
        """
        Parse an XMLTV file.  Returns True on success.
        Thread-safe: can be called from a background thread.
        """
        with self._lock:
            self._channels   = {}
            self._programs   = {}
            self._match_cache = {}
            self._loaded     = False
            self._load_error = None

            if not path or not os.path.exists(path):
                self._load_error = f"File not found: {path}"
                print(f"[EPG] {self._load_error}")
                return False

            self._source_path = path
            try:
                tree = ET.parse(path)
                root = tree.getroot()
            except ET.ParseError as exc:
                self._load_error = f"XML parse error: {exc}"
                print(f"[EPG] {self._load_error}")
                return False
            except Exception as exc:
                self._load_error = str(exc)
                print(f"[EPG] unexpected error: {exc}")
                return False

            # ── Parse <channel> elements ──────────────────────────────────
            for ch_el in root.findall('channel'):
                ch_id = ch_el.get('id', '').strip()
                if not ch_id:
                    continue
                names = [
                    dn.text.strip()
                    for dn in ch_el.findall('display-name')
                    if dn.text and dn.text.strip()
                ]
                icon_el  = ch_el.find('icon')
                icon_url = icon_el.get('src', '') if icon_el is not None else ''
                self._channels[ch_id] = EPGChannel(ch_id, names, icon_url)
                self._programs[ch_id] = []

            # ── Parse <programme> elements ────────────────────────────────
            parsed = skipped = 0
            for prog_el in root.findall('programme'):
                ch_id = prog_el.get('channel', '').strip()
                start_raw = prog_el.get('start', '').strip()
                stop_raw  = prog_el.get('stop',  '').strip()

                start_dt = _parse_xmltv_time(start_raw) if start_raw else None
                stop_dt  = _parse_xmltv_time(stop_raw)  if stop_raw  else None

                if not start_dt or not stop_dt:
                    skipped += 1
                    continue

                # Convert to local time for display
                start_local = _to_local(start_dt)
                stop_local  = _to_local(stop_dt)

                title_el = prog_el.find('title')
                title    = title_el.text.strip() if (title_el is not None and title_el.text) else "Unknown"

                sub_el   = prog_el.find('sub-title')
                subtitle = sub_el.text.strip() if (sub_el is not None and sub_el.text) else ""

                desc_el  = prog_el.find('desc')
                desc     = desc_el.text.strip() if (desc_el is not None and desc_el.text) else ""

                cat_el   = prog_el.find('category')
                category = cat_el.text.strip() if (cat_el is not None and cat_el.text) else ""

                # Episode number (XMLTV or onscreen)
                episode = ""
                for ep_el in prog_el.findall('episode-num'):
                    ep_sys = ep_el.get('system', '')
                    ep_txt = ep_el.text.strip() if ep_el.text else ''
                    if ep_txt:
                        if ep_sys == 'onscreen':
                            episode = ep_txt
                            break
                        elif ep_sys == 'xmltv_ns' and not episode:
                            # Convert  S.E.  notation (0-based) to S01E01
                            parts = ep_txt.split('.')
                            if len(parts) >= 2:
                                try:
                                    s = int(parts[0].split('/')[0]) + 1
                                    e = int(parts[1].split('/')[0]) + 1
                                    episode = f"S{s:02d}E{e:02d}"
                                except (ValueError, IndexError):
                                    episode = ep_txt

                rating = ""
                rating_el = prog_el.find('rating')
                if rating_el is not None:
                    val_el = rating_el.find('value')
                    if val_el is not None and val_el.text:
                        rating = val_el.text.strip()

                icon_el  = prog_el.find('icon')
                icon_url = icon_el.get('src', '') if icon_el is not None else ''

                # Auto-create channel entry if referenced but not declared
                if ch_id not in self._programs:
                    self._programs[ch_id] = []
                    if ch_id not in self._channels:
                        self._channels[ch_id] = EPGChannel(ch_id, [ch_id])

                self._programs[ch_id].append(EPGProgram(
                    channel_id=ch_id,
                    title=title,
                    subtitle=subtitle,
                    description=desc,
                    start=start_local,
                    stop=stop_local,
                    category=category,
                    episode=episode,
                    rating=rating,
                    icon_url=icon_url,
                ))
                parsed += 1

            # Sort each channel's programme list by start time
            for ch_id in self._programs:
                self._programs[ch_id].sort(key=lambda p: p.start)

            total_ch = len(self._channels)
            total_pr = sum(len(v) for v in self._programs.values())
            print(
                f"[EPG] Loaded {os.path.basename(path)}: "
                f"{total_ch} channel(s), {total_pr} programme(s) "
                f"({skipped} entries skipped)"
            )
            self._loaded = True
            return True

    def load_async(self, path: str, callback=None):
        """
        Load the EPG file in a background thread.
        callback(success: bool) is called on the main thread via after()
        if a tk widget is provided — otherwise it's called directly.
        """
        def _worker():
            ok = self.load(path)
            if callback:
                try:
                    callback(ok)
                except Exception as exc:
                    print(f"[EPG] callback error: {exc}")

        t = threading.Thread(target=_worker, daemon=True, name="epg-load")
        t.start()

    # ── Manual / auto channel mapping ─────────────────────────────────────

    def set_manual_map(self, mapping: dict):
        """
        Provide a dict of  { "RetroTV channel name": "xmltv.channel.id" }.
        Names are matched case-insensitively.
        """
        with self._lock:
            self._manual_map = {k.lower().strip(): v for k, v in mapping.items()}
            self._match_cache = {}   # Invalidate cache

    def _find_xmltv_id(self, retrotv_name: str) -> Optional[str]:
        """
        Return the XMLTV channel ID that best matches retrotv_name.
        Priority:
          1. Manual map (exact, case-insensitive)
          2. Exact display-name match
          3. Display-name contains retrotv_name  (or vice-versa)
          4. Fuzzy: token overlap score ≥ 0.5
        Returns None if no suitable match found.
        """
        key = retrotv_name.lower().strip()

        with self._lock:
            # Cache hit
            if key in self._match_cache:
                return self._match_cache[key]

            # 1. Manual map
            if key in self._manual_map:
                xmltv_id = self._manual_map[key]
                self._match_cache[key] = xmltv_id
                print(f"[EPG] manual map: '{retrotv_name}' → '{xmltv_id}'")
                return xmltv_id

            best_id    = None
            best_score = 0.0

            for ch_id, epg_ch in self._channels.items():
                for dname in epg_ch.display_names:
                    dname_l = dname.lower().strip()

                    # 2. Exact
                    if dname_l == key:
                        self._match_cache[key] = ch_id
                        print(f"[EPG] exact match: '{retrotv_name}' → '{ch_id}'")
                        return ch_id

                    # 3. Substring
                    if key in dname_l or dname_l in key:
                        score = len(min(key, dname_l, key=len)) / len(max(key, dname_l, key=len))
                        if score > best_score:
                            best_score = score
                            best_id    = ch_id

                    # 4. Token overlap
                    else:
                        t1 = set(re.split(r'\W+', key))
                        t2 = set(re.split(r'\W+', dname_l))
                        t1.discard(''); t2.discard('')
                        if t1 and t2:
                            overlap = len(t1 & t2) / len(t1 | t2)
                            if overlap > best_score:
                                best_score = overlap
                                best_id    = ch_id

            # Accept if confidence ≥ 0.5
            if best_score >= 0.5 and best_id:
                self._match_cache[key] = best_id
                print(f"[EPG] fuzzy match ({best_score:.2f}): '{retrotv_name}' → '{best_id}'")
                return best_id

            # No match
            print(f"[EPG] no match found for '{retrotv_name}' (best score={best_score:.2f})")
            self._match_cache[key] = None
            return None

    # ── Programme queries ─────────────────────────────────────────────────

    def get_current_program(self, retrotv_name: str,
                            now: Optional[datetime.datetime] = None
                            ) -> Optional[EPGProgram]:
        """Return the programme currently airing on the named channel, or None."""
        now = now or datetime.datetime.now()
        ch_id = self._find_xmltv_id(retrotv_name)
        if not ch_id:
            return None
        with self._lock:
            programs = self._programs.get(ch_id, [])
        for prog in programs:
            if prog.start <= now < prog.stop:
                return prog
        return None

    def get_next_program(self, retrotv_name: str,
                         now: Optional[datetime.datetime] = None
                         ) -> Optional[EPGProgram]:
        """Return the next programme after the current one."""
        now = now or datetime.datetime.now()
        ch_id = self._find_xmltv_id(retrotv_name)
        if not ch_id:
            return None
        with self._lock:
            programs = self._programs.get(ch_id, [])
        current = None
        for prog in programs:
            if prog.start <= now < prog.stop:
                current = prog
                break
        if current:
            for prog in programs:
                if prog.start >= current.stop:
                    return prog
        else:
            for prog in programs:
                if prog.start > now:
                    return prog
        return None

    def get_schedule(self, retrotv_name: str, hours: int = 4,
                     now: Optional[datetime.datetime] = None
                     ) -> list:
        """
        Return a list of EPGProgram objects covering the next `hours`
        hours for the named channel.
        """
        now    = now or datetime.datetime.now()
        cutoff = now + datetime.timedelta(hours=hours)
        ch_id  = self._find_xmltv_id(retrotv_name)
        if not ch_id:
            return []
        with self._lock:
            programs = self._programs.get(ch_id, [])
        result = []
        for prog in programs:
            if prog.stop > now and prog.start < cutoff:
                result.append(prog)
        return result

    def get_all_current(self, retrotv_names: list,
                        now: Optional[datetime.datetime] = None
                        ) -> dict:
        """
        Batch query: return  { retrotv_name: EPGProgram or None }
        for every channel in retrotv_names.  Efficient for guide redraws.
        """
        now = now or datetime.datetime.now()
        return {name: self.get_current_program(name, now) for name in retrotv_names}

    # ── Utility ───────────────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def load_error(self) -> Optional[str]:
        return self._load_error

    @property
    def channel_count(self) -> int:
        with self._lock:
            return len(self._channels)

    @property
    def program_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._programs.values())

    def has_data_for(self, retrotv_name: str) -> bool:
        """Return True if this channel has any EPG data."""
        ch_id = self._find_xmltv_id(retrotv_name)
        if not ch_id:
            return False
        with self._lock:
            return bool(self._programs.get(ch_id))

    def xmltv_channels(self) -> list:
        """Return list of all EPGChannel objects (for diagnostics/mapping UI)."""
        with self._lock:
            return list(self._channels.values())