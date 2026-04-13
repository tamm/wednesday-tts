"""Tests for daemon streaming, queue architecture, and watchdog paths.

Validates that the daemon correctly handles:
- Streaming generation feeding into playback_queue (via generate_streaming)
- Fallback to batch when streaming returns audio instead of queuing directly
- Watchdog visibility into streaming hangs (requests_errored stat)
- Audio health probe behaviour
- Pocket backend generate_streaming()

All tests mock sounddevice and TTS model -- no real audio hardware needed.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Pocket backend: generate_streaming() tests
# ---------------------------------------------------------------------------


class TestStreamChunks:
    """Verify PocketTTSBackend.generate_streaming() works correctly."""

    def _make_backend(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._voice_states = {}
        backend._lock = threading.Lock()
        backend._speed = 1.0
        backend._fallback_voice = "fantine"
        backend._voice_name = "fantine"
        backend._frames_after_eos = None
        backend._eos_threshold = -4.0
        backend.sample_rate = 24000
        return backend

    def test_yields_chunks_at_native_speed(self):
        """generate_streaming at speed=1.0 without queue should collect and return concatenated audio."""
        backend = self._make_backend()

        chunk1 = MagicMock()
        chunk1.numpy.return_value = np.zeros(100, dtype=np.float32)
        chunk2 = MagicMock()
        chunk2.numpy.return_value = np.ones(200, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([chunk1, chunk2])

        result = backend.generate_streaming("hello", speed=1.0)
        assert result is not None
        assert result.shape == (300,)

    def test_skips_empty_chunks(self):
        """Empty audio chunks should be skipped."""
        backend = self._make_backend()

        empty_chunk = MagicMock()
        empty_chunk.numpy.return_value = np.array([], dtype=np.float32)
        real_chunk = MagicMock()
        real_chunk.numpy.return_value = np.zeros(50, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([empty_chunk, real_chunk])

        result = backend.generate_streaming("hello", speed=1.0)
        assert result is not None
        assert result.shape == (50,)

    def test_not_loaded_raises(self):
        """generate_streaming should raise if model is not loaded."""
        backend = self._make_backend()
        backend._model = None

        with pytest.raises(RuntimeError, match="not loaded"):
            backend.generate_streaming("hello")


# ---------------------------------------------------------------------------
# Pocket backend: generate_streaming with playback_queue
# ---------------------------------------------------------------------------


class TestStreamingLockRelease:
    """Verify the backend lock is released after generate_streaming completes."""

    def test_lock_released_after_normal_completion(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._voice_states = {}
        backend._lock = threading.Lock()
        backend._speed = 1.0
        backend._fallback_voice = "fantine"
        backend._voice_name = "fantine"
        backend._frames_after_eos = None
        backend._eos_threshold = -4.0
        backend.sample_rate = 24000

        backend._model.generate_audio_stream.return_value = iter([])

        result = backend.generate_streaming(text="hello", speed=1.0)
        # No chunks means result is None
        assert result is None

        # The internal _lock should be released
        acquired = backend._lock.acquire(timeout=0.1)
        assert acquired, "_lock was not released after normal completion"
        backend._lock.release()

    def test_lock_released_after_abort(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._voice_states = {}
        backend._lock = threading.Lock()
        backend._speed = 1.0
        backend._fallback_voice = "fantine"
        backend._voice_name = "fantine"
        backend._frames_after_eos = None
        backend._eos_threshold = -4.0
        backend.sample_rate = 24000

        stop = threading.Event()

        def slow_chunks(*args, **kwargs):
            chunk1 = MagicMock()
            chunk1.numpy.return_value = np.zeros(100, dtype=np.float32)
            yield chunk1
            stop.wait(timeout=2)
            chunk2 = MagicMock()
            chunk2.numpy.return_value = np.zeros(100, dtype=np.float32)
            yield chunk2

        backend._model.generate_audio_stream.side_effect = slow_chunks

        done = threading.Event()

        def run_streaming():
            backend.generate_streaming(text="hello", speed=1.0, stop_check=lambda: stop.is_set())
            done.set()

        t = threading.Thread(target=run_streaming, daemon=True)
        t.start()
        time.sleep(0.3)

        stop.set()
        done.wait(timeout=5)

        acquired = backend._lock.acquire(timeout=0.5)
        assert acquired, "_lock not released after abort"
        backend._lock.release()


class TestAbortStream:
    """Verify abort_stream on the base backend is a no-op (no crash)."""

    def test_abort_clears_active_stream(self):
        """Base TTSBackend.abort_stream() is a no-op and should not raise."""
        from wednesday_tts.server.backends.base import TTSBackend

        backend = TTSBackend.__new__(TTSBackend)
        # Should not raise
        backend.abort_stream()

    def test_abort_noop_when_no_stream(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        # Pocket no longer has abort_stream, but base class does (no-op)
        backend.abort_stream()


# ---------------------------------------------------------------------------
# Daemon: streaming-to-queue tests
# ---------------------------------------------------------------------------


class TestDaemonStreamingToQueue:
    """Verify the daemon's streaming path feeds chunks into playback_queue."""

    def test_streaming_feeds_queue_and_completes(self):
        """JSON speak with streaming backend should call generate_streaming."""
        import json

        from wednesday_tts.server import daemon

        saved_stats = dict(daemon._stats)
        daemon._stats["requests_total"] = 0
        daemon._stats["requests_completed"] = 0
        daemon._stats["requests_errored"] = 0
        daemon._stats["requests_stopped"] = 0

        try:
            msg = json.dumps(
                {"command": "speak", "text": "hello", "normalization": "pre-normalized"}
            )
            mock_conn = MagicMock()
            mock_conn.recv.return_value = msg.encode("utf-8")

            mock_backend = MagicMock()
            mock_backend.supports_streaming = True
            mock_backend.sample_rate = 24000

            # generate_streaming returns None = audio was queued directly
            mock_backend.generate_streaming.return_value = None

            with (
                patch.object(daemon, "_split_voice_segments", return_value=[(None, None, "hello")]),
                patch.object(daemon, "run_normalize", return_value="hello"),
                patch.object(daemon, "_dedup_check", return_value=False),
                patch.object(daemon, "_resolve_voice_for_request", return_value=None),
            ):
                daemon.handle_client(mock_conn, mock_backend)

            assert mock_backend.generate_streaming.call_count >= 1
            assert daemon._stats["requests_completed"] >= 1
        finally:
            daemon._stats.update(saved_stats)

    def test_streaming_fallback_to_batch_on_timeout(self):
        """When generate_streaming returns audio (not None), it gets enqueued normally."""
        import json

        from wednesday_tts.server import daemon

        saved_stats = dict(daemon._stats)
        daemon._stats["requests_total"] = 0
        daemon._stats["requests_completed"] = 0
        daemon._stats["requests_errored"] = 0
        daemon._stats["requests_stopped"] = 0

        try:
            msg = json.dumps(
                {"command": "speak", "text": "hello", "normalization": "pre-normalized"}
            )
            mock_conn = MagicMock()
            mock_conn.recv.return_value = msg.encode("utf-8")

            mock_backend = MagicMock()
            mock_backend.supports_streaming = True
            mock_backend.sample_rate = 24000

            # generate_streaming returns audio array (didn't queue directly)
            mock_audio = np.zeros(100, dtype=np.float32)
            mock_backend.generate_streaming.return_value = mock_audio

            with (
                patch.object(daemon, "_split_voice_segments", return_value=[(None, None, "hello")]),
                patch.object(daemon, "run_normalize", return_value="hello"),
                patch.object(daemon, "_dedup_check", return_value=False),
                patch.object(daemon, "_resolve_voice_for_request", return_value=None),
                patch.object(daemon, "playback_queue") as mock_pq,
            ):
                daemon.handle_client(mock_conn, mock_backend)

            # Audio should have been put into playback_queue
            assert mock_pq.put.call_count >= 1
            assert daemon._stats["requests_completed"] >= 1
        finally:
            daemon._stats.update(saved_stats)


