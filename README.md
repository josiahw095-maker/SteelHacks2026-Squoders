# SteelHacks2026-Squoders

Our SteelHacks 2026 submission: receive email over a [Meshtastic](https://meshtastic.org/) LoRa mesh network, with no cellular or Wi-Fi needed at the receiving end.

> **Status:** the gateway side works end to end on real hardware: Gmail polling, email parsing, compression into radio packets, LoRa transfer, and decoding on the far node. **Not built yet:** the gateway loop that joins Gmail to the radio, and the phone app. See [TODO.txt](TODO.txt).

This is **not** a fork of the Meshtastic firmware. Both nodes run stock firmware, and we talk to them with the official [`meshtastic`](https://pypi.org/project/meshtastic/) Python library.

## How it works

| Piece | Role |
| --- | --- |
| **Gateway computer** | Internet-connected machine that reads Gmail and runs our software. |
| **Gateway node** | LoRa node connected to the gateway computer. Stock firmware. |
| **End node** | LoRa node connected to the phone over Bluetooth. Stock firmware. |
| **Endpoint** | A phone running our app, which displays the emails. |

```
Gmail ──► Gateway computer ──► Gateway node ══ LoRa mesh ══► End node ──BT──► Phone app
          poll, parse, shrink   sends packets   (encrypted)   receives         decodes + displays
```

1. `MessagePing.py` polls the Gmail API for new mail and fetches each message.
2. `MessageTransform.transform()` turns the Gmail JSON into `{id, date, sender, subject, body}`.
3. `MeshCodec.to_packets()` strips quoted replies, signatures and URLs, compresses the result, and splits it into packets of at most 200 bytes. A typical short email becomes one packet of about 70 bytes.
4. The gateway node sends the packets over the mesh on a private channel.
5. The end node receives them and passes them to the phone over Bluetooth.
6. The app reassembles and decodes them (`MeshCodec.reassemble()` and `decode_message()` are the reference implementation).

The packets are raw bytes sent on the Meshtastic private port (`PRIVATE_APP`), not text. The stock Meshtastic app does not understand them. The packet format is documented at the top of [MeshCodec.py](MeshCodec.py).

The phone does **not** need the channel key. Only the two radios hold it. The end node decrypts each packet and hands the plaintext to the phone, which only has to pair with the end node over Bluetooth.

## Repository layout

| File | Purpose |
| --- | --- |
| [MessagePing.py](MessagePing.py) | Polls Gmail history for new mail and fetches full messages. |
| [MessageTransform.py](MessageTransform.py) | Parses a Gmail message into `{id, date, sender, subject, body}`. |
| [MeshCodec.py](MeshCodec.py) | Packs an email into compressed binary packets and decodes them again, in both directions. Also the format spec for the phone app. |
| [provision.py](provision.py) | Creates a new private channel and puts it on both nodes. |
| [MessageReply.py](MessageReply.py) | Receives mail composed on the endpoint and sends it through Gmail. |
| [SentLog.py](SentLog.py) | Remembers what each thread hash stood for, so a reply can be threaded back. |
| [MeshSend.py](MeshSend.py) | Puts packets on the air, paced for airtime. |
| [MockRadio.py](MockRadio.py) | Stands in for the radio so the whole chain runs on one machine. |
| [endpoint/](endpoint) | The inbox: `station.py` talks to the node, `app.py` is the Streamlit screen. |
| [gmail-start/](gmail-start) | Gmail OAuth helper and its requirements. |
| [TODO.txt](TODO.txt) | What is done and what is left. |

## Setup

You need Python 3 (we used 3.14), two Meshtastic-compatible LoRa nodes (we used LilyGO T-Beam S3 Core boards on firmware 2.7.10), and a USB data cable for each. Antennas must be attached before a node transmits.

### 1. Install

```
pip install meshtastic
pip install -r gmail-start/requirements.txt
```

If the `meshtastic` command isn't found afterwards, run it as `python -m meshtastic` instead.

### 2. Set the radio region

Plug both nodes into the computer and find their serial ports (`COM5`, `COM6` on Windows, `/dev/ttyACM0` on Linux). Set the region for where you are, using `US` in the example. Only do this once per node, and use a legal region for your country:

```
python -m meshtastic --port COM5 --set lora.region US
python -m meshtastic --port COM6 --set lora.region US
```

### 3. Create the private channel

```
python provision.py COM6 COM5
```

This generates a random 32-byte key on the first node, copies the channel to the second node, and checks that both match. The channel URL, which contains the key, is written to `channel.url` and never printed. Each node's previous channel is saved to `channel.backup.url`. **Both files are git-ignored. Never commit or share them**; anyone with `channel.url` can read your mesh traffic. To give another radio the channel, load the URL with `--seturl` over USB, or scan its QR code in the Meshtastic phone app, and hand it over in person or through a password manager.

Add `--name` to choose the channel name (11 bytes or fewer). Nodes on different channel names sit on different frequencies, so all your nodes need the same one.

### 4. Test without a radio

`MockRadio.py` replaces the RF hop with a folder, so the whole chain runs on
one machine. Everything else is real: the same codec, compression, chunking
and reassembly. Useful when you have one node, no partner, or a flat battery.

In one terminal, run the gateway against the mock radio:

```
python MessagePing.py my-account loopback
```

In another, start the endpoint and pick `loopback` in its sidebar:

```
python -m streamlit run endpoint/app.py
```

Send yourself an email. It should appear in the browser within a few seconds,
and `spool/down/` will hold one file per packet if you want to look at the
bytes. Replies and new mail composed in the browser travel back through
`spool/up/` and are sent through the Gmail API.

### 5. Test over the air

Two nodes, two machines. One machine runs the gateway and needs internet and
Gmail credentials; the other runs the endpoint and needs neither.

```
python MessagePing.py my-account COM6              # gateway machine
python -m streamlit run endpoint/app.py            # endpoint machine, port COM5
```

The endpoint prints its node id on connect. Both nodes must share a region
and a channel, or they will not hear each other.

### Speed and reliability

Packets are paced to the radio's real airtime, addressed to one node, and each
is acknowledged before the next goes out. Three things you can tune:

- **Preset.** The default, LONG_FAST, is about 1 kbps: one packet is ~2 s on the
  air. SHORT_FAST is roughly ten times quicker and plenty across a room. Both
  nodes must match. This leaves the channel key alone:

  ```
  python set_preset.py SHORT_FAST COM5 COM6
  ```

  (`provision.py ... --preset SHORT_FAST` does the same while making a new key.)
- **Destination.** The gateway sends to the peer it last heard from. To pin it,
  use `--dest` or set `MESH_DEST`; the endpoint prints its node id on connect:

  ```
  python MessagePing.py my-account COM6 --dest !435c4ce4
  ```
- **Email size.** An email is capped at 6 packets (`MeshCodec.MAX_CHUNKS`); a longer
  body is trimmed and ends in `…`.

Run the unit tests (no radio needed) with `python -m unittest discover -s tests -v`.

### 6. Connect Gmail

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project, enable the **Gmail API**, configure the OAuth consent screen and add your account as a test user.
2. Create an OAuth client ID of type **Desktop app** and save the downloaded file as `gmail-start/secrets.json`.
3. Run the watcher with a name for the account. The first run opens a browser to log in:

```
python MessagePing.py my-account
```

Every new inbox message is fetched and printed as parsed JSON. Everyone who runs this needs their own Google Cloud project and credentials. `secrets.json` and the `tokens/` folder are git-ignored. Never commit them.

### 7. Read it on a phone

There is no native app yet. The endpoint serves a web page, so any device on
the same network can read the inbox: run it on the machine holding the end
node, then open the Network URL that Streamlit prints, for example
`http://10.0.0.5:8501`. On Android, Chrome's "Add to Home screen" gives it an
icon and a full-screen view.

Streamlit binds to every interface. On an open network that means anyone can
read the inbox and send mail as you, so use `--server.address 127.0.0.1` or a
private hotspot if that matters.

## Known issues

- **Bluetooth on Windows can hang when disconnecting.** With `bleak` 3.0 on Python 3.14, commands do their work and then never exit until Ctrl+C. Use USB serial for setup and testing, and a long-lived connection, never calling `close()`, for anything Bluetooth.
- **Range is untested.** All hardware tests so far had the nodes side by side.
- **One channel means one shared secret.** Anyone with the channel key can read every message on it. That's fine for one person's own mail, but not for several users on one mesh.

## Team

Squoders, SteelHacks 2026.
