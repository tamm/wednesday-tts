# DWP Voice

Pre-computed Pocket TTS voice state for the bundled Aussie male voice.

## Files

| File                                                     | Notes                              |
| -------------------------------------------------------- | ---------------------------------- |
| `unmute_kyutai_dwp_aussie_male_enhanced_32k.safetensors` | Use this in config — loads in <1ms |

- **Speaker:** DWP, from [Kyutai TTS Voices](https://huggingface.co/kyutai/tts-voices/blob/main/voice-donations/dwp_enhanced.wav) via [Unmute.sh](https://unmute.sh/voice-donation)
- **License:** CC0 — explicitly donated for TTS use, safe to distribute
- **Processing:** Denoised, loudness-normalised to -21 LUFS

---

## Why safetensors?

Pocket TTS re-encodes the voice WAV into a KV-cache embedding on every TTS call.
Pre-baking that with `export-voice` cuts voice load from ~1050ms on a typical CPU to near zero.

```
pocket-tts export-voice input.wav output.safetensors
```

See the [pocket-tts export-voice docs](https://kyutai-labs.github.io/pocket-tts/CLI%20Commands/export_voice/)
for full options including loading directly from HuggingFace URLs.

To re-export from source: download from the HuggingFace link above, then run `pocket-tts export-voice`.

---

## Using a different voice

**Do not use voice cloning to impersonate real people.** Only clone a voice you have
explicit consent to use. This applies to public figures, colleagues, family — anyone.
The [Pocket TTS model terms](https://huggingface.co/kyutai/pocket-tts) prohibit
impersonation, deception, and presenting generated audio as genuine recordings.

Any clean WAV clip of 3–10 seconds works. Longer clips give marginally better cloning
but add latency; 3–5s is the sweet spot.

**FFmpeg enhancement chain** (recommended before exporting):

```
ffmpeg -i input.wav -ar 32000 \
  -af "highpass=f=80,lowpass=f=15500,afftdn=nf=-20,acompressor=threshold=-25dB:ratio=2:attack=10:release=200:makeup=1,loudnorm=I=-21:TP=-1.5:LRA=11" \
  output_enhanced.wav
```

Always set `-ar 32000` on output — loudnorm upsamples internally to 192kHz and you'll
get a surprise if you forget.

Then:

```
pocket-tts export-voice output_enhanced.wav my-voice.safetensors
```

Point `voice` in `~/.claude/tts-config.json` at the new `.safetensors` file.

---

## Aussie female equivalent

There isn't a CC0-licensed female Australian voice with clear TTS consent we can ship.
Available datasets like GLOBE V2 (curated from Mozilla Common Voice) are CC0, but the
speakers consented to ASR research — not voice cloning. Legally fine; ethically grey.
