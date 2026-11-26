"""What macOS Accessibility can see inside Cubase's "Import Options" dialog (the track list of
Import Tracks from Project), to find out whether DS Monitor could tick tracks there itself.
Read-only: it lists the dialog's elements and clicks nothing.

Run in Terminal while the dialog is open (File › Import › Tracks from Project › pick a cue):
    python3 ~/ds-monitor/diagnose_import_ax.py
It writes ~/Desktop/import_dialog_ax.txt and prints a short summary. Send the file.
Terminal needs Privacy › Accessibility (it has it from diagnose_import.py).

What decides it: if the track names show up as rows/cells/checkboxes (with their names), a
"Select in Cubase" button can tick them by name. If the dialog shows only a few generic
groups with no names, Cubase draws the list itself and it can't be done this way.
"""
import subprocess
from collections import Counter
from pathlib import Path

SCRIPT = r'''
on safe(f)
    try
        set v to f
        if v is missing value then return ""
        return v as text
    on error
        return ""
    end try
end safe
tell application "System Events"
    set p to first process whose name starts with "Cubase"
    if not (exists window "Import Options" of p) then return "NO DIALOG"
    set w to window "Import Options" of p
    set out to ""
    set i to 0
    repeat with e in (entire contents of w)
        set i to i + 1
        if i > 4000 then exit repeat
        set r to ""
        try
            set r to role of e
        end try
        set n to ""
        try
            set n to my safe(name of e)
        end try
        set d to ""
        try
            set d to my safe(description of e)
        end try
        set v to ""
        try
            set v to my safe(value of e)
        end try
        set a to ""
        try
            set a to my safe(name of every action of e)
        end try
        set out to out & r & tab & n & tab & d & tab & v & tab & a & linefeed
    end repeat
    return out
end tell
'''


def main():
    r = subprocess.run(["/usr/bin/osascript", "-e", SCRIPT], capture_output=True, text=True, timeout=300)
    if r.returncode:
        print("osascript failed:", r.stderr.strip())
        return
    text = r.stdout.strip()
    if text == "NO DIALOG":
        print('Cubase\'s "Import Options" window isn\'t open. Open it first, then run this again.')
        return
    out = Path.home() / "Desktop" / "import_dialog_ax.txt"
    out.write_text("role\tname\tdescription\tvalue\tactions\n" + text + "\n", encoding="utf-8")
    rows = [l.split("\t") for l in text.splitlines()]
    roles = Counter(x[0] for x in rows)
    named = [x for x in rows if len(x) > 1 and (x[1] or x[2] or x[3])]
    print(f"{len(rows)} elements in the dialog; roles: " + ", ".join(f"{k} ×{v}" for k, v in roles.most_common(12)))
    print(f"{len(named)} have a name, description or value. First few:")
    for x in named[:15]:
        print("   ", " | ".join(s for s in x if s)[:120])
    print(f"\nFull list: {out}")


if __name__ == "__main__":
    main()
