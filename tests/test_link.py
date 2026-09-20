"""Tests for how packets are paced, addressed and confirmed on the way out.

    python -m unittest discover -s tests -v

No radio needed: the links here are fakes that record what was sent.
"""
import contextlib
import io
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import MeshCodec
import MeshSend


def quiet(call, *args, **kwargs):
    """Run a send without its progress lines cluttering the test report."""
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def packets(count = 4, body_len = 700, mid = "t1"):
    """A message that really does need `count`-ish packets."""
    import random, string
    rnd = random.Random(5)
    body = "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(body_len))
    return MeshCodec.to_packets({"id": mid, "thread": "t", "reply": False, "date": 1789561200,
                                 "sender": "A", "subject": "S", "body": body})


class FakeLink:
    """Records sendData calls. No radio information, like a link we know nothing about."""

    def __init__(self, nodes = None):
        self.calls = []
        self.nodes = nodes or {}

    def sendData(self, packet, destinationId = None, wantAck = False, **kwargs):
        self.calls.append({"packet": bytes(packet), "dest": destinationId, "ack": wantAck, **kwargs})

    def getMyNodeInfo(self):
        return {"user": {"id": "!11111111"}}


class AckLink(FakeLink):
    """Answers each packet the way firmware does: an ack, a NAK, or silence.

    script(n) says what happens to the n-th sendData call: "NONE" is an ack, any
    other name is a NAK with that error, "SILENT" is no answer at all. The answer
    arrives `delay` seconds later on a different thread, as it does from a radio.
    """

    def __init__(self, script = None, delay = 0.0, **kwargs):
        super().__init__(**kwargs)
        self.script = script or (lambda n: "NONE")
        self.delay = delay
        self.times = []

    def sendData(self, packet, destinationId = None, wantAck = False, onResponse = None, **kwargs):
        super().sendData(packet, destinationId = destinationId, wantAck = wantAck,
                         onResponse = onResponse, **kwargs)
        self.times.append(time.monotonic())
        answer, delay = self.script(len(self.calls) - 1), self.delay
        if isinstance(answer, tuple):                    # ("NONE", 0.15): this answer is late
            answer, delay = answer
        if onResponse is None or answer == "SILENT":
            return
        reply = {"decoded": {"routing": {"errorReason": answer}}}
        threading.Timer(delay, onResponse, [reply]).start()


class Pacing(unittest.TestCase):
    """Fix 1: a 2 s pause between packets unless the link says otherwise."""

    def send(self, link, **kwargs):
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            quiet(MeshSend.send_packets, link, packets(), **kwargs)
        return [c.args[0] for c in sleep.call_args_list]

    def test_default_is_two_seconds(self):
        self.assertEqual(MeshSend.SEND_GAP_SECONDS, 2.0)
        pauses = self.send(FakeLink())
        self.assertGreaterEqual(len(pauses), 2)
        self.assertTrue(all(p == 2.0 for p in pauses), pauses)

    def test_pauses_are_between_packets_not_after_the_last(self):
        link = FakeLink()
        pauses = self.send(link)
        self.assertEqual(len(pauses), len(link.calls) - 1)

    def test_explicit_gap_wins(self):
        self.assertTrue(all(p == 0.25 for p in self.send(FakeLink(), gap = 0.25)))

    def test_zero_gap_is_honoured_not_treated_as_missing(self):
        self.assertTrue(all(p == 0 for p in self.send(FakeLink(), gap = 0)))

    def test_dry_run_never_waits(self):
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            quiet(MeshSend.send_packets, None, packets())
        for call in sleep.call_args_list:
            self.assertEqual(call.args[0], 0)

    def test_a_link_can_say_it_needs_no_airtime(self):
        import MockRadio
        self.assertLess(MeshSend.pace(MockRadio.LoopbackInterface.__new__(MockRadio.LoopbackInterface)), 0.5)

    def test_resend_uses_the_same_pacing(self):
        pk = packets()
        MeshSend.remember_sent(pk)
        group = int.from_bytes(pk[0][:2], "big")
        link = FakeLink()
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            sent = quiet(MeshSend.resend, link, group, {0, 1, 2})
        self.assertEqual(sent, 3)
        self.assertTrue(all(c.args[0] == 2.0 for c in sleep.call_args_list))


from types import SimpleNamespace

from meshtastic.protobuf import config_pb2

import set_preset

LORA = config_pb2.Config.LoRaConfig


def radio_link(preset = "LONG_FAST", **fields):
    """A link whose radio settings can be read, like a real SerialInterface."""
    link = FakeLink()
    lora = LORA()
    lora.use_preset = fields.pop("use_preset", True)
    lora.modem_preset = LORA.ModemPreset.Value(preset)
    for name, value in fields.items():
        setattr(lora, name, value)
    link.localNode = SimpleNamespace(localConfig = SimpleNamespace(lora = lora))
    return link


