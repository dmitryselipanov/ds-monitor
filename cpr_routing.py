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


STEREO_OUT_TYPE = 18                   # the project's main output bus (Stereo Out)
STEREO = None                          # marker for "outputs to Stereo Out" in route lists


def track_routes(b: bytes) -> dict[str, list]:
    """{track name: its outputs, in order}: a group/FX channel name, or STEREO (None) for the
    Stereo Out bus. A track's mixer-channel records are used when it has any; otherwise only its
    first track (RecorderBus) record: later copies of that record often point at unrelated ids
    (seen on 3m30, 2026-11-14). Outputs that are neither a group nor Stereo Out are left out."""
    groups = group_names(b)
    stereo = {uid for uid, typ, _ in buses(b) if typ == STEREO_OUT_TYPE}
    def route(o):
        if o in groups:
            return groups[o]
        return STEREO if o in stereo else ...
    ch: dict[str, list] = {}
    for name, o in channel_outputs(b):
        ch.setdefault(name, []).append(route(o))
    first: dict[str, object] = {}
    for name, o in track_outputs(b):
        first.setdefault(name, route(o))
    out = {}
    for name in list(ch) + [n for n in first if n not in ch]:
        if not name or BUS_NAME.match(name):
            continue
        lst = []
        for r in (ch[name] if name in ch else [first[name]]):
            if r is not ... and r not in lst:
                lst.append(r)
        if lst:
            out[name] = lst
    return out


def output_groups(path: Path) -> dict[str, str]:
    return output_routing(path)[0]


