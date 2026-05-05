# DS//Monitor

Orchestration assistant for Cubase. Watches `~/Documents/projects` for CPR saves, extracts active MIDI tracks, and runs them through Claude for orchestration feedback.

## Setup

```bash
cd ds-monitor
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python monitor.py
```

## How it works

1. Save any `.cpr` file in `~/Documents/projects`
2. DS//Monitor detects the save (2 second debounce)
3. Parses the CPR — extracts only tracks with MIDI content
4. Converts MIDI to readable passage description via music21
5. Sends to Claude with orchestration knowledge system prompt
6. Results appear in floating browser window

## Files

- `monitor.py` — main watcher + server + analysis pipeline
- `orchestration_knowledge.md` — system prompt (edit to refine AI behaviour)
- `cubasetools/` — CPR binary parser (from github.com/schwifty00/CubaseTools)
- `requirements.txt` — Python dependencies

## Configuration

Edit `monitor.py`:
- `WATCH_DIR` — change if your projects folder is elsewhere
- `PORT` — change if 47291 is in use

## Floating window

Opens automatically in browser at `http://localhost:47291`. For always-on-top behaviour on Mac, use Safari → Window → Float on Top, or use a dedicated app like [Flotato](https://flotato.com).
