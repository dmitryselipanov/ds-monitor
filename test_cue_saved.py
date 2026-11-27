"""First save of a cue's .cpr → /monitor-cue-saved (Spotting → Writing in DS Scoring)."""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

if "rumps" not in sys.modules:  # menu-bar library: only on the Mac
    try:
        import rumps  # noqa: F401
    except ImportError:
        stub = types.ModuleType("rumps")
        stub.App = object
        stub.MenuItem = lambda *a, **k: None
        stub.timer = lambda *a, **k: (lambda f: f)
        stub.alert = stub.quit_application = lambda *a, **k: None
        sys.modules["rumps"] = stub

import monitor  # noqa: E402

ROOT = Path("/Users/d/Documents/PROJECTS/33 Days")
PROJECT = {"id": "p1", "title": "33 Days"}


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CueNumberTests(unittest.TestCase):
    def test_from_name_then_folder(self):
        self.assertEqual(monitor.cue_number_for_cpr(ROOT / "1m09/1m09 Behind The Glass.cpr"), "1m09")
        self.assertEqual(monitor.cue_number_for_cpr(ROOT / "x/2M10a_Chase V2.cpr"), "2m10a")
        self.assertEqual(monitor.cue_number_for_cpr(ROOT / "3m37/Help Us.cpr"), "3m37")
        self.assertEqual(monitor.cue_number_for_cpr(ROOT / "3m37/3m37.cpr"), "3m37")
        self.assertIsNone(monitor.cue_number_for_cpr(ROOT / "Templates/Orchestral_v16 Colors.cpr"))
        self.assertIsNone(monitor.cue_number_for_cpr(ROOT / "Suites/Main Theme Suite.cpr"))

    def test_episode_folder(self):
        self.assertEqual(monitor.episode_for_path(Path("/P/Show/EP02/1m01/1m01 Open.cpr")), 2)
        self.assertEqual(monitor.episode_for_path(Path("/P/Show/ep 10/1m01 Open.cpr")), 10)
        self.assertIsNone(monitor.episode_for_path(ROOT / "1m01/1m01 Open.cpr"))


class ReportTests(unittest.TestCase):
    def setUp(self):
        monitor._cue_saved_done.clear()
        monitor._cue_saved_logged.clear()
        self.sent, self.notes, self.logs = [], [], []
        self.reply = {"changed": True, "cue_number": "1m09", "title": "Behind The Glass", "stage": "st_writ"}

        def urlopen(req, timeout=None):
            self.sent.append((req.full_url, req.headers, json.loads(req.data)))
            return _Resp(self.reply)

        patches = [
            mock.patch.dict(os.environ, {"DS_MONITOR_SECRET": "s3cret"}),
            mock.patch("urllib.request.urlopen", urlopen),
            mock.patch.object(monitor, "resolve_project", lambda p: PROJECT),
            mock.patch.object(monitor, "notify", lambda *a: self.notes.append(a)),
            mock.patch.object(monitor, "log", self.logs.append),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_first_save_moves_to_writing_and_notifies_once(self):
        path = ROOT / "1m09/1m09 Behind The Glass.cpr"
        self.assertTrue(monitor.report_cue_saved(path)["changed"])
        url, headers, body = self.sent[0]
        self.assertEqual(url, monitor.MONITOR_CUE_SAVED_URL)
        self.assertEqual(headers["Authorization"], "Bearer s3cret")
        self.assertEqual(body, {"project_id": "p1", "cue_number": "1m09"})
        self.assertEqual(self.notes, [("DS Monitor", "33 Days", "1m09 Behind The Glass → Writing")])
        monitor.report_cue_saved(path)  # later saves this run: nothing sent
        self.assertEqual(len(self.sent), 1)

    def test_past_spotting_is_remembered_quietly(self):
        self.reply = {"changed": False, "reason": "past spotting"}
        path = ROOT / "1m09/1m09 Behind The Glass.cpr"
        monitor.report_cue_saved(path)
        monitor.report_cue_saved(path)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.notes, [])

    def test_missing_cue_is_asked_again_but_logged_once(self):
        self.reply = {"changed": False, "reason": "no cue"}
        path = ROOT / "9m01/9m01 New.cpr"
        monitor.report_cue_saved(path)
        monitor.report_cue_saved(path)
        self.assertEqual(len(self.sent), 2)  # the cue may be added in DS Scoring meanwhile
        self.assertEqual(len([l for l in self.logs if "no such cue" in l]), 1)

    def test_episode_is_sent(self):
        monitor.report_cue_saved(Path("/P/Show/EP03/1m01/1m01 Open.cpr"))
        self.assertEqual(self.sent[0][2]["episode"], 3)

    def test_backups_autosaves_and_non_cue_projects_are_ignored(self):
        for p in [ROOT / "1m09/1m09 Behind The Glass-01.cpr",
                  ROOT / "1m09/Auto Saves/1m09 Behind The Glass.cpr",
                  ROOT / "Template.cpr"]:
            self.assertIsNone(monitor.report_cue_saved(p))
        self.assertEqual(self.sent, [])

    def test_no_secret_sends_nothing(self):
        with mock.patch.dict(os.environ, {"DS_MONITOR_SECRET": ""}):
            self.assertIsNone(monitor.report_cue_saved(ROOT / "1m09/1m09 X.cpr"))
        self.assertEqual(self.sent, [])

    def test_worker_unreachable_is_logged_not_raised(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(monitor.report_cue_saved(ROOT / "1m09/1m09 X.cpr"))
        self.assertTrue(any("couldn't reach" in l for l in self.logs))


if __name__ == "__main__":
    unittest.main()
