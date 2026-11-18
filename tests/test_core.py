import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from clipbot import chat, detect
from clipbot.captions import ass_ts, overlay_ass, srt_time
from clipbot.sources import Campaign, Input, hms, load_campaigns
from clipbot.pipeline import package


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


class CampaignTest(unittest.TestCase):
    def test_requires_evidence(self):
        self.assertFalse(Campaign("a", permission_type="campaign").authorized()[0])

    def test_expired(self):
        c = Campaign("a", permission_type="written", evidence="dm", expires_on="2020-01-01")
        self.assertIn("expired", c.authorized(date(2026, 1, 1))[1])

    def test_inactive(self):
        self.assertFalse(Campaign("a", permission_type="written", evidence="dm", active=False).authorized()[0])

    def test_ok(self):
        self.assertTrue(Campaign("a", permission_type="campaign", evidence="https://x").authorized()[0])

    def test_example_file_all_inactive(self):
        cs = load_campaigns(example=Path(__file__).resolve().parents[1] / "campaigns.example.yaml", path=Path("/nonexistent.yaml"))
        self.assertIn("expedia-ishowspeed", cs)
        self.assertEqual([c.id for c in cs.values() if c.authorized()[0]], ["own"])  # only your own posts are on by default
        self.assertFalse(any(c.problems() for c in cs.values()))
        e = cs["expedia-ishowspeed"]
        self.assertEqual(e.inputs[0].window, (9989.0, None))
        self.assertFalse(e.ai_text_allowed)

    def test_hms(self):
        self.assertEqual(hms("2:46:29"), 9989)
        self.assertIsNone(hms(""))

    def test_post_hours_wrap(self):
        c = Campaign("a", post_hours="20-2")
        self.assertTrue(c.can_post_now(23) and c.can_post_now(1))
        self.assertFalse(c.can_post_now(12))

    def test_priority_prefers_fixed_footage_and_open_budget(self):
        fixed = Campaign("f", inputs=[Input("youtube_video", "u")], cpm=1, max_payout=500)
        live = Campaign("l", inputs=[Input("twitch", "x")], cpm=1, max_payout=500)
        spent = Campaign("s", inputs=[Input("youtube_video", "u")], cpm=5, max_payout=500, budget_total=100, budget_used=100)
        self.assertGreater(fixed.priority(), live.priority())
        self.assertEqual(spent.priority(), 0)

    def test_package_rules(self):
        c = Campaign("e", name="Expedia x Speed", ai_text_allowed=False, title_template="Speed's Expedia trip",
                     required_words=["Expedia"], required_hashtags=["#IShowSpeed"], ad_disclosure=True, sponsor="Expedia")
        title, desc, tags = package(c, {"title": "AI TITLE", "description": "ai", "hook": "x", "hashtags": ["#AI"]})
        self.assertNotIn("AI", title)
        self.assertTrue(title.endswith("#ad"))
        self.assertIn("Expedia", desc)
        self.assertIn("#IShowSpeed", tags)
        self.assertTrue(desc.startswith("#ad Paid partnership with Expedia"))


class CoreRulesTest(unittest.TestCase):
    def test_core_example_config(self):
        c = load_campaigns(example=Path(__file__).resolve().parents[1] / "campaigns.example.yaml", path=Path("/nonexistent.yaml"))["core-pool"]
        self.assertEqual(c.min_members, 2)
        self.assertFalse(c.ai_text_allowed)
        self.assertNotIn("youtube_channel", [i.type for i in c.inputs])  # CORE YouTube goes to the 5K pool
        self.assertEqual(c.expires_on, "2026-11-30")
        title, desc, tags = package(c, {"title": "", "description": "", "hook": "", "hashtags": []}, ["Lacy", "Silky"], "lacy")
        self.assertIn("Lacy & Silky", title)
        self.assertIn("CORE", title + desc)
        for t in ("#CORE", "#Lacy", "#Silky"):
            self.assertIn(t, tags)
        self.assertNotIn("twitch.tv", desc)  # show_credit: false


