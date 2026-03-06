# Wire Protocol

The daemon listens on a Unix socket at `/tmp/tts-daemon.sock`. Each connection carries exactly one command. Commands are newline-terminated UTF-8 strings. The daemon reads up to 65536 bytes per connection.

## Commands

### SEQ:N:speed:ct:ts:text

Primary command. Render text and play audio in sequence order.

**Wire format:**
```
SEQ:<seq_num>:<speed>:<content_type>:<timestamp>:<text>\n
```

| Field | Type | Description |
|-------|------|-------------|
| seq_num | int | Sequence number (0-based). Playback order follows sequence order. |
| speed | float or "N" | Tempo multiplier. "N" means use server default (TTS_SPEED env, default 1.25). |
| content_type | string | Content type for normalisation. Values: `markdown`, `normalized`. Empty string defaults to `markdown`. |
| timestamp | float or "" | Wall-clock epoch when the hook sent the request. Used for end-to-end latency tracking. Empty string if not available. |
| text | string | Everything after the 5th colon. The text to speak. |

**Example:**
```
SEQ:0:1.0:markdown:1709715600.123:Hello world\n
SEQ:0:1.0:markdown::Hello world\n
```

**Backward compatibility:** The daemon also accepts the old 4-field format `SEQ:N:speed:text` where `__ct:` and `__t:` prefixes are embedded in the text. This will be removed in a future version.

**Behaviour:**

1. If `content_type != "normalized"`, text is run through the normalisation pipeline
2. Voice tags (`{voice:X}...{/voice}`) are parsed into segments
3. If `seq == 0` and backend supports streaming and no mixed voices: uses streaming path
4. Otherwise: batch render via `_render_segments()`
5. Audio is enqueued in sequence order (waits for `_next_seq == seq`, 5s timeout)

**Response:** `ok` on success, `error` on failure.

### PING

Health check.

**Wire format:**
```
PING\n
```

**Response:**

| Response | Meaning |
|----------|---------|
| `ok` | Daemon is healthy |
| `dying` | Daemon has flagged itself as sick (streaming failed). Will exit after playback finishes. **To be removed** — daemon should not die on streaming failure. |
| `stream-disabled` | Streaming path disabled due to repeated failures, batch still works. **To be removed** — streaming failure should be per-chunk fallback, not a persistent flag. |

### STOP

Stop current audio and drain the playback queue. Resets sequence counter.

**Wire format:**
```
STOP\n
```

**Behaviour:**

1. Calls `sd.stop()` to halt current playback
2. Aborts any active streaming
3. Drains the playback queue
4. Increments `_stop_gen` (cancels in-flight renders)
5. Resets `_next_seq` to 0

**Response:** `ok`

Critical for UX — this is how the UserPromptSubmit hook kills audio when Tamm starts typing or is in a video call.

### DRAIN

Wait for the playback queue to empty, then reset the sequence counter.

**Wire format:**
```
DRAIN\n
```

**Behaviour:**

1. Polls `playback_queue.empty()` with a 30-second deadline
2. Resets `_next_seq` to 0
3. Notifies all waiting threads

**Response:** `ok` (even on timeout)

### NORMALIZE:ct:text

Normalise text without generating audio. Returns the normalised text as UTF-8.

**Wire format:**
```
NORMALIZE:<content_type>:<text>\n
```

| Field | Type | Description |
|-------|------|-------------|
| content_type | string | Content type for normalisation (e.g., `markdown`) |
| text | string | Raw text to normalise |

**Response:** The normalised text as UTF-8 bytes.

### PCM:speed:text

Render text and return raw PCM audio bytes (no playback).

**Wire format:**
```
PCM:<speed>:<text>\n
```

| Field | Type | Description |
|-------|------|-------------|
| speed | float | Tempo multiplier |
| text | string | Text to render |

**Response:** 4 bytes (uint32 LE, sample rate) followed by float32 PCM samples. Empty response if generation fails.

### SPEED:speed:text (DEPRECATED)

Legacy unsequenced render. Bypasses sequence ordering.

**Wire format:**
```
SPEED:<speed>:<text>\n
```

**Behaviour:** Same as SEQ but without sequence ordering. Audio is enqueued directly.

**Status:** To be removed. No active use cases. The speak-response hook used this as a fallback when the daemon reported `dying` or `stream-disabled`, but those states are also being removed.

### STATS

Return telemetry as JSON.

**Wire format:**
```
STATS\n
```

**Response:** JSON object:
```json
{
  "uptime_s": 3600,
  "requests": {
    "total": 42,
    "completed": 40,
    "stopped": 1,
    "errored": 1
  },
  "audio_seconds_total": 180.5,
  "soundstretch": {
    "calls": 30,
    "avg_ms": 45.2
  },
  "backend": "pocket"
}
```

## Error Handling

- If the daemon cannot parse a command, it treats the entire message as plain text and renders it (legacy fallback)
- Connection timeout is 30 seconds per connection
- On any exception in `handle_client()`, `requests_errored` is incremented and `error` is sent back
- Empty messages receive `ok` with no action
