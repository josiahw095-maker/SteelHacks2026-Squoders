"""Remember enough about each sent email to build a real reply later.

The mesh carries only a 2-byte hash of the Gmail threadId, and a hash cannot
be reversed. So when the gateway sends an email out it records what the hash
stood for; when a reply comes back carrying that hash, the gateway looks the
original up here and builds a properly threaded Gmail reply from it.

The file lives beside the account token, so it is covered by the same
gitignore rule. It holds sender addresses and subjects: keep it out of git.
"""

import json
from pathlib import Path

GMAIL_DIR = Path(__file__).resolve().parent / "gmail-start"

# Enough to cover a demo without the file growing forever.
MAX_ENTRIES = 200


def LogPath(account):
    return GMAIL_DIR / "tokens" / f"{account}.sent.json"


def Load(account):
    """Everything remembered so far, or {} if there is nothing usable."""
    try:
        data = json.loads(LogPath(account).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def Remember(account, thread_hash, email):
    """Record one outgoing email under the thread hash the mesh will carry."""
    log = Load(account)
    log[str(thread_hash)] = {
        "thread": email.get("thread") or email.get("id", ""),
        "id": email.get("id", ""),
        "address": email.get("address", ""),
        "subject": email.get("subject", ""),
        "rfc_id": email.get("rfc_id", ""),
    }

    # Oldest first, so trimming drops the entries least likely to be replied
    # to. The guard matters: below MAX_ENTRIES the excess is NEGATIVE, and
    # list(log)[:-n] slices from the far end, so an unguarded trim deletes
    # the NEWEST entries instead - 100 of them at 150 remembered threads.
    excess = len(log) - MAX_ENTRIES
    if excess > 0:
        for key in list(log)[:excess]:
            del log[key]

    path = LogPath(account)
    path.parent.mkdir(parents = True, exist_ok = True)
    path.write_text(json.dumps(log, indent = 1), encoding = "utf-8")
    return log


def Lookup(account, thread_hash):
    """What that thread hash stood for, or None if we never sent it."""
    return Load(account).get(str(thread_hash))
