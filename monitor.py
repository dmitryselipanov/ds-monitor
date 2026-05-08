#!/usr/bin/env python3
"""
DS//Monitor — Orchestration Assistant
Watches ~/Documents/projects for CPR saves, analyses active tracks, 
returns orchestration feedback via Claude API.
"""

import os
import sys
import json
import time
import struct
import re
import threading
from pathlib import Path
from datetime import datetime

try:
    import rumps
    HAS_RUMPS = True
except ImportError:
    HAS_RUMPS = False
    import http.server
    import webbrowser

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────────
WATCH_DIR = Path.home() / "Documents" / "projects"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SYSTEM_PROMPT_PATH = Path(__file__).parent / "orchestration_knowledge.md"
PORT = 47291
AI_ENABLED = False  # toggle via menu bar

SB_URL = "https://ekfipctoizteywmqspcw.supabase.co"
SB_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVrZmlwY3RvaXp0ZXl3bXFzcGN3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI4MjM1ODcsImV4cCI6MjA4ODM5OTU4N30.hFlhgzVvwnMrGep9gLroaT-iyiFK5raLQyuNW1rnXjA"

# ── XML Parser ────────────────────────────────────────────────────────────

def parse_cubase_xml(xml_path: Path) -> list[dict]:
    """Parse Cubase marker XML, return list of cue dicts."""
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as e:
        print(f"  [xml error] {e}")
        return []

    # Find BPM and PPQ
    bpm = 120.0
    ppq = 480
    for node in root.iter():
        if node.get('name') == 'Tempo':
            try: bpm = float(node.get('value', 120))
            except: pass
        if node.get('name') == 'Ticks':
            try: ppq = int(node.get('value', 480))
            except: pass

    TC_SEPS = re.compile(r"['\u2019.:]")
    DOT_TC = re.compile(r'(\d{2})[.\':]\d{2}[.\':]\d{2}[.\':]\d{2}$')

    auto_num = [0]

    def parse_marker(name, length_ticks):
        name = name.strip()
        tc_match = re.search(r"(\d{2}['\u2019.:]\d{2}['\u2019.:]\d{2}['\u2019.:]\d{2})$", name)
        if tc_match:
            tc_str = tc_match.group(1)
            tc = re.sub(r"['\u2019.:]", ':', tc_str)
            rest = name[:name.rfind(tc_match.group(1))].strip()
            parts = rest.split()
            cue_number = parts[0] if parts else ''
            title = ' '.join(parts[1:]) if len(parts) > 1 else rest
            # Remove version suffix
            title = re.sub(r'\s+V\d+(\.\d+)*\s*$', '', title, flags=re.IGNORECASE).strip()
            in_tc = tc
            dur_secs = (length_ticks / ppq) * (60 / bpm) if length_ticks else 0
            h,rem = divmod(int(dur_secs),3600)
            m,s = divmod(rem,60)
            duration = f"{h:02d}:{m:02d}:{s:02d}"
            return dict(cue_number=cue_number, title=title or cue_number, in_tc=in_tc, out_tc=None, duration=duration, has_tc=True)
        else:
            auto_num[0] += 1
            dur_secs = (length_ticks / ppq) * (60 / bpm) if length_ticks else 0
            h,rem = divmod(int(dur_secs),3600)
            m,s = divmod(rem,60)
            duration = f"{h:02d}:{m:02d}:{s:02d}"
            return dict(cue_number=str(auto_num[0]).zfill(2), title=name, in_tc=None, out_tc=None, duration=duration, has_tc=False)

    cues = []
    for marker in root.iter('Marker'):
        name_node = marker.find(".//Name") or marker.find(".//string[@name='Name']")
        length_node = marker.find(".//Length") or marker.find(".//float[@name='Length']")
        name = (name_node.get('value','') if name_node is not None else
                marker.get('name',''))
        length_ticks = 0
        try:
            if length_node is not None:
                length_ticks = float(length_node.get('value', 0))
        except: pass
        if name:
            cue = parse_marker(name, length_ticks)
            if cue:
                cues.append(cue)

    return cues


