"""handle_mixdown_wav with the BWF check: warn vs block, alerts, the waiting list."""

import json
import sys
import tempfile
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

import mixdown_publisher  # noqa: E402
import monitor  # noqa: E402
from test_bwf_check import SILENCE_24, bwf_wav  # noqa: E402

GOOD = bwf_wav(561_911_350)
EARLY = bwf_wav(561_911_350 - 281 * 2002)


class HandleMixdownBwfTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.scoring = self.home / "Library/CloudStorage/Dropbox-Personal/Film Scoring/33 Days/Scoring"
        (self.scoring / "3m37 Help Us").mkdir(parents=True)
        self.mixdown = self.home / "Documents/PROJECTS/33 Days/3m37/Mixdown"
        self.mixdown.mkdir(parents=True)
        self.config = self.home / ".ds-monitor-config.json"
        self.logs, self.alerts, self.telegrams = [], [], []
        project = {"id": "p1", "title": "33 Days", "dropbox_base_path": "/Film Scoring/33 Days"}
        patches = [
            mock.patch.object(monitor, "log", self.logs.append),
            mock.patch.object(monitor, "notify", lambda t, s, b: self.alerts.append(b)),
            mock.patch.object(monitor, "send_telegram", self.telegrams.append),
            mock.patch.object(monitor, "resolve_project", lambda p: project),
            mock.patch.object(monitor, "fetch_cue_names", lambda p: ["3m37 Help Us"]),
            mock.patch.object(monitor, "_fetch_monitor_cues", lambda p: None),
            mock.patch.object(monitor, "DS_CONFIG_PATH", self.config),
            mock.patch.object(monitor, "MIXDOWN_WAITING_PATH", self.home / "mixdown-waiting.json"),
            mock.patch.object(monitor, "_mixdown_waiting", {}),
            mock.patch.object(monitor, "_mixdown_uploaded", {}),
            mock.patch.object(monitor, "_bwf_alerted", {}),
            mock.patch.object(monitor, "_frame_rate_logged", set()),
            mock.patch.object(mixdown_publisher, "STAGING_DIR", self.home / "Library/Caches/ds-monitor/staging"),
            mock.patch.object(Path, "home", lambda: self.home),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def export(self, name, data, mode=None):
        if mode:
            self.config.write_text(json.dumps({"bwf_check": mode}))
        path = self.mixdown / name
        path.write_bytes(data)
        st = path.stat()
        with mock.patch.object(monitor, "read_finished_wav", lambda p, log: (data, (st.st_size, st.st_mtime_ns))):
            monitor.handle_mixdown_wav(path)
        return path

    def published(self):
        return sorted(p.name for p in (self.scoring / "3m37 Help Us").glob("*.wav"))

    def test_pass_publishes_without_alert(self):
        self.export("3m37 Help Us V1.2 03.14.54.19.wav", GOOD)
        self.assertEqual(self.published(), ["3m37 Help Us V1.2 03.14.54.19.wav"])
        self.assertEqual(self.telegrams, [])
        self.assertTrue(any(line.startswith("[bwf] PASS") for line in self.logs))
        self.assertTrue(any("checking stamps at 23.976 fps" in line for line in self.logs))

    def test_warn_mode_is_the_default_and_publishes_with_alert(self):
        self.export("3m37 Help Us V1.2 03.14.54.19.wav", EARLY)
        self.assertEqual(self.published(), ["3m37 Help Us V1.2 03.14.54.19.wav"])
        self.assertEqual(len(self.telegrams), 1)
        self.assertIn("33 Days\n3m37 Help Us V1.2: BWF stamp 11.720 s early", self.telegrams[0])
        self.assertIn("published anyway", self.alerts[0])
        self.assertEqual(monitor._mixdown_waiting, {})

    def test_block_mode_holds_back_and_waits_for_a_reexport(self):
        path = self.export("3m37 Help Us V1.2 03.14.54.19.wav", EARLY, mode="block")
        self.assertEqual(self.published(), [])
        self.assertIn("not published", self.telegrams[0])
        entry = json.loads((self.home / "mixdown-waiting.json").read_text())[str(path)]
        self.assertTrue(entry[2].startswith("bwf: BWF stamp 11.720 s early"))
        # The minute retry doesn't republish the same bytes...
        with mock.patch.object(monitor.threading, "Thread") as thread:
            monitor.retry_waiting_mixdowns()
        thread.assert_not_called()
        # ...a corrected re-export publishes and clears the entry.
        self.export("3m37 Help Us V1.2 03.14.54.19.wav", GOOD)
        self.assertEqual(self.published(), ["3m37 Help Us V1.2 03.14.54.19.wav"])
        self.assertEqual(monitor._mixdown_waiting, {})

    def test_same_export_alerts_once(self):
        path = self.export("3m37 Help Us V1.2 03.14.54.19.wav", EARLY, mode="block")
        st = path.stat()
        with mock.patch.object(monitor, "read_finished_wav", lambda p, log: (EARLY, (st.st_size, st.st_mtime_ns))):
            monitor.handle_mixdown_wav(path)
        self.assertEqual(len(self.telegrams), 1)

    def test_silent_mix_publishes_with_alert_even_in_block_mode(self):
        self.export("3m37 Help Us V1.2 03.14.54.19.wav", bwf_wav(561_911_350, SILENCE_24), mode="block")
        self.assertEqual(self.published(), ["3m37 Help Us V1.2 03.14.54.19.wav"])
        self.assertIn("silent mix", self.telegrams[0])

    def test_production_versions_are_not_checked(self):
        self.export("3m37 Help Us - NO SAX.wav", bwf_wav(None), mode="block")
        self.assertEqual(self.published(), [])
        self.assertEqual(self.telegrams, [])
        self.assertFalse(any(line.startswith("[bwf]") for line in self.logs))
        self.assertEqual(len(next(iter(monitor._mixdown_waiting.values()))), 2)  # name wait, not BWF

    def test_off_mode(self):
        self.export("3m37 Help Us V1.2 03.14.54.19.wav", bwf_wav(None), mode="off")
        self.assertEqual(self.published(), ["3m37 Help Us V1.2 03.14.54.19.wav"])
        self.assertEqual(self.telegrams, [])

    def test_frame_rate_from_ds_scoring(self):
        with mock.patch.object(monitor, "_fetch_monitor_cues", lambda p: {"cues": [], "frame_rate": "25"}):
            self.assertEqual(monitor.project_frame_rate({"id": "p1", "title": "33 Days"}), "25")

    def test_old_waiting_entries_still_load(self):
        (self.home / "mixdown-waiting.json").write_text(json.dumps({"/x/a.wav": [10, 20], "/x/b.wav": [1, 2, "bwf: x"]}))
        loaded = monitor._load_mixdown_waiting()
        self.assertEqual(loaded[Path("/x/a.wav")], (10, 20))
        self.assertEqual(loaded[Path("/x/b.wav")], (1, 2, "bwf: x"))


if __name__ == "__main__":
    unittest.main()
