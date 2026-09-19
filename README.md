# SteelHacks2026-Squoders

Our SteelHacks 2026 submission: receive email over a [Meshtastic](https://meshtastic.org/) LoRa mesh network, with no cellular or Wi-Fi needed at the receiving end.

> **Status:** early development. The architecture is defined, but the code in this repo is still scaffolding (see [Repository layout](#repository-layout)).

## How it works

Four pieces make up the system:

| Piece | Role |
| --- | --- |
| **Gateway computer** | Internet-connected machine that receives email and runs our software. |
| **Gateway node** | LoRa node paired to the gateway computer over Bluetooth. Runs stock Meshtastic firmware. |
| **End node** | LoRa node paired to the endpoint over Bluetooth. Runs stock Meshtastic firmware. |
| **Endpoint** | Usually a phone running our app, which displays the emails. |

```
Email ──► Gateway computer ──BT──► Gateway node ══ LoRa mesh ══► End node ──BT──► Endpoint (app)
          (strip + shorten)        (encrypts + sends)            (receives)       (processes + displays)
```

1. An email arrives at the gateway computer running our software.
2. The software strips and shortens the email so it can travel efficiently over the low-bandwidth mesh.
3. The software sends the result to the gateway node over Bluetooth.
4. The node encrypts the message and sends it over the mesh.
5. The end node receives the message and passes it to the endpoint over Bluetooth.
6. The endpoint processes the message and shows it in our app.

## Repository layout

| File | Purpose |
| --- | --- |
| [MessagePing.py](MessagePing.py) | Gateway-side loop that checks for incoming pings and serves message requests. Stub, not yet runnable. |
| [MessageTransform.py](MessageTransform.py) | Email stripping and shortening logic. Empty placeholder. |
| [projectContext.txt](projectContext.txt) | Working notes on the project's goals and data flow. |

## Getting started

There is nothing to install or run yet. The only planned prerequisites are:

- Python 3 on the gateway computer
- Two Meshtastic-compatible LoRa nodes on stock firmware, each paired over Bluetooth
- An email account the gateway software can read

This section will be filled in with setup and run instructions as the implementation lands.

## Roadmap

- [ ] Receive email on the gateway computer
- [ ] Strip and shorten emails ([MessageTransform.py](MessageTransform.py))
- [ ] Send messages to the gateway node over Bluetooth
- [ ] Ping and message-request handling ([MessagePing.py](MessagePing.py))
- [ ] Endpoint app that receives and displays messages

## Team

Squoders, SteelHacks 2026.
