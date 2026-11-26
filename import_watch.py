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
    return _front_is_cubase


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


def dialog_open() -> bool:
    """Is Cubase's "Import Options" window showing?"""
    titles = _titles_ax()
    if titles is not None:
        return DIALOG in titles
    script = ('tell application "System Events" to exists window "%s" of '
              '(first process whose name starts with "Cubase")' % DIALOG)
    # long timeout: the first call can sit behind macOS's permission prompt until it's answered
    return _run(["/usr/bin/osascript", "-e", script], timeout=120).strip() == "true"


def open_cprs() -> list[str]:
    """.cpr files Cubase has open (its auto-saves left out)."""
    out = _run(["/usr/sbin/lsof", "-c", "Cubase", "-Fn"], timeout=10)
    return [l[1:] for l in out.splitlines()
            if l.startswith("n") and l.lower().endswith(".cpr")
            and not any(d in l.lower() for d in _SKIP_DIRS)]


class Watcher:
    """Turns what's seen each poll into what to do: ("show", path), ("close", None) or None."""

    def __init__(self):
        self.showing = False       # dialog seen and panel shown for it
        self.dialog = False

    def step(self, dialog: bool, cprs) -> tuple | None:
        """`cprs` is called only while the dialog is up and no panel was shown for it yet."""
        if dialog and not self.showing:
            files = cprs()
            if not self.dialog:
                log(f"[data] {DIALOG} is open; .cpr files Cubase has open: {files or 'none yet'}")
            self.dialog = True
            if files:
                self.showing = True
                return ("show", files[-1])
            return None            # .cpr not seen (yet): try again next poll
        if not dialog and self.dialog:
            log(f"[data] {DIALOG} closed")
            was = self.showing
            self.showing = self.dialog = False
            return ("close", None) if was else None
        return None

    def interval(self) -> float:
        """Seconds to the next poll: quick while Cubase is in front; with the osascript fallback
        (a process per check) a little slower."""
        if not (self.dialog or _front_is_cubase):
            return 1.0
        return 0.25 if _ax_ok is not False else 0.5

    def poll(self) -> tuple | None:
        if not self.dialog and not cubase_in_front():
            return None
        return self.step(dialog_open(), open_cprs)
