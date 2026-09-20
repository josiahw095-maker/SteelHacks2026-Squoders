"""Put every node on the same LoRa modem preset, without touching the channel key.

    python set_preset.py SHORT_FAST COM5 COM6

Both ends of a link must use the same preset or they will not hear each other,
so this changes them together and then checks they match. The channel and its
key are left alone (unlike provision.py, which makes a new key).

Faster presets carry more per second but reach less far; SHORT_FAST is about
ten times quicker than the LONG_FAST default and is plenty across a room.
"""
import sys
import time

from meshtastic.protobuf import config_pb2

PRESET = config_pb2.Config.LoRaConfig.ModemPreset


def PresetOf(iface):
    """The modem preset a connected node is using, as its name."""
    return PRESET.Name(iface.localNode.localConfig.lora.modem_preset)


def ApplyPreset(iface, name):
    """Set and write the preset on one connected node. The node reboots after."""
    value = PRESET.Value(name.upper())                 # ValueError if there is no such preset
    lora = iface.localNode.localConfig.lora
    lora.use_preset = True
    lora.modem_preset = value
    iface.localNode.writeConfig("lora")
    return value


def SetPresets(ports, name, connect, wait = 20, log = print):
    """Apply `name` to every port, wait for the reboots, and verify.

    connect(port) opens a node; it is passed in so this can be tested without
    hardware. Returns True only if every node reports the requested preset.
    """
    PRESET.Value(name.upper())                         # refuse a bad name before touching anything
    for port in ports:
        iface = connect(port)
        ApplyPreset(iface, name)
        log(f"{port}: preset set to {name.upper()}")
        iface.close()

    log(f"waiting {wait}s for the nodes to reboot...")
    time.sleep(wait)

    found = {}
    for port in ports:
        iface = connect(port)
        found[port] = PresetOf(iface)
        iface.close()
    ok = all(v == name.upper() for v in found.values())
    log("verified: " + ", ".join(f"{p}={v}" for p, v in found.items())
        if ok else f"MISMATCH: {found}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    from provision import Connect
    sys.exit(0 if SetPresets(sys.argv[2:], sys.argv[1], Connect) else 1)
