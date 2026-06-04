// DS//Scoring Track Bridge — Cubase MIDI Remote API Script
// Watches selected track and sends name via IAC Driver DS Bridge to DS Monitor.

var midiremote_api = require('midiremote_api_v1');
var driver = midiremote_api.makeDeviceDriver('DSScoring', 'TrackBridge', 'Dmitry Selipanov');

// Ports — no detection unit, passive script only
var midiInput  = driver.mPorts.makeMidiInput('DS Bridge In');
var midiOutput = driver.mPorts.makeMidiOutput('DS Bridge Out');

// Minimal surface
var surface = driver.mSurface;
var dummyKnob = surface.makeKnob(0, 0, 1, 1);

// Mapping page
var page = driver.mMapping.makePage('DSBridge');
var trackSel = page.mHostAccess.mTrackSelection;

// Bind knob to volume so page stays active
page.makeValueBinding(dummyKnob.mSurfaceValue, trackSel.mMixerChannel.mValue.mVolume);

// Track name callback
trackSel.mMixerChannel.mValue.mOnTitleChange = function(activeDevice, objectTitle, valueTitle) {
    var name = String(objectTitle || '');
    if (!name) return;
    sendSysEx(activeDevice, name);
};

function sendSysEx(activeDevice, name) {
    // SysEx: F0 7D [7-bit ASCII bytes, max 64] F7
    var bytes = [0xF0, 0x7D];
    for (var i = 0; i < name.length && i < 64; i++) {
        bytes.push(name.charCodeAt(i) & 0x7F);
    }
    bytes.push(0xF7);
    midiOutput.sendMidi(activeDevice, bytes);
}
