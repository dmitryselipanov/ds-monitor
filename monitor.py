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
import http.server
import webbrowser
from pathlib import Path
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Config ────────────────────────────────────────────────────────────────
WATCH_DIR = Path.home() / "Documents" / "projects"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SYSTEM_PROMPT_PATH = Path(__file__).parent / "orchestration_knowledge.md"
PORT = 47291  # local server for floating window

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

    # Only return tracks that have notes
    result = [t for t in result if t['notes']]
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
            model="claude-sonnet-4-20250514",
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
    diagnose_cpr(cpr_path)

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

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "created")

    def on_moved(self, event):
        if not event.is_directory:
            print(f"[moved] {Path(event.src_path).name} → {Path(event.dest_path).name}")
            self._handle(event.dest_path, "moved")


# ── Local HTTP Server (floating window) ───────────────────────────────────

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DS//Monitor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, sans-serif;
  background: #111;
  color: #eee;
  font-size: 13px;
  user-select: none;
  min-width: 340px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #1a1a1a;
  border-bottom: 1px solid #333;
}
.title { font-family: monospace; font-size: 11px; color: #888; letter-spacing: 2px; }
.cue { font-weight: 700; font-size: 14px; color: #fff; }
.time { font-family: monospace; font-size: 10px; color: #555; }
.status-bar {
  padding: 6px 12px;
  font-family: monospace;
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
  border-bottom: 1px solid #222;
}
.status-idle { color: #555; }
.status-checking { color: #f90; }
.status-ok { color: #4c4; background: rgba(64,200,64,.08); }
.status-warning { color: #f90; background: rgba(255,159,10,.08); }
.status-error { color: #f44; background: rgba(255,64,64,.08); }
.summary {
  padding: 8px 12px;
  font-size: 12px;
  color: #aaa;
  border-bottom: 1px solid #222;
  line-height: 1.4;
}
.checks { padding: 4px 0; }
.check {
  padding: 7px 12px;
  border-bottom: 1px solid #1a1a1a;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.check-header { display: flex; align-items: center; gap: 8px; }
.track-name { font-weight: 600; font-size: 12px; color: #ddd; }
.bar-label { font-family: monospace; font-size: 10px; color: #555; }
.severity-ok { color: #4c4; }
.severity-warning { color: #f90; }
.severity-error { color: #f44; }
.check-issue { font-size: 12px; color: #bbb; line-height: 1.4; }
.check-suggestion { font-size: 11px; color: #666; font-style: italic; line-height: 1.4; }
.empty { padding: 20px 12px; color: #444; font-family: monospace; font-size: 11px; text-align: center; }
.watching { padding: 6px 12px; font-family: monospace; font-size: 9px; color: #333; }
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="title">DS//ORCH</div>
    <div class="cue" id="cue">—</div>
  </div>
  <div class="time" id="time">—</div>
</div>
<div class="status-bar" id="status-bar">IDLE</div>
<div class="summary" id="summary" style="display:none"></div>
<div class="checks" id="checks"></div>
<div class="watching">watching ~/Documents/projects</div>
<script>
function sevIcon(s) {
  if (s === 'ok') return '✓';
  if (s === 'warning') return '⚠';
  return '✗';
}
async function refresh() {
  try {
    const r = await fetch('/state');
    const d = await r.json();
    document.getElementById('cue').textContent = d.cue || '—';
    document.getElementById('time').textContent = d.last_saved || '—';
    const sb = document.getElementById('status-bar');
    sb.className = 'status-bar status-' + (d.status || 'idle');
    sb.textContent = d.status === 'checking' ? '⟳ CHECKING...' : (d.status || 'IDLE').toUpperCase();
    const sum = document.getElementById('summary');
    if (d.summary) { sum.textContent = d.summary; sum.style.display = ''; }
    else { sum.style.display = 'none'; }
    const checks = document.getElementById('checks');
    if (!d.checks || !d.checks.length) {
      checks.innerHTML = d.status === 'ok' ? '<div class="empty">All clear</div>' : '';
    } else {
      checks.innerHTML = d.checks.map(c => `
        <div class="check">
          <div class="check-header">
            <span class="severity-${c.severity}">${sevIcon(c.severity)}</span>
            <span class="track-name">${c.track}</span>
            ${c.bar ? `<span class="bar-label">bar ${c.bar}</span>` : ''}
          </div>
          <div class="check-issue">${c.issue}</div>
          ${c.suggestion ? `<div class="check-suggestion">→ ${c.suggestion}</div>` : ''}
        </div>`).join('');
    }
  } catch(e) {}
  setTimeout(refresh, 1500);
}
refresh();
</script>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # suppress logs

    def do_GET(self):
        if self.path == "/state":
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if not ANTHROPIC_API_KEY:
        print("⚠️  Set ANTHROPIC_API_KEY environment variable before running.")
        print("   export ANTHROPIC_API_KEY=sk-ant-...")

    if not WATCH_DIR.exists():
        print(f"⚠️  Watch directory not found: {WATCH_DIR}")
        print("   Create it or update WATCH_DIR in monitor.py")

    # Start HTTP server in background thread
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # Open floating window
    url = f"http://localhost:{PORT}"
    print(f"DS//Monitor running → {url}")
    webbrowser.open(url)

    # Start file watcher
    handler = CprHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=True)
    observer.start()
    print(f"Watching: {WATCH_DIR}")
    print("Save any .cpr file to trigger analysis. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        server.shutdown()

    observer.join()


if __name__ == "__main__":
    main()