def output_routing(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """({track name: chosen group}, {track name: every group its records point at, in order})."""
    try:
        routes = track_routes(Path(path).read_bytes())
    except Exception:
        return {}, {}
    result, every = {}, {}
    for name, outs in routes.items():
        gs = [g for g in outs if g and g[:2].isdigit()]
        if not gs:
            continue
        every[name] = gs
        # two tracks with the same name ("Ride" → 13_BAND ALL and 12g_SYNTH PERC HIGH DRY):
        # a section's own group beats a family ALL/SUM bus; otherwise the first wins
        result[name] = min(gs, key=lambda g: (_group_rank(g), gs.index(g)))
    return result, every


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
STEREO_BY_DESIGN = re.compile(r"^sketch\b", re.I)      # sketch instruments go to Stereo Out on purpose
NOT_A_TRACK = re.compile(r"^((stereo|mono) (out|in)\b|input|output)", re.I)


def routing_records(b: bytes) -> dict[str, list]:
    """{track name: every output it has, in order}: a section group name, or None for Stereo Out.
    Group/FX channels' own records and Cubase's input/output channels are skipped."""
    out = {}
    for name, outs in track_routes(b).items():
        if NOT_A_TRACK.match(name):
            continue
        lst = [g for g in outs if g is STEREO or g[:2].isdigit()]
        if lst:
            out[name] = lst
    return out


def template_records(template: Path) -> dict[str, list]:
    """The template's routing, cached until the template file changes (it's ~200 MB)."""
    st = template.stat()
    key = [2, str(template), st.st_size, st.st_mtime_ns]     # 2: channel records first, real Stereo Out
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
    audited, so its own routing isn't reported again). Kinds: same (one name, several groups),
    bus (into a SUM/RVB/FX), family (straight into a family ALL/COMBINED bus or 00_2MIX, skipping
    the section's DRY group: the old band-bus habit), none (Stereo Out or nothing)."""
    issues = []
    for name, outs in sorted(project.items(), key=lambda kv: kv[0].lower()):
        if UTILITY_TRACK.match(name.strip()):
            continue
        p = [g for g in outs if g]
        known = name in template
        t = set(g for g in template.get(name, []) if g)
        changed = [g for g in p if g not in t]
        # a track now going to Stereo Out (or nothing) where the template's track of that name
        # went to a group: re-routed, or a copy of it routed straight out
        lost_group = None in outs and (not known or None not in template[name]) \
            and not STEREO_BY_DESIGN.match(name)
        if known and not changed and not lost_group:
            continue
        if len(p) > 1 and changed:
            issues.append({"track": name, "kind": "same", "groups": p})
        elif any(_group_rank(g) == 1 for g in (changed if known else p)):
            issues.append({"track": name, "kind": "bus", "groups": [g for g in p if _group_rank(g) == 1][:1]})
        elif any(_group_rank(g) == 2 for g in (changed if known else p)) and not STEREO_BY_DESIGN.match(name):
            issues.append({"track": name, "kind": "family", "groups": [g for g in (changed if known else p) if _group_rank(g) == 2][:1]})
        elif lost_group:
            # "also": groups other records of this name go to (a disabled or hidden copy may be the
            # one on Stereo Out; only_enabled() decides with the export's track count)
            issues.append({"track": name, "kind": "none", "groups": [], "also": p})
    return issues


def check_project(project_cpr: Path, template_cpr: Path) -> list[dict]:
    return project_issues(routing_records(Path(project_cpr).read_bytes()), template_records(Path(template_cpr)))


def describe(issue: dict) -> str:
    g = issue["groups"]
    if issue["kind"] == "same":
        return f'{issue["track"]}: tracks with this name go to different groups ({", ".join(g)}). Rename one or fix its output.'
    if issue["kind"] == "bus":
        return f'{issue["track"]}: outputs into {g[0]}, not a section group. Usually it should go to that section\'s DRY group.'
    if issue["kind"] == "family":
        return f'{issue["track"]}: outputs straight into {g[0]}, skipping a section. Route it to its section\'s DRY group (band: 13a KIT … 13g VOX BGV).'
    return f'{issue["track"]}: outputs to Stereo Out (or nothing), not a section group.'


def enabled_tracks(project_cpr: Path) -> tuple[dict | None, Path | None]:
    """{name: how many tracks} enabled at the project's newest XML export (Cubase never
    exports disabled tracks), from the slim XMLs DS Monitor keeps next to the .cpr.
    (None, None) when there is no export yet. The .cpr has no readable "disabled" flag."""
    import json
    from xml_slim import is_slim
    folder = Path(project_cpr).parent
    xmls = sorted((x for x in folder.glob("*.xml") if is_slim(x)), key=lambda x: x.stat().st_mtime, reverse=True)
    for x in xmls:
        try:
            m = re.search(r"<ds_tracks><!\[CDATA\[(.*?)\]\]></ds_tracks>", x.read_text(encoding="utf-8", errors="replace"), re.S)
            if m:
                count: dict[str, int] = {}
                for t in json.loads(m.group(1)):
                    if t.get("name"):
                        count[t["name"]] = count.get(t["name"], 0) + 1
                return count, x
        except (OSError, ValueError):
            continue
    return None, None


def only_enabled(issues: list[dict], enabled: dict) -> tuple[list[dict], list[str]]:
    """Issues of tracks enabled at the last export ({name: count}), and the names left out.
    Records can't say which of several same-named channels is the enabled one, so a Stereo Out
    record is dropped when the export has just one track of that name and another record of it
    goes to a group (2m29: the visible SF Mandolin Grain → 12m, a hidden copy → Stereo Out)."""
    keep, skipped = [], []
    for i in issues:
        n = enabled.get(i["track"], 0)
        if not n or (i["kind"] == "none" and i.get("also") and n == 1):
            skipped.append(i["track"])
        else:
            keep.append(i)
    return keep, skipped


def short(issue: dict) -> str:
    """One short line for the menu and the result window."""
    g = issue["groups"]
    if issue["kind"] == "same":
        return f'{issue["track"]}: same name, different groups ({", ".join(g)})'
    if issue["kind"] == "bus":
        return f'{issue["track"]} → {g[0]} (not a DRY group)'
    if issue["kind"] == "family":
        return f'{issue["track"]} → {g[0]} (family bus, not a DRY group)'
    return f'{issue["track"]} → Stereo Out (no group)'


# The result window's sections, most common slip first: heading, then what to do about it.
KINDS = [
    ("family", "Into a family bus", "Skips the section. Route each to its section's DRY group "
               "(band: 13a KIT … 13g VOX BGV)."),
    ("none", "No group", "Goes straight to Stereo Out. Route each to its section's DRY group."),
    ("bus", "Into a SUM or reverb bus", "Not a section group. Usually it belongs in that section's DRY group."),
    ("same", "Same name, different groups", "Tracks sharing a name output to different groups. "
             "Rename one or fix its output."),
]


def target(issue: dict) -> str:
    """Where the track goes now, for the window's right-hand column."""
    if issue["kind"] == "none":
        return "Stereo Out"
    return " / ".join(issue["groups"])


def grouped(issues: list[dict]) -> list[tuple[str, str, str, list[dict]]]:
    """(kind, heading, hint, issues) for each kind present, in KINDS order."""
    out = []
    for kind, heading, hint in KINDS:
        rows = [i for i in issues if i["kind"] == kind]
        if rows:
            out.append((kind, heading, hint, rows))
    return out


def as_text(project: str, issues: list[dict], note: str = "") -> str:
    """The result as plain text, for the window's Copy button."""
    n = len(issues)
    lines = [f"{project}: {n} routing issue{'s' if n != 1 else ''}" if n else f"{project}: routing OK"]
    for _, heading, hint, rows in grouped(issues):
        lines += ["", f"{heading} ({len(rows)}): {hint}"]
        lines += [f"  {i['track']}  →  {target(i)}" for i in rows]
    if note:
        lines += ["", note]
    return "\n".join(lines)


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


def why(project: Path, template: Path):
    """One line per Check Routing issue: what the project's records and the template say."""
    b = Path(project).read_bytes()
    every = {uid: (typ, name) for uid, typ, name in buses(b)}
    def show(o):
        if o is None:
            return "none"
        typ, name = every.get(o, (None, "?"))
        return f"{name} (type {typ})"
    tmpl = template_records(Path(template))
    tr, ch = track_outputs(b), channel_outputs(b)
    for i in project_issues(routing_records(b), tmpl):
        n = i["track"]
        print(f"{n} [{i['kind']}]")
        print(f"   template: {tmpl.get(n, 'not in template')}")
        print(f"   track records:   {[show(o) for m, o in tr if m == n][:4]}")
        print(f"   channel records: {[show(o) for m, o in ch if m == n][:4]}")


KEY_VALUE = re.compile(rb"\x00\x00\x00([\x03-\x30])([A-Za-z][A-Za-z0-9 _]{1,46})\x00\x00([\x01\x02\x03\x04\x05\x06\x08])")


def keys(path: Path, names: list[str], before: int = 3000, after: int = 3000):
    """Key=value pairs stored around each named track's records, to find where a flag such as
    'disabled' lives: run it on a disabled and an enabled track and compare."""
    b = Path(path).read_bytes()
    for want in names:
        raw = want.encode() + b"\x00"
        hits, i = [], b.find(raw)
        while i >= 0 and len(hits) < 6:
            if b[i - 1:i] != b" ":
                hits.append(i)
            i = b.find(raw, i + 1)
        print(f"=== {want}: {len(hits)} occurrence(s)")
        for h in hits:
            seg = b[max(0, h - before):h + after]
            pairs = []
            for m in KEY_VALUE.finditer(seg):
                if len(m.group(2)) + 1 != m.group(1)[0]:
                    continue
                v = seg[m.end():m.end() + 8]
                pos = m.start() + max(0, h - before) - h
                pairs.append(f"{pos:+d} {m.group(2).decode()}={v.hex()}")
            print(f"  @{h}: " + " | ".join(pairs))


FLAG_WORDS = re.compile(rb"disab|activ|enab|frozen|freeze|hidden|offline|bypass|state|flag|mute|on$", re.I)


def flags(path: Path, names: list[str], span: int = 60000):
    """Every on/off-looking setting in the file (how often, which values), then for each named
    track the nearest one of each kind before and after its mixer channel. Compare a disabled
    and an enabled track to find which setting means 'disabled'."""
    b = Path(path).read_bytes()
    found = {}
    for m in KEY_VALUE.finditer(b):
        k = m.group(2)
        if len(k) + 1 != m.group(1)[0] or not FLAG_WORDS.search(k):
            continue
        found.setdefault(k.decode(), []).append((m.start(), b[m.end():m.end() + 8].hex()))
    print("== on/off-looking settings: count, distinct values")
    for k, v in sorted(found.items(), key=lambda kv: -len(kv[1])):
        vals = {}
        for _, x in v:
            vals[x] = vals.get(x, 0) + 1
        print(f"  {k}: {len(v)}  " + ", ".join(f"{x}×{n}" for x, n in sorted(vals.items(), key=lambda t: -t[1])[:4]))
    import bisect as bs
    for want in names:
        m = re.search(re.escape(STRING_VALUE) + rb"." + re.escape(want.encode()) + rb"\x00", b, re.S)
        i = m.start() if m else -1
        print(f"== {want} (mixer channel at {i})")
        if i < 0:
            continue
        for k, v in sorted(found.items()):
            pos = [p for p, _ in v]
            j = bs.bisect_left(pos, i)
            near = []
            if j > 0 and i - pos[j - 1] < span:
                near.append(f"before {i - pos[j - 1]}: {v[j - 1][1]}")
            if j < len(pos) and pos[j] - i < span:
                near.append(f"after {pos[j] - i}: {v[j][1]}")
            if near:
                print(f"  {k}: " + " | ".join(near))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--flags":
        flags(Path(sys.argv[2]), sys.argv[3:])
        sys.exit()
    if len(sys.argv) >= 3 and sys.argv[1] == "--keys":
        keys(Path(sys.argv[2]), sys.argv[3:])
        sys.exit()
    if len(sys.argv) >= 3 and sys.argv[1] == "--why":
        why(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else TEMPLATE_DEFAULT)
        sys.exit()
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
