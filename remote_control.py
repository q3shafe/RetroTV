"""
remote_control.py - LAN remote control for RetroTV (tv.py).

Runs a tiny HTTP server in a background thread and serves a mobile web
remote.  Thread-safety rule: the server thread NEVER touches Tk or the VLC
player.  Instead:
  * commands are put on a queue.Queue and executed by the Tk main thread
    (RemoteServer.pump(), driven from app.after), and
  * status / channel data is a snapshot built on the Tk main thread
    (RemoteServer.refresh_snapshot()) and merely read by the server thread.

API (all /api/* need the token via 'X-Token' header or '?t=' query):
  GET  /               mobile remote page (no token needed to load it)
  GET  /api/status     current channel, programme, volume, state
  GET  /api/channels   channel list
  GET  /api/guide      schedule grid for all channels (built on demand)
  POST /api/cmd        {"action": "next|prev|vol_up|vol_down|mute|play_pause|
                                   stop|fullscreen|favorite|guide|record|
                                   set_volume|tune|prev_track|next_track|seek|seek_ms",
                        "value": ...}   (seek = 0..1000 permille, seek_ms = milliseconds)
"""
import hmac
import html
import json
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ACTIONS = {"next", "prev", "vol_up", "vol_down", "mute", "play_pause", "stop",
           "fullscreen", "favorite", "guide", "record", "set_volume", "tune",
           "prev_track", "next_track", "seek", "seek_ms"}
MAX_BODY = 4096


