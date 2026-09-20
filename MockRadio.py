"""A stand-in for the radio: packets travel through a folder, not the air.

One node cannot hear itself, so a single radio can never test a round trip.
This replaces the RF hop and nothing else - the same packets, the same codec,
the same chunking - so everything except the antenna is exercised for real.

    spool/down/   gateway  -> endpoint   (mail arriving)
    spool/up/     endpoint -> gateway    (replies and new mail)

Each packet is one file, so you can watch them appear and read their bytes.
That makes a decent demo in its own right when the hardware misbehaves.

Use "loopback" wherever a port name is expected:

    python MessagePing.py <account> loopback
    ... and pick Loopback in the endpoint sidebar.
"""

import itertools
import threading
import time
from pathlib import Path

SPOOL = Path(__file__).resolve().parent / "spool"
DOWN = SPOOL / "down"
UP = SPOOL / "up"

# How long a delivered packet's file is kept before being cleaned up.
KEEP_SECONDS = 120

PORT_NAME = "loopback"

# Two packets written in the same microsecond would otherwise land on the
# same filename and one would be lost.
_counter = itertools.count()


def IsLoopback(target):
    return bool(target) and str(target).strip().lower() == PORT_NAME


def _Box(outgoing):
    """Where this side writes, and where it reads from."""
    return (DOWN, UP) if outgoing == "down" else (UP, DOWN)


class LoopbackInterface:
    """Quacks like the part of the meshtastic interface we actually use.

    direction is which folder this side SENDS into: the gateway sends "down",
    the endpoint sends "up".
    """

    # A folder needs no airtime, so senders may skip the pause between packets.
    gap_hint = 0.05

    def __init__(self, direction = "down", node_id = "!loopback"):
        self.direction = direction
        self.node_id = node_id
        self.send_box, self.recv_box = _Box(direction)
        for box in (self.send_box, self.recv_box):
            box.mkdir(parents = True, exist_ok = True)
        self.seen = set()
        # Claiming a file and recording it must be one step: two callers
        # polling at once would otherwise both claim the same packet.
        self.lock = threading.Lock()

    # --- the bits MeshSend and Station call ------------------------------

    def sendData(self, packet, destinationId = None, wantAck = False, onResponse = None, **kwargs):
        name = "%.6f-%06d-%d.pkt" % (time.time(), next(_counter), len(packet))
        (self.send_box / name).write_bytes(bytes(packet))
        if onResponse is not None:
            # A folder never loses a packet, so it acknowledges at once.
            onResponse({"decoded": {"routing": {"errorReason": "NONE"}}})
        return None

    def getMyNodeInfo(self):
        return {"user": {"id": self.node_id}}

    def close(self):
        pass

    # --- receiving, which the caller polls --------------------------------

    def Receive(self):
        """Every packet that has arrived since the last call, oldest first."""
        arrived = []
        with self.lock:
            for path in sorted(self.recv_box.glob("*.pkt")):
                if path.name in self.seen:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue      # still being written; pick it up next time
                self.seen.add(path.name)
                arrived.append(data)
        return arrived

    def Sweep(self, now = None):
        """Delete packet files old enough that both sides have seen them."""
        now = now if now is not None else time.time()
        removed = 0
        for box in (self.send_box, self.recv_box):
            for path in box.glob("*.pkt"):
                try:
                    if now - path.stat().st_mtime > KEEP_SECONDS:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        return removed


def Clear():
    """Empty the spool. Worth doing before a demo."""
    removed = 0
    for box in (DOWN, UP):
        box.mkdir(parents = True, exist_ok = True)
        for path in box.glob("*.pkt"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def Pending():
    """What is sitting in the spool right now, for a status line."""
    DOWN.mkdir(parents = True, exist_ok = True)
    UP.mkdir(parents = True, exist_ok = True)
    return {"down": len(list(DOWN.glob("*.pkt"))),
            "up": len(list(UP.glob("*.pkt")))}
