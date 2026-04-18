"""Tests for Stop-hook TTS preemption behaviour.

When a speak request arrives with flush_session=True (Stop hook), the daemon
must:

1. Let the currently-playing audio chunk finish naturally (no _skip_gen truncation).
2. Drain all queued chunks from the same session.
3. Leave other sessions' playback and queue completely untouched.
4. Prepend "Oh! " to the Stop-hook message text.

These tests operate directly on daemon module globals and helper functions —
no real audio hardware is exercised.
"""

from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch


def _get_daemon():
    """Return the daemon module, using the cached import if already loaded."""
    import wednesday_tts.server.daemon as d

    return d


def _drain_playback_queue(d) -> list:
    """Drain and return all items from the playback queue."""
    items = []
    while True:
        try:
            item = d.playback_queue.get_nowait()
            d.playback_queue.task_done()
            items.append(item)
        except queue.Empty:
            break
    return items


def _reset_daemon_state(d) -> None:
    """Reset daemon globals to a clean state before each test."""
    # Drain playback queue
    _drain_playback_queue(d)
    # Clear session registry
    with d._msg_session_lock:
        d._msg_session.clear()
    # Clear skip sets
    d._skip_msg_ids.clear()
    # Clear msg_done
    with d._msg_done_lock:
        d._msg_done.clear()
    # Clear deferred buffer
    d._playback_deferred.clear()
    # Reset current-playing trackers
    d._playback_current_msg_id = -1
    d._playing_msg_id = -1
    # Clear barge-in state
    with d._barge_in_lock:
        d._barge_in_pending.clear()
        d._barge_in_dropped_once = False


# ---------------------------------------------------------------------------
# _flush_session: current chunk is NOT truncated
# ---------------------------------------------------------------------------


class TestFlushSessionDoesNotTruncateCurrentChunk:
    """_flush_session must not increment _stop_gen or _skip_gen for the current chunk."""

    def setup_method(self):
        d = _get_daemon()
        _reset_daemon_state(d)

    def test_stop_gen_unchanged_when_current_chunk_belongs_to_session(self):
        """_flush_session must not touch _stop_gen (no global playback kill)."""
        d = _get_daemon()
        session_id = "sess-aaa"
        # Register a message as belonging to this session
        with d._msg_session_lock:
            d._msg_session[42] = session_id
        # Simulate this message being the currently playing chunk
        d._playback_current_msg_id = 42

        stop_gen_before = d._stop_gen

        with d._speak_pipeline_lock:
            d._flush_session(session_id)

        assert d._stop_gen == stop_gen_before, (
            "_stop_gen must not be modified by _flush_session — other sessions share it"
        )

    def test_skip_gen_unchanged_when_current_chunk_belongs_to_session(self):
        """_flush_session must not increment _skip_gen for the current chunk."""
        d = _get_daemon()
        session_id = "sess-bbb"
        with d._msg_session_lock:
            d._msg_session[43] = session_id
        d._playback_current_msg_id = 43

        skip_gen_before = d._skip_gen

        with d._speak_pipeline_lock:
            d._flush_session(session_id)

        assert d._skip_gen == skip_gen_before, (
            "_skip_gen must not be incremented by _flush_session "
            "(that would truncate the current chunk)"
        )

    def test_skip_gen_unchanged_when_no_current_chunk(self):
        """_flush_session with nothing currently playing must not touch _skip_gen."""
        d = _get_daemon()
        session_id = "sess-ccc"
        with d._msg_session_lock:
            d._msg_session[44] = session_id
        d._playback_current_msg_id = -1  # nothing playing

        skip_gen_before = d._skip_gen

        with d._speak_pipeline_lock:
            d._flush_session(session_id)

        assert d._skip_gen == skip_gen_before


# ---------------------------------------------------------------------------
# _flush_session: queued chunks from same session are drained
# ---------------------------------------------------------------------------


