import base64
import html
import re

def get_header(message, name):
    """Find one header, case insensitively. Returns "" if abesnt"""
    for header in message["payload"].get("headers", []):
        if header["name"].lower() == name.lower():
            return header["value"]

    return ""

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
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def transform(message):
    found = {}
    collect_text(message["payload"], found)

    body = found.get("text/plain") or html_to_text(found.get("text/html", ""))

    return {
        "id" : message["id"],
        "date": int(message.get("internalDate", 0)) // 1000,
        "sender": get_header(message, "From"),
        "subject": get_header(message, "Subject"),
        "body": body.strip(),
                           
    }