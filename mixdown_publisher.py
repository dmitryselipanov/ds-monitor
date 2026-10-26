"""Publish finished Cubase mixdown WAVs into the local Dropbox cue folder.

Only review-ready mixes are published: the filename, minus version and
timecode, must be exactly "{cue} {title}" of a cue in DS Scoring, or else
exactly the name of the cue's Dropbox folder (when DS Scoring can't be reached,
or for a cue it doesn't have). A DS Scoring name goes into that cue's folder
even if the folder still carries an old title, and the folder is created when
the cue has none yet. Production versions with anything else in the name
("1m02 City Pulse - NO SAX") stay local.

The Dropbox desktop app syncs them, so there is no upload size limit and no
Dropbox token on this Mac. The mixdown stays in Cubase's Mixdown folder; the
already-validated bytes are written to a staging file on the same volume and
renamed into place, so Dropbox never sees a partial WAV. Older WAVs in the cue
folder move to Prev Versions first, as the Worker upload used to do.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from video_publisher import CUE_NAME, EPISODE_NAME, cue_from_filename, local_dropbox_project

PREV_VERSIONS = "Prev Versions"
STAGING_DIR = Path.home() / "Library" / "Caches" / "ds-monitor" / "mixdown-staging"

_publish_lock = threading.Lock()


def _free_name(folder: Path, name: str) -> Path:
    """`name`, or `name (1)`, `name (2)`… — like Dropbox's autorename."""
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while candidate.exists():
        candidate = folder / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _normalize(name: str) -> str:
    name = name.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", name).strip().casefold()


def _cue_folders(scoring_root: Path, cue_id: str) -> tuple[list[Path], list[Path]]:
    """(folders for `cue_id` under Scoring/ and its EPxx/ folders, the EPxx/ folders)."""
    if not scoring_root.is_dir():
        raise FileNotFoundError(f"Dropbox Scoring folder is unavailable: {scoring_root}")
    episodes = [p for p in scoring_root.iterdir() if p.is_dir() and EPISODE_NAME.fullmatch(p.name)]
    cue_folders = []
    for parent in [scoring_root] + episodes:
        for folder in parent.iterdir():
            match = CUE_NAME.match(folder.name)
            if folder.is_dir() and match and match.group("id").casefold() == cue_id:
                cue_folders.append(folder)
    return cue_folders, episodes


def review_folder(scoring_root: Path, wav_path: Path, cue_names: list[str] | None = None) -> Path | None:
    """The cue folder a review-ready mixdown belongs in, or None for a production version.

    `cue_names` are the project's "{cue} {title}" names from DS Scoring (None
    when unavailable). A mixdown with one of those names belongs in its cue's
    folder: the one with that exact name, else the cue's only folder that isn't
    named after a variant, else a new folder (returned without being created).

    Otherwise the cleaned filename must equal a cue folder's name, and no other
    folder for the same cue may be a shorter prefix of it: with "1m02 City Pulse"
    present, "1m02 City Pulse - FLUTE GUIDE" is a variant even though it has a folder.
    """
    cue_id, folder_name = cue_from_filename(wav_path)
    cue_folders, episodes = _cue_folders(scoring_root, cue_id)
    wanted = _normalize(folder_name)
    official = {}
    for name in cue_names or []:
        match = CUE_NAME.match(name.strip())
        if match and match.group("id").casefold() == cue_id:
            official[_normalize(name)] = re.sub(r"\s+", " ", name.replace("/", "-").replace(":", "-")).strip()

    if wanted in official:
        exact = [f for f in cue_folders if _normalize(f.name) == wanted]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None
        main = [f for f in cue_folders
                if not any(_normalize(f.name) != o and _normalize(f.name).startswith(o) for o in official)]
        if len(main) == 1:
            return main[0]
        if main:
            return None
        parent = scoring_root / f"EP{int(cue_id.split('m', 1)[0]):02d}" if episodes else scoring_root
        return parent / official[wanted]
    if any(wanted.startswith(o) for o in official):
        return None  # "{cue} {title}" plus more: a production version of a DS Scoring cue

    exact = [f for f in cue_folders if _normalize(f.name) == wanted]
    if len(exact) != 1:
        return None
    if any(f is not exact[0] and wanted.startswith(_normalize(f.name)) for f in cue_folders):
        return None
    return exact[0]


def publish_mixdown(wav_path: Path, wav_bytes: bytes, project: dict, log, *, cue_names: list[str] | None = None,
                    home: Path | None = None, staging_dir: Path | None = None) -> Path | None:
    """Write `wav_bytes` into the cue's Dropbox folder and return the new file.

    Returns None, writing nothing, when the mixdown isn't a review-ready version.
    """
    dropbox_project = local_dropbox_project(project["dropbox_base_path"], home)
    staging_dir = staging_dir or STAGING_DIR
    staging_dir.mkdir(parents=True, exist_ok=True)

    with _publish_lock:
        folder = review_folder(dropbox_project / "Scoring", wav_path, cue_names)
        if folder is None:
            return None
        if not folder.exists():
            folder.mkdir(parents=True)
            log(f"[mixdown] created cue folder {folder}")
        elif _normalize(folder.name) != _normalize(cue_from_filename(wav_path)[1]):
            log(f"[mixdown] cue folder is still named \"{folder.name}\"; DS Scoring calls it "
                f"\"{cue_from_filename(wav_path)[1]}\" — rename it in Dropbox when convenient")
        if staging_dir.stat().st_dev != folder.stat().st_dev:
            raise OSError(f"Staging {staging_dir} and Dropbox are on different volumes; atomic rename unavailable")
        fd, tmp_name = tempfile.mkstemp(dir=staging_dir, suffix=".wav")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)  # mkstemp makes it private to this user
            old_wavs = [p for p in folder.iterdir()
                        if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".wav"]
            if old_wavs:
                prev = folder / PREV_VERSIONS
                prev.mkdir(exist_ok=True)
                for old in old_wavs:
                    archived = _free_name(prev, old.name)
                    os.replace(old, archived)
                    log(f"[mixdown] moved previous version to {archived}")
            destination = folder / wav_path.name
            os.replace(tmp, destination)
        finally:
            tmp.unlink(missing_ok=True)
        if destination.stat().st_size != len(wav_bytes):
            raise OSError(f"Published WAV has unexpected size: {destination}")
        return destination
