"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

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

    Returns how many were resent; 0 means we no longer have that message,
    which is the honest answer rather than a silent failure.
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
        send_packets(link, chosen, dest = dest, gap = gap, remember = False)
    return len(chosen)


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


# How long to wait for a single packet's ack before retrying, and how many
# tries before giving up on it (and the rest of the message with it).
ACK_TIMEOUT_SECONDS = 8.0
ACK_TRIES = 3


def _send_and_wait(link, packet, dest, timeout = ACK_TIMEOUT_SECONDS, tries = ACK_TRIES):
    """Send one packet to dest and block until the firmware confirms it,
    retrying on timeout. Returns True once acked, False if every try failed.

    Only meaningful for an addressed packet - nobody can ack a broadcast, so
    this is not used when dest is unknown.
    """
    for attempt in range(1, tries + 1):
        acked = threading.Event()
        outcome = []

        def on_response(reply):
            # Called on the radio's own thread once the ack (or nak) arrives.
            routing = ((reply or {}).get("decoded") or {}).get("routing") or {}
            outcome.append(routing.get("errorReason", "NONE"))
            acked.set()

        # hopLimit = 0: nobody may relay this. Meshtastic's "implicit ack" lets
        # the sender consider a packet delivered the moment ANY nearby node
        # rebroadcasts it - not necessarily the real destination. With relaying
        # off, only the true destination can possibly answer, so an ack here
        # actually means what we think it means.
        link.sendData(packet, destinationId = dest, wantAck = True, hopLimit = 0,
                     onResponse = on_response, onResponseAckPermitted = True)
        if acked.wait(timeout) and outcome and outcome[0] == "NONE":
            return True
        print("  no ack (attempt %d/%d)%s" % (attempt, tries,
              "" if not outcome else ": " + outcome[0]))
    return False


def send_packets(link, packets, dest=None, gap=SEND_GAP_SECONDS, remember=True):
    """Send packets one at a time, only handing over the next once the last
    is acknowledged - never more than one packet in flight.

    A link of None prints instead of transmitting, so the whole pipeline can
    be exercised without hardware. dest of None looks for the single other
    node on the mesh; if none can be pinned down, nobody can ack a broadcast,
    so packets fall back to the old courtesy-paced, unconfirmed send instead.

    Returns how many packets got through. A failed ack stops the rest of the
    message rather than sending on into a link that is not working.
    """
    if remember:
        remember_sent(packets)

    if dest is None and link is not None:
        dest = only_peer(link)

    total = len(packets)
    sent = 0
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.hex()))
            sent += 1
            continue

        if dest:
            if not _send_and_wait(link, packet, dest):
                print("  FAILED    %s  never acknowledged; stopping" % label)
                break
            print("  acked     %s" % label)
        else:
            link.sendData(packet)
            print("  sent      %s  (broadcast, unconfirmed)" % label)
            if position < total - 1:
                time.sleep(gap)
        sent += 1

    return sent
