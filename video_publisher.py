"""Publish completed Cubase video renders into the local Dropbox cue folder.

Cubase must export to PROJECTS/<project>/Video Exports, outside Dropbox. Only
finished video files are moved; Cubase's temporary audio never enters Dropbox.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path


STAGING_NAME = "Video Exports"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}
TIMECODE_SUFFIX = re.compile(r"\s+\d{2}[.:'’]\d{2}[.:'’]\d{2}[.:'’]\d{2}$")
VERSION_SUFFIX = re.compile(r"\s+V\d+(?:\.\d+)*$", re.IGNORECASE)
CUE_NAME = re.compile(r"^(?P<id>\d{1,3}m(?:\d{1,3}[a-z]?|[a-z]))[\s_-]+(?P<title>.+)$", re.IGNORECASE)
EPISODE_NAME = re.compile(r"^EP\d+$", re.IGNORECASE)


def project_for_staged_video(path: Path, watch_roots: list[Path]) -> Path | None:
    """Return the project folder only for a direct child of Video Exports."""
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return None
    for root in watch_roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) == 3 and relative.parts[1].casefold() == STAGING_NAME.casefold():
            return root / relative.parts[0]
    return None


def cue_from_filename(path: Path) -> tuple[str, str]:
    stem = TIMECODE_SUFFIX.sub("", path.stem).strip()
    stem = VERSION_SUFFIX.sub("", stem).strip()
    match = CUE_NAME.fullmatch(stem)
    if not match or not match.group("title").strip():
        raise ValueError(f"Video filename needs cue number and title: {path.name}")
    return match.group("id").casefold(), stem


def local_dropbox_project(base_path: str, home: Path | None = None) -> Path:
    """Map a project's Dropbox API path to its existing local Dropbox folder."""
    home = home or Path.home()
    parts = Path(base_path).parts
    if not base_path.startswith("/") or len(parts) < 2 or any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid Dropbox project path: {base_path!r}")
    relative = Path(*parts[1:])
    roots = [home / "Library/CloudStorage/Dropbox-Personal", home / "Dropbox (Personal)"]
    matches = [root / relative for root in roots if (root / relative).is_dir()]
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one synced Dropbox project folder for {base_path!r}; found {len(matches)}")
    return matches[0]


