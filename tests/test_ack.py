"""Fix 4: send each packet when the last one is acknowledged, not after a fixed sleep.

    python -m unittest discover -s tests -v
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "endpoint"))

import MeshSend
import MockRadio
from test_link import AckLink, FakeLink, packets, quiet, radio_link

PEER = "!aabbccdd"


def send(link, pk = None, **kwargs):
    return quiet(MeshSend.send_packets, link, pk or packets(), dest = PEER, wait_ack = True, **kwargs)


class WaitingForAcks(unittest.TestCase):
    def setUp(self):
        MeshSend.forget_peer()
        MeshSend.default_dest = None

    tearDown = setUp

    def test_it_asks_the_library_to_report_acks(self):
        link = AckLink()
        send(link)
        for call in link.calls:
            self.assertTrue(call["ack"])
            self.assertTrue(callable(call["onResponse"]))
            self.assertIs(call["onResponseAckPermitted"], True)     # without this the library hides acks from us

    def test_every_packet_is_confirmed(self):
        pk = packets()
        link = AckLink()
        self.assertEqual(send(link, pk), len(pk))
        self.assertEqual([c["packet"] for c in link.calls], pk)

    def test_the_next_packet_waits_for_the_previous_ack(self):
        link = AckLink(delay = 0.1)
        send(link)
        gaps = [b - a for a, b in zip(link.times, link.times[1:])]
        self.assertTrue(gaps and all(g >= 0.09 for g in gaps), gaps)

    def test_it_takes_only_as_long_as_the_acks_do(self):
        pk = packets()
        link = AckLink(delay = 0.05)
        start = time.monotonic()
        send(link, pk)
        took = time.monotonic() - start
        fixed = (len(pk) - 1) * MeshSend.SEND_GAP_SECONDS
        self.assertLess(took, 1.0)
        self.assertGreater(fixed / took, 10, f"ack-paced {took:.2f}s vs fixed-gap {fixed:.0f}s")

    def test_the_pause_after_an_ack_is_tiny(self):
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            send(AckLink())
        self.assertTrue(sleep.call_args_list)
        self.assertTrue(all(c.args[0] == MeshSend.ACK_GAP for c in sleep.call_args_list))

    def test_an_explicit_gap_is_still_honoured_when_waiting(self):
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            send(AckLink(), gap = 1.5)
        self.assertTrue(all(c.args[0] == 1.5 for c in sleep.call_args_list))


class WhenPacketsFail(unittest.TestCase):
    def setUp(self):
        MeshSend.forget_peer()
        MeshSend.default_dest = None

    tearDown = setUp

    def test_a_nak_is_retried_with_the_same_packet(self):
        pk = packets()
        link = AckLink(script = lambda n: "MAX_RETRANSMIT" if n == 1 else "NONE")
        self.assertEqual(send(link, pk), len(pk))
        self.assertEqual(len(link.calls), len(pk) + 1)
        self.assertEqual(link.calls[1]["packet"], link.calls[2]["packet"])

    def test_it_gives_up_after_the_tries_and_leaves_the_rest_unsent(self):
        pk = packets()
        link = AckLink(script = lambda n: "NONE" if n == 0 else "MAX_RETRANSMIT")
        self.assertEqual(send(link, pk), 1)
        self.assertEqual(len(link.calls), 1 + MeshSend.ACK_TRIES)
        self.assertNotIn(pk[2], [c["packet"] for c in link.calls])

    def test_silence_times_out_and_retries(self):
        link = AckLink(script = lambda n: "SILENT")
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.05):
            self.assertEqual(send(link), 0)
        self.assertEqual(len(link.calls), MeshSend.ACK_TRIES)

    def test_a_late_ack_for_an_earlier_attempt_is_not_taken_for_the_next(self):
        # Attempt 1 is acked, but 0.15 s late (after its 0.05 s timeout). Attempt 2 is refused.
        # If the late ack were mistaken for attempt 2's, the packet would count as delivered.
        link = AckLink(script = lambda n: ("NONE", 0.15) if n == 0 else "MAX_RETRANSMIT")
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.05):
            self.assertEqual(send(link), 0)
        time.sleep(0.25)                                    # let the late ack fire; it must do no harm
        self.assertEqual(len(link.calls), 2)

    def test_on_sent_is_only_called_for_packets_that_arrived(self):
        seen = []
        link = AckLink(script = lambda n: "NONE" if n == 0 else "NO_ROUTE")
        send(link, on_sent = lambda pos, total: seen.append((pos, total)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 1)

    def test_a_failed_peer_is_forgotten_so_the_next_message_does_not_keep_trying_it(self):
        MeshSend.note_peer(PEER)
        send(AckLink(script = lambda n: "MAX_RETRANSMIT"))
        self.assertIsNone(MeshSend.pick_dest(FakeLink()))

    def test_a_pinned_destination_survives_a_failure(self):
        MeshSend.default_dest = PEER
        send(AckLink(script = lambda n: "MAX_RETRANSMIT"))
        self.assertEqual(MeshSend.pick_dest(FakeLink()), PEER)

    def test_the_failure_is_printed_so_the_operator_can_see_it(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            MeshSend.send_packets(AckLink(script = lambda n: "MAX_RETRANSMIT"), packets(),
                                  dest = PEER, wait_ack = True)
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("MAX_RETRANSMIT", out.getvalue())


class WhenAcksDoNotApply(unittest.TestCase):
    def test_a_broadcast_is_never_waited_for(self):
        link = AckLink(script = lambda n: "SILENT")                 # would hang if it waited
        pk = packets()
        result = quiet(MeshSend.send_packets, link, pk, dest = None, wait_ack = True, gap = 0)
        self.assertEqual(result, len(pk))
        self.assertTrue(all(c.get("onResponse") is None for c in link.calls))

    def test_without_wait_ack_the_fixed_pacing_is_unchanged(self):
        link = AckLink()
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            quiet(MeshSend.send_packets, link, packets(), dest = PEER)
        self.assertTrue(all(c.args[0] == MeshSend.SEND_GAP_SECONDS for c in sleep.call_args_list))
        self.assertTrue(all(c.get("onResponse") is None for c in link.calls))

    def test_a_dry_run_does_not_wait(self):
        pk = packets()
        self.assertEqual(quiet(MeshSend.send_packets, None, pk, dest = PEER, wait_ack = True), len(pk))


class Timeouts(unittest.TestCase):
    def test_a_slow_radio_is_given_longer_than_a_fast_one(self):
        slow = MeshSend.ack_timeout(radio_link("LONG_FAST"))
        fast = MeshSend.ack_timeout(radio_link("SHORT_FAST"))
        self.assertTrue(15 < slow < 25, slow)
        self.assertEqual(fast, MeshSend.MIN_ACK_WAIT)

    def test_an_unreadable_radio_gets_the_cautious_default(self):
        self.assertEqual(MeshSend.ack_timeout(FakeLink()), MeshSend.UNKNOWN_ACK_WAIT)

    def test_a_link_can_state_its_own(self):
        link = FakeLink()
        link.ack_hint = 0.5
        self.assertEqual(MeshSend.ack_timeout(link), 0.5)


class WithTheMockRadio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix = "spool-ack-"))
        self.saved = (MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP)
        MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP = self.tmp, self.tmp / "down", self.tmp / "up"

    def tearDown(self):
        MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP = self.saved
        shutil.rmtree(self.tmp, ignore_errors = True)

    def test_the_folder_radio_acknowledges_immediately(self):
        pk = packets()
        link = MockRadio.LoopbackInterface("down")
        start = time.monotonic()
        result = quiet(MeshSend.send_packets, link, pk, dest = PEER, wait_ack = True)
        self.assertEqual(result, len(pk))
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(len(list((self.tmp / "down").glob("*.pkt"))), len(pk))


class EndpointReportsFailure(unittest.TestCase):
    def station(self):
        from station import Station
        return Station(mock = True)

    def test_an_unanswered_reply_raises_instead_of_pretending_it_was_sent(self):
        from station import SendFailed
        st = self.station()
        st.peer, st.link = PEER, AckLink(script = lambda n: "SILENT")
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.02):
            with self.assertRaises(SendFailed) as caught:
                quiet(st.Reply, 1234, "hello")
        self.assertIn("acknowledged", str(caught.exception))
        self.assertEqual(st.sent, [], "a reply that never got through must not be recorded as sent")

    def test_after_a_failure_it_stops_addressing_the_gateway(self):
        from station import SendFailed
        st = self.station()
        st.peer, st.link = PEER, AckLink(script = lambda n: "SILENT")
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.02):
            with self.assertRaises(SendFailed):
                quiet(st.Reply, 1234, "hello")
        self.assertIsNone(st.peer)
        self.assertTrue(any("acknowledged" in e["text"] for e in st.Feed()))

    def test_a_delivered_reply_is_recorded(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink()
        with mock.patch.object(MeshSend.time, "sleep"):
            self.assertEqual(quiet(st.Reply, 1234, "hello"), 1)
        self.assertEqual(len(st.sent), 1)

    def test_a_failed_resend_request_does_not_break_the_page_refresh(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink(script = lambda n: "SILENT")
        st.Accept(packets()[0])                                      # first packet of a longer email: it stalls
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.02):
            asks = quiet(st.Chase, now = time.time() + 60)           # must not raise
        self.assertEqual(len(asks), 1)

    def test_the_web_server_turns_a_failed_send_into_an_error_message(self):
        import server
        st = self.station()
        st.peer, st.link = PEER, AckLink(script = lambda n: "SILENT")
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.02):
            result = quiet(server.Send, "Sending reply", lambda tick: st.Reply(1234, "hi", on_progress = tick))
        self.assertFalse(result["ok"])
        self.assertIn("acknowledged", result["error"])


if __name__ == "__main__":
    unittest.main()
