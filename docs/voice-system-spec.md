# Voice System Spec

Reference document for the TTS voice system as implemented. Describes what exists, not what is planned.

---

## Config Structure

Config lives at `~/.claude/tts-config.json`. The daemon reads it at startup.

Top-level keys:

| Key | Type | Description |
|-----|------|-------------|
| `active_model` | string | Which backend to load: `pocket`, `qwen3`, or `sam` |
| `max_chars` | int | Request truncation limit |
| `models` | object | Per-backend config blocks |

Each backend has its own block under `models.<name>`. Example:

```json
{
  "active_model": "qwen3",
  "max_chars": 500,
  "models": {
    "pocket": {
      "voice": "/path/to/voice.safetensors",
      "fallback_voice": "fantine",
      "speed": 1.25,
      "lsd_decode_steps": 1,
      "noise_clamp": null,
      "eos_threshold": -4.0,
      "voice_pool": ["..."]
    },
    "qwen3": {
      "speed": 1.3,
      "voice": "/tmp/tamm_voice_sample.wav",
      "voice_text": "Transcript of reference audio...",
      "seed": 42,
      "voice_pool": ["..."]
    },
    "sam": {
      "speed": 72,
      "pitch": 64,
      "mouth": 128,
      "throat": 128
    }
  }
}
```

### Config wiring at startup

`daemon.py:main()` reads the config file, then:

1. Selects backend: `TTS_BACKEND` env var > `active_model` > `"pocket"`
2. Reads `models.<backend>` into `_model_config`
3. Builds `_kwargs` from config fields (backend-specific, see below)
4. `POCKET_TTS_VOICE` env var overrides `voice` for pocket
5. Instantiates `backend_cls(**_kwargs)` and calls `backend.load()`

SAM is always loaded as a secondary backend (`_override_backends["sam"]`) for guillemet tag rendering, regardless of the active model.

---

## Backends

### Pocket

**Class:** `PocketTTSBackend`
**Sample rate:** 24000 Hz (updated from model after load)
**Streaming:** yes

Kyutai's pocket-tts neural TTS. Supports true streaming inference with direct playback queue integration.

**Voice types:**

- Predefined names: `alba`, `marius`, `fantine`, `eponine`, `azelma`, etc. Passed directly to `model.get_state_for_audio_prompt(name)` — no URI construction needed.
- Safetensors paths: `/path/to/voice.safetensors` — passed through the same API call; pocket-tts resolves them internally.
- Any other string (URL, `hf://` URI) is also passed through directly.

**Voice state caching:** `_voice_states` dict caches loaded states by voice name. First call for a given voice loads it; subsequent calls return the cached state.

**Fallback chain:**
1. Try `get_state_for_audio_prompt(voice_name)`
2. On any exception (and if `voice_name != fallback_voice`): log and try `get_state_for_audio_prompt(fallback_voice)`
3. If fallback also fails: exception propagates

Default fallback voice: `fantine` (configurable via `fallback_voice`).

**Streaming modes:**

- Speed ~= 1.0 with queue: `_generate_streaming_direct` — raw chunks queued immediately.
- Speed != 1.0 with queue: `_generate_streaming_pipe` — chunks fed into a soundstretch subprocess. **On hold** — soundstretch produces unnatural output. Currently speed is set to 1.25 but the pipe path degrades quality.
- No queue: collect all chunks, concatenate, return array.

**Config keys:** `voice`, `fallback_voice`, `speed`, `lsd_decode_steps`, `noise_clamp`, `eos_threshold`, `frames_after_eos`

---

### Qwen3

**Class:** `Qwen3TTSBackend`
**Sample rate:** 24000 Hz (updated from model after load)
**Streaming:** disabled (`supports_streaming = False`) due to per-chunk volume inconsistency

Alibaba's Qwen3-TTS via mlx-audio. MLX-native, Apple Silicon only.

**Voice types:**

- Audio file paths (`.wav`, `.mp3`, `.flac`, `.ogg`, `.m4a`, `.aac`, `.opus`): used as `ref_audio` for in-context learning (ICL) voice cloning. **This is the only reliable way to get consistent voice.**
- Seed tags (`seed:N`): set `mx.random.seed(N)` before generation. **Seeds do NOT pin voice identity** — they only make identical text reproducible. Different text with the same seed produces noticeably different voice character because the voice emerges from autoregressive token sampling (`categorical_sampling` with top_k/top_p) and drifts with text content and length.

**Voice consistency — critical notes:**

