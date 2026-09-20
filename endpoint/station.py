"""The endpoint's radio side: receive mail, send replies, hold what arrived.

Deliberately free of Streamlit. Everything here is what a TypeScript port
would have to reimplement, so keeping it separate makes that swap a rewrite
of one file rather than an untangling. The UI only ever calls these methods.

Set mock=True to run the whole endpoint with no radio attached.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MeshCodec
import MeshSend

# A half-received message is forgotten after this long.
GROUP_TIMEOUT = 300

# How many times we are willing to ask for missing parts.
MAX_REQUESTS = 3

# How long a message may stall before we ask. This CANNOT be a constant:
# it has to stay comfortably above the gap the sender leaves between
# packets, and that gap is now airtime, so it moves with the modem preset.
# At LONG_FAST the gateway leaves 16.8 s between packets and the endpoint
# sees them 18.7 s apart - against which the old flat 20 s left 1.3 s of
# margin, so nearly every long message got chased while it was still
# arriving perfectly well.
#
# A spurious chase is not free. This radio is half-duplex: while it
# transmits a request it cannot hear, so an ask sent into the middle of an
# incoming message can cost us the very packet we were waiting for - and
# that loss triggers another ask. Long messages fail from the back end
# first, which is exactly the shape of the trouble.
REQUEST_MARGIN = 3.0        # multiples of the sender's inter-packet gap
REQUEST_AFTER_FLOOR = 30.0


def SendInterval():
    """How far apart a sender leaves its packets, at the current preset."""
    return (MeshSend.gap_for(MeshCodec.MAX_PAYLOAD)
            + MeshSend.airtime_seconds(MeshCodec.MAX_PAYLOAD + MeshSend.MESH_HEADER))


def RequestAfter():
    """Seconds of silence that mean a message really has stalled."""
    return max(REQUEST_AFTER_FLOOR, REQUEST_MARGIN * SendInterval())

# Lines kept for the live feed on screen.
MAX_EVENTS = 200

# Chunk ids remembered briefly after decoding, so a late duplicate - a
# resend that lands once the message is already complete - is dropped rather
# than starting a phantom group that never finishes. It has to EXPIRE: the
# same email sent again later is a real message, not a duplicate, and the id
# is only 16 bits so it will legitimately come round again.
DONE_WINDOW = 45
MAX_DONE = 60


class Station:
    """One connection to the end node, plus everything heard so far."""

    def __init__(self, port = None, mock = False):
        self.port = port
        self.mock = mock
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()  # serializes Send() across threads;
                                            # see Send() for why
        self.messages = []          # decoded inbound mail, oldest first
        self.groups = {}            # packets still waiting for their siblings
        self.sent = []              # what we pushed back, for the UI to show
        self.events = []            # a running log for the screen
        self.done = {}              # chunk id -> when it was decoded
        self.link = None
        self.node_id = None
        self.error = None

        if not mock:
            self.Connect()

    # --- the radio ---------------------------------------------------------

    def Connect(self):
        """Open the node. Any failure is recorded, never raised, so the UI
        can show it instead of dying on import."""
        try:
            # A different radio may be firmware that does report its
            # TX queue draining, so stop holding the last one against it.
            MeshSend.forget_queue_trust()
            from MockRadio import IsLoopback, LoopbackInterface
            if IsLoopback(self.port):
                # No radio: packets arrive through a folder, so there is no
                # pubsub callback and Poll() has to be called instead.
                self.link = LoopbackInterface(direction = "up")
                self.node_id = "!loopback"
                return

            from pubsub import pub
            if self.port and (self.port.upper().startswith("COM")
                              or self.port.startswith("/dev/")):
                import meshtastic.serial_interface as serial_interface
                self.link = serial_interface.SerialInterface(self.port)
            else:
                from meshtastic.ble_interface import BLEInterface
                self.link = BLEInterface(self.port)

            self.node_id = self.link.getMyNodeInfo()["user"]["id"]
            MeshSend.read_preset(self.link)   # airtime, and so pacing, depend on it
            pub.subscribe(self.OnReceive, "meshtastic.receive")
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"

    def OnReceive(self, packet, interface = None):
        """pubsub callback, on meshtastic's thread. Only parks data."""
        decoded = packet.get("decoded", {})
        if decoded.get("portnum") != "PRIVATE_APP":
            return
        payload = decoded.get("payload")
        if payload:
            # How strong and how clean, as the receiving radio heard it. When
            # packets go missing this is the first thing worth knowing: a
            # marginal SNR means they are being lost on the air, while a
            # very STRONG rssi at close range means the front end is being
            # overloaded, which costs packets just as surely.
            self.Accept(payload, snr = packet.get("rxSnr"),
                        rssi = packet.get("rxRssi"))

    def Accept(self, payload, snr = None, rssi = None):
        """Feed one packet in. Returns the message if that completed one.

        Separate from OnReceive so the endpoint can be driven with bytes in
        tests, with no radio anywhere.
        """
        key = bytes(payload[:2])

        with self.lock:
            seen_at = self.done.get(key)
            if seen_at is not None and time.time() - seen_at < DONE_WINDOW:
                return None                  # a late duplicate of that message

        if len(payload) >= 3:
            part, claimed = payload[2] >> 4, payload[2] & 0x0F
            where = f"part {part + 1} of {claimed}"
        else:
            where = "runt"
        heard = ""
        if snr is not None or rssi is not None:
            heard = f"  snr {snr if snr is not None else '?'}"                     f" rssi {rssi if rssi is not None else '?'}"
        self.Note("rx", f"packet in  {key.hex()} {where}{heard}", len(payload))

        with self.lock:
            group = self.groups.setdefault(key, {"packets": [], "seen": 0.0})
            if bytes(payload) in group["packets"]:
                return None                  # same packet twice; ignore
            group["packets"].append(bytes(payload))
            group["seen"] = time.time()
            data = MeshCodec.reassemble(group["packets"])
            if data is None:
                return None
            # Capture what the delivery cost before the group is discarded.
            packet_count = len(group["packets"])
            air_bytes = sum(len(p) for p in group["packets"])
            del self.groups[key]
            now = time.time()
            self.done[key] = now
            if len(self.done) > MAX_DONE:
                for stale in [k for k, t in self.done.items()
                              if now - t > DONE_WINDOW][:len(self.done) - MAX_DONE]:
                    del self.done[stale]

        try:
            message = MeshCodec.decode_message(data)
        except Exception:
            return None                      # corrupt; drop quietly

        if message["outbound"]:
            return None                      # our own send heard back

        self.Note("mail", f"decoded: {message['subject'] or '(no subject)'}", air_bytes)
        message["at"] = time.time()
        message["packets"] = packet_count
        message["airbytes"] = air_bytes
        # What the reader actually got, against what it cost to carry.
        delivered = len((message["sender"] + message["subject"]
                         + message["body"]).encode("utf-8"))
        message["delivered"] = delivered
        message["ratio"] = (delivered / air_bytes) if air_bytes else 0.0

        with self.lock:
            self.messages.append(message)
        return message

    def Note(self, kind, text, bytes_ = 0):
        """Record one line for the live feed the browser shows.

        Bounded, because this runs for the length of a demo and nobody wants
        an endpoint that slowly eats memory.
        """
        with self.lock:
            self.events.append({"at": time.time(), "kind": kind,
                                "text": text, "bytes": bytes_})
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]

    def Feed(self, limit = 40):
        with self.lock:
            return list(self.events[-limit:])[::-1]

    def Close(self):
        """Let go of the radio so another connection can take its place.

        The pubsub subscription has to go first: a Station that is still
        listening keeps receiving into an inbox nobody is looking at, and
        those stale listeners pile up every time the transport is switched.
        """
        try:
            from pubsub import pub
            pub.unsubscribe(self.OnReceive, "meshtastic.receive")
        except Exception:
            pass

        link, self.link = self.link, None
        if link is not None and hasattr(link, "close"):
            # BLEInterface.close() can wait forever on Windows, so give the
            # disconnect ten seconds and then abandon it.
            closer = threading.Thread(target = link.close, daemon = True)
            closer.start()
            closer.join(10)

    def Poll(self):
        """Pull packets in when there is no callback (the mock radio).

        Safe to call always: with a real radio there is nothing to pull.
        """
        if not hasattr(self.link, "Receive"):
            return 0
        arrived = 0
        for payload in self.link.Receive():
            self.Accept(payload)
            arrived += 1
        return arrived

    def Chase(self, now = None):
        """Ask the gateway to resend the parts of any stalled message.

        A message is stalled when nothing new has arrived for it in
        REQUEST_AFTER seconds. Each one is chased at most MAX_REQUESTS times
        so a gateway that has gone away cannot start a loop.

        Returns [(chunk id, missing parts)] for whatever was asked for.
        """
        now = now if now is not None else time.time()
        asks = []
        request_after = RequestAfter()
        interval = SendInterval()

        # A send already in flight owns the radio. Chasing now would block on
        # send_lock until that send finished - and this runs on the browser's
        # poll, so the page would freeze exactly while it most wants to draw
        # progress. A stalled group is still stalled at the next poll, so
        # nothing is lost by skipping this round; the request counters are
        # left alone too, since we did not actually ask for anything.
        # (A send that starts in the gap between this probe and Send() below
        # only puts us back where we were, so the probe is worth having even
        # though it is not airtight.)
        if not self.send_lock.acquire(blocking = False):
            return []
        self.send_lock.release()

        with self.lock:
            for key, group in self.groups.items():
                if now - group["seen"] < request_after:
                    continue
                if group.get("requests", 0) >= MAX_REQUESTS:
                    continue
                missing = MeshCodec.missing_parts(group["packets"])
                if not missing:
                    continue

                # Senders transmit in order, so a part below the highest we
                # have seen is genuinely lost, while a part above it may not
                # have been sent yet. Asking for packets that are still in
                # flight doubles the load on the channel that is already the
                # reason they are late - one stalled message went out as 18
                # packets instead of 7 that way, and the extra traffic cost
                # more packets than it recovered.
                high = MeshCodec.highest_part(group["packets"])
                outstanding = (missing[-1] - high) * interval

                if now - group["seen"] >= request_after + outstanding:
                    wanted = missing      # the sender cannot still be going
                else:
                    wanted = [part for part in missing if part < high]
                if not wanted:
                    continue

                group["requests"] = group.get("requests", 0) + 1
                group["seen"] = now          # back off before asking again
                asks.append((int.from_bytes(key, "big"), wanted))

        # One Send() call, not one per group: Send() only paces *between*
        # packets in the same call, and a request is always a single packet.
        # Calling Send() separately per group meant simultaneous stalls fired
        # back-to-back with no gap between them at all - a burst.
        packets = []
        for group_id, missing in asks:
            self.Note("ask", f"asked for parts {missing} of {group_id:04x}")
            packets.extend(MeshCodec.request_packets(group_id, missing))
        if packets:
            self.Send(packets)
        return asks

    def Expire(self, now = None):
        """Forget half-received messages that will never complete."""
        now = now if now is not None else time.time()
        with self.lock:
            stale = [k for k, g in self.groups.items()
                     if now - g["seen"] > GROUP_TIMEOUT]
            for key in stale:
                del self.groups[key]
        return len(stale)

    # --- what the UI reads -------------------------------------------------

    def Inbox(self):
        with self.lock:
            return list(self.messages)

    def Threads(self):
        """[(thread, [messages])], most recently active conversation first."""
        grouped = {}
        for message in self.Inbox():
            grouped.setdefault(message["thread"], []).append(message)
        for entries in grouped.values():
            entries.sort(key = lambda m: m["at"])
        return sorted(grouped.items(),
                      key = lambda item: item[1][-1]["at"], reverse = True)

    def Waiting(self):
        """Half-received messages, as {chunk id: packets so far}."""
        with self.lock:
            return {key.hex(): (len(group["packets"]),
                                group.get("requests", 0))
                    for key, group in self.groups.items()}

    def Status(self):
        if self.mock:
            return "demo mode (no radio)"
        if self.node_id == "!loopback":
            return "loopback - packets through MockRadio/spool"
        if self.error:
            return f"not connected - {self.error}"
        return f"connected to {self.node_id}"

    # --- what the UI writes ------------------------------------------------

    def Send(self, packets, on_progress = None):
        """Put packets on the air, waiting after each for the radio's own TX
        queue to report empty before handing over the next - never more than
        one packet outstanding at once, confirmed by the device itself
        rather than guessed at with a fixed delay.

        Chase(), Reply() and Compose() all call this from whatever HTTP
        request thread happens to invoke them, and the browser can have
        several requests in flight at once (the steady state poll plus
        watchProgress()'s faster one while a send is running). send_lock
        keeps two of those from calling sendData() at the same time and
        interleaving their transmissions.

        on_progress(sent, total) is called after each packet so a caller can
        show how far along the send is.
        """
        total = len(packets)

        def report(sent):
            if on_progress:
                on_progress(sent, total)

        with self.send_lock:
            if self.link is None:
                for position in range(total):     # demo mode: pretend, but pace it
                    time.sleep(0.2)
                    report(position + 1)
                return total

            for position, packet in enumerate(packets):
                # A radio holding packets it has not transmitted cannot be
                # helped by another one, and at zero free slots sendData()
                # blocks forever inside the library - which would take this
                # request thread, and the send_lock, down with it.
                MeshSend.pace_before(self.link)   # airtime owed from last time

                if MeshSend.queue_backed_up(self.link):
                    self.Note("tx", f"STOPPED at packet {position + 1}/{total}"
                                    f" - the radio is holding packets it has"
                                    f" not transmitted; the channel is saturated")
                    return position

                self.link.sendData(packet)
                report(position + 1)      # the bar moves on the handover...
                _, timed_out = MeshSend.pace_after(self.link, len(packet))
                # ...and the feed line says what this packet owes the channel
                # before the next one may go out.
                self.Note("tx", f"packet out ({position + 1}/{total})"
                                f" - next in {MeshSend.gap_for(len(packet)):.2f} s"
                                + (" - queue never reported empty" if timed_out
                                   else ""), len(packet))
            return total

    def Reply(self, thread, body, on_progress = None):
        """Reply into a conversation. thread is the 16-bit hash we received."""
        count = self.Send(MeshCodec.reply_packets(thread, body),
                          on_progress = on_progress)
        with self.lock:
            self.sent.append({"kind": "reply", "thread": thread,
                              "body": body, "at": time.time()})
        return count

    def Compose(self, address, subject, body, on_progress = None):
        """Send a brand new email, carrying its own recipient."""
        count = self.Send(MeshCodec.compose_packets(address, subject, body),
                          on_progress = on_progress)
        with self.lock:
            self.sent.append({"kind": "new", "to": address, "subject": subject,
                              "body": body, "at": time.time()})
        return count

    # --- demo mode ---------------------------------------------------------

    def SeedDemo(self):
        """A few messages so the UI can be built with no hardware."""
        samples = [
            ("Alice Smith", "Lunch tomorrow",
             "Are we still on for noon? The cafe on Forbes works for me."),
            ("Jordan Rivera", "Q3 planning follow-up",
             "Thanks for joining the call. Launch moves to October 14 so QA "
             "gets another week. Priya owns the vendor contract."),
            ("Alice Smith", "Re: Lunch tomorrow",
             "Perfect, see you at noon."),
        ]
        now = time.time()
        # Encoded and fed back through Accept, so demo mail takes exactly the
        # same path as real mail: chunk, reassemble, decode.
        for offset, (sender, subject, body) in enumerate(samples):
            packets = MeshCodec.to_packets({
                "id": f"demo{offset}",
                "thread": "demo-thread-a" if "Lunch" in subject else "demo-thread-b",
                "reply": subject.startswith("Re:"),
                "date": int(now) - (len(samples) - offset) * 600,
                "sender": sender,
                "subject": subject,
                "body": body,
            })
            for packet in packets:
                self.Accept(packet)
