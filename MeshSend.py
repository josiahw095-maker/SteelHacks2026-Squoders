"""Send MeshCodec packets to the gateway node over Bluetooth or USB serial.

meshtastic is imported lazily inside each function so the rest of the pipeline
- and --dry-run - still works on a machine with no radio attached.

The packets are raw bytes, so they go out with sendData on the private port
(PortNum.PRIVATE_APP, the library default), never sendText.
"""

import time

# Courtesy gap between packets. A full 200-byte packet is roughly 2 s of
# airtime at LONG_FAST, and every hop rebroadcasts what it hears.
SEND_GAP_SECONDS = 5.0


def scan():
    """Nearby Meshtastic nodes as (name, address), for finding the gateway."""
    from meshtastic.ble_interface import BLEInterface
    return [(d.name, d.address) for d in BLEInterface.scan()]


def open_link(target=None):
    """Connect to the gateway node.

    A COM port or /dev path opens over USB serial, which is what the hardware
    tests use; anything else is treated as a Bluetooth name or address.
    """
    if target and (target.upper().startswith("COM") or target.startswith("/dev/")):
        import meshtastic.serial_interface
        return meshtastic.serial_interface.SerialInterface(target)

    from meshtastic.ble_interface import BLEInterface
    return BLEInterface(target)


def send_packets(link, packets, dest=None, gap=SEND_GAP_SECONDS):
    """Send packets in order, pausing between them to limit airtime.

    A link of None prints instead of transmitting, so the whole pipeline can
    be exercised without hardware. dest of None broadcasts to the channel.
    """
    total = len(packets)
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.hex()))
        else:
            if dest:
                link.sendData(packet, destinationId=dest, wantAck=True)
            else:
                link.sendData(packet, wantAck=True)
            print("  sent      %s" % label)

        if position < total - 1:
            time.sleep(gap)

    return total
