"""Timing diagnostics: when was each packet sent, acked and received?

    python -m unittest discover -s tests -v
"""
import contextlib
import io
import re
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

import MeshCodec
import MeshSend
import MockRadio
from test_link import AckLink, FakeLink, packets, quiet

PEER = "!aabbccdd"
CLOCK_TIME = re.compile(r"\d\d:\d\d:\d\d\.\d{3}")


def group_of(pk):
    return int.from_bytes(pk[0][:2], "big")


def capture(call, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = call(*args, **kwargs)
    return result, out.getvalue()


def email(want_timing = False, body_len = 700):
    import random, string
    rnd = random.Random(4)
    body = "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(body_len))
    return {"id": "tm1", "thread": "t", "reply": False, "date": 1789561200, "sender": "A",
            "subject": "S", "body": body, "want_timing": want_timing}


class Codec(unittest.TestCase):
    def test_a_request_for_timing_survives_the_round_trip(self):
        message = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(email(True))))
        self.assertTrue(message["want_timing"])
        self.assertFalse(message["timing"] or message["outbound"] or message["request"])
        self.assertEqual(message["sender"], "A")
        self.assertEqual(message["subject"], "S")

    def test_mail_does_not_ask_for_timing_unless_told_to(self):
        message = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(email(False))))
        self.assertFalse(message["want_timing"])

    def test_the_new_flags_do_not_disturb_the_old_ones(self):
        flags = {"REPLY": MeshCodec.FLAG_REPLY, "OUT": MeshCodec.FLAG_OUTBOUND, "REQ": MeshCodec.FLAG_REQUEST,
                 "DEFLATE": MeshCodec.FLAG_DEFLATE, "WANT": MeshCodec.FLAG_WANT_TIMING, "TIMING": MeshCodec.FLAG_TIMING}
        self.assertEqual(len(set(flags.values())), len(flags))              # all distinct
        for value in flags.values():
            self.assertEqual(bin(value).count("1"), 1)                      # each a single bit
        both = email(True)
        both["reply"] = True
        message = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(both)))
        self.assertTrue(message["reply"] and message["want_timing"])

    def test_a_timing_report_round_trips_with_millisecond_offsets(self):
        arrivals = {0: 1000.000, 1: 1002.512, 2: 1005.031}
        packets_ = MeshCodec.timing_packets(0xB530, arrivals)
        self.assertEqual(len(packets_), 1)
        message = MeshCodec.decode_message(MeshCodec.reassemble(packets_))
        self.assertTrue(message["timing"] and message["outbound"])
        self.assertFalse(message["request"] or message["reply"])
        self.assertEqual(message["thread"], 0xB530)
        self.assertEqual(MeshCodec.parse_timing(message), (1000000, {0: 0, 1: 2512, 2: 5031}))

    def test_a_report_for_the_longest_email_still_fits_one_packet(self):
        arrivals = {part: 1_790_000_000.123 + part * 19.7 for part in range(MeshCodec.MAX_PARTS)}
        packets_ = MeshCodec.timing_packets(0x1234, arrivals)
        self.assertEqual(len(packets_), 1)
        self.assertLessEqual(len(packets_[0]), MeshCodec.MAX_PAYLOAD)
        offsets = MeshCodec.parse_timing(MeshCodec.decode_message(MeshCodec.reassemble(packets_)))[1]
        self.assertEqual(len(offsets), MeshCodec.MAX_PARTS)

    def test_a_garbled_report_reads_as_nothing_rather_than_crashing(self):
        for body in ("", "not numbers", "123 x:y", "123 4", "abc 1:2"):
            self.assertIsNone(MeshCodec.parse_timing({"body": body}), repr(body))


