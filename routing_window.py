"""The Check Routing result window: a translucent panel in the style of Tracks with Data.
The cue's name, a one-line summary, then one section per kind of slip (what's wrong and what
to do), each listing the tracks with where they go now. Main thread only.
"""

import objc
from AppKit import (NSApp, NSButton, NSColor, NSFont, NSMakeRect, NSPanel, NSPasteboard,
                    NSScreen, NSScrollView, NSView, NSVisualEffectView)
from Foundation import NSObject

from cpr_routing import as_text, grouped, target
from data_window import (NSBackingStoreBuffered, NSControlSizeSmall, NSFloatingWindowLevel,
                         NSFontWeightBold, NSFontWeightMedium, NSPasteboardTypeString,
                         NSViewHeightSizable, NSViewMinXMargin, NSViewMinYMargin,
                         NSViewWidthSizable, NSVisualEffectBlendingModeBehindWindow,
                         NSVisualEffectMaterialHUDWindow, NSVisualEffectStateActive,
                         NSWindowCollectionBehaviorCanJoinAllSpaces,
                         NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowStyleMaskClosable,
                         NSWindowStyleMaskFullSizeContentView, NSWindowStyleMaskResizable,
                         NSWindowStyleMaskTitled, NSWindowTitleHidden, _Flipped, _text)
from tracks_with_data import COLOURS, family

WIDTH = 480
PAD = 20
ROW = 24
HEAD_GAP = 18       # space above a section heading
HEADING = 24        # heading line + separator
HINT_LINE = 15      # one line of the grey "what to do" text
HINT_CHARS = 70     # rough characters per hint line at WIDTH
TOP = 92
TARGET_W = 190      # right-hand column: where the track goes now

_open = []          # panels and their controllers, kept so they aren't garbage-collected


class _Controller(NSObject):
    def initWithText_recheck_(self, text, recheck):
        self = objc.super(_Controller, self).init()
        if self is None:
            return None
        self.text, self.recheck = text, recheck
        return self

    def copyList_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(self.text, NSPasteboardTypeString)
        sender.setTitle_("Copied")

    def checkAgain_(self, sender):
        sender.setTitle_("Checking…")
        sender.setEnabled_(False)
        if self.recheck:
            self.recheck()

    def windowWillClose_(self, note):
        for i, (panel, ctl) in enumerate(_open):
            if ctl is self:
                _open.pop(i)
                break


def close_all():
    for panel, _ in list(_open):
        panel.close()


def _hint_h(hint: str) -> int:
    return HINT_LINE * (1 + len(hint) // HINT_CHARS)


def _place(groups):
    """(item, y) for each heading / hint / row, and the list's total height."""
    y, placed = 4, []
    for kind, heading, hint, rows in groups:
        y += HEAD_GAP if placed else 0
        placed.append((("heading", heading, len(rows)), y))
        y += HEADING
        placed.append((("hint", hint), y))
        y += _hint_h(hint) + 6
        for r in rows:
            placed.append((("row", r), y))
            y += ROW
    return placed, y + 16


def _dot(hexcol, x, y):
    r_, g_, b_ = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    d = NSView.alloc().initWithFrame_(NSMakeRect(x, y, 8, 8))
    d.setWantsLayer_(True)
    d.layer().setCornerRadius_(4)
    d.layer().setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r_, g_, b_, 1.0).CGColor())
    d.setAutoresizingMask_(NSViewMinXMargin)
    return d


def _list_view(groups, w):
    placed, list_h = _place(groups)
    doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, w, list_h))
    doc.setAutoresizingMask_(NSViewWidthSizable)
    orange, secondary = NSColor.systemOrangeColor(), NSColor.secondaryLabelColor()
    for it, y in placed:
        if it[0] == "heading":
            _, heading, n = it
            h = _text(heading.upper(), NSMakeRect(PAD, y + 2, w - 2 * PAD - 44, 16), size=11,
                      weight=NSFontWeightBold, color=orange)
            h.setAutoresizingMask_(NSViewWidthSizable)
            c = _text(str(n), NSMakeRect(w - PAD - 40, y + 2, 40, 16), size=11,
                      weight=NSFontWeightBold, color=orange, right=True)
            c.setAutoresizingMask_(NSViewMinXMargin)
            line = NSView.alloc().initWithFrame_(NSMakeRect(PAD, y + 20, w - 2 * PAD, 1))
            line.setWantsLayer_(True)
            line.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
            line.setAutoresizingMask_(NSViewWidthSizable)
            for v in (h, c, line):
                doc.addSubview_(v)
        elif it[0] == "hint":
            hint = _text(it[1], NSMakeRect(PAD, y, w - 2 * PAD, _hint_h(it[1])), size=11,
                         color=secondary, wrap=True)
            hint.setAutoresizingMask_(NSViewWidthSizable)
            doc.addSubview_(hint)
        else:
            r = it[1]
            name = _text(r["track"], NSMakeRect(PAD, y + 3, w - 2 * PAD - TARGET_W - 12, 18),
                         size=13, weight=NSFontWeightMedium)
            name.setAutoresizingMask_(NSViewWidthSizable)
            doc.addSubview_(name)
            dest = target(r)
            hexcol = COLOURS.get(family(r["groups"][0])) if r["groups"] else None
            tx = w - PAD - TARGET_W
            arrow = _text("→", NSMakeRect(tx - 4, y + 4, 16, 16), size=12, color=NSColor.tertiaryLabelColor())
            arrow.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(arrow)
            if hexcol:                        # the destination family's folder colour
                doc.addSubview_(_dot(hexcol, tx + 16, y + 9))
            info = _text(dest, NSMakeRect(tx + (28 if hexcol else 16), y + 5,
                                          TARGET_W - (28 if hexcol else 16), 16), size=11,
                         color=secondary)
            info.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0))
            info.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(info)
    return doc


