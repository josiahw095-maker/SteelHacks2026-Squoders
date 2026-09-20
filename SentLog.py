"""Remember enough about each sent email to build a real reply later.

The mesh carries only a 2-byte hash of the Gmail threadId, and a hash cannot
be reversed. So when the gateway sends an email out it records what the hash
stood for; when a reply comes back carrying that hash, the gateway looks the
original up here and builds a properly threaded Gmail reply from it.

The file lives beside the account token, so it is covered by the same
gitignore rule. It holds sender addresses and subjects: keep it out of git.
"""

import json
import threading
from pathlib import Path

GMAIL_DIR = Path(__file__).resolve().parent / "gmail-start"

# Enough to cover a demo without the file growing forever.
MAX_ENTRIES = 200

# The file is the durable copy; this is the running process's view of it, so
# that a lookup is a dict hit rather than a read and a JSON parse. Every send
# and every reply used to re-read the whole file. Written from the Gmail loop
# and read when a reply arrives, hence the lock.
_cache = {}
_lock = threading.Lock()


def LogPath(account):
    return GMAIL_DIR / "tokens" / f"{account}.sent.json"


def _Cached(account):
    """The live dict for one account. The caller must hold _lock."""
    log = _cache.get(account)
    if log is None:
        try:
            data = json.loads(LogPath(account).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        log = data if isinstance(data, dict) else {}
        _cache[account] = log
    return log


def Load(account):
    """Everything remembered so far, or {} if there is nothing usable.

    A copy: the caller must not be able to edit the log behind Remember's
    back, or the file and this process would drift apart.
    """
    with _lock:
        return dict(_Cached(account))


def Remember(account, thread_hash, email):
    """Record one outgoing email under the thread hash the mesh will carry."""
    with _lock:
        log = _Cached(account)
        log[str(thread_hash)] = {
            "thread": email.get("thread") or email.get("id", ""),
            "id": email.get("id", ""),
            "address": email.get("address", ""),
            "subject": email.get("subject", ""),
            "rfc_id": email.get("rfc_id", ""),
        }

        # Oldest first, so trimming drops the entries least likely to be
        # replied to. max(0, ...) matters: excess is negative until the log
        # is full, and list(log)[:-99] means "all but the last 99", so the
        # unguarded slice deleted from the FRONT every time the log passed
        # 100 entries. It settled at ~100 instead of MAX_ENTRIES, and a
        # reply to anything older was dropped as an unknown thread.
        excess = max(0, len(log) - MAX_ENTRIES)
        for key in list(log)[:excess]:
            del log[key]

        path = LogPath(account)
        path.parent.mkdir(parents = True, exist_ok = True)
        path.write_text(json.dumps(log, indent = 1), encoding = "utf-8")
        return dict(log)


def Lookup(account, thread_hash):
    """What that thread hash stood for, or None if we never sent it."""
    with _lock:
        return _Cached(account).get(str(thread_hash))


def Forget(account = None):
    """Drop the cached view, so the next call re-reads the file.

    Only needed if something outside this process edits the log.
    """
    with _lock:
        _cache.pop(account, None) if account else _cache.clear()
