import tempfile
import unittest
from pathlib import Path

import cpr_routing as cr
from xml_slim import replace_with_slim
from test_xml_slim import FULL


def bus(uid, typ, name):
    raw = name.encode()
    return cr.NAME_START + bytes([len(raw) + 1]) + raw + cr.NAME_END + cr.BUS_KEY + uid.to_bytes(8, "big") \
        + cr.BUS_TYPE + typ.to_bytes(8, "big") + b"\x01\x02"


def track(name, out_uid):
    raw = name.encode() + b"\x00"
    out = cr.TRACK_OUT + out_uid.to_bytes(8, "big") if out_uid is not None else b""
    return b"..track.." + out + b"..." + cr.TRACK_NAME + bytes([len(raw) + 1]) + raw + b"..."


def channel(name, out_uid):
    """An audio track's mixer channel: no RecorderBus record, only VST Multitrack + String name."""
    raw = name.encode() + b"\x00"
    return (b"..." + cr.CHANNEL + b"\x00\x00RuntimeID\x00\x00Name\x00" + cr.STRING_VALUE + bytes([len(raw) + 1]) + raw
            + b"..plugins.." + cr.TRACK_OUT + out_uid.to_bytes(8, "big") + b"...")


# Same patterns the template reader matched on Orchestral_v16 (groups 14, FX 17; an
# instrument channel with a colliding id, type 20, must not win).
CPR = (b"header" + bus(10962, 14, "12l_SYNTH PADS LOW DRY") + bus(10970, 14, "07b_STR HIGH LONGS DRY")
       + bus(10962, 20, "Propheeet") + bus(10980, 17, "05a_HARP DRY") + bus(300, 18, "Stereo Out")
       + track("Propheeet", 10962) + track("SF AS Vlns I Legato", 10970) + track("Sketch Piano I", 300)
       + track("New Track Without Preset", 10962) + track("No Output", None)
       + track("SF AS Vlns I Legato", 10962)   # stale second record: the first one wins
       + track("SF Harp Main", 10980)
       + track("12l_SYNTH PADS LOW DRY", 10970)   # a group channel's own record: not a track
       + channel("Noise Beat", 10990) + bus(10990, 14, "12f_SYNTH PERC MID SUM")
       + channel("Propheeet", 10962)              # channel record agrees with the first track record
       + bus(11000, 14, "13_BAND ALL") + bus(11010, 14, "12g_SYNTH PERC HIGH DRY")
       + channel("Ride", 11000) + channel("Ride", 11010))   # two tracks named Ride: the section group wins


class CprRoutingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cpr = Path(self.dir.name) / "2m29 The Gift.cpr"
        self.cpr.write_bytes(CPR)

    def tearDown(self):
        self.dir.cleanup()

    def test_each_track_gets_its_section_group(self):
        self.assertEqual(cr.output_groups(self.cpr), {
            "Propheeet": "12l_SYNTH PADS LOW DRY",
            "SF AS Vlns I Legato": "07b_STR HIGH LONGS DRY",
            "New Track Without Preset": "12l_SYNTH PADS LOW DRY",
            "SF Harp Main": "05a_HARP DRY",
            "Noise Beat": "12f_SYNTH PERC MID SUM",
            "Ride": "12g_SYNTH PERC HIGH DRY",
        })

    def test_unreadable_file_gives_nothing(self):
        self.cpr.write_bytes(b"\x00\x01garbage")
        self.assertEqual(cr.output_groups(self.cpr), {})
        self.assertEqual(cr.output_groups(Path(self.dir.name) / "missing.cpr"), {})

    def test_finds_the_project_of_an_xml(self):
        xml = Path(self.dir.name) / "2m29 The Gift.xml"
        self.assertEqual(cr.find_project(xml), self.cpr)
        other = Path(self.dir.name) / "2m29 The Gift V2.xml"
        (Path(self.dir.name) / "2m29 The Gift-01.cpr").write_bytes(b"backup")
        self.assertEqual(cr.find_project(other), self.cpr)

    def test_slim_file_carries_the_groups(self):
        import json, re
        xml = Path(self.dir.name) / "2m29 The Gift.xml"
        xml.write_text(FULL, encoding="utf-8")
        stats = replace_with_slim(xml, groups={"Kick&Snare": "03d_SNARES DRY"})
        self.assertEqual(stats["grouped"], 1)
        tracks = json.loads(re.search(r"<ds_tracks><!\[CDATA\[(.*?)\]\]></ds_tracks>", xml.read_text(), re.S).group(1))
        self.assertEqual({t["name"]: t.get("g") for t in tracks}, {"Kick&Snare": "03d_SNARES DRY", "Vln I": None})


