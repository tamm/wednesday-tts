"""Regression tests for barge-in pending-list clearing on stop.

Verifies that _stop_playback() clears _barge_in_pending and resets
_barge_in_dropped_once. This ensures that when UserPromptSubmit fires
stop-tts.sh (which sends {"command":"stop"}), held speaks are dropped
rather than replayed after the user finishes dictating.
"""

from __future__ import annotations

import queue
import sys
from unittest.mock import MagicMock

# Stub heavy native deps — no audio hardware needed.
for _mod in ("sounddevice", "soundfile"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _get_daemon():
    """Import daemon module, re-using cached import if already loaded."""
    import wednesday_tts.server.daemon as d
    return d


class TestStopClearsBargeinPending:
    """_stop_playback must clear barge-in state in all stop scenarios."""

    def setup_method(self):
        """Reset daemon barge-in globals and drain the playback queue before each test."""
        d = _get_daemon()
        with d._barge_in_lock:
            d._barge_in_pending.clear()
            d._barge_in_dropped_once = False
        # Drain the playback queue so _stop_playback's queue-drain loop exits fast.
        while True:
            try:
                d.playback_queue.get_nowait()
                d.playback_queue.task_done()
            except queue.Empty:
                break

    def test_stop_clears_pending_list(self):
        """_stop_playback drains _barge_in_pending."""
        d = _get_daemon()
        with d._barge_in_lock:
            d._barge_in_pending.append({"command": "speak", "text": "held message"})

        d._stop_playback()

        with d._barge_in_lock:
            assert d._barge_in_pending == [], (
                "_barge_in_pending must be empty after stop"
            )

    def test_stop_resets_dropped_once(self):
        """_stop_playback resets _barge_in_dropped_once to False."""
        d = _get_daemon()
        with d._barge_in_lock:
            d._barge_in_dropped_once = True

        d._stop_playback()

        with d._barge_in_lock:
            assert d._barge_in_dropped_once is False, (
                "_barge_in_dropped_once must be False after stop"
            )

    def test_stop_clears_multiple_pending(self):
        """_stop_playback clears all held speaks regardless of count."""
        d = _get_daemon()
        with d._barge_in_lock:
            for i in range(5):
                d._barge_in_pending.append({"command": "speak", "text": f"msg {i}"})
            d._barge_in_dropped_once = True

        d._stop_playback()

        with d._barge_in_lock:
            assert d._barge_in_pending == []
            assert d._barge_in_dropped_once is False

    def test_stop_no_pending_is_safe(self):
        """_stop_playback with empty _barge_in_pending does not raise."""
        d = _get_daemon()
        with d._barge_in_lock:
            assert d._barge_in_pending == []

        d._stop_playback()  # must not raise

        with d._barge_in_lock:
            assert d._barge_in_pending == []
            assert d._barge_in_dropped_once is False
