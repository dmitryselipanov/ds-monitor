import os
import struct
import tempfile
import unittest
from pathlib import Path

from bwf_check import (SILENT_DBFS, check_stamp, evaluate_mixdown, normalize_rate, parse_wav, peak_dbfs)
from wav_check import wav_file_problem, wav_header_problem

TONE_24 = (struct.pack("<i", 4_000_000)[:3] + struct.pack("<i", -4_000_000)[:3]) * 480   # about -6 dBFS
SILENCE_24 = bytes(6 * 4800)


def chunk(cid: bytes, body: bytes) -> bytes:
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def bext_body(time_reference: int, *, version=1, true_peak=None) -> bytes:
    body = bytearray(602)  # Cubase writes the fixed 602-byte bext, no coding history
    body[0:16] = b"Cubase mixdown  "
    struct.pack_into("<Q", body, 338, time_reference)
    struct.pack_into("<H", body, 346, version)
    if true_peak is not None:
        struct.pack_into("<h", body, 416, round(true_peak * 100))
    return bytes(body)


IXML = (b'<?xml version="1.0" encoding="UTF-8"?><BWFXML><IXML_VERSION>1.61</IXML_VERSION>'
        b"<SPEED><MASTER_SPEED>24000/1001</MASTER_SPEED><TIMECODE_RATE>24000/1001</TIMECODE_RATE>"
        b"</SPEED><TRACK_LIST><TRACK_COUNT>2</TRACK_COUNT></TRACK_LIST></BWFXML>")


