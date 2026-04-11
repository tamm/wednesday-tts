# Voice Pipeline Spec

Source of truth for how voice selection works, end to end.

## Wire Protocol

All communication from hooks to the daemon is a single JSON object sent over the Unix socket at `/tmp/tts-daemon.sock`, newline-terminated. No custom syntax. Just JSON.

### Message Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["command"],
  "properties": {
    "command": {
      "type": "string",
      "enum": ["speak", "stop", "skip", "ping", "drain", "normalize", "stats", "render"],
      "description": "Required. speak = normalise, render audio, play through speakers. stop = halt current playback and drain the entire queue immediately; new speaks are accepted straight after. skip = drop the remaining chunks of the currently playing message only; later queued messages are preserved, and a 3s grace window rejects incoming speaks (a second skip inside the grace escalates to a full stop). ping = health check, returns 'ok'. drain = block until the playback queue empties. normalize = return cleaned text without generating audio. stats = return telemetry as JSON. render = normalise, render audio, return raw PCM bytes (no playback)."
    },
    "text": {
      "type": "string",
      "description": "The text to speak, normalise, or render. May contain ««guillemet»» tags for inline voice switches. Required when command is speak, normalize, or render."
    },
    "normalization": {
      "type": "string",
      "enum": ["markdown", "pre-normalized"],
      "description": "Controls text cleanup before synthesis. Omit for standard normalization (numbers, abbreviations, symbols). 'markdown' = strip markdown formatting (headers, links, code fences, bold, etc.) then standard normalization. 'pre-normalized' = text is already clean, skip all normalization."
    },
    "voice_hash": {
      "type": "string",
      "pattern": "^[0-9a-f]{8}$",
      "description": "8-char hex hash used to deterministically select a voice from the pool. The daemon maps it via int(hash, 16) % pool_size. The hook can derive this from anything stable — repo root, cwd, etc. Omit to use the model's default voice."
    },
    "session_id": {
      "type": "string",
      "description": "Caller's session UUID (e.g. from Claude Code). Included in all log lines for this request so you can trace a voice issue back to a specific session."
    },
    "pan": {
      "type": "number",
      "minimum": 0.0,
      "maximum": 1.0,
      "description": "Stereo pan position computed from terminal window location. 0.0 = hard left, 0.5 = centre, 1.0 = hard right. Omit for centre."
    },
    "timestamp": {
      "type": "number",
      "description": "Wall-clock epoch (seconds) when the hook fired. Used to measure end-to-end latency from hook trigger to first audio output."
    }
  },
  "allOf": [
    {
      "if": { "properties": { "command": { "enum": ["speak", "normalize", "render"] } } },
      "then": { "required": ["text"] }
    }
  ]
}
```

## Barge-in hold

When the user is dictating to wednesday-yarn (voice input), yarn touches `/tmp/wednesday-yarn-barge-in` to signal "be quiet, I'm talking". The daemon reads this flag directly — hooks are NOT involved in barge-in detection, they always send their speak requests and let the daemon decide.

The daemon's behaviour while the flag is fresh:

1. **Drop what's currently playing.** The very first speak request arriving while the flag is fresh triggers a one-shot `skip` on whatever message is mid-playback. This zero-delay drop gives the user an audible break the instant they start talking. If nothing is playing yet, the cycle still starts — subsequent arrivals are held, not talked over.
2. **Hold, do not drop, new speak requests.** Every speak request arriving during the window is appended to an in-memory pending list. `{"command":"speak",...}` clients still get an immediate `ok` ack — the daemon has the text, it's just holding it until the user finishes.
3. **Flag re-touches extend the window.** `_BARGE_IN_WINDOW_SECS` (3 seconds) is measured from the most recent flag mtime, not from first touch. Yarn touches the flag roughly once per second while dictation is active, so the window floats with the user.
4. **Replay in arrival order.** When the flag has not been touched for the window duration AND the daemon is not already mid-replay, `_barge_in_worker` drains the pending list under lock and re-enters `_process_speak` for each held message. Audio flows through the normal pipeline.
5. **Hard ceiling.** `_BARGE_IN_MAX_AGE_SECS` (30 seconds) is the absolute ceiling. If the flag has not been touched in 30 seconds the daemon treats it as stale (crashed dictation source) and removes it. This is the only fail-safe that prevents a wedged yarn from permanently muting TTS.
6. **Pending-list cap.** `_BARGE_IN_MAX_PENDING` (16 messages) caps the hold list. If the user dictates long enough that more than 16 messages pile up, the oldest is dropped with a log line. Stale held speaks are worse than silence.

**Semantics compared to stop and skip:**
- `stop` (explicit user "shut up": SIGUSR1, stop-tts.sh, `{"command":"stop"}`): drain the playback queue entirely, NO grace, NO pending list interaction. Subsequent speaks are accepted normally. Stop is a deliberate silence-now-and-go-back-to-normal action.
- `skip` (`{"command":"skip"}`): drop the current message's chunks by msg_id. Later queued messages are preserved. No grace window. This is "I've heard enough of this one, move on".
- Barge-in is the ONLY mechanism with a hold window, and it queues rather than rejects.

**What the pipeline must preserve:** no voice is ever lost during normal dictation. If the user dictates, pauses, and Claude has produced replies during that window, the user hears them in order once they stop talking. Voices are only dropped when the pending cap is exceeded (continuous 16+ replies during one uninterrupted dictation) or the 30-second staleness ceiling fires (yarn crash).

## Voice Pool

The voice pool lives in `~/.claude/tts-config.json` under the active model's config. It is an array of voice entries.

### Voice Entry Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["name", "voice"],
  "properties": {
    "name": {
      "type": "string",
      "description": "Unique human-readable identifier. Used in logs and for named voice references in guillemet tags."
    },
    "voice": {
      "type": "string",
      "description": "Path to voice file (wav, safetensors) or a predefined backend name (e.g. 'alba')."
    },
    "voice_text": {
      "type": "string",
      "description": "Reference transcript for voice cloning backends. Omit if not needed."
    }
  }
}
```

