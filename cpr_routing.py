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


CHANNEL = b"VST Multitrack"            # every audio/instrument channel record starts with this
STRING_VALUE = b"String\x00\x00\x08\x00\x00\x00"   # then ...Name ... String <len><name>


def channel_outputs(b: bytes) -> list[tuple[str, int | None]]:
    """(channel name, output bus uid) per mixer channel. Audio tracks only have this kind of
    record (no RecorderBus name), instrument tracks have both. The output is the first
    OutputBusValue after the channel's name and before the next channel."""
    chans = _positions(b, CHANNEL)
    outs = _positions(b, TRACK_OUT)
    rows = []
    for k, c in enumerate(chans):
        sv = b.find(STRING_VALUE, c, c + 300)
        if sv < 0:
            continue
        j = sv + len(STRING_VALUE)
        n = b[j]
        name = b[j + 1:j + n].split(b"\x00")[0].decode("utf-8", "replace").strip()
        end = chans[k + 1] if k + 1 < len(chans) else len(b)
        o = bisect.bisect_left(outs, j)
        out = int.from_bytes(b[outs[o] + len(TRACK_OUT):outs[o] + len(TRACK_OUT) + 8], "big") \
            if o < len(outs) and outs[o] < end else None
        rows.append((name, out))
    return rows


def _group_rank(g: str) -> int:
    """0 = a section's own group (12g_... DRY, 12a_SUB BOOMS), 1 = SUM/RVB/FX, 2 = family ALL/COMBINED."""
    if re.search(r" (ALL|COMBINED)$", g) or re.match(r"^\d\d_", g):
        return 2
    if re.search(r" (SUM|RVB|HALL|SUPERHALL|ROOM|PLATE|DLY|SEND \d)$", g):
        return 1
    return 0


def output_groups(path: Path) -> dict[str, str]:
    return output_routing(path)[0]


