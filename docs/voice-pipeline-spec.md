# Voice Pipeline Spec

Source of truth for how a `speak` request becomes a voiced utterance in the
macOS daemon.

## Scope

This spec covers:

- daemon wire protocol
- request-level voice resolution
- inline guillemet voice switching
- hook responsibilities
- stop / skip / barge-in behaviour

It does not try to describe every backend's synthesis internals. See
`docs/voice-system-spec.md` for backend-specific voice details.

## Transport

Hooks and local integrations talk to the daemon over:

- socket path: `/tmp/tts-daemon.sock`
- framing: one JSON object per line
- reply style: short ack payload such as `ok`, `error`, or command-specific data

## Command Schema

Current daemon commands:

- `speak`
- `stop`
- `skip`
- `ping`
- `chirp`
- `drain`
- `normalize`
- `stats`
- `render`

Canonical `speak` shape:

```json
{
  "command": "speak",
  "text": "Hello there",
  "normalization": "markdown",
  "voice_hash": "7f2c1a90",
  "session_id": "session-uuid",
  "timestamp": 1777600000.123,
  "source": "stop",
  "pan": 0.52,
  "flush_session": true
}
```

Field notes:

- `text`
  - required for `speak`, `normalize`, and `render`
- `normalization`
  - `markdown`
  - `plain`
  - `pre-normalized`
- `voice_hash`
  - optional stable 8-char hex hash from the caller's repo / cwd
- `session_id`
  - optional, but preferred
- `timestamp`
  - optional wall-clock time used for hook-to-daemon latency logging
- `source`
  - usually `stop` or `pre-tool`
- `pan`
  - optional stereo position from `0.0` to `1.0`
- `flush_session`
  - stop-hook preemption hint; used to flush older queued audio from the same
    Claude session before reading the final response

## Hook Responsibilities

Shared hook behaviour lives in `integrations/claude-code/hook_common.py`.

Hooks are intentionally thin. They should:

1. Read the Claude Code payload.
2. Exit early if muted.
3. Exit early for sub-agents and teammates.
4. Extract assistant text.
5. Compute `voice_hash` from repo root or cwd.
6. Compute `pan` when available.
7. Send JSON to the daemon and exit.

Hooks should not:

- pick voices directly from the config pool
- normalize text themselves
- implement barge-in
- duplicate daemon dedup logic

## Request-Level Voice Resolution

The daemon resolves one request voice before inline tags are applied.

Resolution order:

1. If `session_id` is present and the active model has a `voice_pool`:
   - `sha256(session_id)[:8]`
   - modulo pool length
2. Else if `voice_hash` is present and the active model has a `voice_pool`:
   - `int(voice_hash, 16) % len(pool)`
3. Else use `default_voice` from the active model config
4. Else use the backend's native default

The daemon logs the decision on `[voice]` lines and then emits a `[req]` line
with the resolved request voice.

## Voice Pool Format

The current daemon expects voice-pool entries to be objects, not bare strings.

Typical shape:

```json
{
  "active_model": "qwen3",
  "models": {
    "qwen3": {
      "voice_pool": [
        {
          "name": "warm-a",
          "voice": "/path/to/ref-a.wav",
          "voice_text": "Reference transcript"
        },
        {
          "name": "warm-b",
          "voice": "/path/to/ref-b.wav",
          "voice_text": "Reference transcript"
        }
      ],
      "default_voice": {
        "name": "default",
        "voice": "/path/to/default.wav",
        "voice_text": "Reference transcript"
      },
      "guillemet_voice": "sam"
    }
  }
}
```

If `default_voice` is absent but the active model config has a top-level
`voice`, the daemon synthesizes a default entry from that value.

## Guillemet Syntax

Inline voice switching is parsed before normalization.

Supported forms:

- `««text»»`
  - guillemet voice
  - defaults to `sam`
  - may use `guillemet_voice` from config instead
- `««voice_name»text»»`
  - named pool voice or configured fallback default
- `««voice_name|instruct»text»»`
  - named voice plus backend-specific instruct style
- `««|instruct»text»»`
  - request voice plus instruct
- `««instruct|text»»`
  - shorthand for request voice plus instruct

Important behaviour:

- Plain text segments use the resolved request voice.
- `sam` is special-cased as a backend switch, not a pool name lookup.
- Named guillemet voices resolve by `name` in `voice_pool`.
- Unknown named guillemet voices fall back to `default_voice`, not SAM.

## Segment Rendering

The daemon splits text into `(voice, instruct, text)` segments.

Then:

1. Normalizes each segment's text unless `pre-normalized`
2. Dedups the reassembled spoken text
3. Chooses one of three render modes

### Direct-play streaming

Used when:

- backend supports streaming
- backend supports direct-play
- there is no SAM / mixed-backend switch

Current practical case: `vibevoice`.

### Queue streaming

Used when:

- backend supports streaming
- backend does not use direct-play for this request
- there is no SAM / mixed-backend switch

Current practical case: `pocket`, and `vibevoice` when it returns queued
buffers rather than owning playback directly.

### Batch / multi-segment render

Used when:

- there are multiple segments
- a SAM override is involved
- per-segment `instruct` needs to be preserved
- the backend does not stream

The daemon renders each segment, resamples to the primary backend's sample
rate if needed, and cross-fades segment boundaries.

## Stop, Skip, and Flush

### `stop`

- drains the playback queue
- clears pending barge-in speaks
- clears session tracking
- bumps `_stop_gen` so in-flight generation bails

Meaning: forget everything and go silent now.

### `skip`

- skips the current message
- drains queued chunks for that same `msg_id`
- preserves later messages

Meaning: move on from this utterance, not the whole queue.

### `flush_session`

Stop-hook preemption path for a Claude session.

- drops queued and in-flight future chunks from the same `session_id`
- does not hard-cut the chunk already being written
- prepends a short transition cue to the final response

Meaning: replace stale pre-tool audio for this session with the final answer.

## Barge-In Hold

While `/tmp/wednesday-yarn-barge-in` is fresh:

- incoming `speak` requests are held, not rejected
- the first held request in the cycle skips the currently playing message
- later held requests stay queued in memory

When the flag clears:

- held requests replay in arrival order through the normal pipeline

When a full `stop` happens during barge-in:

- held requests are discarded

## Logging Contract

At minimum, a normal request should produce:

- one `[req]` line
- one or more backend synth lines
- one `[playback] first-chunk` line for queue-driven playback or the backend's
  first-audio equivalent for direct-play

`scripts/analyse_latency.py` depends on this log shape. If you change the
markers, update that script in the same change.
