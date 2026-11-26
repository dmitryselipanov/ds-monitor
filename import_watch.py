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

DIALOG = "Import Options"
_SKIP_DIRS = ("/auto saves/",)


def _run(cmd, timeout=5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def cubase_in_front() -> bool:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return bool(app) and str(app.localizedName() or "").startswith("Cubase")
    except Exception:
        return True          # can't tell: look anyway


def dialog_open() -> bool:
    """Is Cubase's "Import Options" window showing?"""
    script = ('tell application "System Events" to exists window "%s" of '
              '(first process whose name starts with "Cubase")' % DIALOG)
    return _run(["/usr/bin/osascript", "-e", script]).strip() == "true"


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
            self.dialog = True
            if files:
                self.showing = True
                return ("show", files[-1])
            return None            # .cpr not seen (yet): try again next poll
        if not dialog and self.dialog:
            was = self.showing
            self.showing = self.dialog = False
            return ("close", None) if was else None
        return None

    def poll(self) -> tuple | None:
        if not self.dialog and not cubase_in_front():
            return None
        return self.step(dialog_open(), open_cprs)
