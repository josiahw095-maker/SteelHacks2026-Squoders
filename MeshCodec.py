"""Turns a parsed email into small binary packets for the mesh, and back.

    Gmail JSON --MessageTransform.transform--> email dict
               --to_packets-->  [bytes, ...]  --(radio)-->
               --reassemble + decode_message-->  dict

A reply travels the other way with reply_packets(); both directions share one
record layout, so the endpoint app implements the codec only once.

The packets are raw bytes, not text. Send them with the Meshtastic library's
sendData on the private port (PortNum.PRIVATE_APP, which is its default), not
sendText. The stock Meshtastic app does not understand them. That is obscurity,
not security: the channel key is what protects them (see provision.py).

Message (the bytes that get chunked):

    <flag: 1 byte> <rest>

    bit 0  DEFLATE   rest is the record after raw deflate using DICT; pack()
                     sets it only when that is smaller than the raw record
    bit 1  REPLY     this email is itself a reply (its In-Reply-To was set)
    bit 2  OUTBOUND  endpoint -> gateway (a reply being sent) rather than
                     gateway -> endpoint
    bit 3  REQUEST   not mail at all: a plea to resend the parts listed in
                     the body, for the message named in the thread field
    bits 4-7         reserved (use them for a format version if this changes)

    record  <time:   4 bytes, big-endian uint32, UTC minutes since the epoch>
            <thread: 2 bytes, big-endian uint16, crc32 of the Gmail threadId>
            <sender> "\n" <subject> "\n" <body>          (all UTF-8)

Sender and subject never contain "\n", so the receiver parses the text with
split(b"\n", 2). The body comes last and may contain newlines. An outbound
reply leaves sender and subject empty: the gateway already holds the address,
the Message-ID and the thread, and builds the real email from those.

For OUTBOUND messages the sender slot means "the other party", so a new email
(OUTBOUND set, REPLY clear) carries its RECIPIENT there and its subject in the
subject slot. OUTBOUND with REPLY set is a reply and uses thread instead.

thread groups a conversation; the 2-byte id in the chunk header groups the
packets of one message. For the first mail in a thread the two are equal.

Chunk (each mesh packet, at most MAX_PAYLOAD bytes):

    <id: 2 bytes> <part<<4 | total: 1 byte> <slice of the message>

    id      crc32 of the Gmail message id, low 16 bits; groups the packets
    part    0-based INDEX of this packet   (0 .. total-1)
    total   1-based COUNT of packets        (1 .. MAX_PARTS)
            The two use different conventions but share one byte, so
            packet 2 of 3 is part=1, total=3, i.e. the byte 0x13.

Text is shortened BEFORE it is compressed, and the message is compressed BEFORE
it is split. Dropping packets from the end of a compressed stream would make it
undecodable, so encode() trims the body until everything fits in MAX_CHUNKS.

DICT, MAX_SENDER and MAX_SUBJECT are part of the format. The endpoint app must
embed the identical DICT bytes; changing it breaks every deployed app.
"""
import re
import struct
import time
import zlib

MAX_PAYLOAD = 200   # bytes per mesh packet, conservative (the firmware limit is 233)
MAX_SENDER = 20     # bytes
MAX_SUBJECT = 40    # bytes
MAX_ADDRESS = 100   # bytes; a recipient address is never shortened
HEADER = 3          # bytes of chunk header (id + part/total)
MAX_PARTS = 15      # part and total share one byte, 4 bits each: the header's hard limit
MAX_CHUNKS = 6      # the body is trimmed until the email fits in this many packets.
                    # Each packet is ~2 s on the air at LONG_FAST plus the pause
                    # between them, so 6 is about 15 s (with acks) and 15 would
                    # hold the radio for over a minute.
MAX_EXPANSION = 50  # text this much larger than the budget is cut before it is
                    # even compressed, so a huge email costs milliseconds, not
                    # seconds. Ordinary text shrinks 2-3x; only pathological
                    # repetition beats 50x, and that is what this still allows.
RECORD_HEAD = 6     # bytes of record header (4 time + 2 thread)
ELLIPSIS = "…"  # 3 bytes in UTF-8, marks text that was cut

FLAG_DEFLATE = 0x01
FLAG_REPLY = 0x02
FLAG_OUTBOUND = 0x04
FLAG_REQUEST = 0x08

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


# --- Byte-safe text helpers (merged in from the old MessagePayload.py) -------

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


def short_hash(text):
    """The 16-bit id used for both the chunk group and the thread."""
    return zlib.crc32((text or "").encode()) & 0xFFFF


def one_line(text):
    return " ".join(text.split())


def shorten(text, limit):
    """Cut to `limit` UTF-8 bytes and mark the cut with an ellipsis."""
    if len(text.encode("utf-8")) <= limit:
        return text
    return truncate_bytes(text, limit - len(ELLIPSIS.encode())).rstrip() + ELLIPSIS


# --- Shrinking the text ------------------------------------------------------

