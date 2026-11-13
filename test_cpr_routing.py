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
       + bus(10962, 20, "Propheeet") + bus(10980, 17, "05a_HARP DRY") + bus(300, 1, "Stereo Out")
       + track("Propheeet", 10962) + track("SF AS Vlns I Legato", 10970) + track("Sketch Piano I", 300)
       + track("New Track Without Preset", 10962) + track("No Output", None)
       + track("SF AS Vlns I Legato", 10962)   # stale second record: the first one wins
       + track("SF Harp Main", 10980)
       + track("12l_SYNTH PADS LOW DRY", 10970)   # a group channel's own record: not a track
       + channel("Noise Beat", 10990) + bus(10990, 14, "12f_SYNTH PERC MID SUM")
       + channel("Propheeet", 10970)              # the RecorderBus record already placed it: kept
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
        tracks = json.loads(re.search(r"CDATA\[(.*)\]\]>", xml.read_text(), re.S).group(1))
        self.assertEqual({t["name"]: t.get("g") for t in tracks}, {"Kick&Snare": "03d_SNARES DRY", "Vln I": None})


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


if __name__ == "__main__":
    unittest.main()
