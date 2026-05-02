# Voice System Spec

Reference document for backend-specific voice behaviour and config.

## Config Layout

Config lives at `~/.claude/tts-config.json`.

Top-level keys commonly used by the daemon:

- `active_model`
- `error_chime`
- `voice_command_chirp`
- `models`

Each backend has its own block under `models.<backend>`.

For the current daemon, voice-related keys that matter most are:

- `voice_pool`
- `default_voice`
- `guillemet_voice`
- backend-native `voice`
- backend-native `voice_text`

## Request Voice vs Inline Voice

Two concepts matter:

- request voice
  - chosen once per request from `session_id`, `voice_hash`, `voice_pool`,
    and `default_voice`
- inline voice
  - chosen inside the text with guillemet tags

Plain text segments use the request voice. Tagged segments can override it.

## Shared Voice Entry Shape

Where the daemon expects structured voice entries, it uses:

```json
{
  "name": "warm-a",
  "voice": "/path/to/voice-or-reference",
  "voice_text": "Optional transcript"
}
```

`voice_text` is only meaningful for backends that use reference transcripts.

## Backend Matrix

### Pocket

- class: `PocketTTSBackend`
- sample rate: 24000 Hz
- streaming: yes
- direct-play: no

Voice input forms:

- predefined pocket names such as `fantine`, `alba`, `marius`
- local voice paths that pocket can resolve directly
- other prompt identifiers passed through to pocket

Notes:

- The backend caches voice states by the exact `voice` string.
- On voice load failure it falls back to `fallback_voice`.
- `voice_pool` entries can be dicts whose `voice` values are passed through to
  pocket's `get_state_for_audio_prompt()`.

Relevant config keys:

- `voice`
- `fallback_voice`
- `speed`
- `lsd_decode_steps`
- `noise_clamp`
- `eos_threshold`
- `frames_after_eos`

### VibeVoice

- class: `VibeVoiceBackend`
- sample rate: 24000 Hz
- streaming: yes
- direct-play: yes

Voice input forms:

- `.pt` prompt path
- speaker name resolved inside `voices_dir`

Notes:

- The backend searches `~/dev/VibeVoice/demo/voices/streaming_model` by default.
- Voice prompts are upstream-generated `.pt` files, not WAV clones.
- This backend currently has the most specialized playback/logging behaviour.

Relevant config keys:

- `model_path`
- `voice`
- `voices_dir`
- `device`
- `cfg_scale`
- `ddpm_steps`
- `speed`
- `prebuffer_sec`
- `ringbuffer_sec`

### Qwen3

- class: `Qwen3TTSBackend`
- sample rate: 24000 Hz
- streaming: disabled in daemon use
- direct-play: no

Voice input forms:

- reference-audio path
- structured dict entry with `voice` and optional `voice_text`
- `seed:N` tags are still understood by the backend, but are not reliable for
  stable voice identity across different text

Notes:

- Best voice consistency comes from reference audio plus transcript.
- `instruct` is meaningful here and flows from guillemet tags.

Relevant config keys:

- `model_id`
- `voice`
- `voice_text`
- `speed`
- `seed`
- `temperature`
- `instruct`

### MOSS

- class: `MossNanoBackend`
- sample rate: backend-dependent runtime output
- streaming: no
- direct-play: no

Voice input forms:

- preset speaker names such as `Junhao`
- optional clone prompt audio, depending on runtime availability

Relevant config keys:

- `voice`
- `prompt_audio_path`
- `model_dir`
- `cpu_threads`
- `max_new_frames`
- `voice_clone_max_text_tokens`
- `speed`
- `seed`
- `enable_wetext`

### Kokoro

- class: `KokoroBackend`
- sample rate: typically 24000 Hz
- streaming: no
- direct-play: no

Voice input forms:

- built-in Kokoro voice names such as `af_bella`

Relevant config keys:

- `voice`
- `speed`
- `samplerate`

### SAM

- class: `SAMBackend`
- sample rate: 22050 Hz
- streaming: no
- direct-play: no

Notes:

- Usually used as the default guillemet voice.
- Ignores structured voice selection beyond the special `sam` backend switch.
- When mixed with neural segments, audio is resampled and cross-faded by the
  daemon.

Relevant config keys:

- `speed`
- `pitch`
- `mouth`
- `throat`

### Soprano

- class: `SopranoBackend`
- streaming: no
- direct-play: no

Relevant config keys:

- `backend`
- `device`
- `temperature`
- `top_p`
- `repetition_penalty`
- `samplerate`
- `venv_path`

### Chatterbox

- class: `ChatterboxBackend`
- streaming: no
- direct-play: no

Relevant config keys:

- `device`
- `voice_clone`
- `samplerate`
- `venv_path`

## Voice Pool Guidance

For consistency, prefer structured entries with stable names:

```json
"voice_pool": [
  {
    "name": "repo-a",
    "voice": "fantine"
  },
  {
    "name": "repo-b",
    "voice": "/path/to/ref.wav",
    "voice_text": "Reference transcript"
  }
]
```

Why:

- guillemet named lookup uses `name`
- logs are clearer when the daemon can emit a stable label
- different backends can still interpret `voice` in their own way

## Guillemet Voice

`««text»»` uses:

- `guillemet_voice` if configured
- otherwise `sam`

`guillemet_voice` may be:

- `"sam"`
- a pool-entry `name`
- a structured voice entry dict

If a named `guillemet_voice` cannot be resolved, the daemon falls back to the
active backend's `default_voice`.
