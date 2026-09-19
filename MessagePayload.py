"""Wire-format helpers for putting an email on the Meshtastic mesh.

A Meshtastic packet carries at most DATA_PAYLOAD_LEN bytes and the firmware
does not fragment for us, so every field has to be measured in UTF-8 bytes
rather than characters before it goes out.
"""

import re

# mesh_pb2.Constants.DATA_PAYLOAD_LEN: sendData() raises above this.
PACKET_BYTES = 233

# Header fields are delimited; the body is last so it needs no escaping.
FIELD_DELIM = "|"
ID_CHARS = 4
SENDER_MAX = 24
SUBJECT_MAX = 48
INDEX_DIGITS = 2
MAX_PACKETS = 8


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
    return (message_id or "")[-ID_CHARS:]

def payload_head(msg, index, total):
    """The delimited part that precedes the body in every packet."""
    return FIELD_DELIM.join([
        short_id(msg.get("id")),
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
    """Cut text into character-safe pieces of at most room bytes each."""
    pieces = []
    rest = text
    while rest and len(pieces) < MAX_PACKETS:
        piece = truncate_bytes(rest, room)
        pieces.append(piece)
        rest = rest[len(piece):]
    return pieces or [""]

def build_packets(msg):
    """One email -> the list of packets that carry it."""
    room = PACKET_BYTES - len(payload_head(msg, 0, 0).encode("utf-8"))
    pieces = split_body((msg.get("body") or "").strip(), room)
    total = len(pieces)
    return [build_payload(msg, p, i, total) for i, p in enumerate(pieces)]

