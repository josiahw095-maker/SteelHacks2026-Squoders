"""Fix 3: packets go to one node, not to everyone.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "endpoint"))

import MeshCodec
import MeshSend
from test_link import AckLink, FakeLink, packets, quiet, radio_link

PEER = "!aabbccdd"


class Addressing(unittest.TestCase):
    def send(self, link, **kwargs):
        return quiet(MeshSend.send_packets, link, packets(), gap = 0, **kwargs)

    def test_normalize_accepts_the_usual_spellings(self):
        for text in ("!435c4ce4", "435c4ce4", "!435C4CE4", " !435c4ce4 "):
            self.assertEqual(MeshSend.normalize_dest(text), "!435c4ce4")

    def test_normalize_rejects_names_ports_and_junk(self):
        for bad in ("COM5", "", None, "!xyz12345", "short", "!1234567", "!123456789",
                    "Meshtastic 23c0", "!435c4ce4;drop"):
            with self.assertRaises(ValueError, msg = repr(bad)):
                MeshSend.normalize_dest(bad)

    def test_a_destination_is_used_for_every_packet(self):
        link = FakeLink()
        self.send(link, dest = "!AABBCCDD")
        self.assertGreaterEqual(len(link.calls), 3)
        self.assertTrue(all(c["dest"] == PEER and c["ack"] for c in link.calls))

    def test_no_destination_still_broadcasts(self):
        link = FakeLink()
        self.send(link)
        self.assertTrue(all(c["dest"] is None for c in link.calls))

    def test_a_bad_destination_fails_before_anything_is_sent(self):
        link = FakeLink()
        with self.assertRaises(ValueError):
            self.send(link, dest = "Meshtastic 23c0")
        self.assertEqual(link.calls, [])

    def test_the_librarys_sys_exit_becomes_an_ordinary_error(self):
        class Exiting(FakeLink):
            def sendData(self, *a, **k):
                raise SystemExit(1)
        with self.assertRaises(RuntimeError):
            self.send(Exiting(), dest = PEER)

    def test_progress_is_reported_after_each_packet(self):
        seen = []
        self.send(FakeLink(), dest = PEER, on_sent = lambda pos, total: seen.append((pos, total)))
        total = seen[0][1]
        self.assertEqual(seen, [(i, total) for i in range(1, total + 1)])


class PickingAPeer(unittest.TestCase):
    NOW = 1_000_000

    def setUp(self):
        MeshSend.default_dest = None
        MeshSend.forget_peer()

    tearDown = setUp

    def node(self, name, age):
        return {"user": {"longName": name}, "lastHeard": self.NOW - age}

    def link(self, **nodes):
        return FakeLink({f"!{k}": v for k, v in nodes.items()})

    def test_an_id_given_outright_wins_over_everything(self):
        MeshSend.default_dest = "!11111111"
        MeshSend.note_peer("!22222222", now = self.NOW)
        self.assertEqual(MeshSend.pick_dest(FakeLink(), explicit = "!33333333", now = self.NOW), "!33333333")

    def test_a_pinned_destination_beats_learning_and_guessing(self):
        MeshSend.default_dest = "44444444"
        MeshSend.note_peer("!22222222", now = self.NOW)
        self.assertEqual(MeshSend.pick_dest(FakeLink(), now = self.NOW), "!44444444")

    def test_the_node_we_last_heard_from_is_used(self):
        MeshSend.note_peer("!AABBCCDD", now = self.NOW)
        self.assertEqual(MeshSend.pick_dest(FakeLink(), now = self.NOW + 60), PEER)

    def test_a_learned_peer_expires(self):
        MeshSend.note_peer(PEER, now = self.NOW)
        self.assertIsNone(MeshSend.pick_dest(FakeLink(), now = self.NOW + MeshSend.LEARNED_MAX_AGE + 1))

    def test_garbage_is_never_learned(self):
        for bad in (None, "", "COM5", "!zz"):
            MeshSend.note_peer(bad, now = self.NOW)
        self.assertIsNone(MeshSend.pick_dest(FakeLink(), now = self.NOW))

    def test_one_fresh_node_among_dozens_of_stale_ones_is_found(self):
        """The NodeDB keeps every node ever heard on any channel; yours held 30+ from the public mesh."""
        nodes = {f"{i:08x}": self.node(f"stale {i}", 86_400 + i) for i in range(33)}
        nodes["aabbccdd"] = self.node("Warons2", 30)
        nodes["11111111"] = self.node("me", 5)                    # FakeLink says it is !11111111
        self.assertEqual(MeshSend.pick_dest(self.link(**nodes), now = self.NOW), PEER)

    def test_two_fresh_nodes_are_ambiguous_so_broadcast(self):
        link = self.link(aabbccdd = self.node("A", 30), bbccddee = self.node("B", 40))
        self.assertIsNone(MeshSend.pick_dest(link, now = self.NOW))

    def test_only_stale_nodes_means_broadcast(self):
        self.assertIsNone(MeshSend.pick_dest(self.link(aabbccdd = self.node("A", 99_999)), now = self.NOW))

    def test_a_node_we_have_no_heard_time_for_is_not_trusted(self):
        self.assertIsNone(MeshSend.pick_dest(self.link(aabbccdd = {"user": {"longName": "A"}}), now = self.NOW))

    def test_our_own_node_is_never_the_peer(self):
        link = self.link(**{"11111111": self.node("me", 5)})
        self.assertIsNone(MeshSend.pick_dest(link, now = self.NOW))


class GatewayUsesIt(unittest.TestCase):
    def setUp(self):
        MeshSend.default_dest = None
        MeshSend.forget_peer()

    tearDown = setUp

    def test_a_resend_goes_to_the_peer_not_the_channel(self):
        import MessageReply
        pk = packets(mid = "gw-resend")
        MeshSend.remember_sent(pk)
        group = int.from_bytes(pk[0][:2], "big")
        MeshSend.note_peer(PEER)
        link = AckLink()
        with mock.patch.object(MeshSend.time, "sleep"):
            sent = quiet(MessageReply.Resend, link, {"thread": group, "request": True, "body": "0,1"})
        self.assertEqual(sent, 2)
        self.assertEqual([c["dest"] for c in link.calls], [PEER, PEER])
        self.assertEqual([c["packet"] for c in link.calls], [pk[0], pk[1]])      # two different packets, not one retried

    def test_receiving_a_reply_teaches_the_gateway_who_the_endpoint_is(self):
        import MessageReply
        MessageReply._groups.clear()
        MessageReply._replies.clear()
        for pkt in MeshCodec.reply_packets("thread-x", "hello from the phone"):
            MessageReply.OnReceive({"fromId": "!AABBCCDD", "decoded": {"portnum": "PRIVATE_APP", "payload": pkt}})
        self.assertEqual(MeshSend.pick_dest(FakeLink()), PEER)

    def test_junk_from_a_stranger_does_not_teach_it_anything(self):
        import MessageReply
        MessageReply._groups.clear()
        MessageReply.OnReceive({"fromId": "!99999999",
                                "decoded": {"portnum": "PRIVATE_APP", "payload": b"\x01\x02\x03\x04\x05"}})
        MessageReply.OnReceive({"fromId": "!99999999",
                                "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi"}})
        self.assertIsNone(MeshSend.pick_dest(FakeLink()))

    def test_command_line_dest_is_split_from_the_positional_arguments(self):
        import MessagePing
        self.assertEqual(MessagePing.SplitArgs(["acct", "COM5", "--dest", "!AABBCCDD"], {}),
                         (["acct", "COM5"], "!AABBCCDD"))
        self.assertEqual(MessagePing.SplitArgs(["--dest", "!aabbccdd", "acct", "COM5"], {}),
                         (["acct", "COM5"], "!aabbccdd"))
        self.assertEqual(MessagePing.SplitArgs(["acct", "--dry-run"], {}), (["acct", "--dry-run"], None))

    def test_environment_variable_is_the_fallback(self):
        import MessagePing
        self.assertEqual(MessagePing.SplitArgs(["acct"], {"MESH_DEST": "!aabbccdd"})[1], "!aabbccdd")
        self.assertEqual(MessagePing.SplitArgs(["acct", "--dest", "!11111111"], {"MESH_DEST": "!aabbccdd"})[1],
                         "!11111111")

    def test_dest_without_a_value_is_an_error(self):
        import MessagePing
        with self.assertRaises(ValueError):
            MessagePing.SplitArgs(["acct", "--dest"], {})


class EndpointAddressing(unittest.TestCase):
    """The endpoint learns the gateway from its first email and then talks to it directly."""

    def station(self):
        from station import Station
        return Station(mock = True)

    def email(self, mid = "ep1"):
        return packets(count = 1, body_len = 60, mid = mid)

    def test_it_starts_by_broadcasting(self):
        st = self.station()
        st.link = FakeLink()
        quiet(st.Send, self.email(), gap = 0)
        self.assertTrue(all(c["dest"] is None for c in st.link.calls))

    def test_a_received_email_teaches_it_the_gateway(self):
        st = self.station()
        for pkt in self.email():
            st.Accept(pkt, source = "!AABBCCDD")
        self.assertEqual(st.peer, PEER)

    def test_a_partial_email_teaches_it_nothing_yet(self):
        st = self.station()
        st.Accept(packets()[0], source = PEER)
        self.assertIsNone(st.peer)

    def test_its_own_send_heard_back_is_not_the_gateway(self):
        st = self.station()
        for pkt in MeshCodec.reply_packets("t", "hi"):
            st.Accept(pkt, source = "!11111111")
        self.assertIsNone(st.peer)

    def test_a_bad_source_id_is_ignored(self):
        st = self.station()
        for pkt in self.email():
            st.Accept(pkt, source = "not an id")
        self.assertIsNone(st.peer)

    def test_replies_then_go_to_the_gateway_directly(self):
        st = self.station()
        for pkt in self.email():
            st.Accept(pkt, source = PEER)
        st.link = AckLink()
        with mock.patch.object(MeshSend.time, "sleep"):
            quiet(st.Reply, 1234, "on my way")
        self.assertTrue(st.link.calls and all(c["dest"] == PEER for c in st.link.calls))

    def test_resend_requests_are_addressed_too(self):
        st = self.station()
        st.peer = PEER
        st.link = AckLink()
        quiet(st.Send, MeshCodec.request_packets(0xABCD, [1]), gap = 0)
        self.assertEqual([c["dest"] for c in st.link.calls], [PEER])

    def test_progress_reaches_the_screen(self):
        st = self.station()
        st.link = FakeLink()
        seen = []
        quiet(st.Send, packets(), gap = 0, on_progress = lambda sent, total: seen.append((sent, total)))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual([e["kind"] for e in st.Feed()].count("tx"), len(seen))

    def test_it_no_longer_uses_a_hard_coded_two_second_gap(self):
        st = self.station()
        st.link = radio_link("SHORT_FAST")
        st.link.sendData = lambda *a, **k: None
        with mock.patch.object(MeshSend.time, "sleep") as sleep:
            quiet(st.Send, packets())
        self.assertTrue(sleep.call_args_list)
        self.assertTrue(all(c.args[0] == MeshSend.pace(st.link) for c in sleep.call_args_list))
        self.assertLess(MeshSend.pace(st.link), 2.0)


if __name__ == "__main__":
    unittest.main()