### Default Voice

The model config must include a `default_voice` field using the same schema as a pool entry:

```json
{
  "default_voice": {
    "name": "default",
    "voice": "/Users/tammsjodin/Music/voices/qwen3/default.wav",
    "voice_text": "When the sunlight strikes random raindrops..."
  }
}
```

This is the voice used when no pool entry can be resolved. It is a voice entry, not a bare path.

### Guillemet Voice

The model config can optionally include a `guillemet_voice` field to control what `««text»»` (no voice specifier) uses:

```json
{
  "guillemet_voice": "sam"
}
```

If set to `"sam"`, uses the SAM backend (default). Otherwise, it follows the same voice entry schema as pool entries and uses the primary backend. Omit to default to SAM.

### Voice Resolution

The daemon is the **sole decision point** for voice selection. Hooks do not read the voice pool they just send enough information to make decisions.

Resolution order:

1. If `session_id` is provided → `int(sha256(session_id)[:8], 16) % len(pool)` → use that pool entry.
2. Else if `voice_hash` is provided → `int(voice_hash, 16) % len(pool)` → use that pool entry.
3. If neither is available, or pool is empty → use the model's **default voice**.
4. If resolution fails for any reason → use the model's default voice.

Never fall back to SAM. SAM is not a fallback — it is a special-purpose voice for inline switching only.

If the model backend itself fails (crash, missing model, GPU error), the final escape hatch on macOS is the system `say` command. Something is always better than silence.

## Inline Voice Switching (Guillemet Tags)

Guillemet tags `««...»»` allow mid-sentence voice changes within a message. They are for fun/effect — a retro robot voice for a word, a different character voice for a quote. They are **not** the mechanism for choosing the message's primary voice.