def show(project: str, issues: list[dict], note: str = "", secs: float = 0.0, recheck=None):
    """Open (or replace) the result window for `project`. recheck: called by Check Again."""
    close_all()
    groups = grouped(issues)
    note_h = (16 * (1 + len(note) // 66) + 2) if note else 0
    head_h = TOP + (note_h + 4 if note_h else 0)
    body_h = _place(groups)[1] if groups else 40
    needed = head_h + body_h + 44                      # + the button row
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
             | NSWindowStyleMaskFullSizeContentView)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(120, 200, WIDTH, needed), style, NSBackingStoreBuffered, False)
    panel.setTitle_(f"Check Routing: {project}")
    panel.setTitleVisibility_(NSWindowTitleHidden)
    panel.setTitlebarAppearsTransparent_(True)
    panel.setMovableByWindowBackground_(True)
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setReleasedWhenClosed_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary)
    panel.setMinSize_((360, 180))
    panel.setFrameAutosaveName_("DSCheckRouting")
    ctl = _Controller.alloc().initWithText_recheck_(as_text(project, issues, note), recheck)
    panel.setDelegate_(ctl)

    screen = panel.screen() or NSScreen.mainScreen()
    if screen is not None:                             # whole list, up to the screen's height
        vis, f = screen.visibleFrame(), panel.frame()
        title_bar = f.size.height - panel.contentRectForFrameRect_(f).size.height
        hgt = min(needed + title_bar, vis.size.height)
        top_edge = min(f.origin.y + f.size.height, vis.origin.y + vis.size.height)
        y = max(vis.origin.y, top_edge - hgt)
        panel.setFrame_display_(NSMakeRect(f.origin.x, y, f.size.width, hgt), True)

    content = panel.contentRectForFrameRect_(panel.frame())
    w, h = content.size.width, content.size.height
    bg = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    bg.setMaterial_(NSVisualEffectMaterialHUDWindow)
    bg.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    bg.setState_(NSVisualEffectStateActive)
    panel.setContentView_(bg)

    title = _text(project, NSMakeRect(PAD, h - 58, w - 2 * PAD - 80, 24), size=18, weight=NSFontWeightBold)
    title.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    bg.addSubview_(title)
    n = len(issues)
    when = f"  ·  checked in {secs:.0f}s" if secs >= 1 else ""
    if n:
        summary, color = f"⚠ {n} routing issue{'s' if n != 1 else ''}{when}", NSColor.systemOrangeColor()
    else:
        summary, color = f"✓ Routing OK{when}", NSColor.systemGreenColor()
    sub = _text(summary, NSMakeRect(PAD, h - 78, w - 2 * PAD, 16), size=12, weight=NSFontWeightMedium, color=color)
    sub.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    bg.addSubview_(sub)

    copy = NSButton.buttonWithTitle_target_action_("Copy", ctl, "copyList:")
    copy.setControlSize_(NSControlSizeSmall)
    copy.setFont_(NSFont.systemFontOfSize_(11))
    copy.setFrame_(NSMakeRect(w - PAD - 70, h - 56, 70, 22))
    copy.setAutoresizingMask_(NSViewMinXMargin | NSViewMinYMargin)
    bg.addSubview_(copy)

    top = TOP
    if note:
        nt = _text(note, NSMakeRect(PAD, h - top - note_h + 6, w - 2 * PAD, note_h), size=11,
                   color=NSColor.secondaryLabelColor(), wrap=True)
        nt.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        bg.addSubview_(nt)
        top += note_h + 4

    # bottom row: Check Again (save in Cubase first) and Done
    done = NSButton.buttonWithTitle_target_action_("Done", panel, "performClose:")
    done.setKeyEquivalent_("\r")
    done.setFrame_(NSMakeRect(w - PAD - 90, 12, 90, 28))
    done.setAutoresizingMask_(NSViewMinXMargin)
    bg.addSubview_(done)
    if recheck:
        again = NSButton.buttonWithTitle_target_action_("Check Again", ctl, "checkAgain:")
        again.setFrame_(NSMakeRect(w - PAD - 90 - 8 - 120, 12, 120, 28))
        again.setAutoresizingMask_(NSViewMinXMargin)
        bg.addSubview_(again)
        tip = _text("Save in Cubase first", NSMakeRect(PAD, 18, w - 2 * PAD - 230, 16), size=11,
                    color=NSColor.tertiaryLabelColor())
        tip.setAutoresizingMask_(NSViewWidthSizable)
        bg.addSubview_(tip)

    if groups:
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 48, w, h - top - 48))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.contentView().setDrawsBackground_(False)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setDocumentView_(_list_view(groups, w))
        bg.addSubview_(scroll)
    else:
        ok = _text("No issues in tracks added or changed since the template.",
                   NSMakeRect(PAD, h - top - 20, w - 2 * PAD, 16), size=12,
                   color=NSColor.secondaryLabelColor())
        ok.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        bg.addSubview_(ok)

    _open.append((panel, ctl))
    NSApp.activateIgnoringOtherApps_(True)
    panel.makeKeyAndOrderFront_(None)
    return panel
