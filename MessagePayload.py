"""Wire format for carrying email over the Meshtastic mesh, both directions.

A Meshtastic packet carries at most DATA_PAYLOAD_LEN bytes and the firmware
does not fragment for us, so every field is measured in UTF-8 bytes rather
than characters before it goes out.

One frame serves both directions, so the framing exists in exactly one place:

    kind | id | thread | reply | index | total | sender | subject | body

kind is M for mail leaving the gateway and R for a reply coming back from the
endpoint. A reply leaves sender and subject empty: the gateway already holds
the address, the Message-ID and the thread, and builds the real email from
those at composition time. The body is last, so it never needs escaping.
"""

import re

# mesh_pb2.Constants.DATA_PAYLOAD_LEN: sendData() raises above this.
PACKET_BYTES = 233

# Header fields are delimited; the body is last so it needs no escaping.
FIELD_DELIM = "|"
FIELD_COUNT = 9

ID_CHARS = 4
SENDER_MAX = 24
SUBJECT_MAX = 48
INDEX_DIGITS = 2
MAX_PACKETS = 8

# Appended when a body is too long to fit in MAX_PACKETS.
TRUNCATED_MARK = " [...]"

# Which way the message is travelling.
KIND_MAIL = "M"
KIND_REPLY = "R"


def truncate_bytes(text, max_bytes):
    """Cut text to at most max_bytes of UTF-8, never splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text

    cut = max_bytes
    # Walk back off any continuation byte (0b10xxxxxx) so the cut lands on a
    # character boundary.
    while cut > 0 and raw[cut] & 0xC0 == 0x80:
        cut -= 1

    return raw[:cut].decode("utf-8", errors="ignore")


def clean_field(value, max_bytes):
    """Prepare a header field for the delimited part of the payload."""
    text = (value or "").replace(FIELD_DELIM, "/")
    text = re.sub(r"\s+", " ", text).strip()
    return truncate_bytes(text, max_bytes)


def short_id(message_id):
    """Last few chars of a Gmail id: enough to group the chunks of one message."""
    return (message_id or "")[-ID_CHARS:]


def payload_head(msg, index, total):
    """The delimited part that precedes the body in every packet."""
    return FIELD_DELIM.join([
        msg.get("kind", KIND_MAIL),
        short_id(msg.get("id")),
        short_id(msg.get("thread")),
        "1" if msg.get("reply") else "0",
        str(index).zfill(INDEX_DIGITS),
        str(total).zfill(INDEX_DIGITS),
        clean_field(msg.get("sender"), SENDER_MAX),
        clean_field(msg.get("subject"), SUBJECT_MAX),
    ]) + FIELD_DELIM


def build_payload(msg, body, index=0, total=1):
    """Pack one already-sliced body piece into a single mesh packet."""
    head = payload_head(msg, index, total)
    room = PACKET_BYTES - len(head.encode("utf-8"))
    return (head + truncate_bytes(body, room)).encode("utf-8")


def split_body(text, room):
    """Cut text into character-safe pieces of at most room bytes each.

    Anything past MAX_PACKETS is dropped, so the last piece is marked to stop
    a cut-off message looking complete at the far end.
    """
    pieces = []
    rest = text
    while rest and len(pieces) < MAX_PACKETS:
        piece = truncate_bytes(rest, room)
        pieces.append(piece)
        rest = rest[len(piece):]

    if rest:
        mark_bytes = len(TRUNCATED_MARK.encode("utf-8"))
        pieces[-1] = truncate_bytes(pieces[-1], room - mark_bytes) + TRUNCATED_MARK

    return pieces or [""]


def build_packets(msg):
    """One message -> the packets that carry it. Works in either direction."""
    room = PACKET_BYTES - len(payload_head(msg, 0, 0).encode("utf-8"))
    pieces = split_body((msg.get("body") or "").strip(), room)
    total = len(pieces)
    return [build_payload(msg, p, i, total) for i, p in enumerate(pieces)]


def reply_message(thread, body, in_reply_to=""):
    """A reply heading back to the gateway, shaped for build_packets()."""
    return {
        "kind": KIND_REPLY,
        "id": in_reply_to,
        "thread": thread,
        "reply": True,
        "sender": "",
        "subject": "",
        "body": body,
    }


def parse_packet(packet):
    """One packet -> its fields. The exact inverse of build_payload().

    maxsplit stops at the body, so pipes inside the body survive untouched.
    """
    text = packet.decode("utf-8") if isinstance(packet, bytes) else packet
    fields = text.split(FIELD_DELIM, FIELD_COUNT - 1)
    if len(fields) != FIELD_COUNT:
        raise ValueError("packet has %d fields, expected %d"
                         % (len(fields), FIELD_COUNT))

    kind, ident, thread, reply, index, total, sender, subject, body = fields
    return {
        "kind": kind,
        "id": ident,
        "thread": thread,
        "reply": reply == "1",
        "index": int(index),
        "total": int(total),
        "sender": sender,
        "subject": subject,
        "body": body,
    }


def reassemble(packets):
    """The packets of one message -> the message, with any gaps reported.

    Bodies are joined with no separator and no stripping: the whitespace at
    the seams is real content.
    """
    parsed = sorted((parse_packet(p) for p in packets), key=lambda f: f["index"])
    if not parsed:
        raise ValueError("no packets to reassemble")

    expected = set(range(parsed[0]["total"]))
    seen = {f["index"] for f in parsed}

    message = dict(parsed[0])
    message.pop("index", None)
    message["body"] = "".join(f["body"] for f in parsed)
    message["complete"] = seen == expected
    message["missing"] = sorted(expected - seen)
    return message
