"""Kokoro 82M TTS backend."""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from ...normalize.chunking import chunk_text_intelligently  # noqa: TID252
from .base import DEFAULT_SPEED, TTSBackend


class KokoroBackend(TTSBackend):
    """Kokoro 82M neural TTS via the `kokoro` package.

    Config keys (from tts-config.json models.kokoro):
        voice            — Kokoro voice ID (default: af_bella)
        speed            — playback speed multiplier; applied natively by Kokoro
        samplerate       — output sample rate (default: 24000)
        lang_code        — language pipeline code (default: "a" = American English)
        repo_id          — HF repo override (default: hexgrad/Kokoro-82M)
        fast_first_chunk — when True (default), route through DIRECT-PLAY streaming
                           path so first audio starts as soon as the first KPipeline
                           chunk is ready, instead of waiting for full synthesis.
                           Set to False to restore the original batch behaviour.
        prebuffer_sec    — seconds of audio to pre-fill before opening the
                           PortAudio stream (default 0.15).  Only used when
                           fast_first_chunk=True.
        ringbuffer_sec   — ring buffer capacity in seconds (default 30.0).
    """

    sample_rate = 24000

    def __init__(
        self,
        voice: str | None = None,
        speed: float = DEFAULT_SPEED,
        samplerate: int = 24000,
        lang_code: str = "a",
        repo_id: str = "hexgrad/Kokoro-82M",
        fast_first_chunk: bool = True,
        prebuffer_sec: float = 0.15,
        ringbuffer_sec: float = 30.0,
    ) -> None:
        self._pipeline = None
        self._voice = voice or os.environ.get("KOKORO_VOICE", "af_bella")
        self._speed = speed
        self._lang_code = lang_code
        self._repo_id = repo_id
        self._fast_first_chunk = fast_first_chunk
        self._prebuffer_sec = float(prebuffer_sec)
        self._ringbuffer_sec = float(ringbuffer_sec)
        self.sample_rate = samplerate
        self._lock = threading.Lock()
        # Persistent OutputStream — opened lazily on first play_streaming
        # call, then reused for every subsequent utterance until either the
        # output device changes or the daemon shuts down. Reusing the stream
        # avoids the ~150-500ms sd._terminate/_initialize/_OutputStream cost
        # on each utterance.
        self._persistent_stream = None
        self._persistent_stream_lock = threading.Lock()
        # The ring buffer the persistent stream's callback reads from.
        # Reset between utterances; the stream itself stays open.
        self._stream_ring: np.ndarray | None = None
        self._stream_ring_capacity: int = 0
        self._stream_write_idx: int = 0
        self._stream_read_idx: int = 0
        self._stream_cond = threading.Condition()
        self._stream_underrun_events: int = 0
        self._stream_underrun_samples: int = 0

    # ── Capability flags (computed after init) ───────────────────────

    @property
    def supports_streaming(self) -> bool:  # type: ignore[override]
        return self._fast_first_chunk

    @property
    def supports_direct_play(self) -> bool:  # type: ignore[override]
        return self._fast_first_chunk

    # ── Model load ────────────────────────────────────────────────────

    def load(self) -> None:
        from kokoro import KPipeline  # type: ignore[import]

        self._pipeline = KPipeline(lang_code=self._lang_code, repo_id=self._repo_id)

    # ── Batch generate (original path, used when fast_first_chunk=False
    #    or as a fallback from play_streaming on error) ─────────────────

    def generate(
        self, text: str, speed: float | None = None, voice: str | None = None
    ) -> np.ndarray | None:
        if self._pipeline is None:
            raise RuntimeError("KokoroBackend not loaded — call load() first")
        if not text or not text.strip():
            return None

        use_speed = speed if speed is not None else self._speed
        use_voice = voice or self._voice
        if isinstance(use_voice, dict):
            use_voice = use_voice.get("voice") or self._voice

        t0 = time.time()
        chunks: list[np.ndarray] = []
        n_segments = 0
        try:
            with self._lock:
                for result in self._pipeline(text, voice=use_voice, speed=use_speed):
                    if result.audio is None:
                        continue
                    arr = result.audio
                    if hasattr(arr, "numpy"):
                        arr = arr.numpy()
                    arr = np.asarray(arr, dtype=np.float32)
                    if arr.ndim > 1:
                        arr = arr.squeeze()
                    if arr.size > 0:
                        chunks.append(arr)
                        n_segments += 1
        except Exception as exc:
            print(f"[kokoro] generate error: {exc}", flush=True)
            return None

        if not chunks:
            print(f"[kokoro] no audio for {len(text)} chars", flush=True)
            return None

        combined = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        elapsed = time.time() - t0
        duration = combined.size / self.sample_rate if self.sample_rate else 0.0
        rtf = f"{elapsed / duration:.2f}" if duration > 0 else "n/a"
        print(
            f"[kokoro] {n_segments} segments, generated {duration:.1f}s audio in {elapsed:.1f}s "
            f"(RTF {rtf}, voice={use_voice})",
            flush=True,
        )
        return combined

    # ── Streaming direct-play ─────────────────────────────────────────

    def play_streaming(
        self,
        text: str,
        speed: float | None = None,
        voice: str | None = None,
        stop_check=None,
        msg_id: int = -1,
        audio_context=None,
    ) -> bool:
        """Pre-buffered, callback-driven playback from the KPipeline generator.

        KPipeline yields audio chunks one at a time as they are synthesised.
        This method feeds those chunks into a numpy ring buffer while a
        sounddevice OutputStream callback drains it, so the first audio out
        of the speaker happens as soon as prebuffer_sec of audio is ready —
        typically after the first KPipeline segment (~80-150ms).

        Architecture mirrors vibevoice.VibeVoiceBackend.play_streaming.

        Returns True on a clean finish, False on stop/error.
        """
        if self._pipeline is None:
            raise RuntimeError("KokoroBackend not loaded — call load() first")
        if not text or not text.strip():
            return False

        import sounddevice as sd  # type: ignore[import]

        use_speed = speed if speed is not None else self._speed
        use_voice = voice or self._voice
        if isinstance(use_voice, dict):
            use_voice = use_voice.get("voice") or self._voice

        sr = self.sample_rate
        prebuffer_max_samples = max(1, int(self._prebuffer_sec * sr))
        ring_capacity = max(prebuffer_max_samples * 4, int(self._ringbuffer_sec * sr))

        # Ring buffer state — guarded by cond.
        ring = np.zeros(ring_capacity, dtype=np.float32)
        write_idx = 0   # absolute samples written
        read_idx = 0    # absolute samples read
        gen_done = False
        cond = threading.Condition()
        underrun_events = 0
        underrun_samples = 0

        def _available() -> int:
            return write_idx - read_idx

        def _ring_write(arr: np.ndarray) -> None:
            nonlocal write_idx
            n = arr.size
            with cond:
                # Back-pressure: wait if ring is full.
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
            with cond:
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

        gen_error: list[BaseException] = []
        # Synth wall-clock: from first generator iteration to last yielded chunk.
        # Excludes playback time, so synth_rtf = synth_elapsed / audio_s reflects
        # the model's true generation speed (independent of how long playback takes).
        gen_t_start: list[float] = []
        gen_t_end: list[float] = []
        gen_audio_samples: list[int] = [0]

        # Pre-split into a tiny first chunk + larger remainder so KPipeline's
        # first yielded segment is small and ready quickly. After the first
        # piece plays, the remainder is fed to KPipeline as a single call;
        # its own internal segmentation handles the rest.
        text_pieces: list[str]
        if len(text) > 200:
            split_chunks = chunk_text_intelligently(
                text,
                first_chunk_min=40,
                first_chunk_max=150,
                second_third_min=80,
                second_third_max=200,
                chunk_min=200,
                chunk_max=400,
            )
            if split_chunks:
                # First piece tiny, everything else concatenated for KPipeline.
                first = split_chunks[0]
                rest = " ".join(split_chunks[1:]).strip()
                text_pieces = [first] + ([rest] if rest else [])
            else:
                text_pieces = [text]
        else:
            text_pieces = [text]

        def _drain_generator() -> None:
            """Run the KPipeline generator across each pre-split text piece."""
            nonlocal gen_done
            chunk_idx = 0
            try:
                with self._lock:
                    gen_t_start.append(time.time())
                    last_chunk_t = gen_t_start[-1]
                    for piece_idx, piece in enumerate(text_pieces):
                        if stop_check and stop_check():
                            break
                        for result in self._pipeline(
                            piece, voice=use_voice, speed=use_speed
                        ):
                            if stop_check and stop_check():
                                break
                            if result.audio is None:
                                continue
                            arr = result.audio
                            if hasattr(arr, "numpy"):
                                arr = arr.numpy()
                            arr = np.asarray(arr, dtype=np.float32)
                            if arr.ndim > 1:
                                arr = arr.squeeze()
                            if arr.size > 0:
                                now = time.time()
                                gen_ms = (now - last_chunk_t) * 1000.0
                                chunk_dur_ms = arr.size * 1000.0 / sr
                                chunk_text = getattr(result, "graphemes", "") or ""
                                print(
                                    f"[kokoro-chunk] msg_id={msg_id} "
                                    f"piece={piece_idx} i={chunk_idx} "
                                    f"chars={len(chunk_text)} gen={gen_ms:.0f}ms "
                                    f"dur={chunk_dur_ms:.0f}ms "
                                    f"rtf={gen_ms / chunk_dur_ms:.2f} "
                                    f"text={chunk_text[:60]!r}",
                                    flush=True,
                                )
                                gen_audio_samples[0] += arr.size
                                _ring_write(arr)
                                last_chunk_t = now
                                chunk_idx += 1
                    gen_t_end.append(time.time())
            except BaseException as exc:  # noqa: BLE001
                gen_error.append(exc)
            finally:
                with cond:
                    gen_done = True
                    cond.notify_all()

        t0 = time.time()
        drain_thread = threading.Thread(
            target=_drain_generator, name="kokoro-drain", daemon=True
        )
        drain_thread.start()

        # Wait until prebuffer_max_samples are ready OR generation finishes.
        with cond:
            while (
                _available() < prebuffer_max_samples
                and not gen_done
                and not (stop_check and stop_check())
            ):
                cond.wait(timeout=0.1)
            prebuffer_filled = _available()

        if stop_check and stop_check():
            drain_thread.join(timeout=5.0)
            return False

        ac_lock = getattr(audio_context, "lock", None) if audio_context else None
        ac_device_changed = (
            getattr(audio_context, "device_changed", None) if audio_context else None
        )

        def _open_stream(force_reinit: bool = False):
            """Open a fresh OutputStream.

            Skip sd._terminate/_initialize unless the caller explicitly asks
            for a reinit (e.g. on device change). The reinit costs 150-500ms
            and is unnecessary if the audio device hasn't changed.
            """
            opener = lambda: sd.OutputStream(  # noqa: E731
                samplerate=sr,
                channels=1,
                dtype="float32",
                blocksize=1024,
                latency="low",
                callback=_audio_callback,
            )
            try:
                if force_reinit and ac_lock is not None:
                    with ac_lock:
                        try:
                            sd._terminate()
                            sd._initialize()
                        except Exception as exc:  # noqa: BLE001
                            print(f"[kokoro-play] sd reinit warn: {exc}", flush=True)
                        return opener()
                return opener()
            except Exception as exc:  # noqa: BLE001
                print(f"[kokoro-play] open stream failed: {exc}", flush=True)
                return None

        stream = _open_stream()
        if stream is None:
            with cond:
                gen_done = True
                cond.notify_all()
            drain_thread.join(timeout=5.0)
            return False

        # first-audio = time from request to first sample heading to the speaker.
        # prebuffer = how much audio was queued at that moment (capped to the
        # configured prebuffer_sec; large values mean generation outran the
        # opener and we shouldn't penalise the TTFS reading).
        first_audio_ms = (time.time() - t0) * 1000.0
        prebuf_s = min(prebuffer_filled / sr, self._prebuffer_sec)
        print(
            f"[kokoro-play] prebuffer={prebuf_s:.2f}s "
            f"first-audio={first_audio_ms:.0f}ms",
            flush=True,
        )

        stream.start()
        try:
            while True:
                if stop_check and stop_check():
                    break
                if ac_device_changed is not None and ac_device_changed.is_set():
                    print("[kokoro-play] device changed — reopening stream", flush=True)
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    ac_device_changed.clear()
                    # Device changed → force reinit to pick up new default.
                    new_stream = _open_stream(force_reinit=True)
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

        drain_thread.join(timeout=5.0)
        ok = not gen_error and not (stop_check and stop_check())
        if gen_error:
            print(f"[kokoro-play] generate error: {gen_error[0]}", flush=True)

        elapsed = time.time() - t0
        played_sec = write_idx / sr if write_idx else 0.0
        gen_audio_sec = gen_audio_samples[0] / sr if gen_audio_samples[0] else 0.0
        # synth_elapsed = drain-thread wall time (model gen only, no playback)
        if gen_t_start and gen_t_end:
            synth_elapsed = gen_t_end[0] - gen_t_start[0]
        else:
            synth_elapsed = 0.0
        synth_rtf = (
            f"{synth_elapsed / gen_audio_sec:.2f}" if gen_audio_sec > 0 else "n/a"
        )
        play_rtf = f"{elapsed / played_sec:.2f}" if played_sec > 0 else "n/a"
        underrun_ms = underrun_samples * 1000.0 / sr
        # Field names: `rtf` = synth-only (matches batch-mode RTF semantics);
        # `play_rtf` = wall/playback ratio (always ~1.0 for streaming, less useful).
        print(
            f"[kokoro-play] done elapsed={elapsed:.1f}s audio={played_sec:.1f}s "
            f"synth_elapsed={synth_elapsed:.2f}s rtf={synth_rtf} play_rtf={play_rtf} "
            f"underruns={underrun_events} ({underrun_ms:.0f}ms) "
            f"voice={use_voice} ok={ok}",
            flush=True,
        )
        return ok
