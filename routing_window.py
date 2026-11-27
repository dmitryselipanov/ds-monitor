"""The Check Routing result window: a translucent panel in the style of Tracks with Data.
The cue's name, a one-line summary, then one section per kind of slip (what's wrong and what
to do), each listing the tracks with where they go now. Main thread only.
"""

import math

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

MIN_WIDTH = 440
MAX_HINT_LINE = 700  # a short hint stays on one line up to this window width
PAD = 20
ROW = 24
HEAD_GAP = 18       # space above a section heading
HEADING = 24        # heading line + separator
TOP = 92
ARROW_W = 18        # the → before where the track goes now

_open = []          # panels and their controllers, kept so they aren't garbage-collected


class _RoutingController(NSObject):
    def initWithText_recheck_(self, text, recheck):
        self = objc.super(_RoutingController, self).init()
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


def _width(text, size, weight=None) -> float:
    """How wide `text` draws on one line."""
    f = _text(text, NSMakeRect(0, 0, 4000, 30), size=size, weight=weight)
    f.sizeToFit()
    return math.ceil(f.frame().size.width)


def _wrap_h(text, size, width) -> float:
    """How tall `text` is when wrapped to `width`."""
    f = _text(text, NSMakeRect(0, 0, width, 30), size=size, wrap=True)
    return math.ceil(f.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 100000)).size.height)


def _place(groups, w):
    """(item, y) for each heading / hint / row, and the list's total height at width w."""
    y, placed = 4, []
    for kind, heading, hint, rows in groups:
        y += HEAD_GAP if placed else 0
        placed.append((("heading", heading, len(rows)), y))
        y += HEADING
        hh = _wrap_h(hint, 11, w - 2 * PAD)
        placed.append((("hint", hint, hh), y))
        y += hh + 6
        for r in rows:
            placed.append((("row", r), y))
            y += ROW
    return placed, y + 16


def _list_view(groups, w, target_w):
    placed, list_h = _place(groups, w)
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
            hint = _text(it[1], NSMakeRect(PAD, y, w - 2 * PAD, it[2] + 2), size=11,
                         color=secondary, wrap=True)
            hint.setAutoresizingMask_(NSViewWidthSizable)
            doc.addSubview_(hint)
        else:
            r = it[1]
            name = _text(r["track"], NSMakeRect(PAD, y + 3, w - 2 * PAD - target_w - 12, 18),
                         size=13, weight=NSFontWeightMedium)
            name.setAutoresizingMask_(NSViewWidthSizable)
            doc.addSubview_(name)
            dest = target(r)
            tx = w - PAD - target_w
            arrow = _text("→", NSMakeRect(tx - 4, y + 4, 16, 16), size=12, color=NSColor.tertiaryLabelColor())
            arrow.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(arrow)
            # plain text: Cubase's output buses have no colour of their own
            info = _text(dest, NSMakeRect(tx + ARROW_W, y + 4, target_w - ARROW_W, 17), size=12, color=secondary)
            info.setAutoresizingMask_(NSViewMinXMargin)
            doc.addSubview_(info)
    return doc


def show(project: str, issues: list[dict], note: str = "", secs: float = 0.0, recheck=None):
    """Open (or replace) the result window for `project`. recheck: called by Check Again."""
    close_all()
    groups = grouped(issues)
    rows = [r for g in groups for r in g[3]]
    # wide enough for the longest track name and destination side by side, the title with its
    # Copy button, and each hint on one line (up to MAX_HINT_LINE; longer hints wrap)
    target_w = ARROW_W + max([_width(target(r), 12) for r in rows] or [0]) + 4
    name_w = max([_width(r["track"], 13, NSFontWeightMedium) for r in rows] or [0]) + 4
    w = max(MIN_WIDTH,
            2 * PAD + name_w + 24 + target_w,
            2 * PAD + _width(project, 18, NSFontWeightBold) + 100,
            min(MAX_HINT_LINE, max([2 * PAD + _width(g[2], 11) + 4 for g in groups] or [0])))
    screen = NSScreen.mainScreen()
    vis = screen.visibleFrame() if screen is not None else None
    if vis is not None:
        w = min(w, vis.size.width - 40)
    note_h = _wrap_h(note, 11, w - 2 * PAD) if note else 0
    head_h = TOP + (note_h + 8 if note_h else 0)
    body_h = _place(groups, w)[1] if groups else 40
    needed = head_h + body_h + 44                      # + the button row
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
             | NSWindowStyleMaskFullSizeContentView)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, needed), style, NSBackingStoreBuffered, False)
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
    ctl = _RoutingController.alloc().initWithText_recheck_(as_text(project, issues, note), recheck)
    panel.setDelegate_(ctl)

    if vis is not None:                                # taller than the screen: the list scrolls
        f = panel.frame()
        title_bar = f.size.height - panel.contentRectForFrameRect_(f).size.height
        panel.setFrame_display_(NSMakeRect(0, 0, f.size.width, min(needed + title_bar, vis.size.height)), False)
    panel.center()                                     # always opens in the middle of the screen

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
        nt = _text(note, NSMakeRect(PAD, h - top - note_h + 4, w - 2 * PAD, note_h + 2), size=11,
                   color=NSColor.secondaryLabelColor(), wrap=True)
        nt.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        bg.addSubview_(nt)
        top += note_h + 8

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
        scroll.setDocumentView_(_list_view(groups, w, target_w))
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
