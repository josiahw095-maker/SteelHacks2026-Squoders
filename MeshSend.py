"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import math
import re
import struct
import threading
import time
from collections import OrderedDict

# Pause between packets when we cannot tell how fast the radio is. Sized for
# LONG_FAST, where one 200-byte packet is about 1.9 s on the air and the
# receiver's rebroadcast takes about as long again. At 2 s the next packet was
# queued while the last was still going out, and packets went missing; every
# hardware test at 5 s delivered everything.
SEND_GAP_SECONDS = 5.0


# The size the codec fills packets to (MeshCodec.MAX_PAYLOAD).
PACKET_BYTES = 200

# Meshtastic wraps our payload in a 16-byte mesh header plus a little protobuf.
FRAME_OVERHEAD = 22

# Pause = this many packet-airtimes. Measured, not derived: 5 s at LONG_FAST
# (1.9 s airtime, so 2.6x) delivered everything, and about 1x did not.
GAP_AIRTIMES = 2.6
MIN_GAP = 0.3

# (spread factor, bandwidth in Hz, coding-rate denominator) for each preset,
# from Meshtastic's modem preset table. Anything not listed falls back to
# SEND_GAP_SECONDS rather than guessing.
PRESETS = {
    "SHORT_TURBO":   (7, 500e3, 5),
    "SHORT_FAST":    (7, 250e3, 5),
    "SHORT_SLOW":    (8, 250e3, 5),
    "MEDIUM_FAST":   (9, 250e3, 5),
    "MEDIUM_SLOW":   (10, 250e3, 5),
    "LONG_FAST":     (11, 250e3, 5),
    "LONG_MODERATE": (11, 125e3, 8),
    "LONG_SLOW":     (12, 125e3, 8),
}


def airtime(frame_bytes, sf, bandwidth, cr = 5, preamble = 16):
    """Seconds one LoRa frame occupies the channel (the Semtech formula).

    cr is the coding-rate denominator, 5 for 4/5 up to 8 for 4/8. Meshtastic
    sends an explicit header with CRC on, and a 16-symbol preamble.
    """
    symbol = (2 ** sf) / bandwidth
    optimize = 1 if symbol > 0.016 else 0            # low-data-rate optimisation
    payload_symbols = 8 + max(math.ceil((8 * frame_bytes - 4 * sf + 28 + 16)
                                        / (4 * (sf - 2 * optimize))) * cr, 0)
    return (payload_symbols + preamble + 4.25) * symbol


def radio_params(link):
    """(sf, bandwidth, cr) the link's radio is set to, or None if we cannot tell."""
    try:
        lora = link.localNode.localConfig.lora
        if lora.use_preset:
            from meshtastic.protobuf import config_pb2
            name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
            return PRESETS.get(name)
        if lora.spread_factor and lora.bandwidth:
            return (lora.spread_factor, lora.bandwidth * 1000, lora.coding_rate or 5)
    except (AttributeError, ValueError):
        pass
    return None


def packet_airtime(link, size = PACKET_BYTES):
    """Seconds one of our packets takes on this link's radio, or None if unknown."""
    params = radio_params(link)
    return None if params is None else airtime(size + FRAME_OVERHEAD, *params)


def pace(link):
    """Seconds to wait between packets on this link.

    A link may carry its own answer in `gap_hint` (the mock radio does, since
    a folder needs no airtime). Otherwise the pause follows the radio's real
    airtime, so a fast preset is not held back by a gap sized for a slow one,
    and a slow preset is not hurried. If the radio cannot be read, the safe
    default applies.
    """
    hint = getattr(link, "gap_hint", None)
    if hint is not None:
        return hint
    seconds = packet_airtime(link)
    if seconds is None:
        return SEND_GAP_SECONDS
    return round(max(MIN_GAP, GAP_AIRTIMES * seconds), 1)


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


