import unittest
from clipbot.core import duration_windows,srt_time,make_title
from pathlib import Path

class CoreTest(unittest.TestCase):
    def test_srt_time(self):
        self.assertEqual(srt_time(61.235),'00:01:01,235')
    def test_non_overlapping_windows(self):
        selected=duration_windows(150,36,2,[.1,.2,.7,.1,.3,.95,.1,.2,.3,.4,.1,.2])
        self.assertEqual(len(selected),2)
        self.assertGreaterEqual(abs(selected[0][0]-selected[1][0]),28)
    def test_small_input(self):
        self.assertEqual(duration_windows(5,36,2,[.8]),[])
    def test_title(self):
        self.assertIn('GTA 6',make_title(Path('someone__01.mp4'),.9))
if __name__=='__main__':unittest.main()
