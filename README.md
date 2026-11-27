# DS//Monitor

## Tracks with Data (importing tracks from another cue)

Cubase's **Import Tracks from Project** dialog lists all ~1,500 template tracks and
doesn't show which ones hold anything. Menu bar → **Tracks with Data…** → pick the
cue you're importing *from* (its `.cpr`, or its `.xml` export). A panel floats above
Cubase with only that cue's tracks that play (unmuted MIDI or audio), under section
headings (Woodwinds, Strings, …) in track-list order, with how many notes or audio
regions each has. Tracks whose parts are all muted are listed separately at the end.
**Copy** puts the list on the clipboard. Nothing is loaded in Cubase.

It also opens by itself: when Cubase's Import Tracks from Project shows its track list
(the "Import Options" window), DS Monitor reads which `.cpr` Cubase has open (the cue
picked) and shows its panel, without taking the focus; it closes with the dialog. Turn
this off with the menu's **Tracks with Data on Import Tracks**. It needs DS Monitor's
Python allowed in System Settings › Privacy & Security › Accessibility (to see Cubase's
window titles); it only looks while Cubase is in front.

The list comes from the cue's XML export (select all tracks → File › Export ›
Selected Tracks, next to the `.cpr` with its name); full, slim and older exports are
all read, without changing them. Cubase never exports disabled tracks, so everything
listed was enabled at that export. If the project was saved after the export, the
window says so in orange: export again for an up-to-date list. Command line:
`python3 tracks_with_data.py "/path/to/cue.cpr"`.

## First save → Writing

Saving a cue's project in Cubase (`1m09 Behind The Glass.cpr`, or any `.cpr` inside a
`1m09` folder) moves that cue from **Spotting** to **Writing** in DS Scoring, with a Mac
notification. Only cues still at Spotting (or with no stage) move. A cue you've set to
anything else is never changed, and the stage never goes backwards. In a series, keep cues under
`EP01`-style folders: cue numbers repeat across episodes, and a number shared by several cues
with no episode folder is left alone (the log says so). Cubase backups (`-01.cpr`) and Auto Saves don't
count. It goes through the Worker's `/monitor-cue-saved` with `DS_MONITOR_SECRET`, the same
secret the mixdown publisher uses, so nothing new needs setting up.

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
`Video Exports` and records the reason in `~/Library/Logs/ds-monitor.log`. The same is true
when publishing fails. A matching WAV in the cue's Cubase
`Mixdown` folder is skipped while the video render is active; ordinary audio
bounces are published as below.

## Mixdown publishing

Only review-ready mixes reach Dropbox. A WAV exported to a Cubase `Mixdown`
folder is copied into the project's local Dropbox cue folder (and synced by the
Dropbox app) only when its name, minus version and timecode, is exactly
`{cue} {title}` of a cue in DS Scoring:

- `1m02 City Pulse V1.1 01.01.57.22.wav` → `Scoring/1m02 City Pulse/`
- first export of a cue with no folder yet → the folder is created
  (`Scoring/EPxx/` in projects laid out by episode)
- cue folder still named after an old title → goes into that folder; the log
  suggests renaming it
- `1m02 City Pulse - NO SAX.wav` → stays local (production version)

