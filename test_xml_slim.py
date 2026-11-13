import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from xml_slim import SlimError, is_slim, replace_with_slim, slim, stats_of_slim

# Shaped like a real Cubase "Export > Selected Tracks" file: indented over many lines,
# self-closing value tags, the project tempo track nested inside the first track's
# Domain, an audio track with a tempo map of its own, and a marker track.
FULL = """<?xml version="1.0" encoding="utf-8"?>
<tracklist2>
   <list name="track" type="obj">
      <obj class="MInstrumentTrackEvent" ID="1">
         <int name="Flags" value="1"/>
         <obj class="MListNode" name="Node" ID="2">
            <string name="Name" value="Kick&amp;Snare" wide="true"/>
            <member name="Domain">
               <obj class="MTempoTrackEvent" name="Tempo Track" ID="100">
                  <list name="TempoEvent" type="obj">
                     <obj class="MTempoEvent" ID="101">
                        <float name="BPM" value="80"/>
                        <float name="PPQ" value="0"/>
                     </obj>
                     <obj class="MTempoEvent" ID="102">
                        <float name="BPM" value="60"/>
                        <float name="PPQ" value="7680"/>
                     </obj>
                  </list>
               </obj>
            </member>
            <list name="Events" type="obj">
               <obj class="MMidiPartEvent" ID="3">
                  <int name="Flags" value="2"/>
                  <float name="Start" value="960"/>
                  <float name="Length" value="480"/>
                  <obj class="MMidiPart" name="Node" ID="4">
                     <list name="Events" type="obj">
                        <obj class="MMidiController" ID="40">
                           <float name="Start" value="5"/>
                           <int name="Data1" value="1"/>
                        </obj>
                        <obj class="MMidiNote" ID="5">
                           <float name="Start" value="0"/>
                           <int name="Data1" value="60"/>
                           <float name="Length" value="240.5"/>
                        </obj>
                     </list>
                  </obj>
               </obj>
               <obj class="MMidiPartEvent" ID="6">
                  <float name="Start" value="1920"/>
                  <float name="Length" value="960"/>
                  <float name="Offset" value="-120"/>
                  <obj class="MMidiPart" name="Node" ID="7">
                     <list name="Events" type="obj">
                        <obj class="MMidiNote" ID="8">
                           <float name="Start" value="120"/>
                           <int name="Data1" value="62"/>
                           <float name="Length" value="480"/>
                           <int name="Flags" value="2"/>
                        </obj>
                     </list>
                  </obj>
               </obj>
            </list>
         </obj>
         <bin name="audioComponent">{blob}</bin>
      </obj>
      <obj class="MInstrumentTrackEvent" ID="9">
         <obj class="MListNode" name="Node" ID="10">
            <string name="Name" value="Vln I"/>
            <member name="Domain"><obj name="Tempo Track" ID="100"/></member>
         </obj>
      </obj>
      <obj class="MAudioTrackEvent" ID="11">
         <obj class="MListNode" name="Node" ID="12">
            <string name="Name" value="Maya demo"/>
            <member name="Domain">
               <obj class="MTempoTrackEvent" name="Tempo Track" ID="200">
                  <list name="TempoEvent" type="obj">
                     <obj class="MTempoEvent" ID="201">
                        <float name="BPM" value="120"/>
                        <float name="PPQ" value="0"/>
                     </obj>
                  </list>
               </obj>
            </member>
         </obj>
      </obj>
      <obj class="MMarkerTrackEvent" ID="13">
         <obj class="MListNode" name="Node" ID="14">
            <string name="Name" value="Cue"/>
            <member name="Domain"><obj name="Tempo Track" ID="100"/></member>
            <list name="Events" type="obj">
               <obj class="MRangeMarkerEvent" ID="15">
                  <float name="Start" value="0"/>
                  <float name="Length" value="149760"/>
                  <member name="Additional Attributes">
                     <int name="Farb" value="230"/>
                  </member>
                  <string name="Name" value="2m29 The Gift 02.48.01.19" wide="true"/>
               </obj>
               <obj class="MMarkerEvent" ID="16">
                  <float name="Start" value="12"/>
                  <string name="Name" value="START"/>
               </obj>
            </list>
         </obj>
      </obj>
   </list>
</tracklist2>
""".replace("{blob}", "A" * 50000)


class SlimTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "2m29 The Gift V2.xml"
        self.path.write_text(FULL, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def slim_doc(self):
        text, stats = slim(self.path)
        return ET.fromstring(text), stats

    def test_keeps_tracks_parts_notes_and_mute_flags(self):
        root, stats = self.slim_doc()
        tracks = json.loads(root.find("ds_tracks").text)
        self.assertEqual([t["name"] for t in tracks], ["Kick&Snare", "Vln I"])
        kick = tracks[0]
        self.assertEqual(kick["parts"], [
            {"s": 960, "l": 480, "o": 0, "m": 2, "n": [[0, 240.5, 60, 0]]},
            {"s": 1920, "l": 960, "o": -120, "m": 0, "n": [[120, 480, 62, 2]]},
        ])
        self.assertEqual(stats["notes"], 2)
        self.assertEqual(stats["muted_parts"], 1)

    def test_audio_tracks_without_regions_are_left_out(self):
        root, stats = self.slim_doc()
        names = [t["name"] for t in json.loads(root.find("ds_tracks").text)]
        self.assertNotIn("Maya demo", names)
        self.assertEqual(stats["tracks"], 2)

    def test_audio_regions_are_kept_with_mute_flags(self):
        audio = """
      <obj class="MAudioTrackEvent" ID="20">
         <obj class="MListNode" name="Node" ID="21">
            <string name="Name" value="Kick&amp;Snare Loop"/>
            <list name="Events" type="obj">
               <obj class="MAudioEvent" ID="22">
                  <float name="Start" value="1920"/>
                  <float name="Length" value="960"/>
                  <obj class="MFadeIn" name="Fade In" ID="23"><float name="Start" value="0"/><float name="Length" value="10"/><int name="Flags" value="2"/></obj>
               </obj>
               <obj class="MAudioEvent" ID="24">
                  <int name="Flags" value="2"/>
                  <float name="Start" value="4800"/>
                  <float name="Length" value="480"/>
               </obj>
               <obj class="MAudioPartEvent" ID="25">
                  <float name="Start" value="9600"/>
                  <float name="Length" value="1920"/>
                  <obj class="MAudioPart" name="Node" ID="26">
                     <list name="Events" type="obj">
                        <obj class="MAudioEvent" ID="27"><float name="Start" value="0"/><float name="Length" value="480"/></obj>
                        <obj class="MAudioEvent" ID="28"><float name="Start" value="960"/><float name="Length" value="480"/></obj>
                     </list>
                  </obj>
               </obj>
            </list>
         </obj>
      </obj>"""
        self.path.write_text(FULL.replace('      <obj class="MMarkerTrackEvent"', audio + '\n      <obj class="MMarkerTrackEvent"'), encoding="utf-8")
        root, stats = self.slim_doc()
        loop = next(t for t in json.loads(root.find("ds_tracks").text) if t["name"] == "Kick&Snare Loop")
        self.assertEqual(loop["cls"], "Audio")
        self.assertEqual([(p["s"], p["l"], p["m"], p.get("a")) for p in loop["parts"]],
                         [(1920, 960, 0, 1), (4800, 480, 2, 1), (9600, 1920, 0, 1)])
        self.assertEqual(stats["audio_regions"], 3)

    def test_project_tempo_map_is_kept_and_audio_tempo_set_aside(self):
        root, _ = self.slim_doc()
        bpm = [o.find("float[@name='BPM']").get("value") for o in root.findall("obj[@class='MTempoEvent']")]
        self.assertEqual(bpm, ["80", "60"])
        aside = root.findall("obj[@class='xMTempoEvent']")
        self.assertEqual([o.find("float[@name='BPM']").get("value") for o in aside], ["120"])

    def test_cue_marker_copied_whole_and_labelled_with_its_track(self):
        root, stats = self.slim_doc()
        (cue,) = root.findall("obj[@class='MRangeMarkerEvent']")
        self.assertEqual(cue.get("ds_track"), "Cue")
        self.assertEqual(cue.find("string[@name='Name']").get("value"), "2m29 The Gift 02.48.01.19")
        self.assertEqual(cue.find("float[@name='Length']").get("value"), "149760")
        self.assertEqual(stats["range_markers"], 1)
        self.assertEqual(root.find("obj[@class='MMarkerEvent']").get("ds_track"), "Cue")

    def test_drops_plugin_state_and_controllers(self):
        text, _ = slim(self.path)
        self.assertNotIn("A" * 300, text)
        self.assertNotIn("MMidiController", text)
        self.assertLess(len(text), len(FULL) / 10)

    def test_stats_read_back_match(self):
        text, stats = slim(self.path)
        out = self.path.with_name("check.xml")
        out.write_text(text, encoding="utf-8")
        self.assertEqual(stats_of_slim(out), stats)

    # ── replacing the original ───────────────────────────────────────────

    def test_replaces_original_under_same_name(self):
        stats = replace_with_slim(self.path)
        self.assertTrue(is_slim(self.path))
        self.assertEqual(stats["original_bytes"], len(FULL.encode()))
        self.assertLess(stats["slim_bytes"], stats["original_bytes"])
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["2m29 The Gift V2.xml"])

    def test_already_slim_is_left_alone(self):
        replace_with_slim(self.path)
        before = self.path.read_bytes()
        self.assertIsNone(replace_with_slim(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def assert_untouched(self, original):
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [self.path.name])

    def test_cut_off_file_is_never_deleted(self):
        cut = FULL.encode()[: len(FULL) // 2]
        self.path.write_bytes(cut)
        with self.assertRaisesRegex(SlimError, "incomplete"):
            replace_with_slim(self.path)
        self.assert_untouched(cut)

    def test_file_missing_only_its_last_tag_is_never_deleted(self):
        cut = FULL.rstrip().removesuffix("</tracklist2>").encode()
        self.path.write_bytes(cut)
        with self.assertRaisesRegex(SlimError, "incomplete"):
            replace_with_slim(self.path)
        self.assert_untouched(cut)

    def test_other_xml_is_left_alone(self):
        other = b'<?xml version="1.0"?>\n<score-partwise><part id="P1"/></score-partwise>\n'
        self.path.write_bytes(other)
        with self.assertRaisesRegex(SlimError, "not a Cubase track export"):
            replace_with_slim(self.path)
        self.assert_untouched(other)

    def test_open_file_is_not_touched(self):
        original = self.path.read_bytes()
        with self.assertRaisesRegex(SlimError, "still open"):
            replace_with_slim(self.path, is_open=lambda p: True)
        self.assert_untouched(original)

    def test_file_changed_during_conversion_is_not_deleted(self):
        import xml_slim
        real = xml_slim.slim

        def slim_then_cubase_writes_again(path, groups=None):
            result = real(path, groups)
            with open(path, "a") as f:
                f.write("\n")
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        xml_slim.slim = slim_then_cubase_writes_again
        try:
            with self.assertRaisesRegex(SlimError, "changed while"):
                replace_with_slim(self.path)
        finally:
            xml_slim.slim = real
        self.assertFalse(is_slim(self.path))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [self.path.name])

    def test_markers_only_export_still_imports(self):
        markers_only = """<?xml version="1.0" encoding="utf-8"?>
<tracklist2>
   <list name="track" type="obj">
      <obj class="MMarkerTrackEvent" ID="1">
         <obj class="MListNode" name="Node" ID="2">
            <string name="Name" value="Cue"/>
            <list name="Events" type="obj">
               <obj class="MRangeMarkerEvent" ID="3">
                  <float name="Start" value="0"/>
                  <float name="Length" value="9600"/>
                  <string name="Name" value="1m01 Opening V1 01.00.00.00"/>
               </obj>
            </list>
         </obj>
      </obj>
   </list>
</tracklist2>
"""
        self.path.write_text(markers_only, encoding="utf-8")
        stats = replace_with_slim(self.path)
        self.assertEqual((stats["range_markers"], stats["tracks"], stats["notes"]), (1, 0, 0))
        root = ET.parse(self.path).getroot()
        self.assertEqual(root.find("obj[@class='MRangeMarkerEvent']/string[@name='Name']").get("value"),
                         "1m01 Opening V1 01.00.00.00")


if __name__ == "__main__":
    unittest.main()
