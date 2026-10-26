# DS//Monitor

## Video previews to Dropbox

In Cubase's **Export Video** dialog, set **File Path** to a folder named
`Video Exports` directly inside the film's local project folder, for example
`Documents/PROJECTS/33 Days/Video Exports`. Keep the cue number, title, and
optional version in the MP4 filename (`1m09 Behind The Glass V1.1.mp4`).

DS Monitor waits until the MP4 stops changing, Cubase closes it, and its audio
stream can be read. It then moves the completed file to the matching cue folder
in local Dropbox. Dropbox performs the upload. If the cue folder does not exist,
Monitor creates it; projects with `EP01`-style episode folders keep that layout.
The temporary render and its WAV stay outside Dropbox, and the completed MP4 is
removed from `Video Exports` by the move. On this Mac, the project and Dropbox
folders share a volume, so the move is atomic.

Each new completed export replaces a video with the same filename and removes
previous MP4/MOV/M4V versions with the same cue number in that cue folder.
The new video is moved successfully before older versions are removed. Audio
files and unrelated files are retained. Version numbers do not determine which
video wins: the most recently published export becomes the current preview.

The MP4 stays local in the Dropbox folder while Dropbox uploads it. Once Dropbox
shows it as synced, use Finder's **Make online-only** on that MP4 if you want to
free the local space. DS Monitor does not evict files from Dropbox automatically.

If the project, filename, or destination is ambiguous, Monitor leaves the MP4 in
`Video Exports` and records the reason in `/tmp/ds-monitor.log`. The same is true
when publishing fails. A matching WAV in the cue's Cubase
`Mixdown` folder is skipped while the video render is active; ordinary audio
bounces continue through the existing upload path.

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
