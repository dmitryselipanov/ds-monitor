import unittest

from import_watch import Watcher

CPR = "/P/1m01 Opening/1m01 Opening.cpr"
prep = lambda p: {"project": p}


class WatcherTests(unittest.TestCase):
    def test_prepared_while_reading_then_shown_then_closed(self):
        w, calls = Watcher(), []
        cprs = lambda: calls.append(1) or [CPR]
        self.assertEqual(w.step(True, ["3m37 Help Us"], cprs, prep), [])
        self.assertEqual(w.step(True, ["Reading Project File", "3m37"], cprs, prep), [])
        self.assertEqual(w.ready, (CPR, {"project": CPR}))
        self.assertEqual(w.step(True, ["Reading Project File"], cprs, prep), [])   # not again
        self.assertEqual(len(calls), 1)
        self.assertEqual(w.step(True, ["Import Options", "3m37"], cprs, prep),
                         [("show", CPR, {"project": CPR}, True)])
        self.assertEqual(len(calls), 1)                 # used the prepared list
        self.assertEqual(w.step(True, ["Import Options"], cprs, prep), [])
        self.assertEqual(w.step(True, ["3m37"], cprs, prep), [("close",)])
        self.assertEqual(w.step(True, ["3m37"], cprs, prep), [])

    def test_hides_when_another_app_is_in_front(self):
        w = Watcher()
        w.step(True, ["Import Options"], lambda: [CPR], prep)
        self.assertEqual(w.step(False, ["Import Options"], lambda: [CPR], prep), [("visible", False)])
        self.assertEqual(w.step(False, ["Import Options"], lambda: [CPR], prep), [])
        self.assertEqual(w.step(True, ["Import Options"], lambda: [CPR], prep), [("visible", True)])

    def test_opened_while_another_app_in_front_starts_hidden(self):
        w = Watcher()
        self.assertEqual(w.step(False, ["Import Options"], lambda: [CPR], prep),
                         [("show", CPR, {"project": CPR}, False)])

    def test_waits_for_the_file_without_reading_phase(self):
        w, files = Watcher(), []
        self.assertEqual(w.step(True, ["Import Options"], lambda: files, prep), [])
        files.append("/P/a.cpr")
        self.assertEqual(w.step(True, ["Import Options"], lambda: files, prep),
                         [("show", "/P/a.cpr", {"project": "/P/a.cpr"}, True)])

    def test_cancelled_while_reading_forgets_the_prepared_list(self):
        w = Watcher()
        w.step(True, ["Reading Project File"], lambda: [CPR], prep)
        w.step(True, ["3m37"], lambda: [], prep)
        self.assertIsNone(w.ready)
        self.assertEqual(w.step(True, ["Import Options"], lambda: ["/P/b.cpr"], prep)[0][1], "/P/b.cpr")

    def test_osascript_style_titles(self):
        w = Watcher()
        self.assertEqual(w.step(True, ["FullScreen, Import Options, Cubase Pro Project"], lambda: [CPR], prep)[0][0], "show")


if __name__ == "__main__":
    unittest.main()
