"""Qwen3-TTS backend — Alibaba's multilingual TTS via mlx-audio on Apple Silicon."""

from __future__ import annotations

import os
import queue
import threading
import time

import numpy as np

from .base import DEFAULT_SPEED, TTSBackend

_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

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


_LOG_THREAD = threading.Thread(target=_log_worker, name="qwen3-log", daemon=True)
_LOG_THREAD.start()


def _alog(msg: str) -> None:
    """Non-blocking log — drops the message if the queue is saturated."""
    try:
        _LOG_QUEUE.put_nowait(msg)
    except queue.Full:
        pass


def _is_audio_file(path: str) -> bool:
    """Check if path points to an existing audio file with a supported extension."""
    return os.path.isfile(path) and os.path.splitext(path)[1].lower() in _AUDIO_EXTS


def _model_tag(model_id: str) -> str:
    """Short, log-friendly tag derived from the HF model_id.

    e.g. 'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit' -> 'qwen3-pro' (1.7B)
         'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit' -> 'qwen3-light' (0.6B)
    Anything we can't classify falls back to 'qwen3'.
    """
    if "1.7B" in model_id:
        return "qwen3-pro"
    if "0.6B" in model_id:
        return "qwen3-light"
    return "qwen3"


class Qwen3TTSBackend(TTSBackend):
    """Qwen3-TTS via mlx-audio (MLX-native, Apple Silicon optimised).

    Config keys (from tts-config.json models.qwen3):
        model_id    — mlx-community model ID
                      (default: mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit)
        voice       — path to reference audio WAV for voice cloning (optional)
        voice_text  — transcription of the reference audio (optional, improves quality)
        speed       — native speed multiplier (handled by the model, not soundstretch)
        seed        — fixed random seed for reproducible output (int, optional)
        instruct    — style/emotion instruction string (optional)
        streaming   — enable DIRECT-PLAY streaming mode (default: false)
        prebuffer_sec   — seconds of audio to buffer before starting playback (default: 0.5)
        ringbuffer_sec  — ring-buffer capacity in seconds (default: 30.0)

    Voice pool entries (in tts-config.json models.qwen3.voice_pool) can be:
        - Audio file paths: "/path/to/voice.wav" — used as ref_audio for ICL cloning
        - Objects with voice_text: {"voice": "/path/to.wav", "voice_text": "transcript"}
        - Seed tags: "seed:42" — deterministic seed-based voice generation
    """

    sample_rate = 24000

    def __init__(
        self,
        model_id: str = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        voice: str | None = None,
        voice_text: str | None = None,
        speed: float = DEFAULT_SPEED,
        seed: int | None = None,
        instruct: str = "",
        temperature: float = 0.75,
        repetition_penalty: float = 1.2,
        top_p: float = 0.85,
        top_k: int = 30,
        streaming: bool = False,
        prebuffer_sec: float = 0.5,
        ringbuffer_sec: float = 30.0,
    ) -> None:
        self._model_id = model_id or os.environ.get(
            "QWEN3_TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit"
        )
        self._voice = voice  # path to default reference audio WAV
        self._voice_text = voice_text  # transcription of default reference audio
        self._speed = speed
        self._seed = seed if seed is not None else 7  # always pin voice
        self._temperature = temperature
        self._repetition_penalty = repetition_penalty
        self._top_p = top_p
        self._top_k = top_k
        self._instruct = instruct
        self._streaming = streaming
        self._prebuffer_sec = float(prebuffer_sec)
        self._ringbuffer_sec = float(ringbuffer_sec)
        self._model = None
        self._lock = threading.Lock()
        self._tag = _model_tag(self._model_id)
        # Set streaming capability flags based on config.
        # When streaming=True, opt into DIRECT-PLAY (same path as vibevoice).
        # When streaming=False, stay on BATCH path — supports_streaming=False
        # prevents the daemon from even attempting STREAM or DIRECT-PLAY.
        self.supports_streaming = streaming
        self.supports_direct_play = streaming

    def _resolve_voice(self, voice: str | None) -> tuple[str | None, str | None, int]:
        """Resolve a voice parameter into (ref_audio, ref_text, seed).

        Voice resolution order:
        1. voice is a seed tag ("seed:42") → use that seed with configured default ref_audio
        2. voice is a supported audio file → use as ref_audio with fixed seed
        3. voice is something unrecognised (pocket safetensors, predefined name, etc.)
           → log warning, fall back to configured default voice
        4. voice is None → use configured default voice with fixed seed

        Seed is NEVER None — always returns a valid int to prevent random voice.

        Returns:
            (ref_audio_path | None, ref_text | None, seed)
        """
        if voice is not None:
            # Dict entry from voice_pool: {"voice": "path", "voice_text": "transcript"}
            if isinstance(voice, dict):
                v_path = voice.get("voice", "")
                v_text = voice.get("voice_text")
                if _is_audio_file(v_path):
                    return v_path, v_text, self._seed
                # Dict but no valid audio — fall through to default

            # Supported audio file (string path)
            if isinstance(voice, str) and _is_audio_file(voice):
                ref_text = self._voice_text if voice == self._voice else None
                return voice, ref_text, self._seed

            # Unrecognised — fall back to default
            if not isinstance(voice, dict):
                print(
                    f"[qwen3] voice {voice!r} not recognised (not audio, not seed:N), "
                    f"using default",
                    flush=True,
                )

        # Default: configured voice with fixed seed
        if self._voice and _is_audio_file(self._voice):
            return self._voice, self._voice_text, self._seed
        return None, None, self._seed

    def load(self) -> None:
        from mlx_audio.tts import load  # type: ignore[import]

        self._model = load(self._model_id)
        if hasattr(self._model, "sample_rate"):
            self.sample_rate = self._model.sample_rate

    def generate(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        instruct: str | None = None,
    ) -> np.ndarray | None:
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        ref_audio, ref_text, seed = self._resolve_voice(voice)
        use_instruct = instruct or self._instruct or None
        print(
            f"[qwen3] resolve: voice={voice!r} → ref_audio={ref_audio!r}, "
            f"seed={seed}, instruct={use_instruct!r}",
            flush=True,
        )

        t0 = time.time()

        try:
            with self._lock:
                import mlx.core as mx  # type: ignore[import]

                mx.random.seed(seed)

                chunks = list(
                    self._model.generate(
                        text=text,
                        speed=use_speed,
                        temperature=self._temperature,
                        repetition_penalty=self._repetition_penalty,
                        top_p=self._top_p,
                        top_k=self._top_k,
                        ref_audio=ref_audio,
                        ref_text=ref_text,
                        instruct=use_instruct,
                        split_pattern="",  # we handle chunking in the daemon
                    )
                )

            if not chunks:
                print(f"[qwen3] generate returned no chunks for {len(text)} chars", flush=True)
                return None

            # Collect audio from all GenerationResult segments
            arrays = []
            for result in chunks:
                audio = result.audio
                arr = np.array(audio, dtype=np.float32)
                if arr.ndim > 1:
                    arr = arr.squeeze()
                arrays.append(arr)

                if result.sample_rate and result.sample_rate != self.sample_rate:
                    self.sample_rate = result.sample_rate

            combined = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
            elapsed = time.time() - t0
            duration = len(combined) / self.sample_rate
            voice_desc = f"ref={ref_audio}" if ref_audio else f"seed={seed}"
            print(
                f"[{self._tag}] generated {duration:.1f}s audio in {elapsed:.1f}s "
                f"(RTF {elapsed / duration:.2f}, {voice_desc}, model={self._model_id})",
                flush=True,
            )
            return combined

        except Exception as exc:
            print(f"[qwen3] generate error: {exc}", flush=True)
            return None

    def play_streaming(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        stop_check=None,
        msg_id: int = -1,
        audio_context=None,
    ) -> bool:
        """Pre-buffered, callback-driven DIRECT-PLAY streaming for Qwen3-TTS.

        Architecture mirrors vibevoice.VibeVoiceBackend.play_streaming:
        - mlx-audio generator runs on a background thread, yielding chunks
          via stream=True
        - A drain thread pulls chunks from the generator and writes them into
          a numpy ring-buffer
        - A sounddevice OutputStream callback drains the ring-buffer in
          real-time
        - Playback begins once prebuffer_sec seconds are buffered (or
          generation finishes, whichever is first)

        Returns True on a clean finish, False on stop/error.
        """
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")
        if not text or not text.strip():
            return False

        import sounddevice as sd  # type: ignore[import]

        ref_audio, ref_text, seed = self._resolve_voice(voice)
        use_instruct = self._instruct or None
        sr = self.sample_rate

        prebuffer_max_samples = max(1, int(self._prebuffer_sec * sr))
        ring_capacity = max(prebuffer_max_samples * 4, int(self._ringbuffer_sec * sr))

        # Ring buffer state — all guarded by `cond`.
        ring = np.zeros(ring_capacity, dtype=np.float32)
        write_idx = 0  # absolute sample count written
        read_idx = 0   # absolute sample count read
        gen_done = False
        cond = threading.Condition()
        underrun_events = 0
        underrun_samples = 0
        cb_log: list[tuple[float, int, int, int, float]] = []
        cb_log_max = 4096
        cb_frames_seen: dict[int, int] = {}

        def _available() -> int:
            return write_idx - read_idx

        def _ring_write(arr: np.ndarray) -> None:
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
                idx0 = write_idx % ring_capacity
                end = idx0 + n
                if end <= ring_capacity:
                    ring[idx0:end] = arr
                else:
                    first = ring_capacity - idx0
                    ring[idx0:] = arr[:first]
                    ring[: n - first] = arr[first:]
                write_idx += n
                cond.notify_all()

        def _audio_callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
            nonlocal read_idx, underrun_events, underrun_samples
            cb_t0 = time.perf_counter()
            cb_frames_seen[frames] = cb_frames_seen.get(frames, 0) + 1
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
            if (
                take < frames
                or status_flags
                or cb_total_us > 1500
                or lock_wait_us > 500
            ):
                if len(cb_log) < cb_log_max:
                    cb_log.append((cb_t0, frames, take, avail, cb_total_us))

        gen_error: list[BaseException] = []
        # Synth wall-clock + audio sample count tracked by the gen thread so
        # we can report a synth-only RTF in the done line (separate from the
        # wall/playback ratio which always sits near 1.0 for streaming).
        synth_t_first: list[float] = []
        synth_t_last: list[float] = []
        synth_audio_samples: list[int] = [0]

        def _run_gen() -> None:
            nonlocal gen_done
            try:
                with self._lock:
                    import mlx.core as mx  # type: ignore[import]

                    mx.random.seed(seed)

                    for result in self._model.generate(
                        text=text,
                        temperature=self._temperature,
                        repetition_penalty=self._repetition_penalty,
                        top_p=self._top_p,
                        top_k=self._top_k,
                        ref_audio=ref_audio,
                        ref_text=ref_text,
                        instruct=use_instruct,
                        split_pattern="",
                        stream=True,
                        streaming_interval=1.5,
                    ):
                        if stop_check and stop_check():
                            break
                        arr = np.array(result.audio, dtype=np.float32)
                        if arr.ndim > 1:
                            arr = arr.squeeze()
                        if arr.size == 0:
                            continue
                        if result.sample_rate and result.sample_rate != self.sample_rate:
                            self.sample_rate = result.sample_rate
                        now = time.time()
                        if not synth_t_first:
                            synth_t_first.append(now)
                        synth_t_last[:] = [now]
                        synth_audio_samples[0] += arr.size
                        _ring_write(arr)

            except BaseException as exc:  # noqa: BLE001
                gen_error.append(exc)
            finally:
                with cond:
                    gen_done = True
                    cond.notify_all()

        t0 = time.time()
        gen_thread = threading.Thread(target=_run_gen, name="qwen3-gen", daemon=True)
        gen_thread.start()

        # Wait until prebuffer is filled or generation finishes.
        min_start_fill = int(0.5 * sr)
        with cond:
            while (
                _available() < min_start_fill
                and _available() < prebuffer_max_samples
                and not gen_done
                and not (stop_check and stop_check())
            ):
                cond.wait(timeout=0.1)
            prebuffer_filled = _available()

        if stop_check and stop_check():
            with cond:
                gen_done = True
                cond.notify_all()
            gen_thread.join(timeout=5.0)
            return False

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
                        try:
                            sd._terminate()
                            sd._initialize()
                        except Exception as exc:  # noqa: BLE001
                            _alog(f"[{self._tag}-play] sd reinit warn: {exc}")
                        return opener()
                return opener()
            except Exception as exc:  # noqa: BLE001
                _alog(f"[{self._tag}-play] open stream failed: {exc}")
                return None

        stream = _open_stream()
        if stream is None:
            with cond:
                gen_done = True
                cond.notify_all()
            gen_thread.join(timeout=5.0)
            return False

        first_audio_ms = (time.time() - t0) * 1000.0
        print(
            f"[{self._tag}-play] prebuffer={prebuffer_filled / sr:.2f}s "
            f"first-audio={first_audio_ms:.0f}ms",
            flush=True,
        )

        stream.start()
        try:
            while True:
                if stop_check and stop_check():
                    break
                if ac_device_changed is not None and ac_device_changed.is_set():
                    _alog(f"[{self._tag}-play] device changed — reopening stream")
                    try:
                        stream.stop()
                        stream.close()
                    except Exception as exc:  # noqa: BLE001
                        _alog(f"[{self._tag}-play] close-on-swap warn: {exc}")
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
        ok = not gen_error and not (stop_check and stop_check())
        if gen_error:
            print(f"[{self._tag}-play] generate error: {gen_error[0]}", flush=True)

        elapsed = time.time() - t0
        played_sec = write_idx / sr if write_idx else 0.0
        gen_audio_sec = synth_audio_samples[0] / sr if synth_audio_samples[0] else 0.0
        if synth_t_first and synth_t_last:
            synth_elapsed = synth_t_last[0] - synth_t_first[0]
        else:
            synth_elapsed = 0.0
        synth_rtf = (
            f"{synth_elapsed / gen_audio_sec:.2f}" if gen_audio_sec > 0 else "n/a"
        )
        play_rtf = f"{elapsed / played_sec:.2f}" if played_sec > 0 else "n/a"
        underrun_ms = underrun_samples * 1000.0 / sr
        frames_summary = ", ".join(
            f"{n}x{cnt}" for n, cnt in sorted(cb_frames_seen.items())
        ) or "(none)"
        voice_desc = ref_audio if ref_audio else f"seed={seed}"
        # `rtf` = synth-only (matches batch RTF semantics);
        # `play_rtf` = wall/playback ratio (~1.0 for streaming, kept for parity).
        print(
            f"[{self._tag}-play] done elapsed={elapsed:.1f}s audio={played_sec:.1f}s "
            f"synth_elapsed={synth_elapsed:.2f}s rtf={synth_rtf} play_rtf={play_rtf} "
            f"underruns={underrun_events} ({underrun_ms:.0f}ms) "
            f"cb_blocks=[{frames_summary}] anomalies={len(cb_log)} "
            f"voice={voice_desc} ok={ok}",
            flush=True,
        )
        for ev in cb_log[:20]:
            ts_ev, fr, took, av, us = ev
            rel = ts_ev - t0
            print(
                f"[{self._tag}-cb] t+{rel:.3f}s frames={fr} delivered={took} "
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
        instruct: str | None = None,
        playback_queue=None,
        stop_check=None,
        msg_id: int = -1,
    ) -> np.ndarray | None:
        """Generate audio with streaming — yield chunks to playback_queue as they arrive.

        If playback_queue is provided, chunks are queued directly and None is returned.
        If playback_queue is None, collects all chunks and returns concatenated array.
        """
        if self._model is None:
            raise RuntimeError("Qwen3TTSBackend not loaded — call load() first")

        ref_audio, ref_text, seed = self._resolve_voice(voice)
        use_instruct = instruct or self._instruct or None
        _FIRST_CHUNK_TIMEOUT = 15.0  # qwen3 ICL prefill can be slow

        t0 = time.time()
        total_samples = 0
        n_chunks = 0
        collected: list[np.ndarray] = []

        try:
            with self._lock:
                import mlx.core as mx  # type: ignore[import]

                mx.random.seed(seed)

                for result in self._model.generate(
                    text=text,
                    temperature=self._temperature,
                    repetition_penalty=self._repetition_penalty,
                    top_p=self._top_p,
                    top_k=self._top_k,
                    ref_audio=ref_audio,
                    ref_text=ref_text,
                    instruct=use_instruct,
                    split_pattern="",  # we handle chunking in the daemon
                    stream=True,
                    streaming_interval=1.5,
                ):
                    if stop_check and stop_check():
                        break
                    if n_chunks == 0 and time.time() - t0 > _FIRST_CHUNK_TIMEOUT:
                        print(
                            f"[qwen3-stream] first-chunk timeout ({_FIRST_CHUNK_TIMEOUT}s)",
                            flush=True,
                        )
                        break

                    arr = np.array(result.audio, dtype=np.float32)
                    if arr.ndim > 1:
                        arr = arr.squeeze()
                    if arr.size == 0:
                        continue

                    if result.sample_rate and result.sample_rate != self.sample_rate:
                        self.sample_rate = result.sample_rate

                    n_chunks += 1
                    total_samples += arr.size

                    if playback_queue is not None:
                        # First chunk carries subtitle text; rest are None
                        subtitle = text if n_chunks == 1 else None
                        playback_queue.put((arr, subtitle, msg_id))
                    else:
                        collected.append(arr)

        except Exception as exc:
            print(f"[qwen3-stream] error: {exc}", flush=True)
            if playback_queue is None and not collected:
                return None

        elapsed = time.time() - t0
        duration = total_samples / self.sample_rate if total_samples > 0 else 0
        voice_desc = f"ref={ref_audio}" if ref_audio else f"seed={seed}"
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[{self._tag}-stream] {n_chunks} chunks, generated {duration:.1f}s audio in {elapsed:.1f}s "
            f"(RTF {rtf}, {voice_desc}, model={self._model_id})",
            flush=True,
        )

        if playback_queue is not None:
            return None

        if not collected:
            return None
        return np.concatenate(collected) if len(collected) > 1 else collected[0]
