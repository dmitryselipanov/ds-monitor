"""Decide when a Cubase mixdown WAV is finished and safe to upload.

Cubase writes the WAV header with placeholder sizes, streams the audio, and
patches the real RIFF and data sizes in last. That final patch doesn't change
the file's length, so a size-only check can pass on a file whose header still
says "no audio". Only a WAV whose header agrees with its length is uploaded.
"""

import struct
import subprocess
import time
from pathlib import Path

RIFF_HEADER = 12
CHUNK_HEADER = 8


def wav_header_problem(data: bytes, total_size: int | None = None) -> str | None:
    """Return why the header is unfinished or broken, or None if it is valid.

    `data` is the whole file, or just its start when `total_size` gives the
    real length (enough bytes to reach the data chunk's header).
    """
    size = len(data) if total_size is None else total_size
    if len(data) < RIFF_HEADER or data[8:12] != b"WAVE":
        return "not a WAVE file"
    tag = data[:4]
    riff_size = struct.unpack_from("<I", data, 4)[0]
    ds64_riff = ds64_data = None
    if tag == b"RF64":
        if data[12:16] != b"ds64" or len(data) < 36:
            return "RF64 without ds64 chunk"
        ds64_riff, ds64_data = struct.unpack_from("<QQ", data, 20)
        riff_size = ds64_riff
    elif tag != b"RIFF":
        return "not a RIFF file"
    if riff_size != size - 8:
        return f"RIFF size {riff_size} does not match file size {size} - 8"

    pos, have_fmt = RIFF_HEADER, False
    while pos + CHUNK_HEADER <= min(len(data), size):
        chunk_id, chunk_size = struct.unpack_from("<4sI", data, pos)
        if chunk_id == b"fmt ":
            have_fmt = True
        elif chunk_id == b"data":
            if tag == b"RF64" and chunk_size == 0xFFFFFFFF:
                chunk_size = ds64_data
            if not have_fmt:
                return "data chunk before fmt chunk"
            if chunk_size == 0:
                return "data size is zero"
            if pos + CHUNK_HEADER + chunk_size > size:
                return f"data size {chunk_size} runs past end of file"
            return None
        pos += CHUNK_HEADER + chunk_size + (chunk_size & 1)
    return "no data chunk"


def wav_file_problem(path: Path) -> str | None:
    """Check the header of a WAV on disk without reading the audio."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError as e:
        return str(e)
    return wav_header_problem(head, size)


def file_is_open(path: Path) -> bool:
    """True while any process (Cubase while exporting) still has the file open."""
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-t", "--", str(path)],
            capture_output=True, timeout=20,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True  # Unable to verify: keep waiting.


def read_finished_wav(path: Path, log, *, quiet_seconds=5.0, open_grace_seconds=120.0,
                      timeout=7200.0, poll_seconds=1.0, is_open=file_is_open):
    """Wait for a finished WAV and return (bytes, (size, mtime_ns)), or None.

    Finished means: unchanged for `quiet_seconds`, a valid header on disk, no
    process holding it open, and the bytes read back also have a valid header
    and the file didn't change while being read. A file that stays open but is
    otherwise finished for `open_grace_seconds` is accepted anyway (Cubase
    keeps a mixdown open if it imports it back into the project).
    """
    deadline = time.monotonic() + timeout
    last_identity = unchanged_since = None
    last_reason = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            log(f"[mixdown] file disappeared: {path}")
            return None
        identity = (stat.st_size, stat.st_mtime_ns)
        now = time.monotonic()
        if identity != last_identity:
            last_identity, unchanged_since = identity, now
        elif stat.st_size > 0 and now - unchanged_since >= quiet_seconds:
            reason = wav_file_problem(path)
            if reason is None and is_open(path):
                reason = "still open by another process"
                if now - unchanged_since >= open_grace_seconds:
                    log(f"[mixdown] {path.name} still open after {open_grace_seconds:.0f}s "
                        f"unchanged with a valid header — uploading anyway")
                    reason = None
            if reason is None:
                data = path.read_bytes()
                reason = wav_header_problem(data)
                try:
                    after = path.stat()
                except FileNotFoundError:
                    return None
                if reason is None and (after.st_size, after.st_mtime_ns) == identity:
                    return data, identity
                reason = reason or "changed while being read"
            if reason != last_reason:
                log(f"[mixdown] waiting for {path.name}: {reason}")
                last_reason = reason
        time.sleep(poll_seconds)
    log(f"[mixdown] gave up waiting for a finished WAV after {timeout:.0f}s ({last_reason}): {path}")
    return None