class RoutingIssuesTests(unittest.TestCase):
    def tracks(self, *names, muted=()):
        return [{"name": n, "parts": [{"s": 0, "l": 960, "o": 0, "m": 2 if n in muted else 0, "n": [[0, 480, 60, 0]]}]} for n in names]

    def test_flags_the_three_kinds_and_skips_the_rest(self):
        tracks = self.tracks("Ride", "Ride", "Detuned Mid Pad", "Unrouted Synth", "Propheeet", "Dx", "Muted Thing", muted=("Muted Thing",))
        chosen = {"Ride": "12g_SYNTH PERC HIGH DRY", "Detuned Mid Pad": "03a_BASS DRUMS SUM", "Propheeet": "12l_SYNTH PADS LOW DRY"}
        every = {"Ride": ["13_BAND ALL", "12g_SYNTH PERC HIGH DRY"], "Detuned Mid Pad": ["03a_BASS DRUMS SUM"], "Propheeet": ["12l_SYNTH PADS LOW DRY", "Stale Group"]}
        issues = cr.routing_issues(tracks, chosen, every)
        self.assertEqual([(i["track"], i["kind"]) for i in issues],
                         [("Ride", "same"), ("Detuned Mid Pad", "bus"), ("Unrouted Synth", "none")])
        # Propheeet has a stale second record but only one track: not flagged. Dx and muted tracks: skipped.
        self.assertIn("Rename one or fix its output", cr.describe(issues[0]))

    def test_nothing_when_the_project_could_not_be_read(self):
        self.assertEqual(cr.routing_issues(self.tracks("Anything"), {}, {}), [])

    def test_slim_file_carries_the_issues(self):
        import json, re
        with tempfile.TemporaryDirectory() as d:
            xml = Path(d) / "cue.xml"
            xml.write_text(FULL.replace('<int name="Flags" value="2"/>\n                        </obj>', '</obj>'), encoding="utf-8")
            stats = replace_with_slim(xml, groups={"Vln I": "07b_STR HIGH LONGS DRY"}, every={"Vln I": ["07b_STR HIGH LONGS DRY"]})
            self.assertEqual(stats["routing_issues"], 1)                 # Kick&Snare plays with no group
            self.assertEqual(stats["issues"][0]["track"], "Kick&Snare")
            body = json.loads(re.search(r"<ds_routing><!\[CDATA\[(.*?)\]\]></ds_routing>", xml.read_text(), re.S).group(1))
            self.assertEqual(body[0]["kind"], "none")


class CheckRoutingTests(unittest.TestCase):
    TEMPLATE = {
        "Propheeet": ["12l_SYNTH PADS LOW DRY"],
        "Sketch Piano I": [None],                       # Stereo Out by design: never reported
        "Detuned Mid Pad": ["03a_BASS DRUMS SUM"],      # a slip already in the template: not reported again
        "SF Harp Main": ["05a_HARP DRY"],
    }

    def test_only_tracks_added_or_rerouted_since_the_template(self):
        project = dict(self.TEMPLATE)
        project.update({
            "Ride": ["13_BAND ALL", "12g_SYNTH PERC HIGH DRY"],   # new, two tracks, two groups
            "Noise Beat": ["12f_SYNTH PERC MID SUM"],              # new, into a SUM
            "New Synth": [None],                                    # new, no group
            "SF Harp Main": ["05a_HARP RVB"],                       # re-routed into a reverb sum
            "Gtr": ["13_BAND ALL"],                                 # new, straight into the family bus
            "Gtr Dist": ["13c_GTR EL DRY"],                         # new and fine
            "Sketch Piano V": ["06_PIANO ALL"],                     # sketch instruments: by design
            "Dx": [None],                                           # utility
            "Propheeet": ["12l_SYNTH PADS LOW DRY", None],          # a copy routed straight to Stereo Out
        })
        issues = cr.project_issues(project, self.TEMPLATE)
        self.assertEqual([(i["track"], i["kind"]) for i in issues],
                         [("Gtr", "family"), ("New Synth", "none"), ("Noise Beat", "bus"), ("Propheeet", "none"),
                          ("Ride", "same"), ("SF Harp Main", "bus")])

    def test_family_bus_wording(self):
        i = {"track": "Gtr", "kind": "family", "groups": ["13_BAND ALL"]}
        self.assertEqual(cr.short(i), "Gtr → 13_BAND ALL (family bus, not a DRY group)")
        self.assertIn("13_BAND ALL", cr.describe(i))

    def test_template_track_rerouted_to_stereo_out(self):
        project = dict(self.TEMPLATE, **{"SF Harp Main": [None]})
        self.assertEqual([(i["track"], i["kind"]) for i in cr.project_issues(project, self.TEMPLATE)],
                         [("SF Harp Main", "none")])

    def test_records_keep_unrouted_tracks_and_skip_channels(self):
        rec = cr.routing_records(CPR)
        self.assertEqual(rec["Sketch Piano I"], [None])
        self.assertEqual(rec["Ride"], ["13_BAND ALL", "12g_SYNTH PERC HIGH DRY"])
        self.assertNotIn("12l_SYNTH PADS LOW DRY", rec)

    def test_check_project_with_cached_template(self):
        with tempfile.TemporaryDirectory() as d:
            proj, tmpl = Path(d) / "2m29.cpr", Path(d) / "Orchestral.cpr"
            proj.write_bytes(CPR)
            # the template: the same file before Noise Beat and the two Ride tracks were added
            tmpl.write_bytes(CPR.replace(channel("Noise Beat", 10990), b"").replace(channel("Ride", 11000), b"").replace(channel("Ride", 11010), b""))
            old = cr.TEMPLATE_CACHE
            cr.TEMPLATE_CACHE = Path(d) / "cache.json"
            try:
                first = cr.check_project(proj, tmpl)
                self.assertTrue(cr.TEMPLATE_CACHE.exists())
                self.assertEqual(cr.check_project(proj, tmpl), first)     # second run reads the cache
            finally:
                cr.TEMPLATE_CACHE = old
            self.assertEqual({i["track"]: i["kind"] for i in first}, {"Ride": "same", "Noise Beat": "bus"})


