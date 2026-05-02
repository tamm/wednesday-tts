"""VibeVoice-Realtime backend — Microsoft's streaming TTS (~200ms first audio)."""

from __future__ import annotations

import copy
import glob
import os
import queue
import threading
import time

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend, soundstretch_tempo

_LOG_QUEUE: queue.Queue[str | None] = queue.Queue(maxsize=1024)


def _log_worker() -> None:
    while True:
        msg = _LOG_QUEUE.get()
        if msg is None:
            break
        try:
            print(msg, flush=True)
        except Exception:  # noqa: BLE001 — logging must never crash the worker
            pass


_LOG_THREAD = threading.Thread(target=_log_worker, name="vibevoice-log", daemon=True)
_LOG_THREAD.start()


def _alog(msg: str) -> None:
    """Non-blocking log — drops the message if the queue is saturated."""
    try:
        _LOG_QUEUE.put_nowait(msg)
    except queue.Full:
        pass


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
    supports_direct_play = True
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
                print(
                    f"[vibevoice] flash_attention_2 failed ({exc}), retrying with sdpa", flush=True
                )
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
        # Voice prompts are local .pt files produced by VibeVoice's own pipeline
        # (cached prefill state, not just tensors), under ~/dev/VibeVoice. Trusted source.
        prefilled = torch.load(voice_path, map_location=target, weights_only=False)  # nosec B614
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
        audio_context=None,
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

        # Ring buffer state — all guarded by `cond`.
        ring = np.zeros(ring_capacity, dtype=np.float32)
        write_idx = 0  # absolute count of samples written (mod ring_capacity for indexing)
        read_idx = 0  # absolute count of samples read
        gen_done = False
        cond = threading.Condition()
        # Underrun stats — incremented from the audio callback when the
        # ring didn't have enough samples to fill a callback block.
        underrun_events = 0
        underrun_samples = 0
        # Per-callback samples for visibility: every block received, what
        # fraction of frames we delivered, lead at that moment, and how
        # long we spent in the lock. Bounded to recent history so we can
        # dump it after each utterance without unbounded memory.
        cb_log: list[tuple[float, int, int, int, float]] = []
        cb_log_max = 4096
        # Detect frame-count anomalies (PortAudio asking for unusual block
        # sizes — possibly a sign of resampler weirdness).
        cb_frames_seen: dict[int, int] = {}
        # Absolute sample positions where the drain thread saw end-of-chunk
        # silence. The drain thread uses these to decide where to inject
        # silence padding into the ring; the audio callback never inspects
        # them — playback never parks or stretches at these.
        pause_marks: list[int] = []

        def _available() -> int:
            return write_idx - read_idx

        def _ring_write(arr: np.ndarray, pause_offset: int | None) -> None:
            nonlocal write_idx
            n = arr.size
            with cond:
                # Back-pressure if ring is full.
                while _available() + n > ring_capacity and not (stop_check and stop_check()):
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
            nonlocal read_idx, underrun_events, underrun_samples
            cb_t0 = time.perf_counter()
            cb_frames_seen[frames] = cb_frames_seen.get(frames, 0) + 1
            # Note any PortAudio status flags (input/output underflow/overflow).
            status_flags = int(status) if status else 0
            with cond:
                lock_acquired_at = time.perf_counter()
                avail = _available()
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
                    outdata[take:, 0] = 0.0
                    underrun_events += 1
                    underrun_samples += frames - take
            cb_t1 = time.perf_counter()
            lock_wait_us = (lock_acquired_at - cb_t0) * 1e6
            cb_total_us = (cb_t1 - cb_t0) * 1e6
            # Record only the events we'd want to inspect: anomalies
            # (underrun, slow callback, lock contention, status flags).
            if take < frames or status_flags or cb_total_us > 1500 or lock_wait_us > 500:
                if len(cb_log) < cb_log_max:
                    cb_log.append(
                        (
                            cb_t0,
                            frames,
                            take,
                            avail,
                            cb_total_us,
                        )
                    )

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

        # Predictive silence injection — see docs/streaming-buffer-pacing.md.
        # Inject silence ONLY at chunk boundaries that ended in trailing
        # silence (sample-aligned zero crossings — no clicks). Decision is
        # driven by an EMA of buffer fill, not instantaneous fill, so we
        # don't react to momentary jitter. Three regions:
        #   critical: fill_ema < critical_sec → long hold at this pause
        #   comfort:  fill_ema < comfort_sec  → short hold at this pause
        #   healthy:  otherwise               → no padding
        # Disabled — set non-zero to re-enable predictive pad-at-pause.
        # Kept in place (non-destructive) because the blocksize=1024 fix
        # solved the popping; this layer was symptom-bandaging.
        critical_sec = 0.0
        comfort_sec = 0.0
        ema_alpha = 0.3
        small_pad_ms = 0.0
        large_pad_ms = 0.0
        fill_ema_sec = 0.0  # smoothed buffer fill, in seconds
        total_injected_sec = 0.0
        # Padding is only meaningful once the audio callback is actually
        # draining the ring. Pre-stream-start, buffer fill is low simply
        # because we're still filling — that's expected, not starvation.
        playback_started = threading.Event()

        def _inject_silence(ms: float) -> None:
            n = max(1, int(ms * sr / 1000))
            silence = np.zeros(n, dtype=np.float32)
            _ring_write(silence, None)

        # Synth wall-clock: from first generator chunk to last, plus total
        # generated audio in samples. Lets us report a synth-only RTF that
        # actually reflects model speed (separate from playback wall time).
        synth_t_first: list[float] = []
        synth_t_last: list[float] = []
        synth_audio_samples: list[int] = [0]

        def _drain_streamer() -> None:
            nonlocal gen_done, fill_ema_sec, total_injected_sec
            chunk_idx = 0
            last_chunk_end = time.time()
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
                    now = time.time()
                    gen_ms = (now - last_chunk_end) * 1000.0
                    if not synth_t_first:
                        synth_t_first.append(now)
                    synth_t_last[:] = [now]
                    synth_audio_samples[0] += arr32.size
                    _ring_write(arr32, pause_off)
                    with cond:
                        fill_sec = _available() / sr

                    # Update EMA — first sample seeds it directly so we don't
                    # start at 0 and bias the first decisions low.
                    if chunk_idx == 0:
                        fill_ema_sec = fill_sec
                    else:
                        fill_ema_sec = ema_alpha * fill_sec + (1 - ema_alpha) * fill_ema_sec

                    # Pause-only injection: only act if THIS chunk ended in
                    # silence AND playback has started (no point padding
                    # against starvation when nothing is draining yet).
                    pad_tag = "-"
                    if pause_off is not None and playback_started.is_set():
                        pad_ms = 0.0
                        if fill_ema_sec < critical_sec:
                            pad_ms = large_pad_ms
                        elif fill_ema_sec < comfort_sec:
                            pad_ms = small_pad_ms
                        if pad_ms > 0:
                            _inject_silence(pad_ms)
                            total_injected_sec += pad_ms / 1000.0
                            # Bump EMA optimistically so we don't double-pad
                            # the next chunk on the same dip.
                            fill_ema_sec += pad_ms / 1000.0
                            pad_tag = f"{pad_ms:.0f}ms"

                    chunk_dur_ms = arr32.size * 1000.0 / sr
                    inst_rtf = gen_ms / chunk_dur_ms if chunk_dur_ms > 0 else 0.0
                    pause_tag = "-"
                    if pause_off is not None:
                        pause_ms = (arr32.size - pause_off) * 1000.0 / sr
                        pause_tag = f"{pause_ms:.0f}ms"
                    _alog(
                        f"[vibevoice-chunk] msg_id={msg_id} i={chunk_idx} "
                        f"gen={gen_ms:.0f}ms dur={chunk_dur_ms:.0f}ms "
                        f"rtf={inst_rtf:.2f} lead={fill_sec:.2f}s "
                        f"pause={pause_tag} pad={pad_tag} "
                        f"underruns={underrun_events} cb_anomalies={len(cb_log)}"
                    )
                    chunk_idx += 1
                    last_chunk_end = now
            except Exception as exc:  # noqa: BLE001
                print(f"[vibevoice-play] streamer drain error: {exc}", flush=True)
            finally:
                with cond:
                    gen_done = True
                    cond.notify_all()

        gen_thread = threading.Thread(target=_run, name="vibevoice-gen", daemon=True)
        drain_thread = threading.Thread(target=_drain_streamer, name="vibevoice-drain", daemon=True)
        t0 = time.time()
        gen_thread.start()
        drain_thread.start()

        # Wait for either the first natural pause OR the hard ceiling.
        # Require a minimum fill regardless of pause detection — the first
        # chunk often ends in a tiny 30ms silence that's not a real pause,
        # and starting playback at fill≈0.1s causes underrun pops for the
        # first several chunks until the buffer catches up.
        min_start_fill = int(0.5 * sr)
        with cond:
            while (
                _available() < min_start_fill
                and not (pause_marks and _available() >= min_start_fill)
                and _available() < prebuffer_max_samples
                and not gen_done
                and not (stop_check and stop_check())
            ):
                cond.wait(timeout=0.1)
            prebuffer_filled = _available()
            saw_pause = bool(pause_marks)
        if stop_check and stop_check():
            return False

        # audio_context (optional, supplied by daemon) lets us cooperate with
        # the daemon's PortAudio lock and device-change machinery so that
        # mid-utterance device swaps (headphones ↔ speakers) reopen the
        # stream against the current default device instead of silently
        # writing to a stale device.
        ac_lock = getattr(audio_context, "lock", None) if audio_context else None
        ac_device_changed = (
            getattr(audio_context, "device_changed", None) if audio_context else None
        )

        def _open_stream():
            opener = lambda: sd.OutputStream(  # noqa: E731
                samplerate=sr,
                channels=1,
                dtype="float32",
                blocksize=1024,
                latency="low",
                callback=_audio_callback,
            )
            try:
                if ac_lock is not None:
                    with ac_lock:
                        # Re-init under the daemon's lock so the device list
                        # is fresh and we don't race with the daemon's own
                        # _terminate/_initialize calls.
                        try:
                            sd._terminate()
                            sd._initialize()
                        except Exception as exc:  # noqa: BLE001
                            _alog(f"[vibevoice-play] sd reinit warn: {exc}")
                        return opener()
                return opener()
            except Exception as exc:  # noqa: BLE001
                _alog(f"[vibevoice-play] open stream failed: {exc}")
                return None

        stream = _open_stream()
        if stream is None:
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

        stream.start()
        playback_started.set()
        try:
            # Wait until generation is done AND ringbuffer drained, or stop.
            # Also watch for device-change events: on each one, close the
            # current stream and open a fresh one against the new default.
            while True:
                if stop_check and stop_check():
                    break
                if ac_device_changed is not None and ac_device_changed.is_set():
                    _alog("[vibevoice-play] device changed — reopening stream")
                    try:
                        stream.stop()
                        stream.close()
                    except Exception as exc:  # noqa: BLE001
                        _alog(f"[vibevoice-play] close-on-swap warn: {exc}")
                    ac_device_changed.clear()
                    new_stream = _open_stream()
                    if new_stream is None:
                        break
                    stream = new_stream
                    stream.start()
                with cond:
                    if gen_done and _available() <= 0:
                        break
                    cond.wait(timeout=0.2)
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        gen_thread.join(timeout=5.0)
        drain_thread.join(timeout=5.0)
        ok = not gen_error and not (stop_check and stop_check())
        if gen_error:
            print(f"[vibevoice-play] generate error: {gen_error[0]}", flush=True)

        elapsed = time.time() - t0
        # write_idx is now an absolute sample count (no wrap), so this is exact.
        played_sec = write_idx / sr if write_idx else 0.0
        gen_audio_sec = synth_audio_samples[0] / sr if synth_audio_samples[0] else 0.0
        # synth_elapsed = drain wall time across all yielded chunks (model only).
        if synth_t_first and synth_t_last:
            synth_elapsed = synth_t_last[0] - synth_t_first[0]
        else:
            synth_elapsed = 0.0
        synth_rtf = f"{synth_elapsed / gen_audio_sec:.2f}" if gen_audio_sec > 0 else "n/a"
        play_rtf = f"{elapsed / played_sec:.2f}" if played_sec > 0 else "n/a"
        underrun_ms = underrun_samples * 1000.0 / sr
        # Summarise frame-size distribution (PortAudio's block sizes).
        frames_summary = (
            ", ".join(f"{n}x{cnt}" for n, cnt in sorted(cb_frames_seen.items())) or "(none)"
        )
        # Field names: `rtf` = synth-only (matches batch RTF semantics);
        # `play_rtf` = wall/playback ratio (~1.0 for streaming, kept for parity).
        print(
            f"[vibevoice-play] done elapsed={elapsed:.1f}s audio={played_sec:.1f}s "
            f"synth_elapsed={synth_elapsed:.2f}s rtf={synth_rtf} play_rtf={play_rtf} "
            f"injected={total_injected_sec:.2f}s "
            f"underruns={underrun_events} ({underrun_ms:.0f}ms) "
            f"cb_blocks=[{frames_summary}] anomalies={len(cb_log)} "
            f"voice={os.path.basename(voice_path)} ok={ok}",
            flush=True,
        )
        # Dump first 20 callback anomalies if any — these are the moments
        # most likely to contain the cause of audible pops.
        for ev in cb_log[:20]:
            ts, fr, took, av, us = ev
            rel = ts - t0
            print(
                f"[vibevoice-cb] t+{rel:.3f}s frames={fr} delivered={took} "
                f"avail_at_entry={av} ({av / sr * 1000.0:.0f}ms) "
                f"cb_total={us:.0f}us",
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
