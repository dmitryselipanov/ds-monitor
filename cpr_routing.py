"""Read each track's output group from a Cubase .cpr (read-only).

The .cpr format is undocumented. This is the reader from the Orchestral template work
(Obsidian: Cubase Orchestral Template/Tools/cprbus.py + cprtracks.py), which parsed the
whole template: 304 groups/FX channels, 1,835 track records, 1,507 unique names.

Usage: python3 cpr_routing.py "/path/project.cpr"   → prints every track and its output group

    output_groups(path) -> {track name: group name}, e.g. {"Propheeet": "12l_SYNTH PADS LOW DRY"}

Only names that resolve to a group or FX channel are returned; tracks that output to
Stereo Out, to nothing, or to something unresolvable are left out (DS Scoring then falls
back to the track name). Never raises on a malformed file: returns {} instead.
"""

import bisect
import re
import sys
from pathlib import Path

GROUP_TYPES = (14, 17)            # 14 = group channel, 17 = FX channel
BUS_KEY = b'\x00\x00\x00\x08Bus UID\x00\x00\x01'
BUS_TYPE = b'\x00\x00\x00\tBus Type\x00\x00\x01'
NAME_END = b'\x00\xef\xbb\xbf'    # names are UTF-8 followed by this terminator
NAME_START = b'\x00\x00\x08\x00\x00\x00'
TRACK_NAME = b'RecorderBus\x00\x00\x02\x00\x06\x00\x00\x00\x05\x00\x00\x00\x05Name\x00\x00\x08\x00\x00\x00'
BUS_NAME = re.compile(r"^\d\d[a-z]?_")
TRACK_OUT = b'OutputBusValue\x00\x00\x02\x00\x06\x00\x00\x00\x01\x00\x00\x00\x06Value\x00\x00\x01'


def _positions(b: bytes, pat: bytes) -> list[int]:
    out, i = [], b.find(pat)
    while i >= 0:
        out.append(i)
        i = b.find(pat, i + 1)
    return out


def buses(b: bytes) -> list[tuple[int, int | None, str]]:
    """(bus uid, bus type, name) for every named bus record (cprbus.py)."""
    rows = []
    for i in _positions(b, BUS_KEY):
        j = i + len(BUS_KEY)
        uid = int.from_bytes(b[j:j + 8], "big")
        k = j + 8
        typ = int.from_bytes(b[k + 15:k + 23], "big") if b[k:k + 15] == BUS_TYPE else None
        if b[i - 4:i] != NAME_END:
            continue
        e = i - 4
        st = b.rfind(NAME_START, max(0, e - 300), e)
        if st < 0:
            continue
        rows.append((uid, typ, b[st + 7:e].decode("utf-8", "replace")))
    return rows


def group_names(b: bytes) -> dict[int, str]:
    """bus uid → group/FX channel name. Group ids collide with port/instrument ids, so
    only groups and FX channels are kept (a track's section output is always one)."""
    names = {}
    for uid, typ, name in buses(b):
        if typ in GROUP_TYPES:
            names.setdefault(uid, name)
    return names


def track_outputs(b: bytes) -> list[tuple[str, int | None]]:
    """(track name, output bus uid) per track record (cprtracks.py)."""
    names = _positions(b, TRACK_NAME)
    outs = _positions(b, TRACK_OUT)
    rows, prev = [], 0
    for i in names:
        j = i + len(TRACK_NAME)
        n = b[j]
        name = b[j + 1:j + n].split(b"\x00")[0].decode("utf-8", "replace")
        k = bisect.bisect_left(outs, i) - 1           # last output value between the previous track and this one
        out = int.from_bytes(b[outs[k] + len(TRACK_OUT):outs[k] + len(TRACK_OUT) + 8], "big") \
            if k >= 0 and outs[k] >= prev else None
        rows.append((name, out))
        prev = j
    return rows


def output_groups(path: Path) -> dict[str, str]:
    try:
        b = Path(path).read_bytes()
        groups = group_names(b)
        result = {}
        for name, out in track_outputs(b):
            g = groups.get(out)
            # A name can have several records (stale copies in the file): keep the first
            # that points at a real section group ("07b_STR HIGH LONGS DRY").
            # Group and FX channels have records too ("01a_WW MELODY SUM" → "01_WW ALL"): skip them
            if name and g and name not in result and g[:2].isdigit() and not BUS_NAME.match(name):
                result[name] = g
        return result
    except Exception:
        return {}


def find_project(xml_path: Path) -> Path | None:
    """The .cpr an XML export belongs to: same name, else the newest .cpr in its folder
    (Cubase's numbered backups "name-01.cpr" are skipped)."""
    xml_path = Path(xml_path)
    same = xml_path.with_suffix(".cpr")
    if same.exists():
        return same
    cprs = [p for p in xml_path.parent.glob("*.cpr") if not re.search(r"-\d{2}$", p.stem)]
    return max(cprs, key=lambda p: p.stat().st_mtime) if cprs else None


def playing_tracks(xml_path: Path) -> list[str]:
    """Tracks that play in a Cubase XML export (full or slim): an unmuted note, or an unmuted audio
    region. Audio tracks get " (audio)" after their name."""
    import json
    text = Path(xml_path).read_text(encoding="utf-8", errors="replace")
    m = re.search(r"<ds_tracks><!\[CDATA\[(.*?)\]\]></ds_tracks>", text, re.S)
    if m:
        tracks = json.loads(m.group(1))
    else:
        from xml_slim import slim
        text, _ = slim(Path(xml_path))
        tracks = json.loads(re.search(r"<ds_tracks><!\[CDATA\[(.*?)\]\]></ds_tracks>", text, re.S).group(1))
    def plays(p):
        if p["m"] & 2:
            return False
        return bool(p.get("a")) or any(not (n[3] or 0) & 2 for n in p["n"])   # audio region, or an unmuted note
    return [t["name"] + (" (audio)" if any(p.get("a") for p in t["parts"]) else "") for t in tracks
            if any(plays(p) for p in t["parts"])]


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit('usage: python3 cpr_routing.py "/path/project.cpr" ["/path/cue export.xml"]\n'
                 '  with the XML: only the groups of tracks that play in that export')
    res = output_groups(Path(sys.argv[1]))
    if len(sys.argv) == 3:
        playing = [n.removesuffix(" (audio)") for n in playing_tracks(Path(sys.argv[2]))]
        missing = [n for n in playing if n not in res]
        res = {n: res[n] for n in playing if n in res}
        if missing:
            print("No output group (named from the track name instead): " + ", ".join(missing) + "\n")
    by_group = {}
    for name, g in res.items():
        by_group.setdefault(g, []).append(name)
    for g in sorted(by_group):
        names = sorted(by_group[g], key=str.lower)
        print(f"{g}  ({len(names)})\n    " + ", ".join(names))
    print(f"\n{len(res)} tracks in {len(by_group)} groups")
