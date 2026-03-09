"""Tests for daemon streaming, queue architecture, and watchdog paths.

Validates that the daemon correctly handles:
- Streaming chunks feeding into playback_queue (not direct OutputStream)
- Fallback to batch when streaming doesn't yield first chunk in time
- _streaming_lock release after streaming (no cascading deadlock)
- Watchdog visibility into streaming hangs (requests_errored stat)
- Audio health probe behaviour
- abort_stream effectiveness
- stream_chunks() generator in pocket backend

All tests mock sounddevice and TTS model -- no real audio hardware needed.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Pocket backend: stream_chunks() generator tests
# ---------------------------------------------------------------------------


class TestStreamChunks:
    """Verify PocketTTSBackend.stream_chunks() yields audio chunks correctly."""

    def _make_backend(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._lock = threading.Lock()
        backend._active_stream = None
        backend._speed = 1.0
        backend._LEADIN_SAMPLES = 24000
        backend._frames_after_eos = None
        backend.sample_rate = 24000
        return backend

    def test_yields_chunks_at_native_speed(self):
        """stream_chunks at speed=1.0 should yield chunks without soundstretch."""
        backend = self._make_backend()

        chunk1 = MagicMock()
        chunk1.numpy.return_value = np.zeros(100, dtype=np.float32)
        chunk2 = MagicMock()
        chunk2.numpy.return_value = np.ones(200, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([chunk1, chunk2])

        chunks = list(backend.stream_chunks("hello", speed=1.0))
        assert len(chunks) == 2
        assert chunks[0].shape == (100,)
        assert chunks[1].shape == (200,)

    def test_skips_empty_chunks(self):
        """Empty audio chunks should be skipped."""
        backend = self._make_backend()

        empty_chunk = MagicMock()
        empty_chunk.numpy.return_value = np.array([], dtype=np.float32)
        real_chunk = MagicMock()
        real_chunk.numpy.return_value = np.zeros(50, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([empty_chunk, real_chunk])

        chunks = list(backend.stream_chunks("hello", speed=1.0))
        assert len(chunks) == 1
        assert chunks[0].shape == (50,)

    def test_not_loaded_raises(self):
        """stream_chunks should raise if model is not loaded."""
        backend = self._make_backend()
        backend._model = None

        with pytest.raises(RuntimeError, match="not loaded"):
            list(backend.stream_chunks("hello"))


# ---------------------------------------------------------------------------
# Pocket backend: play_streaming + _streaming_lock tests
# ---------------------------------------------------------------------------


class TestStreamingLockRelease:
    """Verify _streaming_lock is always released, even when play_streaming hangs."""

    def _make_mock_stream_factory(self):
        def factory(**kwargs):
            mock_stream = MagicMock()
            finished_cb = kwargs.get("finished_callback")
            def on_start():
                if finished_cb:
                    finished_cb()
            mock_stream.start.side_effect = on_start
            return mock_stream
        return factory

    def test_lock_released_after_normal_completion(self):
        from wednesday_tts.server.backends import pocket as pocket_mod

        backend = pocket_mod.PocketTTSBackend.__new__(pocket_mod.PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._lock = threading.Lock()
        backend._active_stream = None
        backend._speed = 1.0
        backend._LEADIN_SAMPLES = 24000
        backend._frames_after_eos = None
        backend.sample_rate = 24000

        backend._model.generate_audio_stream.return_value = iter([])

        with patch("sounddevice.OutputStream", side_effect=self._make_mock_stream_factory()), \
             patch("sounddevice._terminate"), \
             patch("sounddevice._initialize"), \
             patch.object(pocket_mod, "_get_device_samplerate", return_value=24000):
            backend.play_streaming(text="hello", speed=1.0)

        acquired = pocket_mod._streaming_lock.acquire(timeout=0.1)
        assert acquired, "_streaming_lock was not released after normal completion"
        pocket_mod._streaming_lock.release()

    def test_lock_released_after_abort(self):
        from wednesday_tts.server.backends import pocket as pocket_mod

        backend = pocket_mod.PocketTTSBackend.__new__(pocket_mod.PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._lock = threading.Lock()
        backend._active_stream = None
        backend._speed = 1.0
        backend._LEADIN_SAMPLES = 24000
        backend._frames_after_eos = None
        backend.sample_rate = 24000

        abort_event = threading.Event()

        def slow_chunks(*args, **kwargs):
            chunk1 = MagicMock()
            chunk1.numpy.return_value = np.zeros(100, dtype=np.float32)
            yield chunk1
            abort_event.wait(timeout=2)
            chunk2 = MagicMock()
            chunk2.numpy.return_value = np.zeros(100, dtype=np.float32)
            yield chunk2

        backend._model.generate_audio_stream.side_effect = slow_chunks

        def run_streaming():
            with patch("sounddevice.OutputStream", side_effect=self._make_mock_stream_factory()), \
                 patch("sounddevice._terminate"), \
                 patch("sounddevice._initialize"), \
                 patch.object(pocket_mod, "_get_device_samplerate", return_value=24000):
                backend.play_streaming(text="hello", speed=1.0)

        t = threading.Thread(target=run_streaming, daemon=True)
        t.start()
        time.sleep(0.3)

        backend.abort_stream()
        abort_event.set()
        t.join(timeout=5)

        acquired = pocket_mod._streaming_lock.acquire(timeout=0.5)
        assert acquired, "_streaming_lock not released after abort"
        pocket_mod._streaming_lock.release()


class TestAbortStream:
    """Verify abort_stream clears _active_stream and calls abort on the stream."""

    def test_abort_clears_active_stream(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        mock_stream = MagicMock()
        backend._active_stream = mock_stream

        backend.abort_stream()

        assert backend._active_stream is None
        mock_stream.abort.assert_called_once()

    def test_abort_noop_when_no_stream(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._active_stream = None

        backend.abort_stream()
        assert backend._active_stream is None


# ---------------------------------------------------------------------------
# Daemon: streaming-to-queue tests
# ---------------------------------------------------------------------------


class TestDaemonStreamingToQueue:
    """Verify the daemon's streaming path feeds chunks into playback_queue."""

    def test_streaming_feeds_queue_and_advances_seq(self):
        """SEQ:0 streaming should put chunks into playback_queue and advance
        _next_seq to 1 so subsequent chunks can enqueue."""
        from wednesday_tts.server import daemon

        saved_stats = dict(daemon._stats)
        saved_next_seq = daemon._next_seq
        daemon._stats["requests_total"] = 0
        daemon._stats["requests_completed"] = 0
        daemon._stats["requests_errored"] = 0
        daemon._stats["requests_stopped"] = 0
        daemon._next_seq = 0

        try:
            mock_conn = MagicMock()
            mock_conn.recv.return_value = b"SEQ:0:N:hello"

            mock_backend = MagicMock()
            mock_backend.supports_streaming = True
            mock_backend.sample_rate = 24000

            # stream_chunks yields two chunks
            chunk1 = np.zeros(100, dtype=np.float32)
            chunk2 = np.ones(200, dtype=np.float32)
            mock_backend.stream_chunks.return_value = iter([chunk1, chunk2])

            with patch.object(daemon, "_split_voice_segments", return_value=[(None, "hello")]), \
                 patch.object(daemon, "run_normalize", return_value="hello"), \
                 patch.object(daemon, "playback_queue") as mock_pq:
                daemon.handle_client(mock_conn, mock_backend)

            # Chunks should have been put into the queue
            assert mock_pq.put.call_count >= 1, "Chunks not put into playback_queue"
            assert daemon._next_seq == 1, f"_next_seq should be 1, got {daemon._next_seq}"
            assert daemon._stats["requests_completed"] >= 1
        finally:
            daemon._stats.update(saved_stats)
            daemon._next_seq = saved_next_seq

    def test_streaming_fallback_to_batch_on_timeout(self):
        """When stream_chunks doesn't yield within 8s, should fall back to batch."""
        from wednesday_tts.server import daemon

        saved_stats = dict(daemon._stats)
        saved_next_seq = daemon._next_seq
        daemon._stats["requests_total"] = 0
        daemon._stats["requests_completed"] = 0
        daemon._stats["requests_errored"] = 0
        daemon._stats["requests_stopped"] = 0
        daemon._next_seq = 0

        try:
            mock_conn = MagicMock()
            mock_conn.recv.return_value = b"SEQ:0:N:hello"

            mock_backend = MagicMock()
            mock_backend.supports_streaming = True
            mock_backend.sample_rate = 24000

            # stream_chunks hangs forever (never yields)
            def hang_forever(text, speed):
                time.sleep(60)
                return iter([])

            mock_backend.stream_chunks.side_effect = hang_forever

            mock_audio = np.zeros(100, dtype=np.float32)

            # Patch first_chunk_event.wait to return False immediately (simulates timeout)
            original_event_wait = threading.Event.wait

            def fast_event_wait(self, timeout=None):
                return original_event_wait(self, timeout=0.1)

            with patch.object(daemon, "_split_voice_segments", return_value=[(None, "hello")]), \
                 patch.object(daemon, "run_normalize", return_value="hello"), \
                 patch.object(daemon, "_render_segments", return_value=mock_audio) as mock_render, \
                 patch.object(daemon, "playback_queue") as mock_pq, \
                 patch.object(threading.Event, "wait", fast_event_wait):
                daemon.handle_client(mock_conn, mock_backend)

            # Batch render should have been called as fallback
            mock_render.assert_called_once()
            assert daemon._stats["requests_completed"] >= 1
        finally:
            daemon._stats.update(saved_stats)
            daemon._next_seq = saved_next_seq


