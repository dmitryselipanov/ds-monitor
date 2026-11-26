"""The "Tracks with Data" window: a translucent panel that floats above Cubase and lists the
tracks of a cue that play (MIDI or audio), under template-section headings in track-list
order, with fully muted tracks in their own list at the end. Main thread only.

Uses AppKit through PyObjC, which rumps already brings in.
"""

from pathlib import Path

import AppKit
import objc
from AppKit import (NSApp, NSButton, NSColor, NSFont, NSMakeRect, NSOpenPanel, NSPanel, NSScreen,
                    NSPasteboard, NSScrollView, NSTextField, NSView, NSVisualEffectView)
from Foundation import NSObject, NSURL

from tracks_with_data import COLOURS, as_text, detail, report, sections


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
NSTextAlignmentRight = _const("NSTextAlignmentRight", 2)
NSLineBreakByTruncatingTail = _const("NSLineBreakByTruncatingTail", 4)
NSControlSizeSmall = _const("NSControlSizeSmall", 1)
NSWindowCollectionBehaviorCanJoinAllSpaces = _const("NSWindowCollectionBehaviorCanJoinAllSpaces", 1)
NSWindowCollectionBehaviorFullScreenAuxiliary = _const("NSWindowCollectionBehaviorFullScreenAuxiliary", 256)
NSWindowStyleMaskTitled = _const("NSWindowStyleMaskTitled", 1)
NSWindowStyleMaskClosable = _const("NSWindowStyleMaskClosable", 2)
NSWindowStyleMaskResizable = _const("NSWindowStyleMaskResizable", 8)
NSWindowStyleMaskFullSizeContentView = _const("NSWindowStyleMaskFullSizeContentView", 1 << 15)
NSWindowTitleHidden = _const("NSWindowTitleHidden", 1)
NSVisualEffectMaterialHUDWindow = _const("NSVisualEffectMaterialHUDWindow", 13)
NSVisualEffectBlendingModeBehindWindow = _const("NSVisualEffectBlendingModeBehindWindow", 0)
NSVisualEffectStateActive = _const("NSVisualEffectStateActive", 1)
NSFontWeightMedium = _const("NSFontWeightMedium", 0.23)
NSFontWeightSemibold = _const("NSFontWeightSemibold", 0.3)
NSFontWeightBold = _const("NSFontWeightBold", 0.4)

WIDTH = 440
PAD = 20            # left/right margin
ROW = 24            # track row height
HEAD_GAP = 16       # space above a section heading
HEADING = 20        # section heading height
TOP = 92            # title area (below the window's traffic lights)

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
        sender.setTitle_("Copied")

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


def _text(text, frame, size=13, weight=None, color=None, right=False, wrap=False):
    f = NSTextField.alloc().initWithFrame_(frame)
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    if weight is None:
        f.setFont_(NSFont.systemFontOfSize_(size))
    else:
        f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    f.setTextColor_(color or NSColor.labelColor())
    if right:
        f.setAlignment_(NSTextAlignmentRight)
    if not wrap:
        f.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        f.cell().setUsesSingleLineMode_(True)
    return f


def _layout(rep) -> list[tuple]:
    """("heading", title, count) / ("row", row, muted) items in display order."""
    items = []
    for title, rows in sections(rep["rows"]):
        items.append(("heading", title, len(rows)))
        items += [("row", r, False) for r in rows]
    if rep["muted"]:
        items.append(("heading", "Muted", len(rep["muted"])))
        items += [("row", r, True) for r in rep["muted"]]
    return items


def _place(items):
    """(item, y) for each item, and the list's total height."""
    y, placed = 4, []
    for it in items:
        if it[0] == "heading":
            y += HEAD_GAP if placed else 0
            placed.append((it, y))
            y += HEADING
        else:
            placed.append((it, y))
            y += ROW
    return placed, y + 16


def _list_view(items, w):
    """The scrolling list: section headings and track rows, laid out top to bottom."""
    placed, list_h = _place(items)
    doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, w, list_h))
    doc.setAutoresizingMask_(NSViewWidthSizable)
    secondary, tertiary = NSColor.secondaryLabelColor(), NSColor.tertiaryLabelColor()
    for it, y in placed:
        if it[0] == "heading":
            _, title, n = it
            x = PAD
            hexcol = COLOURS.get(title)
            if hexcol:                        # the family's folder colour, as a small dot
                r, g, b_ = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
                dot = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y + 5, 9, 9))
                dot.setWantsLayer_(True)
                dot.layer().setCornerRadius_(4.5)
                dot.layer().setBackgroundColor_(
                    NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b_, 1.0).CGColor())
                doc.addSubview_(dot)
                x = PAD + 16
            h = _text(title.upper(), NSMakeRect(x, y + 2, w - x - PAD - 40, 16),
                      size=10.5, weight=NSFontWeightSemibold, color=secondary)
            h.setAutoresizingMask_(NSViewWidthSizable)
            c = _text(str(n), NSMakeRect(w - PAD - 40, y + 2, 40, 16), size=10.5,
                      weight=NSFontWeightSemibold, color=tertiary, right=True)
            c.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(h)
            doc.addSubview_(c)
            line = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y + HEADING - 1, w - 2 * PAD, 1))
            line.setWantsLayer_(True)
            line.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
            line.setAutoresizingMask_(NSViewWidthSizable)
            doc.addSubview_(line)
        else:
            _, r, muted = it
            dw = 130
            name = _text(r["name"], NSMakeRect(PAD, y + 3, w - 2 * PAD - dw - 8, 18), size=13,
                         weight=NSFontWeightMedium, color=secondary if muted else None)
            name.setAutoresizingMask_(NSViewWidthSizable)
            info = _text(detail(r), NSMakeRect(w - PAD - dw, y + 5, dw, 16), size=11,
                         color=tertiary, right=True)
            info.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0))
            info.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(name)
            doc.addSubview_(info)
    return doc