def cue_destination(scoring_root: Path, cue_id: str, folder_name: str) -> Path:
    """Reuse a cue folder by ID; create it in the existing flat/episode layout."""
    if not scoring_root.is_dir():
        raise FileNotFoundError(f"Dropbox Scoring folder is unavailable: {scoring_root}")
    candidates = []
    top = [p for p in scoring_root.iterdir() if p.is_dir()]
    for parent in [scoring_root] + [p for p in top if EPISODE_NAME.fullmatch(p.name)]:
        for folder in parent.iterdir():
            if not folder.is_dir():
                continue
            match = CUE_NAME.match(folder.name)
            if match and match.group("id").casefold() == cue_id:
                candidates.append(folder)
    exact = [folder for folder in candidates
             if folder.name.strip().casefold() == folder_name.strip().casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) > 1:
        raise ValueError(f"More than one Dropbox cue folder matches {cue_id}: {candidates}")
    if candidates:
        return candidates[0]
    episodes = [p for p in top if EPISODE_NAME.fullmatch(p.name)]
    parent = scoring_root
    if episodes:
        reel = int(cue_id.split("m", 1)[0])
        parent = scoring_root / f"EP{reel:02d}"
    destination = parent / folder_name
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def video_is_readable(path: Path) -> bool:
    """Cubase's completed preview must have a readable audio stream."""
    try:
        result = subprocess.run(
            ["/usr/bin/afinfo", str(path)], capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0 and "estimated duration:" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def video_is_open(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-t", "--", str(path)],
            capture_output=True, timeout=20,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True  # Unable to verify: leave the render alone.


def wait_for_complete(path: Path, *, quiet_seconds: float = 45, timeout: float = 7200,
                      poll_seconds: float = 5, readable=video_is_readable,
                      is_open=video_is_open) -> bool:
    """Require a quiet file, no open writer, and readable MP4 audio metadata."""
    deadline = time.monotonic() + timeout
    last_identity = None
    unchanged_since = None
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        identity = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size > 1024 * 1024 and identity == last_identity:
            if unchanged_since is not None and time.monotonic() - unchanged_since >= quiet_seconds:
                if not is_open(path) and readable(path):
                    try:
                        after = path.stat()
                    except FileNotFoundError:
                        return False
                    if (after.st_size, after.st_mtime_ns) == identity:
                        return True
        else:
            last_identity = identity
            unchanged_since = time.monotonic()
        time.sleep(poll_seconds)
    return False


class VideoPublisher:
    def __init__(self, watch_roots, resolve_project, log):
        self.watch_roots = [Path(root) for root in watch_roots]
        self.resolve_project = resolve_project
        self.log = log
        self.inflight = set()
        self.recent = {}  # (project folder, video stem) -> (created, published)
        self.lock = threading.Lock()
        self.publish_lock = threading.Lock()

    def is_video_render_wav(self, wav_path: Path) -> bool:
        """Recognize Cubase's companion WAV while its matching video is rendering."""
        wav_path = Path(wav_path)
        if wav_path.suffix.lower() != ".wav" or wav_path.parent.name.casefold() != "mixdown":
            return False
        for root in self.watch_roots:
            try:
                relative = wav_path.relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) < 4:
                continue
            project = root / relative.parts[0]
            stem = wav_path.stem.strip().casefold()
            try:
                wav_stat = wav_path.stat()
                wav_created = getattr(wav_stat, "st_birthtime", wav_stat.st_ctime)
            except FileNotFoundError:
                return False
            staging = project / STAGING_NAME
            if staging.is_dir():
                for video in staging.iterdir():
                    if video.suffix.lower() not in VIDEO_SUFFIXES or video.stem.casefold() != stem:
                        continue
                    try:
                        video_stat = video.stat()
                    except FileNotFoundError:
                        continue
                    video_created = getattr(video_stat, "st_birthtime", video_stat.st_ctime)
                    if abs(wav_created - video_created) <= 3600:
                        return True
            with self.lock:
                recent = self.recent.get((project, stem))
            if recent and recent[0] - 60 <= wav_created <= recent[1] + 10:
                return True
        return False

    def schedule(self, path: Path) -> bool:
        path = Path(path)
        if project_for_staged_video(path, self.watch_roots) is None:
            return False
        with self.lock:
            if path in self.inflight:
                return True
            self.inflight.add(path)
        threading.Thread(target=self._publish, args=(path,), daemon=True).start()
        return True

    def scan_staging(self):
        """Retry files missed during a restart, or left after an offline error."""
        for root in self.watch_roots:
            if not root.is_dir():
                continue
            for project in root.iterdir():
                staging = project / STAGING_NAME
                if staging.is_dir():
                    for path in staging.iterdir():
                        if path.is_file():
                            self.schedule(path)

    def _publish(self, path: Path):
        try:
            self.log(f"[video] waiting for completed render: {path}")
            if not wait_for_complete(path):
                self.log(f"[video] render not complete; left in staging: {path}")
                return
            with self.publish_lock:
                finished_stat = path.stat()
                project = self.resolve_project(path)
                if not project or not project.get("dropbox_base_path"):
                    raise ValueError(f"No DS Scoring project or Dropbox destination for {path}")
                cue_id, folder_name = cue_from_filename(path)
                dropbox_project = local_dropbox_project(project["dropbox_base_path"])
                folder = cue_destination(dropbox_project / "Scoring", cue_id, folder_name)
                destination = folder / path.name
                if path.stat().st_dev != folder.stat().st_dev:
                    raise OSError("Staging and Dropbox are on different volumes; atomic move unavailable")
                source_stat = path.stat()
                if ((source_stat.st_size, source_stat.st_mtime_ns) !=
                        (finished_stat.st_size, finished_stat.st_mtime_ns) or video_is_open(path)):
                    raise OSError("Video changed or reopened after completion check; retrying later")
                expected_size = source_stat.st_size
                old_videos = []
                for candidate in folder.iterdir():
                    if candidate == destination or candidate.suffix.lower() not in VIDEO_SUFFIXES:
                        continue
                    if not candidate.is_file() or candidate.is_symlink():
                        continue
                    try:
                        candidate_id, _ = cue_from_filename(candidate)
                    except ValueError:
                        continue
                    if candidate_id == cue_id:
                        old_videos.append(candidate)
                os.replace(path, destination)  # Publish the complete video before removing old versions.
                if destination.stat().st_size != expected_size:
                    raise OSError(f"Moved video has unexpected size: {destination}")
                for old_video in old_videos:
                    try:
                        old_video.unlink()
                        self.log(f"[video] removed previous cue video: {old_video}")
                    except OSError as exc:
                        self.log(f"[video] new video published, but old video cleanup failed: {old_video}: {exc}")
                with self.lock:
                    self.recent[(project_for_staged_video(path, self.watch_roots), path.stem.casefold())] = (
                        getattr(source_stat, "st_birthtime", source_stat.st_ctime), time.time()
                    )
                self.log(f"[video] moved completed {expected_size}-byte video to Dropbox: {destination}")
        except Exception as exc:
            self.log(f"[video] publish failed; staged file kept: {path}: {exc}")
        finally:
            with self.lock:
                self.inflight.discard(path)
