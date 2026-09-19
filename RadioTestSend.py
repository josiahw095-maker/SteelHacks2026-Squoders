"""Hardware test, sending side. Run after RadioTestListen.py is listening.

    python RadioTestSend.py Meshtastic_23c0 !abcd1234
    python RadioTestSend.py COM6 !abcd1234          (USB serial, more reliable)

The first argument is the sending node's Bluetooth name or COM port, the second is the node id
that RadioTestListen.py printed. Sends a fake email through the real pipeline:
Gmail JSON -> MessageTransform.transform -> MeshCodec -> radio -> LoRa -> receiving node.
"""
import base64
import sys
import threading
import time

import meshtastic.ble_interface
import meshtastic.serial_interface

import MeshCodec
from MessageTransform import transform

BODY = (
    "Hi all,\nThanks for joining the planning call today. Here is a summary of what "
    "we agreed. First, the launch date moves to October 14 so QA has another week. "
    "Second, Priya will own the vendor contract and send the draft by Friday. Third, "
    "we need volunteers for the on-call rotation starting next month. Please reply "
    "with your availability by end of day Wednesday. The full notes are in the "
    "shared folder. Let me know if I missed anything.\nThanks,\nJordan"
)

FAKE_EMAIL = {
    "id": "18c0ffee1234abcd",
    "internalDate": str(int(time.time() * 1000)),
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From", "value": "Jordan Rivera <jordan.rivera@example.com>"},
            {"name": "Subject", "value": "Q3 planning follow-up"},
        ],
        "body": {"data": base64.urlsafe_b64encode(BODY.encode()).decode()},
    },
}

if __name__ == "__main__":
    ble_name, dest = sys.argv[1], sys.argv[2]
    packets = MeshCodec.to_packets(transform(FAKE_EMAIL))
    print(f"{len(packets)} packet(s): {[len(p) for p in packets]} bytes")

    if ble_name.upper().startswith("COM") or ble_name.startswith("/dev/"):
        iface = meshtastic.serial_interface.SerialInterface(ble_name)
    else:
        iface = meshtastic.ble_interface.BLEInterface(ble_name)
    for i, packet in enumerate(packets, 1):
        print(f"sending packet {i}/{len(packets)} to {dest}")
        iface.sendData(packet, destinationId=dest, wantAck=True)
        time.sleep(5)   # leave the radio time to send before the next one
    time.sleep(5)
    # BleakClient.disconnect() hangs on this laptop; give up on it after 10 s.
    closer = threading.Thread(target=iface.close, daemon=True)
    closer.start()
    closer.join(10)