def push_pending_imports(xml_path: Path, cues: list[dict]):
    """Push parsed cues to Supabase pending_imports table."""
    if not cues:
        return
    try:
        import urllib.request
        # Find matching project by walking up from XML to PROJECTS root
        # Structure: PROJECTS/[ProjectFolder]/...subdirs.../file.xml
        project_id = None
        parts = xml_path.parts
        watch_parts = WATCH_DIR.parts
        # Find the folder immediately under WATCH_DIR
        if len(parts) > len(watch_parts):
            project_folder = parts[len(watch_parts)].lower()
            for p in projects:
                title = (p.get('title') or '').lower().strip()
                if title and (title == project_folder or project_folder.startswith(title) or title.startswith(project_folder)):
                    project_id = p['id']
                    break
            # Fallback: check if any part of the path contains the title
            if not project_id:
                for p in projects:
                    title = (p.get('title') or '').lower().strip()
                    if title and any(title in part.lower() for part in parts):
                        project_id = p['id']
                        break

        rows = []
        for c in cues:
            rows.append({
                "project_id": project_id,
                "raw_xml_path": str(xml_path),
                "cue_number": c['cue_number'],
                "title": c['title'],
                "in_tc": c.get('in_tc'),
                "out_tc": c.get('out_tc'),
                "duration": c.get('duration'),
                "has_tc": c.get('has_tc', True),
                "status": "pending"
            })

        data = json.dumps(rows).encode()
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pending_imports",
            data=data,
            method="POST",
            headers={
                "apikey": SB_KEY,
                "Authorization": f"Bearer {SB_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
        )
        with urllib.request.urlopen(req) as r:
            print(f"  [xml] pushed {len(rows)} cues to pending_imports (project_id={project_id})")
    except Exception as e:
        print(f"  [xml error] push failed: {e}")


def handle_xml(xml_path: Path):
    """Push raw XML content to pending_imports for DS Scoring to parse."""
    folder = xml_path.parent
    cprs = list(folder.glob("*.cpr"))
    if not cprs:
        print(f"  [xml skip] no CPR in same folder: {xml_path.name}")
        return
    print(f"  [xml detected] {xml_path.name}")
    try:
        raw_xml = xml_path.read_text(encoding='utf-8', errors='ignore')
    except Exception as e:
        print(f"  [xml error] could not read file: {e}")
        return

    try:
        import urllib.request
        # Find matching project
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/projects?select=id,title",
            headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
        )
        with urllib.request.urlopen(req) as r:
            projects = json.loads(r.read())

        project_id = None
        parts = xml_path.parts
        watch_parts = WATCH_DIR.parts
        if len(parts) > len(watch_parts):
            project_folder = parts[len(watch_parts)].lower()
            for p in projects:
                title = (p.get('title') or '').lower().strip()
                if title and (title == project_folder or project_folder.startswith(title) or title.startswith(project_folder)):
                    project_id = p['id']
                    break
            if not project_id:
                for p in projects:
                    title = (p.get('title') or '').lower().strip()
                    if title and any(title in part.lower() for part in parts):
                        project_id = p['id']
                        break

        row = {
            "project_id": project_id,
            "raw_xml_path": str(xml_path),
            "raw_xml": raw_xml,
            "title": xml_path.stem,
            "status": "pending"
        }
        data = json.dumps(row).encode()
        req = urllib.request.Request(
            f"{SB_URL}/rest/v1/pending_imports",
            data=data,
            method="POST",
            headers={
                "apikey": SB_KEY,
                "Authorization": f"Bearer {SB_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            }
        )
        with urllib.request.urlopen(req) as r:
            print(f"  [xml] pushed raw XML to pending_imports (project_id={project_id})")
    except Exception as e:
        print(f"  [xml error] push failed: {e}")

def diagnose_cpr(cpr_path: Path):
    """Dump bytes around first note record to find track name pattern."""
    data = cpr_path.read_bytes()
    adcn = data.find(b'adcn\x00\x01')
    if adcn < 0:
        print("No adcn\\x00\\x01 found")
        return
    
    print(f"\n=== First note record at byte {adcn} ===")
    
    # Show 200 bytes before the note
    before = data[max(0,adcn-200):adcn]
    print(f"\nBytes before note (hex):")
    for i in range(0, len(before), 16):
        chunk = before[i:i+16]
        hex_str = ' '.join(f'{b:02x}' for b in chunk)
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f"  {hex_str:<48}  {ascii_str}")
    
    # Find any readable strings near the note
    print(f"\nReadable strings in 2KB before note:")
    chunk = data[max(0,adcn-2000):adcn]
    for m in re.finditer(rb'[\x20-\x7e]{4,}', chunk):
        s = m.group(0).decode('ascii', errors='ignore')
        print(f"  pos -{adcn - (max(0,adcn-2000) + m.start())}: '{s}'")


