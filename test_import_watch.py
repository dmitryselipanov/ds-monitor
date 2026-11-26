import unittest

from import_watch import Watcher


class WatcherTests(unittest.TestCase):
    def test_show_once_then_close(self):
        w, calls = Watcher(), []
        cprs = lambda: calls.append(1) or ["/P/1m01 Opening/1m01 Opening.cpr"]
        self.assertIsNone(w.step(False, cprs))
        self.assertEqual(w.step(True, cprs), ("show", "/P/1m01 Opening/1m01 Opening.cpr"))
        self.assertIsNone(w.step(True, cprs))          # still open: nothing new, no lsof
        self.assertEqual(len(calls), 1)
        self.assertEqual(w.step(False, cprs), ("close", None))
        self.assertIsNone(w.step(False, cprs))

    def test_waits_for_the_file(self):
        w, files = Watcher(), []
        self.assertIsNone(w.step(True, lambda: files))
        files.append("/P/a.cpr")
        self.assertEqual(w.step(True, lambda: files), ("show", "/P/a.cpr"))

    def test_dialog_closed_before_file_seen(self):
        w = Watcher()
        self.assertIsNone(w.step(True, lambda: []))
        self.assertIsNone(w.step(False, lambda: []))     # nothing was shown: nothing to close
        self.assertEqual(w.step(True, lambda: ["/P/b.cpr"]), ("show", "/P/b.cpr"))


if __name__ == "__main__":
    unittest.main()
