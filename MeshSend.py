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
SEND_GAP_SECONDS = 4.0

# How long to wait for the device's own TX queue to report empty before
# giving up and moving on anyway - a jammed queue would otherwise stall the
# whole message forever - and how often to check.
#
# This is an escape hatch, not pacing. It belongs above how long a packet
# really takes to clear, so that it fires only when something is wedged -
# but it is now also the cost of LEARNING that a radio never reports its
# queue draining, paid QUEUE_TRUST_LIMIT times before we stop asking. On the
# measured run every packet ran the clock out at 15 s while packets were
# plainly getting through, so 15 s bought nothing but 30 s of dead air per
# session. Six is still triple the ~2 s airtime of a full 200-byte packet at
# LONG_FAST.
QUEUE_CLEAR_TIMEOUT = 6.0

# Polling costs a memory read: queueStatus is a cached protobuf that the
# interface's reader thread updates, not a round trip to the radio. A coarse
# poll would just add dead air to every packet.
QUEUE_POLL_SECONDS = 0.1

# Some firmware sends a QueueStatus only when a packet is ENQUEUED, never
# again when it is actually transmitted. free then never climbs back to
# maxlen, so waiting for an empty queue can only ever end in the timeout -
# measured here as every single packet reporting "GAVE UP" at exactly the
# timeout while the mesh delivered them normally. Waiting on a signal that
# cannot arrive is worse than not waiting: it is a fixed delay wearing the
# costume of real feedback. After this many consecutive timeouts we stop
# believing the queue and pace on airtime instead.
QUEUE_TRUST_LIMIT = 2

_queue_timeouts = 0
_queue_trusted = True


def forget_queue_trust():
    """Believe the queue again. Call when a different radio is opened - the
    next one may be firmware that does report the drain."""
    global _queue_timeouts, _queue_trusted
    _queue_timeouts, _queue_trusted = 0, True


def wait_for_clear_queue(link, timeout = QUEUE_CLEAR_TIMEOUT, poll = QUEUE_POLL_SECONDS):
    """Block until the radio's own TX queue reports empty.

    This is what actually gates "one packet at a time": queueStatus.free is
    real feedback from the device about what it has and has not transmitted
    yet, not a guess about airtime. If this firmware never reports a queue
    depth at all (queueStatus stays None), there is nothing to wait on, and
    the caller's own SEND_GAP_SECONDS pause is the only pacing available.

    Returns (seconds waited, whether the timeout fired). Giving up means the
    next packet goes out with this one still outstanding - the very burst
    this is here to prevent - so the caller says so rather than hiding it.
    Airtime pacing is not "giving up": it is the deliberate fallback for a
    radio whose queue we have learned says nothing useful.
    """
    global _queue_timeouts, _queue_trusted

    if getattr(link, "queueStatus", None) is None or not _queue_trusted:
        time.sleep(SEND_GAP_SECONDS)
        return SEND_GAP_SECONDS, False

    # Timed against the clock rather than counted in poll intervals, so the
    # number that comes back is what actually elapsed.
    start = time.time()
    while link.queueStatus.free < link.queueStatus.maxlen:
        waited = time.time() - start
        if waited < timeout:
            time.sleep(poll)
            continue

        _queue_timeouts += 1
        if _queue_timeouts >= QUEUE_TRUST_LIMIT:
            _queue_trusted = False
            print("  this radio never reports its TX queue draining (free %d "
                  "of %d after %d waits); pacing on %.1f s of airtime instead"
                  % (link.queueStatus.free, link.queueStatus.maxlen,
                     _queue_timeouts, SEND_GAP_SECONDS))
        return waited, True

    _queue_timeouts = 0         # it drained; the signal is real after all
    return time.time() - start, False


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
    forget_queue_trust()
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


def resend(link, group, wanted, dest = None):
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
        send_packets(link, chosen, dest = dest, remember = False)
    return len(chosen)


def send_packets(link, packets, dest=None, remember=True):
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
        waited, gave_up = wait_for_clear_queue(link)
        # The measurement that settles the pacing: how long this radio's own
        # queue really took, packet by packet, and what it claimed to hold.
        status = getattr(link, "queueStatus", None)
        depth = "" if status is None else "  (%d/%d free)" % (status.free,
                                                              status.maxlen)
        print("  sent      %s  waited %5.2f s%s%s"
              % (label, waited, depth,
                 "   GAVE UP - sending anyway" if gave_up else ""))

    return total