class SenderLog(unittest.TestCase):
    def setUp(self):
        MeshSend.forget_peer()
        MeshSend.default_dest = None

    tearDown = setUp

    def send(self, link, pk = None, **kwargs):
        pk = pk or packets()
        result, out = capture(MeshSend.send_packets, link, pk, dest = PEER, wait_ack = True, **kwargs)
        return result, out, pk

    def test_the_time_format_is_hours_minutes_seconds_and_milliseconds(self):
        self.assertRegex(MeshSend.stamp(time.time()), r"^\d\d:\d\d:\d\d\.\d{3}$")
        self.assertTrue(MeshSend.stamp(1000.123).endswith(".123"))
        self.assertTrue(MeshSend.stamp(1000.9996).endswith(".999"))         # never rounds up into the next second

    def test_every_packet_prints_when_it_was_sent_and_when_its_ack_came_back(self):
        _, out, pk = self.send(AckLink(delay = 0.05))
        sent = [line for line in out.splitlines() if line.strip().startswith("sent ")]
        acked = [line for line in out.splitlines() if line.strip().startswith("acked ")]
        self.assertEqual((len(sent), len(acked)), (len(pk), len(pk)))
        for line in sent + acked:
            self.assertRegex(line, CLOCK_TIME)

    def test_the_ack_line_says_how_long_the_ack_took(self):
        _, out, _ = self.send(AckLink(delay = 0.1))
        waits = [float(m) for m in re.findall(r"\(\+(\d+\.\d+) s", out)]
        self.assertTrue(waits)
        for wait in waits:
            self.assertTrue(0.09 <= wait < 0.6, wait)

    def test_the_record_holds_real_increasing_times(self):
        pk = packets()
        self.send(AckLink(delay = 0.05), pk)
        entries = MeshSend.timeline_for(group_of(pk))
        self.assertEqual([e["part"] for e in entries], list(range(len(pk))))
        for e in entries:
            self.assertEqual((e["outcome"], e["attempts"]), ("NONE", 1))
            self.assertGreaterEqual(e["acked_at"] - e["sent_at"], 0.045)
        sent = [e["sent_at"] for e in entries]
        self.assertEqual(sent, sorted(sent))

    def test_the_ack_time_is_when_it_arrived_not_when_we_next_looked(self):
        pk = packets()
        self.send(AckLink(delay = 0.2), pk)
        wait = MeshSend.timeline_for(group_of(pk))[0]
        self.assertTrue(0.19 <= wait["acked_at"] - wait["sent_at"] <= 0.5)

    def test_a_retry_is_recorded_and_printed(self):
        pk = packets()
        link = AckLink(script = lambda n: "MAX_RETRANSMIT" if n == 1 else "NONE")
        _, out, _ = self.send(link, pk)
        entry = MeshSend.timeline_for(group_of(pk))[1]
        self.assertEqual(entry["attempts"], 2)
        self.assertIn("after 2 tries", out)

    def test_a_failure_is_recorded_without_an_ack_time(self):
        pk = packets()
        self.send(AckLink(script = lambda n: "NONE" if n == 0 else "MAX_RETRANSMIT"), pk)
        entries = MeshSend.timeline_for(group_of(pk))
        self.assertEqual(entries[-1]["outcome"], "MAX_RETRANSMIT")
        self.assertIsNone(entries[-1]["acked_at"])
        self.assertIsNotNone(entries[-1]["sent_at"])

    def test_without_acks_the_send_times_are_still_logged(self):
        pk = packets()
        _, out = capture(MeshSend.send_packets, FakeLink(), pk, dest = PEER, gap = 0)
        self.assertEqual(len(CLOCK_TIME.findall(out)), len(pk))
        for e in MeshSend.timeline_for(group_of(pk)):
            self.assertEqual(e["outcome"], "SENT")
            self.assertIsNone(e["acked_at"])

    def test_a_resend_leaves_the_original_record_alone(self):
        pk = packets(mid = "tm-resend")
        self.send(AckLink(), pk)
        original = MeshSend.timeline_for(group_of(pk))
        with mock.patch.object(MeshSend.time, "sleep"):
            quiet(MeshSend.resend, AckLink(), group_of(pk), {1}, dest = PEER, wait_ack = True)
        self.assertIs(MeshSend.timeline_for(group_of(pk)), original)

    def test_the_record_is_bounded(self):
        for i in range(MeshSend.RECENT_LIMIT + 10):
            quiet(MeshSend.send_packets, FakeLink(), packets(count = 1, body_len = 30, mid = f"bound{i}"), gap = 0)
        self.assertLessEqual(len(MeshSend._timelines), MeshSend.RECENT_LIMIT)

    def test_an_unknown_message_has_no_record(self):
        self.assertIsNone(MeshSend.timeline_for(0xFFFF))