# ── State ─────────────────────────────────────────────────────────────────
state = {
    "status": "idle",
    "cue": "",
    "last_saved": "",
    "checks": [],
    "summary": "",
    "error": "",
}

# ── CPR MIDI Extraction ───────────────────────────────────────────────────

def extract_midi_tracks(cpr_path: Path) -> list[dict]:
    """
    CPR parser using exact track name prefix pattern discovered via binary analysis.
    Pattern: \x00\x00\x00\x00\x00\x00\x00\x00\x80\x00\x00\xbf + 8 bytes + name + \x00\xef\xbb\xbf
    Notes live within 500KB of their track name position.
    """
    data = cpr_path.read_bytes()
    print(f"  [read] {len(data)//1024//1024}MB loaded")

    # Extract tempo
    tempo = 120.0
    bpm_match = re.search(rb'BPM\x00\x00\x04(.{8})', data)
    if bpm_match:
        try:
            t = struct.unpack('>d', bpm_match.group(1))[0]
            if 20 < t < 300:
                tempo = t
        except:
            pass

    # Extract time signature
    time_sig = "4/4"
    ts_match = re.search(rb'Numerator\x00\x00\x01(.{8}).*?Denominator\x00\x00\x01(.{8})', data, re.DOTALL)
    if ts_match:
        try:
            num = struct.unpack('>q', ts_match.group(1))[0]
            den = struct.unpack('>q', ts_match.group(2))[0]
            if 1 <= num <= 16 and den in [2,4,8,16]:
                time_sig = f"{num}/{den}"
        except:
            pass

    # Find track names using exact prefix pattern
    PREFIX = b'\x00\x00\x00\x00\x00\x00\x00\x00\x80\x00\x00\xbf'
    BOM = b'\x00\xef\xbb\xbf'
    pattern = re.escape(PREFIX) + rb'.{8}([\x21-\x7e][\x20-\x7e]{1,58}?)' + re.escape(BOM)
    
    name_positions = []
    for m in re.finditer(pattern, data, re.DOTALL):
        name = m.group(1).decode('ascii', errors='ignore').strip()
        # Must look like an instrument name
        if (name and len(name) >= 2 and 
            re.match(r'^[A-Za-z]', name) and
            not re.match(r'^[A-Z][a-z]+[A-Z]', name)):  # skip camelCase internal names
            name_positions.append((m.start(), name))

    print(f"  [names] found {len(name_positions)} track names")

    if not name_positions:
        return []

    # Find all note records
    adcn_matches = list(re.finditer(rb'adcn\x00\x01(.{8})', data, re.DOTALL))
    print(f"  [notes] found {len(adcn_matches)} total note records")
    if not adcn_matches:
        return []

    cap7_set = {m.group(1)[7] for m in adcn_matches}
    use_block24 = cap7_set == {0}

    # Build note position list for fast lookup
    import bisect
    note_positions = [m.start() for m in adcn_matches]

    # Voronoi assignment: each note goes to its nearest track name
    # Build sorted list of name positions for bisect
    sorted_name_positions = sorted(name_positions, key=lambda x: x[0])
    spos = [p for p,n in sorted_name_positions]
    snames = [n for p,n in sorted_name_positions]

    tracks_dict = {}

    for i, nm in enumerate(adcn_matches):
        captured = nm.group(1)
        block = data[nm.end():nm.end()+37]
        if len(block) < 37:
            continue
        try:
            pitch = block[24] if use_block24 else captured[7]
            note_length = struct.unpack_from(">d", block, 0)[0]
            note_pos_val = struct.unpack_from(">d", block, 26)[0]
            on_vel = block[35]
            if not (0 < pitch <= 127 and 0 < on_vel <= 127):
                continue
            if not (0 < note_length < 1000000):
                continue

            # Find nearest name (Voronoi)
            idx = bisect.bisect_left(spos, nm.start())
            best_name = None
            best_dist = float('inf')
            for j in [idx-1, idx]:
                if 0 <= j < len(spos):
                    dist = abs(nm.start() - spos[j])
                    if dist < best_dist:
                        best_dist = dist
                        best_name = snames[j]

            if not best_name:
                continue

            if best_name not in tracks_dict:
                tracks_dict[best_name] = {
                    'name': best_name,
                    'track_type': 'Instrument',
                    'notes': [],
                    'tempo': tempo,
                    'time_sig': time_sig,
                }
            tracks_dict[best_name]['notes'].append({
                'pitch': pitch,
                'position': round(note_pos_val, 2),
                'length': round(note_length, 2),
                'velocity': on_vel,
            })
        except (struct.error, IndexError):
            continue

    result = []
    for t in tracks_dict.values():
        # Deduplicate notes by pitch+position
        seen = set()
        deduped = []
        for n in sorted(t['notes'], key=lambda n: n['position']):
            key = (n['pitch'], round(n['position'], 1))
            if key not in seen:
                seen.add(key)
                deduped.append(n)
        t['notes'] = deduped
        result.append(t)

    # Only return tracks with musically meaningful content:
    # at least 2 notes with more than 1 unique pitch (filters controller data)
    result = [t for t in result if t['notes'] and len(set(n['pitch'] for n in t['notes'])) > 1]
    print(f"  [tracks] {[(t['name'], len(t['notes'])) for t in result[:10]]}")
    return result


