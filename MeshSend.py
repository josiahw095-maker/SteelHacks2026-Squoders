"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import struct
import time
from collections import OrderedDict

# Fallback pacing, used only when the radio never reports a queue depth at
# all (old firmware). A full 200-byte packet is roughly 2 s of airtime at
# LONG_FAST, and every hop rebroadcasts what it hears.
SEND_GAP_SECONDS = 2.0

# How long to wait for the device's own TX queue to report empty before
# giving up and moving on anyway - a jammed queue would otherwise stall the
# whole message forever - and how often to check.
QUEUE_CLEAR_TIMEOUT = 10.0
QUEUE_POLL_SECONDS = 0.1


def wait_for_clear_queue(link, timeout = QUEUE_CLEAR_TIMEOUT, poll = QUEUE_POLL_SECONDS):
    """Block until the radio's own TX queue reports empty.

    This is what actually gates "one packet at a time": queueStatus.free is
    real feedback from the device about what it has and has not transmitted
    yet, not a guess about airtime. If this firmware never reports a queue
    depth at all (queueStatus stays None), there is nothing to wait on, and
    the caller's own SEND_GAP_SECONDS pause is the only pacing available.
    """
    if getattr(link, "queueStatus", None) is None:
        time.sleep(SEND_GAP_SECONDS)
        return
    waited = 0.0
    while link.queueStatus.free < link.queueStatus.maxlen and waited < timeout:
        time.sleep(poll)
        waited += poll


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


def send_packets(link, packets, dest=None, gap=SEND_GAP_SECONDS, remember=True):
    """Send packets one at a time, waiting after each for the radio's own TX
    queue to report empty before handing over the next - never more than one
    packet outstanding at once, confirmed by the device itself rather than
    guessed at with a fixed delay.

    A link of None prints instead of transmitting, so the whole pipeline can
    be exercised without hardware. dest of None broadcasts to the channel;
    an explicit dest addresses one node.
    """
    if remember:
        remember_sent(packets)

    total = len(packets)
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.hex()))
            continue

        if dest:
            link.sendData(packet, destinationId = dest)
        else:
            link.sendData(packet)
        print("  sent      %s" % label)
        wait_for_clear_queue(link)

    return total
