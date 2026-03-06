# Playback Spec

## Principles

1. **One player, one queue.** `playback_worker` is the ONLY code that touches the audio device. It calls `sd.play()` for each item in the queue. Nothing else opens OutputStreams.

2. **Full audio clips only.** Every item in the playback queue is a complete numpy array — one whole utterance. No tiny streaming chunks. The model can use streaming inference internally to render faster, but the result is accumulated into one array before queueing.

3. **Session-based ordering.** Each caller (Claude Code session, other client) has a session ID. Messages from the same session play in FIFO order. Sessions take turns — once a session starts playing, it plays all its queued messages before the next session gets a go.

4. **STOP semantics.** STOP means "shut up now" — it clears the queue and stops current playback. STOP is sent ONLY on:
   - UserPromptSubmit (user typed something new)
   - Hook cancellation (user interrupted)

   STOP is NOT sent between pre-tool and stop hooks within the same turn.

## Hook lifecycle (one Claude Code turn)

```
User submits prompt
  → UserPromptSubmit hook fires → sends STOP (kill previous audio)

Claude thinks, writes text, calls a tool
  → PreToolUse hook fires → sends SEQ with session_id
    → daemon renders audio (batch or accumulated stream)
    → daemon queues the full audio array
    → playback_worker plays it

Claude calls another tool
  → PreToolUse hook fires again → sends SEQ with session_id
    → dedup catches repeated text, skips
    → new text gets rendered and queued

Claude finishes turn
  → Stop hook fires → sends SEQ with session_id
    → dedup catches repeated text, skips
    → any new text gets rendered and queued after current playback
```

## Multiple callers

```
Session A queues msg 1, msg 2
Session B queues msg 3
Session A queues msg 4

Playback order: msg 1, msg 2, msg 4, msg 3
(Session A started first, plays all its queued items, then B gets a turn)
```

## Wire protocol

No changes to the wire format. Session ID is already in the SEQ command or can be added as a field. The daemon tracks which session is "active" and prioritises its queue.

## What this means for the code

- `stream_chunks()` in pocket.py: still useful for fast inference, but the daemon accumulates all chunks into one array before queueing. No per-chunk queueing.
- `play_streaming()` in pocket.py: dead code. Remove it.
- `playback_worker` in daemon.py: stays as-is. One `sd.play()` per complete audio array.
- No OutputStreams opened anywhere except inside `playback_worker` (via `sd.play()`).
- The streaming OutputStream experiment (callback-based) is scrapped.