### Syntax

| Pattern | Meaning |
|---------|---------|
| Plain text (no tags) | Speak with the **request voice** (resolved from session_id, voice_hash, or default). |
| `««voice_name»text»»` | Speak `text` in the named voice on the primary backend. |
| `««voice_name\|instruct»text»»` | Named voice with instruct text (e.g. `««alba\|whisper»boo»»`). |
| `««\|instruct»text»»` | Instruct only, no voice switch. Uses the request voice with the given instruct (e.g. `««\|excited»great news»»`). |
| `««text»»` | Speak `text` in the **guillemet voice** (see below). Defaults to SAM (retro 1982 formant synth). |

## Pipeline

### Terminology

- **Message**: One complete request from a hook. One assistant response = one message. Identified by `msg_id`.
- **Segment**: A contiguous piece of text within a message that shares a single voice. A message with no guillemet tags has one segment. A message like `"Hello ««world»» goodbye"` has three segments (plain, guillemet, plain).
- **Chunk**: A piece of audio within a segment. Streaming backends produce multiple chunks per segment for lower latency. Batch backends produce one chunk per segment.

Message > Segment > Chunk. Playback order follows this hierarchy strictly.

### Step 1: Hook fires

Two hooks can trigger speech:

- `speak-response.py` — Stop hook, runs at end of assistant turn.
- `pre-tool-speak.py` — PreToolUse hook, speaks mid-turn text blocks before each tool call.

Shared behaviour that MUST match between the two hooks lives in `integrations/claude-code/hook_common.py`: the mute check, the primary-session filter, voice hashing, stereo pan, and the Unix-socket sender. Both hooks import from there so they cannot drift out of sync. When adding a new speech-producing hook, import the same helpers; do not reimplement them.

The hooks do NOT implement barge-in detection. Barge-in (user-is-dictating) is handled entirely by the daemon — see "Barge-in hold" below. Hooks always send; the daemon decides whether to play now, hold, or drop.

**Both hooks MUST apply the same primary-session filter.** A Claude Code assistant message from a teammate or sub-agent must never reach the daemon.

The hook:

1. Extracts `cwd`, `session_id` from the Claude Code payload.
2. Filters out sub-agent and teammate messages. Per the Claude Code hooks docs (https://code.claude.com/docs/en/hooks.md, https://code.claude.com/docs/en/agent-teams.md, https://code.claude.com/docs/en/sub-agents.md), the Stop and PreToolUse payloads flag non-primary turns with: `agent_id` + `agent_type` (Task-tool sub-agent) or `team_name` + `teammate_name` (agent-team teammate). If ANY of `agent_id`, `agent_type`, `team_name`, `teammate_name` is present, the hook exits without sending anything. As a second layer of defence, the hook also consults `~/.claude/teams/*/config.json`: if the payload's `session_id` is listed in any team and is not that team's `leadSessionId`, it is treated as a teammate. This is a hard rule: sub-agent / teammate turns must be silent. Both `speak-response.py` and `pre-tool-speak.py` perform this check as their first action after parsing the payload. Do NOT remove or narrow this check. Do NOT guess field names — they come from the official Claude Code docs.
3. Extracts the assistant message text from the payload.
4. Computes `voice_hash`: SHA-256 of the git repo root (or cwd if not in a repo), truncated to 8 hex chars.
5. Computes `pan`: stereo position from the terminal window's screen location (macOS only, falls back to centre).
6. Records `timestamp` for latency tracking.
7. Sends a JSON object to the daemon socket with all of the above.

### Step 2: Daemon receives request

1. Parse JSON. Extract `session_id`, `voice_hash`, `text`, etc.
2. Assign a `msg_id` (monotonic integer). One request = one message = one `msg_id`. Used everywhere: rendering, queueing, playback, logging.
3. Resolve voice from `session_id` or `voice_hash` (see Voice Resolution above). Log it.
4. This resolved voice is the **request voice** for the entire message.

Messages are processed and played strictly one at a time. A message is all segments and all chunks from a single request. If a new request arrives while a previous message is still rendering or playing, it waits. The listener never hears audio from two messages interleaved.

### Step 3: Parse inline voice switches

Before normalisation, parse guillemet tags in the text:

1. Split text into segments: plain text segments and tagged segments.
2. Plain text segments get the request voice.
3. Tagged segments get their specified voice (SAM if no voice specifier, named voice otherwise).
4. Each segment knows its voice and backend before normalisation begins.

### Step 4: Normalise

Run each segment's text through the normalisation pipeline (unless `normalization` is `"pre-normalized"`). Voice identifiers are never normalised.

### Step 5: Render

For each segment:
- Plain/named voice segments → primary backend with assigned voice.
- SAM segments → SAM backend.

Segments are rendered and played in order. Each segment streams individually if the backend supports it — a mixed-voice message is just a sequence of segments, each with its own voice and backend.

### Step 6: Playback

- Segments play in order: segment 0 finishes completely before segment 1 starts.
- Within a streaming segment, chunks play in generation order.
- No segment or chunk is skipped or reordered.
- The next message does not begin until every segment of the current message has finished playing.
- **STOP**: Cancels the current message immediately. Discards all queued messages. Silence until a new speak request arrives.
- **SKIP** (SIGUSR1): Cancels the current message immediately. The next queued message begins playing. Use this when the user interrupts but more messages are waiting.

## Logging

Every request gets a `msg_id` (monotonic integer, assigned by daemon on receipt).

### Log Points

| Point | Tag | What to log |
|-------|-----|-------------|
| Hook send | `[hook]` | `voice_hash=H session=S cwd=PATH` |
| Daemon receive | `[req]` | `msg_id=N voice_hash=H session=S → voice=NAME` |
| Segment parse | `[req]` | `msg_id=N seg=I backend=B voice=NAME chars=C` |
| Render complete | `[req]` | `msg_id=N seg=I audio=Xs rtf=R` |
| Enqueue | `[req]` | `msg_id=N seg=I enqueued` |
| Playback start | `[play]` | `msg_id=N seg=I start` |
| Playback done | `[play]` | `msg_id=N seg=I done` |
| Message done | `[play]` | `msg_id=N all segments played` |

**Privacy note:** For now, text content appears in logs for debugging convenience. Long-term, replace text with a hash of what was spoken.

**Session/agent IDs** are included at receive time so you can trace which session produced which voice.

## What NOT to do

| Rule | Why |
|------|-----|
| Do not embed voice selection in the text body | Conflates message voice with inline switching. Caused the dict-as-string bug where voice dicts were serialised into guillemet tags and re-parsed. |
| Do not fall back to SAM on resolution failure | SAM is a novelty voice for inline fun, not a fallback. Default voice exists for a reason. |
| Do not read the voice pool in the hook | One reader (daemon), one decision point. Hook sends a hash, daemon resolves. Prevents pool-size mismatches. |
| Do not use `str(dict)` as a voice identifier | Voice entries are dicts in memory, passed by reference. Never serialise them to strings for matching or transport. |
| Do not send sequence numbers from hooks | Chunking and ordering are internal daemon concerns. Hooks send whole messages. |
| Do not use custom string-delimited wire formats | Colons, pipes, and other delimiters appear in normal text and break parsing. JSON is the wire format, full stop. |
| Do not resolve the voice more than once per request | Resolve at receive time, pass the resolved entry through the pipeline. Re-resolution caused the same hash to produce different results when config was re-read mid-request. |
| Do not normalise voice identifiers | Parse guillemet tags before normalisation. Voice names and paths must not go through the text normaliser. |
| Do not use `int(time.time())` as any kind of identifier | Wall-clock time is not monotonic and collides across concurrent requests. Use monotonic counters or UUIDs. |
