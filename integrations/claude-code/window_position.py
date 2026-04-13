#!/usr/bin/env python3
"""Detect terminal window position and compute stereo pan value.

Uses JXA to query NSScreen bounds and AppleScript for iTerm2 window bounds.
Estimates physical screen width from logical points (mm_per_point constant),
computes the viewing angle to the window centre, and maps it to stereo pan.

The maths:
  1. Query all screens (logical origin + size in points) via NSScreen.
  2. Estimate physical width: logical_width * mm_per_point.
  3. Convert window logical X to physical mm, inserting gap_mm between screens.
  4. Compute angle: atan2(dx_mm, viewing_distance_mm).
  5. Map angle to pan: pan = 0.5 + (angle / max_angle) * 0.5.

Config (in ~/.claude/tts-config.json under "spatial"):
    viewing_distance_mm — distance from eyes to screens. Default 1000.
    max_angle           — degrees off-centre for full pan. Default 90.
    mm_per_point        — mm per logical point. Default 0.22.
    gap_mm              — physical gap between monitors. Default 70.
    centre_x            — override perceptual centre (logical X coord).
                          Defaults to centre of the main display.

Returns 0.5 (centre) on any failure — silent fallback, never crashes.
"""

import json
import math
import os
import subprocess

_DEFAULT_MM_PER_POINT = 0.22


def _get_screen_info() -> list[dict]:
    """Return screen info from NSScreen.

    Each dict: {x, y, w, h} in logical points.
    """
    script = (
        'ObjC.import("AppKit");'
        "var ss=$.NSScreen.screens;"
        "var r=[];"
        "for(var i=0;i<ss.count;i++){"
        "var s=ss.objectAtIndex(i);"
        "var f=s.frame;"
        "r.push(f.origin.x+','+f.origin.y+','+f.size.width+','+f.size.height);"
        '} r.join("\\n");'
    )
    try:
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
    except Exception:
        return []

    result = []
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            x, y, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        result.append({"x": x, "y": y, "w": w, "h": h})
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
            capture_output=True,
            text=True,
            timeout=3,
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


def _logical_x_to_mm(x: float, screens: list[dict], mm_per_pt: float, gap_mm: float) -> float:
    """Convert a logical X coordinate to physical mm from the left edge.

    Walks through screens left to right, accumulating physical width
    and inserting gap_mm between adjacent screens.
    """
    sorted_screens = sorted(screens, key=lambda s: s["x"])

    mm_total = 0.0
    for i, scr in enumerate(sorted_screens):
        if i > 0:
            mm_total += gap_mm
        scr_left = scr["x"]
        scr_right = scr["x"] + scr["w"]
        scr_width_mm = scr["w"] * mm_per_pt
        if x <= scr_left:
            break
        elif x >= scr_right:
            mm_total += scr_width_mm
        else:
            mm_total += (x - scr_left) * mm_per_pt
            return mm_total
    return mm_total


def compute_pan() -> float:
    """Compute stereo pan position for the current terminal window.

    Returns 0.0 (full left) to 1.0 (full right), defaulting to 0.5 on failure.
    """
    iterm_sid = os.environ.get("ITERM_SESSION_ID", "")
    if ":" not in iterm_sid:
        return 0.5
    session_uuid = iterm_sid.split(":", 1)[1]

    bounds = _get_window_bounds_by_session(session_uuid)
    if bounds is None:
        return 0.5
    x1, _y1, x2, _y2 = bounds
    window_centre_x = (x1 + x2) / 2.0

    screens = _get_screen_info()
    if not screens:
        return 0.5

    spatial = _load_spatial_config()
    viewing_dist = float(spatial.get("viewing_distance_mm", 1000))
    max_angle = float(spatial.get("max_angle", 90))
    mm_per_pt = float(spatial.get("mm_per_point", _DEFAULT_MM_PER_POINT))
    gap_mm = float(spatial.get("gap_mm", 70))

    # Perceptual centre — centre of main display (origin 0,0)
    if "centre_x" in spatial:
        centre_logical_x = float(spatial["centre_x"])
    else:
        main = next((s for s in screens if s["x"] == 0 and s["y"] == 0), None)
        if main:
            centre_logical_x = main["w"] / 2.0
        else:
            global_left = min(s["x"] for s in screens)
            global_right = max(s["x"] + s["w"] for s in screens)
            centre_logical_x = (global_left + global_right) / 2.0

    # Convert to physical mm
    window_mm = _logical_x_to_mm(window_centre_x, screens, mm_per_pt, gap_mm)
    centre_mm = _logical_x_to_mm(centre_logical_x, screens, mm_per_pt, gap_mm)

    # Viewing angle and pan
    dx_mm = window_mm - centre_mm
    angle_deg = math.degrees(math.atan2(dx_mm, viewing_dist))
    pan = 0.5 + (angle_deg / max_angle) * 0.5
    return max(0.0, min(1.0, pan))


if __name__ == "__main__":
    pan = compute_pan()
    print(f"pan={pan:.3f}")
