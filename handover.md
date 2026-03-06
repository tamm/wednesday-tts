# Handover — 6 Mar 2026

## What Was Done This Session

Fixed garbled/doubled audio in the wednesday-tts daemon. Commit: `333961d`.

### Root Cause (now fixed)
`play_streaming()` in pocket.py opened its own PortAudio `OutputStream` and played
audio directly to the device. Simultaneously, `playback_worker` in daemon.py called
`sd.play()`. Two audio paths into one device = garbled, doubled, choppy audio.

### Fixes Landed
1. **Core fix**: `stream_chunks()` generator added to `PocketTTSBackend` — yields
   numpy chunks without playing them. `SEQ:0` streaming now feeds chunks into
   `playback_queue`. `playback_worker` is now the ONLY code that touches the audio
   device.
2. **`stop-tts.sh`**: was sending `PING` instead of `STOP` via socket when PID file
   existed — queued chunks kept playing after new prompt. Now always sends `STOP`.
3. **`speak-response.py`**: removed upfront `PING` before sending. Sends `SEQ:0`
   directly. 10s timeout, then diagnostic ping + optional daemon kickstart.
4. **`pocket.py`**: PortAudio reinit only on retries, not first attempt.
5. **Removed**: `_streaming_disabled`, `_dying`, `_mark_dying()` — no more permanent
   failure states. Per-chunk batch fallback instead.
6. **Docs written**: `docs/architecture.md`, `docs/wire-protocol.md`,
   `docs/streaming-stability-plan.md`.

## Current State

- Daemon running, PID ~13048, clean log, `PING` returns `ok`
- 519/520 tests passing (1 pre-existing URL normalisation failure, unrelated)
- CoreAudio was in a bad state all day from crashes — reboot pending to verify fixes

## What Needs Verification After Reboot

1. Send a few TTS requests — confirm no garbling, no doubling
2. Confirm `Ctrl+Option+X` hotkey stops audio (Hammerspoon was mid-update earlier)
3. Confirm submitting a new prompt kills in-progress audio
4. Send a long response — confirm all chunks play cleanly in order, no overlapping

## Known Remaining Issues

### PortAudio callback still "not consuming audio" on fresh boot
The `[TTS] callback not consuming audio` message still fires on the very first
request after a fresh daemon start. This comes from `_buf_put()` inside
`play_streaming()` — but `play_streaming()` is no longer called from daemon.py.

**Likely explanation**: `play_streaming()` is still intact in pocket.py and may be
called from somewhere else, or the message is from a previous daemon run (the log
file is cumulative). Need to confirm after reboot.

If it persists: the 8s `_buf_put` timeout and the PortAudio queue fill issue may
still affect the new `stream_chunks()` path differently. Monitor the log after reboot.

### `stream_chunks()` acquires `self._lock` for the full generation
The `with self._lock:` in `stream_chunks()` holds the generation lock for the entire
streaming session. If a STOP arrives mid-stream, the lock prevents a new request from
starting until the current generation finishes. This may cause a brief delay after
STOP. Not urgent but worth watching.

### Health probe opens a test OutputStream every 30s
`_audio_health_worker()` opens and immediately closes a test `OutputStream` to probe
PortAudio. This runs while `playback_queue` is empty. On macOS, opening a second
OutputStream while one is live causes `-50` errors. The check `playback_queue.empty()`
is supposed to guard against this, but there's a race: the queue may empty between
the check and the probe open. Low risk, but if crackle returns, suspect this.

### Pre-existing failing test
`tests/test_pipeline.py::TestURLs::test_bare_domain_dot_in_path` — URL normalisation
bug, pre-dates this session. Not urgent.

## What To Work On Next

1. **Verify fixes after reboot** — smoke test everything above
2. **Remove `play_streaming()` from pocket.py** — it's now dead code (daemon no longer
   calls it). Removing it eliminates the PortAudio callback path entirely, which was
   the source of all the `-50` / wedging issues. This is the cleanup that completes
   the architecture fix.
3. **Fix the pre-existing URL test** — low priority
4. **Consider: lock-free streaming** — `stream_chunks()` holds `_lock` for the full
   generation. Could yield chunks before acquiring lock, or use a per-generation lock.
