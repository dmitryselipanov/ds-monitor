"""Check a Cubase mixdown's Broadcast Wave timestamp and flag silent mixes.

The stamp check compares the bext TimeReference (samples since midnight) with
the timecode at the end of the filename ("1m02 City Pulse V1.1 01.01.57.22.wav")
at the project frame rate. Cubase exports start at bar 1, which sits on the
project start timecode, so a correct file matches to the sample: at 48 kHz and
23.976 fps one frame is exactly 2,002 samples. Frame maths uses exact fractions
(24000/1001), never floats.

Ported from bwfcheck.py (vault: "BWF timestamp delivery - research and to-do -
2026-11-26"), which was tested on real Cubase 15.0.30 exports.

The frame rate comes from the export's own iXML (Cubase writes the project's
rate there), else from DS Scoring's project setting, else 23.976.

Command line: python3 bwf_check.py FILE_OR_FOLDER [...] [--fps 23.976]
(--fps is used for files without an iXML frame rate)
"""

from __future__ import annotations

import math
import re
import struct
import sys
from array import array
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

RATES = {  # name: (nominal frames per second for counting, real frame rate)
    "23.976": (24, Fraction(24000, 1001)),
    "24": (24, Fraction(24)),
    "25": (25, Fraction(25)),
    "29.97": (30, Fraction(30000, 1001)),  # non-drop
    "30": (30, Fraction(30)),
}
RATE_ALIASES = {"23.98": "23.976", "24000/1001": "23.976", "23.976023976": "23.976",
                "30000/1001": "29.97", "29.97ndf": "29.97", "24/1": "24", "25/1": "25", "30/1": "30"}
DEFAULT_RATE = "23.976"
TC_RE = re.compile(r"(\d{2})[.:_](\d{2})[.:_](\d{2})[.:_](\d{2})(?!.*\d{2}[.:_]\d{2}[.:_]\d{2}[.:_]\d{2})")
SILENT_DBFS = -90.0

BEXT_TIME_REFERENCE = 338
BEXT_VERSION = 346
BEXT_MAX_TRUE_PEAK = 416   # v2: int16, 0.01 dB units; 0x7FFF = not set
BEXT_MIN_V2 = 424
BEXT_UNSET = 0x7FFF
WAVE_FORMAT_PCM, WAVE_FORMAT_FLOAT, WAVE_FORMAT_EXTENSIBLE = 1, 3, 0xFFFE


def normalize_rate(value) -> str | None:
    """"23.976", "23.98", "24000/1001", 23.976… → a RATES key, or None if unknown."""
    if value is None:
        return None
    text = str(value).strip().lower().replace(" ", "").removesuffix("fps")
    if text in RATES:
        return text
    if text in RATE_ALIASES:
        return RATE_ALIASES[text]
    try:
        number = float(text)
    except ValueError:
        return None
    for name, (_, real) in RATES.items():
        if abs(number - float(real)) < 0.006:
            return name
    return None


@dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits: int
    format_tag: int
    time_reference: int | None = None   # None: no bext chunk
    bext_version: int | None = None
    max_true_peak: float | None = None  # dBTP from bext v2, when set
    ixml: str | None = None
    data_offset: int | None = None
    data_size: int | None = None


def parse_wav(data: bytes) -> WavInfo:
    """Read fmt, bext, iXML and the data chunk's position from a RIFF/RF64 WAV in memory."""
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise ValueError("not a WAV/RF64 file")
    ds64_data = None
    fmt = bext = ixml = None
    data_offset = data_size = None
    pos = 12
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from("<4sI", data, pos)
        body = pos + 8
        if cid == b"ds64" and size >= 16:
            ds64_data = struct.unpack_from("<Q", data, body + 8)[0]
        elif cid == b"fmt ":
            fmt = data[body:body + size]
        elif cid == b"bext":
            bext = data[body:body + size]
        elif cid == b"iXML":
            ixml = data[body:body + size].decode("utf-8", "replace")
        elif cid == b"data":
            if size == 0xFFFFFFFF and ds64_data is not None:
                size = ds64_data
            data_offset, data_size = body, min(size, len(data) - body)
            # Keep going: some writers put iXML (or even bext) after the audio.
        pos = body + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise ValueError("no fmt chunk")
    format_tag, channels, sample_rate = struct.unpack_from("<HHI", fmt, 0)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    if format_tag == WAVE_FORMAT_EXTENSIBLE and len(fmt) >= 26:
        format_tag = struct.unpack_from("<H", fmt, 24)[0]  # first two bytes of the SubFormat GUID
    info = WavInfo(sample_rate, channels, bits, format_tag, ixml=ixml,
                   data_offset=data_offset, data_size=data_size)
    if bext is not None and len(bext) >= BEXT_TIME_REFERENCE + 8:
        info.time_reference = struct.unpack_from("<Q", bext, BEXT_TIME_REFERENCE)[0]
        if len(bext) >= BEXT_VERSION + 2:
            info.bext_version = struct.unpack_from("<H", bext, BEXT_VERSION)[0]
        if (info.bext_version or 0) >= 2 and len(bext) >= BEXT_MIN_V2:
            peak = struct.unpack_from("<h", bext, BEXT_MAX_TRUE_PEAK)[0]
            # 0x7FFF is "not set". Exactly 0 is what an unfilled field reads as, so it
            # isn't trusted either: the samples are scanned instead.
            if peak not in (BEXT_UNSET, 0):
                info.max_true_peak = peak / 100
    return info


