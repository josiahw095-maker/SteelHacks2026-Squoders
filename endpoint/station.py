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

# A half-received message is forgotten after this long.
GROUP_TIMEOUT = 300

# How long a message may stall before we ask for the missing parts, and
# how many times we are willing to ask.
REQUEST_AFTER = 20
MAX_REQUESTS = 3

# Lines kept for the live feed on screen.
MAX_EVENTS = 200

# Chunk ids remembered briefly after decoding, so a late duplicate - a
# resend that lands once the message is already complete - is dropped rather
# than starting a phantom group that never finishes. It has to EXPIRE: the
# same email sent again later is a real message, not a duplicate, and the id
# is only 16 bits so it will legitimately come round again.
DONE_WINDOW = 45
MAX_DONE = 60

# The inbox is what the endpoint serializes on every poll, so it does not get
# to grow forever. Far more than a demo will ever reach; it is a backstop, not
# a policy.
MAX_MESSAGES = 500


class Station:
    """One connection to the end node, plus everything heard so far."""

    def __init__(self, port = None, mock = False):
        self.port = port
        self.mock = mock
        self.lock = threading.Lock()
        # Held for the length of a whole send. Chase() runs on whichever
        # thread happened to poll, a reply runs on the request thread, and
        # two of them calling sendData at once interleaves two messages on
        # the air. This is the radio, not the bookkeeping: never hold both.
        self.send_lock = threading.Lock()
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
            self.Accept(payload)

    def Accept(self, payload):
        """Feed one packet in. Returns the message if that completed one.

        Separate from OnReceive so the endpoint can be driven with bytes in
        tests, with no radio anywhere.
        """
        key = bytes(payload[:2])

        with self.lock:
            seen_at = self.done.get(key)
            if seen_at is not None and time.time() - seen_at < DONE_WINDOW:
                return None                  # a late duplicate of that message

        self.Note("rx", f"packet in  ({key.hex()})", len(payload))

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
                # Oldest first, rather than expired-only: a burst of fresh ids
                # has nothing expired in it, so filtering on DONE_WINDOW alone
                # would drop nothing and let the dict grow past its cap.
                for stale in sorted(self.done, key = self.done.get)[
                        :len(self.done) - MAX_DONE]:
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
            if len(self.messages) > MAX_MESSAGES:
                del self.messages[:len(self.messages) - MAX_MESSAGES]
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
        The loopback interface sweeps its own spool from Receive(), so the
        folder this reads does not grow for the length of the demo.
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

        with self.lock:
            for key, group in self.groups.items():
                if now - group["seen"] < REQUEST_AFTER:
                    continue
                if group.get("requests", 0) >= MAX_REQUESTS:
                    continue
                missing = MeshCodec.missing_parts(group["packets"])
                if not missing:
                    continue
                group["requests"] = group.get("requests", 0) + 1
                group["seen"] = now          # back off before asking again
                asks.append((int.from_bytes(key, "big"), missing, key))

        sent = []
        for group_id, missing, key in asks:
            # wait = False: this runs on a polling thread, and blocking it
            # behind a reply that is mid-air would stall the whole page.
            if self.Send(MeshCodec.request_packets(group_id, missing),
                         wait = False) is None:
                with self.lock:
                    group = self.groups.get(key)
                    if group:
                        # Hand the attempt back. It was never asked for, so
                        # it should not count against MAX_REQUESTS, and the
                        # next sweep should pick it up rather than wait out
                        # another REQUEST_AFTER.
                        group["requests"] = max(0, group.get("requests", 1) - 1)
                        group["seen"] = 0.0
                continue
            self.Note("ask", f"asked for parts {missing} of {group_id:04x}")
            sent.append((group_id, missing))
        return sent

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

    def Send(self, packets, gap = 2.0, on_progress = None, wait = True):
        """Put packets on the air, pausing between them for airtime.

        on_progress(sent, total) is called after each packet so a caller can
        show how far along the send is; a full packet is seconds of airtime,
        which is long enough to be worth showing.

        wait = False returns None straight away when the radio is already
        busy, instead of queueing behind a send that may run for half a
        minute. That is what Chase wants: a resend request it cannot put out
        now is asked for again on the next sweep anyway.
        """
        if not self.send_lock.acquire(blocking = wait):
            return None
        try:
            total = len(packets)

            def report(sent):
                if on_progress:
                    on_progress(sent, total)

            if self.link is None:
                for position in range(total):  # demo mode: pretend, but pace it
                    time.sleep(0.2)
                    report(position + 1)
                return total

            for position, packet in enumerate(packets):
                self.link.sendData(packet, wantAck = True)
                self.Note("tx", f"packet out ({position + 1}/{total})", len(packet))
                report(position + 1)
                if position < total - 1:
                    time.sleep(gap)
            return total
        finally:
            self.send_lock.release()

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
