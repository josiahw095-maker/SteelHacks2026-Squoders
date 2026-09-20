"""Mesh Mail endpoint - a small HTTP API over Station, plus the web UI.

    python endpoint/server.py loopback
    python endpoint/server.py COM5
    python endpoint/server.py demo

Only the standard library. The browser polls /api/state every couple of
seconds, which matches how fast the radio delivers anyway.

The front end is plain HTML and JavaScript in endpoint/web/, so it can be
lifted onto a phone later without a build step: point it at a different
transport and the screen is unchanged.
"""

import hashlib
import json
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from station import Station

WEB = HERE / "web"
TYPES = {".html": "text/html; charset=utf-8",
         ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}

# How the current send is going, so the page can draw a real progress bar
# rather than a spinner. One send at a time is plenty here.
progress = {"active": False, "sent": 0, "total": 0, "what": ""}
progress_lock = threading.Lock()

station = None
station_lock = threading.RLock()
current_target = "loopback"


def SetProgress(**fields):
    with progress_lock:
        progress.update(fields)


def Query(path):
    """The query string of a request path, as {name: first value}."""
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


# Enumerating serial ports goes through the Windows registry and measured at
# ~2.3 ms - between 83% and 97% of an /api/state response that is otherwise a
# fraction of a millisecond. Ports change when somebody plugs a cable in, not
# every two seconds, so the list is cached and /api/ports forces a rescan.
OPTIONS_TTL = 5.0
_options = {"at": 0.0, "value": None}
_options_lock = threading.Lock()


def ScanOptions():
    """Transports the browser can offer: the two fakes, then real ports."""
    options = [
        {"value": "demo", "label": "Demo - sample mail, no radio"},
        {"value": "loopback", "label": "Loopback - real codec, packets via folder"},
    ]
    try:
        import serial.tools.list_ports as list_ports
        for port in list_ports.comports():
            radio = " (radio)" if port.vid == 0x303A else ""
            options.append({"value": port.device,
                            "label": f"{port.device} - {port.description}{radio}"})
    except Exception:
        pass
    return options


def Options(fresh = False):
    """The transport list, from cache unless it is stale or fresh is asked."""
    now = time.monotonic()
    with _options_lock:
        if not fresh and _options["value"] is not None \
                and now - _options["at"] < OPTIONS_TTL:
            return _options["value"]

    # Scanned outside the lock: a slow enumeration should not hold up the
    # pollers, and the worst a race costs is one extra scan.
    options = ScanOptions()
    with _options_lock:
        _options["value"] = options
        _options["at"] = time.monotonic()
    return options


def Connect(target):
    """Swap the radio for another one. Returns the new status line."""
    global station
    with station_lock:
        if station is not None:
            station.Close()
        if target == "demo":
            station = Station(mock = True)
            station.SeedDemo()
        else:
            station = Station(port = target)
        return station.Status()

# The page only ever draws the recent end of the inbox, but Snapshot used to
# serialize every message every poll: 24 KB of JSON at 50 messages, 225 KB at
# 500, twice a second while a send was in flight. The totals below still count
# the whole inbox, so the numbers on screen stay honest.
#
# Both caps are needed. MAX_THREADS alone bounds nothing when the mail piles
# into a handful of long conversations, which is exactly what a reply thread
# is; MAX_THREAD_MESSAGES bounds the other axis.
MAX_THREADS = 30
MAX_THREAD_MESSAGES = 50


