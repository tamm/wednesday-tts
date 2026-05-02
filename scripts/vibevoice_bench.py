"""Offline benchmark for the VibeVoice backend.

Drives the same generator + ringbuffer as `play_streaming`, but instead of
opening an audio device it captures every chunk + pseudo-callback pull into
metrics and writes the rendered audio to a WAV file. Lets us see RTF,
chunk-arrival jitter, and how often the ringbuffer would have under-run on
the real device — without having to listen to it.

Usage:
    .venv/bin/python scripts/vibevoice_bench.py \
        --text "test sentence here" --voice Carter

Output:
    /tmp/vibevoice_bench.wav  — full rendered audio
    Stdout:
        chunk timeline    — t_arrival_ms, samples, ms_audio
        pull timeline     — t_ms, available_at_pull, would_underrun
        summary           — total audio s, gen s, RTF, max gap, underruns
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from wednesday_tts.server.backends.vibevoice import VibeVoiceBackend  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--text",
        default=(
            "Silence aware stretching is now in place. When the buffer runs low, "
            "the callback freezes on silent samples instead of cutting words."
        ),
    )
    p.add_argument("--voice", default="Carter")
    p.add_argument("--prebuffer", type=float, default=0.8)
    p.add_argument("--out", default="/tmp/vibevoice_bench.wav")
    p.add_argument("--device", default=None, help="mps | cuda | cpu (auto)")
    args = p.parse_args()

    backend = VibeVoiceBackend(
        voice=args.voice, prebuffer_sec=args.prebuffer, device=args.device
    )
    print(f"[bench] loading model on {backend._resolve_device()}…", flush=True)
    backend.load()
    print("[bench] loaded. running warm-up pass…", flush=True)
    # Warm up: cold first prefill on MPS hits ~7s; subsequent runs are fast.
    # Discard the warm-up timing entirely.
    _wu = backend.generate("Hi.", voice=args.voice)
    if _wu is None:
        print("[bench] warm-up returned no audio", flush=True)
    else:
        print(f"[bench] warm-up done ({_wu.size / backend.sample_rate:.2f}s).", flush=True)

    # Use the model.generate streaming path directly (same as play_streaming
    # does internally), but capture chunks into our own ringbuffer simulation.
    from vibevoice.modular.streamer import AudioStreamer  # type: ignore

    voice_path = backend._voice_path(args.voice)
    prefilled = backend._load_voice(voice_path)
    inputs = backend._prepare_inputs(args.text, prefilled)

    streamer = AudioStreamer(batch_size=1, stop_signal=None, timeout=None)
    sr = backend.sample_rate

    chunk_log: list[tuple[float, int]] = []  # (t_arrival_s_since_start, samples)
    audio_chunks: list[np.ndarray] = []

    t0 = time.time()
    gen_error: list[BaseException] = []

    def _run() -> None:
        try:
            with backend._lock:
                backend._model.generate(
                    **inputs,
                    max_new_tokens=None,
                    cfg_scale=backend._cfg_scale,
                    tokenizer=backend._processor.tokenizer,
                    generation_config={"do_sample": False},
                    verbose=False,
                    audio_streamer=streamer,
                    all_prefilled_outputs=__import__("copy").deepcopy(prefilled),
                )
        except BaseException as exc:
            gen_error.append(exc)
        finally:
            streamer.end()

    th = threading.Thread(target=_run, daemon=True)
    th.start()

    print("[bench] consuming chunks…", flush=True)
    for chunk in streamer.get_stream(0):
        arr = chunk.float().numpy().squeeze()
        if arr.ndim > 1:
            arr = arr.mean(axis=0).astype(np.float32)
        if arr.size == 0:
            continue
        arr = arr.astype(np.float32, copy=False)
        chunk_log.append((time.time() - t0, int(arr.size)))
        audio_chunks.append(arr)

    th.join(timeout=5.0)
    elapsed = time.time() - t0
    if not audio_chunks:
        print("[bench] no audio produced", flush=True)
        if gen_error:
            print(f"[bench] error: {gen_error[0]}")
        return

    audio = np.concatenate(audio_chunks)
    duration_s = audio.size / sr
    rtf = elapsed / duration_s if duration_s > 0 else float("inf")

    # Save WAV.
    try:
        import soundfile as sf  # type: ignore

        sf.write(args.out, audio, sr)
        print(f"[bench] wrote {args.out}", flush=True)
    except Exception as exc:
        print(f"[bench] could not write wav: {exc}", flush=True)

    # Chunk timeline.
    print("\n=== Chunks ===")
    print(
        f"{'idx':>4}  {'t_arr_ms':>9}  "
        f"{'gap_ms':>8}  {'samples':>8}  {'ms_audio':>9}"
    )
    last_t = 0.0
    for i, (t_arr, n) in enumerate(chunk_log):
        gap_ms = (t_arr - last_t) * 1000
        print(
            f"{i:>4}  {t_arr * 1000:>9.0f}  {gap_ms:>8.0f}  "
            f"{n:>8d}  {n / sr * 1000:>9.0f}"
        )
        last_t = t_arr

    # Simulate playback drain at real-time, given pre-buffer threshold.
    prebuffer_samples = int(args.prebuffer * sr)
    cumulative_audio = 0
    chunks_total = []
    for t_arr, n in chunk_log:
        cumulative_audio += n
        chunks_total.append((t_arr, cumulative_audio))

    # Find when prebuffer fills.
    start_t = None
    for t_arr, total in chunks_total:
        if total >= prebuffer_samples:
            start_t = t_arr
            break
    if start_t is None:
        # Generation finished before prebuffer filled.
        start_t = chunks_total[-1][0]

    # Now simulate: at each ms after start_t, we need cumulative samples to
    # reach (now - start_t)*sr. If actual cumulative is less, we'd underrun.
    underruns = 0
    max_underrun_ms = 0
    sim_steps = int((elapsed - start_t) * 1000) + 1
    for ms in range(sim_steps):
        t_now = start_t + ms / 1000
        wanted = int(ms / 1000 * sr)
        # find latest cumulative as of t_now
        actual = 0
        for t_arr, total in chunks_total:
            if t_arr <= t_now:
                actual = total
            else:
                break
        if wanted > actual:
            underruns += 1
            short_ms = (wanted - actual) / sr * 1000
            if short_ms > max_underrun_ms:
                max_underrun_ms = short_ms

    print("\n=== Summary ===")
    print(f"audio duration:        {duration_s:.2f} s")
    print(f"generation elapsed:    {elapsed:.2f} s")
    print(f"RTF (gen/audio):       {rtf:.2f}")
    print(f"chunks:                {len(chunk_log)}")
    print(f"prebuffer fill at:     {start_t:.2f} s")
    print(f"first-audio latency:   {start_t * 1000:.0f} ms")
    print(f"sim underrun ms:       {underruns} ms ({underruns / 1000:.2f} s)")
    print(f"max underrun depth:    {max_underrun_ms:.0f} ms")
    print(f"would playback finish: {duration_s + start_t:.2f} s vs gen {elapsed:.2f} s")

    if gen_error:
        print(f"[bench] gen error: {gen_error[0]}")


if __name__ == "__main__":
    main()
