# Wednesday TTS Architecture

This document describes the current runtime architecture in this repo.
It is intentionally implementation-first: if this conflicts with an older
note elsewhere, trust the code under `src/wednesday_tts/server/`.

## Runtime Topology

There are two service entrypoints:

- `src/wednesday_tts/server/daemon.py`
  - Primary macOS runtime.
  - Unix socket server at `/tmp/tts-daemon.sock`.
  - Used by the Claude Code hooks and the Gemini CLI integration.
  - Owns the current queueing, barge-in, direct-play, spatial audio, and
    per-request voice-selection logic.
- `src/wednesday_tts/server/app.py`
  - Windows-oriented Flask HTTP service on `http://localhost:5678`.
  - Exposes `/speak`, `/stop`, `/normalize`, `/health`, `/stats`, `/reload`.
  - Still useful for the Python client and Windows service installs, but it is
    not the authoritative implementation for the current Claude Code path.

For day-to-day repo work, treat the Unix-socket daemon as the main system.

## End-to-End Flow

macOS / Claude Code:

1. A Claude Code hook fires.
2. The hook filters out teammate and sub-agent sessions.
3. The hook computes `voice_hash`, optional `pan`, and `timestamp`.
4. The hook sends a newline-terminated JSON message to `/tmp/tts-daemon.sock`.
5. `daemon.py` acknowledges immediately with `ok`.
6. The daemon resolves the request voice, parses guillemet tags, normalizes
   text, dedups recent repeats, renders audio, and either:
   - queues audio for the playback worker, or
   - lets a direct-play backend own playback itself.

Windows / HTTP:

1. A client posts raw text to `/speak`.
2. `app.py` optionally normalizes it.
3. The active backend renders audio.
4. The service plays audio locally.

## Wire Protocol

The daemon protocol is JSON over a Unix domain socket, newline-terminated.

Core commands:

- `speak`
- `stop`
- `skip`
- `ping`
- `chirp`
- `drain`
- `normalize`
- `stats`
- `render`

See `docs/voice-pipeline-spec.md` for the request schema and command semantics.

## Playback Model

There are now two playback paths in the daemon:

### Queue-driven playback

Used by batch backends and queue-streaming backends.

- `playback_worker()` owns the long-lived audio output path.
- Items in `playback_queue` are tuples of `(audio_array, text, msg_id)`.
- The worker preserves message ordering and watches for device changes.
- PortAudio stereo playback is the default fallback.
- On supported Bluetooth outputs, the daemon can route through the
  SpatialStream helper for head-tracked / spatialized playback.

### Direct-play streaming

Used by backends that opt into `supports_direct_play`.

- The backend opens and manages its own low-latency output stream.
- This is currently how `vibevoice` gets its lowest-latency path.
- The daemon still owns request-level control: stop checks, device-change
  signalling, msg IDs, logging, and barge-in / skip decisions.

This means the old rule "only playback_worker ever touches the audio device"
is no longer universally true. It is still true for queue-driven playback, but
VibeVoice's direct-play path is an explicit exception in the current design.

## Voice Selection

Request-level voice selection happens in the daemon, not in the hooks.

Resolution order:

1. `session_id` hashed into the active backend's `voice_pool`
2. `voice_hash` hashed into the active backend's `voice_pool`
3. `default_voice` from config
4. backend-native default

Inline guillemet tags are then applied on top of that request voice.

Current supported tag shapes:

- `<<text>>` conceptually, written with guillemets: `««text»»`
  - Uses `guillemet_voice` if configured, otherwise SAM.
- `««voice_name»text»»`
- `««voice_name|instruct»text»»`
- `««|instruct»text»»`
- `««instruct|text»»`

See `docs/voice-pipeline-spec.md` and `docs/voice-system-spec.md`.

## Barge-In

The daemon owns barge-in hold logic.

- Fresh flag path: `/tmp/wednesday-yarn-barge-in`
- Window: 3 seconds from latest touch
- Hard stale cutoff: 30 seconds
- Pending hold cap: 16 speak requests

Behaviour:

- If the user is dictating, new `speak` requests are held.
- The first held request in a barge-in cycle drops the current playing
  message via `skip`.
- Once the flag clears, held speaks replay in arrival order.
- A full `stop` clears the pending hold list instead of replaying it.

## Backends

Backends registered today:

- `pocket`
- `vibevoice`
- `kokoro`
- `qwen3`
- `moss`
- `sam`
- `soprano`
- `chatterbox`

Important practical differences:

- `pocket`
  - streaming-capable
  - queue-streaming path
  - default backend for most local use
- `vibevoice`
  - streaming-capable
  - queue-streaming and direct-play support
  - best latency when direct-play is viable
- `qwen3`
  - no streaming in the daemon path
  - supports voice-clone-style reference audio and `instruct`
- `sam`
  - special inline robot voice and fallback-friendly override backend

## Observability

Current observability is log-first.

- Main analytics script: `scripts/analyse_latency.py`
- Primary daemon log patterns:
  - `[req]`
  - `[voice]`
  - `[playback]`
  - `[spatial]`
  - backend-specific synth lines such as `[pocket]`, `[pocket-stream-*]`,
    `[vibevoice-play]`, `[qwen3]`, `[moss]`, `[kokoro]`

The analytics script is the single source of truth for latency summaries.
If a backend adds a new log shape, extend that script instead of creating a
parallel metrics parser.

## Logging Consistency Gaps

Current gaps worth fixing:

- Backends do not all emit the same start / first-audio / done markers.
- Some backend logs include `voice=` and `RTF`; some only partially do.
- Direct-play and queue-driven paths do not produce identical playback-stage
  logs.
- Request metadata is rich on `[req]` lines but less consistent on downstream
  synth / playback lines.

## Recommended Logging Direction

The smallest high-value cleanup would be:

1. Standardize three lifecycle markers for every backend:
   - `backend-start`
   - `backend-first-audio`
   - `backend-done`
2. Always include:
   - `msg_id`
   - backend name
   - `voice`
   - `session_id` or short session token when available
   - `source`
   - `audio_s`
   - `elapsed_s`
   - `rtf` when derivable
3. Keep `[req]` as the request envelope line and add backend-specific follow-up
   lines keyed by the same `msg_id`.
4. Update `scripts/analyse_latency.py` whenever a backend log contract changes.

That gets you much more comparable backend analytics without introducing a new
telemetry subsystem.
