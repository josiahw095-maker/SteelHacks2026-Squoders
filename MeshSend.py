"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import math
import struct
import time
from collections import OrderedDict

# --- what actually limits this mesh -----------------------------------------
# Not the radio's TX queue: the channel. A LoRa packet occupies the air for a
# length of time set by the modem preset, and while it does, no node on that
# frequency can transmit. Measured here at LONG_FAST: 200-byte packets 4 s
# apart is 47% of the channel from the gateway alone, and the peer node
# repeats each one it hears, so the channel saw ~93%. At that point neither
# radio can find a clear moment, packets pile up in a TX queue that never
# drains, and sends stop reaching the air at all - which is exactly what the
# logs showed, free counting 15, 14, 13 ... down and never recovering.
#
# So pacing is computed from airtime, not guessed at and not read off the
# queue. The queue tells us whether a packet left the radio; it says nothing
# about whether we have been polite enough to let anyone else speak.

# (spreading factor, bandwidth kHz, coding-rate denominator) per preset.
PRESET_RADIO = {
    "SHORT_FAST":  (7, 250, 5),
    "SHORT_SLOW":  (8, 250, 5),
    "MEDIUM_FAST": (9, 250, 5),
    "MEDIUM_SLOW": (10, 250, 5),
    "LONG_FAST":   (11, 250, 5),
    "LONG_SLOW":   (12, 125, 8),
}
DEFAULT_PRESET = "LONG_FAST"        # what a node ships with

MESH_HEADER = 16                    # bytes the firmware wraps around our payload
PREAMBLE_SYMBOLS = 16

# The share of the channel one node may take. 10% is ordinary LoRa manners,
# and it keeps us well clear of the firmware's own airtime governor. Raising
# it does not make a message arrive sooner; past a point it stops arriving.
DUTY_CYCLE_PERCENT = 10

# Which preset the radio is actually on, learned at open_link(). Airtime
# differs by more than 10x across the presets, so guessing is not an option.
_preset = DEFAULT_PRESET


def airtime_seconds(payload_bytes, preset = None):
    """How long one packet of this size holds the air, in seconds.

    The Semtech time-on-air formula. Worth computing rather than
    approximating: every pacing decision below is a multiple of it.
    """
    sf, bw_khz, cr = PRESET_RADIO.get(preset or _preset,
                                      PRESET_RADIO[DEFAULT_PRESET])
    symbol = (2 ** sf) / (bw_khz * 1000.0)
    slow = 1 if symbol > 0.016 else 0      # long symbols need the low-rate optimizer
    payload_symbols = 8 + max(0, math.ceil(
        (8 * payload_bytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * slow))) * cr)
    return (payload_symbols + PREAMBLE_SYMBOLS + 4.25) * symbol


def gap_for(packet_bytes, preset = None):
    """How long to stay quiet after sending a packet of this size."""
    air = airtime_seconds(packet_bytes + MESH_HEADER, preset)
    return air * (100.0 / DUTY_CYCLE_PERCENT - 1.0)


def read_preset(link):
    """Ask the radio which preset it is on. Called when a link is opened."""
    global _preset
    _preset = DEFAULT_PRESET
    try:
        from meshtastic.protobuf import config_pb2
        lora = link.localNode.localConfig.lora
        name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        if lora.use_preset and name in PRESET_RADIO:
            _preset = name
    except Exception:
        pass            # no radio, or a custom modem config; assume the default
    return _preset


# How long to wait for the device's own TX queue to report empty before
# moving on, and how often to check. This is a check that the radio is
# keeping up, not the pacing - gap_for() is the pacing, and it is applied
# whatever the queue says.
QUEUE_CLEAR_TIMEOUT = 6.0
QUEUE_POLL_SECONDS = 0.1

# Some firmware sends a QueueStatus only when a packet is ENQUEUED, never
# again when it is transmitted, so free never climbs back to maxlen and the
# wait can only ever end in the timeout. Measured here: every packet ran the
# clock out while the mesh delivered them normally. After this many
# consecutive timeouts we stop asking and rely on the airtime gap alone.
QUEUE_TRUST_LIMIT = 2

