import tempfile
import unittest
from pathlib import Path

from mixdown_publisher import publish_mixdown
from test_wav_check import cubase_wav


class PublishMixdownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.scoring = self.home / "Library/CloudStorage/Dropbox-Personal/Film Scoring/Holod/Scoring"
        self.scoring.mkdir(parents=True)
        self.staging = self.home / "Library/Caches/ds-monitor/mixdown-staging"
        self.project = {"title": "Holod", "dropbox_base_path": "/Film Scoring/Holod"}
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def cue_folder(self, name, parent=None):
        folder = (parent or self.scoring) / name
        folder.mkdir(parents=True)
        return folder

    def publish(self, name, data=None, cue_names=None):
        return publish_mixdown(Path("/x/Mixdown") / name, data or cubase_wav(bytes(1000)), self.project,
                               self.logs.append, cue_names=cue_names, home=self.home, staging_dir=self.staging)

    def test_review_mix_goes_into_its_cue_folder(self):
        folder = self.cue_folder("1m01 Opening")
        data = cubase_wav(bytes(1000))
        dest = self.publish("1m01 Opening 01.00.01.18.wav", data)
        self.assertEqual(dest, folder / "1m01 Opening 01.00.01.18.wav")
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(list(self.staging.iterdir()), [])  # no staging leftovers
        self.assertEqual(dest.stat().st_mode & 0o777, 0o644)

    def test_version_and_timecode_are_ignored_when_matching(self):
        folder = self.cue_folder("1m02 Chapter 02 - Audition")
        self.assertEqual(self.publish("1m02 Chapter 02 - Audition V1.2 01.02.47.22.wav").parent, folder)

    def test_production_variants_stay_local(self):
        self.cue_folder("1m02 City Pulse")
        self.cue_folder("1m02 City Pulse - FLUTE GUIDE")  # even a variant that has a folder
        for name in ["1m02 City Pulse - NO SAX.wav", "1m02 City Pulse - VLN SOLO V1.1 01.01.57.22.wav",
                     "1m02 City Pulse - FLUTE GUIDE.wav"]:
            self.assertIsNone(self.publish(name), name)
        self.assertEqual(sorted(p.name for p in self.scoring.iterdir()),
                         ["1m02 City Pulse", "1m02 City Pulse - FLUTE GUIDE"])
        self.assertEqual([p for p in self.scoring.rglob("*.wav")], [])

    def test_first_export_of_a_ds_scoring_cue_creates_its_folder(self):
        dest = self.publish("2m19 Chaos 02.23.52.19.wav", cue_names=["2m19 Chaos", "2m20 Our Own Church"])
        self.assertEqual(dest, self.scoring / "2m19 Chaos" / "2m19 Chaos 02.23.52.19.wav")
        self.assertIn(f"[mixdown] created cue folder {self.scoring / '2m19 Chaos'}", self.logs)

    def test_new_folder_uses_ds_scoring_spelling_and_the_episode_layout(self):
        self.cue_folder("EP01")
        dest = self.publish("2m26 i’ll do what i have to.wav", cue_names=["2m26 I'll Do What I Have To"])
        self.assertEqual(dest.parent, self.scoring / "EP02" / "2m26 I'll Do What I Have To")

    def test_ds_scoring_name_goes_into_a_folder_with_an_old_title(self):
        folder = self.cue_folder("2m20 Your Own Church")
        dest = self.publish("2m20 Our Own Church 02.26.49.02.wav", cue_names=["2m20 Our Own Church"])
        self.assertEqual(dest.parent, folder)
        self.assertEqual([p.name for p in self.scoring.iterdir()], ["2m20 Your Own Church"])
        self.assertTrue(any("still named" in line for line in self.logs))

    def test_ds_scoring_name_skips_variant_folders(self):
        main = self.cue_folder("1m02 Old Title")
        self.cue_folder("1m02 City Pulse - FLUTE GUIDE")
        self.assertEqual(self.publish("1m02 City Pulse.wav", cue_names=["1m02 City Pulse"]).parent, main)

    def test_ds_scoring_name_with_two_candidate_folders_stays_local(self):
        self.cue_folder("1m02 Old Title")
        self.cue_folder("1m02 Older Title")
        self.assertIsNone(self.publish("1m02 City Pulse.wav", cue_names=["1m02 City Pulse"]))

    def test_variant_of_a_ds_scoring_cue_stays_local_and_creates_nothing(self):
        for name in ["2m19 Chaos - NO SAX.wav", "2m19 Chaos Alt.wav"]:
            self.assertIsNone(self.publish(name, cue_names=["2m19 Chaos"]), name)
        self.assertEqual(list(self.scoring.iterdir()), [])

    def test_variant_with_its_own_folder_stays_local_when_ds_scoring_knows_the_cue(self):
        self.cue_folder("2m19 Chaos - NO SAX")
        self.assertIsNone(self.publish("2m19 Chaos - NO SAX.wav", cue_names=["2m19 Chaos"]))

    def test_cue_missing_from_ds_scoring_falls_back_to_its_folder(self):
        folder = self.cue_folder("2mX Dualism")
        self.assertEqual(self.publish("2mX Dualism.wav", cue_names=["2m19 Chaos"]).parent, folder)
        self.assertIsNone(self.publish("2mY Unknown.wav", cue_names=["2m19 Chaos"]))

    def test_cue_without_dropbox_folder_is_not_published_or_created(self):
        self.assertIsNone(self.publish("2m19 Chaos 02.23.52.19.wav"))
        self.assertEqual(list(self.scoring.iterdir()), [])

    def test_new_version_moves_old_wav_to_prev_versions(self):
        folder = self.cue_folder("1m01 Opening")
        self.publish("1m01 Opening 01.00.01.18.wav")
        self.publish("1m01 Opening V1.1 01.00.01.18.wav", cubase_wav(bytes(2000)))
        self.assertEqual(sorted(p.name for p in folder.glob("*.wav")), ["1m01 Opening V1.1 01.00.01.18.wav"])
        self.assertTrue((folder / "Prev Versions/1m01 Opening 01.00.01.18.wav").is_file())

    def test_same_name_reexport_keeps_both_versions(self):
        folder = self.cue_folder("1m01 Opening")
        name = "1m01 Opening 01.00.01.18.wav"
        for size in (1000, 2000, 3000):
            self.publish(name, cubase_wav(bytes(size)))
        self.assertEqual((folder / name).read_bytes(), cubase_wav(bytes(3000)))
        self.assertEqual(sorted(p.name for p in (folder / "Prev Versions").iterdir()),
                         ["1m01 Opening 01.00.01.18 (1).wav", "1m01 Opening 01.00.01.18.wav"])

    def test_cue_folder_found_inside_episode_folder(self):
        folder = self.cue_folder("2m14 You Got That Look", self.scoring / "EP02")
        self.assertEqual(self.publish("2m14 You Got That Look 02.06.50.06.wav").parent, folder)

    def test_matching_ignores_case_spacing_and_curly_apostrophes(self):
        folder = self.cue_folder("1mX You’ll Blow Up One Day")
        self.assertEqual(self.publish("1mx you'll  blow up one day 01.07.17.05.wav").parent, folder)

    def test_files_over_the_old_80mb_limit_publish(self):
        self.cue_folder("1m10 I Need To Go Back")
        data = cubase_wav(bytes(120 * 1024 * 1024))
        self.assertEqual(self.publish("1m10 I Need To Go Back 01.22.17.07.wav", data).stat().st_size, len(data))

    def test_non_cue_filename_is_rejected(self):
        with self.assertRaises(ValueError):
            self.publish("Synth Bass.wav")


if __name__ == "__main__":
    unittest.main()
