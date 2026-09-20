import base64
import html
import re
from email.header import decode_header, make_header
from email.utils import parseaddr

from MeshCodec import quote_cut

def get_header(message, name):
    """Find one header, case insensitively. Returns "" if abesnt"""
    for header in message["payload"].get("headers", []):
        if header["name"].lower() == name.lower():
            return header["value"]

    return ""

def decode_mime_words(value):
    """Decode RFC 2047 encoded-words (=?UTF-8?B?...?=) into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def parse_sender(from_header):
    """Shorten a From header to something worth spending mesh bytes on.

    'Alice Smith <alice@example.com>' -> 'Alice Smith'
    'alice@example.com'               -> 'alice'
    """
    name, address = parseaddr(from_header or "")
    name = decode_mime_words(name).strip().strip('"').strip()
    if name:
        return name
    if address:
        return address.split("@", 1)[0]
    return decode_mime_words(from_header).strip()


def decode(data):
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(data + padding)
    return raw.decode("utf-8", errors = "replace")

def collect_text(payload, found):
    if payload.get("filename"):
        return

    mime = payload.get("mimeType", "")

    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            collect_text(part, found)
        return

    if mime in ("text/plain", "text/html"):
        data = payload.get("body", {}).get("data")
        if data and mime not in found:
            found[mime] = decode(data)

def html_to_text(raw):
    """Crude but dependency-free: drop tags, unescape entities, tidy space."""
    raw = QUOTE_HTML.split(raw)[0]
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# Quoted history in HTML mail, caught before the tags get stripped away.
# The plain-text markers are NOT repeated here: they live in MeshCodec, which
# is the single place that decides where an email ends.
QUOTE_HTML = re.compile(r"(?i)<blockquote|<div[^>]*gmail_quote")


def strip_quotes(body):
    """Drop reply history and signatures from the end of a plain-text body.

    Where to cut is MeshCodec.quote_cut's decision rather than a second copy
    of the same markers: the codec cuts again before chunking, and two lists
    that had drifted apart would disagree about where an email ends. Falls
    back to the original when stripping would leave nothing, so a
    forward-only mail still carries something.
    """
    lines = body.splitlines()
    cut = quote_cut(lines)
    if cut is None:
        return body.strip()
    return "\n".join(lines[:cut]).strip() or body.strip()


def transform(message):
    found = {}
    collect_text(message["payload"], found)

    body = found.get("text/plain") or html_to_text(found.get("text/html", ""))

    return {
        "id" : message["id"],
        "date": int(message.get("internalDate", 0)) // 1000,
        "sender": parse_sender(get_header(message, "From")),
        "subject": decode_mime_words(get_header(message, "Subject")),
        "thread": message.get("threadId", ""),
        "reply": bool(get_header(message, "In-Reply-To")),
        "address": parseaddr(get_header(message, "From"))[1],
        "rfc_id": get_header(message, "Message-ID"),
        "body": strip_quotes(body),
                           
    }