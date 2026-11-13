"""Shrink a full Cubase XML export to what DS Scoring needs, then replace the original.

A full "Export > Selected Tracks" XML of a cue is ~250 MB, almost all plugin state,
mixer settings, controller data and audio hit points. DS Scoring needs only:

- the cue markers (MRangeMarkerEvent) and other markers, copied verbatim so the
  existing importer reads them exactly as before; each is tagged with its marker
  track (ds_track="Cue", "Video Marker", ...)
- the project tempo map and time signatures, copied verbatim. Audio files carry
  tempo maps of their own; those are renamed xMTempoEvent so the importer skips them
- every MIDI/instrument track: name, parts (position, length, offset, mute flag)
  and notes (start, length, pitch, mute flag), as one JSON block in <ds_tracks>
- every audio track's regions (position, length, mute flag; "a": 1, no notes), so DS
  Scoring can show which sounds a cue uses even when they're audio

Disabled tracks are not in Cubase's export at all; muted parts/notes carry Flags bit 2.

The slim file keeps the original's name (so "2m29 The Gift V2.xml" still matches
its .cpr) and the original is deleted only after the slim file has been written,
read back and checked against what was found in the original.
"""

import html
import json
import os
import re
from collections import Counter
from pathlib import Path

SLIM_MARK = 'ds_slim="1"'
CUBASE_ROOTS = ("tracklist2", "tracklist")
COPY = {"MRangeMarkerEvent", "MMarkerEvent", "MTempoEvent", "MTimeSignatureEvent"}
NOT_TRACKS = {"MTempoTrackEvent", "MSignatureTrackEvent", "MAutomationTrackEvent"}
MUTED = 2
_ATTR = re.compile(r'(\w+)="([^"]*)"')


class SlimError(Exception):
    """The file was left untouched; the message says why."""