class EndpointRecords(unittest.TestCase):
    """The endpoint notes when each packet arrived and, if asked, reports it."""

    def station(self):
        from station import Station
        return Station(mock = True)

    def arrive(self, st, pk, start = 1000.0, gap = 2.5, source = PEER):
        clock = [start]
        with mock.patch.object(time, "time", lambda: clock[0]):
            for i, packet in enumerate(pk):
                clock[0] = start + i * gap
                capture(st.Accept, packet, source = source)
        return clock[0]

    def test_each_packets_arrival_time_is_recorded(self):
        st, pk = self.station(), packets()
        self.arrive(st, pk)
        arrivals = st.Inbox()[0]["arrivals"]
        self.assertEqual(arrivals, {i: 1000.0 + i * 2.5 for i in range(len(pk))})

    def test_a_duplicate_does_not_overwrite_the_first_arrival(self):
        st, pk = self.station(), packets()
        capture(st.Accept, pk[0])
        first = st.groups[bytes(pk[0][:2])]["times"][0]
        time.sleep(0.02)
        capture(st.Accept, pk[0])
        self.assertEqual(st.groups[bytes(pk[0][:2])]["times"][0], first)

    def test_the_live_feed_says_which_part_arrived(self):
        st, pk = self.station(), packets()
        self.arrive(st, pk)
        texts = [e["text"] for e in st.Feed(limit = 50)]
        self.assertTrue(any(f"part 1/{len(pk)}" in t for t in texts), texts)
        self.assertTrue(any(f"part {len(pk)}/{len(pk)}" in t for t in texts))

    def test_the_console_prints_the_arrival_times_when_a_message_completes(self):
        st, pk = self.station(), packets()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for packet in pk:
                st.Accept(packet)
        self.assertEqual(len(CLOCK_TIME.findall(out.getvalue())), len(pk))

    def test_a_message_that_asks_for_a_report_queues_one(self):
        st = self.station()
        pk = MeshCodec.to_packets(email(True))
        self.arrive(st, pk)
        self.assertEqual(len(st.reports), 1)
        self.assertEqual(st.reports[0]["group"], group_of(pk))
        self.assertEqual(sorted(st.reports[0]["times"]), list(range(len(pk))))

    def test_nothing_is_queued_when_no_report_was_asked_for(self):
        st = self.station()
        self.arrive(st, MeshCodec.to_packets(email(False)))
        self.assertEqual(st.reports, [])

    def test_the_report_is_held_back_so_it_does_not_collide_with_the_last_ack(self):
        from station import REPORT_DELAY
        st = self.station()
        st.peer, st.link = PEER, AckLink()
        pk = MeshCodec.to_packets(email(True))
        self.arrive(st, pk)
        due = st.reports[0]["due"]
        self.assertGreater(due, 0)
        self.assertEqual(quiet(st.FlushReports, now = due - 0.5), 0)
        self.assertEqual(st.link.calls, [])
        with mock.patch.object(MeshSend.time, "sleep"):
            self.assertEqual(quiet(st.FlushReports, now = due + 0.1), 1)
        self.assertEqual(len(st.link.calls), 1)
        self.assertGreaterEqual(REPORT_DELAY, 1.0)

    def test_the_report_carries_the_recorded_times_to_the_gateway(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink()
        pk = MeshCodec.to_packets(email(True))
        self.arrive(st, pk, start = 5000.0, gap = 3.0)
        with mock.patch.object(MeshSend.time, "sleep"):
            quiet(st.FlushReports, now = time.time() + 10_000)
        call = st.link.calls[0]
        self.assertEqual(call["dest"], PEER)
        message = MeshCodec.decode_message(MeshCodec.reassemble([call["packet"]]))
        self.assertTrue(message["timing"])
        first, offsets = MeshCodec.parse_timing(message)
        self.assertEqual(first, 5_000_000)
        self.assertEqual(offsets, {i: i * 3000 for i in range(len(pk))})

    def test_a_report_is_sent_once(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink()
        self.arrive(st, MeshCodec.to_packets(email(True)))
        with mock.patch.object(MeshSend.time, "sleep"):
            self.assertEqual(quiet(st.FlushReports, now = time.time() + 10_000), 1)
            self.assertEqual(quiet(st.FlushReports, now = time.time() + 20_000), 0)

    def test_the_normal_page_refresh_sends_due_reports_too(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink()
        self.arrive(st, MeshCodec.to_packets(email(True)))
        with mock.patch.object(MeshSend.time, "sleep"):
            quiet(st.Chase, now = time.time() + 10_000)
        self.assertEqual(len(st.link.calls), 1)

    def test_a_report_that_cannot_be_delivered_is_dropped_not_retried_forever(self):
        st = self.station()
        st.peer, st.link = PEER, AckLink(script = lambda n: "SILENT")
        self.arrive(st, MeshCodec.to_packets(email(True)))
        with mock.patch.object(MeshSend, "ack_timeout", return_value = 0.02):
            self.assertEqual(quiet(st.FlushReports, now = time.time() + 10_000), 1)
        self.assertEqual(st.reports, [])


class GatewayPrints(unittest.TestCase):
    ACCOUNT = "zz-timing"

    def setUp(self):
        import MessageReply
        MeshSend.forget_peer()
        MeshSend.default_dest = None
        MessageReply._groups.clear()
        MessageReply._replies.clear()

    def report(self, group, arrivals):
        return MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.timing_packets(group, arrivals)))

    def record(self, group, rows):
        MeshSend._timelines[group] = [
            {"part": i, "size": 200, "sent_at": s, "acked_at": a, "outcome": "NONE" if a else "TIMEOUT",
             "attempts": 1} for i, (s, a) in enumerate(rows)]

    def test_the_table_puts_both_machines_times_side_by_side(self):
        import MessageReply
        self.record(0x0AAA, [(100.0, 102.5), (105.0, 107.5), (110.0, 112.5)])
        message = self.report(0x0AAA, {0: 101.0, 1: 104.0, 2: 109.0})
        summary, out = capture(MessageReply.ShowTiming, message)
        for expected in (MeshSend.stamp(100.0), MeshSend.stamp(102.5), MeshSend.stamp(101.0), MeshSend.stamp(109.0)):
            self.assertIn(expected, out)
        rows = [l for l in out.splitlines() if re.match(r"\s+\d+\s+\d\d:", l)]
        self.assertEqual(len(rows), 3)

    def test_the_summary_reports_the_numbers_that_settle_the_argument(self):
        import MessageReply
        self.record(0x0BBB, [(100.0, 102.5), (105.0, 124.0), (130.0, 132.0)])         # part 2 waited 19 s
        message = self.report(0x0BBB, {0: 101.0, 1: 104.0, 2: 129.0})
        summary, out = capture(MessageReply.ShowTiming, message)
        self.assertAlmostEqual(summary["ack_wait_max"], 19.0, places = 2)
        self.assertAlmostEqual(summary["ack_wait_avg"], (2.5 + 19.0 + 2.0) / 3, places = 2)
        self.assertAlmostEqual(summary["tx_gap_max"], 25.0, places = 2)
        self.assertAlmostEqual(summary["rx_gap_max"], 25.0, places = 2)                # what the endpoint saw
        self.assertEqual((summary["parts_reported"], summary["parts_sent"]), (3, 3))
        self.assertIn("longest 19.000 s", out)

    def test_a_part_that_never_arrived_shows_a_dash(self):
        import MessageReply
        self.record(0x0CCC, [(100.0, 102.0), (105.0, None)])
        message = self.report(0x0CCC, {0: 101.0})
        summary, out = capture(MessageReply.ShowTiming, message)
        self.assertEqual((summary["parts_reported"], summary["parts_sent"]), (1, 2))
        rows = {int(l.split()[0]): l.split() for l in out.splitlines() if re.match(r"\s+\d+\s+\d\d:", l)}
        self.assertEqual(rows[2][3:5], ["-", "-"])                 # part 2: no ack wait, never received
        self.assertNotEqual(rows[1][4], "-")                         # part 1 arrived

    def test_a_report_for_a_message_we_did_not_send_still_shows_the_endpoints_times(self):
        import MessageReply
        message = self.report(0x0DDD, {0: 50.0, 1: 52.0})
        summary, out = capture(MessageReply.ShowTiming, message)
        self.assertIn("no send record", out)
        self.assertIn(MeshSend.stamp(52.0), out)
        self.assertEqual(summary["parts_reported"], 2)

    def test_a_garbled_report_is_reported_not_a_crash(self):
        import MessageReply
        message = {"thread": 1, "body": "garbage", "outbound": True, "timing": True}
        result, out = capture(MessageReply.ShowTiming, message)
        self.assertIsNone(result)
        self.assertIn("could not be read", out)

    def test_a_timing_report_is_shown_and_never_emailed(self):
        import MessageReply

        class Service:
            sent = []
            def users(self): return self
            def messages(self): return self
            def send(self, **kw): self.sent.append(kw); return self
            def execute(self): return {"id": "x"}

        message = self.report(0x0EEE, {0: 1.0})
        service = Service()
        capture(MessageReply.Deliver, service, self.ACCOUNT, message)
        self.assertEqual(service.sent, [])

    def test_the_gateway_queues_a_report_like_any_outbound_message(self):
        import MessageReply
        for packet in MeshCodec.timing_packets(0x0FFF, {0: 10.0, 1: 12.5}):
            MessageReply.Collect(packet)
        queued = MessageReply.Drain()
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0]["timing"])

    def test_the_command_line_flag_is_accepted_and_not_mistaken_for_a_port(self):
        import MessagePing
        self.assertEqual(MessagePing.SplitArgs(["acct", "--timing", "COM5"], {}), (["acct", "COM5"], None))
        self.assertEqual(MessagePing.SplitArgs(["acct", "COM5", "--dest", "!aabbccdd", "--timing"], {}),
                         (["acct", "COM5"], "!aabbccdd"))

    def test_the_gateway_loop_really_asks_for_timing_when_the_flag_is_set(self):
        source = (ROOT / "MessagePing.py").read_text(encoding = "utf-8")
        self.assertIn('email["want_timing"] = timing', source)
        self.assertIn('"--timing" in sys.argv', source)


