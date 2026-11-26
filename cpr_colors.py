#!/usr/bin/env python3
"""Print the colours of a Cubase project's track-list folders and tracks (read-only).

Usage:  python3 cpr_colors.py "/path/to/project.cpr"
        python3 cpr_colors.py --probe "/path/to/project.cpr" "PRIM Flute" "WW" ...
        (--probe: every labelled field stored with each named track or folder, to find where
        the colour lives; compare tracks of different colours)

One line per folder, indented like the track list:
  - **STRINGS**  folder #RRGGBBAA  · tracks: #RRGGBBAA ×812, default ×4
"default" = no colour set (Cubase's default). Colours are printed as the stored 32-bit value in
hex; which byte order Cubase uses (RGBA or ARGB) is checked by comparing with the track list.

How: folder and track headers are archive objects (class refs 0x800000e7 folders, 0x800000bf
tracks in Cubase 15 files; see cprfolders.py in the vault's template Tools). Folder objects span
their children. A header's colour is the 4 bytes 10 bytes after the next b"braF" (cprcolors.py).
"""
import re
import struct
import sys
from collections import Counter

FOLDER, TRACK = b"\x80\x00\x00\xe7", b"\x80\x00\x00\xbf"


def headers(b: bytes, ref: bytes) -> list[tuple[int, int, int, str]]:
    """(start, end of object, end of name, name) for every header object of class `ref`."""
    out = []
    for m in re.finditer(re.escape(ref) + rb"(....)\x00\x00\x00([\x02-\x80])", b, re.S):
        n = m.group(2)[0]
        s = m.end()
        name = b[s:s + n]
        if name.endswith(b"\x00\xef\xbb\xbf") and len(name) == n:
            out.append((m.start(), m.start() + 8 + struct.unpack(">I", m.group(1))[0], s + n,
                        name[:-4].decode("utf-8", "replace")))
    return out


def colour(b: bytes, after: int, limit: int) -> int | None:
    """The colour stored after a header's name, or None (default) if there's none before `limit`."""
    seg = b[after:min(limit, after + 400)]
    k = seg.find(b"braF")
    return int.from_bytes(seg[k + 10:k + 14], "big") if k >= 0 and len(seg) >= k + 14 else None


def hexc(c: int | None) -> str:
    return "default" if c is None else f"#{c:08X}"


def main(path: str):
    b = open(path, "rb").read()
    folders, tracks = headers(b, FOLDER), headers(b, TRACK)
    if not folders:
        print("No folders found: the class refs may differ in this file (see the docstring).")
        return
    starts = sorted([f[0] for f in folders] + [t[0] for t in tracks] + [len(b)])

    def next_start(pos):
        i = starts.index(pos)
        return starts[i + 1]

    def parent(pos):
        inside = [f for f in folders if f[0] < pos < f[1]]
        return max(inside, key=lambda f: f[0])[0] if inside else None

    fcol = {f[0]: colour(b, f[2], next_start(f[0])) for f in folders}
    tcols = {}                                   # folder start → Counter of its own tracks' colours
    loose = Counter()
    for t in tracks:
        c = colour(b, t[2], next_start(t[0]))
        p = parent(t[0])
        (tcols.setdefault(p, Counter()) if p is not None else loose)[hexc(c)] += 1
    kids = {}
    for f in folders:
        kids.setdefault(parent(f[0]), []).append(f)

    def walk(par, depth):
        for f in sorted(kids.get(par, [])):
            tc = tcols.get(f[0])
            tline = ("  · tracks: " + ", ".join(f"{k} ×{v}" for k, v in tc.most_common(4))
                     + (" …" if len(tc) > 4 else "")) if tc else ""
            print("  " * depth + f"- **{f[3]}**  folder {hexc(fcol[f[0]])}{tline}")
            walk(f[0], depth + 1)

    print(f"{path}\n{len(folders)} folders, {len(tracks)} tracks\n")
    walk(None, 0)
    if loose:
        print("\nTracks outside folders: " + ", ".join(f"{k} ×{v}" for k, v in loose.most_common(6)))


def probe(path: str, names: list[str]):
    """For each header with one of `names`: every 4+ letter ASCII label in the 3 KB after its name,
    with the 16 bytes after it, so fields can be compared. "--- next: X" marks where the next
    track or folder header starts (fields past it may belong to that one)."""
    b = open(path, "rb").read()
    hs = sorted(headers(b, FOLDER) + headers(b, TRACK))
    starts = [h[0] for h in hs] + [len(b)]
    for i, h in enumerate(hs):
        if h[3] not in names:
            continue
        kind = "folder" if b[h[0]:h[0] + 4] == FOLDER else "track"
        end = h[2] + 3000
        nxt = [x for x in hs[i + 1:] if x[0] < end]
        print(f"\n== {kind} {h[3]!r} at {h[0]} (object ends +{h[1] - h[2]})")
        for m in re.finditer(rb"[A-Za-z][A-Za-z0-9 _]{3,40}", b[h[2]:end]):
            at = h[2] + m.end()
            while nxt and nxt[0][0] < h[2] + m.start():
                print(f"  --- next: {nxt.pop(0)[3]!r}")
            print(f"  +{m.start():5d} {m.group().decode()!r:34s} {b[at:at + 16].hex(' ')}")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--probe":
        probe(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) == 2:
        main(sys.argv[1])
    else:
        sys.exit(__doc__)
