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
    Parse CPR binary, extract only tracks that have MIDI content.
    Returns list of {name, track_type, notes: [{pitch, position, length, velocity}], tempo, time_sig}
    """
    try:
        from cubasetools.core.cpr_parser import parse_cpr
        project = parse_cpr(cpr_path)
    except Exception as e:
        raise RuntimeError(f"CPR parse failed: {e}")

    active_tracks = []
    for track in project.tracks:
        if not track.midi_parts:
            continue
        notes = []
        for part in track.midi_parts:
            for note in part.notes:
                notes.append({
                    "pitch": note.pitch,
                    "position": note.position,
                    "length": note.length,
                    "velocity": note.velocity,
                })
        if notes:
            active_tracks.append({
                "name": track.name,
                "track_type": track.track_type.value,
                "notes": sorted(notes, key=lambda n: n["position"]),
                "tempo": project.tempo,
                "time_sig": project.time_signature,
            })

    return active_tracks


# ── music21 Passage Description ───────────────────────────────────────────

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

    try:
        tracks = extract_midi_tracks(cpr_path)
        if not tracks:
            state["status"] = "ok"
            state["summary"] = f"No active MIDI tracks in {cue_name}."
            return

        passage = describe_passage(tracks)
        result = analyse_with_claude(passage, cue_name)

        state["status"] = result.get("status", "ok")
        state["checks"] = result.get("checks", [])
        state["summary"] = result.get("summary", "")

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        state["summary"] = f"Analysis failed: {e}"


# ── File Watcher ──────────────────────────────────────────────────────────

class CprHandler(FileSystemEventHandler):
    def __init__(self):
        self._debounce_timers = {}

    def on_modified(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".cpr":
            return
        # Ignore Cubase backup copies (filename ends with -01, -02 etc before extension)
        if re.search(r'-\d{2}$', path.stem):
            return
        # Debounce: wait 2s after last modification before analysing
        if path in self._debounce_timers:
            self._debounce_timers[path].cancel()
        timer = threading.Timer(2.0, lambda: run_analysis(path))
        self._debounce_timers[path] = timer
        timer.start()


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
