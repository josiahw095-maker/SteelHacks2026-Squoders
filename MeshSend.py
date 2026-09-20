"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import queue
import struct
import threading
import time
from collections import OrderedDict

# Courtesy gap between packets. A full 200-byte packet is roughly 2 s of
# airtime at LONG_FAST, and every hop rebroadcasts what it hears.
SEND_GAP_SECONDS = 2.0


def scan():
    """Nearby Meshtastic nodes as (name, address), for finding the gateway."""
    from meshtastic.ble_interface import BLEInterface
    return [(d.name, d.address) for d in BLEInterface.scan()]


def open_link(target=None):
    """Connect to the gateway node.

    A COM port or /dev path opens over USB serial, which is what the hardware
    tests use; anything else is treated as a Bluetooth name or address.
    """
    from MockRadio import IsLoopback, LoopbackInterface
    if IsLoopback(target):
        # No radio at all: packets go through a folder. See MockRadio.py.
        return LoopbackInterface(direction = "down")

    if target and (target.upper().startswith("COM") or target.startswith("/dev/")):
        import meshtastic.serial_interface
        return meshtastic.serial_interface.SerialInterface(target)

    from meshtastic.ble_interface import BLEInterface
    return BLEInterface(target)


# What we have sent lately, so a lost packet can be replayed instead of the
# whole email being resent. Keyed by the 2-byte chunk id the receiver quotes.
RECENT_LIMIT = 30
_recent = OrderedDict()


def remember_sent(packets):
    """Keep a copy of an outgoing message, for possible resend."""
    if not packets:
        return
    group, _ = struct.unpack(">HB", packets[0][:3])
    _recent[group] = list(packets)
    while len(_recent) > RECENT_LIMIT:
        _recent.popitem(last = False)


def resend(link, group, wanted, gap = SEND_GAP_SECONDS, dest = None):
    """Put the named parts of a remembered message back on the air.

    Returns how many were QUEUED; 0 means we no longer have that message,
    which is the honest answer rather than a silent failure. gap is kept for
    callers that pass it, but a running sender thread paces the whole outbox
    itself, so it only applies to the inline fallback.
    """
    packets = _recent.get(group)
    if not packets:
        return 0

    chosen = []
    for packet in packets:
        _, pt = struct.unpack(">HB", packet[:3])
        if (pt >> 4) in wanted:
            chosen.append(packet)

    if chosen:
        queue_packets(link, chosen, dest = dest, remember = False)
    return len(chosen)


# --- sending without blocking the caller ------------------------------------
#
# send_packets spends SEND_GAP_SECONDS between packets, so a 15-packet email
# is half a minute inside one call. MessagePing calls it from the same loop
# that polls Gmail and acts on replies, so for that half minute the gateway
# stops watching the inbox and stops answering the endpoint. The packets still
# have to go out one at a time - that is airtime, not overhead - but nothing
# says the caller has to stand there and watch.

_outbox = queue.Queue()
_worker = None
_worker_link = None
_worker_lock = threading.Lock()


def start_sender(link, gap = SEND_GAP_SECONDS):
    """Start the thread that drains the outbox onto the air.

    Idempotent: calling it twice keeps the first thread, because two threads
    on one radio would interleave two messages packet by packet.
    """
    global _worker, _worker_link
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return _worker
        _worker_link = link
        _worker = threading.Thread(target = _pump, args = (gap,), daemon = True)
        _worker.start()
        return _worker


def _pump(gap):
    while True:
        packets, dest = _outbox.get()
        try:
            send_packets(_worker_link, packets, dest = dest, gap = gap,
                         remember = False)
            # The gap inside send_packets is between packets, not after the
            # last one, so back-to-back messages would otherwise run into
            # each other with no pause at all.
            if not _outbox.empty():
                time.sleep(gap)
        except Exception as error:
            print(f"  send failed: {type(error).__name__}: {error}")
        finally:
            _outbox.task_done()


def queue_packets(link, packets, dest = None, remember = True):
    """Hand packets to the sender thread and return at once.

    Falls back to sending inline when no sender thread is running, so a
    caller that never started one - a test, or a --dry-run - behaves exactly
    as it did before. Returns how many packets were accepted, which is not
    the same as how many have been transmitted.
    """
    if remember:
        remember_sent(packets)

    with _worker_lock:
        running = _worker is not None and _worker.is_alive()

    if not running:
        return send_packets(link, packets, dest = dest, remember = False)

    _outbox.put((list(packets), dest))
    return len(packets)


def drain(timeout = 60):
    """Wait for the outbox to empty, so Ctrl+C does not strand packets.

    Returns True if everything went out inside the timeout.
    """
    end = time.time() + timeout
    while pending() and time.time() < end:
        time.sleep(0.2)
    return not pending()


def pending():
    """How many messages are still waiting for the air.

    unfinished_tasks, not qsize: the worker pops a message off the queue
    before it starts transmitting it, so qsize drops to zero while a message
    is still going out a packet at a time. Counting only what is queued made
    drain() return the moment the LAST email started sending, which is
    exactly the one it exists to wait for.
    """
    return _outbox.unfinished_tasks


def peers(link):
    """Other nodes this radio has heard, as [(id, name)], nearest-known first.

    Meshtastic fills interface.nodes in as node-info packets arrive, so this
    grows over the first minute or two after connecting. Our own node is left
    out: there is no point addressing ourselves.
    """
    nodes = getattr(link, "nodes", None) or {}
    mine = None
    try:
        mine = link.getMyNodeInfo()["user"]["id"]
    except Exception:
        pass

    found = []
    for node_id, node in nodes.items():
        if node_id == mine:
            continue
        user = node.get("user", {}) or {}
        found.append((node_id, user.get("longName") or user.get("shortName") or node_id))
    return sorted(found, key=lambda pair: pair[1].lower())


def only_peer(link):
    """The single other node, when there is exactly one. Otherwise None.

    On a two-node mesh this is what "dynamic" means in practice: nobody has
    to type an id, and adding a third node makes the choice explicit rather
    than silently picking wrong.
    """
    found = peers(link)
    return found[0][0] if len(found) == 1 else None


def send_packets(link, packets, dest=None, gap=SEND_GAP_SECONDS, remember=True):
    """Send packets in order, pausing between them to limit airtime.

    A link of None prints instead of transmitting, so the whole pipeline can
    be exercised without hardware. dest of None broadcasts to the channel.
    """
    if remember:
        remember_sent(packets)

    total = len(packets)
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.hex()))
        else:
            if dest:
                link.sendData(packet, destinationId=dest, wantAck=True)
            else:
                link.sendData(packet, wantAck=True)
            print("  sent      %s" % label)

        if position < total - 1:
            time.sleep(gap)

    return total
