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
    if payload:
        Collect(payload)


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


def Deliver(service, account, message, dry_run = False):
    """Act on one decoded outbound message, whichever kind it is.

    OUTBOUND with REPLY set is a reply to a thread we remember; OUTBOUND on
    its own is a new email that carries its own recipient.
    """
    if not message.get("outbound"):
        return None
    if message.get("reply"):
        return SendReply(service, account, message, dry_run)
    return SendNew(service, message, dry_run)