class Airtime(unittest.TestCase):
    """Fix 2: the pause follows the radio's real airtime."""

    def test_formula_matches_the_semtech_reference(self):
        # Published example: 10-byte frame, SF7, 125 kHz, CR 4/5, 8-symbol preamble = 41.2 ms
        self.assertAlmostEqual(MeshSend.airtime(10, 7, 125e3, 5, preamble = 8), 0.0412, delta = 0.0005)

    def test_a_long_fast_packet_is_about_two_seconds(self):
        seconds = MeshSend.packet_airtime(radio_link("LONG_FAST"))
        self.assertTrue(1.7 < seconds < 2.1, seconds)

    def test_faster_presets_take_less_time(self):
        times = [MeshSend.packet_airtime(radio_link(name))
                 for name in ("LONG_SLOW", "LONG_FAST", "MEDIUM_FAST", "SHORT_FAST", "SHORT_TURBO")]
        self.assertEqual(times, sorted(times, reverse = True))
        self.assertGreater(times[1] / times[3], 8)          # SHORT_FAST is ~10x LONG_FAST

    def test_unreadable_radio_gives_no_airtime(self):
        self.assertIsNone(MeshSend.packet_airtime(FakeLink()))


class PresetPacing(unittest.TestCase):
    def total_wait(self, link, count = 15):
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            quiet(MeshSend.send_packets, link, packets(), gap = None)
        return sum(c.args[0] for c in sleep.call_args_list), sleep.call_count

    def test_long_fast_paces_just_past_one_airtime(self):
        link = radio_link("LONG_FAST")
        gap = MeshSend.pace(link)
        self.assertTrue(2.0 <= gap <= 2.5, gap)
        self.assertGreater(gap, MeshSend.packet_airtime(link))      # the packet is off the air first

    def test_short_fast_paces_far_faster(self):
        gap = MeshSend.pace(radio_link("SHORT_FAST"))
        self.assertTrue(MeshSend.MIN_GAP <= gap < 1.0, gap)

    def test_pause_never_drops_below_the_floor(self):
        self.assertEqual(MeshSend.pace(radio_link("SHORT_TURBO")), MeshSend.MIN_GAP)

    def test_a_radio_we_cannot_read_gets_the_safe_default(self):
        self.assertEqual(MeshSend.pace(FakeLink()), MeshSend.SEND_GAP_SECONDS)

    def test_custom_modem_settings_are_used_when_no_preset(self):
        slow = radio_link("LONG_FAST", use_preset = False, spread_factor = 12, bandwidth = 125, coding_rate = 8)
        self.assertGreater(MeshSend.pace(slow), MeshSend.SEND_GAP_SECONDS)

    def test_a_15_packet_email_sends_much_sooner_on_a_fast_preset(self):
        long_fast = MeshSend.pace(radio_link("LONG_FAST")) * 14
        short_fast = MeshSend.pace(radio_link("SHORT_FAST")) * 14
        # SHORT_FAST's own airtime is under MIN_GAP, so the floor, not the
        # airtime, is what sets its pace; the ratio stops widening there.
        self.assertGreater(long_fast / short_fast, 6)

    def test_the_gap_used_is_the_gap_for_that_radio(self):
        wait, pauses = self.total_wait(radio_link("SHORT_FAST"))
        self.assertGreater(pauses, 0)
        self.assertAlmostEqual(wait / pauses, MeshSend.pace(radio_link("SHORT_FAST")))


class FakeNodes:
    """A bench of nodes keyed by port. A write takes effect only after 'reboot' (a reconnect)."""

    def __init__(self, presets, stubborn = ()):
        self.presets, self.stubborn, self.writes = dict(presets), set(stubborn), []

    def connect(self, port):
        bench = self
        link = radio_link(bench.presets[port])
        link.close = lambda: None

        def write(section):
            bench.writes.append((port, section))
            if port not in bench.stubborn:
                bench.presets[port] = LORA.ModemPreset.Name(link.localNode.localConfig.lora.modem_preset)
        link.localNode.writeConfig = write
        return link


class SetPreset(unittest.TestCase):
    def test_both_nodes_end_up_on_the_new_preset(self):
        bench = FakeNodes({"COM5": "LONG_FAST", "COM6": "LONG_FAST"})
        with mock.patch.object(set_preset.time, "sleep"):
            ok = set_preset.SetPresets(["COM5", "COM6"], "short_fast", bench.connect, log = lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(bench.presets, {"COM5": "SHORT_FAST", "COM6": "SHORT_FAST"})
        self.assertEqual(sorted(bench.writes), [("COM5", "lora"), ("COM6", "lora")])

    def test_a_node_that_did_not_take_it_is_reported(self):
        bench = FakeNodes({"COM5": "LONG_FAST", "COM6": "LONG_FAST"}, stubborn = ["COM6"])
        with mock.patch.object(set_preset.time, "sleep"):
            ok = set_preset.SetPresets(["COM5", "COM6"], "SHORT_FAST", bench.connect, log = lambda *_: None)
        self.assertFalse(ok)

    def test_a_bad_preset_name_changes_nothing(self):
        bench = FakeNodes({"COM5": "LONG_FAST"})
        with self.assertRaises(ValueError):
            set_preset.SetPresets(["COM5"], "WARP_SPEED", bench.connect, log = lambda *_: None)
        self.assertEqual(bench.writes, [])

    def test_every_preset_we_know_the_timing_of_is_a_real_preset(self):
        for name in MeshSend.PRESETS:
            LORA.ModemPreset.Value(name)                     # raises if Meshtastic has no such name


if __name__ == "__main__":
    unittest.main()