class RealRecordsTests(unittest.TestCase):
    """Record shapes seen in 3m30 Still Here (2026-11-14, cpr_routing --why)."""
    CPR3 = (b"h" + bus(1, 18, "Stereo Out") + bus(70, 17, "07b_STR HIGH LONGS DRY")
            + bus(80, 20, "SSB 2 Cimbassi Shorts") + bus(90, 17, "12n_SYNTH LEADS DLY")
            + bus(95, 14, "12e_SYNTH PERC LOW SUM")
            # channel record right, second track record junk (id of nothing): routed fine
            + track("SSS Vlns I Sus CS-Ord", 70) + track("SSS Vlns I Sus CS-Ord", 4242) + channel("SSS Vlns I Sus CS-Ord", 70)
            # output id only matches an instrument/port (type 20): unknown, not reported
            + track("BH Flutes-Clarinets Shorts", 80) + channel("BH Flutes-Clarinets Shorts", 80)
            # really on Stereo Out
            + track("Serum Pulse", 1) + track("Serum Pulse", 1) + channel("Serum Pulse", 1)
            + track("Sketch Piano I", 1) + channel("Sketch Piano I", 1)
            # into a delay FX channel
            + track("Synplant Nice Pad", 90) + channel("Synplant Nice Pad", 90)
            # channel unreadable, junk second track record into a SUM: the junk is ignored
            + track("Iceni Swells", 4242) + track("Iceni Swells", 95) + channel("Iceni Swells", 4242))

    def test_only_real_slips(self):
        issues = cr.project_issues(cr.routing_records(self.CPR3), {})
        self.assertEqual([(i["track"], i["kind"]) for i in issues],
                         [("Serum Pulse", "none"), ("Synplant Nice Pad", "bus")])
        self.assertIn("Stereo Out", cr.describe(issues[0]))

    def test_export_groups_ignore_junk_records(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "3m30.cpr"
            p.write_bytes(self.CPR3)
            chosen, every = cr.output_routing(p)
        self.assertEqual(chosen["SSS Vlns I Sus CS-Ord"], "07b_STR HIGH LONGS DRY")
        self.assertEqual(every["SSS Vlns I Sus CS-Ord"], ["07b_STR HIGH LONGS DRY"])
        self.assertNotIn("Iceni Swells", chosen)


class EnabledTracksTests(unittest.TestCase):
    def test_names_from_the_newest_slim_export(self):
        import os, time
        with tempfile.TemporaryDirectory() as d:
            cpr = Path(d) / "3m30 Still Here.cpr"
            cpr.write_bytes(b"x")
            self.assertEqual(cr.enabled_tracks(cpr), (None, None))
            old, new = Path(d) / "3m30 Still Here V1.xml", Path(d) / "3m30 Still Here V2.xml"
            old.write_text(FULL); replace_with_slim(old)
            new.write_text(FULL); replace_with_slim(new)
            os.utime(old, (time.time() - 100, time.time() - 100))
            names, src = cr.enabled_tracks(cpr)
            self.assertEqual(src, new)
            self.assertTrue(names)
            self.assertTrue(all(isinstance(v, int) for v in names.values()))
            (Path(d) / "notes.xml").write_text("<x/>")          # not a slim export: ignored
            self.assertEqual(cr.enabled_tracks(cpr)[1], new)


class OnlyEnabledTests(unittest.TestCase):
    def test_hidden_copy_on_stereo_out_is_not_the_enabled_track(self):
        template = {}
        project = {"SF Mandolin Grain": ["12m_SYNTH PADS HIGH DRY", None],   # visible → 12m, hidden copy → Stereo Out
                   "Serum Pulse": [None],                                    # really on Stereo Out
                   "Dup Pad": ["12l_SYNTH PADS LOW DRY", None],              # two enabled copies, one straight out
                   "2 HORNS": ["07a_STR MELODY DLY"]}                        # disabled divider
        issues = cr.project_issues(project, template)
        keep, skipped = cr.only_enabled(issues, {"SF Mandolin Grain": 1, "Serum Pulse": 1, "Dup Pad": 2})
        self.assertEqual([i["track"] for i in keep], ["Dup Pad", "Serum Pulse"])
        self.assertEqual(sorted(skipped), ["2 HORNS", "SF Mandolin Grain"])


class PlayingTracksTests(unittest.TestCase):
    def test_only_tracks_with_unmuted_notes(self):
        with tempfile.TemporaryDirectory() as d:
            xml = Path(d) / "cue.xml"
            xml.write_text(FULL, encoding="utf-8")
            # Kick&Snare: one muted part, and a muted note in the other part → plays nothing
            self.assertEqual(cr.playing_tracks(xml), [])
            replace_with_slim(xml)
            self.assertEqual(cr.playing_tracks(xml), [])
            xml.write_text(FULL.replace('<int name="Flags" value="2"/>\n                        </obj>', '</obj>'), encoding="utf-8")
            self.assertEqual(cr.playing_tracks(xml), ["Kick&Snare"])
            audio = '<obj class="MAudioTrackEvent" ID="90"><obj class="MListNode" name="Node" ID="91"><string name="Name" value="Ride"/>' \
                    '<list name="Events" type="obj"><obj class="MAudioEvent" ID="92"><float name="Start" value="0"/><float name="Length" value="960"/></obj></list></obj></obj>'
            xml.write_text(FULL.replace('      <obj class="MMarkerTrackEvent"', audio + '\n      <obj class="MMarkerTrackEvent"'), encoding="utf-8")
            self.assertEqual(cr.playing_tracks(xml), ["Ride (audio)"])


class TestResultWindowText(unittest.TestCase):
    ISSUES = [
        {"track": "Piano", "kind": "family", "groups": ["06_PIANO ALL"]},
        {"track": "Piano Main", "kind": "family", "groups": ["06_PIANO ALL"]},
        {"track": "VII Str Ens Wh Sustains Low", "kind": "none", "groups": [], "also": []},
        {"track": "Horn", "kind": "same", "groups": ["02a_BRASS DRY", "02b_BRASS DRY"]},
    ]

    def test_grouped_in_kinds_order_skipping_empty(self):
        g = cr.grouped(self.ISSUES)
        self.assertEqual([k for k, *_ in g], ["family", "none", "same"])
        self.assertEqual([i["track"] for i in g[0][3]], ["Piano", "Piano Main"])

    def test_target(self):
        self.assertEqual(cr.target(self.ISSUES[0]), "06_PIANO ALL")
        self.assertEqual(cr.target(self.ISSUES[2]), "Stereo Out")
        self.assertEqual(cr.target(self.ISSUES[3]), "02a_BRASS DRY / 02b_BRASS DRY")

    def test_as_text(self):
        t = cr.as_text("3m37 Help Us", self.ISSUES, "a note")
        self.assertTrue(t.startswith("3m37 Help Us: 4 routing issues"))
        self.assertIn("Into a family bus (2):", t)
        self.assertIn("  VII Str Ens Wh Sustains Low  →  Stereo Out", t)
        self.assertTrue(t.endswith("a note"))
        self.assertEqual(cr.as_text("X", []), "X: routing OK")


if __name__ == "__main__":
    unittest.main()