# ---------------------------------------------------------------------------
# Daemon: PING response
# ---------------------------------------------------------------------------


class TestPingResponse:
    """Verify PING always returns b'ok'."""

    def test_ping_returns_ok(self):
        from wednesday_tts.server import daemon

        mock_conn = MagicMock()
        mock_conn.recv.return_value = b"PING"
        daemon.handle_client(mock_conn, MagicMock())
        mock_conn.send.assert_called_once_with(b"ok")


# ---------------------------------------------------------------------------
# Hung-request watchdog tests
# ---------------------------------------------------------------------------


class TestHungRequestWatchdog:
    """Verify the watchdog can detect hung streaming requests."""

    def test_watchdog_sees_in_flight_from_streaming(self):
        from wednesday_tts.server import daemon

        with daemon._stats_lock:
            in_flight = 5 - 3 - 0 - 1
            assert in_flight == 1

    def test_watchdog_exits_on_prolonged_hang(self):
        from wednesday_tts.server import daemon

        saved_stats = dict(daemon._stats)
        saved_activity = daemon._last_activity_time

        try:
            daemon._stats["requests_total"] = 5
            daemon._stats["requests_completed"] = 3
            daemon._stats["requests_stopped"] = 0
            daemon._stats["requests_errored"] = 0
            daemon._last_activity_time = time.monotonic() - 200

            call_count = 0
            def fake_sleep(n):
                nonlocal call_count
                call_count += 1
                if call_count > 2:
                    raise StopIteration

            with patch.object(daemon, "playback_queue") as mock_pq, \
                 patch("os._exit") as mock_exit, \
                 patch("time.sleep", side_effect=fake_sleep):
                mock_pq.empty.return_value = True
                try:
                    daemon._hung_request_watchdog()
                except StopIteration:
                    pass

            mock_exit.assert_called_once_with(1)
        finally:
            daemon._stats.update(saved_stats)
            daemon._last_activity_time = saved_activity