# Everything from the first marker to the end of the body is reply history or a
# signature, so we cut there rather than filtering line by line.
QUOTE_MARKERS = [
    re.compile(r"^>"),                                          # quoted line
    re.compile(r"\bwrote:\s*$"),                                # "... Alice <a@x> wrote:"
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I),  # Outlook
    re.compile(r"^\s*From:\s+\S"),                              # Outlook header block
    re.compile(r"^\s*_{5,}\s*$"),                               # Outlook rule
    re.compile(r"^--\s*$"),                                     # signature delimiter
    re.compile(r"^\s*Sent from my \w+", re.I),                  # mobile signature
    re.compile(r"^\s*Unsubscribe\b", re.I),                     # bulk mail footer
]

# Gmail wraps "On <date> <name> wrote:" across two lines, so a match on the
# second half would leave the first half behind. These back up to its start.
ATTRIBUTION_OPEN = re.compile(r"^\s*On\b")
WROTE_END = re.compile(r"\bwrote:\s*$")


def quote_cut(lines):
    """Index of the first line of reply history, or None if there is none."""
    cut = None
    for index, line in enumerate(lines):
        if any(marker.search(line) for marker in QUOTE_MARKERS):
            cut = index
            break

    if cut is None:
        return None

    if WROTE_END.search(lines[cut]) and not ATTRIBUTION_OPEN.search(lines[cut]):
        for back in range(cut - 1, max(-1, cut - 3), -1):
            if ATTRIBUTION_OPEN.search(lines[back]):
                cut = back
                break

    return cut


