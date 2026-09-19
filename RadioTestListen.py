"""Hardware test, receiving side. Run this first, on the node plugged in by USB.

    python RadioTestListen.py COM5

Prints its own node id (give that to RadioTestSend.py), then waits for packets and
prints the decoded email. Understands both formats: PRIVATE_APP binary packets
(MeshCodec, sent by RadioTestSend.py) and text packets (MessagePayload, sent by
MessagePing.py).
"""
import sys
import time

import meshtastic.serial_interface
from meshtastic.protobuf import config_pb2
from pubsub import pub

import MeshCodec
import MessagePayload

groups = {}   # 2-byte email id -> packets received so far


text_groups = {}   # (kind, id) -> text packets received so far


def OnText(packet, decoded):
    """Text messages: MessagePing.py sends these (MessagePayload's format)."""
    text = decoded["payload"].decode("utf-8", errors="replace")
    try:
        fields = MessagePayload.parse_packet(text)
    except ValueError:
        print(f"text from {packet.get('fromId')}: {text!r}   (not our format)")
        return
    print(f"text packet from {packet.get('fromId')}: {len(text.encode())} bytes, "
          f"part {fields['index'] + 1}/{fields['total']}")

    key = (fields["kind"], fields["id"])
    group = text_groups.setdefault(key, [])
    group.append(text)
    message = MessagePayload.reassemble(group)
    if not message["complete"]:
        print(f"  waiting for parts {[i + 1 for i in message['missing']]}...")
        return
    del text_groups[key]
    print(f"\nEMAIL  from={message['sender']!r}  subject={message['subject']!r}  "
          f"reply={message['reply']}\n{message['body']}\n")


def OnReceive(packet, interface=None):
    decoded = packet.get("decoded", {})
    if decoded.get("portnum") == "TEXT_MESSAGE_APP":
        OnText(packet, decoded)
        return
    if decoded.get("portnum") != "PRIVATE_APP":
        print(f"other packet from {packet.get('fromId')}: {decoded.get('portnum')}")
        return
    payload = decoded["payload"]
    print(f"packet from {packet.get('fromId')}: {len(payload)} bytes  {payload.hex()}")

    group = groups.setdefault(payload[:2], [])
    group.append(payload)
    data = MeshCodec.reassemble(group)
    if data is None:
        print("  waiting for the rest of this email...")
        return
    del groups[payload[:2]]
    sender, subject, minutes, body = MeshCodec.decode_message(data)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(minutes * 60))
    print(f"\nEMAIL  from={sender!r}  subject={subject!r}  time={when}\n{body}\n")


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "COM5"
    pub.subscribe(OnReceive, "meshtastic.receive")
    iface = meshtastic.serial_interface.SerialInterface(port)
    lora = iface.localNode.localConfig.lora
    region = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
    preset = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
    print(f"region={region} preset={preset} (region must not be UNSET, and must match the sender)")
    print("listening as", iface.getMyNodeInfo()["user"]["id"], "- Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        iface.close()
