# Wednesday TTS Architecture

## System Overview

```
Claude Code hook  -->  Unix socket daemon  -->  TTS backend  -->  playback queue  -->  audio device
(speak-response.py)    (daemon.py)              (pocket.py)       (playback_worker)    (sounddevice)
(pre-tool-speak.py)
```

Two hooks trigger speech:

- **speak-response.py** (Stop event) — speaks the final assistant message after a full turn
- **pre-tool-speak.py** (PreToolUse event) — speaks mid-turn assistant text before each tool call

Both hooks connect to the daemon via a Unix socket at `/tmp/tts-daemon.sock`, send a command, and exit. The daemon keeps the TTS model loaded in memory and handles all audio output.

## The Playback Queue

A singleton `queue.Queue` sits between audio generation and audio output.

- Any thread can call `playback_queue.put(audio_array)` to enqueue audio
- Only one thread — `playback_worker` — reads from the queue and calls `sd.play()`
- This is the fundamental rule: **playback_worker is the ONLY code that calls sd.play()**. No other code plays audio directly.

Why: PortAudio cannot handle multiple concurrent streams. Two things calling `sd.play()` at the same time produces garbled audio (two voices overlapping) or PortAudio errors. The queue serialises all audio output through a single path.

## Batch Path

The default path for most requests:

1. `handle_client()` receives text via the socket
2. Text is normalised (markdown to speakable text)
3. `_render_segments()` calls `backend.generate()` to produce a numpy audio array
4. The array is enqueued via `playback_queue.put(audio)`
5. `playback_worker` dequeues it and calls `_try_play()` which calls `sd.play()`

This path is reliable. `sd.play()` is non-blocking and `_try_play()` has its own watchdog (deadline-based timeout, PortAudio reinit on failure, one retry).

## Streaming Path

Used for SEQ:0 requests when the backend supports streaming (`supports_streaming = True`). Provides lower time-to-first-sound by playing audio chunks as they are generated rather than waiting for the full utterance.

Current implementation (pocket.py `play_streaming()`):

1. Opens a callback-mode `sd.OutputStream`
2. The TTS model yields audio chunks via `generate_audio_stream()`
3. Each chunk is upsampled to device rate and put into an internal `audio_buf` queue
4. A PortAudio callback pulls from `audio_buf` and writes to the hardware

**Problem**: `play_streaming()` bypasses the playback queue entirely. It opens its own OutputStream and plays audio directly. This means:

- If `playback_worker` is also playing something, two audio streams overlap (garbled audio)
- The `_streaming_lock` exists to prevent concurrent streams, but it doesn't coordinate with `playback_worker`
- The daemon waits for streaming to finish before allowing `playback_worker` to play the next item, but this coordination is fragile

**The fix (planned)**: streaming must route generated chunks through the playback queue, not play them directly. `playback_worker` remains the single audio output path.

## SEQ Ordering

Hooks send requests with sequence numbers: `SEQ:N:speed:text`. The daemon ensures playback order matches sequence order even when requests arrive or render out of order.

Mechanism:

- `_next_seq` tracks which sequence number the playback queue expects next
- When a chunk with `seq=N` finishes rendering, it waits on `_order_cond` until `_next_seq == N`
- Once its turn arrives, it enqueues audio and increments `_next_seq`
- `_order_cond.notify_all()` wakes other waiting threads
- A 5-second timeout prevents deadlock if a chunk is lost

Currently, hooks only send SEQ:0 (single chunk per response). The mechanism supports multi-chunk delivery if needed in future.

`_stop_gen` is a generation counter incremented on STOP. In-flight renders compare against their snapshot to bail out early when the user interrupts.

## Voice Switching

Text can contain inline voice tags: `««words»»` (SAM voice)

Processing:

1. `_split_voice_segments()` parses text into `(voice_name, text)` tuples
2. Plain text gets `voice_name=None` (uses the primary backend)
3. Tagged text gets the specified voice name
4. `_render_segments()` renders each segment with the appropriate backend
5. Override backends are lazy-loaded and cached in `_voice_cache`
6. All segments are resampled to the primary backend's sample rate and concatenated

Mixed-voice messages always use the batch path (streaming is disabled for them) because segments from different backends must be stitched together.

## STOP Handling

`_stop_playback()`:

1. Calls `sd.stop()` to halt current audio
2. Calls `backend.abort_stream()` if streaming is active
3. Drains the playback queue
4. Increments `_stop_gen` and resets `_next_seq` to 0
5. Notifies all threads waiting on `_order_cond`

Triggered by: STOP command via socket, SIGUSR1 signal (from `stop-tts.sh`).

## Background Threads

The daemon runs several background threads:

| Thread | Purpose |
|--------|---------|
| `playback_worker` | Dequeues audio and calls `sd.play()` — the singleton audio output |
| `_audio_health_worker` | Probes PortAudio periodically, disables streaming or exits on failure |
| `_hung_request_watchdog` | Detects generate() hangs via in-flight request count staleness |
| Per-request handler threads | One per socket connection, runs `handle_client()` |

## Daemon Lifecycle

Managed by launchd (`com.tamm.wednesday-tts`). On startup:

1. Load TTS backend and model into memory
2. Write PID to `/tmp/tts-daemon.pid`
3. Bind Unix socket at `/tmp/tts-daemon.sock`
4. Start `playback_worker`, `_audio_health_worker`, `_hung_request_watchdog`
5. Accept connections in a loop, spawning handler threads

On exit: drain playback queue, close socket, remove socket and PID files.

Launchd restarts the daemon automatically on crash or `os._exit(1)`.
