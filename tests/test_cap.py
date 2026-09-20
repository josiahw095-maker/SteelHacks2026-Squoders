"""Fix 5: an email is capped at 6 packets, and trimmed no more than it has to be.

    python -m unittest discover -s tests -v
"""
import random
import string
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import MeshCodec
import MeshSend
from test_link import radio_link

LIMIT = MeshCodec.MAX_CHUNKS * (MeshCodec.MAX_PAYLOAD - MeshCodec.HEADER)
ELLIPSIS_BYTES = len(MeshCodec.ELLIPSIS.encode())


def scrambled(n, seed = 9):
    rnd = random.Random(seed)
    return "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(n))


def email(body, **fields):
    base = {"id": "cap1", "thread": "t", "reply": False, "date": 1789561200,
            "sender": "Ann", "subject": "Big one", "body": body}
    base.update(fields)
    return base


def kept(data):
    """Bytes of body text that survived, not counting the ellipsis."""
    body = MeshCodec.decode_message(data)["body"]
    return len(body.encode()) - (ELLIPSIS_BYTES if body.endswith(MeshCodec.ELLIPSIS) else 0)


def old_encode(body):
    """The trimming this replaces: shave 10% and try again."""
    while True:
        data = MeshCodec.pack("Ann", "S", 29833333, 0, body, 0)
        if len(data) <= LIMIT or not body:
            return data
        size = len(body.encode())
        body = MeshCodec.shorten(body, size * 9 // 10) if size > 12 else ""


def best_possible(body):
    """The most text that fits, found the slow obvious way (scan down from the top)."""
    for keep in range(len(body.encode()), 0, -4):
        candidate = MeshCodec.shorten(body, keep)
        if len(MeshCodec.pack("Ann", "S", 29833333, 0, candidate, 0)) <= LIMIT:
            return len(candidate.encode()) - (ELLIPSIS_BYTES if candidate.endswith(MeshCodec.ELLIPSIS) else 0)
    return 0


class TheCap(unittest.TestCase):
    def test_the_limits_are_what_we_decided(self):
        self.assertEqual(MeshCodec.MAX_CHUNKS, 6)
        self.assertLessEqual(MeshCodec.MAX_CHUNKS, MeshCodec.MAX_PARTS)

    def test_a_huge_email_uses_exactly_the_cap(self):
        packets = MeshCodec.to_packets(email(scrambled(300_000)))
        self.assertEqual(len(packets), MeshCodec.MAX_CHUNKS)

    def test_no_packet_is_over_the_size_limit(self):
        for size in (10, 900, 1500, 2500, 9000, 200_000):
            for packet in MeshCodec.to_packets(email(scrambled(size))):
                self.assertLessEqual(len(packet), MeshCodec.MAX_PAYLOAD)

    def test_the_cap_holds_for_every_size_around_the_boundary(self):
        for size in range(1200, 2600, 37):
            packets = MeshCodec.to_packets(email(scrambled(size)))
            self.assertLessEqual(len(packets), MeshCodec.MAX_CHUNKS, size)

    def test_replies_and_new_mail_obey_the_same_cap(self):
        self.assertLessEqual(len(MeshCodec.reply_packets("thread", scrambled(20_000))), MeshCodec.MAX_CHUNKS)
        self.assertLessEqual(len(MeshCodec.compose_packets("a@b.co", "Hi", scrambled(20_000))), MeshCodec.MAX_CHUNKS)

    def test_a_cut_email_says_so(self):
        packets = MeshCodec.to_packets(email(scrambled(20_000)))
        body = MeshCodec.decode_message(MeshCodec.reassemble(packets))["body"]
        self.assertTrue(body.endswith(MeshCodec.ELLIPSIS))

    def test_an_email_that_fits_is_left_alone(self):
        text = "Hello Sam, lunch at noon? " * 20
        body = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(email(text))))["body"]
        self.assertEqual(body, MeshCodec.strip_body(text))

    def test_text_that_compresses_well_is_not_cut_just_for_being_long(self):
        text = "the quick brown fox " * 1000                          # 20 KB, but tiny once compressed
        body = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(email(text))))["body"]
        self.assertEqual(body, MeshCodec.strip_body(text))

    def test_the_longest_wait_is_now_about_fifteen_seconds_with_acks(self):
        link = radio_link("LONG_FAST")
        data = MeshSend.packet_airtime(link)
        ack = MeshSend.airtime(20, *MeshSend.radio_params(link))
        six = 6 * (data + ack) + 5 * MeshSend.ACK_GAP
        fifteen = 15 * (data + ack) + 14 * MeshSend.ACK_GAP
        self.assertLess(six, 20)
        self.assertGreater(fifteen, 35)                               # what the old cap could hold the radio for


class TrimmingKeepsAsMuchAsPossible(unittest.TestCase):
    SIZES = range(1500, 3000, 43)

    def test_it_keeps_nearly_everything_that_could_fit(self):
        for size in self.SIZES:
            body = scrambled(size)
            got = kept(MeshCodec.encode("Ann", "S", 29833333, 0, body, 0))
            best = best_possible(body)
            self.assertGreaterEqual(got, best * 0.98, f"{size} chars: kept {got}, could keep {best}")

    def test_it_is_never_worse_than_shaving_ten_percent(self):
        for size in self.SIZES:
            body = scrambled(size)
            new = kept(MeshCodec.encode("Ann", "S", 29833333, 0, body, 0))
            old = kept(old_encode(body))
            self.assertGreaterEqual(new, old - 8, f"{size} chars: new kept {new}, old kept {old}")

    def test_the_old_way_really_did_throw_text_away(self):
        """Evidence the change was needed: worst case for each, over bodies just past the limit."""
        worst_old = worst_new = 0.0
        for size in self.SIZES:
            body = scrambled(size)
            best = best_possible(body)
            if not best or best >= size:
                continue                                              # this one fit whole
            worst_old = max(worst_old, 1 - kept(old_encode(body)) / best)
            worst_new = max(worst_new, 1 - kept(MeshCodec.encode("Ann", "S", 29833333, 0, body, 0)) / best)
        self.assertGreater(worst_old, 0.05, f"old algorithm lost at most {worst_old:.1%}")
        self.assertLess(worst_new, 0.02, f"new algorithm lost {worst_new:.1%}")

    def test_the_result_still_decodes_and_is_a_prefix_of_the_original(self):
        for size in (1700, 2300, 4000, 25_000):
            text = scrambled(size)
            message = MeshCodec.decode_message(MeshCodec.reassemble(MeshCodec.to_packets(email(text))))
            self.assertTrue(text.startswith(message["body"][:-len(MeshCodec.ELLIPSIS)].rstrip()), size)

    def test_a_multibyte_text_is_never_cut_mid_character(self):
        text = ("日本語のメール\U0001F600 " * 400)
        packets = MeshCodec.to_packets(email(text))
        body = MeshCodec.decode_message(MeshCodec.reassemble(packets))["body"]         # raises if a character was split
        self.assertLessEqual(len(packets), MeshCodec.MAX_CHUNKS)
        self.assertTrue(body)

    def test_a_million_characters_is_still_quick(self):
        body = " ".join(scrambled(6, seed = i) for i in range(150_000))
        start = time.time()
        packets = MeshCodec.to_packets(email(body))
        took = time.time() - start
        self.assertLess(took, 1.0, f"{took:.2f}s")
        self.assertEqual(len(packets), MeshCodec.MAX_CHUNKS)


if __name__ == "__main__":
    unittest.main()
