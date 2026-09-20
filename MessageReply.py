"""Receive mail composed on the endpoint and send it through Gmail.

Handles both kinds the endpoint can send: a reply to a thread the gateway
remembers, and a brand new email that carries its own recipient.

    endpoint app --reply_packets--> LoRa --> gateway node
                 --Collect--> reassemble --> queue --> SendReply --> Gmail

The radio delivers packets on the meshtastic library's own thread, while
MessagePing is blocked in its poll loop on the main thread. So Collect() only
parks finished replies on a queue, and the poll loop calls Drain() to pick
them up. Nothing touches Gmail from the radio thread.
"""

import base64
import threading
import time
from email.message import EmailMessage

import MeshCodec
import SentLog

# A half-received reply is forgotten after this long, so a lost packet does
# not pin its siblings in memory forever.
GROUP_TIMEOUT = 300

# Packets waiting to be completed, keyed by the 2-byte chunk id. Written from
# the radio thread and swept from the main one, so it needs the lock.
_groups = {}
_groups_lock = threading.Lock()

# A repeat of a request we already served this recently is ignored. The
# endpoint asks again when a message has been quiet for a while, and its timer
# can run out while our resend is still on the air - on 2026-09-20 that made
# the gateway send the same four parts twice, 82 s of airtime that delivered
# nothing. Serving the first request is the answer to both.
#
# This has to cover one resend on the air (about 13 s for a full six parts at
# LONG_FAST) and no more. If the resend is genuinely lost the endpoint asks
# again, and by then its backoff has pushed the second ask well past this, so
# the repeat that matters is still served.
RESEND_DEBOUNCE = 20
_resent = {}

# Finished replies waiting for the poll loop. Queue is already thread-safe.
_replies = []
_replies_lock = threading.Lock()


# --- receiving --------------------------------------------------------------

def Collect(payload):
    """Add one received packet. Returns the reply if that completed one.

    Kept separate from OnReceive so the whole path can be tested without a
    radio: hand it the bytes reply_packets() produced.
    """
    key = bytes(payload[:2])

    with _groups_lock:
        group = _groups.setdefault(key, {"packets": [], "seen": 0.0})
        group["packets"].append(bytes(payload))
        group["seen"] = time.time()
        data = MeshCodec.reassemble(group["packets"])
        if data is None:
            return None            # still missing packets, or they disagree
        del _groups[key]

    try:
        message = MeshCodec.decode_message(data)
    except Exception as error:                 # corrupt, wrong DICT, bad UTF-8
        print(f"  dropped an undecodable message: {type(error).__name__}: {error}")
        return None

    if not message["outbound"]:
        return None                # our own mail heard back; not a reply

    with _replies_lock:
        _replies.append(message)
    return message


def OnReceive(packet, interface = None):
    """pubsub callback. Ignores anything that is not one of our packets."""
    decoded = packet.get("decoded", {})
    if decoded.get("portnum") != "PRIVATE_APP":
        return
    payload = decoded.get("payload")
    if payload and Collect(payload) is not None:
        # A whole valid message from the endpoint: now we know who it is, so
        # our packets can go to it directly instead of to everyone.
        from MeshSend import note_peer
        note_peer(packet.get("fromId"))


def Listen():
    """Start receiving. The radio interface must already be open."""
    from pubsub import pub
    pub.subscribe(OnReceive, "meshtastic.receive")


def Drain():
    """Every reply that has arrived since the last call."""
    with _replies_lock:
        out = list(_replies)
        _replies.clear()
    return out


def Expire(now = None):
    """Forget half-received replies that will never complete."""
    now = now if now is not None else time.time()
    with _groups_lock:
        stale = [k for k, g in _groups.items() if now - g["seen"] > GROUP_TIMEOUT]
        for key in stale:
            del _groups[key]
    return len(stale)


# --- sending it on as real email --------------------------------------------

def BuildReply(original, body):
    """An RFC 2822 reply to `original`, ready for the Gmail API.

    threadId alone puts it in the right Gmail conversation, but In-Reply-To
    and References are what make every other mail client thread it too, so
    both are set.
    """
    mail = EmailMessage()
    mail["To"] = original["address"]

    subject = original.get("subject") or ""
    mail["Subject"] = subject if subject[:3].lower() == "re:" else f"Re: {subject}"

    if original.get("rfc_id"):
        mail["In-Reply-To"] = original["rfc_id"]
        mail["References"] = original["rfc_id"]

    mail.set_content(body)
    return mail


def SendReply(service, account, message, dry_run = False):
    """Send one decoded reply as real Gmail. Returns the new message id."""
    original = SentLog.Lookup(account, message["thread"])
    if original is None:
        print(f"  reply for unknown thread {message['thread']}; dropped")
        return None
    if not original.get("address"):
        print(f"  no address recorded for thread {message['thread']}; dropped")
        return None

    mail = BuildReply(original, message["body"])

    if dry_run:
        print(f"  [dry-run] would reply to {mail['To']} / {mail['Subject']!r}")
        print(f"  [dry-run] in-reply-to {original.get('rfc_id')!r} thread {original['thread']}")
        return None

    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
    sent = service.users().messages().send(
        userId = "me",
        body = {"raw": raw, "threadId": original["thread"]},
    ).execute()

    print(f"  replied to {mail['To']} in thread {original['thread']} (id {sent.get('id')})")
    return sent.get("id")


def BuildNew(address, subject, body):
    """A brand new email, with no thread to attach it to."""
    mail = EmailMessage()
    mail["To"] = address
    mail["Subject"] = subject
    mail.set_content(body)
    return mail


