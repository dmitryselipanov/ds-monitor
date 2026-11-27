import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tracks_with_data import as_text, detail, export_for, report, sections, summarize
from xml_slim import slim

TRACKS = [
    {"cls": "Instrument", "name": "SSS Vlns I Legato", "g": "07a_STR MELODY DRY",
     "parts": [{"s": 0, "l": 960, "o": 0, "m": 0, "n": [[0, 240, 60, 0], [240, 240, 62, 0]]}]},
    {"cls": "Instrument", "name": "Sketch Piano I", "parts": []},          # enabled, empty
    {"cls": "Instrument", "name": "Zebra", "g": "12k_SYNTH BASS DRY",
     "parts": [{"s": 0, "l": 960, "o": 0, "m": 2, "n": [[0, 10, 30, 0]]}]},
    {"cls": "Audio", "name": "Stereo In 1-2", "g": "15a_SAMPLING DRY",
     "parts": [{"s": 0, "l": 960, "o": 0, "m": 0, "a": 1, "n": []},
               {"s": 960, "l": 960, "o": 0, "m": 0, "a": 1, "n": []}]},
    {"cls": "Instrument", "name": "Zebra", "g": "12l_SYNTH PADS LOW DRY",
     "parts": [{"s": 0, "l": 960, "o": 0, "m": 0, "n": []}]},
]


def write_slim(path: Path, tracks=TRACKS):
    payload = json.dumps(tracks)
    path.write_text('<?xml version="1.0" encoding="utf-8"?>\n<tracklist2 ds_slim="1">\n'
                    f"<ds_tracks><![CDATA[{payload}]]></ds_tracks>\n</tracklist2>\n", encoding="utf-8")