Seeds are NOT sufficient for voice consistency. The Base model's `categorical_sampling` function is JIT-compiled (`@mx.compile`) with its own random state management. Even with `mx.random.seed(N)` called before every generate, the resulting voice varies significantly across different text inputs. Tested: same seed across 5 different sentences showed RMS variation of 0.025–0.062 (2.5x range). With ref_audio, the same test showed 0.058–0.078 (1.3x range).

The correct approach for consistent voice is **always provide ref_audio**. The Base model is designed for voice cloning via ICL — without a reference sample, voice identity is effectively random. Voice pool entries for qwen3 should be WAV file paths, not seed tags.

Some seeds also land on Chinese-trained speaker profiles, causing English text to be spoken with Chinese accent or numbers read in Chinese. This is because the model is multilingual (10 languages) and the random seed selects from the full speaker distribution.

**Voice resolution order** (`_resolve_voice`):

1. `seed:N` tag → seed-based generation, no ref_audio (unreliable voice identity)
2. Recognised audio file → ref_audio for ICL (reliable voice identity)
3. Unrecognised string (e.g. pocket safetensors, predefined name) → log warning, fall back to configured default
4. `None` → use configured default voice and seed

**Temperature:** Controls sampling randomness. Default 0.9 is too wild — voices shift dramatically. Set to 0.75 for more consistent output. Online recommendation is 0.8.

**Instruct parameter:** optional style/emotion string passed to the model. Can be set as a default in config (`instruct` key) or per-request via guillemet pipe syntax. Controls speaking style — e.g. `"calm and warm"`, `"enthusiastic"`.

**Speed:** The `speed` parameter is accepted by `model.generate()` but **ignored** — mlx-audio's source says "not directly supported yet". No native speed control. Soundstretch post-processing is on hold (sounds unnatural).

**Config keys:** `model_id`, `voice`, `voice_text`, `speed` (currently no-op), `seed`, `temperature`, `instruct`

---

### SAM

**Class:** `SAMBackend`
**Sample rate:** 22050 Hz
**Streaming:** no

1982 Commodore 64 formant synthesizer via the `samtts` package. Zero neural dependencies. Used as the secondary backend for guillemet-tagged inline voice switching.

Voice parameter is accepted for API compatibility but ignored — SAM has one voice.

**Post-processing pipeline:**

1. Convert 8-bit unsigned PCM (0–255, centre 128) to float32 (−1…+1)
2. Single-pole IIR lowpass filter (alpha=0.55) — smooths harsh 8-bit edges
3. Comb-filter reverb — 4 delay taps at ~20/40/60/80ms, decay=0.3, falloff=0.5
4. 10ms fade in/out — prevents clicks at segment boundaries
5. Volume scale (default 0.20) — matched to neural TTS output levels

When SAM audio is concatenated with neural TTS segments, it is resampled from 22050 Hz to the primary backend's rate (24000 Hz) and cross-faded at the boundary (8ms overlap).

**Config keys:** `speed` (1–255, SAM native; higher = slower), `pitch`, `mouth`, `throat`

---

## Voice Pool

Each backend config block can contain a `voice_pool` array. This is the set of voices available for per-repo voice assignment.

```json
"voice_pool": [
  "/path/to/voice.wav",
  "seed:7",
  "seed:42",
  "fantine"
]
```

Pool entries are backend-specific:

| Backend | Valid entry types |
|---------|-------------------|
| pocket | safetensors paths, predefined names (`fantine`, `alba`, etc.) |
| qwen3 | audio file paths (`.wav` etc.), seed tags (`seed:N`) |
| sam | (no pool — SAM has one voice) |

### How hooks select from the pool

Both `speak-response.py` and `pre-tool-speak.py` call `_get_repo_voice(cwd)`:

1. Run `git rev-parse --show-toplevel` in `cwd` to get the repo root. Fall back to `cwd` if not a git repo.
2. Read `~/.claude/tts-config.json`, get `active_model`, then `models.<active>.voice_pool`.
3. Hash the repo root path with SHA-256, take the first 8 hex chars as an integer.
4. `pool[hash % len(pool)]` — deterministic, stable per repo.

If `voice_pool` is empty or config is unreadable, returns `None` (use backend default).

The selected voice is wrapped in a guillemet tag before sending to the daemon:

```python
body_str = f"««{voice}»{body_str}»»"
```

---

## Guillemet Tag Syntax

Guillemet tags switch voice mid-text. The daemon's `_split_voice_segments` function parses them.

Outer delimiters: `««` (U+00AB U+00AB) and `»»` (U+00BB U+00BB).

### Forms