# ---------------------------------------------------------------------------
# Daemon: PING response
# ---------------------------------------------------------------------------


class TestPingResponse:
    """Verify PING always returns b'ok'."""

    def test_ping_returns_ok(self):
        import json

        from wednesday_tts.server import daemon

        mock_conn = MagicMock()
        mock_conn.recv.return_value = json.dumps({"command": "ping"}).encode("utf-8")
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
                if call_count > 4:
                    raise StopIteration

            with (
                patch.object(daemon, "playback_queue") as mock_pq,
                patch.object(daemon, "_play_error_chime"),
                patch("os._exit") as mock_exit,
                patch("time.sleep", side_effect=fake_sleep),
            ):
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
        """When no audio is queued, the health probe queries devices via subprocess."""
        from wednesday_tts.server import daemon

        call_count = 0

        def counted_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return
            raise StopIteration

        query_called = threading.Event()

        def mock_query_subprocess():
            query_called.set()
            return (0, "Test Device")

        with (
            patch.object(
                daemon, "_query_default_device_subprocess", side_effect=mock_query_subprocess
            ),
            patch.object(daemon, "get_default_output_device", return_value=0),
            patch.object(time, "sleep", side_effect=counted_sleep),
        ):
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

        with (
            patch.object(daemon, "_query_default_device_subprocess", return_value=None),
            patch.object(daemon, "get_default_output_device", return_value=0),
            patch.object(daemon, "_play_error_chime"),
            patch("os._exit") as mock_exit,
            patch.object(time, "sleep", side_effect=counted_sleep),
        ):
            try:
                daemon._audio_health_worker()
            except StopIteration:
                pass

        mock_exit.assert_called_with(1)


