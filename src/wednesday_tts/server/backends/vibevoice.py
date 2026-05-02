"""VibeVoice-Realtime backend — Microsoft's streaming TTS (~200ms first audio)."""

from __future__ import annotations

import copy
import glob
import os
import threading
import time

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend, soundstretch_tempo


def _chunk_pause_offset(
    arr: np.ndarray,
    sr: int,
    tail_ms: float = 30.0,
    window_ms: float = 5.0,
    min_silence_ms: float = 20.0,
    rms_thresh: float = 0.005,
) -> int | None:
    """If the chunk ends in silence, return the sample offset where that
    trailing silence starts. Otherwise return None.

    Scans a 5ms sliding RMS window across the last ``tail_ms`` ms of
    ``arr``. A run of at least ``min_silence_ms`` ms of consecutive
    sub-threshold windows touching the end-of-chunk counts as a pause
    boundary.
    """
    if arr.size == 0:
        return None
    win = max(1, int(window_ms * sr / 1000))
    tail = min(arr.size, int(tail_ms * sr / 1000))
    if tail < win:
        return None
    seg = arr[-tail:]
    silent_samples = 0
    pos = seg.size
    while pos >= win:
        rms = float(np.sqrt(np.mean(seg[pos - win : pos] ** 2)))
        if rms <= rms_thresh:
            silent_samples += win
            pos -= win
        else:
            break
    if silent_samples * 1000 / sr >= min_silence_ms:
        return arr.size - silent_samples
    return None


