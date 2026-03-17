# Spatial Audio — Feature Plan

Positional stereo panning based on terminal window location on screen.

## Status: v1 in progress

## v1 — Stereo panning (current)

Terminal windows map to a left-right pan position. A window on the far left of your leftmost monitor pans the voice left; far right pans right; centre is centre.

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
- Pan = (window_centre_x - global_left) / (global_right - global_left)
- Clamped to [0.0, 1.0]

## v2 — HRTF binaural (future, not yet)

Convolve mono audio with head-related impulse responses (HRIRs) from the CIPIC database using scipy. This would give real positional depth even without Apple spatial audio enabled.

- Use scipy.signal.fftconvolve with CIPIC HRIR data
- Map pan position to azimuth angle for HRIR lookup
- Would give front/side/behind positioning, not just left/right

### Not using spaudiopy because:
- Designed for Ambisonics (spherical harmonics, loudspeaker arrays) — way too heavy
- Pulls in matplotlib, h5py, resampy, joblib on top of numpy/scipy
- Offline rendering model doesn't suit our streaming 100ms chunks
- Raw HRIR convolution with scipy achieves the same result with zero extra deps

## v3 — Apple spatial audio (speculative)

AVAudioEngine via pyobjc could theoretically feed into Apple's spatial audio pipeline with AirPods head tracking. Unclear if this is practical — the rendering happens at the AVFoundation layer and may fight with PortAudio.

### Not doing this yet because:
- No Python API exists for programmatic spatial audio positioning
- Would require replacing sounddevice with AVAudioEngine (pyobjc)
- Head tracking integration is undocumented for non-app contexts