def close_all():
    for panel, _ in list(_open):
        panel.close()


def show(path: Path, activate: bool = True):
    """Open a floating window with the tracks that play in the cue at `path` (.cpr or .xml).
    activate=False (opened by itself next to Cubase's import dialog): don't take the focus."""
    rep = report(path)
    items = _layout(rep)
    note_h = (16 * sum(1 + len(l) // 66 for l in rep["note"].split("\n")) + 2) if rep["note"] else 0
    head_h = TOP + (note_h + 4 if note_h else 0)
    needed = head_h + max(_place(items)[1], 40)
    height = needed
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
             | NSWindowStyleMaskFullSizeContentView)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(80, 160, WIDTH, height), style, NSBackingStoreBuffered, False)
    panel.setTitle_(f"Tracks with Data: {rep['project']}")
    panel.setTitleVisibility_(NSWindowTitleHidden)
    panel.setTitlebarAppearsTransparent_(True)
    panel.setMovableByWindowBackground_(True)
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)       # stay visible while Cubase is in front
    panel.setReleasedWhenClosed_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setMinSize_((320, 200))
    panel.setFrameAutosaveName_("DSTracksWithData")    # opens where it was last left
    ctl = _Controller.alloc().initWithText_(as_text(rep))
    panel.setDelegate_(ctl)

    bg = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, height))
    bg.setMaterial_(NSVisualEffectMaterialHUDWindow)
    bg.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    bg.setState_(NSVisualEffectStateActive)
    panel.setContentView_(bg)
    w, h = WIDTH, height

    # title area: cue name, then a summary line, then any note (stale export in orange)
    title = _text(rep["project"], NSMakeRect(PAD, h - 58, w - 2 * PAD - 80, 24), size=18,
                  weight=NSFontWeightBold)
    title.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    bg.addSubview_(title)
    if rep["export"]:
        summary = f"{len(rep['rows'])} tracks playing"
        if rep["muted"]:
            summary += f"  ·  {len(rep['muted'])} muted"
    else:
        summary = "No export found"
    sub = _text(summary, NSMakeRect(PAD, h - 78, w - 2 * PAD, 16), size=12,
                color=NSColor.secondaryLabelColor())
    sub.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    bg.addSubview_(sub)

    copy = NSButton.buttonWithTitle_target_action_("Copy", ctl, "copyList:")
    copy.setControlSize_(NSControlSizeSmall)
    copy.setFont_(NSFont.systemFontOfSize_(11))
    copy.setFrame_(NSMakeRect(w - PAD - 70, h - 56, 70, 22))
    copy.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
    bg.addSubview_(copy)

    top = TOP
    if rep["note"]:
        color = NSColor.systemOrangeColor() if rep["stale"] else NSColor.secondaryLabelColor()
        note = _text(rep["note"], NSMakeRect(PAD, h - top - note_h + 6, w - 2 * PAD, note_h),
                     size=11, color=color, wrap=True)
        note.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        bg.addSubview_(note)
        top += note_h + 4

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h - top))
    scroll.setHasVerticalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setDrawsBackground_(False)
    scroll.contentView().setDrawsBackground_(False)
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setDocumentView_(_list_view(items, w))
    bg.addSubview_(scroll)

    # tall enough for the whole list (up to the screen's height), keeping the remembered
    # position's top edge and width; moved up if the list wouldn't fit below it
    screen = panel.screen() or NSScreen.mainScreen()
    if screen is not None:
        vis, f = screen.visibleFrame(), panel.frame()
        title_bar = f.size.height - panel.contentRectForFrameRect_(f).size.height
        hgt = min(needed + title_bar, vis.size.height)
        top = min(f.origin.y + f.size.height, vis.origin.y + vis.size.height)
        y = max(vis.origin.y, top - hgt)
        panel.setFrame_display_(NSMakeRect(f.origin.x, y, f.size.width, hgt), True)

    _open.append((panel, ctl))
    if activate:
        NSApp.activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)
    else:
        panel.orderFrontRegardless()
    return panel
