"""Which tracks of a cue hold MIDI or audio, read from the slim XML next to its .cpr.

Use: pick the cue you want to import tracks *from*. Cubase's Import Tracks from Project
dialog lists every one of the template's ~1,500 tracks with no hint which hold anything;
this list shows only the ones that do, in track-list order, so they can be ticked off
while selecting them in that dialog, without opening the source project.

Source: the cue's XML export (File > Export > Selected Tracks, slimmed by DS Monitor).
Cubase never exports disabled tracks, so every track listed was enabled at that export.
A track that holds parts but is disabled isn't in the export and can't be listed.
"""

import json
import re
from pathlib import Path

MUTED = 2
_TRACKS = re.compile(r"<ds_tracks><!\[CDATA\[(.*?)\]\]></ds_tracks>", re.S)


def _is_cubase_xml(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(512)
    except OSError:
        return False
    return any(f"<{r}" in head for r in ("tracklist2", "tracklist"))


def export_for(path: Path) -> Path | None:
    """The XML export of a cue: `path` itself if it's an .xml, else the .xml with the .cpr's
    name, else the newest slim XML in the .cpr's folder, else its newest Cubase XML.
    Full (not yet slimmed) and older exports count too."""
    from xml_slim import is_slim
    path = Path(path)
    if path.suffix.lower() == ".xml":
        return path if _is_cubase_xml(path) else None
    same = path.with_suffix(".xml")
    if same.exists() and _is_cubase_xml(same):
        return same
    xmls = [x for x in path.parent.glob("*.xml") if not x.name.startswith(".") and _is_cubase_xml(x)]
    slims = [x for x in xmls if is_slim(x)]
    pick = slims or xmls
    return max(pick, key=lambda x: x.stat().st_mtime) if pick else None


def read_tracks(xml_path: Path) -> list[dict]:
    """The tracks of an XML export in the export's order (Cubase's track-list order): the
    ds_tracks list of a slim file, or read from a full export in memory (the file isn't changed)."""
    from xml_slim import is_slim, slim
    xml_path = Path(xml_path)
    if is_slim(xml_path):
        text = xml_path.read_text(encoding="utf-8", errors="replace")
    else:
        text, _ = slim(xml_path)
    m = _TRACKS.search(text)
    if not m:
        raise ValueError(f"{xml_path.name} has no track list")
    return json.loads(m.group(1).replace("]]]]><![CDATA[>", "]]>"))


# Template families, by the number their section groups start with (01a_WW MELODY DRY → 01).
FAMILIES = {"01": "Woodwinds", "02": "Brass", "03": "Orchestral Percussion", "04": "Ethnic Percussion",
            "05": "Harp", "06": "Piano", "07": "Strings", "08": "Choir", "09": "Orchestra",
            "10": "Orchestra", "11": "Orchestra", "12": "Synths & Design", "13": "Band",
            "14": "Ethnic & Misc", "15": "Sampling & Ext"}
# Folder colours of the template's families (read by eye from a track-list screenshot,
# 2026-11-26; tracks inside use shades of these). Families without one show no dot.
COLOURS = {"Woodwinds": "#0BA3D9", "Brass": "#D4AE00", "Orchestral Percussion": "#5C4A30",
           "Ethnic Percussion": "#5C4A30", "Harp": "#E0620F", "Piano": "#E0620F",
           "Strings": "#3D7A12", "Choir": "#8E7CC3", "Synths & Design": "#5C3622",
           "Sampling & Ext": "#5C3622", "Ethnic & Misc": "#0B80B2"}
UTILITY = {"dx", "sfx", "temp", "video", "cue", "video marker", "music marker", "signature", "tempo"}


def family(group: str) -> str | None:
    m = re.match(r"(\d\d)", group or "")
    return FAMILIES.get(m.group(1)) if m else None


def summarize(tracks: list[dict]) -> list[dict]:
    """One row per track that holds at least one part or audio region, in order:
    {name, group, family, midi, audio, notes, muted (every part muted), same_name}.
    For a track with some unmuted parts, only those are counted."""
    rows, fam = [], None
    for t in tracks:
        parts = t.get("parts") or []
        if not parts:
            continue
        live = [p for p in parts if not (p.get("m") or 0) & MUTED]
        counted = live or parts
        name = t.get("name") or "(unnamed)"
        g = t.get("g") or ""
        if name.strip().lower() in UTILITY:   # template utility rows (Dx, Sfx, Temp …): never imported
            continue
        f = family(g) or fam or "Other"         # ungrouped tracks (audio takes) stay in the section above
        fam = f
        audio = sum(1 for p in counted if p.get("a"))
        rows.append({
            "name": name, "group": g, "family": f,
            "audio": audio, "midi": len(counted) - audio,
            "notes": sum(len(p.get("n") or []) for p in counted if not p.get("a")),
            "muted": not live,
        })
    dup = {}
    for r in rows:
        dup[r["name"]] = dup.get(r["name"], 0) + 1
    for r in rows:
        r["same_name"] = dup[r["name"]]
    return rows


def sections(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """Rows grouped under a heading each time the family changes, keeping track-list order."""
    out = []
    for r in rows:
        if not out or out[-1][0] != r["family"]:
            out.append((r["family"], []))
        out[-1][1].append(r)
    return out


def detail(r: dict) -> str:
    """What a track holds, short: "38 notes", "10 audio regions", "1 empty part"."""
    what = []
    if r["midi"]:
        what.append(f"{r['notes']} note{'s' if r['notes'] != 1 else ''}" if r["notes"]
                    else f"{r['midi']} empty part{'s' if r['midi'] != 1 else ''}")
    if r["audio"]:
        what.append(f"{r['audio']} audio region{'s' if r['audio'] != 1 else ''}")
    return " + ".join(what)


def report(path: Path) -> dict:
    """Everything the window shows for the picked .cpr or .xml:
    {project, export (Path | None), rows (playing), muted (every part muted), total (tracks in
    the export), stale (bool), note}."""
    path = Path(path)
    export = export_for(path)
    out = {"project": path.stem, "export": export, "rows": [], "muted": [], "total": 0,
           "stale": False, "note": ""}
    if export is None:
        out["note"] = ("No XML export next to this project yet. In that cue: select all tracks → "
                       "File › Export › Selected Tracks, saved next to the .cpr with its name.")
        return out
    tracks = read_tracks(export)
    rows = summarize(tracks)
    out["rows"] = [r for r in rows if not r["muted"]]
    out["muted"] = [r for r in rows if r["muted"]]
    out["total"] = len(tracks)
    notes = []
    if not tracks:
        notes.append(f"{export.name} holds no tracks (probably an older markers-only export). "
                     "In that cue: select all tracks → File › Export › Selected Tracks, saved over it.")
    if export.stem != path.stem and path.suffix.lower() == ".cpr":
        notes.append(f"Read from {export.name} (no export named like this .cpr).")
    if path.suffix.lower() == ".cpr":
        try:
            if path.stat().st_mtime > export.stat().st_mtime + 60:
                out["stale"] = True
                notes.append("Saved after this export: export again for an up-to-date list.")
        except OSError:
            pass
    dups = sorted({r["name"] for r in out["rows"] if r["same_name"] > 1})
    if dups:
        notes.append("More than one track named " + ", ".join(dups[:6]) + (" …" if len(dups) > 6 else ""))
    out["note"] = "\n".join(notes)
    return out


def as_text(rep: dict) -> str:
    """Plain-text version (for Copy and the command line)."""
    lines = [f"{rep['project']}: {len(rep['rows'])} tracks with data"]
    if rep["note"]:
        lines.append(rep["note"])
    for title, rows in sections(rep["rows"]):
        lines += ["", title.upper()] + [f"  {r['name']}  ({detail(r)})" for r in rows]
    if rep["muted"]:
        lines += ["", "MUTED"] + [f"  {r['name']}  ({detail(r)})" for r in rep["muted"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        print(as_text(report(Path(a))))
        print()