class VibeVoiceBackend(TTSBackend):
    """VibeVoice-Realtime-0.5B — torch / MPS streaming TTS.

    Voice prompts are pre-baked .pt files from the upstream repo
    (`demo/voices/streaming_model/*.pt`) — single-speaker only.

    Config keys (from tts-config.json models.vibevoice):
        model_path  — HF id or local dir (default: microsoft/VibeVoice-Realtime-0.5B)
        voice       — speaker name OR full path to a .pt voice prompt
        voices_dir  — directory holding .pt prompts (defaults to ~/dev/VibeVoice/demo/voices/streaming_model)
        device      — "mps" | "cuda" | "cpu" (auto-detected if omitted)
        cfg_scale   — classifier-free guidance scale (default 1.5)
        ddpm_steps  — diffusion steps (default 5)
        speed       — tempo multiplier; applied via soundstretch
    """

    sample_rate = 24000
    supports_streaming = True
    # Streaming chunks are mid-utterance — no trim/fade/pad. Default values
    # would slice 5ms off each end and add 50ms of silence, creating an
    # audible seam at every coalesced-buffer boundary.
    anti_click_shape = {
        "trim_start": 0.0,
        "trim_end": 0.0,
        "pad_start": 0.0,
        "pad_end": 0.0,
        "fade_in": 0.0,
        "fade_out": 0.0,
    }

    def __init__(
        self,
        model_path: str = "microsoft/VibeVoice-Realtime-0.5B",
        voice: str = "Carter",
        voices_dir: str | None = None,
        device: str | None = None,
        cfg_scale: float = 1.5,
        ddpm_steps: int = 5,
        speed: float = DEFAULT_SPEED,
        prebuffer_sec: float = 0.5,
        ringbuffer_sec: float = 30.0,
    ) -> None:
        self._model_path = model_path
        self._voice = voice
        self._voices_dir = os.path.expanduser(
            voices_dir or "~/dev/VibeVoice/demo/voices/streaming_model"
        )
        self._device = device
        self._cfg_scale = float(cfg_scale)
        self._ddpm_steps = int(ddpm_steps)
        self._speed = speed
        self._prebuffer_sec = float(prebuffer_sec)
        self._ringbuffer_sec = float(ringbuffer_sec)
        self._model = None
        self._processor = None
        self._voice_cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Model load ────────────────────────────────────────────────────

    def _resolve_device(self) -> str:
        if self._device:
            return self._device
        try:
            import torch  # type: ignore[import]

            if torch.backends.mps.is_available():
                return "mps"
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        return "cpu"

    def load(self) -> None:
        import torch  # type: ignore[import]
        from vibevoice.modular.modeling_vibevoice_streaming_inference import (  # type: ignore[import]
            VibeVoiceStreamingForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_streaming_processor import (  # type: ignore[import]
            VibeVoiceStreamingProcessor,
        )

        self._device = self._resolve_device()
        if self._device == "mps":
            dtype = torch.float32
            attn = "sdpa"
        elif self._device == "cuda":
            dtype = torch.bfloat16
            attn = "flash_attention_2"
        else:
            dtype = torch.float32
            attn = "sdpa"

        print(
            f"[vibevoice] loading {self._model_path} device={self._device} "
            f"dtype={dtype} attn={attn}",
            flush=True,
        )

        self._processor = VibeVoiceStreamingProcessor.from_pretrained(self._model_path)

        try:
            if self._device == "mps":
                model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
                    self._model_path, torch_dtype=dtype, attn_implementation=attn, device_map=None
                )
                model.to("mps")
            else:
                model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
                    self._model_path,
                    torch_dtype=dtype,
                    attn_implementation=attn,
                    device_map=self._device,
                )
        except Exception as exc:
            if attn == "flash_attention_2":
                print(f"[vibevoice] flash_attention_2 failed ({exc}), retrying with sdpa", flush=True)
                model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
                    self._model_path,
                    torch_dtype=dtype,
                    attn_implementation="sdpa",
                    device_map=self._device if self._device != "mps" else None,
                )
                if self._device == "mps":
                    model.to("mps")
            else:
                raise

        model.eval()
        model.set_ddpm_inference_steps(num_steps=self._ddpm_steps)
        self._model = model

    # ── Voice resolution ─────────────────────────────────────────────

    def _voice_path(self, voice: str | None) -> str:
        """Resolve a voice name or path to a concrete .pt path."""
        candidate = voice or self._voice
        if isinstance(candidate, dict):
            candidate = candidate.get("voice") or self._voice
        if os.path.isfile(candidate) and candidate.endswith(".pt"):
            return candidate
        # Search voices_dir for a name match
        name = os.path.basename(str(candidate)).lower()
        if not self._voices_dir or not os.path.isdir(self._voices_dir):
            raise FileNotFoundError(
                f"[vibevoice] voices_dir not found: {self._voices_dir}. "
                "Clone https://github.com/microsoft/VibeVoice and point voices_dir at "
                "demo/voices/streaming_model/."
            )
        pts = glob.glob(os.path.join(self._voices_dir, "**", "*.pt"), recursive=True)
        # exact match (filename without extension)
        for p in pts:
            stem = os.path.splitext(os.path.basename(p))[0].lower()
            if stem == name:
                return p
        # partial match
        for p in pts:
            stem = os.path.splitext(os.path.basename(p))[0].lower()
            if name in stem or stem in name:
                return p
        if pts:
            print(f"[vibevoice] no match for {candidate!r}, using {pts[0]}", flush=True)
            return pts[0]
        raise FileNotFoundError(f"[vibevoice] no .pt voice prompts in {self._voices_dir}")

    def _load_voice(self, voice_path: str):
        """Load and cache the prefilled voice prompt tensors on the active device."""
        import torch  # type: ignore[import]

        cached = self._voice_cache.get(voice_path)
        if cached is not None:
            return cached
        target = self._device if self._device != "cpu" else "cpu"
        prefilled = torch.load(voice_path, map_location=target, weights_only=False)
        self._voice_cache[voice_path] = prefilled
        return prefilled

    def _prepare_inputs(self, text: str, prefilled):
        import torch  # type: ignore[import]

        # Light cleanup matching upstream demo
        clean = text.replace("’", "'").replace("“", '"').replace("”", '"')
        inputs = self._processor.process_input_with_cached_prompt(
            text=clean,
            cached_prompt=prefilled,
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        target = self._device if self._device != "cpu" else "cpu"
        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(target)
        return inputs

    # ── Generation ──────────────────────────────────────────────────

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> np.ndarray | None:
        if self._model is None or self._processor is None:
            raise RuntimeError("VibeVoiceBackend not loaded — call load() first")
        if not text or not text.strip():
            return None

        use_speed = speed if speed is not None else self._speed
        voice_path = self._voice_path(voice)
        prefilled = self._load_voice(voice_path)
        inputs = self._prepare_inputs(text, prefilled)

        t0 = time.time()
        try:
            with self._lock:
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=None,
                    cfg_scale=self._cfg_scale,
                    tokenizer=self._processor.tokenizer,
                    generation_config={"do_sample": False},
                    verbose=False,
                    all_prefilled_outputs=copy.deepcopy(prefilled),
                )
        except Exception as exc:
            print(f"[vibevoice] generate error: {exc}", flush=True)
            return None

        speech = outputs.speech_outputs[0] if outputs.speech_outputs else None
        if speech is None:
            print("[vibevoice] no audio in output", flush=True)
            return None
        arr = speech.detach().to("cpu").float().numpy().squeeze()
        if arr.ndim > 1:
            arr = arr.mean(axis=0).astype(np.float32)
        if arr.size == 0:
            return None

        if abs(use_speed - 1.0) > 0.01:
            arr = soundstretch_tempo(arr, self.sample_rate, use_speed)

        elapsed = time.time() - t0
        duration = arr.size / self.sample_rate
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[vibevoice] {duration:.1f}s in {elapsed:.1f}s (RTF {rtf}, voice={os.path.basename(voice_path)})",
            flush=True,
        )
        return arr.astype(np.float32, copy=False)

    def play_streaming(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        stop_check=None,
        msg_id: int = -1,
    ) -> bool:
        """Pre-buffered, callback-driven playback straight from the model.

        Architecture mirrors Microsoft's web demo (see demo/web/index.html):
        a numpy ringbuffer fed by the generator thread, drained by a
        sounddevice OutputStream callback. Pre-buffers ``prebuffer_sec`` of
        audio before starting the device so chunk-arrival jitter and
        diffusion-step pauses don't underrun the device.

        Returns True on a clean finish, False on stop/error.
        """
        if self._model is None or self._processor is None:
            raise RuntimeError("VibeVoiceBackend not loaded — call load() first")
        if not text or not text.strip():
            return False

        import sounddevice as sd  # type: ignore[import]
        from vibevoice.modular.streamer import AudioStreamer  # type: ignore[import]

        voice_path = self._voice_path(voice)
        prefilled = self._load_voice(voice_path)
        inputs = self._prepare_inputs(text, prefilled)

        sr = self.sample_rate
        # Hard ceiling for prebuffer-without-pause: if speech is so dense
        # we never see a chunk-end pause, start anyway after this much.
        prebuffer_max_samples = max(1, int(self._prebuffer_sec * sr))
        ring_capacity = max(prebuffer_max_samples * 4, int(self._ringbuffer_sec * sr))
        # When ring drops below low_water AND we're at a pause mark, the
        # callback parks on silence to let the generator catch up.
        low_water = int(0.5 * sr)
        catchup_water = low_water * 2  # resume normal play once buffer recovers
        max_stretch_samples = int(1.5 * sr)  # cap pause extension at 1.5s

        # Ring buffer state — all guarded by `cond`.
        ring = np.zeros(ring_capacity, dtype=np.float32)
        write_idx = 0  # absolute count of samples written (mod ring_capacity for indexing)
        read_idx = 0  # absolute count of samples read
        gen_done = False
        cond = threading.Condition()
        # Absolute sample positions where it's safe to stretch a pause.
        # Each entry is the absolute write count at the start of the
        # trailing-silence run inside a chunk.
        pause_marks: list[int] = []
        stretched_samples = 0
        currently_stretching = False
        stretch_run = 0  # how long we've been parked on the current pause

        def _available() -> int:
            return write_idx - read_idx

        def _ring_write(arr: np.ndarray, pause_offset: int | None) -> None:
            nonlocal write_idx
            n = arr.size
            with cond:
                # Back-pressure if ring is full.
                while _available() + n > ring_capacity and not (
                    stop_check and stop_check()
                ):
                    cond.wait(timeout=0.5)
                if stop_check and stop_check():
                    return
                start_abs = write_idx
                idx0 = start_abs % ring_capacity
                end = idx0 + n
                if end <= ring_capacity:
                    ring[idx0:end] = arr
                else:
                    first = ring_capacity - idx0
                    ring[idx0:] = arr[:first]
                    ring[: n - first] = arr[first:]
                if pause_offset is not None:
                    pause_marks.append(start_abs + pause_offset)
                write_idx = start_abs + n
                cond.notify_all()

        def _audio_callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
            nonlocal read_idx, stretched_samples, currently_stretching, stretch_run
            with cond:
                avail = _available()

                # Drop pause marks we've already passed.
                while pause_marks and pause_marks[0] < read_idx:
                    pause_marks.pop(0)

                # Pause-aware stretching: if buffer is low AND we're at
                # (or just past) the next pause mark, park on silence
                # until either buffer recovers or we hit the stretch cap.
                if not gen_done:
                    next_mark = pause_marks[0] if pause_marks else None
                    at_pause = (
                        next_mark is not None
                        and read_idx >= next_mark
                    )
                    if currently_stretching:
                        # Continue stretching unless buffer recovered or capped.
                        if avail >= catchup_water or stretch_run >= max_stretch_samples:
                            currently_stretching = False
                            stretch_run = 0
                        else:
                            outdata[:, 0] = 0.0
                            stretched_samples += frames
                            stretch_run += frames
                            return
                    elif avail < low_water and at_pause:
                        # Enter stretching mode.
                        currently_stretching = True
                        stretch_run = frames
                        stretched_samples += frames
                        outdata[:, 0] = 0.0
                        return

                take = min(frames, avail)
                if take > 0:
                    idx0 = read_idx % ring_capacity
                    end = idx0 + take
                    if end <= ring_capacity:
                        outdata[:take, 0] = ring[idx0:end]
                    else:
                        first = ring_capacity - idx0
                        outdata[:first, 0] = ring[idx0:]
                        outdata[first:take, 0] = ring[: take - first]
                    read_idx += take
                    cond.notify_all()
                if take < frames:
                    outdata[take:, 0] = 0.0  # underrun → silence

        streamer = AudioStreamer(batch_size=1, stop_signal=None, timeout=None)
        gen_error: list[BaseException] = []

        def _run() -> None:
            nonlocal gen_done
            try:
                with self._lock:
                    self._model.generate(
                        **inputs,
                        max_new_tokens=None,
                        cfg_scale=self._cfg_scale,
                        tokenizer=self._processor.tokenizer,
                        generation_config={"do_sample": False},
                        verbose=False,
                        audio_streamer=streamer,
                        stop_check_fn=(lambda: bool(stop_check and stop_check())),
                        all_prefilled_outputs=copy.deepcopy(prefilled),
                    )
            except BaseException as exc:  # noqa: BLE001
                gen_error.append(exc)
            finally:
                streamer.end()

        def _drain_streamer() -> None:
            nonlocal gen_done
            try:
                for chunk in streamer.get_stream(0):
                    if stop_check and stop_check():
                        break
                    arr = chunk.float().numpy().squeeze()
                    if arr.ndim > 1:
                        arr = arr.mean(axis=0).astype(np.float32)
                    if arr.size == 0:
                        continue
                    arr32 = arr.astype(np.float32, copy=False)
                    pause_off = _chunk_pause_offset(arr32, sr)
                    _ring_write(arr32, pause_off)
            except Exception as exc:  # noqa: BLE001
                print(f"[vibevoice-play] streamer drain error: {exc}", flush=True)
            finally:
                with cond:
                    gen_done = True
                    cond.notify_all()

        gen_thread = threading.Thread(
            target=_run, name="vibevoice-gen", daemon=True
        )
        drain_thread = threading.Thread(
            target=_drain_streamer, name="vibevoice-drain", daemon=True
        )
        t0 = time.time()
        gen_thread.start()
        drain_thread.start()

        # Wait for either the first natural pause OR the hard ceiling.
        with cond:
            while (
                not pause_marks
                and _available() < prebuffer_max_samples
                and not gen_done
                and not (stop_check and stop_check())
            ):
                cond.wait(timeout=0.1)
            prebuffer_filled = _available()
            saw_pause = bool(pause_marks)
        if stop_check and stop_check():
            return False

        try:
            stream = sd.OutputStream(
                samplerate=sr,
                channels=1,
                dtype="float32",
                blocksize=0,
                latency="low",
                callback=_audio_callback,
            )
        except Exception as exc:
            print(f"[vibevoice-play] failed to open OutputStream: {exc}", flush=True)
            with cond:
                gen_done = True
                cond.notify_all()
            gen_thread.join(timeout=5.0)
            drain_thread.join(timeout=5.0)
            return False

        first_audio_ms = (time.time() - t0) * 1000.0
        print(
            f"[vibevoice-play] prebuffer={prebuffer_filled / sr:.2f}s "
            f"saw_pause={saw_pause} first-audio={first_audio_ms:.0f}ms",
            flush=True,
        )

        with stream:
            # Wait until generation is done AND ringbuffer drained, or stop.
            while True:
                if stop_check and stop_check():
                    break
                with cond:
                    if gen_done and _available() <= 0:
                        break
                    cond.wait(timeout=0.2)

        gen_thread.join(timeout=5.0)
        drain_thread.join(timeout=5.0)
        ok = not gen_error and not (stop_check and stop_check())
        if gen_error:
            print(f"[vibevoice-play] generate error: {gen_error[0]}", flush=True)

        elapsed = time.time() - t0
        # write_idx is now an absolute sample count (no wrap), so this is exact.
        played_sec = write_idx / sr if write_idx else 0.0
        rtf = f"{elapsed / played_sec:.2f}" if played_sec > 0 else "n/a"
        print(
            f"[vibevoice-play] done elapsed={elapsed:.1f}s audio={played_sec:.1f}s "
            f"rtf={rtf} stretched={stretched_samples / sr:.2f}s "
            f"voice={os.path.basename(voice_path)} ok={ok}",
            flush=True,
        )
        return ok

    def generate_streaming(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        playback_queue=None,
        stop_check=None,
        msg_id: int = -1,
    ) -> np.ndarray | None:
        """Stream chunks via VibeVoice's AudioStreamer — first audio in ~200ms.

        Generation runs on a background thread; we drain the streamer's queue and
        push each chunk into playback_queue (or accumulate them if None).
        """
        if self._model is None or self._processor is None:
            raise RuntimeError("VibeVoiceBackend not loaded — call load() first")
        if not text or not text.strip():
            return None

        from vibevoice.modular.streamer import AudioStreamer  # type: ignore[import]

        voice_path = self._voice_path(voice)
        prefilled = self._load_voice(voice_path)
        inputs = self._prepare_inputs(text, prefilled)

        streamer = AudioStreamer(batch_size=1, stop_signal=None, timeout=None)

        gen_error: list[BaseException] = []

        def _run() -> None:
            try:
                with self._lock:
                    self._model.generate(
                        **inputs,
                        max_new_tokens=None,
                        cfg_scale=self._cfg_scale,
                        tokenizer=self._processor.tokenizer,
                        generation_config={"do_sample": False},
                        verbose=False,
                        audio_streamer=streamer,
                        stop_check_fn=(lambda: bool(stop_check and stop_check())),
                        all_prefilled_outputs=copy.deepcopy(prefilled),
                    )
            except BaseException as exc:  # noqa: BLE001 — propagate via flag
                gen_error.append(exc)
                streamer.end()

        worker = threading.Thread(target=_run, name="vibevoice-gen", daemon=True)

        t0 = time.time()
        n_chunks = 0
        total_samples = 0
        collected: list[np.ndarray] = []

        worker.start()
        sample_iter = streamer.get_stream(0)
        # Coalesce small streamer chunks (~130ms) into ~1s buffers before
        # handing to playback_queue. The spatial-stream path waits for each
        # buffer to finish playing before grabbing the next, so smaller
        # chunks = more gaps. ~1s is a good balance between first-audio
        # latency and smoothness.
        _coalesce_target = int(self.sample_rate * 3.0)
        _buf: list[np.ndarray] = []
        _buf_samples = 0
        _first_emitted = False

        def _flush_buf() -> None:
            nonlocal _buf, _buf_samples, _first_emitted, n_chunks
            if not _buf:
                return
            merged = np.concatenate(_buf) if len(_buf) > 1 else _buf[0]
            _buf = []
            _buf_samples = 0
            n_chunks += 1
            if playback_queue is not None:
                subtitle = text if not _first_emitted else None
                _first_emitted = True
                playback_queue.put((merged, subtitle, msg_id))
            else:
                collected.append(merged)

        try:
            for chunk in sample_iter:
                if stop_check and stop_check():
                    break
                arr = chunk.float().numpy().squeeze()
                if arr.ndim > 1:
                    arr = arr.mean(axis=0).astype(np.float32)
                if arr.size == 0:
                    continue
                arr = arr.astype(np.float32, copy=False)
                total_samples += arr.size
                _buf.append(arr)
                _buf_samples += arr.size
                if _buf_samples >= _coalesce_target:
                    _flush_buf()
            _flush_buf()
        except Exception as exc:
            print(f"[vibevoice-stream] iter error: {exc}", flush=True)
            _flush_buf()

        worker.join(timeout=2.0)
        if gen_error:
            print(f"[vibevoice-stream] generate error: {gen_error[0]}", flush=True)

        elapsed = time.time() - t0
        duration = total_samples / self.sample_rate if total_samples else 0.0
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[vibevoice-stream] {n_chunks} chunks, {duration:.1f}s in {elapsed:.1f}s "
            f"(RTF {rtf}, voice={os.path.basename(voice_path)})",
            flush=True,
        )

        if playback_queue is not None:
            return None
        if not collected:
            return None
        return np.concatenate(collected) if len(collected) > 1 else collected[0]
