# Chimes and Fallback Tones

The daemon plays short audio cues for several events. Every cue is configurable
and prefers a real sound file. The synthesised tones in this codebase exist
only as last-resort fallbacks for when nothing is configured and no system
sound is available.

## Hearing-safety contract

**Synthesised fallback tones in this repo MUST obey:**

- frequency ≤ 880 Hz (A5)
- amplitude ≤ 0.1 (linear)
- short fade-in and fade-out to avoid clicks
- no high-pitched chirps, no alarm-style alerts, ever

This is a hard constraint, not a style preference. On 2026-05-03, an earlier
fallback chirp at 1200 / 1800 Hz and amplitude 0.35 nearly caused hearing
damage when played through in-ear monitors. The current fallback is a gentle
E5 → A5 pair (659 Hz then 880 Hz at 0.1) and that ceiling is not to be raised.

If you find yourself wanting a louder or higher tone for "audibility", the
correct answer is to point the relevant config key at a real sound file. The
synth fallbacks are intentionally bland.

## Cues and where they are wired

| Cue                     | Trigger                                          | Code                                                |
|-------------------------|--------------------------------------------------|-----------------------------------------------------|
| Error chime             | request timeout / unhandled error                | `daemon.py::_play_error_chime`                      |
| Voice-command chime     | voice-command socket message received            | `daemon.py::play_voice_command_chime` (or similar) |
| Cross-session chime     | Windows path queue contention                    | `app.py::play_chime`                                |
| Windows fallback beep   | Windows hook-level fallback                      | `platform.py::_play_chime_windows`                  |

## Configuration points

All paths support `~` expansion.

### `~/.claude/tts-config.json`

```json
{
  "error_chime": "~/Music/sounds/red-alert.wav",
  "voice_command_chime": "~/Music/sounds/computer-acknowledge.aiff"
}
```

- `error_chime` — file played on errors / timeouts. If unset or missing, the
  daemon tries `/System/Library/Sounds/Sosumi.aiff`, then the synthesised
  fallback.
- `voice_command_chime` — file played when the voice-command socket message
  is received. If unset, the synthesised fallback plays.

### Environment

- `TTS_CHIME_DIR` — directory of sound files (`.wav`, `.aiff`, `.mp3`,
  `.flac`, `.ogg`) used by `app.play_chime`. A random file is picked each
  time. If empty or unset, the synthesised fallback plays.

## Where to find replacement sounds

Free packs that work well:

- Star Trek TOS / TNG / DS9 UI sound effects (widely available, search for
  "LCARS sound pack" or "TOS computer sounds")
- macOS system sounds in `/System/Library/Sounds/`
- Any short `.wav` / `.aiff` you already own

Drop them in a folder you control (for example `~/Music/sounds/`) and point
the config keys above at them. Once a real sound file resolves, the
synthesised fallback never runs.

## When modifying tone code

Search the repo for `np.sin` and `np.cos` before changing anything in this
area. Stereo pan-law uses `np.cos` / `np.sin` as a constant gain — that is
not a tone and should not be touched. Anything else producing audio from a
sine generator must obey the contract at the top of this document.