def tidy(lines):
    """Collapse the whitespace and spend no bytes on full URLs."""
    text = "\n".join(lines)
    text = re.sub(r"https?://\S+", "[link]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def strip_body(body):
    """Drop everything that is not the new content of the email.

    Falls back to the whole body when stripping would leave nothing, so a
    forward-only mail still carries something.
    """
    lines = body.replace("\r\n", "\n").split("\n")
    cut = quote_cut(lines)
    if cut is None:
        return tidy(lines)
    return tidy(lines[:cut]) or tidy(lines)


# --- Packing, compressing and splitting -------------------------------------

def deflate(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15, 9, zlib.Z_DEFAULT_STRATEGY, zdict=DICT)
    return c.compress(data) + c.flush()


def inflate(data):
    d = zlib.decompressobj(-15, zdict=DICT)
    return d.decompress(data) + d.flush()


def pack(sender, subject, minutes, thread, body, flags):
    """flag byte + (raw or deflated) record; whichever is smaller.

    sender and subject go through one_line() here rather than at the call
    site: a newline in either would move the split in decode_message() and
    silently corrupt both the subject and the body.
    """
    record = struct.pack(">IH", minutes, thread) + b"\n".join(
        (one_line(sender).encode(), one_line(subject).encode(), body.encode()))
    packed = deflate(record)
    if len(packed) < len(record):
        return bytes([flags | FLAG_DEFLATE]) + packed
    return bytes([flags]) + record


def encode(sender, subject, minutes, thread, body, flags):
    """Pack, trimming the body (marked with an ellipsis) until the result fits
    in MAX_CHUNKS packets. Trimming happens on the text, before compression.

    When it does not fit, the longest body that does is found by bisection, so
    an email only just over the limit loses only what it must. (Shaving 10% at
    a time threw away up to a tenth of the text, about 400 bytes, for nothing.)
    """
    limit = MAX_CHUNKS * (MAX_PAYLOAD - HEADER)

    size = len(body.encode())
    if size > limit * MAX_EXPANSION:
        body = shorten(body, limit * MAX_EXPANSION)
        size = len(body.encode())

    data = pack(sender, subject, minutes, thread, body, flags)
    if len(data) <= limit or not body:
        return data

    best, low, high = pack(sender, subject, minutes, thread, "", flags), 0, size
    while high - low > 1:                          # low fits, high does not
        middle = (low + high) // 2
        kept = shorten(body, middle) if middle > len(ELLIPSIS.encode()) else ""
        candidate = pack(sender, subject, minutes, thread, kept, flags)
        if len(candidate) <= limit:
            low, best = middle, candidate
        else:
            high = middle
    return best


def chunk(group_id, data):
    """Split into packets of at most MAX_PAYLOAD bytes, each with the 3-byte
    header described at the top of the file."""
    room = MAX_PAYLOAD - HEADER
    pieces = [data[i:i + room] for i in range(0, len(data), room)]
    if len(pieces) > MAX_PARTS:
        raise ValueError("%d pieces, but the header holds at most %d"
                         % (len(pieces), MAX_PARTS))
    mid = short_hash(group_id)
    return [struct.pack(">HB", mid, i << 4 | len(pieces)) + piece
            for i, piece in enumerate(pieces)]


def to_packets(email):
    """The dict MessageTransform.transform() returns -> one packet per chunk."""
    sender = shorten(one_line(email["sender"]), MAX_SENDER)
    subject = shorten(one_line(email["subject"]), MAX_SUBJECT)
    thread = short_hash(email.get("thread") or email["id"])
    flags = FLAG_REPLY if email.get("reply") else 0
    data = encode(sender, subject, email["date"] // 60, thread,
                  strip_body(email["body"]), flags)
    return chunk(email["id"], data)


def reply_packets(thread_id, body, message_id=""):
    """A reply heading endpoint -> gateway, in the same format.

    thread_id is either the Gmail threadId, which the gateway has, or the
    16-bit hash of it, which is all the endpoint ever sees. Sender and
    subject are left empty: the gateway looks the original message up by
    thread and builds the real email, quoting included, when it sends.
    """
    thread = thread_id if isinstance(thread_id, int) else short_hash(thread_id)
    data = encode("", "", int(time.time()) // 60, thread, one_line(body),
                  FLAG_OUTBOUND | FLAG_REPLY)
    return chunk(str(message_id or thread_id), data)


def compose_packets(address, subject, body):
    """A brand new email heading endpoint -> gateway.

    OUTBOUND without REPLY means "compose", so the gateway reads the
    recipient out of the sender slot instead of looking a thread up. The
    address is not run through shorten(): a truncated address is not an
    address, so it is capped at MAX_ADDRESS and otherwise left whole.
    """
    minutes = int(time.time()) // 60
    address = truncate_bytes(one_line(address), MAX_ADDRESS)
    subject = shorten(one_line(subject), MAX_SUBJECT)
    data = encode(address, subject, minutes, 0, one_line(body), FLAG_OUTBOUND)
    return chunk("%s|%s|%d" % (address, subject, minutes), data)


def missing_parts(packets):
    """Which part indices are absent from the packets in hand.

    [] means the set is complete. None means the packets disagree about how
    many there should be, so there is nothing sensible to ask for.
    """
    parts, total = set(), None
    for packet in packets:
        _, pt = struct.unpack(">HB", packet[:HEADER])
        part, claimed = pt >> 4, pt & 0x0F
        if total is None:
            total = claimed
        elif claimed != total:
            return None
        parts.add(part)

    if total is None:
        return None
    return sorted(set(range(total)) - parts)


def request_packets(group_id, wanted):
    """Ask the far side to resend particular parts of one message.

    The message being chased rides in the thread field, which is the same
    two bytes as the chunk id, and the wanted parts travel as "0,3,7" in the
    body. A request is always one packet, so it cannot itself go missing in
    pieces. Its own chunk id is derived separately so it never collides with
    the message it is asking about.
    """
    group = group_id if isinstance(group_id, int) else short_hash(group_id)
    data = encode("", "", int(time.time()) // 60, group,
                  ",".join(str(part) for part in wanted),
                  FLAG_OUTBOUND | FLAG_REQUEST)
    return chunk("request-%d" % group, data)


def wanted_parts(message):
    """The part indices a REQUEST message is asking for."""
    if not message.get("request"):
        return []
    out = []
    for piece in (message.get("body") or "").split(","):
        piece = piece.strip()
        if piece.isdigit():
            out.append(int(piece))
    return out


# --- Inverse: what the endpoint app has to do (kept here as the reference) ---

def reassemble(packets):
    """Packets of ONE message, any order -> the message bytes, or None.

    None means the set is not usable: a packet is missing, the packets
    disagree about how many there are, or one claims a part outside that
    range. Disagreement means at least one packet is corrupt, so we refuse
    rather than guess. The app should group packets by their 2-byte id first.
    """
    parts, total = {}, None
    for packet in packets:
        _, pt = struct.unpack(">HB", packet[:HEADER])
        part, claimed = pt >> 4, pt & 0x0F

        if total is None:
            total = claimed
        elif claimed != total:
            return None
        if part >= total:
            return None

        parts[part] = packet[HEADER:]

    if total is None or len(parts) != total:
        return None
    return b"".join(parts[i] for i in range(total))


def decode_message(data):
    """Message bytes -> the fields, whichever direction they travelled."""
    flags = data[0]
    record = inflate(data[1:]) if flags & FLAG_DEFLATE else data[1:]
    minutes, thread = struct.unpack(">IH", record[:RECORD_HEAD])
    sender, subject, body = record[RECORD_HEAD:].split(b"\n", 2)
    return {
        "minutes": minutes,
        "thread": thread,
        "reply": bool(flags & FLAG_REPLY),
        "outbound": bool(flags & FLAG_OUTBOUND),
        "request": bool(flags & FLAG_REQUEST),
        "sender": sender.decode(),
        "subject": subject.decode(),
        "body": body.decode(),
    }


if __name__ == "__main__":
    # Smoke test with a fake parsed email, including a round trip both ways.
    fake = {
        "id": "18c0ffee1234abcd",
        "thread": "18c0ffee0000aaaa",
        "reply": True,
        "date": 1790000000,
        "sender": "Ada Lovelace",
        "subject": "Lunch?",
        "body": "Hi team,\r\nLunch at noon? See https://example.com/menu\r\n\r\n-- \r\nAda\r\n",
    }
    packets = to_packets(fake)
    for packet in packets:
        print(len(packet), packet.hex())
    print("inbound  ->", decode_message(reassemble(reversed(packets))))

    back = reply_packets("18c0ffee0000aaaa", "Noon works, see you there.")
    print("outbound ->", decode_message(reassemble(back)))