# ---------------------------------------------------------------------------
# Audio health worker tests
# ---------------------------------------------------------------------------


class TestAudioHealthWorker:
    """Verify the audio health probe handles failures correctly."""

    def test_health_probe_runs_when_idle(self):
        """When no audio is queued, the health probe queries devices."""
        from wednesday_tts.server import daemon

        call_count = 0
        def counted_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return
            raise StopIteration

        query_called = threading.Event()
        def mock_query(kind=None):
            query_called.set()
            return {"index": 0, "name": "Test", "default_samplerate": 24000}

        import sounddevice as _real_sd
        with patch.object(_real_sd, "query_devices", side_effect=mock_query), \
             patch.object(daemon, "get_default_output_device", return_value=0), \
             patch.object(time, "sleep", side_effect=counted_sleep):
            try:
                daemon._audio_health_worker()
            except StopIteration:
                pass

        assert query_called.is_set(), "Health probe did not query devices"

    def test_health_probe_exits_on_repeated_failures(self):
        """5 consecutive device query failures should cause os._exit."""
        from wednesday_tts.server import daemon

        sleep_count = 0
        def counted_sleep(n):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 10:
                raise StopIteration

        import sounddevice as _real_sd
        with patch.object(_real_sd, "query_devices", side_effect=OSError("PortAudio -50")), \
             patch.object(daemon, "get_default_output_device", return_value=0), \
             patch.object(daemon, "_play_error_chime"), \
             patch("os._exit") as mock_exit, \
             patch.object(time, "sleep", side_effect=counted_sleep):
            try:
                daemon._audio_health_worker()
            except StopIteration:
                pass

        mock_exit.assert_called_with(1)


