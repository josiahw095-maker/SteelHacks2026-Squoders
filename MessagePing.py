"""Watch a Gmail inbox and push each new message over the mesh.

    python MessagePing.py <account> --dry-run     print packets, do not transmit
    python MessagePing.py <account> COM6          send over USB serial
    python MessagePing.py <account>               send over Bluetooth
    ... add  --dest !435c4ce4  (or set MESH_DEST) to address one node directly
    ... add  --timing  to have the endpoint report when each packet arrived

Without --dest the gateway addresses the peer it last heard from, or the only
other node it has heard lately, and broadcasts only if it knows neither.

The last processed historyId is saved next to the account token, so mail that
arrives while this is not running is still delivered on the next start.
"""

import os
import random
import sys
import threading
import time
from pathlib import Path

GMAIL_DIR = Path(__file__).resolve().parent / "gmail-start"
sys.path.insert(0, str(GMAIL_DIR))

from get_credentials import get_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from MessageTransform import transform
from MeshCodec import to_packets, short_hash
from SentLog import Remember
from MessageReply import Listen, Drain, Expire, Deliver, Collect
import MeshSend
from MeshSend import open_link, send_packets, pick_dest, normalize_dest

POLL_SECONDS = 2

# Gmail statuses worth retrying rather than crashing on.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BACKOFF = 60


def SplitArgs(argv, environ = None):
    """(positional arguments, pinned destination or None) from the command line.

    --dest may appear anywhere; MESH_DEST in the environment is the fallback.
    """
    environ = os.environ if environ is None else environ
    args, dest = [a for a in argv if a != "--timing"], None
    if "--dest" in args:
        at = args.index("--dest")
        if at + 1 >= len(args):
            raise ValueError("--dest needs a node id, e.g. --dest !435c4ce4")
        dest = args[at + 1]
        del args[at:at + 2]
    return args, dest or environ.get("MESH_DEST") or None


def CheckPingRecieved(service, history_id):
    response = service.users().history().list(
        userId = "me",
        startHistoryId = history_id,
        historyTypes = ["messageAdded"],
        labelId = "INBOX",
    ).execute()
    return response.get("history", []), response["historyId"]


def GetID(history):
    ids = []
    for record in history:
        for added in record.get("messagesAdded", []):
            message_id = added["message"]["id"]
            if message_id not in ids:
                ids.append(message_id)
    return ids


def ReqMessage(service, message_id):
    return service.users().messages().get(
        userId ="me", id = message_id, format = "full"
    ).execute()


# --- remembering where we got to -------------------------------------------

def HistoryPath(account):
    """Where the last processed historyId lives. tokens/ is already ignored."""
    return GMAIL_DIR / "tokens" / f"{account}.history"


def LoadHistoryId(account):
    """The saved position, or None if there is not one worth trusting."""
    path = HistoryPath(account)
    try:
        saved = path.read_text().strip()
    except OSError:
        return None
    return saved if saved.isdigit() else None


def SaveHistoryId(account, history_id):
    path = HistoryPath(account)
    path.parent.mkdir(parents = True, exist_ok = True)
    path.write_text(str(history_id))


# --- surviving the network --------------------------------------------------

def WithBackoff(call, what):
    """Run a Gmail call, retrying transient failures instead of dying.

    A 404 means the saved historyId has expired; that is the caller's problem
    to reset, so it is re-raised rather than retried.
    """
    delay = 1
    while True:
        try:
            return call()
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            if status not in RETRY_STATUSES:
                raise
            print(f"  Gmail {what} returned {status}; retrying in {delay}s")
        except OSError as error:
            print(f"  network error during {what} ({error}); retrying in {delay}s")

        time.sleep(delay + random.random())
        delay = min(delay * 2, MAX_BACKOFF)


def CurrentHistoryId(service):
    return WithBackoff(
        lambda: service.users().getProfile(userId = "me").execute(),
        "getProfile")["historyId"]


