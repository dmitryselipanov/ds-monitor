"""Open Tracks with Data by itself when Cubase's Import Tracks from Project shows its track list.

What Cubase shows (diagnose_import.py, 2026-11-26, Cubase 15):
  "Select a project to merge with the active one"   the file chooser
  "Reading Project File"                            while it reads the picked .cpr
  "Import Options"                                  the track list; the picked .cpr is still open
Cubase doesn't keep the active project's .cpr open, so the .cpr it has open while "Import
Options" is up is the cue being imported from. Only reads: window titles (System Events,
needs DS Monitor's Python in Privacy › Accessibility) and open files (lsof).
"""

import subprocess
from pathlib import Path

DIALOG = "Import Options"
READING = "Reading Project File"
OWN_APPS = ("Python", "DS//Monitor")      # clicking the panel brings DS Monitor itself to the front
_SKIP_DIRS = ("/auto saves/",)

log = lambda msg: None      # monitor.py sets this to its log()
_said = set()


def _once(msg):
    """Log a message the first time it's seen (the watcher polls every second)."""
    if msg not in _said:
        _said.add(msg)
        log(msg)


def _run(cmd, timeout=5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            _once(f"[data] {Path(cmd[0]).name} failed ({r.returncode}): {r.stderr.strip()[:300]}")
        return r.stdout
    except Exception as e:
        _once(f"[data] {Path(cmd[0]).name} failed: {e}")
        return ""


_front = None
_front_is_cubase = False
_ax_ok = None               # True: Accessibility API works here; False: osascript fallback


def cubase_in_front() -> bool:
    global _front, _front_is_cubase
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        name = str(app.localizedName() or "") if app else ""
    except Exception as e:
        _once(f"[data] can't tell the front app: {e}")
        return True          # can't tell: look anyway
    if name != _front:
        _front = name
        log(f"[data] front app: {name or '?'}")
    _front_is_cubase = name.startswith("Cubase")
    return _front_is_cubase or name in OWN_APPS


def _cubase_pid():
    from AppKit import NSWorkspace
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if str(app.localizedName() or "").startswith("Cubase"):
            return app.processIdentifier()
    return None


def _titles_ax() -> list[str] | None:
    """Cubase's window titles straight from the Accessibility API (in-process, ~1 ms).
    None if that API isn't available here (pyobjc-framework-ApplicationServices missing) or
    refuses (no Accessibility permission): the caller falls back to osascript."""
    global _ax_ok
    try:
        from ApplicationServices import (AXUIElementCopyAttributeValue,
                                         AXUIElementCreateApplication)
    except Exception:
        _ax_ok = False
        _once("[data] Accessibility API not importable here: using osascript (slower)")
        return None
    pid = _cubase_pid()
    if pid is None:
        return []
    app = AXUIElementCreateApplication(pid)
    err, wins = AXUIElementCopyAttributeValue(app, "AXWindows", None)
    if err:
        _ax_ok = False
        _once(f"[data] Accessibility API error {err}: using osascript")
        return None
    titles = []
    for w in wins or []:
        e, t = AXUIElementCopyAttributeValue(w, "AXTitle", None)
        if not e and t:
            titles.append(str(t))
    _ax_ok = True
    _once("[data] reading Cubase's windows through the Accessibility API")
    return titles


def titles() -> list[str]:
    """Cubase's window titles. On the osascript fallback, one string with all of them
    (callers test with `in`, so a comma inside a title doesn't matter)."""
    t = _titles_ax()
    if t is not None:
        return t
    script = ('tell application "System Events" to get name of every window of '
              '(first process whose name starts with "Cubase")')
    # long timeout: the first call can sit behind macOS's permission prompt until it's answered
    out = _run(["/usr/bin/osascript", "-e", script], timeout=120).strip()
    return [out] if out else []


def open_cprs() -> list[str]:
    """.cpr files Cubase has open (its auto-saves left out). Asks lsof about Cubase's process
    only (-p), which is quicker than matching every process by name."""
    try:
        pid = _cubase_pid()
    except Exception:
        pid = None
    cmd = ["/usr/sbin/lsof", "-p", str(pid), "-Fn"] if pid else ["/usr/sbin/lsof", "-c", "Cubase", "-Fn"]
    out = _run(cmd, timeout=10)
    return [l[1:] for l in out.splitlines()
            if l.startswith("n") and l.lower().endswith(".cpr")
            and not any(d in l.lower() for d in _SKIP_DIRS)]


def _has(titles, name) -> bool:
    return any(name in t for t in titles)


class Watcher:
    """Turns what each poll sees into what to do, as a list of events:
      ("show", path, report, visible)  open the panel (report ready-made, see prepare)
      ("visible", bool)                hide it / show it again (Cubase left / back in front)
      ("close",)                       the import dialog closed
    The picked cue is found and its track list read while Cubase still shows "Reading Project
    File" (about 3 s), so the panel can open as soon as "Import Options" appears."""

    def __init__(self):
        self.dialog = False        # Import Options is open
        self.showing = False       # panel shown for it
        self.visible = False       # panel currently on screen
        self.ready = None          # (path, report) prepared while Cubase was reading the file

    def _get(self, cprs, prepare):
        files = cprs()
        if not files:
            return None
        path = files[-1]
        try:
            return path, prepare(path)
        except Exception as e:
            log(f"[data] couldn't read {path}: {e}")
            return path, None

    def step(self, front_ok: bool, titles, cprs, prepare) -> list:
        """`cprs()` lists the .cpr files Cubase has open; `prepare(path)` builds the report."""
        ev = []
        reading, dialog = _has(titles, READING), _has(titles, DIALOG)
        if reading and not dialog and self.ready is None:
            self.ready = self._get(cprs, prepare)
            if self.ready:
                log(f"[data] Cubase is reading {self.ready[0]}: list prepared")
        if dialog and not self.showing:
            if not self.dialog:
                log(f"[data] {DIALOG} is open")
            self.dialog = True
            got = self.ready or self._get(cprs, prepare)
            if got:
                self.showing, self.visible, self.ready = True, front_ok, None
                ev.append(("show", got[0], got[1], front_ok))
        elif not dialog and self.dialog:
            log(f"[data] {DIALOG} closed")
            if self.showing:
                ev.append(("close",))
            self.dialog = self.showing = self.visible = False
        elif self.showing and front_ok != self.visible:
            self.visible = front_ok
            ev.append(("visible", front_ok))
        if not reading and not dialog:
            self.ready = None      # cancelled while reading, or done
        return ev

    def interval(self) -> float:
        """Seconds to the next poll: quick while Cubase is in front or the panel is up; with the
        osascript fallback (a process per check) a little slower."""
        if not (self.dialog or self.ready or _front_is_cubase):
            return 1.0
        return 0.25 if _ax_ok is not False else 0.5

    def poll(self, prepare) -> list:
        front = cubase_in_front()
        if not (front or self.dialog or self.ready):
            return []
        return self.step(front, titles(), open_cprs, prepare)