def SendNew(service, message, dry_run = False):
    """Send a composed email. The recipient rides in the sender slot."""
    address = (message.get("sender") or "").strip()
    if "@" not in address:
        print(f"  composed mail has no usable recipient ({address!r}); dropped")
        return None

    mail = BuildNew(address, message.get("subject") or "(no subject)", message["body"])

    if dry_run:
        print(f"  [dry-run] would send a NEW email to {mail['To']} / {mail['Subject']!r}")
        return None

    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
    sent = service.users().messages().send(
        userId = "me", body = {"raw": raw}).execute()

    print(f"  sent a new email to {mail['To']} (id {sent.get('id')})")
    return sent.get("id")


def Resend(link, message):
    """Put back on the air the parts a receiver says it never got."""
    from MeshCodec import wanted_parts
    from MeshSend import resend, pick_dest

    wanted = wanted_parts(message)
    if not wanted:
        return None
    if link is None:
        print(f"  [dry-run] would resend parts {wanted} of 0x{message['thread']:04x}")
        return None

    now = time.time()
    key = (message["thread"], tuple(wanted))
    for stale in [k for k, when in _resent.items() if now - when > RESEND_DEBOUNCE]:
        del _resent[stale]
    if key in _resent:
        print(f"  already resending parts {wanted} of 0x{message['thread']:04x}; ignoring the repeat")
        return 0
    _resent[key] = now

    dest = pick_dest(link)
    count = resend(link, message["thread"], wanted, dest = dest, wait_ack = bool(dest))
    if count:
        print(f"  resent {count} packet(s) of 0x{message['thread']:04x}")
    else:
        print(f"  asked for 0x{message['thread']:04x} but it is no longer cached")
    return count


def _clock(moment):
    from MeshSend import stamp
    return stamp(moment) if moment else "-"


def _seconds(value):
    return "%.3f" % value if value is not None else "-"


# One row of the timing table. Every cell is a single token, so a row can be read
# back with split() and the columns stay lined up whatever is missing.
TIMING_ROW = "{:>5}  {:>12}  {:>12}  {:>8}  {:>12}  {:>7}  {:>7}  {:>8}{}"


def ShowTiming(message):
    """Print the endpoint's arrival times beside our own send and ack times.

    The two machines' clocks may not agree, so the gap columns are the ones to
    trust: time between our sends, time between their arrivals. The last column
    is a one-way delay only if both clocks are synced.

    Returns a summary dict (also handy for tests), or None if unreadable.
    """
    import MeshSend
    parsed = MeshCodec.parse_timing(message)
    if parsed is None:
        print("  timing report could not be read")
        return None
    first_ms, offsets = parsed
    group = message["thread"]
    ours = {e["part"]: e for e in (MeshSend.timeline_for(group) or [])}
    if not ours:
        print("  (no send record for that message: it was sent before this run)")

    print(f"  timing for message {group:04x}")
    print(TIMING_ROW.format("part", "sent", "ack in", "ack wait", "received", "gap tx", "gap rx", "tx->rx*", ""))
    print(TIMING_ROW.format("", "(gateway)", "(gateway)", "", "(endpoint)", "", "", "", ""))
    prev_sent = prev_rx = None
    waits, tx_gaps, rx_gaps = [], [], []
    for part in sorted(set(ours) | set(offsets)):
        entry = ours.get(part, {})
        sent, acked = entry.get("sent_at"), entry.get("acked_at")
        rx = (first_ms + offsets[part]) / 1000 if part in offsets else None
        wait = acked - sent if sent and acked else None
        tx_gap = sent - prev_sent if sent and prev_sent else None
        rx_gap = rx - prev_rx if rx and prev_rx else None
        transit = rx - sent if rx and sent else None
        for value, bucket in ((wait, waits), (tx_gap, tx_gaps), (rx_gap, rx_gaps)):
            if value is not None:
                bucket.append(value)
        note = "" if entry.get("attempts", 1) <= 1 else f"  ({entry['attempts']} tries)"
        print(TIMING_ROW.format(part + 1, _clock(sent), _clock(acked), _seconds(wait), _clock(rx),
                                _seconds(tx_gap), _seconds(rx_gap), _seconds(transit), note))
        prev_sent, prev_rx = sent or prev_sent, rx or prev_rx

    print("  * one-way delay, valid only if both machines' clocks are in sync")
    summary = {"ack_wait_max": max(waits, default = None), "ack_wait_avg": (sum(waits) / len(waits)) if waits else None,
               "tx_gap_max": max(tx_gaps, default = None), "rx_gap_max": max(rx_gaps, default = None),
               "parts_reported": len(offsets), "parts_sent": len(ours)}
    if waits:
        print("   ack wait: average %.3f s, longest %.3f s" % (summary["ack_wait_avg"], summary["ack_wait_max"]))
    if rx_gaps:
        print("   longest silence the endpoint saw between packets: %.3f s" % summary["rx_gap_max"])
    return summary


def Deliver(service, account, message, dry_run = False, link = None):
    """Act on one decoded outbound message, whichever kind it is.

    TIMING is a diagnostic report, not mail. REQUEST is a plea to resend
    packets. OUTBOUND with REPLY set is a reply to a thread we remember;
    OUTBOUND on its own is a new email that carries its own recipient.
    """
    if not message.get("outbound"):
        return None
    if message.get("timing"):
        return ShowTiming(message)
    if message.get("request"):
        return Resend(link, message)
    if message.get("reply"):
        return SendReply(service, account, message, dry_run)
    return SendNew(service, message, dry_run)
