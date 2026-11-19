#!/usr/bin/env python3
"""
dvr_scheduler.py — standalone background DVR scheduler for shafe-tv.

Polls dvr_schedule.json every POLL_INTERVAL seconds and launches ffmpeg
for any pending recordings whose start time has arrived, without needing
the GUI app to be open.

Usage:
    python  dvr_scheduler.py        # runs until Ctrl-C / SIGTERM
    pythonw dvr_scheduler.py        # no console window (Windows)
    python  dvr_scheduler.py --once # single poll then exit (for testing)

To run automatically on Windows login, create a Task Scheduler task:
    Action:   Start a program
    Program:  pythonw.exe
    Arguments: "C:\\path\\to\\dvr_scheduler.py"
    Trigger:  At log on

NOTE: Do not run this alongside the main tv.py GUI at the same time —
both processes share dvr_schedule.json and could attempt to start the
same recording simultaneously.
"""

import datetime
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL   = 30   # seconds between schedule checks
START_TOLERANCE = 45   # start recording up to this many seconds before start_utc
                       # must be > POLL_INTERVAL to avoid missing a scheduled show

SCHEDULE_FILE   = "dvr_schedule.json"
RECORDINGS_DIR  = "recordings"
PID_FILE        = "dvr_scheduler.pid"
LOG_FILE        = "dvr_scheduler.log"

# ── Globals ────────────────────────────────────────────────────────────────────

# sched_id → {'proc': Popen, 'stop_utc': datetime, 'out_path': str, 'title': str}
_active:  dict = {}
_running: bool = True
log = logging.getLogger("dvr_scheduler")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_dir() -> str:
    """Directory that contains this script (or the frozen exe)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _find_ffmpeg(base_dir: str) -> str | None:
    """Return path to ffmpeg, mirroring the lookup order in tv.py."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        os.path.join(base_dir, "ffmpeg.exe"),
        os.path.join(base_dir, "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _parse_utc(iso_str: str) -> datetime.datetime:
    """Parse an ISO datetime string and return a timezone-aware UTC datetime.
    Naive strings are assumed to be UTC."""
    dt = datetime.datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _load_schedules(path: str) -> list:
    """Read dvr_schedule.json, retrying briefly on transient write contention."""
    for attempt in range(3):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f).get('schedules', [])
        except (json.JSONDecodeError, OSError):
            if attempt < 2:
                time.sleep(0.3)
    log.warning("Could not read %s after 3 attempts", path)
    return []


def _cleanup_stale_tmp(path: str) -> None:
    """Remove a leftover .tmp file if present from a previous crashed write."""
    tmp = path + '.tmp'
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
            log.warning("Removed stale temp file: %s", tmp)
        except OSError as e:
            log.warning("Could not remove stale temp file %s: %s", tmp, e)


def _save_schedules(path: str, schedules: list) -> None:
    """Write schedule list atomically, stripping the non-serialisable _proc key."""
    clean = [{k: v for k, v in s.items() if k != '_proc'} for s in schedules]
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'schedules': clean}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)   # atomic on same filesystem
    except OSError as e:
        log.error("Could not save schedules: %s", e)


# ── Recording lifecycle ───────────────────────────────────────────────────────

def _start_recording(sched: dict, ffmpeg: str, recordings_dir: str,
                     schedule_path: str, schedules: list) -> None:
    """Launch ffmpeg for sched, update status to 'recording', and save."""
    sched_id = sched['id']
    os.makedirs(recordings_dir, exist_ok=True)

    safe_ch    = re.sub(r'[^\w\-]', '_', sched.get('channel_name', 'CH'))
    safe_title = re.sub(r'[^\w\-]', '_', sched.get('title', 'rec'))[:40]
    try:
        start_dt = _parse_utc(sched['start_utc'])
        ts_str   = start_dt.strftime("%Y%m%d_%H%M")
    except Exception:
        ts_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")

    out_path = os.path.join(recordings_dir, f"{safe_ch}_{safe_title}_{ts_str}.ts")

    cmd = [
        ffmpeg, "-y", "-loglevel", "warning",
        "-i", sched['url'],
        "-c", "copy", "-fflags", "+genpts",
        out_path,
    ]
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc  = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        log.error("Failed to start ffmpeg for '%s': %s", sched.get('title'), e)
        sched['status'] = 'failed'
        _save_schedules(schedule_path, schedules)
        return

    sched['status']   = 'recording'
    sched['out_path'] = out_path
    _save_schedules(schedule_path, schedules)
    log.info("Recording started: '%s' → %s (pid %d)",
             sched.get('title'), out_path, proc.pid)

    try:
        stop_utc = _parse_utc(sched['stop_utc'])
    except Exception:
        stop_utc = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        log.warning("No stop_utc for '%s' — defaulting to 1 hour", sched.get('title'))

    _active[sched_id] = {
        'proc':     proc,
        'stop_utc': stop_utc,
        'out_path': out_path,
        'title':    sched.get('title', ''),
    }


