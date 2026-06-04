// DS//Scoring Track Bridge — Cubase MIDI Remote API Script
// Watches track selection, sends track name via IAC Driver → DS Monitor → DS//Scoring
//
// INSTALL:
//   1. Enable IAC Driver in Audio MIDI Setup, add a bus named "DS Bridge"
//   2. Copy this file to:
//      ~/Documents/Steinberg/Cubase/MIDI Remote/Driver Scripts/Local/DSScoring/bridge/DSScoring_bridge.js
//   3. Restart Cubase — script auto-loads, no controller needed
//
// Protocol: SysEx F0 7D [ASCII bytes of track name, max 64 chars] F7

var midiremote_api = require('midiremote_api_v1');
var driver = midiremote_api.makeDeviceDriver('DSScoring', 'TrackBridge', 'Dmitry Selipanov');

// ── Ports ──────────────────────────────────────────────────────────────────
var midiOutput = driver.mPorts.makeMidiOutput('DS Bridge');

driver.makeDetectionUnit()
    .detectPortPair(
        driver.mPorts.makeMidiInput('DS Bridge'),
        midiOutput
    )
    .expectOutputNameEndsWith('DS Bridge');

// ── Surface (minimal — API requires at least one surface element) ──────────
var surface = driver.mSurface;
var knob = surface.makeKnob(0, 0, 1, 1);

// ── Mapping page ───────────────────────────────────────────────────────────
var page = driver.mMapping.makePage('DS Bridge');

// Bind knob to volume so the page is valid (not used functionally)
page.makeValueBinding(
    knob.mSurfaceValue,
    page.mHostAccess.mTrackSelection.mMixerChannel.mValue.mVolume
);

// ── Track selection callback ───────────────────────────────────────────────
page.mHostAccess.mTrackSelection.mMixerChannel.mValue.mOnTitleChange =
    function(activeDevice, objectTitle, valueTitle) {
        // objectTitle = track name, valueTitle = parameter name ("Volume")
        sendTrackName(activeDevice, objectTitle || '');
    };

function sendTrackName(activeDevice, name) {
    var bytes = [0xF0, 0x7D]; // SysEx start + manufacturer ID 7D (non-commercial)
    for (var i = 0; i < name.length && i < 64; i++) {
        bytes.push(name.charCodeAt(i) & 0x7F); // 7-bit safe
    }
    bytes.push(0xF7); // SysEx end
    midiOutput.sendMidi(activeDevice, bytes);
}