def output_routing(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """({track name: chosen group}, {track name: every group its records point at, in order})."""
    try:
        b = Path(path).read_bytes()
        groups = group_names(b)
        result, rank, every = {}, {}, {}
        # instrument tracks' RecorderBus records first (proven on the template), then mixer
        # channel records, which also cover audio tracks
        for order, (name, out) in enumerate(track_outputs(b) + channel_outputs(b)):
            g = groups.get(out)
            # Group and FX channels have records too ("01a_WW MELODY SUM" → "01_WW ALL"): skip them
            if not (name and g and g[:2].isdigit() and not BUS_NAME.match(name)):
                continue
            # A name can have several records: stale copies, or two tracks with the same name
            # ("Ride" → 12g_SYNTH PERC HIGH DRY and "Ride" → 13_BAND ALL). A section's own group
            # beats a family ALL/SUM bus; otherwise the first record wins.
            r = (_group_rank(g), order)
            if g not in every.setdefault(name, []):
                every[name].append(g)
            if name not in rank or r < rank[name]:
                result[name], rank[name] = g, r
        return result, every
    except Exception:
        return {}, {}


UTILITY_TRACK = re.compile(r"^(dx|sfx|fx|temp|temp music|dialog(ue)?|video|cue|video marker|music marker|signature|tempo)( \d+)?$", re.I)


def routing_issues(tracks: list[dict], chosen: dict[str, str], every: dict[str, list[str]]) -> list[dict]:
    """Routing slips among the tracks that play in an export (slim ds_tracks):
    - same:    two or more tracks with this name output to different groups
    - bus:     output into a SUM, RVB or reverb FX channel instead of a section group
    - none:    a music track that plays but outputs to no group (Stereo Out, nothing)
    Only checked when the project's routing could be read at all."""
    if not chosen:
        return []
    def plays(t):
        return any(not p["m"] & 2 and (p.get("a") or any(not (n[3] or 0) & 2 for n in p["n"])) for p in t["parts"])
    playing = [t for t in tracks if plays(t)]
    count = {}
    for t in tracks:
        count[t["name"]] = count.get(t["name"], 0) + 1
    issues, seen = [], set()
    for t in playing:
        name = t["name"]
        if name in seen or UTILITY_TRACK.match(name.strip()):
            continue
        seen.add(name)
        g = chosen.get(name)
        if count[name] > 1 and len(every.get(name, [])) > 1:
            issues.append({"track": name, "kind": "same", "groups": every[name]})
        elif g and _group_rank(g) == 1:
            issues.append({"track": name, "kind": "bus", "groups": [g]})
        elif not g:
            issues.append({"track": name, "kind": "none", "groups": []})
    return issues


# ── Check Routing (menu item): a saved project compared with the template ─────────────────
TEMPLATE_DEFAULT = Path.home() / "Library" / "Preferences" / "Cubase 15" / "Project Templates" / "Orchestral_v16 Colors.cpr"
TEMPLATE_CACHE = Path.home() / "Library" / "Caches" / "ds-monitor" / "template-routing.json"
NOT_A_TRACK = re.compile(r"^((stereo|mono) (out|in)\b|input|output)", re.I)


def routing_records(b: bytes) -> dict[str, list]:
    """{track name: every output it has, in order}: a group name, or None when the output isn't
    a group or FX channel (Stereo Out, nothing). Group/FX channels' own records are skipped."""
    groups = group_names(b)
    out: dict[str, list] = {}
    for name, o in track_outputs(b) + channel_outputs(b):
        if not name or BUS_NAME.match(name) or NOT_A_TRACK.match(name):
            continue
        g = groups.get(o)
        g = g if g and g[:2].isdigit() else None
        lst = out.setdefault(name, [])
        if g not in lst:
            lst.append(g)
    return out


def template_records(template: Path) -> dict[str, list]:
    """The template's routing, cached until the template file changes (it's ~200 MB)."""
    st = template.stat()
    key = [str(template), st.st_size, st.st_mtime_ns]
    try:
        import json
        cached = json.loads(TEMPLATE_CACHE.read_text())
        if cached.get("key") == key:
            return cached["records"]
    except Exception:
        pass
    records = routing_records(template.read_bytes())
    try:
        import json
        TEMPLATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TEMPLATE_CACHE.write_text(json.dumps({"key": key, "records": records}))
    except OSError:
        pass
    return records


def project_issues(project: dict[str, list], template: dict[str, list]) -> list[dict]:
    """Routing slips in tracks added or re-routed since the template (the template itself was
    audited, so its own routing isn't reported again)."""
    issues = []
    for name, outs in sorted(project.items(), key=lambda kv: kv[0].lower()):
        if UTILITY_TRACK.match(name.strip()):
            continue
        p = [g for g in outs if g]
        known = name in template
        t = set(g for g in template.get(name, []) if g)
        changed = [g for g in p if g not in t]
        if known and not changed:
            continue
        if len(p) > 1 and changed:
            issues.append({"track": name, "kind": "same", "groups": p})
        elif any(_group_rank(g) == 1 for g in (changed if known else p)):
            issues.append({"track": name, "kind": "bus", "groups": [g for g in p if _group_rank(g) == 1][:1]})
        elif not p and not known:
            issues.append({"track": name, "kind": "none", "groups": []})
    return issues


def check_project(project_cpr: Path, template_cpr: Path) -> list[dict]:
    return project_issues(routing_records(Path(project_cpr).read_bytes()), template_records(Path(template_cpr)))


def describe(issue: dict) -> str:
    g = issue["groups"]
    if issue["kind"] == "same":
        return f'{issue["track"]}: tracks with this name go to different groups ({", ".join(g)}). Rename one or fix its output.'
    if issue["kind"] == "bus":
        return f'{issue["track"]}: outputs into {g[0]}, not a section group. Usually it should go to that section\'s DRY group.'
    return f'{issue["track"]}: plays but outputs to no group (Stereo Out or nothing).'


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


def debug(path: Path, names: list[str]):
    """Print what the file holds around each named track, to see how its routing is stored."""
    b = Path(path).read_bytes()
    every = {uid: (typ, name) for uid, typ, name in buses(b)}
    groups = group_names(b)
    recs = track_outputs(b)
    for want in names:
        print(f"=== {want}")
        mine = [(n, o) for n, o in recs if n == want]
        print(f"  track records (RecorderBus name): {len(mine)}")
        for n, o in mine[:5]:
            print(f"    output id {o} -> group {groups.get(o)!r}, any bus {every.get(o)!r}")
        ch = [(n, o) for n, o in channel_outputs(b) if n == want]
        print(f"  channel records (VST Multitrack): {len(ch)}")
        for n, o in ch[:5]:
            print(f"    output id {o} -> group {groups.get(o)!r}, any bus {every.get(o)!r}")
        raw = want.encode()
        for c in _positions(b, CHANNEL):
            if raw in b[c:c + 300]:
                k = b.find(b"String", c, c + 300)
                print(f"  bytes after 'String': {b[k:k + 24 + len(raw)]!r}")
                break
        hits, i = [], b.find(raw)
        while i >= 0 and len(hits) < 12:
            hits.append(i)
            i = b.find(raw, i + 1)
        print(f"  name found {len(hits)}{'+' if len(hits) == 12 else ''} times in the file")
        for h in hits[:8]:
            before = re.findall(rb"[A-Za-z][A-Za-z ]{2,}", b[max(0, h - 160):h])
            after = re.findall(rb"[A-Za-z][A-Za-z ]{2,}", b[h + len(raw):h + len(raw) + 400])
            print(f"   @{h}: before {[x.decode() for x in before[-6:]]}")
            print(f"           after  {[x.decode() for x in after[:14]]}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--debug":
        debug(Path(sys.argv[2]), sys.argv[3:])
        sys.exit()
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
