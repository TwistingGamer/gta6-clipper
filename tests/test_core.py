import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from clipbot import chat, detect
from clipbot.captions import ass_ts, overlay_ass, srt_time
from clipbot.sources import Source, load_sources


class TimeFormatTest(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(srt_time(61.235), "00:01:01,235")

    def test_ass_time(self):
        self.assertEqual(ass_ts(61.23), "0:01:01.23")


class WindowTest(unittest.TestCase):
    def test_non_overlapping(self):
        sig = {"audio": [.1, .2, .7, .1, .3, .95, .1, .2, .3, .4, .1, .2]}
        picked = detect.pick_windows(150, 36, sig, 2, 0.0)
        self.assertEqual(len(picked), 2)
        self.assertGreaterEqual(abs(picked[0].start - picked[1].start), 28)
        self.assertEqual(picked[0].start, 50.0)

    def test_too_short(self):
        self.assertEqual(detect.pick_windows(5, 36, {"audio": [.8]}, 2, 0), [])

    def test_chat_outweighs_audio(self):
        sig = {"audio": [.6, .0, .0, .0], "chat": [.0, .0, .0, .9]}
        self.assertEqual(detect.pick_windows(70, 36, sig, 1, 0)[0].start, 30.0)

    def test_clip_scores(self):
        out = detect.clip_scores([(1000 + 45, 500)], 1000, 120, 36)
        self.assertGreater(out[2], 0.5)
        self.assertEqual(out[0], 0.0)


class SourceTest(unittest.TestCase):
    def test_requires_evidence(self):
        self.assertFalse(Source("a", permission_type="campaign").authorized()[0])

    def test_expired(self):
        s = Source("a", permission_type="written", evidence="dm", expires_on="2020-01-01")
        self.assertIn("expired", s.authorized(date(2026, 1, 1))[1])

    def test_inactive(self):
        s = Source("a", permission_type="written", evidence="dm", active=False)
        self.assertFalse(s.authorized()[0])

    def test_ok(self):
        s = Source("a", permission_type="campaign", evidence="https://x")
        self.assertTrue(s.authorized()[0])

    def test_example_file_is_inactive(self):
        srcs = load_sources(Path(__file__).resolve().parents[1] / "sources.example.yaml")
        self.assertIn("example_streamer", srcs)
        self.assertFalse(srcs["example_streamer"].authorized()[0])
        self.assertTrue(srcs["example_streamer"].ad_disclosure)


class ChatTest(unittest.TestCase):
    def test_spike_detected(self):
        login = f"unittest_{int(time.time())}"
        path = chat.chat_file(login)
        start = 1_000_000.0
        try:
            with path.open("w") as fh:
                for s in range(120):
                    n = 12 if 60 <= s < 70 else 1
                    for _ in range(n):
                        fh.write(json.dumps({"t": start + s, "m": "KEKW" if n > 1 else "hello"}) + "\n")
            scores = chat.chat_scores(login, start, 120)
            self.assertIsNotNone(scores)
            best = max(range(len(scores)), key=scores.__getitem__)
            self.assertIn(best * 10, (30, 40, 50, 60))
        finally:
            path.unlink(missing_ok=True)


class OverlayTest(unittest.TestCase):
    def test_ad_label_and_no_hook(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "o.ass"
            overlay_ass(p, 30, "", "twitch.tv/x", "Paid partnership #ad")
            text = p.read_text()
            self.assertIn("Paid partnership #ad", text)
            self.assertNotIn(",Hook,,", text)


if __name__ == "__main__":
    unittest.main()