class TestFlushSessionDrainsQueue:
    """_flush_session must drop queued chunks from the flushed session."""

    def setup_method(self):
        d = _get_daemon()
        _reset_daemon_state(d)

    def _enqueue_chunk(self, d, msg_id: int, session_id: str, seq: int = 0) -> tuple:
        """Enqueue a fake audio chunk tuple and register its session."""
        import numpy as np

        chunk = (np.zeros(100, dtype=np.float32), 24000, msg_id, seq)
        with d._msg_session_lock:
            d._msg_session[msg_id] = session_id
        d.playback_queue.put(chunk)
        return chunk

    def test_same_session_queued_chunks_are_dropped(self):
        """Queued chunks from the flushed session must not survive the drain."""
        d = _get_daemon()
        session_id = "sess-flush-1"

        self._enqueue_chunk(d, msg_id=10, session_id=session_id)
        self._enqueue_chunk(d, msg_id=11, session_id=session_id)

        with d._speak_pipeline_lock:
            d._flush_session(session_id)

        remaining = _drain_playback_queue(d)
        msg_ids_remaining = [item[2] for item in remaining if isinstance(item, tuple) and len(item) >= 3]
        assert 10 not in msg_ids_remaining, "msg_id=10 (same session) must be dropped"
        assert 11 not in msg_ids_remaining, "msg_id=11 (same session) must be dropped"

    def test_other_session_queued_chunks_survive(self):
        """Queued chunks from OTHER sessions must survive the flush."""
        d = _get_daemon()
        session_flush = "sess-flush-2"
        session_other = "sess-other-2"

        self._enqueue_chunk(d, msg_id=20, session_id=session_flush)
        self._enqueue_chunk(d, msg_id=21, session_id=session_other)

        with d._speak_pipeline_lock:
            d._flush_session(session_flush)

        remaining = _drain_playback_queue(d)
        msg_ids_remaining = [item[2] for item in remaining if isinstance(item, tuple) and len(item) >= 3]
        assert 20 not in msg_ids_remaining, "same-session chunk must be dropped"
        assert 21 in msg_ids_remaining, "other-session chunk must survive"

    def test_mixed_queue_only_session_chunks_dropped(self):
        """With a mixed queue, only the target session's chunks are removed."""
        d = _get_daemon()
        session_a = "sess-aaa-3"
        session_b = "sess-bbb-3"
        session_c = "sess-ccc-3"

        self._enqueue_chunk(d, msg_id=30, session_id=session_a)
        self._enqueue_chunk(d, msg_id=31, session_id=session_b)
        self._enqueue_chunk(d, msg_id=32, session_id=session_a)
        self._enqueue_chunk(d, msg_id=33, session_id=session_c)

        with d._speak_pipeline_lock:
            d._flush_session(session_b)

        remaining = _drain_playback_queue(d)
        msg_ids_remaining = [item[2] for item in remaining if isinstance(item, tuple) and len(item) >= 3]
        assert 31 not in msg_ids_remaining, "session_b chunk must be dropped"
        assert 30 in msg_ids_remaining, "session_a chunks must survive"
        assert 32 in msg_ids_remaining, "session_a chunks must survive"
        assert 33 in msg_ids_remaining, "session_c chunk must survive"


# ---------------------------------------------------------------------------
# _flush_session: cross-session isolation for currently-playing chunk
# ---------------------------------------------------------------------------


class TestFlushSessionCrossSessionIsolation:
    """A flush for session A must not affect session B's currently playing audio."""

    def setup_method(self):
        d = _get_daemon()
        _reset_daemon_state(d)

    def test_other_session_current_chunk_not_killed(self):
        """If another session is playing, skip_gen must stay unchanged."""
        d = _get_daemon()
        session_flush = "sess-flush-iso"
        session_playing = "sess-playing-iso"

        # Register some queued msg for the session to flush
        with d._msg_session_lock:
            d._msg_session[50] = session_flush
        # Simulate a DIFFERENT session playing
        d._playback_current_msg_id = 99
        with d._msg_session_lock:
            d._msg_session[99] = session_playing

        skip_gen_before = d._skip_gen

        with d._speak_pipeline_lock:
            d._flush_session(session_flush)

        assert d._skip_gen == skip_gen_before, (
            "skip_gen must not change when a different session is playing"
        )

    def test_no_session_msgs_is_noop(self):
        """_flush_session with no matching msg_ids returns early without touching anything."""
        d = _get_daemon()
        skip_gen_before = d._skip_gen
        stop_gen_before = d._stop_gen

        with d._speak_pipeline_lock:
            d._flush_session("session-with-no-messages")

        assert d._skip_gen == skip_gen_before
        assert d._stop_gen == stop_gen_before


# ---------------------------------------------------------------------------
# "Oh! " prefix is prepended by _process_speak_locked
# ---------------------------------------------------------------------------