def lan_ip() -> str:
    """Best-effort LAN address of this PC (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class RemoteServer:
    def __init__(self, port: int, token: str, log=None):
        self.port = int(port)
        self.token = token
        self.log = log
        self.cmd_queue = queue.Queue(maxsize=64)
        self._lock = threading.Lock()
        self._status = {"ready": False}
        self._channels = []
        self._guide = {"ready": False}
        self.guide_requested_at = 0.0   # monotonic time of the last phone request
        self.support_url = ""          # shown as a 'Support this project' link
        self._httpd = None
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> bool:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # keep console quiet
                pass

            # -- helpers
            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj))

            def _authed(self) -> bool:
                tok = self.headers.get("X-Token")
                if not tok:
                    tok = parse_qs(urlparse(self.path).query).get("t", [""])[0]
                return hmac.compare_digest(tok.encode(), server.token.encode())

            # -- routes
            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    page = REMOTE_HTML.replace("__SUPPORT_URL__", html.escape(server.support_url, quote=True))
                    page = page.replace("__SUPPORT_HIDDEN__", "" if server.support_url else "hidden")
                    return self._send(200, page, "text/html")
                if path == "/api/status":
                    if not self._authed():
                        return self._json(401, {"error": "bad token"})
                    with server._lock:
                        return self._json(200, server._status)
                if path == "/api/guide":
                    if not self._authed():
                        return self._json(401, {"error": "bad token"})
                    server.guide_requested_at = time.monotonic()
                    with server._lock:
                        return self._json(200, server._guide)
                if path == "/api/channels":
                    if not self._authed():
                        return self._json(401, {"error": "bad token"})
                    with server._lock:
                        return self._json(200, server._channels)
                self._json(404, {"error": "not found"})

            def do_POST(self):
                if urlparse(self.path).path != "/api/cmd":
                    return self._json(404, {"error": "not found"})
                if not self._authed():
                    return self._json(401, {"error": "bad token"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n > MAX_BODY:
                        return self._json(413, {"error": "too large"})
                    msg = json.loads(self.rfile.read(n) or b"{}")
                    action = msg.get("action")
                    if action not in ACTIONS:
                        return self._json(400, {"error": "unknown action"})
                    value = msg.get("value")
                    if action in ("set_volume", "tune", "seek", "seek_ms"):
                        value = int(value)
                    server.cmd_queue.put_nowait((action, value))
                except queue.Full:
                    return self._json(503, {"error": "busy"})
                except Exception:
                    return self._json(400, {"error": "bad request"})
                self._json(200, {"ok": True})

        class _Server(ThreadingHTTPServer):
            # On Windows SO_REUSEADDR lets a second process bind the SAME port
            # and silently share it, so two RetroTV instances would split the
            # phone's requests.  Disable it so the second instance fails cleanly.
            allow_reuse_address = (sys.platform != "win32")

        try:
            self._httpd = _Server(("0.0.0.0", self.port), Handler)
        except OSError as exc:
            if self.log:
                self.log.warning("[remote] cannot listen on port %s: %s", self.port, exc)
            return False
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="retrotv-remote")
        self._thread.start()
        if self.log:
            self.log.info("[remote] listening on %s", self.url())
        return True

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def url(self, with_token=True) -> str:
        base = f"http://{lan_ip()}:{self.port}/"
        return f"{base}?t={self.token}" if with_token else base

    # ── Tk main-thread side ──────────────────────────────────────────────
    def set_snapshot(self, status: dict, channels=None):
        with self._lock:
            self._status = status
            if channels is not None:
                self._channels = channels

    def set_guide(self, guide: dict):
        with self._lock:
            self._guide = guide

    def guide_wanted(self, within=120.0) -> bool:
        """True if a phone asked for the guide recently (so it's worth rebuilding)."""
        return (time.monotonic() - self.guide_requested_at) < within

    def drain_commands(self):
        """Return all pending (action, value) tuples (call from Tk thread)."""
        out = []
        while True:
            try:
                out.append(self.cmd_queue.get_nowait())
            except queue.Empty:
                return out


REMOTE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#10141b">
<title>RetroTV Remote</title>
<style>
:root{
  --bg:#10141b; --surface:#1a202b; --surface2:#242c3a; --text:#f1f4f9; --muted:#a3afc2; --line:#2d3646;
  --accent:#6aa3ff; --on-accent:#06152e; --rec:#ff6b6b; --fav:#ffc94d; --ok:#5fd39a; --heart:#ff7a93;
  --radius:18px;
}
@media (prefers-color-scheme: light){
  :root{--bg:#f2f4f8; --surface:#ffffff; --surface2:#e8ecf3; --text:#131925; --muted:#556176; --line:#d8deea;
        --accent:#2563d9; --on-accent:#ffffff; --rec:#d12d2d; --fav:#b87800; --ok:#12805a; --heart:#d63a5c}
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text);overflow-x:hidden;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:16px;line-height:1.4}
body{padding:calc(14px + env(safe-area-inset-top)) 16px calc(92px + env(safe-area-inset-bottom));max-width:520px;margin:0 auto}
svg{width:24px;height:24px;flex:none;fill:currentColor}
svg.s{fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
button{font:inherit;color:inherit;border:0;background:none;padding:0;cursor:pointer;touch-action:manipulation}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
[hidden]{display:none!important}

/* ---------- now playing ---------- */
.card{background:var(--surface);border-radius:var(--radius);padding:18px 18px 16px}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.chan{display:flex;align-items:center;gap:10px;min-width:0}
.badge{background:var(--surface2);border-radius:10px;padding:4px 10px;font-weight:700;font-size:15px;font-variant-numeric:tabular-nums}
.chname{font-size:16px;color:var(--muted);font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status{display:flex;align-items:center;gap:6px;font-size:14px;color:var(--muted);flex:none}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.status.playing .dot{background:var(--ok)}
.status.loading .dot{background:var(--fav)}
.title{font-size:23px;line-height:1.25;font-weight:700;letter-spacing:-.01em;margin:0;
  overflow-wrap:anywhere;word-break:break-word;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.sub{font-size:15px;color:var(--muted);margin-top:6px;overflow-wrap:anywhere}
.sub:empty{display:none}
.prog{height:5px;border-radius:3px;background:var(--surface2);margin-top:14px;overflow:hidden}
.prog i{display:block;height:100%;width:0;background:var(--accent);border-radius:3px}
.seek{display:flex;align-items:center;gap:10px;margin-top:10px;font-size:14px;color:var(--muted);font-variant-numeric:tabular-nums}
.seek input{flex:1;min-width:0}
.seek span{min-width:42px}.seek span:last-child{text-align:right}
.rec-note{display:flex;align-items:center;gap:8px;margin-top:12px;color:var(--rec);font-weight:600;font-size:15px}
.rec-note i{width:10px;height:10px;border-radius:50%;background:var(--rec)}

/* ---------- sliders ---------- */
input[type=range]{-webkit-appearance:none;appearance:none;background:none;height:40px;margin:0;width:100%;touch-action:pan-y}
input[type=range]::-webkit-slider-runnable-track{height:6px;border-radius:3px;background:var(--surface2)}
input[type=range]::-moz-range-track{height:6px;border-radius:3px;background:var(--surface2)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;border-radius:50%;background:var(--accent);margin-top:-10px;border:3px solid var(--bg)}
input[type=range]::-moz-range-thumb{width:20px;height:20px;border-radius:50%;background:var(--accent);border:3px solid var(--bg)}

/* ---------- controls ---------- */
.controls{display:grid;gap:12px;margin-top:14px}
.btn{background:var(--surface);border-radius:var(--radius);min-height:60px;display:flex;align-items:center;justify-content:center;gap:10px;
  font-weight:650;font-size:16px;transition:transform .08s,background .12s}
.btn:active{transform:scale(.97);background:var(--surface2)}
.btn.primary{background:var(--accent);color:var(--on-accent);min-height:72px;font-size:19px}
.btn.primary:active{background:var(--accent);filter:brightness(.9)}
.btn.primary svg{width:30px;height:30px}
.stepper{display:grid;grid-template-columns:76px 1fr 76px;background:var(--surface);border-radius:var(--radius);align-items:stretch;overflow:hidden}
.stepper button{min-height:68px;display:flex;align-items:center;justify-content:center;background:var(--surface2);margin:6px;border-radius:13px}
.stepper button:active{background:var(--accent);color:var(--on-accent)}
.stepper .mid{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-width:0;padding:6px 4px}
.stepper .mid b{font-size:26px;line-height:1.1;font-variant-numeric:tabular-nums}
.stepper .mid span{font-size:14px;color:var(--muted)}
.vol{display:flex;align-items:center;gap:8px;background:var(--surface);border-radius:var(--radius);padding:4px 16px 4px 6px}
.vol .mute{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;flex:none}
.vol .mute:active{background:var(--surface2)}
.vol .mute.on{color:var(--rec)}
.vol output{min-width:34px;text-align:right;font-weight:650;font-variant-numeric:tabular-nums}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.tile{background:var(--surface);border-radius:var(--radius);min-height:76px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;font-size:14px;font-weight:600;padding:6px 2px}
.tile:active{background:var(--surface2)}
.tile.on{color:var(--accent)}
.tile.fav.on{color:var(--fav)}.tile.fav.on svg{fill:currentColor}
.tile.rec.on{color:var(--rec)}
.tile.rec svg{fill:var(--rec)}

/* ---------- mode switches ---------- */
body:not([data-mode=local]) .only-local{display:none}
body[data-mode=guide] .not-guide{display:none}

/* ---------- channels ---------- */
.search{width:100%;background:var(--surface);color:var(--text);border:2px solid transparent;border-radius:14px;padding:14px 16px;font:inherit;font-size:17px}
.search:focus{border-color:var(--accent);outline:0}
.search::placeholder{color:var(--muted)}
.filters{display:flex;gap:8px;margin:12px 0}
.chip{background:var(--surface);border-radius:999px;padding:9px 18px;font-weight:600;font-size:15px;color:var(--muted)}
.chip[aria-pressed=true]{background:var(--accent);color:var(--on-accent)}
.list{display:grid;gap:8px}
.row{display:flex;align-items:center;gap:14px;background:var(--surface);border-radius:14px;padding:12px 14px;min-height:68px;text-align:left;width:100%}
.row:active{background:var(--surface2)}
.row .n{min-width:46px;text-align:center;background:var(--surface2);border-radius:10px;padding:6px 0;font-weight:700;font-size:16px;font-variant-numeric:tabular-nums}
.row .t{flex:1;min-width:0}
.row .t b{display:block;font-size:17px;line-height:1.25;overflow-wrap:anywhere}
.row .t span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:14px;color:var(--muted);margin-top:2px;overflow-wrap:anywhere}
.row .t span:empty{display:none}
.row.cur{outline:2px solid var(--accent)}
.row.cur .n{background:var(--accent);color:var(--on-accent)}
.row .star{color:var(--fav);width:20px;height:20px}
.empty{color:var(--muted);text-align:center;padding:28px 8px}

/* ---------- info panel ---------- */
.sheet{position:fixed;left:0;right:0;top:0;bottom:calc(76px + env(safe-area-inset-bottom));background:var(--bg);z-index:4;
  overflow-y:auto;-webkit-overflow-scrolling:touch;padding:calc(14px + env(safe-area-inset-top)) 16px 24px}
.sheet>div{max-width:520px;margin:0 auto}
.sheet-h{display:flex;align-items:center;justify-content:space-between;gap:12px}
.sheet-h h2{margin:0;font-size:24px;line-height:1.2}
.sheet-h p{margin:2px 0 0;color:var(--muted);font-size:16px;overflow-wrap:anywhere}
.close{width:48px;height:48px;border-radius:14px;background:var(--surface);display:flex;align-items:center;justify-content:center;flex:none}
.close:active{background:var(--surface2)}
.ii{background:var(--surface);border-radius:var(--radius);padding:16px;margin-top:12px}
.ii.now{outline:2px solid var(--accent)}
.ii .il{font-size:14px;font-weight:700;color:var(--accent);margin-bottom:2px}
.ii .it{font-size:20px;line-height:1.25;font-weight:700;overflow-wrap:anywhere}
.ii .itime{font-size:15px;color:var(--muted);margin-top:4px;font-variant-numeric:tabular-nums}
.ii .imeta{display:inline-block;margin-top:8px;background:var(--surface2);border-radius:8px;padding:3px 10px;font-size:14px;font-weight:600}
.ii .idesc{font-size:16px;line-height:1.5;color:var(--muted);margin-top:10px;overflow-wrap:anywhere}
.ii .prog{margin-top:12px}
.note{color:var(--muted);margin-top:14px;font-size:15px}

/* ---------- channel guide ---------- */
.guide{position:fixed;left:0;right:0;top:0;bottom:calc(76px + env(safe-area-inset-bottom));z-index:3;background:var(--bg)}
.gscroll{height:100%;overflow:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.ggrid{position:relative;min-height:100%}
.ghead{display:flex;position:sticky;top:0;z-index:4;height:46px;background:var(--bg)}
.gcorner{position:sticky;left:0;z-index:5;width:124px;flex:none;background:var(--bg);display:flex;align-items:center;padding:0 12px;
  font-weight:650;font-size:14px;color:var(--muted);border-bottom:1px solid var(--line)}
.gticks{position:relative;flex:none;border-bottom:1px solid var(--line)}
.gtick{position:absolute;top:0;height:46px;display:flex;align-items:center;padding-left:10px;font-size:15px;font-weight:650;
  color:var(--muted);border-left:1px solid var(--line);font-variant-numeric:tabular-nums;white-space:nowrap}
.grow{display:flex;height:80px;border-bottom:1px solid var(--line)}
.gch{position:sticky;left:0;z-index:3;flex:none;width:124px;background:var(--bg);display:flex;flex-direction:column;justify-content:center;
  gap:3px;padding:6px 10px;text-align:left;border-right:1px solid var(--line)}
.gch .gnum{font-weight:700;font-size:16px;font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:6px}
.gch .gname{font-size:13.5px;font-weight:650;line-height:1.2;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow-wrap:break-word}
.grow.cur .gch{background:var(--accent);color:var(--on-accent)}
.gstar{width:14px;height:14px;color:var(--fav)}
.grow.cur .gstar{color:var(--on-accent)}
.gtrack{position:relative;flex:none}
.gc{position:absolute;top:5px;bottom:5px;border-radius:12px;background:var(--surface);padding:7px 10px;overflow:hidden;text-align:left;
  display:flex;flex-direction:column;gap:2px}
.gc:active{background:var(--surface2)}
.gc.now{background:var(--surface2);outline:2px solid var(--accent);outline-offset:-2px}
.gc .gt{font-size:15px;font-weight:650;line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.gc .gl{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gc .gp{position:absolute;left:10px;right:10px;bottom:5px;height:3px;border-radius:2px;background:var(--bg)}
.gc .gp i{display:block;height:100%;background:var(--accent);border-radius:2px}
.gc .grec{position:absolute;top:8px;right:9px;width:10px;height:10px;border-radius:50%;background:var(--rec)}
.gnow{position:absolute;top:46px;bottom:0;width:2px;background:var(--rec);opacity:.6;pointer-events:none;z-index:2}
.sheet-h h2{overflow-wrap:anywhere}
.watch{width:100%;margin-top:14px}
.support{display:flex;align-items:center;justify-content:center;gap:10px;min-height:56px;background:var(--surface);border-radius:var(--radius);
  color:var(--text);text-decoration:none;font-weight:650;font-size:16px;margin-top:4px}
.support:active{background:var(--surface2)}
.support svg{color:var(--heart)}

/* ---------- tab bar ---------- */
.tabs{position:fixed;left:0;right:0;bottom:0;background:var(--surface);border-top:1px solid var(--line);
  display:flex;justify-content:center;padding:6px 16px calc(6px + env(safe-area-inset-bottom));z-index:5}
.tabs div{display:flex;width:100%;max-width:520px;gap:8px}
.tabs button{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;padding:8px 0;border-radius:12px;font-size:14px;font-weight:650;color:var(--muted)}
.tabs button[aria-selected=true]{color:var(--accent);background:var(--surface2)}

/* ---------- messages ---------- */
.banner{position:fixed;left:12px;right:12px;top:calc(10px + env(safe-area-inset-top));z-index:9;background:var(--rec);color:#fff;
  border-radius:12px;padding:12px 14px;font-weight:600;font-size:15px;box-shadow:0 6px 24px rgba(0,0,0,.35)}
.pair{background:var(--surface);border-radius:var(--radius);padding:24px 20px;margin-top:8vh}
.pair h1{font-size:24px;margin:0 0 8px}
.pair p{color:var(--muted);margin:0 0 16px}
.pair input{width:100%;font:inherit;font-size:22px;letter-spacing:.08em;padding:14px 16px;border-radius:14px;border:2px solid var(--line);background:var(--bg);color:var(--text);margin-bottom:12px}
.pair input:focus{border-color:var(--accent);outline:0}
.pair .err{color:var(--rec);font-weight:600;margin:0 0 12px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body data-mode="live">

<div id="banner" class="banner" hidden></div>

<!-- pairing -->
<section id="pair" class="pair" hidden>
  <h1>Connect to RetroTV</h1>
  <p>Enter the code shown in RetroTV. Click the phone icon in the control bar to see it.</p>
  <p id="pairErr" class="err" hidden>That code didn't work. Check it and try again.</p>
  <input id="code" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="Pairing code" aria-label="Pairing code">
  <button class="btn primary" id="connect" style="width:100%">Connect</button>
</section>

<!-- remote tab -->
<main id="viewRemote" hidden>
  <section class="card" aria-live="polite">
    <div class="top">
      <div class="chan"><span class="badge" id="chnum">--</span><span class="chname" id="chname"></span></div>
      <div class="status" id="status"><i class="dot"></i><span id="stText">Connecting</span></div>
    </div>
    <h1 class="title" id="title">Loading</h1>
    <div class="sub" id="sub"></div>
    <div class="prog" id="prog" hidden><i id="pbar"></i></div>
    <div class="seek" id="seekrow" hidden>
      <span id="tcur">0:00</span>
      <input id="seek" type="range" min="0" max="1000" value="0" aria-label="Playback position">
      <span id="ttot">0:00</span>
    </div>
    <div class="rec-note" id="recnote" hidden><i></i>Recording this channel</div>
  </section>

  <div class="controls">
    <button class="btn primary not-guide" id="pp" data-cmd="play_pause">
      <svg id="icPlay" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
      <svg id="icPause" viewBox="0 0 24 24" hidden><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
      <span id="ppLabel">Pause</span>
    </button>

    <div class="stepper">
      <button data-cmd="prev" aria-label="Channel down"><svg class="s" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></button>
      <div class="mid"><b id="stCh">--</b><span>Channel</span></div>
      <button data-cmd="next" aria-label="Channel up"><svg class="s" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></svg></button>
    </div>

    <div class="stepper only-local">
      <button data-cmd="prev_track" aria-label="Previous track"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zM9.5 12l8.5 6V6z"/></svg></button>
      <div class="mid"><b id="stTr">--</b><span id="stTrOf">Track</span></div>
      <button data-cmd="next_track" aria-label="Next track"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
    </div>

    <div class="vol">
      <button class="mute" id="mute" data-cmd="mute" aria-label="Mute" aria-pressed="false">
        <svg id="icVol" class="s" viewBox="0 0 24 24"><path d="M4 10v4h3.5L12 17.5v-11L7.5 10z" fill="currentColor"/><path d="M15.5 9a4 4 0 010 6M18 6.5a7.5 7.5 0 010 11"/></svg>
        <svg id="icMute" class="s" viewBox="0 0 24 24" hidden><path d="M4 10v4h3.5L12 17.5v-11L7.5 10z" fill="currentColor"/><path d="M16 9.5l5 5M21 9.5l-5 5"/></svg>
      </button>
      <input id="vol" type="range" min="0" max="100" value="80" aria-label="Volume">
      <output id="volv">80</output>
    </div>

    <div class="tiles">
      <button class="tile" id="bfs" data-cmd="fullscreen" aria-pressed="false">
        <svg class="s" viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>Full screen</button>
      <button class="tile fav not-guide" id="bfav" data-cmd="favorite" aria-pressed="false">
        <svg class="s" viewBox="0 0 24 24"><path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 17l-5.2 2.7 1-5.9L3.5 9.7l5.9-.8z"/></svg>Favorite</button>
      <button class="tile not-guide" id="binfo" aria-pressed="false">
        <svg class="s" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>Info</button>
      <button class="tile rec not-guide" id="brec" data-cmd="record" aria-pressed="false">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6.5"/></svg><span id="recLabel">Record</span></button>
    </div>
    <a class="support" href="__SUPPORT_URL__" target="_blank" rel="noopener noreferrer" __SUPPORT_HIDDEN__>
      <svg viewBox="0 0 24 24"><path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 2.8 5 6.2 5c2.1 0 3.7 1.2 5.8 3.5C14.1 6.2 15.7 5 17.8 5c3.4 0 5.3 3.4 3.8 6.8C19.5 16.4 12 21 12 21z"/></svg>
      Support this project</a>
  </div>
</main>

<!-- channels tab -->
<main id="viewChannels" hidden>
  <input class="search" id="q" type="search" placeholder="Search channels or programs" aria-label="Search channels" autocomplete="off">
  <div class="filters">
    <button class="chip" id="fAll" aria-pressed="true">All</button>
    <button class="chip" id="fFav" aria-pressed="false">Favorites</button>
  </div>
  <div class="list" id="list"></div>
</main>

<!-- channel guide tab -->
<main id="viewGuide" class="guide" hidden>
  <div class="gscroll" id="gScroll"><div class="ggrid" id="gGrid"><div class="note" style="padding:20px 16px">Loading the guide...</div></div></div>
</main>

<section id="progSheet" class="sheet" role="dialog" aria-label="Programme details" hidden><div>
  <div class="sheet-h">
    <div><h2 id="pHead"></h2><p id="pCh"></p></div>
    <button class="close" id="pClose" aria-label="Close"><svg class="s" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>
  <div id="pBody"></div>
</div></section>

<section id="infoSheet" class="sheet" role="dialog" aria-label="Channel info" hidden><div>
  <div class="sheet-h">
    <div><h2 id="infoHead">Info</h2><p id="infoCh"></p></div>
    <button class="close" id="infoClose" aria-label="Close info"><svg class="s" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>
  <div id="infoBody"></div>
</div></section>

<nav class="tabs" id="tabs" hidden><div>
  <button id="tabRemote" aria-selected="true">
    <svg class="s" viewBox="0 0 24 24"><rect x="7" y="2.5" width="10" height="19" rx="3"/><circle cx="12" cy="8" r="1.5"/><path d="M10 13h4M10 16.5h4"/></svg>Remote</button>
  <button id="tabGuide" aria-selected="false">
    <svg class="s" viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M3.5 10h17M10 10v9"/></svg>Guide</button>
  <button id="tabChannels" aria-selected="false">
    <svg class="s" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>Channels</button>
</div></nav>

<script>
var TOKEN = "", chans = [], cur = null, favOnly = false;
var volBusy = false, seekBusy = false, seekLen = 0, failCount = 0;
var infoOpen = false, lastStatus = null, infoKey = "";
function $(id){ return document.getElementById(id); }
try {
  var p = new URLSearchParams(location.search).get("t");
  if (p) { localStorage.setItem("rtv_token", p); history.replaceState(null, "", location.pathname); }
  TOKEN = localStorage.getItem("rtv_token") || "";
} catch (e) {}

function fmt(ms){
  var s = Math.max(0, Math.floor(ms/1000)), h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
  return (h ? h + ":" + String(m).padStart(2,"0") : m) + ":" + String(x).padStart(2,"0");
}
function hide(el, on){ el.toggleAttribute("hidden", !!on); }   // SVG elements have no .hidden property
function banner(t){ var b=$("banner"); b.textContent=t||""; b.hidden=!t; }

/* ---- api ---- */
function api(path, opts){
  opts = opts || {}; opts.headers = Object.assign({"X-Token": TOKEN}, opts.headers || {});
  return fetch(path, opts).then(function(r){
    if (r.status === 401) { showPair(true); throw new Error("auth"); }
    return r.json();
  });
}
function cmd(action, value){
  if (navigator.vibrate) navigator.vibrate(12);
  return api("/api/cmd", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:action, value:value})})
    .then(function(){ setTimeout(poll, 250); })
    .catch(function(e){ if (e.message !== "auth") banner("Can't reach RetroTV. Check the PC is on and on the same Wi-Fi."); });
}

/* ---- views ---- */
function showPair(bad){
  $("pair").hidden = false; $("viewRemote").hidden = true; $("viewChannels").hidden = true; $("viewGuide").hidden = true; $("tabs").hidden = true;
  $("pairErr").hidden = !(bad && TOKEN);
}
var curTab = "remote", guideFirst = true;
var VIEWS = {remote:["viewRemote","tabRemote"], guide:["viewGuide","tabGuide"], channels:["viewChannels","tabChannels"]};
function showTab(name){
  curTab = name;
  if (name !== "remote" && infoOpen) setInfo(false);
  $("progSheet").hidden = true;
  Object.keys(VIEWS).forEach(function(k){
    $(VIEWS[k][0]).hidden = (k !== name);
    $(VIEWS[k][1]).setAttribute("aria-selected", k === name);
  });
  window.scrollTo(0, 0);
  if (name === "channels") loadChannels();
  if (name === "guide") { guideFirst = true; loadGuide(); }
}
function showApp(){ $("pair").hidden = true; $("tabs").hidden = false;
  if ($("viewRemote").hidden && $("viewChannels").hidden && $("viewGuide").hidden) showTab("remote"); }

/* ---- status ---- */
function pressed(el, on){ el.classList.toggle("on", !!on); el.setAttribute("aria-pressed", !!on); }
function render(s){
  lastStatus = s;
  var guide = s.channel_number === 0;
  var mode = guide ? "guide" : s.dvr ? "dvr" : s.local ? "local" : "live";
  document.body.dataset.mode = mode;

  $("chnum").textContent = guide ? "0" : s.channel_number;
  $("chname").textContent = guide ? "Channel guide" : s.dvr ? "DVR recording" : s.channel_name;
  $("title").textContent = guide ? "Channel guide" : (s.now || s.channel_name);
  $("sub").textContent = s.local ? "" : (s.next ? "Up next: " + s.next : (mode === "live" ? "Live" : ""));

  var st = $("status"), word = s.state === "PLAYING" ? "Playing" : s.state === "PAUSED" ? "Paused" : s.state === "LOADING" ? "Loading" : "Stopped";
  st.className = "status " + (s.state === "PLAYING" ? "playing" : s.state === "LOADING" ? "loading" : "");
  $("stText").textContent = word + (s.muted ? ", muted" : "");

  var seekable = !!s.seekable;
  $("seekrow").hidden = !seekable;
  $("prog").hidden = seekable || !(s.progress > 0);
  $("pbar").style.width = ((s.progress || 0) * 100) + "%";
  if (seekable) {
    seekLen = s.length_ms; $("ttot").textContent = fmt(s.length_ms);
    // One slider step per second of media (was 1/1000 of the file, i.e. ~7 s on a 2 h movie)
    var smax = Math.max(1, Math.round(s.length_ms / 1000));
    if (String(smax) !== $("seek").max) $("seek").max = smax;
    if (!seekBusy) { $("seek").value = Math.round(s.time_ms / 1000); $("tcur").textContent = fmt(s.time_ms); }
  }
  $("recnote").hidden = !s.recording;

  var playing = s.state === "PLAYING";
  hide($("icPlay"), playing); hide($("icPause"), !playing);
  $("ppLabel").textContent = playing ? "Pause" : "Play";

  $("stCh").textContent = guide ? "Guide" : s.channel_number;
  if (s.local) { $("stTr").textContent = s.track_pos; $("stTrOf").textContent = "of " + s.track_total + " tracks"; }

  pressed($("mute"), s.muted);
  $("mute").setAttribute("aria-label", s.muted ? "Unmute" : "Mute");
  hide($("icVol"), !!s.muted); hide($("icMute"), !s.muted);
  pressed($("bfs"), s.fullscreen);
  pressed($("bfav"), s.favorite);
  pressed($("brec"), s.recording);
  $("recLabel").textContent = s.recording ? "Stop" : "Record";
  if (!volBusy) { $("vol").value = s.volume; $("volv").textContent = s.volume; }

  if (infoOpen) renderInfo(s);
  if (cur !== s.channel_number) { cur = s.channel_number; renderList(); }
}
function poll(){
  api("/api/status").then(function(s){
    failCount = 0; banner("");
    if (!s.ready) return;
    showApp(); render(s);
  }).catch(function(e){
    if (e.message === "auth") return;
    if (++failCount >= 2) banner("Can't reach RetroTV. Check the PC is on and on the same Wi-Fi.");
  });
}

/* ---- info panel ---- */
function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
function setInfo(open){
  infoOpen = open; $("infoSheet").hidden = !open; pressed($("binfo"), open);
  if (open) { infoKey = ""; if (lastStatus) renderInfo(lastStatus); }
}
function toggleInfo(){
  var open = !infoOpen;
  setInfo(open);
  // Keep the PC's own Info overlay in step: show it when opening, hide it when closing.
  var pcOpen = !!(lastStatus && lastStatus.info_open);
  if (open !== pcOpen) cmd("guide");
}
function renderInfo(s){
  var info = s.info, key = s.channel_number + "|" + JSON.stringify(info || null);
  if (key === infoKey) return;
  infoKey = key;
  var body = $("infoBody"); body.textContent = "";
  $("infoCh").textContent = s.channel_number === 0 ? "Channel guide" : s.channel_number + "  " + s.channel_name;
  $("infoHead").textContent = info ? info.heading : "Info";
  if (!info) { body.appendChild(el("div", "note", "There is no schedule to show for this channel.")); return; }
  if (info.note) body.appendChild(el("div", "note", info.note));
  info.items.forEach(function(it){
    var c = el("div", "ii" + (it.now ? " now" : ""));
    if (it.label) c.appendChild(el("div", "il", it.label));
    c.appendChild(el("div", "it", it.title));
    if (it.time) c.appendChild(el("div", "itime", it.time));
    if (it.progress) { var pr = el("div", "prog"), bar = el("i"); bar.style.width = (it.progress * 100) + "%"; pr.appendChild(bar); c.appendChild(pr); }
    if (it.meta) c.appendChild(el("span", "imeta", it.meta));
    if (it.desc) c.appendChild(el("div", "idesc", it.desc));
    body.appendChild(c);
  });
}

/* ---- channel guide ---- */
var CHW = 124, SLOTW = 160;
function loadGuide(){
  api("/api/guide").then(function(g){
    if (!g.ready) { setTimeout(function(){ if (curTab === "guide") loadGuide(); }, 1000); return; }
    renderGuide(g);
  }).catch(function(){});
}
function svgStar(cls){
  var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", cls); s.setAttribute("fill", "currentColor");
  var pth = document.createElementNS("http://www.w3.org/2000/svg", "path");
  pth.setAttribute("d", "M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 17l-5.2 2.7 1-5.9L3.5 9.7l5.9-.8z");
  s.appendChild(pth); return s;
}
function renderGuide(g){
  var grid = $("gGrid"), trackW = g.slots * SLOTW, curRow = null;
  grid.textContent = ""; grid.style.width = (CHW + trackW) + "px";
  var head = el("div", "ghead"); head.appendChild(el("div", "gcorner", "Channel"));
  var ticks = el("div", "gticks"); ticks.style.width = trackW + "px";
  g.ticks.forEach(function(t, i){ var d = el("div", "gtick", t); d.style.left = (i * SLOTW) + "px"; d.style.width = SLOTW + "px"; ticks.appendChild(d); });
  head.appendChild(ticks); grid.appendChild(head);
  g.rows.forEach(function(r){
    var row = el("div", "grow" + (r.n === cur ? " cur" : ""));
    var ch = el("button", "gch"); ch.setAttribute("aria-label", "Watch channel " + r.n + " " + r.name);
    var num = el("span", "gnum", String(r.n)); if (r.fav) num.appendChild(svgStar("gstar"));
    ch.appendChild(num); ch.appendChild(el("span", "gname", r.name));
    ch.onclick = function(){ cmd("tune", r.n); showTab("remote"); };
    var track = el("div", "gtrack"); track.style.width = trackW + "px";
    r.cells.forEach(function(c){
      var b = el("button", "gc" + (c.now ? " now" : ""));
      b.style.left = (c.s / g.slot * SLOTW + 2) + "px";
      b.style.width = Math.max(8, (c.e - c.s) / g.slot * SLOTW - 4) + "px";
      b.appendChild(el("span", "gt", c.t)); if (c.l) b.appendChild(el("span", "gl", c.l));
      if (c.p) { var pr = el("span", "gp"), bar = el("i"); bar.style.width = (c.p * 100) + "%"; pr.appendChild(bar); b.appendChild(pr); }
      if (c.rec) b.appendChild(el("span", "grec"));
      b.onclick = function(){ openProg(r, c); };
      track.appendChild(b);
    });
    row.appendChild(ch); row.appendChild(track); grid.appendChild(row);
    if (r.n === cur) curRow = row;
  });
  var nl = el("div", "gnow"); nl.style.left = (CHW + g.now / g.slot * SLOTW) + "px"; grid.appendChild(nl);
  if (guideFirst && curRow) { guideFirst = false; $("gScroll").scrollTop = Math.max(0, curRow.offsetTop - 150); }
}
function openProg(r, c){
  $("pHead").textContent = c.t; $("pCh").textContent = r.n + "  " + r.name;
  var body = $("pBody"); body.textContent = "";
  var card = el("div", "ii" + (c.now ? " now" : ""));
  if (c.now) card.appendChild(el("div", "il", "On now"));
  if (c.l) card.appendChild(el("div", "itime", c.l));
  if (c.p) { var pr = el("div", "prog"), bar = el("i"); bar.style.width = (c.p * 100) + "%"; pr.appendChild(bar); card.appendChild(pr); }
  if (c.c) card.appendChild(el("span", "imeta", c.c));
  if (c.d) card.appendChild(el("div", "idesc", c.d));
  if (c.rec) card.appendChild(el("div", "idesc", "A recording is scheduled for this program."));
  if (!c.c && !c.d) card.appendChild(el("div", "idesc", "No description is available."));
  body.appendChild(card);
  var w = el("button", "btn primary watch", "Watch channel " + r.n);
  w.onclick = function(){ cmd("tune", r.n); $("progSheet").hidden = true; showTab("remote"); };
  body.appendChild(w);
  $("progSheet").hidden = false;
}

/* ---- channels ---- */
function loadChannels(){
  api("/api/channels").then(function(c){ chans = c; renderList(); }).catch(function(){});
}
function renderList(){
  var q = $("q").value.trim().toLowerCase(), box = $("list"), shown = 0;
  box.textContent = "";
  chans.forEach(function(c){
    if (favOnly && !c.favorite) return;
    if (q && (c.name + " " + (c.now || "") + " " + (c.description || "")).toLowerCase().indexOf(q) < 0) return;
    shown++;
    var r = document.createElement("button"); r.className = "row" + (c.number === cur ? " cur" : "");
    var n = document.createElement("span"); n.className = "n"; n.textContent = c.number;
    var t = document.createElement("span"); t.className = "t";
    var a = document.createElement("b"); a.textContent = c.name;
    var b = document.createElement("span"); b.textContent = c.now || c.description || "";
    t.appendChild(a); t.appendChild(b); r.appendChild(n); r.appendChild(t);
    if (c.favorite) {
      var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "star");
      var pth = document.createElementNS("http://www.w3.org/2000/svg", "path");
      pth.setAttribute("d", "M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 17l-5.2 2.7 1-5.9L3.5 9.7l5.9-.8z");
      s.appendChild(pth); r.appendChild(s);
    }
    r.onclick = function(){ cmd("tune", c.number); showTab("remote"); };
    box.appendChild(r);
  });
  if (!shown) {
    var e = document.createElement("div"); e.className = "empty";
    e.textContent = favOnly && !q ? "No favorites yet. Tap Favorite on the Remote tab to add the current channel." : "No channels match your search.";
    box.appendChild(e);
  }
}

/* ---- wiring ---- */
document.addEventListener("click", function(e){
  var b = e.target.closest("[data-cmd]"); if (b) cmd(b.dataset.cmd);
});
$("binfo").onclick = toggleInfo;
$("infoClose").onclick = toggleInfo;
$("tabRemote").onclick = function(){ showTab("remote"); };
$("tabChannels").onclick = function(){ showTab("channels"); };
$("tabGuide").onclick = function(){ showTab("guide"); };
$("pClose").onclick = function(){ $("progSheet").hidden = true; };
$("q").addEventListener("input", renderList);
function setFilter(fav){ favOnly = fav; $("fAll").setAttribute("aria-pressed", !fav); $("fFav").setAttribute("aria-pressed", fav); renderList(); }
$("fAll").onclick = function(){ setFilter(false); };
$("fFav").onclick = function(){ setFilter(true); };

var vol = $("vol");
vol.addEventListener("input", function(){ volBusy = true; $("volv").textContent = vol.value; });
vol.addEventListener("change", function(){ cmd("set_volume", parseInt(vol.value, 10)); setTimeout(function(){ volBusy = false; }, 600); });
var seek = $("seek");
var seekTimer = null;
seek.addEventListener("input", function(){ seekBusy = true; clearTimeout(seekTimer); $("tcur").textContent = fmt(parseInt(seek.value, 10) * 1000); });
seek.addEventListener("change", function(){
  clearTimeout(seekTimer);
  cmd("seek_ms", parseInt(seek.value, 10) * 1000);
  seekTimer = setTimeout(function(){ seekBusy = false; }, 2500);
});

function savePair(){ TOKEN = $("code").value.trim(); try { localStorage.setItem("rtv_token", TOKEN); } catch (e) {} start(); }
$("connect").onclick = savePair;
$("code").addEventListener("keydown", function(e){ if (e.key === "Enter") savePair(); });

function start(){ if (!TOKEN) { showPair(false); return; } poll(); loadChannels(); }
start();
setInterval(function(){ if (TOKEN && !document.hidden) poll(); }, 2000);
setInterval(function(){ if (TOKEN && !document.hidden) loadChannels(); }, 60000);
setInterval(function(){ if (TOKEN && !document.hidden && curTab === "guide") loadGuide(); }, 30000);
document.addEventListener("visibilitychange", function(){ if (!document.hidden && TOKEN) poll(); });
</script></body></html>
"""