class SummarizeTests(unittest.TestCase):
    def test_only_tracks_with_parts_in_order(self):
        rows = summarize(TRACKS)
        self.assertEqual([r["name"] for r in rows], ["SSS Vlns I Legato", "Zebra", "Stereo In 1-2", "Zebra"])

    def test_counts_and_flags(self):
        vln, zeb1, aud, zeb2 = summarize(TRACKS)
        self.assertEqual((vln["midi"], vln["notes"], vln["audio"], vln["muted"]), (1, 2, 0, False))
        self.assertTrue(zeb1["muted"])
        self.assertEqual((aud["audio"], aud["midi"]), (2, 0))
        self.assertEqual(zeb1["same_name"], 2)
        self.assertEqual(vln["same_name"], 1)

    def test_partly_muted_counts_only_unmuted_parts(self):
        t = [{"cls": "Instrument", "name": "Harp", "g": "05a_HARP DRY", "parts": [
            {"s": 0, "l": 1, "o": 0, "m": 2, "n": [[0, 1, 60, 0]] * 5},
            {"s": 1, "l": 1, "o": 0, "m": 0, "n": [[0, 1, 60, 0]] * 3}]}]
        (r,) = summarize(t)
        self.assertEqual((r["muted"], r["midi"], r["notes"]), (False, 1, 3))

    def test_families_and_sections(self):
        t = [{"name": "PRIM Flute", "g": "01b_WW HIGH LONGS DRY", "parts": [{"m": 0, "n": [[0, 1, 1, 0]]}]},
             {"name": "SSB 2 Horns Longs", "g": "02c_BRS MID LONGS DRY", "parts": [{"m": 0, "n": []}]},
             {"name": "Euphonium pad 1 Take 1", "parts": [{"m": 0, "a": 1, "n": []}]},
             {"name": "Dx", "parts": [{"m": 0, "a": 1, "n": []}]},
             {"name": "Prophet Bass", "g": "12k_SYNTH BASS DRY", "parts": [{"m": 0, "n": [[0, 1, 1, 0]]}]}]
        got = [(title, [r["name"] for r in rows]) for title, rows in sections(summarize(t))]
        self.assertEqual(got, [("Woodwinds", ["PRIM Flute"]),
                               ("Brass", ["SSB 2 Horns Longs", "Euphonium pad 1 Take 1"]),
                               ("Synthetics & Design", ["Prophet Bass"])])      # Dx left out

    def test_families_sharing_a_folder_share_a_heading(self):
        t = [{"name": n, "g": g, "parts": [{"m": 0, "n": [[0, 1, 1, 0]]}]} for n, g in
             [("SF Harp Main", "05a_HARP DRY"), ("SF Orchestral Grand", "06a_PIANO DRY"),
              ("Serum Pulse", "12q_SYNTH ARPS HIGH DRY"), ("HALion 7", "15a_SAMPLING DRY")]]
        self.assertEqual([(h, len(r)) for h, r in sections(summarize(t))],
                         [("Harp & Piano", 2), ("Synthetics & Design", 2)])

    def test_detail(self):
        vln, zeb1, aud, zeb2 = summarize(TRACKS)
        self.assertEqual(detail(vln), "2 notes")
        self.assertEqual(detail(aud), "2 audio regions")
        self.assertEqual(detail(zeb2), "1 empty part")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_export_named_like_cpr(self):
        cpr = self.dir / "2m29 The Gift V2.cpr"
        cpr.write_bytes(b"x")
        write_slim(self.dir / "2m29 The Gift V1.xml", TRACKS[:1])
        write_slim(self.dir / "2m29 The Gift V2.xml")
        old = time.time() - 100
        os.utime(self.dir / "2m29 The Gift V2.xml", (old, old))
        os.utime(cpr, (old, old))
        rep = report(cpr)
        self.assertEqual(rep["export"].name, "2m29 The Gift V2.xml")
        self.assertEqual([r["name"] for r in rep["rows"]], ["SSS Vlns I Legato", "Stereo In 1-2", "Zebra"])
        self.assertEqual([r["name"] for r in rep["muted"]], ["Zebra"])
        self.assertEqual(rep["total"], 5)
        text = as_text(rep)
        self.assertIn("STRINGS", text)
        self.assertIn("SYNTHETICS & DESIGN", text)
        self.assertTrue(text.rstrip().endswith("Zebra  (1 note)"))
        self.assertFalse(rep["stale"])
        self.assertIn("More than one track named Zebra", rep["note"])

    def test_falls_back_to_newest_slim_and_says_so(self):
        cpr = self.dir / "1m02 City Pulse V3.cpr"
        cpr.write_bytes(b"x")
        (self.dir / "not slim.xml").write_text("<tracklist2></tracklist2>")
        write_slim(self.dir / "1m02 City Pulse V2.xml")
        rep = report(cpr)
        self.assertEqual(rep["export"].name, "1m02 City Pulse V2.xml")
        self.assertIn("Read from 1m02 City Pulse V2.xml", rep["note"])

    def test_stale_when_project_saved_after_export(self):
        cpr = self.dir / "a.cpr"
        write_slim(self.dir / "a.xml")
        cpr.write_bytes(b"x")
        old = time.time() - 3600
        os.utime(self.dir / "a.xml", (old, old))
        rep = report(cpr)
        self.assertTrue(rep["stale"])
        self.assertIn("Saved after this export", rep["note"])

    def test_no_export(self):
        cpr = self.dir / "a.cpr"
        cpr.write_bytes(b"x")
        rep = report(cpr)
        self.assertIsNone(rep["export"])
        self.assertEqual(rep["rows"], [])
        self.assertIn("No XML export", rep["note"])
        self.assertIn("No XML export", as_text(rep))

    def test_full_and_marker_only_exports_are_read(self):
        cpr = self.dir / "1m02 City Pulse V1.1.cpr"
        cpr.write_bytes(b"x")
        (self.dir / "1m02 City Pulse V1.1.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<tracklist2>\n<list name="track" type="obj">\n'
            '<obj class="MMarkerTrackEvent" ID="1"><obj class="MListNode" name="Node" ID="2">'
            '<string name="Name" value="Cue"/></obj></obj>\n</list>\n</tracklist2>\n', encoding="utf-8")
        rep = report(cpr)
        self.assertEqual(rep["export"].name, "1m02 City Pulse V1.1.xml")
        self.assertEqual(rep["rows"], [])
        self.assertIn("holds no tracks", rep["note"])
        self.assertTrue(rep["export"].read_text().startswith("<?xml"))     # left untouched

    def test_subtitle_xml_is_not_an_export(self):
        cpr = self.dir / "a.cpr"
        cpr.write_bytes(b"x")
        (self.dir / "a.xml").write_text("<tt><body/></tt>")
        self.assertIsNone(report(cpr)["export"])

    def test_missing_groups_looked_up_in_the_cpr(self):
        from unittest import mock
        cpr = self.dir / "3m37 Help Us.cpr"
        cpr.write_bytes(b"x")
        write_slim(self.dir / "3m37 Help Us.xml", [
            {"name": "PRIM Sax 1", "g": "01b_WW HIGH LONGS DRY", "parts": [{"m": 0, "n": [[0, 1, 1, 0]]}]},
            {"name": "Euphonium pad 1 Take 1", "parts": [{"m": 0, "a": 1, "n": []}]}])
        old = time.time() - 100
        os.utime(cpr, (old, old))
        with mock.patch("cpr_routing.output_groups", return_value={"Euphonium pad 1 Take 1": "02c_BRS MID LONGS DRY"}):
            rep = report(cpr)
        self.assertEqual([(h, [r["name"] for r in rows]) for h, rows in sections(rep["rows"])],
                         [("Woodwinds", ["PRIM Sax 1"]), ("Brass", ["Euphonium pad 1 Take 1"])])

    def test_xml_picked_directly(self):
        x = self.dir / "a.xml"
        write_slim(x)
        self.assertEqual(export_for(x), x)
        self.assertEqual(len(report(x)["rows"]), 3)

    def test_reads_what_xml_slim_writes(self):
        full = self.dir / "full.xml"
        full.write_text("""<?xml version="1.0" encoding="utf-8"?>
<tracklist2>
   <list name="track" type="obj">
      <obj class="MInstrumentTrackEvent" ID="1">
         <obj class="MListNode" name="Node" ID="2">
            <string name="Name" value="Kick]]&gt;Snare" wide="true"/>
            <list name="Events" type="obj">
               <obj class="MMidiPartEvent" ID="3">
                  <float name="Start" value="0"/>
                  <float name="Length" value="480"/>
                  <obj class="MMidiPart" name="Node" ID="4">
                     <list name="Events" type="obj">
                        <obj class="MMidiNote" ID="5">
                           <float name="Start" value="0"/>
                           <int name="Data1" value="60"/>
                           <float name="Length" value="240"/>
                        </obj>
                     </list>
                  </obj>
               </obj>
            </list>
         </obj>
      </obj>
      <obj class="MInstrumentTrackEvent" ID="6">
         <obj class="MListNode" name="Node" ID="7">
            <string name="Name" value="Empty Instr" wide="true"/>
         </obj>
      </obj>
   </list>
</tracklist2>
""", encoding="utf-8")
        text, _ = slim(full, {"Kick]]>Snare": "03a_BASS DRUMS DRY"})
        out = self.dir / "slim.xml"
        out.write_text(text, encoding="utf-8")
        rows = report(out)["rows"]
        self.assertEqual([(r["name"], r["group"], r["notes"]) for r in rows],
                         [("Kick]]>Snare", "03a_BASS DRUMS DRY", 1)])


if __name__ == "__main__":
    unittest.main()
