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


def summarize(tracks: list[dict]) -> list[dict]:
    """One row per track that holds at least one part or audio region, in order:
    {n: position among them (1-based), name, group, audio, parts, notes, muted (all parts muted)}."""
    rows = []
    for t in tracks:
        parts = t.get("parts") or []
        if not parts:
            continue
        audio = sum(1 for p in parts if p.get("a"))
        rows.append({
            "n": len(rows) + 1,
            "name": t.get("name") or "(unnamed)",
            "group": t.get("g") or "",
            "audio": audio,
            "midi": len(parts) - audio,
            "notes": sum(len(p.get("n") or []) for p in parts),
            "muted": all((p.get("m") or 0) & MUTED for p in parts),
        })
    dup = {}
    for r in rows:
        dup[r["name"]] = dup.get(r["name"], 0) + 1
    for r in rows:
        r["same_name"] = dup[r["name"]]
    return rows


def label(r: dict) -> str:
    """One line for the window: name, then what it holds and where it goes."""
    what = []
    if r["midi"]:
        what.append(f"{r['midi']} MIDI part{'s' if r['midi'] != 1 else ''}, {r['notes']} note{'s' if r['notes'] != 1 else ''}")
    if r["audio"]:
        what.append(f"{r['audio']} audio region{'s' if r['audio'] != 1 else ''}")
    s = f"{r['name']}   ·  {', '.join(what)}"
    if r["group"]:
        s += f"  →  {r['group']}"
    if r["muted"]:
        s += "   (all muted)"
    return s


def report(path: Path) -> dict:
    """Everything the window shows for the picked .cpr or .xml:
    {project, export (Path | None), rows, total (tracks in the export), stale (bool), note}."""
    path = Path(path)
    export = export_for(path)
    out = {"project": path.stem, "export": export, "rows": [], "total": 0, "stale": False, "note": ""}
    if export is None:
        out["note"] = ("No XML export next to this project yet. In that cue: select all tracks → "
                       "File › Export › Selected Tracks, saved next to the .cpr with its name.")
        return out
    tracks = read_tracks(export)
    out["rows"] = summarize(tracks)
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
                notes.append("The project was saved after this export: export again for an up-to-date list.")
        except OSError:
            pass
    dups = sorted({r["name"] for r in out["rows"] if r["same_name"] > 1})
    if dups:
        notes.append("Same name more than once (tell apart by group/order): " + ", ".join(dups[:6])
                     + (" …" if len(dups) > 6 else ""))
    out["note"] = "\n".join(notes)
    return out


def as_text(rep: dict) -> str:
    """Plain-text version (for Copy and the command line)."""
    lines = [f"{rep['project']}: {len(rep['rows'])} tracks with data"
             + (f" (of {rep['total']} enabled at export)" if rep["export"] else "")]
    if rep["note"]:
        lines.append(rep["note"])
    lines += [f"{r['n']:>3}. {label(r)}" for r in rep["rows"]]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        print(as_text(report(Path(a))))
        print()
