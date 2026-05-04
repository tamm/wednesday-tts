"""Tests for the ``chirp`` daemon command.

Verifies that:
- The ``chirp`` command is dispatched correctly by ``handle_client``.
- ``_get_voice_command_chirp_path`` reads the configured path from tts-config.json.
- ``_get_voice_command_chirp_path`` returns None when the key is absent or the file
  does not exist.
- ``_play_voice_command_chirp`` uses ``afplay`` when a valid path is configured.
- ``_play_voice_command_chirp`` falls back to the synthesised chirp when no file is
  configured.

No real audio hardware is exercised — sounddevice and subprocess are mocked.
"""

from __future__ import annotations

import json
import os
import socket
from unittest.mock import MagicMock, patch


def _get_daemon():
    import wednesday_tts.server.daemon as d

    return d


# ---------------------------------------------------------------------------
# _get_voice_command_chirp_path
# ---------------------------------------------------------------------------


class TestGetVoiceCommandChirpPath:
    def test_returns_none_when_no_config(self, tmp_path):
        """No config file → returns None."""
        d = _get_daemon()
        with patch("wednesday_tts.server.daemon.os.path.isfile") as mock_isfile:
            mock_isfile.return_value = False
            result = d._get_voice_command_chirp_path()
        assert result is None

    def test_returns_none_when_key_absent(self, tmp_path):
        """Config exists but no voice_command_chirp key → returns None."""
        cfg = tmp_path / "tts-config.json"
        cfg.write_text(json.dumps({"active_model": "pocket"}), encoding="utf-8")
        d = _get_daemon()
        with patch("wednesday_tts.server.daemon._TTS_CONFIG_PATH", str(cfg)):
            result = d._get_voice_command_chirp_path()
        assert result is None

    def test_returns_none_when_file_missing(self, tmp_path):
        """Config has the key but the referenced sound file does not exist → returns None."""
        sound_path = str(tmp_path / "chirp.mp3")  # does not exist
        cfg = tmp_path / "tts-config.json"
        cfg.write_text(json.dumps({"voice_command_chirp": sound_path}), encoding="utf-8")
        d = _get_daemon()
        with patch("wednesday_tts.server.daemon._TTS_CONFIG_PATH", str(cfg)):
            result = d._get_voice_command_chirp_path()
        assert result is None

    def test_returns_path_when_configured_and_exists(self, tmp_path):
        """Config key points to an existing file → returns that path."""
        sound_file = tmp_path / "chirp.mp3"
        sound_file.write_bytes(b"fake audio")
        cfg = tmp_path / "tts-config.json"
        cfg.write_text(json.dumps({"voice_command_chirp": str(sound_file)}), encoding="utf-8")
        d = _get_daemon()
        with patch("wednesday_tts.server.daemon._TTS_CONFIG_PATH", str(cfg)):
            result = d._get_voice_command_chirp_path()
        assert result == str(sound_file)


# ---------------------------------------------------------------------------
# _play_voice_command_chirp
# ---------------------------------------------------------------------------


class TestPlayVoiceCommandChirp:
    def test_uses_afplay_when_path_configured(self, tmp_path):
        """When a valid chirp path is configured, afplay is invoked."""
        d = _get_daemon()
        fake_path = str(tmp_path / "ds9.mp3")
        with (
            patch.object(d, "_get_voice_command_chirp_path", return_value=fake_path),
            patch("wednesday_tts.server.daemon.subprocess.Popen") as mock_popen,
        ):
            d._play_voice_command_chirp()

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "afplay"
        assert args[1] == fake_path

    def test_synthesised_fallback_when_no_path(self):
        """No configured path → falls back to afplay system sound."""
        d = _get_daemon()
        with (
            patch.object(d, "_get_voice_command_chirp_path", return_value=None),
            patch("wednesday_tts.server.daemon.subprocess.Popen") as mock_popen,
        ):
            d._play_voice_command_chirp()

        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "afplay"
        assert "Tink" in args[1]


# ---------------------------------------------------------------------------
# handle_client — chirp command
# ---------------------------------------------------------------------------


class TestHandleClientChirp:
    """The ``chirp`` command must ack with b'ok' and invoke _play_voice_command_chirp."""

    def _make_conn(self, payload: dict) -> socket.socket:
        """Return a connected pair; the first socket has the payload pre-loaded."""
        server_sock, client_sock = socket.socketpair()
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        client_sock.sendall(raw)
        client_sock.shutdown(socket.SHUT_WR)
        return server_sock, client_sock

    def test_chirp_acks_ok(self):
        d = _get_daemon()
        server_sock, client_sock = self._make_conn({"command": "chirp"})
        mock_backend = MagicMock()
        try:
            with patch.object(d, "_play_voice_command_chirp"):
                d.handle_client(server_sock, mock_backend)
            response = client_sock.recv(64)
            assert response == b"ok"
        finally:
            server_sock.close()
            client_sock.close()

    def test_chirp_calls_play_function(self):
        d = _get_daemon()
        server_sock, client_sock = self._make_conn({"command": "chirp"})
        mock_backend = MagicMock()
        try:
            with patch.object(d, "_play_voice_command_chirp") as mock_play:
                d.handle_client(server_sock, mock_backend)
            mock_play.assert_called_once()
        finally:
            server_sock.close()
            client_sock.close()

    def test_chirp_does_not_invoke_speak_pipeline(self):
        """The chirp command must not call _process_speak."""
        d = _get_daemon()
        server_sock, client_sock = self._make_conn({"command": "chirp"})
        mock_backend = MagicMock()
        try:
            with (
                patch.object(d, "_play_voice_command_chirp"),
                patch.object(d, "_process_speak") as mock_speak,
            ):
                d.handle_client(server_sock, mock_backend)
            mock_speak.assert_not_called()
        finally:
            server_sock.close()
            client_sock.close()
