"""Wire-format helpers for putting an email on the Meshtastic mesh.

A Meshtastic packet carries at most DATA_PAYLOAD_LEN bytes and the firmware
does not fragment for us, so every field has to be measured in UTF-8 bytes
rather than characters before it goes out.
"""

import re

# mesh_pb2.Constants.DATA_PAYLOAD_LEN
PACKET_BYTES = 237

# Header fields are delimited; the body is last so it needs no escaping.
FIELD_DELIM = "|"


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