def describe_passage(tracks: list[dict]) -> str:
    """Convert extracted MIDI tracks into a readable passage description for Claude."""
    if not tracks:
        return "No active MIDI tracks found."

    tempo = tracks[0].get("tempo", 120) if tracks else 120
    time_sig = tracks[0].get("time_sig", "4/4") if tracks else "4/4"
    ppq = 480

    beats_per_bar = int(time_sig.split("/")[0]) if "/" in time_sig else 4
    ticks_per_bar = ppq * beats_per_bar

    lines = [f"Tempo: ♩={tempo}, Time signature: {time_sig}\n"]

    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def midi_to_name(pitch):
        octave = (pitch // 12) - 1
        name = note_names[pitch % 12]
        return f"{name}{octave}"

    def ticks_to_bar_beat(ticks):
        bar = int(ticks // ticks_per_bar) + 1
        beat = ((ticks % ticks_per_bar) / ppq) + 1
        return bar, round(beat, 2)

    # Get double stop analysis
    double_stops = detect_double_stops(tracks)

    for track in tracks:
        lines.append(f"## {track['name']} ({track['track_type']})")
        notes = track["notes"]
        if not notes:
            continue

        # Group notes by bar
        bars = {}
        for note in notes:
            bar, beat = ticks_to_bar_beat(note["position"])
            if bar not in bars:
                bars[bar] = []
            duration_beats = note["length"] / ppq
            bars[bar].append(f"{midi_to_name(note['pitch'])}(beat {beat}, dur {duration_beats:.1f}b, vel {note['velocity']})")

        for bar_num in sorted(bars.keys()):
            lines.append(f"  Bar {bar_num}: {', '.join(bars[bar_num])}")

        # Summary stats
        pitches = [n["pitch"] for n in notes]
        lines.append(f"  Range: {midi_to_name(min(pitches))} – {midi_to_name(max(pitches))}, {len(notes)} notes total")

        # Double stops for this track
        if track["name"] in double_stops:
            lines.append(f"  Double stops detected:")
            for ds in double_stops[track["name"]]:
                lines.append(f"    Bar {ds['bar']}: {ds['lower_name']}+{ds['upper_name']} ({ds['interval_name']}, {ds['semitones']} semitones)")
        lines.append("")

    return "\n".join(lines)


# ── Double Stop Detection ─────────────────────────────────────────────────

def detect_double_stops(tracks: list[dict]) -> dict[str, list[dict]]:
    """
    Find simultaneous notes per track and calculate intervals.
    Returns {track_name: [{bar, notes, interval_semitones, interval_name}]}
    """
    try:
        from music21 import interval, pitch
    except ImportError:
        return {}

    ppq = 480
    result = {}

    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    def midi_to_pitch(midi_num):
        octave = (midi_num // 12) - 1
        name = note_names[midi_num % 12]
        return pitch.Pitch(f"{name}{octave}")

    for track in tracks:
        notes = track["notes"]
        if not notes:
            continue

        tempo = track.get("tempo", 120)
        time_sig = track.get("time_sig", "4/4")
        beats_per_bar = int(time_sig.split("/")[0]) if "/" in time_sig else 4
        ticks_per_bar = ppq * beats_per_bar

        double_stops = []

        # Find overlapping notes
        for i, n1 in enumerate(notes):
            n1_end = n1["position"] + n1["length"]
            for n2 in notes[i+1:]:
                if n2["position"] >= n1_end:
                    break
                if n2["position"] >= n1["position"]:
                    # Simultaneous — calculate interval
                    try:
                        p1 = midi_to_pitch(min(n1["pitch"], n2["pitch"]))
                        p2 = midi_to_pitch(max(n1["pitch"], n2["pitch"]))
                        iv = interval.Interval(noteStart=p1, noteEnd=p2)
                        semitones = abs(n1["pitch"] - n2["pitch"])
                        bar = int(n1["position"] // ticks_per_bar) + 1
                        double_stops.append({
                            "bar": bar,
                            "lower": n1["pitch"],
                            "upper": n2["pitch"],
                            "semitones": semitones,
                            "interval_name": iv.niceName,
                            "lower_name": note_names[min(n1["pitch"], n2["pitch"]) % 12],
                            "upper_name": note_names[max(n1["pitch"], n2["pitch"]) % 12],
                        })
                    except Exception:
                        continue

        if double_stops:
            result[track["name"]] = double_stops

    return result


def analyse_with_claude(passage_text: str, cue_name: str) -> dict:
    """Send passage to Claude API, return parsed JSON response."""
    if not AI_ENABLED:
        return {"status": "ok", "checks": [], "summary": "AI analysis disabled."}
    if not ANTHROPIC_API_KEY:
        # Test mode — mock response showing the pipeline works
        track_lines = [l.strip() for l in passage_text.split('\n') if l.startswith('##')]
        track_names = [l.replace('## ', '').split(' (')[0] for l in track_lines]
        
        # Count double stops mentioned
        ds_lines = [l for l in passage_text.split('\n') if 'Double stops' in l or 'Bar' in l and '+' in l]
        
        checks = []
        for name in track_names[:3]:  # show first 3 tracks
            checks.append({
                "track": name,
                "severity": "ok",
                "bar": None,
                "issue": "TEST MODE — pipeline working, no API key set",
                "suggestion": "Set ANTHROPIC_API_KEY to enable real analysis"
            })
        
        return {
            "status": "warning",
            "checks": checks,
            "summary": f"TEST MODE: Found {len(track_names)} active track(s). Set ANTHROPIC_API_KEY for real analysis."
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system_prompt = SYSTEM_PROMPT_PATH.read_text()

        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": f"Analyse this passage from cue '{cue_name}':\n\n{passage_text}"
            }]
        )

        text = message.content[0].text.strip()
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        return json.loads(text)

    except json.JSONDecodeError:
        return {"status": "warning", "checks": [], "summary": text}
    except Exception as e:
        return {"status": "error", "checks": [], "summary": str(e)}


# ── Analysis Pipeline ─────────────────────────────────────────────────────

def run_analysis(cpr_path: Path):
    """Full pipeline: parse CPR → describe → analyse → update state."""
    global state
    cue_name = cpr_path.stem
    state["status"] = "checking"
    state["cue"] = cue_name
    state["last_saved"] = datetime.now().strftime("%H:%M:%S")
    state["checks"] = []
    state["summary"] = ""
    state["error"] = ""
    print(f"[parsing] {cpr_path.name} ({cpr_path.stat().st_size // 1024 // 1024}MB)...")

    try:
        tracks = extract_midi_tracks(cpr_path)
        print(f"[parsed] found {len(tracks)} active MIDI track(s)")
        if not tracks:
            state["status"] = "ok"
            state["summary"] = f"No active MIDI tracks in {cue_name}."
            return

        passage = describe_passage(tracks)
        result = analyse_with_claude(passage, cue_name)

        state["status"] = result.get("status", "ok")
        state["checks"] = result.get("checks", [])
        state["summary"] = result.get("summary", "")
        print(f"[done] status={state['status']}")

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        state["summary"] = f"Analysis failed: {e}"
        print(f"[error] {e}")


# ── File Watcher ──────────────────────────────────────────────────────────

class CprHandler(FileSystemEventHandler):
    def __init__(self):
        self._debounce_timers = {}

    def _handle(self, path_str, event_type="?"):
        path = Path(path_str)
        if path.suffix.lower() != ".cpr":
            return
        if re.search(r'-\d{2}$', path.stem):
            print(f"[skip] backup ({event_type}): {path.name}")
            return
        print(f"[detected] ({event_type}): {path.name}")
        if path in self._debounce_timers:
            self._debounce_timers[path].cancel()
        timer = threading.Timer(2.0, lambda p=path: threading.Thread(target=run_analysis, args=(p,), daemon=True).start())
        self._debounce_timers[path] = timer
        timer.start()

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "modified")
            self._handle_xml(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "created")
            self._handle_xml(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            print(f"[moved] {Path(event.src_path).name} → {Path(event.dest_path).name}")
            self._handle(event.dest_path, "moved")
            self._handle_xml(event.dest_path)

    def _handle_xml(self, path_str):
        path = Path(path_str)
        if path.suffix.lower() != ".xml":
            return
        if path in self._debounce_timers:
            self._debounce_timers[path].cancel()
        timer = threading.Timer(2.0, lambda p=path: threading.Thread(target=handle_xml, args=(p,), daemon=True).start())
        self._debounce_timers[path] = timer
        timer.start()


# ── Local HTTP Server (floating window) ───────────────────────────────────


# ── Menu Bar App ──────────────────────────────────────────────────────────

STATUS_ICONS = {
    "idle":     "♩",
    "checking": "⟳",
    "ok":       "✓",
    "warning":  "⚠",
    "error":    "✗",
}

STATUS_COLORS = {
    "idle":     "DS//ORCH",
    "checking": "DS//ORCH — Checking…",
    "ok":       "DS//ORCH — OK",
    "warning":  "DS//ORCH — ⚠ Issues",
    "error":    "DS//ORCH — Error",
}

class DSMonitorApp(rumps.App):
    def __init__(self):
        super().__init__("♩", quit_button=None)
        self.ai_item = rumps.MenuItem("○ AI Analysis: OFF", callback=self.toggle_ai)
        self.menu = ["Idle — waiting for save", None, self.ai_item, None, rumps.MenuItem("Quit DS//Monitor", callback=rumps.quit_application)]
        self._last_status = "idle"

    def toggle_ai(self, _):
        global AI_ENABLED
        AI_ENABLED = not AI_ENABLED
        self.ai_item.title = f"{'⚡' if AI_ENABLED else '○'} AI Analysis: {'ON' if AI_ENABLED else 'OFF'}"
        print(f"[ai] {'enabled' if AI_ENABLED else 'disabled'}")

    def update_menu(self):
        s = state["status"]
        cue = state.get("cue", "")
        summary = state.get("summary", "")
        checks = state.get("checks", [])

        # Update icon
        self.title = STATUS_ICONS.get(s, "♩")

        # Rebuild menu
        items = []
        
        # Header
        header = cue if cue else "DS//ORCH"
        if summary:
            header += f" — {summary[:60]}"
        items.append(rumps.MenuItem(header, callback=None))
        items.append(None)

        # Checks
        if checks:
            for c in checks[:8]:
                sev = {"ok": "✓", "warning": "⚠", "error": "✗"}.get(c.get("severity",""), "•")
                track = c.get("track", "")
                issue = c.get("issue", "")
                bar = f" bar {c['bar']}" if c.get("bar") else ""
                label = f"{sev} {track}{bar}: {issue}"[:80]
                items.append(rumps.MenuItem(label, callback=None))
        elif s == "ok":
            items.append(rumps.MenuItem("✓ All clear", callback=None))
        elif s == "idle":
            items.append(rumps.MenuItem("Watching ~/Documents/projects", callback=None))
        elif s == "checking":
            items.append(rumps.MenuItem(f"Parsing {cue}…", callback=None))

        items.append(None)
        items.append(self.ai_item)
        items.append(None)
        items.append(rumps.MenuItem("Quit DS//Monitor", callback=rumps.quit_application))

        self.menu.clear()
        for item in items:
            if item is None:
                self.menu.add(rumps.separator)
            else:
                self.menu.add(item)

    @rumps.timer(2)
    def refresh(self, _):
        if state["status"] != self._last_status or state.get("checks") or state.get("summary"):
            self._last_status = state["status"]
            self.update_menu()


def run_menubar():
    app = DSMonitorApp()
    # Start watcher in background
    handler = CprHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=True)
    observer.start()
    print(f"DS//Monitor started — watching {WATCH_DIR}")
    if not ANTHROPIC_API_KEY:
        print("⚠ No ANTHROPIC_API_KEY — running in test mode")
    app.run()
    observer.stop()
    observer.join()



if __name__ == "__main__":
    if HAS_RUMPS:
        run_menubar()
    else:
        print("Install rumps for menu bar: pip3 install rumps")
        print("Falling back to browser mode...")
        # fallback
