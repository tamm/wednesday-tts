# Spatial Audio — Viewing Angle Formula Spec

How we estimate the physical viewing angle from a terminal window to the user's eyes, and map it to stereo pan.

## The formula

```
physical_width_mm = logical_width_pts * mm_per_point
window_mm         = logical_x_to_mm(window_centre_x, screens, gap_mm)
centre_mm         = logical_x_to_mm(centre_of_main_display, screens, gap_mm)
dx_mm             = window_mm - centre_mm
angle_deg         = atan2(dx_mm, viewing_distance_mm) * (180 / pi)
pan               = 0.5 + (angle_deg / max_angle) * 0.5
pan               = clamp(pan, 0.0, 1.0)
```

Where `logical_x_to_mm` walks screens left-to-right, converting logical coordinates to physical mm and inserting `gap_mm` between each screen boundary.

## Physical width estimation

### For now

One constant: `mm_per_point = 0.22`. Multiply by logical width from NSScreen.

No system_profiler, no display type detection, no pixel pitch lookup. Just `logical_w * 0.22`.

This is wrong by 20-30% on physical width, but atan2 compresses the error. At 1m distance, a 30% width error shifts pan by ~0.03. Inaudible.

### Ideal (future)

Use native resolution from system_profiler combined with scale factor (native_w / logical_w) to pick a more accurate mm_per_point per display. Would reduce the error to ~10%. Not worth the complexity yet — the current approach is good enough and avoids a 5-second system_profiler call.

## Defaults

| Parameter | Default | Config key |
|---|---|---|
| viewing_distance_mm | 1000 | `spatial.viewing_distance_mm` |
| max_angle | 90 | `spatial.max_angle` |
| mm_per_point | 0.22 | `spatial.mm_per_point` |
| gap_mm | 70 | `spatial.gap_mm` |
| centre_x | centre of main display | `spatial.centre_x` |

Config lives in `~/.claude/tts-config.json` under the `"spatial"` key.

## Reference: expected angles and pan values

All values at viewing_distance_mm=1000, gap_mm=70, mm_per_point=0.22.

### Single monitor (centre of screen = 0 deg)

| Setup | Est. width | Edge angle | Edge pan |
|---|---|---|---|
| MacBook 13" (1440 logical) | 317mm | 9.0 deg | 0.550 |
| MacBook 14" (1512 logical) | 333mm | 9.4 deg | 0.552 |
| MacBook 16" (1728 logical) | 380mm | 10.8 deg | 0.560 |
| 24" 1080p (1920 logical) | 422mm | 11.9 deg | 0.566 |
| 27" 4K (1920 logical) | 422mm | 11.9 deg | 0.566 |
| 27" 5K (2560 logical) | 563mm | 15.7 deg | 0.587 |
| 32" 4K (2560 logical) | 563mm | 15.7 deg | 0.587 |
| 32" 5K (2560 logical) | 563mm | 15.7 deg | 0.587 |

Note: 27" 4K at 2× and 24" 1080p at 1× both show 1920 logical points, so they get the same estimated width. The 27" is physically wider (~597mm vs ~527mm) but the pan difference is only ~0.01. Good enough.

### Multi-monitor (main = origin, side to the right, gap=70mm)

| Setup | Total span | Main edge | Side far edge | Side far pan |
|---|---|---|---|---|
| 27" 5K + MacBook 14" | 959mm | 15.7 deg | 30.3 deg | 0.668 |
| 32" 5K + MacBook 14" | 959mm | 15.7 deg | 30.3 deg | 0.668 |
| 27" 5K + 24" 1080p | 1055mm | 15.7 deg | 33.4 deg | 0.686 |
| 32" 5K + 27" 5K | 1196mm | 15.7 deg | 37.6 deg | 0.709 |

## Configurable overrides

All parameters can be overridden in `~/.claude/tts-config.json`:

```json
{
  "spatial": {
    "viewing_distance_mm": 1000,
    "max_angle": 90,
    "mm_per_point": 0.22,
    "gap_mm": 70,
    "centre_x": 1280
  }
}
```

If mm_per_point is set, it overrides the default for all screens. If centre_x is set, it overrides the auto-detected centre of the main display.