def bwf_wav(time_reference: int | None, audio: bytes = TONE_24, *, sr=48000, bits=24, channels=2,
            ixml: bytes | None = IXML, rf64=False, bext_version=1, true_peak=None, format_tag=1) -> bytes:
    """Layout of a Cubase 15 Broadcast Wave export: JUNK/ds64, fmt, bext, iXML, data."""
    fmt = chunk(b"fmt ", struct.pack("<HHIIHH", format_tag, channels, sr, sr * channels * bits // 8,
                                     channels * bits // 8, bits))
    middle = fmt
    if time_reference is not None:
        middle += chunk(b"bext", bext_body(time_reference, version=bext_version, true_peak=true_peak))
    if ixml is not None:
        middle += chunk(b"iXML", ixml)
    if not rf64:
        body = b"WAVE" + chunk(b"JUNK", bytes(28)) + middle + chunk(b"data", audio)
        return b"RIFF" + struct.pack("<I", len(body)) + body
    data = b"data" + struct.pack("<I", 0xFFFFFFFF) + audio + (b"\0" if len(audio) & 1 else b"")
    body = b"WAVE" + chunk(b"ds64", bytes(28)) + middle + data
    out = bytearray(b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body)
    struct.pack_into("<QQQ", out, 20, len(out) - 8, len(audio), len(audio) // (channels * bits // 8))
    return bytes(out)


def tc_samples(hh, mm, ss, ff, spf=2002):
    return (((hh * 60 + mm) * 60 + ss) * 24 + ff) * spf


class KeyNumbersTests(unittest.TestCase):
    """The note's table: 48 kHz, 23.976 non-drop, one frame = 2,002 samples."""

    def test_reference_values(self):
        self.assertEqual(tc_samples(1, 0, 0, 0), 172_972_800)
        self.assertEqual(tc_samples(2, 0, 0, 0), 345_945_600)
        self.assertEqual(tc_samples(1, 1, 57, 22), 178_638_460)
        self.assertEqual(tc_samples(3, 14, 54, 19), 561_911_350)


class StampTests(unittest.TestCase):
    def stamp(self, name, tref, rate="23.976", **kw):
        return check_stamp(name, parse_wav(bwf_wav(tref, **kw)), rate)

    def test_correct_stamps_pass(self):
        for name, tref in [("1m01 Opening 01.00.00.00.wav", 172_972_800),
                           ("2m01 Chaos 02.00.00.00.wav", 345_945_600),
                           ("1m02 City Pulse V1.1 01.01.57.22.wav", 178_638_460),
                           ("3m37 Help Us 03.14.54.19.wav", 561_911_350)]:
            result = self.stamp(name, tref)
            self.assertEqual(result.status, "PASS", (name, result.detail))
            self.assertIn("iXML", result.detail)

    def test_24_fps_maths_is_off_by_3_6_seconds_per_hour(self):
        result = self.stamp("1m01 Opening 01.00.00.00.wav", 172_800_000)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("off by -172,800 samples (-3.600 s, -86.31 frames)", result.detail)
        self.assertIn("not on a frame boundary", result.detail)
        self.assertIn("3.600 s early", result.summary)

    def test_off_frame_start(self):
        # Cubase test 2026-11-26: a locator on the musical grid, 149 samples past 03:17:22:08.
        tref = tc_samples(3, 17, 22, 8) + 149
        result = self.stamp("3m37 Help Us 03.17.22.08.wav", tref)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("not on a frame boundary (+149 samples)", result.detail)
        self.assertIn("off by +149 samples", result.detail)

    def test_off_by_whole_frames_and_seconds(self):
        result = self.stamp("3m37 Help Us V1.2 03.14.54.19.wav", 561_911_350 - 24 * 2002)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("off by -48,048 samples (-1.001 s, -24.00 frames)", result.detail)
        self.assertNotIn("frame boundary", result.detail)
        self.assertIn("1.001 s early", result.summary)

    def test_no_bext(self):
        result = self.stamp("1m01 Opening 01.00.00.00.wav", None)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("no BWF chunk", result.detail)

    def test_stamped_zero(self):
        result = self.stamp("1m01 Opening 01.00.00.00.wav", 0)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("stamped 00:00:00:00", result.detail)

    def test_no_timecode_in_name_warns(self):
        result = self.stamp("1m01 Opening.wav", 172_972_800)
        self.assertEqual(result.status, "WARN")
        self.assertIn("no timecode in filename", result.detail)

    def test_rf64(self):
        data = bwf_wav(561_911_350, rf64=True)
        self.assertIsNone(wav_header_problem(data))
        self.assertEqual(check_stamp("3m37 Help Us 03.14.54.19.wav", parse_wav(data)).status, "PASS")

    def test_other_rates(self):
        self.assertEqual(self.stamp("1m01 A 01.00.00.00.wav", 3600 * 25 * 1920, rate="25").status, "PASS")
        self.assertEqual(self.stamp("1m01 A 01.00.00.00.wav", 3600 * 48000, rate="24").status, "PASS")

    def test_ixml_rate_mismatch_is_noted(self):
        result = self.stamp("1m01 A 01.00.00.00.wav", 3600 * 25 * 1920, rate="25")
        self.assertIn("iXML says 23.976 fps", result.detail)

    def test_frame_rate_names(self):
        for raw in ["23.976", "23.98", "24000/1001", 23.976, " 23.976 fps"]:
            self.assertEqual(normalize_rate(raw), "23.976", raw)
        self.assertEqual(normalize_rate("29.97"), "29.97")
        self.assertIsNone(normalize_rate("59.94"))
        self.assertIsNone(normalize_rate(None))


class SilenceTests(unittest.TestCase):
    def test_tone_is_not_silent(self):
        info = parse_wav(bwf_wav(0, TONE_24))
        self.assertAlmostEqual(peak_dbfs(bwf_wav(0, TONE_24), info, stop_above=None), -6.4, delta=0.1)

    def test_silence_is_flagged(self):
        verdict = evaluate_mixdown("3m37 Help Us 03.14.54.19.wav", bwf_wav(561_911_350, SILENCE_24))
        self.assertEqual(verdict.stamp.status, "PASS")
        self.assertIn("no signal at all", verdict.silence)
        self.assertTrue(verdict.publish)
        self.assertIn("silent mix", verdict.alert)

    def test_dither_level_noise_counts_as_silent(self):
        quiet = (struct.pack("<i", 200)[:3] + struct.pack("<i", -200)[:3]) * 4800  # about -92 dBFS
        verdict = evaluate_mixdown("1m01 A 01.00.00.00.wav", bwf_wav(172_972_800, quiet))
        self.assertIn("peak -92.5 dBFS", verdict.silence)

    def test_16_bit_and_float(self):
        tone16 = struct.pack("<hh", 16000, -16000) * 4800
        info = parse_wav(bwf_wav(0, tone16, bits=16))
        self.assertGreater(peak_dbfs(bwf_wav(0, tone16, bits=16), info), SILENT_DBFS)
        silent_float = struct.pack("<ff", 0.0, 0.00001) * 4800  # -100 dBFS
        data = bwf_wav(0, silent_float, bits=32, format_tag=3)
        self.assertAlmostEqual(peak_dbfs(data, parse_wav(data)), -100.0, delta=0.01)

    def test_bext_true_peak_is_used_when_set(self):
        # bext v2 says -120 dBTP (as in the 2026-11-26 test export): trusted without scanning.
        data = bwf_wav(172_972_800, TONE_24, bext_version=2, true_peak=-120)
        self.assertIn("bext true peak -120.0 dBTP", evaluate_mixdown("1m01 A 01.00.00.00.wav", data).silence)

    def test_unset_bext_loudness_falls_back_to_scan(self):
        data = bwf_wav(172_972_800, SILENCE_24, bext_version=2)  # v2 with loudness fields left at 0
        self.assertIn("no signal at all", evaluate_mixdown("1m01 A 01.00.00.00.wav", data).silence)


class VerdictTests(unittest.TestCase):
    NAME = "3m37 Help Us V1.2 03.14.54.19.wav"
    EARLY = bwf_wav(561_911_350 - 281 * 2002)  # 281 frames = 11.720 s early

    def test_pass_publishes_quietly(self):
        verdict = evaluate_mixdown(self.NAME, bwf_wav(561_911_350), mode="block")
        self.assertTrue(verdict.publish)
        self.assertIsNone(verdict.alert)
        self.assertTrue(verdict.log_lines[0].startswith("[bwf] PASS"))

    def test_warn_mode_publishes_a_failure_and_alerts(self):
        verdict = evaluate_mixdown(self.NAME, self.EARLY, mode="warn")
        self.assertTrue(verdict.publish)
        self.assertEqual(verdict.alert, "3m37 Help Us V1.2: BWF stamp 11.720 s early (-562,562 samples, "
                                        "-281.00 frames), published anyway (BWF check is warn-only)")

    def test_block_mode_holds_a_failure_back(self):
        verdict = evaluate_mixdown(self.NAME, self.EARLY, mode="block")
        self.assertFalse(verdict.publish)
        self.assertIn("not published", verdict.alert)

    def test_block_mode_still_publishes_warnings_and_silence(self):
        self.assertTrue(evaluate_mixdown("1m01 Opening.wav", bwf_wav(172_972_800), mode="block").publish)
        self.assertTrue(evaluate_mixdown(self.NAME, bwf_wav(561_911_350, SILENCE_24), mode="block").publish)

    def test_no_bext_blocks_only_in_block_mode(self):
        data = bwf_wav(None)
        self.assertTrue(evaluate_mixdown(self.NAME, data, mode="warn").publish)
        self.assertFalse(evaluate_mixdown(self.NAME, data, mode="block").publish)

    def test_off_mode_checks_nothing(self):
        verdict = evaluate_mixdown(self.NAME, b"not a wav", mode="off")
        self.assertTrue(verdict.publish)
        self.assertEqual(verdict.log_lines, [])


class WavCheckAcceptsCubaseBwfTests(unittest.TestCase):
    def test_bwf_with_ixml_passes_header_check(self):
        self.assertIsNone(wav_header_problem(bwf_wav(561_911_350)))

    def test_chunks_before_data_larger_than_64_kb(self):
        data = bwf_wav(561_911_350, ixml=b"<BWFXML>" + b" " * 100_000 + b"</BWFXML>")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "3m37 Help Us 03.14.54.19.wav"
            path.write_bytes(data)
            self.assertIsNone(wav_file_problem(path))

    def test_rf64_over_4_gb_on_disk(self):
        # Sparse file: header of a 4.3 GB RF64 export, the audio is a hole.
        audio_size = 4_300_000_002
        head = bytearray(bwf_wav(561_911_350, b"", rf64=True))
        total = len(head) + audio_size
        struct.pack_into("<QQ", head, 20, total - 8, audio_size)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "3m37 Help Us 03.14.54.19.wav"
            with open(path, "wb") as f:
                f.write(head)
                f.truncate(total)
            self.assertIsNone(wav_file_problem(path))
            with open(path, "rb") as f:
                self.assertEqual(parse_wav(f.read(64 * 1024)).time_reference, 561_911_350)

    def test_truncated_rf64_is_rejected(self):
        data = bwf_wav(561_911_350, rf64=True)
        self.assertIn("RIFF size", wav_header_problem(data[:-100]))


REAL_EXPORT = os.environ.get("DS_REAL_BWF_EXPORT", "")


@unittest.skipUnless(REAL_EXPORT, "set DS_REAL_BWF_EXPORT to a real Cubase 15 BWF export")
class RealCubaseExportTests(unittest.TestCase):
    def test_real_export(self):
        path = Path(REAL_EXPORT)
        self.assertIsNone(wav_file_problem(path))
        data = path.read_bytes()
        self.assertIsNone(wav_header_problem(data))
        verdict = evaluate_mixdown(path.name, data)
        self.assertEqual(verdict.stamp.status, "PASS", verdict.stamp.detail)


if __name__ == "__main__":
    unittest.main()