# A queue this close to full means the radio is not transmitting what it has
# been given. Handing it more is pointless and then dangerous: at zero free
# slots the meshtastic library blocks inside sendData() in a sleep loop with
# no timeout, taking the whole poll loop down with it.
QUEUE_FLOOR = 2

_queue_timeouts = 0
_queue_trusted = True


def forget_queue_trust():
    """Believe the queue again. Call when a different radio is opened - the
    next one may be firmware that does report the drain."""
    global _queue_timeouts, _queue_trusted
    _queue_timeouts, _queue_trusted = 0, True


def queue_backed_up(link):
    """Whether the radio is sitting on packets it has not put on the air."""
    status = getattr(link, "queueStatus", None)
    return status is not None and status.free <= QUEUE_FLOOR


def describe_queue(link):
    """The radio's queue, for a log line. res is its error code: non-zero
    means the firmware REFUSED the packet rather than queued it."""
    status = getattr(link, "queueStatus", None)
    if status is None:
        return ""
    return "  (%d/%d free%s)" % (status.free, status.maxlen,
                                 ", res %d" % status.res if status.res else "")


def pace_after(link, packet_bytes, timeout = QUEUE_CLEAR_TIMEOUT,
               poll = QUEUE_POLL_SECONDS):
    """Wait long enough after a packet before offering the radio the next.

    The airtime gap is the floor and always applies: a queue that drains in
    300 ms does not mean we may transmit again in 300 ms. On top of that, if
    this firmware reports its queue draining, we also wait for that, so we
    never hand over a packet while one is genuinely still outstanding.

    Returns (seconds waited, whether the queue check timed out).
    """
    global _queue_timeouts, _queue_trusted

    # A spool folder has no channel to share; pacing it would be theatre.
    if getattr(link, "no_airtime", False):
        return 0.0, False

    gap = gap_for(packet_bytes)
    start = time.time()
    timed_out = False

    # Never wait on the queue for longer than we were going to stay quiet
    # anyway: past that point we are moving on regardless, so the check
    # costs nothing. At LONG_FAST the 6 s timeout disappears inside a 16.8 s
    # gap; at SHORT_FAST, where the gap is 1.6 s, this is what stops the
    # check from becoming the slowest thing in the loop.
    limit = min(timeout, gap)

    if getattr(link, "queueStatus", None) is not None and _queue_trusted:
        while link.queueStatus.free < link.queueStatus.maxlen:
            if time.time() - start >= limit:
                timed_out = True
                _queue_timeouts += 1
                if _queue_timeouts >= QUEUE_TRUST_LIMIT:
                    _queue_trusted = False
                    print("  this radio never reports its TX queue draining;"
                          " pacing on airtime alone from here")
                break
            time.sleep(poll)
        else:
            _queue_timeouts = 0        # it drained; the signal is real after all

    short_by = gap - (time.time() - start)
    if short_by > 0:
        time.sleep(short_by)
    return time.time() - start, timed_out


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
        link = meshtastic.serial_interface.SerialInterface(target)
    else:
        from meshtastic.ble_interface import BLEInterface
        link = BLEInterface(target)

    preset = read_preset(link)
    print("  radio is on %s: a full %d-byte packet is %.2f s of airtime, so "
          "packets go out %.1f s apart to stay under %d%% of the channel"
          % (preset, 200, airtime_seconds(200 + MESH_HEADER),
             gap_for(200), DUTY_CYCLE_PERCENT))
    return link


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

    if not chosen:
        return 0
    return send_packets(link, chosen, dest = dest, remember = False)


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

        # A radio that is sitting on undelivered packets will not be helped
        # by another one, and at zero free slots sendData() blocks forever
        # inside the library. Stop while the answer is still a message.
        if queue_backed_up(link):
            print("  STOPPED at %s: the radio is holding packets it has not "
                  "transmitted%s. The channel is saturated - see "
                  "DUTY_CYCLE_PERCENT and the node's modem preset."
                  % (label, describe_queue(link)))
            return position

        if dest:
            link.sendData(packet, destinationId = dest)
        else:
            link.sendData(packet)
        waited, timed_out = pace_after(link, len(packet))
        print("  sent      %s  waited %5.2f s%s%s"
              % (label, waited, describe_queue(link),
                 "   queue never reported empty" if timed_out else ""))

    return total