def resend(link, group, wanted, gap = None, dest = None, wait_ack = False):
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
        return send_packets(link, chosen, dest = dest, gap = gap, remember = False,
                            wait_ack = wait_ack)
    return 0


# A node heard this recently counts as "the other end". The NodeDB also holds
# every node ever heard on any channel (yours will list dozens from the public
# mesh), so age is what separates the radio on your desk from those.
PEER_MAX_AGE = 600

# A peer learned from a packet we received on our own channel is trusted longer.
LEARNED_MAX_AGE = 6 * 3600

# Set by the command line (--dest) to pin the peer and skip all guessing.
default_dest = None
_learned = {}

DEST_PATTERN = re.compile(r"^!?[0-9a-fA-F]{8}$")


def normalize_dest(dest):
    """'!435C4CE4' or '435c4ce4' -> '!435c4ce4'. Anything else is an error.

    Checked here because the Meshtastic library calls sys.exit() on some bad
    ids, which would take the whole gateway down with it.
    """
    text = str(dest or "").strip()
    if not DEST_PATTERN.match(text):
        raise ValueError(f"not a node id: {dest!r} (expected something like !435c4ce4)")
    return "!" + text.lstrip("!").lower()


def peers(link, max_age = None, now = None):
    """Other nodes this radio has heard, as [(id, name)], sorted by name.

    Meshtastic fills interface.nodes in as node-info packets arrive, so this
    grows over the first minute or two after connecting. Our own node is left
    out: there is no point addressing ourselves. With max_age, nodes not heard
    from within that many seconds are left out too.
    """
    nodes = getattr(link, "nodes", None) or {}
    now = time.time() if now is None else now
    mine = None
    try:
        mine = link.getMyNodeInfo()["user"]["id"]
    except Exception:
        pass

    found = []
    for node_id, node in nodes.items():
        if node_id == mine:
            continue
        if max_age is not None:
            heard = node.get("lastHeard")
            if not heard or now - heard > max_age:
                continue
        user = node.get("user", {}) or {}
        found.append((node_id, user.get("longName") or user.get("shortName") or node_id))
    return sorted(found, key=lambda pair: pair[1].lower())


def only_peer(link, max_age = PEER_MAX_AGE, now = None):
    """The single other node heard lately, when there is exactly one. Otherwise None.

    On a two-node mesh this is what "dynamic" means in practice: nobody has
    to type an id, and adding a third node makes the choice explicit rather
    than silently picking wrong.
    """
    found = peers(link, max_age = max_age, now = now)
    if len(found) != 1:
        return None
    try:
        return normalize_dest(found[0][0])
    except ValueError:
        return None


def note_peer(node_id, now = None):
    """Remember the node a real packet of ours just arrived from."""
    try:
        node_id = normalize_dest(node_id)
    except ValueError:
        return
    _learned.update(id = node_id, at = time.time() if now is None else now)


def forget_peer():
    _learned.clear()


def pick_dest(link, explicit = None, now = None):
    """Who to address packets to, or None to broadcast to the channel.

    In order: an id given outright, the id pinned with --dest, the node we last
    heard one of our own packets from, then the only other node in the NodeDB
    that was heard recently. Broadcasting makes every other node rebroadcast
    each packet, so it is only the fallback when none of those is known.
    """
    now = time.time() if now is None else now
    chosen = explicit or default_dest
    if chosen:
        return normalize_dest(chosen)
    if _learned and now - _learned["at"] <= LEARNED_MAX_AGE:
        return _learned["id"]
    return only_peer(link, now = now)


def _transmit(link, packet, dest, **extra):
    """One sendData call, addressed to dest or broadcast.

    The library calls sys.exit() on some bad input; that becomes an ordinary
    error here so a bad packet cannot end the whole program.
    """
    try:
        if dest:
            return link.sendData(packet, destinationId = dest, wantAck = True, **extra)
        return link.sendData(packet, wantAck = True, **extra)
    except SystemExit as error:
        raise RuntimeError(f"the radio refused the packet ({error})") from None


