import json
import sys
import time
from pathlib import Path

GMAIL_DIR = Path(__file__).resolve().parent / "gmail-start"
sys.path.insert(0, str(GMAIL_DIR))

from get_credentials import get_credentials
from googleapiclient.discovery import build
from MessageTransform import transform

POLL_SECONDS = 2


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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python MessagePing.py <account-name>")
        sys.exit(1)
    account = sys.argv[1]

    creds = get_credentials(
        str(GMAIL_DIR / "tokens" / f"{account}.json"),
        str(GMAIL_DIR / "secrets.json")
    )
    service = build("gmail", "v1", credentials = creds)

    history_id = service.users().getProfile(userId = "me").execute()["historyId"]
    print(f"Watching inbox for {account}. Ctrl + C to stop.")

    while True:
        history, history_id = CheckPingRecieved(service, history_id)
        for message_id in GetID(history):
            message = ReqMessage(service, message_id)
            print(json.dumps(transform(message), indent = 2))
        time.sleep(POLL_SECONDS)