class ContextTest(unittest.TestCase):
    def test_refine_puts_moment_late_in_clip(self):
        import numpy as np
        audio = np.ones(200, dtype=np.float32)
        audio[120] = 6.0                      # the big moment at 120 s
        cand = detect.Candidate(100.0, 36.0, 0.9)
        out = detect.refine(cand, 200, audio, None, lead=25, tail=10)
        self.assertAlmostEqual(out.start, 95.0)
        self.assertAlmostEqual(out.duration, 35.0)

    def test_refine_chat_lag_and_edges(self):
        import numpy as np
        chat_ps = np.zeros(80, dtype=np.float32)
        chat_ps[20] = 30                      # chat erupts 5 s after the moment at 15 s
        out = detect.refine(detect.Candidate(0.0, 36.0, 0.9), 70, None, chat_ps, lead=25, tail=10)
        self.assertEqual(out.start, 0.0)      # can't start before the recording
        self.assertAlmostEqual(out.duration, 35.0)


class GameFilterTest(unittest.TestCase):
    def test_gta6_only(self):
        c = Campaign("a", game_filter="gta6")
        self.assertTrue(c.game_ok("Grand Theft Auto VI"))
        self.assertFalse(c.game_ok("Grand Theft Auto V"))
        self.assertFalse(c.game_ok("Just Chatting"))
        self.assertTrue(c.game_ok("", "FIRST DAY OF GTA 6 !!"))      # YouTube: no category, title says GTA 6
        self.assertFalse(c.game_ok("", "gta 5 rp grind"))
        self.assertFalse(Campaign("b", game_filter="grand theft auto").game_ok("Grand Theft Auto V"))  # old setting

    def test_explicit_gta5_and_any(self):
        c = Campaign("a", game_filter="gta5, gta6")
        self.assertTrue(c.game_ok("Grand Theft Auto V") and c.game_ok("Grand Theft Auto VI"))
        self.assertTrue(Campaign("b").game_ok("Minecraft"))

    def test_example_campaigns_gta6(self):
        cs = load_campaigns(Path(__file__).resolve().parents[1] / "campaigns.example.yaml")
        for cid in ("kai-cenat-gta", "core-pool", "gta-any", "xqc-gta", "gta-rp"):
            self.assertFalse(cs[cid].game_ok("Grand Theft Auto V"), cid)
            self.assertTrue(cs[cid].game_ok("Grand Theft Auto VI"), cid)


class DiscoverTest(unittest.TestCase):
    def test_gta_any_rules(self):
        cs = load_campaigns(Path(__file__).resolve().parents[1] / "campaigns.example.yaml")
        g = cs["gta-any"]
        self.assertTrue(g.discover)
        self.assertFalse(g.submit)
        self.assertIn("ishowspeed", g.discover_exclude)
        self.assertFalse(g.problems())
        from clipbot.sources import covered_logins
        covered = covered_logins(cs)
        self.assertIn("kaicenat", covered)
        self.assertIn("xqc", covered)       # tolerated streamers keep their own rules
        from clipbot.ai import template_text
        title, desc, tags = package(g, template_text(g), [], "somestreamer")
        self.assertIn("somestreamer", title)
        self.assertIn("#GTA6", tags)

    def test_add_discovered(self):
        import os
        from clipbot import sources
        with tempfile.TemporaryDirectory() as d:
            orig = sources._discovered_path
            sources._discovered_path = lambda: Path(d) / "discovered.json"
            try:
                sources.write_discovered({"gta-any": {"newguy": time.time(), "oldguy": time.time() - 99999}})
                cs = {"gta-any": Campaign("gta-any", discover=True)}
                sources.add_discovered(cs, keep_hours=6)
                self.assertEqual(cs["gta-any"].live_logins(), {"newguy"})
            finally:
                sources._discovered_path = orig


class LookTest(unittest.TestCase):
    def test_vice_captions_and_watermark(self):
        from clipbot.captions import captions_ass
        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "c.srt"
            srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nJUST GO\n\n2\n00:00:02,000 --> 00:00:03,000\nNOW\n")
            out = Path(d) / "c.ass"
            captions_ass(srt, out, 560)
            text = out.read_text()
            self.assertIn("Bungee", text)
            self.assertEqual(text.count("JUST GO"), 2)   # glow layer + crisp layer
            o = Path(d) / "o.ass"
            overlay_ass(o, 30, "", "", "", "@gta6.clippaa")
            self.assertIn("@gta6.clippaa", o.read_text())

    def test_no_watermark_where_logos_banned(self):
        cs = load_campaigns(Path(__file__).resolve().parents[1] / "campaigns.example.yaml")
        self.assertFalse(cs["core-pool"].my_watermark)
        self.assertFalse(cs["expedia-ishowspeed"].my_watermark)
        self.assertTrue(cs["gta-any"].my_watermark)


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