| Syntax | Result |
|--------|--------|
| `««text»»` | SAM voice (backward-compatible shorthand — no inner `»` separator) |
| `««voice»text»»` | Named voice or path on primary backend |
| `««2»text»»` | Pool index 2 resolved from config for active backend |
| `««voice\|instruct»text»»` | Voice + instruct (qwen3 only) |
| `««\|instruct»text»»` | Default voice, custom instruct |
| `««seed:42»text»»` | Seed-based voice on qwen3 |
| `««/path/voice.wav»text»»` | Audio file voice on qwen3 |

### Parsing rules

- Content between `««` and `»»` is scanned for an inner `»` (single).
- If found: split into `voice_id » text`. Voice part is parsed for `|` to extract instruct.
- If not found: SAM shorthand — entire content is the text, voice is `"sam"`.
- Empty `voice_id` (e.g. `««|instruct»text»»`) → `None` (use default voice).
- Digit-only `voice_id` → resolved via `_resolve_pool_index` to a pool entry.

Plain text between tags is rendered with the primary backend at default voice.

---

## Voice Resolution Flow

End-to-end for a single request:

```
hook (speak-response.py or pre-tool-speak.py)
  → _get_repo_voice(cwd)      # hash repo path → pool entry
  → wrap in guillemet tag     # ««voice»text»»
  → send to daemon (Unix socket SEQ command)

daemon
  → _split_voice_segments()   # parse guillemet tags → [(voice, instruct, text), ...]
  → _render_segments()        # for each segment:
      - voice == "sam"        → SAM backend, voice=None
      - voice is name/path    → primary backend, voice=that name
      - voice is None         → primary backend, voice=default_voice
      → backend.generate(text, speed=speed, voice=voice, instruct=instruct)
  → cross-fade and concatenate all segment audio
  → play
```

### Per-backend resolution at generate time

**Pocket:** voice string passed to `_get_voice_state(voice)` → `model.get_state_for_audio_prompt(voice)` → cached. Fallback to `fallback_voice` on error.

**Qwen3:** `_resolve_voice(voice)` → `(ref_audio, ref_text, seed)`:
- `seed:N` → seed, no ref_audio
- audio file → ref_audio path
- unrecognised → fall back to configured default

**SAM:** voice parameter ignored.

### Fallback chain summary

1. Hook: if `voice_pool` is empty → no guillemet wrapping → daemon uses backend default voice
2. Daemon: if pool index out of range → falls back to `"sam"`
3. Pocket: if named voice fails to load → `fallback_voice` (default: `fantine`)
4. Qwen3: if voice is unrecognised → configured default `voice` + `seed`
5. `_render_segments`: if `backend.generate()` raises `TypeError` on kwargs → retry with `(text, speed=speed)` only

---

## Instruct System (qwen3 only)

Qwen3-TTS accepts an optional `instruct` string that influences speaking style, emotion, and pacing. SAM and pocket ignore this parameter.

### Sources (in precedence order)

1. Per-segment guillemet pipe syntax: `««voice|instruct»text»»` or `««|instruct»text»»`
2. Default instruct from config: `models.qwen3.instruct`
3. Neither set → `None` (model default behaviour)

### Resolution in `_render_segments`

```python
use_instruct = instruct or default_instruct  # per-segment > default
gen_kwargs["instruct"] = use_instruct        # added only if truthy
```

`default_instruct` is passed into `_render_segments` from the request handler, sourced from config.

### Effect

The model interprets instruct as a free-text style directive. Examples: `"calm and warm"`, `"enthusiastic and upbeat"`, `"slow and clear"`. Behaviour is model-dependent — results vary with phrasing.

---

## Voice Encoding (qwen3)

**Script:** `scripts/encode_voice.py`

Pre-encodes a reference WAV into a `.safetensors` file containing speech tokens and (optionally) a speaker embedding. Intended for faster voice loading at generation time.

### Usage

```bash
python scripts/encode_voice.py input.wav \
    --text "Transcript of what's said in the audio" \
    --output my_voice.safetensors \
    --model mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit
```

### Output format

The `.safetensors` file contains:

| Key | Shape | Description |
|-----|-------|-------------|
| `ref_codes` | `[1, 16, ref_time]` | Speech tokens from the speech tokenizer |
| `speaker_embed` | `[1, enc_dim]` | Speaker embedding (omitted if model has no speaker encoder) |

Metadata fields: `ref_text`, `source_wav`, `audio_duration_s`, `sample_rate`, `model_id`.

### Current status

The backend does not yet use `.safetensors` files at generation time. `Qwen3TTSBackend._resolve_voice` only recognises audio files (`.wav`, `.mp3`, etc.) — a `.safetensors` path is treated as unrecognised and falls back to the default voice. Voice cloning currently re-encodes from WAV on every request by passing `ref_audio` directly to `model.generate()`. The encode script exists for future use when the backend is updated to load pre-encoded prompts.
