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

import json
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

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


def Options():
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

def Snapshot():
    """Everything the page needs, in one object."""
    station.Poll()
    station.Chase()

    threads = []
    for thread, messages in station.Threads():
        newest = messages[-1]
        threads.append({
            "thread": thread,
            "subject": newest["subject"] or "(no subject)",
            "sender": newest["sender"],
            "at": newest["at"],
            "messages": [{
                "sender": m["sender"],
                "body": m["body"],
                "at": m["at"],
                "packets": m["packets"],
                "airbytes": m["airbytes"],
                "delivered": m["delivered"],
                "ratio": round(m["ratio"], 1),
            } for m in messages],
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
            return self.Reply(200, Snapshot())
        if path == "/api/ports":
            return self.Reply(200, {"options": Options(), "target": current_target})

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
