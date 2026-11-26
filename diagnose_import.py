"""Find out what DS Monitor can see of Cubase's Import Tracks from Project, so Tracks with Data
can open by itself for the cue picked there. Read-only: it only watches.

Run in Terminal:  python3 ~/ds-monitor/diagnose_import.py
then, within 90 seconds, in Cubase: File › Import › Tracks from Project, pick a cue, and leave the
track list dialog open for a few seconds before cancelling it. Paste everything it printed.

It watches three things, printing only changes:
  files   .cpr files Cubase has open (lsof)
  prefs   keys in Cubase's preferences domain (the file chooser keeps its last folder there)
  windows Cubase's window titles (System Events; needs Terminal in Privacy › Accessibility)
"""
import plistlib
import subprocess
import time

SECONDS = 90


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:
        return f"ERROR {e}"


def cubase_domains():
    out = run(["defaults", "domains"])
    return [d.strip() for d in out.split(",") if "cubase" in d.lower() or "steinberg" in d.lower()]


def open_cprs():
    out = run(["lsof", "-c", "Cubase", "-Fn"])
    return {l[1:] for l in out.splitlines() if l.startswith("n") and l.lower().endswith(".cpr")}


def prefs(domains):
    keys = {}
    for d in domains:
        try:
            raw = subprocess.run(["defaults", "export", d, "-"], capture_output=True, timeout=5).stdout
            for k, v in plistlib.loads(raw).items():
                if isinstance(v, (str, int, float)) or (isinstance(v, list) and len(repr(v)) < 400):
                    keys[f"{d} {k}"] = repr(v)[:300]
        except Exception as e:
            keys[f"{d} ERROR"] = str(e)
    return keys


def windows():
    script = ('tell application "System Events" to get name of every window of '
              '(every process whose name starts with "Cubase")')
    return run(["osascript", "-e", script]).strip()


def main():
    domains = cubase_domains()
    print("Cubase preference domains:", domains or "none found")
    files, keys, wins = open_cprs(), prefs(domains), windows()
    print("files at start:", sorted(files) or "none")
    print("windows at start:", wins)
    print(f"\nWatching for {SECONDS} s. Now: File › Import › Tracks from Project, pick a cue, wait on its track list.\n")
    end = time.time() + SECONDS
    while time.time() < end:
        time.sleep(0.5)
        t = time.strftime("%H:%M:%S")
        f = open_cprs()
        for p in sorted(f - files):
            print(t, "files   + opened", p)
        for p in sorted(files - f):
            print(t, "files   - closed", p)
        files = f
        k = prefs(domains)
        for n in sorted(set(k) | set(keys)):
            if k.get(n) != keys.get(n):
                print(t, "prefs  ", n, ":", keys.get(n), "→", k.get(n))
        keys = k
        w = windows()
        if w != wins:
            print(t, "windows", w)
            wins = w
    print("\nDone.")


if __name__ == "__main__":
    main()