# --- sending on acknowledgment ------------------------------------------------
#
# A packet addressed to one node is acknowledged by that node's firmware, which
# also retries it on its own. Waiting for that ack instead of sleeping a fixed
# time means the next packet goes out the moment the channel is free, and a
# dead link is noticed within seconds rather than after the whole email.

# How long to wait for an ack, in packet-airtimes. Generous: the firmware
# retries a lost packet itself before it gives up, and that takes a few of them.
ACK_AIRTIMES = 10
MIN_ACK_WAIT = 4.0
UNKNOWN_ACK_WAIT = 20.0

# Attempts per packet of our own, on top of the firmware's retries.
ACK_TRIES = 2

# The pause after an ack before the next packet. The channel is already free.
ACK_GAP = 0.2


def ack_timeout(link):
    """Seconds to wait for one packet's ack on this link before trying again."""
    hint = getattr(link, "ack_hint", None)
    if hint is not None:
        return hint
    seconds = packet_airtime(link)
    if seconds is None:
        return UNKNOWN_ACK_WAIT
    return max(MIN_ACK_WAIT, ACK_AIRTIMES * seconds)


def _send_once(link, packet, dest, timeout):
    """Send one packet and wait for its answer.

    Returns "NONE" for an ack, the firmware's error name for a NAK (for
    example "MAX_RETRANSMIT"), or "TIMEOUT" if nothing came back in time.
    """
    answered = threading.Event()
    outcome = []

    def on_response(reply):
        # Called on the radio's own thread. Each attempt owns its event, so a
        # late answer to an earlier attempt cannot be mistaken for this one.
        routing = ((reply or {}).get("decoded") or {}).get("routing") or {}
        outcome.append(routing.get("errorReason", "NONE"))
        answered.set()

    _transmit(link, packet, dest, onResponse = on_response, onResponseAckPermitted = True)
    if not answered.wait(timeout):
        return "TIMEOUT"
    return outcome[0]


def _send_confirmed(link, packet, dest, tries = ACK_TRIES):
    """Send one packet until it is acknowledged or the tries run out."""
    outcome = "TIMEOUT"
    for _ in range(tries):
        outcome = _send_once(link, packet, dest, ack_timeout(link))
        if outcome == "NONE":
            return "NONE"
    return outcome


def send_packets(link, packets, dest=None, gap=None, remember=True, on_sent=None, wait_ack=False):
    """Send packets in order. Returns how many went out (or were acknowledged).

    dest of None broadcasts to the channel; otherwise it is a node id and only
    that node is addressed. A link of None prints instead of transmitting, so
    the whole pipeline can be exercised without hardware.

    wait_ack sends each packet only after the previous one was acknowledged,
    and gives up on the rest if one cannot be delivered. It needs a dest,
    because a broadcast has nobody to acknowledge it. Otherwise packets are
    spaced by gap, which None sets to whatever suits the link (see pace()).

    on_sent(position, total) is called after each packet that went out.
    """
    if dest:
        dest = normalize_dest(dest)
    confirm = bool(wait_ack and dest and link is not None)
    if gap is None:
        gap = 0 if link is None else (ACK_GAP if confirm else pace(link))

    if remember:
        remember_sent(packets)

    total = len(packets)
    done = 0
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.hex()))
        elif confirm:
            outcome = _send_confirmed(link, packet, dest)
            if outcome != "NONE":
                print("  FAILED    %s  %s; not sending the other %d"
                      % (label, outcome, total - position - 1))
                if _learned.get("id") == dest:
                    forget_peer()             # do not keep addressing a node that is not answering
                break
            print("  acked     %s  by %s" % (label, dest))
        else:
            _transmit(link, packet, dest)
            print("  sent      %s  %s" % (label, "to " + dest if dest else "(broadcast)"))

        done += 1
        if on_sent:
            on_sent(position + 1, total)
        if position < total - 1:
            time.sleep(gap)

    return done
