# VibeVoice Playback Architecture

Status: **Living doc** — debugging the popping problem (May 2026)

This is the single source of truth for how VibeVoice streams audio in this project. Read this before changing playback behaviour. Other backends (kokoro, pocket, sam, qwen3, moss, chatterbox, soprano) are NOT covered here and MUST NOT be touched as part of this work.

## Scope

Only `src/wednesday_tts/server/backends/vibevoice.py::VibeVoiceBackend.play_streaming` is in scope. That's the function the daemon calls when it picks the DIRECT-PLAY path for vibevoice.

There's a second path on this backend, `generate_streaming`, which feeds the daemon's main `playback_queue` instead of opening its own audio device. We don't currently use it for vibevoice (the daemon prefers `play_streaming` because every extra hop added audible gaps in earlier experiments). Out of scope for this doc.

## The pipeline at a glance

```
                      ┌──────────────────────────────────┐
   model.generate     │  AudioStreamer (vibevoice's      │
   (worker thread) ──►│  internal queue of torch tensors)│
                      └────────────┬─────────────────────┘
                                   │  pulled chunk-by-chunk
                                   ▼
                      ┌──────────────────────────────────┐
                      │  drain thread                    │
                      │  - tensor → float32 numpy        │
                      │  - detect trailing silence       │
                      │  - write samples into ringbuffer │
                      └────────────┬─────────────────────┘
                                   │  np.float32 samples
                                   ▼
                      ┌──────────────────────────────────┐
                      │  ringbuffer (np.ndarray)         │
                      │  - write_idx (drain writes here) │
                      │  - read_idx  (callback reads)    │
                      └────────────┬─────────────────────┘
                                   │
                                   ▼
                      ┌──────────────────────────────────┐
                      │  PortAudio callback              │
                      │  - copies samples into outdata   │
                      │  - PortAudio plays to device     │
                      └──────────────────────────────────┘
```

Three threads run concurrently:
- **Generator thread** (`_run`): blocks inside `model.generate(...)`. Tokens go into `streamer`.
- **Drain thread** (`_drain_streamer`): pulls from `streamer`, writes to ringbuffer.
- **PortAudio callback thread**: invoked by the audio driver every block (~256-1024 samples). Reads from ringbuffer.

The main `play_streaming` thread sits in a wait loop until generation is done and the buffer is drained.

A `threading.Condition` guards `write_idx`, `read_idx`, and `pause_marks`. Both the drain thread and the audio callback take the condition before touching them.

## What "streaming" and "ringbuffer" mean here

- **Streaming** = the model emits audio in small chunks (~133ms each) as it generates, instead of producing the whole utterance and then handing it over. We can start playing before the model finishes.
- **Ringbuffer** = a fixed-size numpy array used as a circular buffer between the producer (drain) and the consumer (callback). `write_idx` and `read_idx` are absolute monotonic counts; index into the array via modulo. "Ringbuffer fill" = `write_idx - read_idx` = how many samples are sitting there ready to play.

These are two separate concepts. Streaming is *upstream* of the ringbuffer (model → drain). Ringbuffer is *between* drain and callback. They are not alternatives.

## Lifecycle of one `play_streaming` call

1. **Setup** — load voice prompt, prepare inputs, allocate ringbuffer, create empty `pause_marks` list.
2. **Start generator + drain threads.**
3. **Cold-start prebuffer**: `play_streaming` blocks on the condition until the ringbuffer has accumulated enough audio to start playing safely.
4. **Open the audio stream** (PortAudio) and `stream.start()`.
5. **Wait loop** — main thread sleeps on the condition, waking to check stop, generation-done, or device-change events.
6. **Stream close** — when generation is done AND the ringbuffer is drained, close the stream and join the threads.

## Component-by-component breakdown

### Generation thread (`_run`)
Trivial. Calls `model.generate(...)` with a `stop_check_fn` so SIGUSR1 / barge-in can interrupt it. On exit, calls `streamer.end()` so the drain loop exits.

### AudioStreamer
Lives inside the upstream `vibevoice` package. It's an iterator that yields torch tensors as the model produces them. We do not modify or wrap it.

### Drain thread (`_drain_streamer`)
For each tensor pulled from the streamer:
1. Convert to mono float32 numpy.
2. Skip if empty.
3. Run `_chunk_pause_offset(arr, sr)` — see below.
4. **Write the raw chunk to the ringbuffer.** No crossfade, no fade-in, no manipulation. Just `ring[write_idx:write_idx+n] = arr` (with wrap-around).
5. Update fill EMA.
6. **Maybe inject silence** (see "Pause-time silence injection" below).
7. Emit a `[vibevoice-chunk]` log line.

### `_chunk_pause_offset`
A heuristic that scans the **last 30ms** of the current chunk for trailing silence. RMS of a 5ms sliding window; threshold 0.005. Returns the sample offset where the trailing silence run begins, or `None` if the chunk doesn't end in silence.

This is the **only** silence detector. It runs on every chunk because we don't know which chunks will end in silence until we look. That's not "scanning every sample for silence" — it's a 30ms window check, cheap.

Whether the threshold and window are right is a separate question. Tracked in task #8.

### Ringbuffer
Plain numpy float32 array, size `max(prebuffer_max_samples * 4, ringbuffer_sec * sr)`. Default `ringbuffer_sec = 60s`, so capacity is ~60s of audio. Both the drain thread and the callback take the same `cond` lock before touching it.

`pause_marks: list[int]` — a list of absolute write positions where the drain thread saw `_chunk_pause_offset` return non-None. Used by the callback to know "if I park silence here, I'm parking at a real silence boundary in the audio".