def Snapshot():
    """Everything the page needs, in one object."""
    # A plain read of the global: rebinding it in Connect() is atomic, so the
    # worst this can catch is the station from a moment ago, whereas taking
    # station_lock would park every poller behind a ten-second Close().
    station = globals()["station"]
    if station is None:
        return {"status": "no radio", "target": current_target, "options": Options(),
                "node": None, "error": None, "threads": [], "waiting": [],
                "totals": {"messages": 0, "airbytes": 0, "delivered": 0, "packets": 0},
                "progress": {"active": False, "sent": 0, "total": 0, "what": ""},
                "feed": []}

    station.Poll()
    station.Chase()

    threads = []
    for thread, messages in station.Threads()[:MAX_THREADS]:
        newest = messages[-1]
        shown = messages[-MAX_THREAD_MESSAGES:]
        threads.append({
            "thread": thread,
            "subject": newest["subject"] or "(no subject)",
            "sender": newest["sender"],
            "at": newest["at"],
            "older": len(messages) - len(shown),
            "messages": [{
                "sender": m["sender"],
                "body": m["body"],
                "at": m["at"],
                "packets": m["packets"],
                "airbytes": m["airbytes"],
                "delivered": m["delivered"],
                "ratio": round(m["ratio"], 1),
            } for m in shown],
        })

    inbox = station.Inbox()
    with progress_lock:
        current = dict(progress)

    return {
        "status": station.Status(),
        "target": current_target,
        "options": Options(),
        "node": station.node_id,
        "error": station.error,
        "threads": threads,
        "waiting": [{"id": k, "packets": v[0], "asked": v[1]}
                    for k, v in station.Waiting().items()],
        "totals": {
            "messages": len(inbox),
            "airbytes": sum(m["airbytes"] for m in inbox),
            "delivered": sum(m["delivered"] for m in inbox),
            "packets": sum(m["packets"] for m in inbox),
        },
        "progress": current,
        "feed": station.Feed(),
    }


def Send(what, action):
    """Run a send, publishing progress as it goes."""
    SetProgress(active=True, sent=0, total=0, what=what)
    try:
        def Tick(sent, total):
            SetProgress(sent=sent, total=total)
        count = action(Tick)
        SetProgress(active=False, sent=count, total=count)
        return {"ok": True, "packets": count}
    except Exception as error:
        SetProgress(active=False)
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                      # the radio log is the interesting one

    def Reply(self, code, body, kind="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            state = Snapshot()
            # The radio is quiet most of the time, so most polls would send
            # back a payload byte for byte identical to the last one. The
            # page tells us what it already has; if nothing has moved since,
            # it costs a few dozen bytes instead of up to 225 KB, and the
            # browser skips redrawing entirely.
            raw = json.dumps(state).encode()
            version = hashlib.blake2b(raw, digest_size = 8).hexdigest()
            state["v"] = version
            if Query(self.path).get("v") == version:
                return self.Reply(200, {"unchanged": True, "v": version})
            return self.Reply(200, state)
        if path == "/api/progress":
            # Polled several times a second while a send is on the air. It
            # answers with a few dozen bytes and touches neither the radio
            # nor the inbox, which is the whole point of it existing.
            with progress_lock:
                return self.Reply(200, dict(progress))
        if path == "/api/ports":
            return self.Reply(200, {"options": Options(fresh = True),
                                    "target": current_target})

        name = "index.html" if path == "/" else path.lstrip("/")
        target = (WEB / name).resolve()
        if WEB not in target.parents or not target.is_file():
            return self.Reply(404, {"error": "not found"})
        return self.Reply(200, target.read_bytes(),
                          TYPES.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.Reply(400, {"ok": False, "error": "bad JSON"})

        if self.path == "/api/connect":
            global current_target
            target = (body.get("target") or "").strip()
            if not target:
                return self.Reply(400, {"ok": False, "error": "no transport given"})
            try:
                status = Connect(target)
            except Exception as error:
                return self.Reply(200, {"ok": False,
                                        "error": f"{type(error).__name__}: {error}"})
            current_target = target
            failed = station.error and target not in ("demo", "loopback")
            return self.Reply(200, {"ok": not failed, "status": status,
                                    "error": station.error})

        if self.path == "/api/reply":
            text = (body.get("body") or "").strip()
            if not text:
                return self.Reply(400, {"ok": False, "error": "nothing to send"})
            return self.Reply(200, Send("Sending reply", lambda tick:
                station.Reply(body["thread"], text, on_progress=tick)))

        if self.path == "/api/compose":
            to = (body.get("to") or "").strip()
            text = (body.get("body") or "").strip()
            if "@" not in to:
                return self.Reply(400, {"ok": False, "error": "that is not an address"})
            if not text:
                return self.Reply(400, {"ok": False, "error": "nothing to send"})
            return self.Reply(200, Send("Sending", lambda tick:
                station.Compose(to, (body.get("subject") or "").strip(), text,
                                on_progress=tick)))

        return self.Reply(404, {"ok": False, "error": "no such endpoint"})


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "loopback"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

    current_target = target
    print(f"radio : {Connect(target)}")
    print(f"open  : http://localhost:{port}")
    print("        (phone: swap localhost for this machine's address)")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
