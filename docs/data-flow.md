# TTS Data Flow

This document follows the current primary path: Claude Code hooks talking to
the macOS Unix-socket daemon.

## Stop Hook Flow

1. `integrations/claude-code/speak-response.py` receives the Claude payload.
2. It exits early if muted or if the payload belongs to a sub-agent / teammate.
3. It reads `last_assistant_message`, falling back to the transcript when
   needed.
4. It sends:

```json
{
  "command": "speak",
  "text": "...",
  "normalization": "markdown",
  "voice_hash": "........",
  "session_id": "...",
  "timestamp": 123.456,
  "source": "stop",
  "flush_session": true
}
```

5. `daemon.py` acks immediately, then processes the request.

## Pre-Tool Flow

1. `integrations/claude-code/pre-tool-speak.py` receives the Claude payload.
2. It exits early if muted or not the primary session.
3. It reads assistant text blocks after the latest user message from the
   transcript.
4. It concatenates and truncates them when necessary.
5. It sends a `speak` JSON message with `source: "pre-tool"`.

Unlike older versions, this hook does not maintain its own per-session spoken
hash file. Dedup is now handled server-side in the daemon.

## Daemon Speak Pipeline

Once the daemon receives `{"command":"speak", ...}`:

1. It optionally flushes older queued audio for the same session when
   `flush_session` is set.
2. It checks barge-in hold state.
3. It assigns a new `msg_id`.
4. It resolves the request voice from `session_id`, `voice_hash`, and config.
5. It parses guillemet tags into segments.
6. It normalizes segment text unless `pre-normalized`.
7. It dedups recently spoken text.
8. It chooses render mode:
   - direct-play streaming
   - queue streaming
   - batch / multi-segment
9. It logs the request and downstream synth/playback markers.

## Barge-In Flow

When `/tmp/wednesday-yarn-barge-in` is fresh:

1. The daemon holds new `speak` requests in `_barge_in_pending`.
2. The first held request of the cycle skips the currently playing message.
3. The barge-in worker polls for the flag to clear.
4. Once clear, held requests replay in arrival order.
5. If a full `stop` happens first, held requests are cleared instead.

## Auxiliary Commands

### `stop`

- sent by `scripts/stop-tts.sh`
- drains queue and clears held barge-in requests

### `skip`

- also available via `scripts/stop-tts.sh skip`
- drops the current message and its remaining queued chunks

### `chirp`

- plays the configured voice-command acknowledgement chime
- bypasses normalization, voice selection, and the TTS render pipeline

### `normalize`

- returns normalized text without playback

### `stats`

- returns daemon summary telemetry as JSON

## Analytics Flow

The log parser at `scripts/analyse_latency.py` is the supported analytics path.

It correlates:

- hook-to-daemon latency from `[req]`
- synth completion from backend log lines
- playback start from `[playback]`, `[spatial]`, or backend-specific first-audio

If a backend changes its log markers, update this script in the same change.
