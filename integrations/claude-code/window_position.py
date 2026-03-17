#!/usr/bin/env python3
"""Detect terminal window position and compute stereo pan value.

Uses AppleScript/JXA to query iTerm2 window bounds and screen layout,
then maps the window's horizontal centre to a stereo pan position.

The pan range is compressed based on the physical angular span of
your monitors. Screens in front of you shouldn't produce full
left/right separation — that would sound like the voice is beside
you rather than at the monitor.

Config (in ~/.claude/tts-config.json under "spatial"):
    width    — fraction of stereo field the screens span (0.0-1.0).
               0.5 means screens cover ~90 degrees. Default 0.5.
    centre_x — optional screen X coordinate for perceptual centre.
               Defaults to centre of the main display (origin 0,0).

Returns 0.5 (centre) on any failure — silent fallback, never crashes.
"""
import json
import os
import subprocess


def _get_screen_bounds() -> list[tuple[float, float, float, float]]:
    """Return [(x, y, w, h), ...] for all connected displays via JXA."""
    script = (
        'ObjC.import("AppKit");'
        "var ss=$.NSScreen.screens;"
        "var r=[];"
        "for(var i=0;i<ss.count;i++){"
        "var f=ss.objectAtIndex(i).frame;"
        "r.push(f.origin.x+','+f.origin.y+','+f.size.width+','+f.size.height);"
        '} r.join("\\n");'
    )
    try:
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return []
    result = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) == 4:
            try:
                result.append(tuple(float(p) for p in parts))
            except ValueError:
                continue
    return result


def _get_window_bounds_by_session(session_uuid: str) -> tuple[int, int, int, int] | None:
    """Return (x1, y1, x2, y2) bounds for the iTerm window containing session_uuid."""
    script = f'''
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if unique ID of s contains "{session_uuid}" then
                    set b to bounds of w
                    return "" & item 1 of b & "," & item 2 of b & "," & item 3 of b & "," & item 4 of b
                end if
            end repeat
        end repeat
    end repeat
    return "not found"
end tell
'''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return None
    if out == "not found" or not out:
        return None
    parts = out.split(",")
    if len(parts) != 4:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _load_spatial_config() -> dict:
    """Load spatial config from tts-config.json. Returns defaults on failure."""
    cfg_path = os.path.expanduser("~/.claude/tts-config.json")
    try:
        with open(cfg_path) as f:
            return json.load(f).get("spatial", {})
    except Exception:
        return {}


def compute_pan() -> float:
    """Compute stereo pan position for the current terminal window.

    Returns 0.0 (full left) to 1.0 (full right), defaulting to 0.5 on failure.
    Uses ITERM_SESSION_ID env var to identify the window.

    The raw screen position is compressed into a narrower stereo range
    based on config, so screens in front of you sound in front, not
    hard-panned to the sides.
    """
    # Extract session UUID from ITERM_SESSION_ID (format: w0t0p0:UUID)
    iterm_sid = os.environ.get("ITERM_SESSION_ID", "")
    if ":" not in iterm_sid:
        return 0.5
    session_uuid = iterm_sid.split(":", 1)[1]

    # Get window bounds
    bounds = _get_window_bounds_by_session(session_uuid)
    if bounds is None:
        return 0.5
    x1, _y1, x2, _y2 = bounds
    window_centre_x = (x1 + x2) / 2.0

    # Get screen layout
    screens = _get_screen_bounds()
    if not screens:
        return 0.5

    # Compute global horizontal span across all screens
    global_left = min(s[0] for s in screens)
    global_right = max(s[0] + s[2] for s in screens)
    span = global_right - global_left
    if span <= 0:
        return 0.5

    # Load spatial config
    spatial = _load_spatial_config()
    width = max(0.05, min(1.0, float(spatial.get("width", 0.5))))

    # Determine perceptual centre — default to centre of main display (origin 0,0)
    if "centre_x" in spatial:
        centre_x = float(spatial["centre_x"])
    else:
        # Main display is the one at origin (0,0) — find it
        main = next((s for s in screens if s[0] == 0 and s[1] == 0), None)
        if main:
            centre_x = main[2] / 2.0  # half the main display width
        else:
            centre_x = (global_left + global_right) / 2.0

    # Map window position to [-1, 1] relative to perceptual centre
    # then compress by width and shift to [0, 1]
    offset = (window_centre_x - centre_x) / (span / 2.0)  # -1 to 1 (ish)
    pan = 0.5 + offset * (width / 2.0)
    return max(0.0, min(1.0, pan))


if __name__ == "__main__":
    pan = compute_pan()
    print(f"pan={pan:.3f}")
