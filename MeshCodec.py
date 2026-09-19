"""Turns a parsed email into small binary packets for the mesh, and back.

    Gmail JSON --MessageTransform.transform--> email dict
               --to_packets-->  [bytes, ...]  --(radio)-->
               --reassemble + decode_message-->  (sender, subject, minutes, body)

The packets are raw bytes, not text. Send them with the Meshtastic library's
sendData on the private port (PortNum.PRIVATE_APP, which is its default), not
sendText. The stock Meshtastic app does not understand them. That is obscurity,
not security: the channel key is what protects them (see provision.py).

Message (the bytes that get chunked):

    <flag: 1 byte> <rest>

    flag    0 = rest is the raw record, 1 = rest is the record after raw deflate
            using DICT. pack() keeps whichever is smaller. The upper 7 bits are
            reserved (use them for a format version if this changes).

    record  <time: 4 bytes, big-endian uint32, UTC minutes since the Unix epoch>
            <sender> "\\n" <subject> "\\n" <body>          (all UTF-8)

Sender and subject never contain "\\n", so the receiver parses the text with
split(b"\\n", 2). The body comes last and may contain newlines.

Chunk (each mesh packet, at most MAX_PAYLOAD bytes):

    <id: 2 bytes> <part<<4 | total: 1 byte> <slice of the message>

    id      crc32 of the Gmail message id, low 16 bits; groups the packets
    part    0-based index of this packet
    total   number of packets (1..15)

Text is shortened BEFORE it is compressed, and the message is compressed BEFORE it
is split. Dropping packets from the end of a compressed stream would make it
undecodable, so encode() trims the body until everything fits in MAX_CHUNKS packets.

DICT, MAX_SENDER and MAX_SUBJECT are part of the format. The endpoint app must
embed the identical DICT bytes; changing it breaks every deployed app.
"""
import re
import struct
import zlib

from MessagePayload import truncate_bytes

MAX_PAYLOAD = 200   # bytes per mesh packet, conservative (the firmware limit is ~233)
MAX_CHUNKS = 3      # the body is trimmed until the message fits in this many packets
MAX_SENDER = 20     # bytes
MAX_SUBJECT = 40    # bytes
HEADER = 3          # bytes of chunk header (id + part/total)
ELLIPSIS = "…"  # 3 bytes in UTF-8, marks text that was cut

FLAG_RAW, FLAG_DEFLATE = 0, 1

# Shared compression dictionary. Deflate favors the END of the dictionary, so
# the most common material goes last. Replace with phrases mined from real mail.
DICT = (
    b"Wednesday Thursday Friday Saturday Sunday Monday Tuesday tomorrow yesterday "
    b"planning summary review update project team notes shared folder meeting call "
    b"Best regards, Kind regards, Sincerely, Cheers, Regards, Hi all, Hello, "
    b"Please let me know if you have any questions. Let me know if that works. "
    b"Sounds good. See you there. Looking forward to it. Thanks for your help. "
    b"Re: Fwd: Hey  Hi  the of and to a in is for that with this on you I we are be "
    b"have will can by at from it as not your [link]\nThanks,\nThank you!\nThanks!\n"
)


# --- Shrinking the text ------------------------------------------------------

def one_line(text):
    return " ".join(text.split())


def shorten(text, limit):
    """Cut to `limit` UTF-8 bytes and mark the cut with an ellipsis."""
    if len(text.encode("utf-8")) <= limit:
        return text
    return truncate_bytes(text, limit - len(ELLIPSIS.encode())).rstrip() + ELLIPSIS


def strip_body(body):
    """Drop everything that is not the new content of the email."""
    lines = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith(">"):                              # quoted reply
            continue
        if re.match(r"On .+ wrote:\s*$", line) or line.rstrip() == "--":
            break                                             # reply header / signature
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"https?://\S+", "[link]", text)            # URLs are expensive
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()

    # TODO: "Sent from my iPhone", unsubscribe/legal footers, abbreviation
    # dictionary, optional LLM summary (only if it beats plain truncation).


# --- Packing, compressing and splitting -------------------------------------

def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY, zdict=DICT)
    return c.compress(data) + c.flush()


def inflate(data):
    d = zlib.decompressobj(-15, zdict=DICT)
    return d.decompress(data) + d.flush()


def pack(sender, subject, minutes, body):
    """flag byte + (raw or deflated) record; whichever is smaller."""
    record = struct.pack(">I", minutes) + b"\n".join(
        (sender.encode(), subject.encode(), body.encode()))
    packed = deflate(record)
    if len(packed) < len(record):
        return bytes([FLAG_DEFLATE]) + packed
    return bytes([FLAG_RAW]) + record


def encode(sender, subject, minutes, body):
    """Pack, trimming the body (marked with an ellipsis) until the result fits
    in MAX_CHUNKS packets. Trimming happens on the text, before compression."""
    limit = MAX_CHUNKS * (MAX_PAYLOAD - HEADER)
    while True:
        data = pack(sender, subject, minutes, body)
        if len(data) <= limit or not body:
            return data
        size = len(body.encode())
        body = shorten(body, size * 9 // 10) if size > 12 else ""


def chunk(email_id, data):
    """Split into packets of at most MAX_PAYLOAD bytes, each with the 3-byte
    header described at the top of the file."""
    room = MAX_PAYLOAD - HEADER
    pieces = [data[i:i + room] for i in range(0, len(data), room)]
    mid = zlib.crc32(email_id.encode()) & 0xFFFF
    return [struct.pack(">HB", mid, i << 4 | len(pieces)) + piece
            for i, piece in enumerate(pieces)]


def to_packets(email):
    """The dict MessageTransform.transform() returns -> list of bytes, one per packet."""
    sender = shorten(one_line(email["sender"]), MAX_SENDER)
    subject = shorten(one_line(email["subject"]), MAX_SUBJECT)
    minutes = email["date"] // 60
    return chunk(email["id"], encode(sender, subject, minutes, strip_body(email["body"])))


# --- Inverse: what the endpoint app has to do (kept here as the reference) ---

def reassemble(packets):
    """Packets of ONE email, any order -> the message bytes, or None if any
    packet is missing. The app should group packets by their 2-byte id first."""
    parts, total = {}, None
    for packet in packets:
        _, pt = struct.unpack(">HB", packet[:HEADER])
        parts[pt >> 4] = packet[HEADER:]
        total = pt & 0x0F
    if total is None or len(parts) != total:
        return None
    return b"".join(parts[i] for i in range(total))


def decode_message(data):
    """Message bytes -> (sender, subject, minutes, body)."""
    record = inflate(data[1:]) if data[0] == FLAG_DEFLATE else data[1:]
    (minutes,) = struct.unpack(">I", record[:4])
    sender, subject, body = record[4:].split(b"\n", 2)
    return sender.decode(), subject.decode(), minutes, body.decode()


if __name__ == "__main__":
    # Smoke test with a fake parsed email, including a round trip.
    fake = {
        "id": "18c0ffee1234abcd",
        "date": 1790000000,
        "sender": "Ada Lovelace",
        "subject": "Lunch?",
        "body": "Hi team,\r\nLunch at noon? See https://example.com/menu\r\n\r\n-- \r\nAda\r\n",
    }
    packets = to_packets(fake)
    for packet in packets:
        print(len(packet), packet.hex())
    print(decode_message(reassemble(reversed(packets))))
