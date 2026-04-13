#!/usr/bin/env python3
"""Generate candidate voice clips using seed-based generation.

Run once to audition voices. Pick the best clip, use it as ref_audio in tts-config.json.

Usage:
    cd ~/dev/wednesday-tts
    .venv/bin/python scripts/gen_voice_candidates.py
"""

import os

import mlx.core as mx
import numpy as np
import soundfile as sf
from mlx_audio.tts import load

MODEL_ID = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
TEXT = (
    "When the sunlight strikes random raindrops in the air, they act like a prism "
    "and form a rainbow. The rainbow is a division of white light into many beautiful "
    "colors. These take the shape of a long round arch, with its path high above, "
    "and its two ends apparently beyond the horizon."
)
SEEDS = [78461, 75448, 93949, 29668, 59815, 86790, 54103, 98829, 12352, 44876, 25212]
OUTDIR = os.path.expanduser("~/Music/voices/qwen3-candidates")

os.makedirs(OUTDIR, exist_ok=True)
model = load(MODEL_ID)

for seed in SEEDS:
    mx.random.seed(seed)
    chunks = list(
        model.generate(
            text=TEXT,
            temperature=0.75,
            repetition_penalty=1.2,
            top_p=0.85,
            top_k=30,
            split_pattern="",
        )
    )
    arrays = [np.array(r.audio, dtype=np.float32).squeeze() for r in chunks]
    audio = np.concatenate(arrays)
    path = f"{OUTDIR}/seed_{seed}.wav"
    sf.write(path, audio, 24000)
    print(f"Wrote {path} ({len(audio) / 24000:.1f}s)")

print("\nAudition with:")
print(f'  for f in {OUTDIR}/*.wav; do echo "$f"; afplay "$f"; done')