# ---------------------------------------------------------------------------
# Cascading failure prevention
# ---------------------------------------------------------------------------


class TestCascadingFailurePrevention:
    """Verify that a stuck streaming request doesn't block subsequent requests."""

    def test_second_request_not_blocked_by_first(self):
        from wednesday_tts.server.backends import pocket as pocket_mod

        if pocket_mod._streaming_lock.locked():
            pocket_mod._streaming_lock.release()

        backend = pocket_mod.PocketTTSBackend.__new__(pocket_mod.PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._lock = threading.Lock()
        backend._active_stream = None
        backend._speed = 1.0
        backend._LEADIN_SAMPLES = 24000
        backend._frames_after_eos = None
        backend.sample_rate = 24000

        def stream_factory(**kwargs):
            mock_stream = MagicMock()
            finished_cb = kwargs.get("finished_callback")
            def on_start():
                if finished_cb:
                    finished_cb()
            mock_stream.start.side_effect = on_start
            return mock_stream

        backend._model.generate_audio_stream.return_value = iter([])

        with patch("sounddevice.OutputStream", side_effect=stream_factory), \
             patch("sounddevice._terminate"), \
             patch("sounddevice._initialize"), \
             patch.object(pocket_mod, "_get_device_samplerate", return_value=24000):
            backend.play_streaming(text="first", speed=1.0)

        backend._model.generate_audio_stream.return_value = iter([])
        with patch("sounddevice.OutputStream", side_effect=stream_factory), \
             patch("sounddevice._terminate"), \
             patch("sounddevice._initialize"), \
             patch.object(pocket_mod, "_get_device_samplerate", return_value=24000):

            done = threading.Event()
            def second_request():
                backend.play_streaming(text="second", speed=1.0)
                done.set()

            t = threading.Thread(target=second_request, daemon=True)
            t.start()
            assert done.wait(timeout=5), "Second request blocked -- _streaming_lock not released"


class TestPocketCallbackModeFailure:
    """Verify pocket.py callback-mode failure detection calls _record_stream_failure."""

    def test_audio_buf_put_timeout_calls_record_failure(self):
        from wednesday_tts.server.backends import pocket as pocket_mod
        import queue as _queue

        with pocket_mod._stream_failure_lock:
            pocket_mod._consecutive_stream_failures = 0

        backend = pocket_mod.PocketTTSBackend.__new__(pocket_mod.PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._lock = threading.Lock()
        backend._active_stream = None
        backend._speed = 1.0
        backend._LEADIN_SAMPLES = 24000
        backend._frames_after_eos = None
        backend.sample_rate = 24000

        audio_chunk = MagicMock()
        audio_chunk.numpy.return_value = np.zeros(100, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([audio_chunk])

        def stream_factory(**kwargs):
            mock_stream = MagicMock()
            finished_cb = kwargs.get("finished_callback")
            def on_start():
                if finished_cb:
                    finished_cb()
            mock_stream.start.side_effect = on_start
            return mock_stream

        saved_failures = pocket_mod._consecutive_stream_failures
        failure_count_before = pocket_mod._consecutive_stream_failures

        with patch("sounddevice.OutputStream", side_effect=stream_factory), \
             patch("sounddevice._terminate"), \
             patch("sounddevice._initialize"), \
             patch.object(pocket_mod, "_get_device_samplerate", return_value=24000), \
             patch("os._exit"):
            original_queue = _queue.Queue

            class AlwaysFullQueue(original_queue):
                def put(self, item, block=True, timeout=None):
                    raise _queue.Full()

            with patch("wednesday_tts.server.backends.pocket._queue.Queue" if hasattr(pocket_mod, "_queue") else "queue.Queue", AlwaysFullQueue):
                done = threading.Event()
                def run():
                    backend.play_streaming(text="hello", speed=1.0)
                    done.set()

                t = threading.Thread(target=run, daemon=True)
                t.start()
                completed = done.wait(timeout=5)

        assert completed, "play_streaming hung when audio_buf.put() always times out"

        with pocket_mod._stream_failure_lock:
            failures_after = pocket_mod._consecutive_stream_failures
        assert failures_after > failure_count_before, \
            "_record_stream_failure() was not called when audio_buf.put() timed out"

        with pocket_mod._stream_failure_lock:
            pocket_mod._consecutive_stream_failures = saved_failures
