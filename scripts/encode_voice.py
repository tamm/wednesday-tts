#!/usr/bin/env python3
"""Encode a reference WAV into a reusable .safetensors voice prompt for Qwen3-TTS.

Usage:
    python scripts/encode_voice.py input.wav --text "Transcript of what's said" -o my_voice.safetensors
    python scripts/encode_voice.py input.wav --text "Transcript" --model mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit

The transcript (--text) is stored as metadata and used at generation time for ICL.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def encode_voice(
    wav_path: str,
    ref_text: str,
    output_path: str,
    model_id: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
) -> None:
    import mlx.core as mx
    from mlx_audio.tts import load as load_model
    from mlx_audio.utils import load_audio

    print(f"Loading model {model_id}...")
    t0 = time.time()
    model = load_model(model_id)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    print(f"Loading audio from {wav_path}...")
    ref_audio = load_audio(wav_path, sample_rate=24000)
    duration = ref_audio.shape[0] / 24000
    print(f"Audio: {duration:.1f}s ({ref_audio.shape[0]} samples at 24kHz)")

    # Encode to codec tokens
    print("Encoding speech tokens...")
    t0 = time.time()
    audio_batched = ref_audio[None, None, :]  # [1, 1, samples]
    ref_codes = model.speech_tokenizer.encode(audio_batched)  # [1, 16, ref_time]
    mx.eval(ref_codes)
    print(f"Speech tokens: shape {ref_codes.shape} in {time.time() - t0:.1f}s")

    # Extract speaker embedding
    arrays: dict[str, mx.array] = {"ref_codes": ref_codes}

    if model.speaker_encoder is not None:
        print("Extracting speaker embedding...")
        t0 = time.time()
        speaker_embed = model.extract_speaker_embedding(ref_audio)  # [1, enc_dim]
        mx.eval(speaker_embed)
        arrays["speaker_embed"] = speaker_embed
        print(f"Speaker embed: shape {speaker_embed.shape} in {time.time() - t0:.1f}s")
    else:
        print("No speaker encoder in this model — skipping speaker embedding")

    # Save
    mx.save_safetensors(
        output_path,
        arrays,
        metadata={
            "ref_text": ref_text,
            "source_wav": Path(wav_path).name,
            "audio_duration_s": f"{duration:.2f}",
            "sample_rate": "24000",
            "model_id": model_id,
        },
    )
    print(f"Saved voice prompt to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encode a reference WAV into a Qwen3-TTS voice prompt (.safetensors)"
    )
    parser.add_argument("wav", help="Path to reference audio WAV file")
    parser.add_argument(
        "--text",
        "-t",
        required=True,
        help="Transcript of the reference audio (required for ICL quality)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output .safetensors path (default: same name as input with .safetensors)",
    )
    parser.add_argument(
        "--model", "-m", default="mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit", help="MLX model ID"
    )
    args = parser.parse_args()

    wav_path = Path(args.wav)
    if not wav_path.exists():
        print(f"Error: {wav_path} not found", file=sys.stderr)
        sys.exit(1)

    output = args.output or str(wav_path.with_suffix(".safetensors"))

    encode_voice(
        wav_path=str(wav_path),
        ref_text=args.text,
        output_path=output,
        model_id=args.model,
    )


if __name__ == "__main__":
    main()
