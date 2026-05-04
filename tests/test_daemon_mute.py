"""Tests for /tmp/tts-mute sentinel honouring in the TTS daemon.

Verifies:
- _is_muted() returns False when file is absent, True when present.
- speak command short-circuits (no audio) when muted.
- chirp command short-circuits (no audio) when muted.
- Audio resumes normally after mute file is removed.
"""

from __future__ import annotations

import json
import os
import socket
from unittest.mock import MagicMock, call, patch


def _get_daemon():
    import wednesday_tts.server.daemon as d

    return d


# ---------------------------------------------------------------------------
# _is_muted
# ---------------------------------------------------------------------------


def test_is_muted_absent(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    with patch.object(d, "_MUTE_PATH", mute_file):
        assert d._is_muted() is False


def test_is_muted_present(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    open(mute_file, "w").close()
    with patch.object(d, "_MUTE_PATH", mute_file):
        assert d._is_muted() is True


# ---------------------------------------------------------------------------
# speak — muted path
# ---------------------------------------------------------------------------


def test_speak_muted_does_not_call_process_speak(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    open(mute_file, "w").close()

    conn = MagicMock()
    backend = MagicMock()
    msg = json.dumps({"command": "speak", "text": "hello"}).encode()
    conn.recv.return_value = msg

    with patch.object(d, "_MUTE_PATH", mute_file), patch.object(
        d, "_process_speak"
    ) as mock_speak:
        d.handle_client(conn, backend)
        mock_speak.assert_not_called()

    conn.send.assert_called_with(b"ok")


def test_speak_unmuted_calls_process_speak(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    # file absent → not muted

    conn = MagicMock()
    backend = MagicMock()
    msg = json.dumps({"command": "speak", "text": "hello"}).encode()
    conn.recv.return_value = msg

    with patch.object(d, "_MUTE_PATH", mute_file), patch.object(
        d, "_process_speak"
    ) as mock_speak:
        d.handle_client(conn, backend)
        mock_speak.assert_called_once()


# ---------------------------------------------------------------------------
# chirp — muted path
# ---------------------------------------------------------------------------


def test_chirp_muted_does_not_play(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    open(mute_file, "w").close()

    conn = MagicMock()
    backend = MagicMock()
    msg = json.dumps({"command": "chirp"}).encode()
    conn.recv.return_value = msg

    with patch.object(d, "_MUTE_PATH", mute_file), patch.object(
        d, "_play_voice_command_chirp"
    ) as mock_chirp:
        d.handle_client(conn, backend)
        mock_chirp.assert_not_called()

    conn.send.assert_called_with(b"ok")


def test_chirp_unmuted_plays(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    # file absent → not muted

    conn = MagicMock()
    backend = MagicMock()
    msg = json.dumps({"command": "chirp"}).encode()
    conn.recv.return_value = msg

    with patch.object(d, "_MUTE_PATH", mute_file), patch.object(
        d, "_play_voice_command_chirp"
    ) as mock_chirp:
        d.handle_client(conn, backend)
        mock_chirp.assert_called_once()


# ---------------------------------------------------------------------------
# toggle mid-test: mute → unmute → next call works
# ---------------------------------------------------------------------------


def test_speak_toggle_mute_unmute(tmp_path):
    d = _get_daemon()
    mute_file = str(tmp_path / "tts-mute")
    open(mute_file, "w").close()

    conn = MagicMock()
    backend = MagicMock()

    def make_conn(text):
        c = MagicMock()
        c.recv.return_value = json.dumps({"command": "speak", "text": text}).encode()
        return c

    with patch.object(d, "_MUTE_PATH", mute_file), patch.object(
        d, "_process_speak"
    ) as mock_speak:
        # Muted — no speak
        d.handle_client(make_conn("first"), backend)
        assert mock_speak.call_count == 0

        # Remove mute file → unmuted
        os.unlink(mute_file)

        # Unmuted — speak fires
        d.handle_client(make_conn("second"), backend)
        assert mock_speak.call_count == 1