### PortAudio callback (`_audio_callback`)
Called every audio block (~10-40ms worth of samples) by the audio driver, off the main Python threads.

Currently does:
1. Drop pause marks the read head has already passed.
2. **Pause-aware stretching path**: if `not gen_done` AND fill is below `low_water` (0.5s) AND we're at a pause mark, write zeros instead of audio for this block. Keep doing that until fill recovers above `catchup_water` (1.0s) or we've stretched 1.5s.
3. Otherwise: copy `min(frames, available)` samples from ringbuffer into `outdata`.
4. **Underrun fade-out**: if fewer samples were available than the block needed, ramp the last delivered sample down to zero over up to 64 samples, then fill the rest with zeros.

### Stream lifecycle helpers

- `_open_stream()`: Opens an `sd.OutputStream`. If the daemon supplied an `audio_context`, takes the daemon's `_portaudio_lock` and runs `sd._terminate(); sd._initialize()` first so PortAudio re-detects the current default output device. This is what makes mid-utterance device-swap (headphones ↔ speakers) work.
- Main loop watches `audio_context.device_changed`. If set: stop and close the current stream, clear the event, open a fresh stream against the new default.

### Pause-time silence injection (drain-side)

After writing a chunk, if `pause_off is not None` AND `playback_started.is_set()` AND fill EMA is below a threshold, write a chunk of zeros (~150ms or 400ms depending on EMA region) into the ringbuffer. This pushes the audible pause longer than the natural one.

Thresholds today (subject to change): `critical = 0.25s` → 400ms pad, `comfort = 0.7s` → 150ms pad.

## What I added in this session — and why

| # | Change | Reason it was added | Should it stay? |
|---|--------|---------------------|-----------------|
| 1 | Per-chunk instrumentation log line | We had no per-chunk visibility | **Keep** |
| 2 | Async log queue + worker thread | Worried `print(..., flush=True)` was blocking the drain | **Probably keep** — cheap, no risk |
| 3 | Prebuffer min-fill (0.4s) | First-chunk pause was triggering early start at 0.13s fill → underrun pops | **Keep** |
| 4 | Underrun fade-out (~64 samples) | Hard zero-step on underrun causes a click | **Reconsider** — Tamm wants no fades. Replace with: never let underrun happen. |
| 5 | Crossfade between chunks | Theory that chunk seams click from amplitude mismatch | **REMOVED 14:09** — was probably making it worse |
| 6 | EMA of buffer fill | Decision input for silence injection | **Keep** as instrumentation, decide on use |
| 7 | Drain-side silence injection at pause boundaries | Tamm's spec: predictively pad at safe pause points | **Reconsider** — currently fires repeatedly and creates compound long pauses |
| 8 | `playback_started` gate | Without it, cold-start padded the buffer huge | **Keep** if we keep #7 |
| 9 | `audio_context` for device-change cooperation | Vibevoice was opening its own raw stream, ignoring daemon's PortAudio lock and device-change events | **Keep** |
| 10 | Pre-existing callback "pause-aware stretching" (lines 421-445) | Original mechanism for buffer recovery at pause boundaries | **Reconsider** — duplicates #7's intent |

## The actual user-reported problems

1. **Regular periodic popping** ("every amount of time it does a pop"). Got *milder* over the course of an utterance (i.e. as the buffer filled). My crossfade hypothesis was wrong; ripped out.
2. **Mid-utterance long silence** that sounds like I just stopped talking. Confirmed in logs to coincide with `SIGUSR1 received, stopping playback` from the upstream barge-in / dictation hook stack — not pacing.
3. **No fading**. Tamm explicitly does not want fades anywhere.

## Hypotheses still on the table for the popping

a. **Resource contention**: too many threads / too many writes-during-callback. Possible — the layered cruft I've added increases lock pressure on `cond`.

b. **Underrun, not seam**: the popping fades as the utterance progresses. That correlates with buffer fill rising. If fill is genuinely teetering at the threshold for the first second or so, **every** callback that runs out mid-block creates a tiny zero-step regardless of any fade-out (the *next* callback then resumes from a non-zero sample, creating the discontinuity). The fade-out I added is a band-aid; the real fix is making sure underrun never happens.

c. **The daemon's main-loop device-change handler** racing with `play_streaming`'s own stream operations. `_device_changed` is a single Event shared with the daemon's main playback path. Currently we don't clear it on entry to `play_streaming`. Possible but not yet observed.

d. **Sounddevice / PortAudio block-size jitter** on the M5 with mixed sample rates between the model (24kHz) and the device (often 44.1kHz or 48kHz native). PortAudio resamples internally; if its resampler has glitches when the input source is intermittent, you'd hear them at a regular rate.

## Plan to investigate, NOT to code yet

1. **Listen to a single utterance with everything off**: no underrun fade, no callback pause-stretching, no drain-side silence injection. Just write chunks straight into the ringbuffer and read them out. This is the baseline. If it pops, the source is upstream of all our heuristics (ringbuffer → PortAudio path).
2. If baseline is clean: re-add features one at a time, testing after each. Stop adding when something causes a regression.
3. If baseline is NOT clean: investigate hypotheses (a)-(d) above.

We do not add or remove features without listening to a baseline first. No more guessing.

## Glossary

- **Chunk**: a ~133ms slice of audio that the model produces. Comes from `streamer`. Not a "buffer" by itself.
- **Ringbuffer / ring**: the circular numpy array between drain and callback.
- **Fill**: `write_idx - read_idx`, in samples.
- **EMA**: exponential moving average of fill.
- **Pause mark**: an absolute write-index position where the drain thread detected end-of-chunk silence.
- **Stream**: the `sd.OutputStream` object that talks to PortAudio. Distinct from the upstream model's `AudioStreamer`.
