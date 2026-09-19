"""Send mesh payloads to the gateway node over Bluetooth.

meshtastic is imported lazily inside each function so the rest of the
pipeline - and --dry-run - still works on a machine with no radio attached.
"""

import time

# Courtesy gap between packets. A full packet is roughly a second of airtime
# at the default preset, and every hop rebroadcasts what it hears.
SEND_GAP_SECONDS = 2.0


def scan():
    """Nearby Meshtastic nodes as (name, address), for finding the gateway."""
    from meshtastic.ble_interface import BLEInterface
    return [(d.name, d.address) for d in BLEInterface.scan()]


def open_link(address=None):
    """Connect to the gateway node. address=None takes the only paired node."""
    from meshtastic.ble_interface import BLEInterface
    return BLEInterface(address)


def send_packets(link, packets, gap=SEND_GAP_SECONDS):
    """Send packets in order, pausing between them to limit airtime.

    A link of None prints instead of transmitting, so the whole pipeline can
    be exercised without hardware.
    """
    total = len(packets)
    for position, packet in enumerate(packets):
        label = "%d/%d %3dB" % (position + 1, total, len(packet))
        if link is None:
            print("  [dry-run] %s  %s" % (label, packet.decode("utf-8")))
        else:
            link.sendText(packet.decode("utf-8"))
            print("  sent      %s" % label)

        if position < total - 1:
            time.sleep(gap)

    return total
