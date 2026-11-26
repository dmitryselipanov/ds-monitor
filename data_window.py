"""The "Tracks with Data" window: a small panel that floats above Cubase and lists the tracks
of a cue that hold MIDI or audio, each with a tick box to tick off while selecting tracks in
Cubase's Import Tracks from Project dialog. Main thread only (it's called from a menu item).

Uses AppKit through PyObjC, which rumps already brings in.
"""

from pathlib import Path

import AppKit
import objc
from AppKit import (NSApp, NSButton, NSColor, NSFont, NSMakeRect, NSOpenPanel, NSPanel,
                    NSPasteboard, NSScrollView, NSTextField, NSView)
from Foundation import NSObject, NSURL


def _const(name, value):
    """AppKit constants by name, with Apple's documented value if this PyObjC lacks it."""
    return getattr(AppKit, name, value)


NSBackingStoreBuffered = _const("NSBackingStoreBuffered", 2)
NSFloatingWindowLevel = _const("NSFloatingWindowLevel", 3)
NSModalResponseOK = _const("NSModalResponseOK", 1)
NSPasteboardTypeString = _const("NSPasteboardTypeString", "public.utf8-plain-text")
NSViewMinXMargin = _const("NSViewMinXMargin", 1)
NSViewWidthSizable = _const("NSViewWidthSizable", 2)
NSViewMinYMargin = _const("NSViewMinYMargin", 8)
NSViewHeightSizable = _const("NSViewHeightSizable", 16)
NSWindowCollectionBehaviorCanJoinAllSpaces = _const("NSWindowCollectionBehaviorCanJoinAllSpaces", 1)
NSWindowCollectionBehaviorFullScreenAuxiliary = _const("NSWindowCollectionBehaviorFullScreenAuxiliary", 256)
NSWindowStyleMaskTitled = _const("NSWindowStyleMaskTitled", 1)
NSWindowStyleMaskClosable = _const("NSWindowStyleMaskClosable", 2)
NSWindowStyleMaskResizable = _const("NSWindowStyleMaskResizable", 8)
NSWindowStyleMaskUtilityWindow = _const("NSWindowStyleMaskUtilityWindow", 16)

from tracks_with_data import as_text, label, report

ROW = 22
HEADER = 64
WIDTH = 560

_open = []          # panels and their controllers: kept here so they aren't garbage-collected


class _Flipped(NSView):
    def isFlipped(self):
        return True


class _Controller(NSObject):
    def initWithText_(self, text):
        self = objc.super(_Controller, self).init()
        if self is None:
            return None
        self.text = text
        return self

    def copyList_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self.text, NSPasteboardTypeString)

    def windowWillClose_(self, note):
        for i, (panel, ctl) in enumerate(_open):
            if ctl is self:
                _open.pop(i)
                break


def choose_project(start_dir: Path | None) -> Path | None:
    """File chooser for the cue to import from (.cpr, or its .xml export)."""
    panel = NSOpenPanel.openPanel()
    panel.setTitle_("Tracks with Data: pick the cue to import from")
    panel.setPrompt_("Show Tracks")
    panel.setCanChooseFiles_(True)
    panel.setCanChooseDirectories_(False)
    panel.setAllowsMultipleSelection_(False)
    panel.setAllowedFileTypes_(["cpr", "xml"])
    if start_dir and start_dir.exists():
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(str(start_dir)))
    NSApp.activateIgnoringOtherApps_(True)
    if panel.runModal() != NSModalResponseOK:
        return None
    url = panel.URLs()[0] if panel.URLs() else None
    return Path(url.path()) if url else None


def _label(text, frame, bold=False, size=12):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(True)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    return f


def show(path: Path):
    """Open a floating window with the tracks with data of the cue at `path` (.cpr or .xml)."""
    rep = report(path)
    rows = rep["rows"]
    height = min(HEADER + max(len(rows), 1) * ROW + 16, 760)
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
             | NSWindowStyleMaskUtilityWindow)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(80, 200, WIDTH, height), style, NSBackingStoreBuffered, False)
    panel.setTitle_(f"Tracks with Data: {rep['project']}")
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)       # stay visible while Cubase is in front
    panel.setReleasedWhenClosed_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
    ctl = _Controller.alloc().initWithText_(as_text(rep))
    panel.setDelegate_(ctl)

    content = panel.contentView()
    w, h = content.frame().size.width, content.frame().size.height

    count = (f"{len(rows)} tracks with data (of {rep['total']} enabled at export)"
             if rep["export"] else "No export found")
    top = _label(count, NSMakeRect(12, h - 26, w - 120, 18), bold=True, size=13)
    top.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    content.addSubview_(top)

    copy = NSButton.buttonWithTitle_target_action_("Copy list", ctl, "copyList:")
    copy.setFrame_(NSMakeRect(w - 104, h - 30, 92, 24))
    copy.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)   # stays top-right
    content.addSubview_(copy)

    note = rep["note"] or (rep["export"].name if rep["export"] else "")
    info = _label(note, NSMakeRect(12, h - HEADER + 2, w - 24, HEADER - 30), size=11)
    info.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    if rep["stale"]:
        info.setTextColor_(NSColor.systemOrangeColor())
    content.addSubview_(info)

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h - HEADER))
    scroll.setHasVerticalScroller_(True)
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, w, max(len(rows), 1) * ROW + 8))
    doc.setAutoresizingMask_(NSViewWidthSizable)
    for i, r in enumerate(rows):
        box = NSButton.checkboxWithTitle_target_action_(f"{r['n']:>3}.  {label(r)}", None, None)
        box.setFrame_(NSMakeRect(10, 4 + i * ROW, w - 20, ROW))
        box.setAutoresizingMask_(NSViewWidthSizable)
        if r["muted"]:
            box.setAlphaValue_(0.55)
        doc.addSubview_(box)
    scroll.setDocumentView_(doc)
    content.addSubview_(scroll)

    _open.append((panel, ctl))
    NSApp.activateIgnoringOtherApps_(True)
    panel.makeKeyAndOrderFront_(None)
    return panel