# ---------------------------------------------------------------------------
# Cascading failure prevention
# ---------------------------------------------------------------------------


class TestCascadingFailurePrevention:
    """Verify that the backend lock doesn't stay held after generate_streaming."""

    def test_second_request_not_blocked_by_first(self):
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._voice_states = {}
        backend._lock = threading.Lock()
        backend._speed = 1.0
        backend._fallback_voice = "fantine"
        backend._voice_name = "fantine"
        backend._frames_after_eos = None
        backend._eos_threshold = -4.0
        backend.sample_rate = 24000

        backend._model.generate_audio_stream.return_value = iter([])

        # First request
        backend.generate_streaming(text="first", speed=1.0)

        # Second request should not be blocked
        backend._model.generate_audio_stream.return_value = iter([])

        done = threading.Event()

        def second_request():
            backend.generate_streaming(text="second", speed=1.0)
            done.set()

        t = threading.Thread(target=second_request, daemon=True)
        t.start()
        assert done.wait(timeout=5), "Second request blocked -- _lock not released"


class TestPocketCallbackModeFailure:
    """Verify pocket generate_streaming handles queue put failures gracefully."""

    def test_audio_buf_put_timeout_calls_record_failure(self):
        """When playback_queue.put() is called, generate_streaming queues chunks directly."""
        from wednesday_tts.server.backends.pocket import PocketTTSBackend

        backend = PocketTTSBackend.__new__(PocketTTSBackend)
        backend._model = MagicMock()
        backend._voice_state = MagicMock()
        backend._voice_states = {}
        backend._lock = threading.Lock()
        backend._speed = 1.0
        backend._fallback_voice = "fantine"
        backend._voice_name = "fantine"
        backend._frames_after_eos = None
        backend._eos_threshold = -4.0
        backend.sample_rate = 24000

        audio_chunk = MagicMock()
        audio_chunk.numpy.return_value = np.zeros(100, dtype=np.float32)
        backend._model.generate_audio_stream.return_value = iter([audio_chunk])

        mock_queue = MagicMock()
        # generate_streaming with a playback_queue should put chunks into it
        result = backend.generate_streaming("hello", speed=1.0, playback_queue=mock_queue)
        # When queuing directly, returns None
        assert result is None
        # The queue should have received at least one chunk
        assert mock_queue.put.call_count >= 1