Cue titles come from the Worker's `/monitor-cues`, which needs
`DS_MONITOR_SECRET` in the LaunchAgent plist (the Worker's `MONITOR_SECRET`).
Without it, or when the Worker can't be reached, the name must instead match an
existing cue folder under `Scoring/` exactly, and nothing is created.

A skipped WAV is rechecked every minute for 14 days (list kept in
`~/Library/Caches/ds-monitor/mixdown-waiting.json`), so it publishes as soon as
its cue is added to DS Scoring, without exporting again. WAVs already in the
cue folder move to `Prev Versions/` first. There is no size limit. Monitor
waits until the export is really finished — unchanged, header sizes written,
and no longer open in Cubase — and copies again if the file changes later. The
log says why any WAV was not published. The original stays in `Mixdown`.

### BWF timestamp check

Before a review mix is published, `bwf_check.py` compares its Broadcast Wave
stamp (bext TimeReference) with the timecode at the end of the filename, at the
project's frame rate (exact 24000/1001 maths for 23.976). Export from bar 1 with
**Insert Broadcast Wave Chunk** on and **Use this Timecode** off; at 48 kHz /
23.976 one frame is 2,002 samples, so `03.14.54.19` must read 561,911,350.
Results go to the log as `[bwf] PASS/WARN/FAIL` with the reason: no BWF chunk,
stamped 00:00:00:00, not on a frame boundary (+N samples), or off by N samples /
seconds / frames. A name without a timecode is a WARN. Production versions
aren't checked.

It also flags a mix that is effectively silent (peak below −90 dBFS, from the
bext true-peak field when Cubase fills it, otherwise by scanning the samples).

A FAIL or a silent mix shows as a Mac notification and a Telegram message from
the DS Scoring bot (through the Worker's `/monitor-notify`, with
`DS_MONITOR_SECRET`; the bot token stays in the Worker). `"bwf_check"` in
`~/.ds-monitor-config.json` sets what a FAIL does, read for every export:

- `"warn"` (default): publish anyway, log and alert
- `"block"`: don't publish; the WAV goes on the waiting list with the reason
  and a corrected re-export publishes normally
- `"off"`: no check

A silent mix or a missing timecode never blocks.

The frame rate is automatic: Cubase writes the project's rate into each
export's iXML chunk (keep **Insert iXML Chunk** on), so the check uses the
rate of the project the file came from. Exports without iXML fall back to the
project's frame rate in DS Scoring (via `/monitor-cues`), then 23.976 with a
log line. If the iXML and DS Scoring disagree, the mix still publishes and the
alert says so ("Cubase project is 25 fps but DS Scoring says 24"), since one of
them is set wrong. Drop-frame timecode is a WARN (only non-drop is checked).
Check files by hand: `python3 bwf_check.py "/path/to/Mixdown"` (`--fps 25`
sets the fallback for files without iXML).

## Cubase XML exports: slimmed automatically

Export each cue version as a **full** track XML: select all tracks, then
File → Export → Selected Tracks, named like the `.cpr` (`2m29 The Gift V2.xml`).
These run to ~250 MB, almost all plugin state and controller data.

When one appears in a watched folder, DS Monitor (`xml_slim.py`) reads it piece by
piece and writes a slim file of ~100 KB **under the same name**, then deletes the
original. The slim file keeps:

- every marker, copied unchanged and tagged with its marker track (`ds_track="Cue"`)
- the project tempo map and time signatures (audio files' own tempo maps are
  renamed `xMTempoEvent` so the importer ignores them)
- every MIDI/instrument track: name, parts and notes, with mute flags
  (`<ds_tracks>` JSON). Audio tracks keep only their regions (no audio data), so
  DS Scoring can show an instrument as used without counting playing time;
  disabled tracks are never in Cubase's export.

It also reads the project's `.cpr` (same name next to the XML, else the newest `.cpr`
in that folder) with `cpr_routing.py` and adds each track's **output group**
(`12l_SYNTH PADS LOW DRY`) so DS Scoring can name instruments by section, including
tracks added by hand without a track preset. Read-only; the `.cpr` format is
undocumented, so if it can't be read the slim file just has no groups.
Check one project: `python3 cpr_routing.py "/path/project.cpr"`.

**Routing check.** Every export is checked for routing slips among the tracks that play:
two tracks with the same name going to different groups, outputs into a SUM/RVB/reverb
channel instead of a section group, and music tracks with no group. They show as a Mac
notification, in the log, and in DS Scoring's import dialog.

To check *before* exporting, save in Cubase (Cmd+S) and choose **Check Routing** in the
DS Monitor menu. It compares the saved project with the template (default
`~/Library/Preferences/Cubase 15/Project Templates/Orchestral_v16 Colors.cpr`, or
`"routing_template"` in `~/.ds-monitor-config.json`) and reports slips only in tracks added
or re-routed since the template, including tracks now going straight to Stereo Out. Disabled
tracks are skipped: the `.cpr` has no readable "disabled" flag, so it checks only the tracks that
were in the cue's newest XML export next to the project (Cubase never exports disabled tracks).
Tracks enabled since that export are checked at the next export. The result
shows in the menu under Check Routing (OK, or each issue on its own line) and in the log with
how long each step took; a Mac notification too when macOS allows them. The template's routing is cached in
`~/Library/Caches/ds-monitor/template-routing.json` until the template file changes.

The original is deleted only after all of these hold: the file is a complete Cubase
export (every tag closed), no process has it open, it didn't change during
conversion, and the slim file reads back with the same markers, tracks, parts and
notes. Otherwise it is left untouched and checked again every 15 s (up to 20
times); the log says why. Other XML files (not Cubase exports) are pushed as before,
unchanged. The slim file is what goes to DS Scoring's `pending_imports`.

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
