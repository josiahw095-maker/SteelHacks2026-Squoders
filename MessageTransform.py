import base64
import html
import re
from email.header import decode_header, make_header
from email.utils import parseaddr

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

# Reply history and signatures: everything from the first match to the end of
# the body is history, so we cut rather than filter line by line.
QUOTE_MARKERS = [
    re.compile(r"^>"),                                          # quoted line
    re.compile(r"\bwrote:\s*$"),                                # "... Alice <a@x> wrote:"
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I),  # Outlook
    re.compile(r"^\s*From:\s+\S"),                              # Outlook header block
    re.compile(r"^\s*_{5,}\s*$"),                               # Outlook rule
    re.compile(r"^--\s*$"),                                     # signature delimiter
    re.compile(r"^\s*Sent from my \w+", re.I),                  # mobile signature
]

# Quoted history in HTML mail, caught before the tags get stripped away.
QUOTE_HTML = re.compile(r"(?i)<blockquote|<div[^>]*gmail_quote")


# Gmail wraps "On <date> <name> wrote:" across two lines, so a match on the
# second half would leave the first half behind. These let us back up to the
# start of the attribution.
ATTRIBUTION_OPEN = re.compile(r"^\s*On\b")
WROTE_END = re.compile(r"\bwrote:\s*$")


def strip_quotes(body):
    """Drop reply history and signatures from the end of a plain-text body.

    Everything from the first marker onward is history, so this cuts rather
    than filtering line by line. Falls back to the original when stripping
    would leave nothing, so a forward-only mail still carries something.
    """
    lines = body.splitlines()

    cut = None
    for index, line in enumerate(lines):
        if any(marker.search(line) for marker in QUOTE_MARKERS):
            cut = index
            break

    if cut is None:
        return body.strip()

    if WROTE_END.search(lines[cut]) and not ATTRIBUTION_OPEN.search(lines[cut]):
        for back in range(cut - 1, max(-1, cut - 3), -1):
            if ATTRIBUTION_OPEN.search(lines[back]):
                cut = back
                break

    stripped = "\n".join(lines[:cut]).strip()
    return stripped or body.strip()


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
        "body": strip_quotes(body),
                           
    }