import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wav_check import read_finished_wav, wav_file_problem, wav_header_problem


def cubase_wav(audio: bytes, *, finished=True) -> bytes:
    """Same layout as a Cubase mixdown: JUNK (RF64 reserve), fmt, data."""
    junk = b"JUNK" + struct.pack("<I", 28) + bytes(28)
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 48000, 48000 * 6, 6, 24)
    data_size = len(audio) if finished else 0
    body = b"WAVE" + junk + fmt + b"data" + struct.pack("<I", data_size) + audio
    riff_size = len(body) if finished else 4 + len(junk) + len(fmt) + 8
    return b"RIFF" + struct.pack("<I", riff_size) + body


class HeaderTests(unittest.TestCase):
    def test_finished_cubase_wav_is_valid(self):
        self.assertIsNone(wav_header_problem(cubase_wav(bytes(600))))

    def test_unpatched_header_is_rejected(self):
        # The broken Dropbox upload: audio present, header sizes never written.
        self.assertIn("RIFF size", wav_header_problem(cubase_wav(bytes(600), finished=False)))

    def test_zero_data_size_is_rejected_even_if_riff_size_matches(self):
        data = bytearray(cubase_wav(bytes(600)))
        data_size_at = data.index(b"data") + 4
        data[data_size_at:data_size_at + 4] = struct.pack("<I", 0)
        self.assertEqual(wav_header_problem(bytes(data)), "data size is zero")

    def test_truncated_file_is_rejected(self):
        self.assertIn("RIFF size", wav_header_problem(cubase_wav(bytes(600))[:-100]))

    def test_not_a_wav(self):
        self.assertEqual(wav_header_problem(b"ID3" + bytes(40)), "not a WAVE file")

    def test_header_check_from_disk_reads_only_the_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m01 Opening.wav"
            path.write_bytes(cubase_wav(bytes(200_000)))
            self.assertIsNone(wav_file_problem(path))


class ReadFinishedWavTests(unittest.TestCase):
    def wait(self, path, **kw):
        logs = []
        kw.setdefault("is_open", lambda p: False)
        result = read_finished_wav(path, logs.append, quiet_seconds=0.3, poll_seconds=0.05,
                                   timeout=10, **kw)
        return result, logs

    def test_waits_through_a_pause_before_the_header_is_patched(self):
        # Cubase finishes the audio, sits still longer than the quiet period,
        # then patches the sizes. The size never changes during the patch.
        audio = bytes(range(256)) * 40
        final = cubase_wav(audio)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m01 Opening.wav"
            path.write_bytes(cubase_wav(audio, finished=False))
            self.assertEqual(path.stat().st_size, len(final))

            def patch_header():
                time.sleep(1.0)
                with open(path, "r+b") as f:
                    f.write(final[:final.index(b"data") + 8])
            writer = threading.Thread(target=patch_header)
            writer.start()
            (data, identity), logs = self.wait(path)
            writer.join()
        self.assertEqual(data, final)
        self.assertEqual(identity[0], len(final))
        self.assertTrue(any("RIFF size" in line for line in logs))

    def test_waits_while_another_process_has_it_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m01 Opening.wav"
            path.write_bytes(cubase_wav(bytes(600)))
            opened_until = time.monotonic() + 0.8
            started = time.monotonic()
            (data, _), logs = self.wait(path, is_open=lambda p: time.monotonic() < opened_until)
        self.assertGreaterEqual(time.monotonic() - started, 0.8)
        self.assertTrue(any("still open" in line for line in logs))

    def test_open_file_with_valid_header_is_accepted_after_grace_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m01 Opening.wav"
            path.write_bytes(cubase_wav(bytes(600)))
            result, logs = self.wait(path, is_open=lambda p: True, open_grace_seconds=0.6)
        self.assertIsNotNone(result)
        self.assertTrue(any("uploading anyway" in line for line in logs))

    def test_never_finished_file_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "1m01 Opening.wav"
            path.write_bytes(cubase_wav(bytes(600), finished=False))
            logs = []
            result = read_finished_wav(path, logs.append, quiet_seconds=0.1, poll_seconds=0.05,
                                       timeout=0.5, is_open=lambda p: False)
        self.assertIsNone(result)
        self.assertIn("gave up", logs[-1])


if __name__ == "__main__":
    unittest.main()
