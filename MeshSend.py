"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import struct
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
                # Nobody can ack a broadcast. Asking anyway makes the firmware
                # retransmit it up to three more times on its own, which
                # collides with our own steady pacing on an already busy channel.
                link.sendData(packet)
            print("  sent      %s" % label)

        if position < total - 1:
            time.sleep(gap)

    return total