def _ixml_value(ixml: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", ixml)
    return match.group(1) if match else None


def ixml_timecode(ixml: str | None) -> tuple[str | None, str | None, bool]:
    """(frame rate as a RATES key or None, the raw iXML value, drop-frame?) from an export's iXML.

    Cubase writes the project's frame rate here (TIMECODE_RATE / MASTER_SPEED, e.g.
    24000/1001, and TIMECODE_FLAG NDF or DF), so the export itself says which rate its
    stamp and filename timecode are in.
    """
    if not ixml:
        return None, None, False
    raw = _ixml_value(ixml, "TIMECODE_RATE") or _ixml_value(ixml, "MASTER_SPEED")
    flag = (_ixml_value(ixml, "TIMECODE_FLAG") or "").strip().upper()
    return normalize_rate(raw), raw, flag == "DF"


def ixml_frame_rate(ixml: str | None) -> str | None:
    """The iXML frame rate as a RATES key, if present and supported."""
    return ixml_timecode(ixml)[0]


def tc_string(frames: int, nominal: int) -> str:
    ff = frames % nominal
    s = frames // nominal
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{ff:02d}"


@dataclass
class StampResult:
    status: str                 # PASS, WARN or FAIL
    detail: str                 # full line for the log
    summary: str                # short reason for a notification ("" on PASS)
    notes: list[str] = field(default_factory=list)


def check_stamp(name: str, info: WavInfo, rate: str = DEFAULT_RATE, rate_source: str = "") -> StampResult:
    """Compare the bext TimeReference with the filename timecode (bwfcheck's logic)."""
    nominal, real = RATES[rate]
    sr = info.sample_rate
    spf = Fraction(sr) / real  # samples per frame (2002 at 48 kHz / 23.976)
    tref = info.time_reference
    if tref is None:
        return StampResult("FAIL", f"no BWF chunk (sr {sr})", "no BWF timestamp (bext chunk missing)")
    notes, short = [], []
    frames_exact = Fraction(tref) / spf
    bwf_tc = tc_string(int(frames_exact), nominal)
    if frames_exact.denominator != 1:
        off = (frames_exact - int(frames_exact)) * spf
        notes.append(f"not on a frame boundary (+{round(off)} samples)")
        short.append(f"BWF stamp not on a frame boundary (+{round(off)} samples)")
    if tref == 0:
        notes.append("stamped 00:00:00:00")
        short.append("BWF stamped 00:00:00:00")
    m = TC_RE.search(Path(name).stem)
    if not m:
        status = "WARN" if not notes else "FAIL"
        detail = f"BWF {bwf_tc} | no timecode in filename" + ("; " + "; ".join(notes) if notes else "")
        return StampResult(status, detail, "; ".join(short) or "no timecode in filename", notes)
    hh, mm, ss, ff = map(int, m.groups())
    name_frames = ((hh * 60 + mm) * 60 + ss) * nominal + ff
    expected = name_frames * spf
    if expected.denominator != 1:
        notes.append("sample rate/frame rate give non-integer frame length")
        short.append(f"{sr} Hz and {rate} fps don't give whole-sample frames")
    expected = int(expected)
    diff = tref - expected
    detail = (f"name {tc_string(name_frames, nominal)} | BWF {bwf_tc} | "
              f"TimeReference {tref:,} expected {expected:,} | {rate} fps"
              + (f" ({rate_source})" if rate_source else ""))
    if diff != 0 and tref != 0:
        seconds = Fraction(diff, sr)
        frames = Fraction(diff) / spf
        notes.append(f"off by {diff:+,} samples ({float(seconds):+.3f} s, {float(frames):+.2f} frames)")
        when = "late" if diff > 0 else "early"
        short.append(f"BWF stamp {abs(float(seconds)):.3f} s {when} "
                     f"({diff:+,} samples, {float(frames):+.2f} frames)")
    elif tref == 0 and expected != 0:
        notes.append(f"expected {tc_string(name_frames, nominal)}")
    if info.ixml is not None and rate_source != "iXML":
        detail += " | iXML"
    status = "PASS" if not notes else "FAIL"
    return StampResult(status, detail + ("; " + "; ".join(notes) if notes else ""), "; ".join(short), notes)


# ── Silence ────────────────────────────────────────────────────────────────

SCAN_BLOCK = 4 * 1024 * 1024


def _block_peak(block: bytes, bits: int, format_tag: int) -> float:
    """Peak of one block as a fraction of full scale (C-speed via array)."""
    if format_tag == WAVE_FORMAT_FLOAT:
        typecode = "f" if bits == 32 else "d"
        values = array(typecode)
        values.frombytes(block[:len(block) - len(block) % values.itemsize])
        if sys.byteorder == "big":
            values.byteswap()
        return max((abs(v) for v in (max(values, default=0.0), min(values, default=0.0))), default=0.0)
    if bits == 8:
        return max((abs(b - 128) for b in (max(block, default=128), min(block, default=128)))) / 128
    if bits == 16:
        values, full = array("h"), 32768
    elif bits == 24:
        n = len(block) // 3
        wide = bytearray(n * 4)  # each sample shifted into the top 3 bytes of an int32
        wide[1::4], wide[2::4], wide[3::4] = block[0:n * 3:3], block[1:n * 3:3], block[2:n * 3:3]
        block, values, full = bytes(wide), array("i"), 2 ** 31
    elif bits == 32:
        values, full = array("i"), 2 ** 31
    else:
        raise ValueError(f"unsupported bit depth {bits}")
    values.frombytes(block[:len(block) - len(block) % values.itemsize])
    if sys.byteorder == "big":
        values.byteswap()
    if not values:
        return 0.0
    return max(abs(max(values)), abs(min(values))) / full


def peak_dbfs(data: bytes, info: WavInfo, stop_above: float | None = SILENT_DBFS) -> float:
    """Sample peak in dBFS. Stops early once it is above `stop_above` (enough to say "not silent")."""
    if info.data_offset is None:
        raise ValueError("no data chunk")
    frame_bytes = max(1, info.channels * info.bits // 8)
    block = SCAN_BLOCK - SCAN_BLOCK % frame_bytes
    stop = None if stop_above is None else 10 ** (stop_above / 20)
    peak = 0.0
    end = info.data_offset + info.data_size
    for start in range(info.data_offset, end, block):
        peak = max(peak, _block_peak(data[start:min(start + block, end)], info.bits, info.format_tag))
        if stop is not None and peak > stop:
            break
    return 20 * math.log10(peak) if peak > 0 else float("-inf")


def silence_problem(data: bytes, info: WavInfo, threshold: float = SILENT_DBFS) -> str | None:
    """Why the mix looks silent, or None. bext v2 true peak when set, else a sample scan."""
    if info.max_true_peak is not None:
        if info.max_true_peak < threshold:
            return f"silent mix: bext true peak {info.max_true_peak:.1f} dBTP (below {threshold:.0f})"
        return None
    peak = peak_dbfs(data, info)
    if peak < threshold:
        level = "no signal at all" if peak == float("-inf") else f"peak {peak:.1f} dBFS"
        return f"silent mix: {level} (below {threshold:.0f} dBFS)"
    return None


# ── Verdict for the publisher ──────────────────────────────────────────────

MODES = ("warn", "block", "off")


@dataclass
class MixdownVerdict:
    publish: bool
    stamp: StampResult | None
    silence: str | None
    log_lines: list[str]
    alert: str | None   # one-line message for Telegram / Mac notification, or None
    rate: str = DEFAULT_RATE
    rate_source: str = "default"   # "iXML", "DS Scoring" or "default"


def choose_rate(info: WavInfo | None, project_rate: str | None) -> tuple[str, str, list[str], bool]:
    """(rate, where it came from, warnings, drop-frame?) for one export.

    The export's own iXML wins: it is the Cubase project's setting when the file was
    made. DS Scoring's project frame rate is the fallback (exports without iXML), then 23.976.
    """
    warnings = []
    ixml_rate, raw, drop_frame = ixml_timecode(info.ixml if info else None)
    if raw and not ixml_rate:
        warnings.append(f"iXML frame rate {raw} isn't supported by the check")
    if ixml_rate:
        if project_rate and project_rate != ixml_rate:
            warnings.append(f"Cubase project is {ixml_rate} fps but DS Scoring says {project_rate}")
        return ixml_rate, "iXML", warnings, drop_frame
    if project_rate:
        return project_rate, "DS Scoring", warnings, drop_frame
    return DEFAULT_RATE, "default", warnings, drop_frame


def evaluate_mixdown(name: str, data: bytes, *, rate: str | None = None, mode: str = "warn") -> MixdownVerdict:
    """Run the stamp and silence checks on a finished mixdown already read into memory.

    `rate` is the project's frame rate from DS Scoring, used only when the export has no
    iXML frame rate. mode "warn": always publish; "block": a stamp FAIL is not published;
    "off": no checks. A silent mix, a WARN (no timecode in the name, drop-frame) or a
    frame-rate mismatch never blocks.
    """
    if mode == "off":
        return MixdownVerdict(True, None, None, [], None)
    stem = TC_RE.sub("", Path(name).stem).strip() or Path(name).stem  # "3m37 Help Us V1.2"
    rate_warnings, used_rate, source = [], rate or DEFAULT_RATE, "DS Scoring" if rate else "default"
    try:
        info = parse_wav(data)
    except ValueError as e:
        stamp, info = StampResult("FAIL", f"unreadable WAV: {e}", f"unreadable WAV ({e})"), None
    else:
        used_rate, source, rate_warnings, drop_frame = choose_rate(info, rate)
        if drop_frame:
            stamp = StampResult("WARN", f"drop-frame timecode ({used_rate} DF) — the check only does non-drop",
                                "drop-frame timecode, stamp not checked")
        else:
            stamp = check_stamp(name, info, used_rate, source)
    lines = [f"[bwf] {stamp.status} {name}: {stamp.detail}"]
    lines += [f"[bwf] WARN {name}: {w}" for w in rate_warnings]
    silence = None
    if info is not None and info.data_offset is not None:
        try:
            silence = silence_problem(data, info)
        except ValueError as e:
            lines.append(f"[bwf] {name}: couldn't scan for silence ({e})")

    blocked = mode == "block" and stamp.status == "FAIL"
    if silence:
        lines.append(f"[bwf] WARN {name}: {silence}")
    alerts = []
    if stamp.status == "FAIL":
        outcome = "not published" if blocked else "published anyway (BWF check is warn-only)"
        lines.append(f"[bwf] {name}: {outcome}")
        alerts.append(f"{stamp.summary}, {outcome}")
    if silence:
        alerts.append(silence)
    alerts += rate_warnings
    alert = f"{stem}: " + "; ".join(alerts) if alerts else None
    return MixdownVerdict(not blocked, stamp, silence, lines, alert, used_rate, source)


def main(argv: list[str]) -> int:
    rate, paths = None, []  # --fps: used for files without an iXML frame rate
    it = iter(argv)
    for a in it:
        if a == "--fps":
            rate = normalize_rate(next(it, "")) or ""
        elif a.startswith("--fps="):
            rate = normalize_rate(a.split("=", 1)[1]) or ""
        else:
            paths.append(Path(a))
    if (rate is not None and rate not in RATES) or not paths:
        print(__doc__)
        return 2
    files = []
    for p in paths:
        files += sorted(p.rglob("*.wav")) if p.is_dir() else [p]
    bad = 0
    for f in files:
        try:
            data = f.read_bytes()
        except OSError as e:
            bad += 1
            print(f"FAIL  {f.name}\n      {e}")
            continue
        verdict = evaluate_mixdown(f.name, data, rate=rate, mode="block")
        bad += verdict.stamp.status == "FAIL"
        print(f"{verdict.stamp.status}  {f.name}\n      {verdict.stamp.detail}")
        for line in verdict.log_lines[1:]:
            if "WARN" in line:
                print("      WARN " + line.split(": ", 1)[1])
    fallback = f"{rate} fps" if rate else f"{DEFAULT_RATE} fps"
    print(f"\n{len(files)} file(s), {bad} failed (frame rate from each file's iXML, else {fallback})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