def _stop_recording(sched_id: str, schedule_path: str, reason: str = 'done') -> None:
    """Terminate an active ffmpeg process and mark the schedule entry done."""
    entry = _active.pop(sched_id, None)
    if entry is None:
        return

    proc = entry['proc']
    if proc.poll() is None:
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

    # Reload from disk so we don't clobber changes the GUI app may have made.
    schedules = _load_schedules(schedule_path)
    sched = next((s for s in schedules if s.get('id') == sched_id), None)
    if sched:
        sched['status'] = reason
        _save_schedules(schedule_path, schedules)

    out = entry.get('out_path', '')
    if out and os.path.exists(out):
        sz     = os.path.getsize(out)
        sz_str = f"{sz // 1_048_576} MB" if sz >= 1_048_576 else f"{sz // 1024} KB"
        log.info("Recording %s: %s (%s)", reason, out, sz_str)
    else:
        log.info("Recording %s: '%s'", reason, entry.get('title'))


# ── Signal handling ───────────────────────────────────────────────────────────

def _on_signal(sig, _frame) -> None:
    global _running
    log.info("Signal %d received — shutting down", sig)
    _running = False


# ── Main loop ─────────────────────────────────────────────────────────────────

def _check_pid_file(pid_path: str) -> bool:
    """Return False (and log a warning) if another instance is already running."""
    if not os.path.exists(pid_path):
        return True
    try:
        with open(pid_path) as f:
            old_pid = int(f.read().strip())
    except (ValueError, OSError):
        return True

    # On Windows, check if the PID is still alive.
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            log.warning("Another dvr_scheduler is already running (pid %d) — exiting.", old_pid)
            return False
    else:
        try:
            os.kill(old_pid, 0)
            log.warning("Another dvr_scheduler is already running (pid %d) — exiting.", old_pid)
            return False
        except OSError:
            pass
    return True


def main() -> None:
    global _running

    base_dir       = _base_dir()
    schedule_path  = os.path.join(base_dir, SCHEDULE_FILE)
    recordings_dir = os.path.join(base_dir, RECORDINGS_DIR)
    pid_path       = os.path.join(base_dir, PID_FILE)
    log_path       = os.path.join(base_dir, LOG_FILE)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )

    _cleanup_stale_tmp(schedule_path)

    ffmpeg = _find_ffmpeg(base_dir)
    if not ffmpeg:
        log.error("ffmpeg not found — DVR scheduler cannot record. Exiting.")
        sys.exit(1)

    if not _check_pid_file(pid_path):
        sys.exit(1)

    with open(pid_path, 'w') as f:
        f.write(str(os.getpid()))

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    once = '--once' in sys.argv
    log.info("DVR scheduler started (pid %d, ffmpeg: %s, poll: %ds)",
             os.getpid(), ffmpeg, POLL_INTERVAL)

    try:
        while _running:
            now_utc = datetime.datetime.now(datetime.timezone.utc)

            # ── Stop any recordings that have run past their stop time ──────
            for sched_id in list(_active):
                entry = _active[sched_id]
                proc_done = entry['proc'].poll() is not None
                time_up   = now_utc >= entry['stop_utc']
                if proc_done or time_up:
                    reason = 'done' if not proc_done or time_up else 'done'
                    log.info("Stopping '%s' (%s)", entry['title'],
                             "time up" if time_up else "ffmpeg exited")
                    _stop_recording(sched_id, schedule_path, reason)

            # ── Poll schedule file for pending entries ──────────────────────
            if os.path.exists(schedule_path):
                schedules = _load_schedules(schedule_path)
                missed_any = False

                for sched in schedules:
                    sid    = sched.get('id')
                    status = sched.get('status')

                    if status != 'pending':
                        continue
                    if sid in _active:
                        continue

                    try:
                        start_utc = _parse_utc(sched['start_utc'])
                        stop_utc  = _parse_utc(sched['stop_utc'])
                    except Exception:
                        log.warning("Skipping entry with bad timestamps: %s", sid)
                        continue

                    secs_until_start = (start_utc - now_utc).total_seconds()

                    if now_utc >= stop_utc:
                        # Show already over — we missed it entirely.
                        sched['status'] = 'missed'
                        missed_any = True
                        log.info("Missed: '%s' (ended %s)",
                                 sched.get('title'),
                                 stop_utc.strftime("%H:%M UTC"))

                    elif secs_until_start <= START_TOLERANCE:
                        # Start time is here (or close enough) and show isn't over.
                        if secs_until_start < 0:
                            log.info("Late start (%.0fs) for '%s'",
                                     -secs_until_start, sched.get('title'))
                        _start_recording(sched, ffmpeg, recordings_dir,
                                         schedule_path, schedules)
                        # _start_recording already saved; skip the missed_any save below.
                        continue

                    else:
                        log.debug("Waiting %.0fs for '%s'",
                                  secs_until_start, sched.get('title'))

                if missed_any:
                    _save_schedules(schedule_path, schedules)

            if once:
                log.info("--once flag set, exiting after single poll.")
                break

            # Sleep in short increments so SIGTERM is handled promptly.
            for _ in range(POLL_INTERVAL * 2):
                if not _running:
                    break
                time.sleep(0.5)

    finally:
        if _active:
            log.info("Stopping %d active recording(s) before exit...", len(_active))
            for sched_id in list(_active):
                _stop_recording(sched_id, schedule_path, 'done')
        try:
            os.remove(pid_path)
        except OSError:
            pass
        log.info("DVR scheduler stopped.")


if __name__ == '__main__':
    main()
