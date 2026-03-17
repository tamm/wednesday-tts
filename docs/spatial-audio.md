# Spatial Audio — Feature Plan

Positional stereo panning based on terminal window location on screen.

## Status: v1 complete, v3 implemented

## v1 — Stereo panning (speakers)

Terminal windows map to a left-right pan position. A window on the far left of your leftmost monitor pans the voice left; far right pans right; centre is centre. Active when the default output is speakers (non-Bluetooth).

### How it works

1. **Hook** (speak-response.py / pre-tool-speak.py):
   - Reads `ITERM_SESSION_ID` from env to identify the window
   - Runs AppleScript to get that window's bounds from iTerm
   - Runs JXA to get all screen bounds (NSScreen.screens)
   - Computes normalised pan (0.0 = full left, 1.0 = full right) from window centre relative to total screen span
   - Sends pan value to daemon as a new protocol field

2. **Daemon** (daemon.py):
   - Stores current pan value per-request (module-level var, set before queueing)
   - `playback_worker` opens `channels=2` instead of `channels=1`
   - Applies equal-power (constant power) pan law when writing each chunk:
     ```
     left  = audio * cos(pan * pi/2)
     right = audio * sin(pan * pi/2)
     ```
   - Stacks into shape `(N, 2)` for stereo output

3. **Protocol extension**:
   - New command prefix: `PAN:value` as an optional field
   - Wire format: `SEQ:N:speed:ct:ts:pan:text` (pan is float 0.0-1.0, or empty for centre)

### Panning math

Equal-power pan law (constant power, industry standard):
- At pan=0.5 (centre): left=cos(45deg)=0.707, right=sin(45deg)=0.707 — both channels equal, total power preserved
- At pan=0.0 (full left): left=1.0, right=0.0
- At pan=1.0 (full right): left=0.0, right=1.0

No volume dip at centre, no clipping at extremes.

### Position detection

- Screen bounds from JXA: `ObjC.import("AppKit"); $.NSScreen.screens` gives origin+size per display
- Window bounds from AppleScript: `tell application "iTerm2" to get bounds of window` matched by session UUID
- Physical width estimated from logical points: `logical_w * 0.22 mm/pt`
- Viewing angle: `atan2(dx_mm, viewing_distance_mm)`
- Pan mapped from angle: `0.5 + (angle / max_angle) * 0.5`

See [spatial-audio-formula.md](spatial-audio-formula.md) for the full formula spec, reference tables, and config options.

## v2 — HRTF binaural (skipped)

Skipped in favour of v3 which gives real head tracking via Apple's spatial audio.

## v3 — Apple spatial audio with head tracking (implemented)

When Bluetooth headphones are the default output, the daemon routes audio through a Swift helper subprocess (`SpatialStream`) that uses AVAudioEngine with AVAudioEnvironmentNode. This goes through macOS's spatial audio pipeline, enabling head tracking on supported headphones (Beats, AirPods).

### How it works

1. **Detection**: daemon detects BT headphones by checking the default output device name against a skip list of known non-BT devices, then looks up a MAC-based UID from the CoreAudio device settings plist.

2. **SpatialStream** (`integrations/spatial-audio/SpatialStream.swift`): compiled Swift binary that reads raw float32 mono PCM on stdin and plays via AVAudioEngine. Audio goes through:
   - `AVAudioPlayerNode` → `AVAudioEnvironmentNode` → `mainMixerNode` → output
   - Environment node has `isListenerHeadTrackingEnabled = true` and `outputType = .headphones`
   - Player node uses `.spatializeIfMono` source mode

3. **Real-time pan updates**: the daemon sends inline `PAN!` + float32 commands on stdin to update the player's 3D position mid-playback. No subprocess respawn needed — AVAudioPlayerNode.position updates instantly.

4. **Fallback**: when BT headphones disconnect, the daemon detects the device change and falls back to the v1 PortAudio stereo panning path automatically.

### Stdin protocol

SpatialStream reads a mixed stream on stdin:
- **Audio**: raw float32 mono PCM samples
- **Pan update**: 4-byte magic `PAN!` followed by 4-byte float32 (pan value 0.0–1.0)
- **EOF**: drain remaining audio and exit

### Pan to 3D position mapping

Pan (0.0–1.0) maps to x position (-1.0 to 1.0) in 3D space:
```
x = (pan - 0.5) * 2.0
position = (x, 0, -1)  // negative z = in front of listener
```

### Requirements

- macOS with Bluetooth headphones that support spatial audio (Beats, AirPods)
- Head tracking requires the BT headphones to negotiate a motion data channel
- Safari or another "spatial-aware" app may need to be playing to keep the BT motion channel pinned open (observed on Powerbeats Pro 2 — may not apply to all devices)

### Files

- `integrations/spatial-audio/SpatialStream.swift` — streaming player (used by daemon)
- `integrations/spatial-audio/SpatialPlayer.swift` — file-based player (standalone testing)

## v4 — Live window tracking (future)

Currently, pan position is captured once per request (when the hook fires). The voice stays at that position for the entire response. A future enhancement would continuously track the terminal window position and update the pan in real time during playback.

### Approach

The daemon could poll the window position periodically (e.g. every 500ms) in a background thread and send updated pan values to both:
- The PortAudio path (update `_current_pan` so the next chunk uses the new value)
- The SpatialStream subprocess (send `PAN!` + float32 inline)

This would make the voice "follow" the terminal window as you drag it around, or shift smoothly when switching between terminal windows on different monitors.
