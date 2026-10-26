import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_publisher import (
    VideoPublisher, cue_destination, cue_from_filename, local_dropbox_project,
    project_for_staged_video, wait_for_complete,
)


class VideoPublisherTests(unittest.TestCase):
    def test_only_video_exports_direct_child_is_published(self):
        root = Path('/tmp/PROJECTS')
        self.assertEqual(
            project_for_staged_video(root / '33 Days/Video Exports/1m09 Behind The Glass V1.1.mp4', [root]),
            root / '33 Days',
        )
        self.assertIsNone(project_for_staged_video(root / '33 Days/Mixdown/1m09.mp4', [root]))
        self.assertIsNone(project_for_staged_video(root / '33 Days/Video Exports/1m09.wav', [root]))

    def test_cue_name_keeps_title_and_strips_version_and_timecode(self):
        self.assertEqual(
            cue_from_filename(Path('1m09 Behind The Glass V1.1.mp4')),
            ('1m09', '1m09 Behind The Glass'),
        )
        self.assertEqual(
            cue_from_filename(Path('1mX You\'ll Blow Up One Day 01.07.17.05.mp4')),
            ('1mx', "1mX You'll Blow Up One Day"),
        )
        with self.assertRaises(ValueError):
            cue_from_filename(Path('Director Preview.mp4'))

    def test_reuse_existing_cue_by_id_and_create_missing_flat_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            scoring = Path(tmp) / 'Scoring'
            existing = scoring / '1m09 Old Title'
            existing.mkdir(parents=True)
            self.assertEqual(cue_destination(scoring, '1m09', '1m09 New Title'), existing)
            created = cue_destination(scoring, '1m10', '1m10 New Cue')
            self.assertEqual(created, scoring / '1m10 New Cue')
            self.assertTrue(created.is_dir())

    def test_create_in_episode_layout_and_reject_ambiguous_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            scoring = Path(tmp) / 'Scoring'
            (scoring / 'EP01').mkdir(parents=True)
            created = cue_destination(scoring, '2m06', '2m06 Quick Learner')
            self.assertEqual(created, scoring / 'EP02/2m06 Quick Learner')
            (scoring / 'EP01/2m06 Duplicate').mkdir()
            with self.assertRaises(ValueError):
                cue_destination(scoring, '2m06', '2m06 Unknown Title')

    def test_exact_title_resolves_shared_placeholder_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            scoring = Path(tmp) / 'Scoring'
            (scoring / '2mX Dualism').mkdir(parents=True)
            target = scoring / '2mX Ride Home'
            target.mkdir()
            self.assertEqual(cue_destination(scoring, '2mx', '2mx ride home'), target)
            with self.assertRaises(ValueError):
                cue_destination(scoring, '2mx', '2mX Unknown')
            duplicate = scoring / 'EP02/2mX Ride Home'
            duplicate.mkdir(parents=True)
            with self.assertRaises(ValueError):
                cue_destination(scoring, '2mx', '2mX Ride Home')

    def test_timecoded_music_is_not_video_companion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'PROJECTS'
            project = root / '33 Days'
            stage = project / 'Video Exports'
            stage.mkdir(parents=True)
            video = stage / '1m06 Doorstep Goodbye.mp4'
            video.write_bytes(b'video')
            mixdown = project / 'Film Score/1m06 Doorstep Goodbye/Mixdown'
            mixdown.mkdir(parents=True)
            music = mixdown / '1m06 Doorstep Goodbye 01.13.23.14.wav'
            music.write_bytes(b'music')
            companion = mixdown / '1m06 Doorstep Goodbye.wav'
            companion.write_bytes(b'companion')
            publisher = VideoPublisher([root], lambda _: None, lambda _: None)
            self.assertFalse(publisher.is_video_render_wav(music))
            self.assertTrue(publisher.is_video_render_wav(companion))
            stat = video.stat()
            import time
            publisher.recent[(project, video.stem.casefold())] = (
                getattr(stat, 'st_birthtime', stat.st_ctime), time.time())
            video.unlink()
            self.assertFalse(publisher.is_video_render_wav(music))
            self.assertTrue(publisher.is_video_render_wav(companion))

    def test_local_dropbox_mapping_requires_existing_project_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / 'Library/CloudStorage/Dropbox-Personal/Film Scoring/33 Days'
            project.mkdir(parents=True)
            self.assertEqual(local_dropbox_project('/Film Scoring/33 Days', home), project)
            with self.assertRaises(FileNotFoundError):
                local_dropbox_project('/Film Scoring/Missing', home)

    def test_wait_requires_readable_video_and_closed_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '1m01 Example.mp4'
            path.write_bytes(b'x' * (1024 * 1024 + 1))
            self.assertTrue(wait_for_complete(path, quiet_seconds=0, timeout=1,
                                              poll_seconds=.01, readable=lambda _: True,
                                              is_open=lambda _: False))
            self.assertFalse(wait_for_complete(path, quiet_seconds=0, timeout=.05,
                                               poll_seconds=.01, readable=lambda _: True,
                                               is_open=lambda _: True))

    def test_publish_moves_only_finished_mp4_and_leaves_temp_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'PROJECTS'
            stage = root / '33 Days/Video Exports'
            stage.mkdir(parents=True)
            video = stage / '1m09 Behind The Glass V1.1.mp4'
            video.write_bytes(b'finished video')
            wav = stage / '1m09 Behind The Glass V1.1.wav'
            wav.write_bytes(b'temporary audio')
            dropbox = Path(tmp) / 'Dropbox/Film Scoring/33 Days'
            (dropbox / 'Scoring').mkdir(parents=True)
            logs = []
            publisher = VideoPublisher([root], lambda _: {'dropbox_base_path': '/Film Scoring/33 Days'}, logs.append)
            self.assertTrue(publisher.is_video_render_wav(root / '33 Days/Film Score/1m09 Behind The Glass/Mixdown/1m09 Behind The Glass V1.1 01.19.33.15.wav') is False)
            mixdown = root / '33 Days/Film Score/1m09 Behind The Glass/Mixdown'
            mixdown.mkdir(parents=True)
            companion = mixdown / '1m09 Behind The Glass V1.1 01.19.33.15.wav'
            companion.write_bytes(b'temporary audio')
            self.assertFalse(publisher.is_video_render_wav(companion))
            with patch('video_publisher.wait_for_complete', return_value=True), \
                 patch('video_publisher.local_dropbox_project', return_value=dropbox):
                publisher._publish(video)
            self.assertFalse(video.exists())
            self.assertTrue((dropbox / 'Scoring/1m09 Behind The Glass/1m09 Behind The Glass V1.1.mp4').exists())
            self.assertTrue(wav.exists())
            self.assertFalse(publisher.is_video_render_wav(companion))
            self.assertTrue(any('moved completed' in line for line in logs))

    def test_replace_same_filename_and_remove_previous_versions(self):
        self.check_replacement(False)

    def test_failed_move_preserves_previous_videos_and_source(self):
        self.check_replacement(True)

    def check_replacement(self, fail_move):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'PROJECTS'
            stage = root / '33 Days/Video Exports'
            stage.mkdir(parents=True)
            video = stage / '1m09 Behind The Glass V2.mp4'
            video.write_bytes(b'new completed video')
            dropbox = Path(tmp) / 'Dropbox/33 Days'
            folder = dropbox / 'Scoring/1m09 Behind The Glass'
            folder.mkdir(parents=True)
            same = folder / video.name
            same.write_bytes(b'old same filename')
            old = folder / '1m09 Behind The Glass V1.mov'
            old.write_bytes(b'old version')
            retained = [folder / '1m09 Behind The Glass.wav',
                        folder / 'reference.mp4', folder / '1m10 Other Cue.mp4']
            for path in retained:
                path.write_bytes(b'keep')
            logs = []
            publisher = VideoPublisher([root], lambda _: {'dropbox_base_path': '/Film Scoring/33 Days'}, logs.append)
            with patch('video_publisher.wait_for_complete', return_value=True), \
                 patch('video_publisher.video_is_open', return_value=False), \
                 patch('video_publisher.local_dropbox_project', return_value=dropbox):
                if fail_move:
                    with patch('video_publisher.os.replace', side_effect=OSError('move failed')):
                        publisher._publish(video)
                else:
                    publisher._publish(video)
            if fail_move:
                self.assertEqual(same.read_bytes(), b'old same filename')
                self.assertTrue(old.exists())
                self.assertTrue(video.exists())
            else:
                self.assertEqual(same.read_bytes(), b'new completed video')
                self.assertFalse(old.exists())
                self.assertFalse(video.exists())
            for path in retained:
                self.assertEqual(path.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