if __name__ == "__main__":
    timing = "--timing" in sys.argv[1:]
    try:
        args, pinned = SplitArgs(sys.argv[1:])
        if pinned:
            MeshSend.default_dest = normalize_dest(pinned)
    except ValueError as error:
        print(error)
        sys.exit(1)
    if len(args) < 1:
        print("usage: python MessagePing.py <account-name> [--dry-run | <port-or-ble-address>] [--dest !nodeid] [--timing]")
        sys.exit(1)
    account = args[0]
    target = args[1] if len(args) > 1 else None

    creds = get_credentials(
        str(GMAIL_DIR / "tokens" / f"{account}.json"),
        str(GMAIL_DIR / "secrets.json")
    )
    service = build("gmail", "v1", credentials = creds)

    if target == "--dry-run":
        link = None
        print("Dry run: packets will be printed, not transmitted.")
    else:
        link = open_link(target)
        if not hasattr(link, "Receive"):
            Listen()
        print("Connected to the gateway node. Listening for replies.")

    history_id = LoadHistoryId(account)
    if history_id:
        print(f"Resuming from saved historyId {history_id}.")
    else:
        history_id = CurrentHistoryId(service)
        SaveHistoryId(account, history_id)
        print(f"No saved position; starting from now (historyId {history_id}).")

    print(f"Watching inbox for {account}. Ctrl + C to stop.")

    try:
        while True:
            try:
                history, history_id = WithBackoff(
                    lambda: CheckPingRecieved(service, history_id), "history.list")
            except HttpError as error:
                if getattr(error.resp, "status", None) != 404:
                    raise
                # Gmail drops history older than about a week. Nothing to
                # recover, so start again from now rather than crashing.
                history_id = CurrentHistoryId(service)
                SaveHistoryId(account, history_id)
                print(f"Saved position expired; restarting from now ({history_id}).")
                time.sleep(POLL_SECONDS)
                continue

            for message_id in GetID(history):
                message = WithBackoff(
                    lambda: ReqMessage(service, message_id), "messages.get")
                email = transform(message)
                email["want_timing"] = timing
                packets = to_packets(email)
                print("")
                print(f"{email['sender']}: {email['subject']}  ->  {len(packets)} packet(s)")
                dest = pick_dest(link) if link else None
                sent = send_packets(link, packets, dest = dest, wait_ack = bool(dest))
                if sent < len(packets):
                    print(f"  only {sent} of {len(packets)} packets were confirmed; "
                          "the endpoint will ask for the rest")
                # so a reply carrying this thread hash can be threaded back
                Remember(account, short_hash(email["thread"] or email["id"]), email)

            # Saved only after the batch is sent, so a crash re-sends rather
            # than silently skipping mail.
            SaveHistoryId(account, history_id)

            # Replies arrive on the radio thread; this is where they are acted
            # on. In dry-run nothing is transmitted, so nothing is sent either.
            # With the mock radio there is no pubsub callback, so the
            # gateway has to pull packets out of the spool itself.
            if hasattr(link, "Receive"):
                for payload in link.Receive():
                    Collect(payload)

            Expire()
            for reply in Drain():
                print("")
                kind = ("timing report" if reply["timing"]
                        else "resend request" if reply["request"]
                        else "reply" if reply["reply"] else "new email")
                print(f"{kind} from the endpoint" + ("" if reply["timing"] else f": {reply['body'][:60]!r}"))
                try:
                    Deliver(service, account, reply, dry_run = link is None,
                            link = link)
                except HttpError as error:
                    print(f"  Gmail refused the reply: {error}")
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        if link is not None:
            # BLEInterface.close() can wait forever on Windows (see TODO), so
            # give the disconnect 10 seconds and then leave it behind.
            closer = threading.Thread(target = link.close, daemon = True)
            closer.start()
            closer.join(10)
