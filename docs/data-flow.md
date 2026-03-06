# TTS Data Flow

Full path from user interaction to audio output.

## User submits a prompt

- `UserPromptSubmit` hook fires
  - `stop-tts.sh` runs — sends `STOP` to daemon socket to kill any in-progress audio

## Claude writes text before a tool call

- `PreToolUse` hook fires (`pre-tool-speak.py`)
  - Reads transcript JSONL from disk
  - Finds assistant text blocks since last user message
  - Dedup check via `/tmp/tts-spoken-<session_id>` hashes
  - Sends unclaimed text to daemon: `SEQ:0:1.0:markdown::<text>\n` over Unix socket
  - Daemon receives on accept loop, spawns `handle_client` thread
    - Parses SEQ fields: seq_num, speed, content_type, timestamp, text
    - Runs `run_normalize()` (markdown → spoken text)
    - Checks `use_streaming` — True when SEQ==0, no mixed voices, backend supports it
    - **Streaming path**: spawns `_stream_to_queue` thread
      - Calls `backend.stream_chunks(text, speed)`
        - Holds `self._lock` for entire generation
        - Calls `self._model.generate_audio_stream()` — yields raw numpy chunks
        - Each chunk runs through `soundstretch_tempo()` subprocess (per-chunk — known problem)
        - Yields float32 arrays
      - Each chunk → `playback_queue.put(chunk)`
      - `first_chunk_event.set()` on first chunk
    - Main handler waits up to 8s for `first_chunk_event`
      - If timeout: falls back to batch render via `_render_segments()`
      - If success: advances `_next_seq`, sends `ok` back to hook
    - **Batch fallback path**: `_render_segments()` → `backend.generate()` → single `soundstretch_tempo()` call → `playback_queue.put(audio)`
  - `playback_worker` thread (always running):
    - `playback_queue.get()` — blocks until chunk available
    - `_try_play(item, sample_rate)`
      - `get_default_output_device()` — calls `sd._terminate()` / `sd._initialize()` to rescan PortAudio
      - `sd.play(item, samplerate, device)`
      - Polls `sd.get_stream().active` every 50ms until done or 5s watchdog
    - Picks up next chunk from queue

## Claude finishes its turn

- `Stop` hook fires (`speak-response.py`)
  - Reads `last_assistant_message` from hook payload (or transcript fallback)
  - Dedup check via same `/tmp/tts-spoken-<session_id>` hashes
  - Same send path: `SEQ:0:1.0:markdown:<wall_time>:<text>\n` to daemon socket
  - Same daemon handling as above

## User cancels audio (Ctrl+Option+X)

- Hammerspoon hotkey runs `stop-tts.sh`
  - Sends `STOP` to daemon socket
  - Daemon: `_stop_playback()`
    - `sd.stop()` — kills current PortAudio playback
    - `backend.abort_stream()` — sets `_active_stream = None`
    - Drains `playback_queue`
    - Increments `_stop_gen` — in-flight streaming threads see mismatch and bail

## Background threads in daemon

- `_audio_health_worker` — every 30s opens/closes a test OutputStream to probe PortAudio. KNOWN HAZARD: `CloseStream` can hang in CoreAudio, blocking signal handling and freezing the accept loop.
- `_hung_request_watchdog` — every 10s checks if requests have been in-flight > 120s, exits for launchd restart.

## Known issues (6 Mar 2026)

1. `_audio_health_worker` deadlocked the daemon — `CloseStream` hung inside CoreAudio, froze the main accept loop via signal handling.
2. `stream_chunks()` calls `soundstretch_tempo()` per chunk — subprocess per chunk causes audible gaps between segments.
3. `_try_play()` calls `get_default_output_device()` which does `sd._terminate()`/`sd._initialize()` on EVERY chunk — PortAudio reinit between chunks adds latency.
4. `stream_chunks()` holds `self._lock` for the entire generation — STOP can't start a new request until generation finishes.