class TestOhPrefixPrepended:
    """When flush_session=True, _process_speak_locked must prepend 'Oh! ' to text."""

    def setup_method(self):
        d = _get_daemon()
        _reset_daemon_state(d)

    def test_oh_prefix_prepended_to_stop_hook_text(self):
        """The TTS engine must receive text starting with 'Oh! '."""
        d = _get_daemon()
        session_id = "sess-oh-1"
        received_texts: list[str] = []

        # Build a mock backend that records what text it receives
        mock_backend = MagicMock()
        mock_backend.supports_streaming = False

        def _capture_generate(text, **kwargs):
            received_texts.append(text)
            return None  # simulate no audio (dedup / empty)

        mock_backend.generate.side_effect = _capture_generate

        msg = {
            "command": "speak",
            "text": "Here is the final response.",
            "session_id": session_id,
            "flush_session": True,
            "source": "stop",
        }

        # Patch the expensive bits so we don't need a model loaded
        with (
            patch.object(d, "_flush_session"),
            patch.object(d, "_is_barge_in_fresh", return_value=False),
            patch.object(d, "_dedup_check", return_value=False),
            patch.object(
                d,
                "_render_segments",
                side_effect=lambda *a, **kw: received_texts.append(a[0][0][2] if a[0] else ""),
            ),
            patch.object(d, "_split_voice_segments", side_effect=lambda t: [(None, None, t)]),
            patch.object(d, "run_normalize", side_effect=lambda t, **kw: t),
            patch.object(d, "_resolve_voice_for_request", return_value={"name": "default", "voice": "x"}),
        ):
            with d._speak_pipeline_lock:
                d._process_speak_locked(msg, mock_backend)

        # The text passed into _split_voice_segments (and then _render_segments)
        # should start with "Oh! "
        assert any(t.startswith("Oh! ") for t in received_texts), (
            f"Expected text starting with 'Oh! ', got: {received_texts!r}"
        )

    def test_oh_prefix_literal_value(self):
        """The prefix must be exactly 'Oh! ' — capital O, exclamation, single space."""
        d = _get_daemon()
        session_id = "sess-oh-2"
        split_calls: list[str] = []

        msg = {
            "command": "speak",
            "text": "Done.",
            "session_id": session_id,
            "flush_session": True,
            "source": "stop",
        }

        with (
            patch.object(d, "_flush_session"),
            patch.object(d, "_is_barge_in_fresh", return_value=False),
            patch.object(d, "_dedup_check", return_value=True),  # early-exit after we capture text
            patch.object(
                d,
                "_split_voice_segments",
                side_effect=lambda t: (split_calls.append(t), [(None, None, t)])[1],
            ),
            patch.object(d, "run_normalize", side_effect=lambda t, **kw: t),
            patch.object(d, "_resolve_voice_for_request", return_value={"name": "default", "voice": "x"}),
        ):
            with d._speak_pipeline_lock:
                d._process_speak_locked(msg, MagicMock())

        assert split_calls, "Expected _split_voice_segments to be called"
        assert split_calls[0] == "Oh! Done.", (
            f"Expected 'Oh! Done.', got: {split_calls[0]!r}"
        )

    def test_no_prefix_without_flush_session(self):
        """Regular speaks (no flush_session) must NOT get the 'Oh! ' prefix."""
        d = _get_daemon()
        split_calls: list[str] = []

        msg = {
            "command": "speak",
            "text": "Normal message.",
            "session_id": "sess-no-oh",
            "source": "pre-tool",
        }

        with (
            patch.object(d, "_is_barge_in_fresh", return_value=False),
            patch.object(d, "_dedup_check", return_value=True),
            patch.object(
                d,
                "_split_voice_segments",
                side_effect=lambda t: (split_calls.append(t), [(None, None, t)])[1],
            ),
            patch.object(d, "run_normalize", side_effect=lambda t, **kw: t),
            patch.object(d, "_resolve_voice_for_request", return_value={"name": "default", "voice": "x"}),
        ):
            with d._speak_pipeline_lock:
                d._process_speak_locked(msg, MagicMock())

        assert split_calls, "Expected _split_voice_segments to be called"
        assert not split_calls[0].startswith("Oh! "), (
            f"Non-flush-session text must not start with 'Oh! ', got: {split_calls[0]!r}"
        )