def is_slim(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return SLIM_MARK in f.read(512)
    except OSError:
        return False


def _pieces(f, chunk=1 << 20):
    """Yield the text between consecutive '<' characters, streaming."""
    buf = ""
    while True:
        c = f.read(chunk)
        if not c:
            break
        buf += c
        parts = buf.split("<")
        buf = parts.pop()
        yield from parts
    yield buf


def _num(v):
    f = float(v)
    return int(f) if f == int(f) else round(f, 3)


def slim(src: Path, groups: dict | None = None) -> tuple[str, dict]:
    """Return (slim XML text, stats). Raises SlimError if src isn't a complete Cubase XML.
    groups: {track name: output group} from the project's .cpr (cpr_routing), added as "g"."""
    stack = []                       # (class, ID) per open element
    blocks, blk = [], None           # verbatim copies of markers / tempo / signature
    tracks, cur, tstack = [], None, []
    part = note = None
    in_apart = False                 # inside an audio part: its events are covered by the part
    refs = Counter()                 # how often each Tempo/Signature track is referenced
    root = None
    closed_root = False

    def owner(cls):
        for c, i in reversed(stack):
            if c == cls:
                return i

    with open(src, encoding="utf-8", errors="replace") as f:
        for p in _pieces(f):
            if not p or p[0] in "?!":
                continue
            tag, _, text = p.partition(">")
            if tag.startswith("/"):
                if not stack:
                    raise SlimError("unbalanced closing tag")
                if blk:
                    blk["x"].append("<" + tag + ">")
                cls = stack.pop()[0]
                if not stack:
                    closed_root = True
                if blk and len(stack) < blk["d"]:
                    blocks.append(blk)
                    blk = None
                if cls == "MMidiNote" and note is not None and part is not None:
                    part["n"].append(note)
                    note = None
                elif cls in ("MMidiPartEvent", "MAudioPartEvent") or (cls == "MAudioEvent" and not in_apart):
                    part = None
                if cls == "MAudioPartEvent":
                    in_apart = False
                if tstack and len(stack) < tstack[-1][0]:
                    tstack.pop()
                    cur = tstack[-1][1] if tstack else None
                continue
            selfc = tag.endswith("/")
            name = tag.split(None, 1)[0].rstrip("/")
            if root is None:
                root = name
                if root not in CUBASE_ROOTS:
                    raise SlimError(f"not a Cubase track export (root <{root}>)")
            elif not stack:
                raise SlimError("content after the end of the document")
            a = dict(_ATTR.findall(tag)) if "=" in tag else {}
            if SLIM_MARK.split("=")[0] in a:
                raise SlimError("already slim")
            cls = a.get("class", "") if name == "obj" else ""
            if name == "obj" and a.get("name") in ("Tempo Track", "Signature Track") and "ID" in a:
                refs[a["ID"]] += 1
            if blk is None and (cls in COPY or (cls.endswith("MarkerEvent") and not cls.endswith("TrackEvent"))):
                blk = {"d": len(stack) + 1, "cls": cls, "x": [],
                       "tempo": owner("MTempoTrackEvent"), "sig": owner("MSignatureTrackEvent"),
                       "track": cur["name"] if cur else None}
            if blk:
                blk["x"].append("<" + tag + ">" + (text if len(text) < 200 else ""))
            if cls.endswith("TrackEvent") and cls not in NOT_TRACKS:
                cur = {"cls": cls[1:-10], "name": None, "parts": []}
                tracks.append(cur)
                tstack.append((len(stack) + 1, cur))
            elif cls == "MMidiPartEvent" and cur is not None:
                part = {"s": None, "l": None, "o": 0, "m": 0, "n": []}
                cur["parts"].append(part)
            elif cls == "MMidiNote" and part is not None:
                note = [None, None, None, 0]
            elif cls == "MAudioPartEvent" and cur is not None:
                part = {"s": None, "l": None, "o": 0, "m": 0, "a": 1, "n": []}
                cur["parts"].append(part)
                in_apart = True
            elif cls == "MAudioEvent" and cur is not None and not in_apart:
                part = {"s": None, "l": None, "o": 0, "m": 0, "a": 1, "n": []}
                cur["parts"].append(part)
            if name in ("float", "int", "string") and "name" in a:
                k, v = a["name"], a.get("value", "")
                top = stack[-1][0] if stack else ""
                if note is not None and top == "MMidiNote":
                    if k == "Start": note[0] = _num(v)
                    elif k == "Length": note[1] = _num(v)
                    elif k == "Data1": note[2] = int(v)
                    elif k == "Flags": note[3] = int(v)
                elif part is not None and (top == "MMidiPartEvent" or (part.get("a") and (top == "MAudioPartEvent" or (top == "MAudioEvent" and not in_apart)))):
                    if k == "Start": part["s"] = _num(v)
                    elif k == "Length": part["l"] = _num(v)
                    elif k == "Offset": part["o"] = _num(v)
                    elif k == "Flags": part["m"] = int(v)
                elif cur is not None and k == "Name" and cur["name"] is None:
                    cur["name"] = html.unescape(v)
            if not selfc:
                stack.append((cls, a.get("ID")))
            elif blk and len(stack) < blk["d"]:
                blocks.append(blk)
                blk = None

    if root is None:
        raise SlimError("empty file")
    if stack or not closed_root:
        raise SlimError("incomplete XML (Cubase may still be writing it)")

    # The project tempo/signature track is the one every track's Domain points at.
    tdef = Counter(b["tempo"] for b in blocks if b["cls"] == "MTempoEvent")
    sdef = Counter(b["sig"] for b in blocks if b["cls"] == "MTimeSignatureEvent")
    ptempo = max(tdef, key=lambda i: (refs[i], tdef[i])) if tdef else None
    psig = max(sdef, key=lambda i: (refs[i], sdef[i])) if sdef else None

    for t in tracks:                 # an audio part's own events would otherwise count twice
        t["parts"] = [p for p in t["parts"] if not (p.get("a") and p["s"] is None)]
    tracks = [t for t in tracks if t["parts"] or t["cls"] in ("Instrument", "Midi")]
    for t in tracks:
        if groups and groups.get(t["name"]):
            t["g"] = groups[t["name"]]
    seen, keep = set(), []
    for b in blocks:
        x = re.sub(r">\s+<", "><", "".join(b["x"]))
        other = ((b["cls"] == "MTempoEvent" and b["tempo"] != ptempo)
                 or (b["cls"] == "MTimeSignatureEvent" and b["sig"] != psig))
        if other:
            x = x.replace('class="M', 'class="xM', 1)
        if b["track"] and "Marker" in b["cls"]:
            x = x.replace("<obj ", '<obj ds_track="%s" ' % html.escape(b["track"], quote=True), 1)
        key = re.sub(r' ID="\d+"', "", x)
        if key not in seen:
            seen.add(key)
            keep.append(x)

    payload = json.dumps(tracks, separators=(",", ":"), ensure_ascii=False)
    text = ('<?xml version="1.0" encoding="utf-8"?>\n<%s %s>\n' % (root, SLIM_MARK)
            + "\n".join(keep) + "\n<ds_tracks><![CDATA[" + payload.replace("]]>", "]]]]><![CDATA[>")
            + "]]></ds_tracks>\n</%s>\n" % root)
    return text, _stats_from(keep, tracks)


def _stats_from(blocks_xml, tracks) -> dict:
    return {
        "range_markers": sum(1 for x in blocks_xml if 'class="MRangeMarkerEvent"' in x),
        "tempo_events": sum(1 for x in blocks_xml if 'class="MTempoEvent"' in x),
        "tracks": len(tracks),
        "parts": sum(len(t["parts"]) for t in tracks),
        "notes": sum(len(p["n"]) for t in tracks for p in t["parts"]),
        "audio_regions": sum(1 for t in tracks for p in t["parts"] if p.get("a")),
        "muted_parts": sum(1 for t in tracks for p in t["parts"] if p["m"] & MUTED),
        "grouped": sum(1 for t in tracks if t.get("g")),
    }


def stats_of_slim(path: Path) -> dict:
    """Read a slim file back as XML and recount what's in it."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    if root.get("ds_slim") != "1":
        raise SlimError("written file is not marked slim")
    objs = [ET.tostring(o, encoding="unicode") for o in root.findall("obj")]
    node = root.find("ds_tracks")
    tracks = json.loads(node.text) if node is not None and node.text else []
    return _stats_from(objs, tracks)


def replace_with_slim(path: Path, *, is_open=None, groups=None) -> dict | None:
    """Slim `path` in place. Returns stats, or None if it was already slim.

    The original is deleted (atomically replaced by the slim file under the same
    name) only when all of these hold:
      - it is a complete Cubase XML (every element closed)
      - it was not open by another process before conversion and did not change
        while it was read
      - the slim file reads back as valid XML with the same markers, tracks,
        parts and notes as were found in the original
    Otherwise SlimError is raised and the original is left as it was.
    """
    if is_slim(path):
        return None
    if is_open is not None and is_open(path):
        raise SlimError("still open by another process")
    before = path.stat()
    text, stats = slim(path, groups)
    tmp = path.with_name("." + path.name + ".ds-slim.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            check = stats_of_slim(tmp)
        except Exception as e:
            raise SlimError(f"slim file did not read back: {e}")
        if check != stats:
            raise SlimError(f"slim file check failed: wrote {check}, expected {stats}")
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise SlimError("original changed while it was being converted")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    stats["original_bytes"] = before.st_size
    stats["slim_bytes"] = path.stat().st_size
    return stats
