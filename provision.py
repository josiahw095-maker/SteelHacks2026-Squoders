"""Creates a new private Meshtastic channel and puts it on two nodes.

    python provision.py COM6 COM5                 (USB ports of the two nodes)
    python provision.py COM6 COM5 --name MyMesh --yes

Node A gets a fresh random 32-byte key and the channel name; the channel is then
copied to node B. The channel URL (which contains the key) is written to
channel.url, which is git-ignored. It is never printed. Anyone who has that
file can read the mesh traffic, so share it only in person or through a
password manager. Each node's previous channel is saved to channel.backup.url
before it is replaced.

Both nodes must already have a region set (e.g. --set lora.region US).
"""
import argparse
import os
import sys
import time

import meshtastic.serial_interface
from meshtastic.protobuf import config_pb2

URL_FILE = "channel.url"
BACKUP_FILE = "channel.backup.url"
DEFAULT_PSK = b"\x01"


def Connect(port, tries=12):
    """Open a serial connection, retrying while a node reboots."""
    for _ in range(tries):
        try:
            return meshtastic.serial_interface.SerialInterface(port)
        except (Exception, SystemExit) as error:
            print(f"  waiting for {port} ({type(error).__name__})...")
            time.sleep(5)
    sys.exit(f"could not open {port}")


def Snapshot(iface):
    """Everything that has to match for two nodes to hear each other."""
    lora = iface.localNode.localConfig.lora
    ch = iface.localNode.channels[0].settings
    return (lora.region, lora.modem_preset, lora.use_preset, lora.channel_num,
            ch.name, bytes(ch.psk))


def Main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("port_a", help="node that generates the key, e.g. COM6")
    parser.add_argument("port_b", help="node that receives the channel, e.g. COM5")
    parser.add_argument("--name", default="SquodersNet", help="channel name (max 11 chars)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--preset", help="also set this modem preset on both nodes first, "
                        "e.g. SHORT_FAST (faster, shorter range; default is LONG_FAST)")
    args = parser.parse_args()
    if len(args.name.encode()) > 11:
        sys.exit("channel name must be 11 bytes or fewer")

    if args.preset:
        from set_preset import SetPresets
        if not SetPresets([args.port_a, args.port_b], args.preset, Connect):
            sys.exit("could not put both nodes on the same preset; nothing else was changed")

    a, b = Connect(args.port_a), Connect(args.port_b)
    if config_pb2.Config.LoRaConfig.UNSET == a.localNode.localConfig.lora.region:
        sys.exit("node A has no region set; run: python -m meshtastic --port "
                 f"{args.port_a} --set lora.region US")

    if not args.yes:
        answer = input(f"Replace channel 0 on {args.port_a} and {args.port_b} "
                       f"with a new private channel '{args.name}'? Type yes: ")
        if answer.strip().lower() != "yes":
            sys.exit("cancelled")

    with open(BACKUP_FILE, "a", encoding="utf-8") as backup:
        for iface in (a, b):
            node_id = iface.getMyNodeInfo()["user"]["id"]
            backup.write(f"# {node_id} {time.strftime('%Y-%m-%d %H:%M')}\n"
                         f"{iface.localNode.getURL(includeAll=False)}\n")
    print(f"old channels backed up to {BACKUP_FILE} (git-ignored)")

    node = a.localNode
    node.channels[0].settings.name = args.name
    node.channels[0].settings.psk = os.urandom(32)
    node.writeChannel(0)
    url = node.getURL(includeAll=False)
    b.localNode.setURL(url)
    with open(URL_FILE, "w", encoding="utf-8") as f:
        f.write(url + "\n")
    print(f"new channel '{args.name}' applied to both nodes; URL saved to {URL_FILE} (not shown)")

    time.sleep(3)
    a.close()
    b.close()
    print("waiting for the nodes to reboot...")
    time.sleep(20)

    a, b = Connect(args.port_a), Connect(args.port_b)
    snap_a, snap_b = Snapshot(a), Snapshot(b)
    a.close()
    b.close()
    ok = (snap_a == snap_b and snap_a[4] == args.name
          and len(snap_a[5]) == 32 and snap_a[5] != DEFAULT_PSK)
    print("verified: both nodes have the same private channel" if ok
          else "MISMATCH: the nodes do not have identical channel settings")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    Main()