class RoundTrip(unittest.TestCase):
    """The whole loop through the mock radio: send, receive, report, print."""

    def setUp(self):
        import MessageReply
        self.tmp = Path(tempfile.mkdtemp(prefix = "spool-timing-"))
        self.saved = (MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP)
        MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP = self.tmp, self.tmp / "down", self.tmp / "up"
        MessageReply._groups.clear()
        MessageReply._replies.clear()
        MeshSend.forget_peer()

    def tearDown(self):
        MockRadio.SPOOL, MockRadio.DOWN, MockRadio.UP = self.saved
        shutil.rmtree(self.tmp, ignore_errors = True)

    def test_a_timed_email_comes_back_as_a_table_with_a_row_per_packet(self):
        import MessageReply
        from station import Station
        gateway, endpoint = MockRadio.LoopbackInterface("down"), Station(port = "loopback")

        pk = MeshCodec.to_packets(email(True))
        sent, sender_log = capture(MeshSend.send_packets, gateway, pk, dest = PEER, wait_ack = True)
        self.assertEqual(sent, len(pk))
        self.assertEqual(len(re.findall(r"^\s+acked ", sender_log, re.M)), len(pk))

        endpoint.Poll()
        self.assertEqual(len(endpoint.Inbox()), 1)
        with mock.patch.object(MeshSend.time, "sleep"):
            quiet(endpoint.FlushReports, now = time.time() + 60)

        for payload in gateway.Receive():
            MessageReply.Collect(payload)
        reports = MessageReply.Drain()
        self.assertEqual(len(reports), 1)
        summary, table = capture(MessageReply.Deliver, None, "zz-timing", reports[0], False, gateway)

        rows = [l for l in table.splitlines() if re.match(r"\s+\d+\s+\d\d:", l)]
        self.assertEqual(len(rows), len(pk))
        self.assertEqual((summary["parts_reported"], summary["parts_sent"]), (len(pk), len(pk)))
        self.assertIsNotNone(summary["ack_wait_avg"])


if __name__ == "__main__":
    unittest.main()
