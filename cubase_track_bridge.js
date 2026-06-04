// DS//Scoring Track Bridge — Cubase MIDI Remote API Script
// Minimal script: no port detection, no surface binding conflicts.
// Uses a CustomValueVariable to catch track title changes.
//
// INSTALL:
//   mkdir -p ~/Documents/Steinberg/Cubase/MIDI\ Remote/Driver\ Scripts/Local/DSScoring_bridge
//   cp cubase_track_bridge.js .../DSScoring_bridge/DSScoring_bridge.js
// Then in Cubase: Studio → MIDI Remote Manager → Scripts tab → refresh (↻)
// Script loads silently — no controller needed, no port binding.

var midiremote_api = require('midiremote_api_v1');
var driver = midiremote_api.makeDeviceDriver('DSScoring', 'TrackBridge', 'Dmitry Selipanov');

// ── Declare a virtual MIDI output only — no input, no detection ────────────
var midiOutput = driver.mPorts.makeMidiOutput('DS Bridge');

// No detectPortPair — this prevents conflicts with existing MIDI Remote scripts.
// Cubase will not try to match this script to any hardware device.

// ── Minimal surface ────────────────────────────────────────────────────────
var surface = driver.mSurface;
var trackNameVar = surface.makeCustomValueVariable('trackName');

// ── Mapping page ───────────────────────────────────────────────────────────
var page = driver.mMapping.makePage('DSBridge');
var trackSel = page.mHostAccess.mTrackSelection;

// Bind the custom variable to the selected mixer channel volume
// (required to keep the page active — we use volume as an anchor)
page.makeValueBinding(
    trackNameVar,
    trackSel.mMixerChannel.mValue.mVolume
);

// ── Track name callback ────────────────────────────────────────────────────
trackNameVar.mOnTitleChange = function(activeDevice, objectTitle, valueTitle) {
    // objectTitle = track name when selected channel changes
    if (objectTitle && objectTitle !== valueTitle) {
        sendTrackName(activeDevice, objectTitle);
    }
};

// Also hook directly on mixer channel title change
trackSel.mMixerChannel.mValue.mOnTitleChange = function(activeDevice, objectTitle, valueTitle) {
    sendTrackName(activeDevice, objectTitle || '');
};

function sendTrackName(activeDevice, name) {
    if (!name || !name.trim()) return;
    // SysEx: F0 7D [ASCII bytes, max 64] F7
    var bytes = [0xF0, 0x7D];
    var clean = name.trim();
    for (var i = 0; i < clean.length && i < 64; i++) {
        bytes.push(clean.charCodeAt(i) & 0x7F);
    }
    bytes.push(0xF7);
    midiOutput.sendMidi(activeDevice, bytes);
}
