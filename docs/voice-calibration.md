# Qwen3-TTS Voice Calibration

How to select, generate, and configure a stable reference voice for the Qwen3-TTS backend.

---

## Why ref_audio, not seeds

Seeds (`seed:N`) do not pin voice identity. They only make generation reproducible for identical text. Different text with the same seed produces a different voice, because voice character emerges from autoregressive token sampling and drifts with text content and length.

Measured: same seed across 5 different sentences → RMS variation 0.025–0.062 (2.5x range). With ref_audio → 0.058–0.078 (1.3x range).

Additional hazard: some seeds map onto Chinese speaker profiles. English text ends up with a Chinese accent or numbers spoken in Chinese. The model covers 10 languages and the seed selects from the full speaker distribution.

The correct approach is always `ref_audio`. The Base model is designed for voice cloning via in-context learning (ICL). Without a reference clip, voice identity is random.

---

## Generating candidate voices

The goal is to generate several candidate clips with different seeds, listen to each, and keep the best one as the permanent reference.

### Step 1 — Generate candidates

Run the daemon directly via the client, or use a one-off Python script. The daemon isn't needed for this — call mlx-audio directly:

```python
#!/usr/bin/env python3
"""Generate candidate voice clips using seed-based generation.

Run once to audition voices. Pick the best clip, then use it as ref_audio.
"""
import numpy as np
import soundfile as sf
from mlx_audio.tts import load

MODEL_ID = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
TEXT = "This is a test of the voice quality. I want to hear how this voice sounds on a range of sentence types, including questions and statements."
SEEDS = [7, 42, 99, 137, 256, 512, 1000, 2048]
OUTDIR = "/tmp/voice-candidates"

import os, mlx.core as mx
os.makedirs(OUTDIR, exist_ok=True)
model = load(MODEL_ID)

for seed in SEEDS:
    mx.random.seed(seed)
    chunks = list(model.generate(text=TEXT, temperature=0.75))
    arrays = [np.array(r.audio, dtype=np.float32).squeeze() for r in chunks]
    audio = np.concatenate(arrays)
    path = f"{OUTDIR}/seed_{seed}.wav"
    sf.write(path, audio, 24000)
    print(f"Wrote {path}")
```

```bash
python /tmp/gen_candidates.py
```

### Step 2 — Listen and pick

```bash
for f in /tmp/voice-candidates/*.wav; do echo "$f"; afplay "$f"; done
```

Or open the folder in Finder and use Quick Look.

Pick the clip with the clearest, most natural-sounding voice in English. Avoid clips with any accent drift or unusual prosody.

### Step 3 — Save the chosen clip

```bash
mkdir -p ~/dev/wednesday-tts/sounds/voices/qwen3
cp /tmp/voice-candidates/seed_42.wav ~/dev/wednesday-tts/sounds/voices/qwen3/default.wav
```

Replace `seed_42.wav` with whichever file you chose.

---

## Configuring the chosen voice

In `~/.claude/tts-config.json`, under `models.qwen3`:

```json
"qwen3": {
  "voice": "/Users/yourname/dev/wednesday-tts/sounds/voices/qwen3/default.wav",
  "voice_text": "This is a test of the voice quality. I want to hear how this voice sounds on a range of sentence types, including questions and statements.",
  "seed": 7,
  "temperature": 0.75
}
```

- `voice` — absolute path to the chosen WAV. Use an absolute path; the daemon does not expand `~`.
- `voice_text` — exact transcript of what is said in the reference clip. Providing this improves ICL quality. If you used the script above, it is the value of `TEXT`.
- `seed` — still set a fixed seed. It won't determine voice identity (ref_audio does that), but it prevents the non-voice aspects of generation from wandering between calls.

### How the backend picks this up

`Qwen3TTSBackend._resolve_voice()` resolves in this order:

1. `seed:N` tag → seed only, no ref_audio (don't use this for normal operation)
2. Recognised audio file path → used as `ref_audio` for ICL cloning
3. Unrecognised string → warning, falls back to configured default
4. `None` → uses `self._voice` (the `voice` key from config) and `self._seed`

For daily use, no voice is specified per-request, so path 4 applies. The configured WAV is passed as `ref_audio` to `model.generate()` on every call.

---

## Temperature tuning

Default: `0.75`.

| Value | Effect |
|-------|--------|
| 0.9 (original default) | High variance — voice drifts noticeably between sentences |
| 0.8 (online recommendation) | Better, still some drift |
| 0.75 | Good balance: stable voice character, still natural prosody |
| 0.6 | Too flat — prosody becomes robotic |

Lower temperature reduces sampling randomness. Too low and the voice sounds monotone. Too high and you get unpredictable voice shifts, especially on short or unusual text.

Set in config:

```json
"temperature": 0.75
```

---

## Sampling parameters for audio quality

Beyond temperature, three parameters control degenerate (garbled/repetitive) audio. All are passed to `model.generate()` and configurable in `tts-config.json` under `models.qwen3`.

| Parameter | mlx-audio default | Description |
|-----------|-------------------|-------------|
| `repetition_penalty` | 1.05 | Penalises repeated speech tokens. Higher = less garbled loops. |
| `top_p` | 1.0 | Nucleus sampling cutoff. Lower = cuts low-probability garbage tokens. |
| `top_k` | 50 | Candidate pool size. Lower = tighter, less noise. |

### Per-hardware recommendations

4-bit quantisation introduces sampling noise. Weaker hardware needs stricter settings to compensate.

| Hardware | repetition_penalty | top_p | top_k | Notes |
|----------|-------------------|-------|-------|-------|
| M1 (Erin) | 1.35 | 0.7 | 20 | Aggressive. Required for 0.6B-4bit at real-time. |
| Newer chips | 1.2 | 0.85 | 30 | Can afford looser settings, especially with 6bit/8bit models. |

Higher precision models (6bit, 8bit) produce less degenerate audio at the same settings, so you can relax the penalties.

### Voice drift across chunks

With daemon-side chunking, each text chunk is an independent `model.generate()` call. ICL voice cloning re-runs from scratch per chunk, so the voice can shift between chunks — like the same person switching dialects.

This is worse on 4-bit and with shorter chunks. Potential mitigations:
- **Streaming mode** (`supports_streaming = True`): one continuous generation call, so the model maintains voice state throughout. Currently disabled due to per-chunk volume inconsistency, but may work now that voice pinning is in place.
- **Longer chunks**: more context per generation = more stable voice, but higher latency.
- **Higher precision model**: less quantisation noise = less drift.

---

## Repeating the process on a new machine

1. Install the model if not cached: first `daemon.load()` call will download it.
2. Run the candidate generation script above. Adjust `SEEDS` to taste.
3. Listen, pick, copy to `sounds/voices/qwen3/default.wav`.
4. Update `~/.claude/tts-config.json` with the absolute path.
5. Restart the daemon:

```bash
launchctl kickstart -k gui/$(id -u)/com.tamm.wednesday-tts
```

6. Test:

```bash
echo "This is a voice test." | python -m wednesday_tts.client.api
```

If you want a completely different voice character, regenerate with different seeds or change the `TEXT` sample to something that emphasises the vocal qualities you want (shorter sentences → less prosody complexity).

---

## Notes

- The reference WAV is loaded on every `generate()` call (re-encoded from raw audio by mlx-audio). Pre-encoding to `.safetensors` via `scripts/encode_voice.py` is not yet supported at generate time — the backend only recognises audio file extensions.
- Voice pool entries for qwen3 in `tts-config.json` should be WAV paths, not `seed:N` tags. Seeds in the pool will produce inconsistent voice across different repos.
