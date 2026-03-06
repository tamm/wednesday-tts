"""Pocket TTS backend — supports true streaming for lowest latency."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import threading
import time

import numpy as np

from .base import TTSBackend, DEFAULT_SPEED, soundstretch_tempo


class PocketTTSBackend(TTSBackend):
    """Pocket TTS voice-cloned model.

    Config keys (from tts-config.json models.pocket):
        voice               — voice name or path/URL (default: fantine)
        fallback_voice      — fallback if voice load fails (default: fantine)
        speed               — tempo multiplier; 1.0 = native (soundstretch applied)
        lsd_decode_steps    — LSD decode steps, lower = faster (default: 1)
        noise_clamp         — noise clamping factor (default: None)
        eos_threshold       — end-of-speech threshold (default: -4.0)
        frames_after_eos    — extra frames after EOS token
    """

    sample_rate = 24000  # updated from model after load()
    supports_streaming = True

    def __init__(
        self,
        voice: str = "fantine",
        fallback_voice: str = "fantine",
        speed: float = DEFAULT_SPEED,
        lsd_decode_steps: int = 1,
        noise_clamp: float | None = None,
        eos_threshold: float = -4.0,
        frames_after_eos: int | None = None,
    ) -> None:
        self._voice_name = voice or os.environ.get("POCKET_TTS_VOICE", "fantine")
        self._fallback_voice = fallback_voice
        self._speed = speed
        self._lsd_decode_steps = lsd_decode_steps
        self._noise_clamp = noise_clamp
        self._eos_threshold = eos_threshold
        self._frames_after_eos = frames_after_eos

        self._model = None
        self._voice_state = None
        self._lock = threading.Lock()  # generate_audio is not thread-safe

    def load(self) -> None:
        from pocket_tts import TTSModel  # type: ignore[import]
        from pocket_tts.utils.utils import PREDEFINED_VOICES  # type: ignore[import]

        self._model = TTSModel.load_model(
            lsd_decode_steps=self._lsd_decode_steps,
            noise_clamp=self._noise_clamp,
            eos_threshold=self._eos_threshold,
        )
        self.sample_rate = self._model.sample_rate

        voice_ref = PREDEFINED_VOICES.get(self._voice_name, self._voice_name)
        try:
            self._voice_state = self._model.get_state_for_audio_prompt(voice_ref)
        except Exception as exc:
            fallback_ref = PREDEFINED_VOICES.get(self._fallback_voice, self._fallback_voice)
            print(f"[pocket] Voice '{self._voice_name}' failed ({exc}), falling back to '{self._fallback_voice}'")
            self._voice_state = self._model.get_state_for_audio_prompt(fallback_ref)

    def generate(self, text: str, speed: float | None = None) -> "np.ndarray | None":
        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        with self._lock:
            audio = self._model.generate_audio(self._voice_state, text)

        if audio is None:
            return None
        arr = audio.numpy() if hasattr(audio, "numpy") else np.array(audio)
        if arr.size == 0:
            return None

        if abs(use_speed - 1.0) > 0.01:
            arr = soundstretch_tempo(arr, self.sample_rate, use_speed)
        return arr

    def generate_streaming(self, text: str, speed: float | None = None,
                           playback_queue=None, stop_check=None) -> "np.ndarray | None":
        """Generate audio via streaming inference, pipe through soundstretch, queue chunks.

        If playback_queue is provided and speed != 1.0:
            Spawns a soundstretch pipe (stdin/stdout). Model chunks are written
            to the pipe as WAV PCM. Stretched output is read in a separate thread
            and queued directly into playback_queue for immediate playback.
            Returns None (audio went directly to queue).

        If playback_queue is None or speed ~= 1.0:
            Collects all chunks, concatenates, applies soundstretch if needed,
            returns the array (caller queues it).

        stop_check: callable returning True if generation should abort (STOP fired).
        """
        if self._model is None:
            raise RuntimeError("PocketTTSBackend not loaded — call load() first")

        use_speed = speed if speed is not None else self._speed
        needs_speed = abs(use_speed - 1.0) > 0.01
        _GENERATE_DEADLINE = 8.0

        # If we have a queue, stream directly into it
        if playback_queue is not None:
            if needs_speed:
                return self._generate_streaming_pipe(
                    text, use_speed, playback_queue, stop_check, _GENERATE_DEADLINE
                )
            # speed ~= 1.0: queue raw chunks directly, no soundstretch
            return self._generate_streaming_direct(
                text, playback_queue, stop_check, _GENERATE_DEADLINE
            )

        # No queue: collect all chunks, concatenate, return
        chunks: list[np.ndarray] = []
        gen_start = time.monotonic()

        with self._lock:
            for audio_chunk in self._model.generate_audio_stream(
                self._voice_state,
                text,
                frames_after_eos=self._frames_after_eos,
            ):
                if stop_check and stop_check():
                    break
                if time.monotonic() - gen_start > _GENERATE_DEADLINE:
                    print(f"[stream] generation deadline ({_GENERATE_DEADLINE}s) hit", flush=True)
                    break
                arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                if arr.ndim > 1:
                    arr = arr.flatten()
                if arr.size > 0:
                    chunks.append(arr)

        if not chunks:
            return None

        gen_elapsed = time.monotonic() - gen_start
        total_samples = sum(c.size for c in chunks)
        print(f"[stream] {len(chunks)} chunks, {total_samples / self.sample_rate:.1f}s audio in {gen_elapsed:.1f}s", flush=True)

        result = np.concatenate(chunks)
        if needs_speed:
            result = soundstretch_tempo(result, self.sample_rate, use_speed)
        return result

    def _generate_streaming_direct(self, text: str, playback_queue,
                                    stop_check, deadline: float) -> None:
        """Stream model chunks directly into playback_queue (no soundstretch)."""
        gen_start = time.monotonic()
        total_samples = 0
        n_chunks = 0

        with self._lock:
            for audio_chunk in self._model.generate_audio_stream(
                self._voice_state,
                text,
                frames_after_eos=self._frames_after_eos,
            ):
                if stop_check and stop_check():
                    break
                if time.monotonic() - gen_start > deadline:
                    print(f"[stream-direct] generation deadline ({deadline}s) hit", flush=True)
                    break
                arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                if arr.ndim > 1:
                    arr = arr.flatten()
                if arr.size > 0:
                    playback_queue.put(arr.astype(np.float32))
                    total_samples += arr.size
                    n_chunks += 1

        gen_elapsed = time.monotonic() - gen_start
        print(f"[stream-direct] {n_chunks} chunks, {total_samples / self.sample_rate:.1f}s audio in {gen_elapsed:.1f}s", flush=True)
        return None

    def _generate_streaming_pipe(self, text: str, speed: float,
                                  playback_queue, stop_check,
                                  deadline: float) -> None:
        """Stream model output through soundstretch pipe, queue stretched chunks.

        Pipeline:
            generate_audio_stream() → WAV PCM → soundstretch stdin
            soundstretch stdout → read chunks → playback_queue

        The WAV header declares a very large data size so soundstretch
        processes it as a stream without waiting for EOF.
        """
        ss = shutil.which("soundstretch")
        if not ss:
            print("[stream-pipe] soundstretch not found, falling back to batch", flush=True)
            return self.generate_streaming(text, speed, playback_queue=None, stop_check=stop_check)

        tempo_pct = (speed - 1.0) * 100
        proc = subprocess.Popen(
            [ss, "stdin", "stdout", f"-tempo={tempo_pct:+.0f}", "-speech"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        sr = self.sample_rate

        # Write WAV header with large data size (stream mode)
        def _wav_header(sample_rate: int, num_channels: int = 1, bits: int = 16) -> bytes:
            """WAV header with 0x7FFFFFFF data size — signals streaming to soundstretch."""
            data_size = 0x7FFFFFFF
            byte_rate = sample_rate * num_channels * (bits // 8)
            block_align = num_channels * (bits // 8)
            header = struct.pack(
                '<4sI4s4sIHHIIHH4sI',
                b'RIFF', data_size + 36, b'WAVE',
                b'fmt ', 16, 1, num_channels,
                sample_rate, byte_rate, block_align, bits,
                b'data', data_size,
            )
            return header

        # Reader thread: read stretched audio from stdout, queue it
        reader_done = threading.Event()

        def _read_stretched():
            try:
                # Skip the output WAV header (44 bytes)
                hdr = proc.stdout.read(44)
                if not hdr or len(hdr) < 44:
                    return

                READ_SIZE = sr * 2  # 1 second of int16 = sr * 2 bytes
                while True:
                    if stop_check and stop_check():
                        break
                    data = proc.stdout.read(READ_SIZE)
                    if not data:
                        break
                    # Convert int16 PCM to float32
                    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                    if samples.size > 0:
                        playback_queue.put(samples)
            except Exception as exc:
                print(f"[stream-pipe] reader error: {exc}", flush=True)
            finally:
                reader_done.set()

        reader_thread = threading.Thread(target=_read_stretched, daemon=True)
        reader_thread.start()

        # Writer: generate chunks, convert to int16 PCM, feed to soundstretch stdin
        gen_start = time.monotonic()
        total_samples = 0
        try:
            proc.stdin.write(_wav_header(sr))
            proc.stdin.flush()

            with self._lock:
                for audio_chunk in self._model.generate_audio_stream(
                    self._voice_state,
                    text,
                    frames_after_eos=self._frames_after_eos,
                ):
                    if stop_check and stop_check():
                        break
                    if time.monotonic() - gen_start > deadline:
                        print(f"[stream-pipe] generation deadline ({deadline}s) hit", flush=True)
                        break
                    arr = audio_chunk.numpy() if hasattr(audio_chunk, "numpy") else np.array(audio_chunk)
                    if arr.ndim > 1:
                        arr = arr.flatten()
                    if arr.size == 0:
                        continue

                    # Convert float32 → int16 PCM for soundstretch
                    pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype(np.int16)
                    try:
                        proc.stdin.write(pcm.tobytes())
                        proc.stdin.flush()
                    except BrokenPipeError:
                        print("[stream-pipe] soundstretch pipe broke", flush=True)
                        break
                    total_samples += arr.size

        except Exception as exc:
            print(f"[stream-pipe] writer error: {exc}", flush=True)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

        gen_elapsed = time.monotonic() - gen_start
        print(f"[stream-pipe] generated {total_samples / sr:.1f}s audio in {gen_elapsed:.1f}s, waiting for stretch", flush=True)

        # Wait for reader to finish (soundstretch processes remaining input)
        reader_done.wait(timeout=10.0)

        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()

        return None
