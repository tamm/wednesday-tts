"""Tests for wednesday_tts.platform — mocked I/O and subprocess."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import wednesday_tts.platform as plat

# ---------------------------------------------------------------------------
# spoken_hashes_path()
# ---------------------------------------------------------------------------


class TestSpokenHashesPath:
    def test_returns_string(self):
        result = plat.spoken_hashes_path("abc123")
        assert isinstance(result, str)

    def test_contains_session_id(self):
        result = plat.spoken_hashes_path("mysession")
        assert "mysession" in result

    def test_contains_tts_spoken_prefix(self):
        result = plat.spoken_hashes_path("xyz")
        assert "tts-spoken" in result

    def test_different_sessions_give_different_paths(self):
        a = plat.spoken_hashes_path("session-a")
        b = plat.spoken_hashes_path("session-b")
        assert a != b


# ---------------------------------------------------------------------------
# suppress_dictation() / unsuppress_dictation()
# ---------------------------------------------------------------------------


class TestDictationSuppression:
    def test_suppress_creates_file(self, tmp_path):
        suppress_path = str(tmp_path / "dictation-suppress")
        with patch.object(plat, "SUPPRESS_PATH", suppress_path):
            plat.suppress_dictation()
        assert os.path.exists(suppress_path)

    def test_suppress_does_not_raise_on_permission_error(self, tmp_path):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            plat.suppress_dictation()  # must not raise

    def test_unsuppress_removes_file(self, tmp_path):
        suppress_path = str(tmp_path / "dictation-suppress")
        open(suppress_path, "w").close()
        with patch.object(plat, "SUPPRESS_PATH", suppress_path):
            plat.unsuppress_dictation()
        assert not os.path.exists(suppress_path)

    def test_unsuppress_silent_when_file_missing(self, tmp_path):
        suppress_path = str(tmp_path / "no-such-file")
        with patch.object(plat, "SUPPRESS_PATH", suppress_path):
            plat.unsuppress_dictation()  # must not raise


# ---------------------------------------------------------------------------
# record_failure() / clear_failures()
# ---------------------------------------------------------------------------


class TestFailureLog:
    def test_record_failure_appends_timestamp(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        before = time.time()
        with patch.object(plat, "FAILURE_PATH", failure_path):
            plat.record_failure()
        with open(failure_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        ts = float(lines[0].strip())
        assert before <= ts <= time.time() + 1

    def test_record_failure_multiple_appends(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            plat.record_failure()
            plat.record_failure()
            plat.record_failure()
        with open(failure_path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_record_failure_silent_on_io_error(self):
        with patch("builtins.open", side_effect=OSError("disk full")):
            plat.record_failure()  # must not raise

    def test_clear_failures_removes_file(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        open(failure_path, "w").close()
        with patch.object(plat, "FAILURE_PATH", failure_path):
            plat.clear_failures()
        assert not os.path.exists(failure_path)

    def test_clear_failures_silent_when_file_missing(self, tmp_path):
        failure_path = str(tmp_path / "no-such-file")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            plat.clear_failures()  # must not raise


# ---------------------------------------------------------------------------
# should_restart_daemon()
# ---------------------------------------------------------------------------


class TestShouldRestartDaemon:
    def test_returns_false_when_no_failure_file(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            assert plat.should_restart_daemon() is False

    def test_mac_returns_false_when_file_empty(self, tmp_path):
        # macOS: empty file means no timestamps recorded — should not restart.
        failure_path = str(tmp_path / "tts-daemon-failures")
        open(failure_path, "w").close()
        with patch.object(plat, "FAILURE_PATH", failure_path):
            with patch.object(plat, "IS_WINDOWS", False):
                assert plat.should_restart_daemon() is False

    def test_windows_returns_true_when_failure_file_exists(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        with open(failure_path, "w") as f:
            f.write(f"{time.time()}\n")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            with patch.object(plat, "IS_WINDOWS", True):
                result = plat.should_restart_daemon()
        assert result is True

    def test_mac_returns_false_when_failure_is_recent(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        with open(failure_path, "w") as f:
            f.write(f"{time.time()}\n")  # failure just now
        with patch.object(plat, "FAILURE_PATH", failure_path):
            with patch.object(plat, "IS_WINDOWS", False):
                result = plat.should_restart_daemon()
        assert result is False

    def test_mac_returns_true_when_failure_is_old(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        old_ts = time.time() - 400  # older than the 300s threshold
        with open(failure_path, "w") as f:
            f.write(f"{old_ts}\n")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            with patch.object(plat, "IS_WINDOWS", False):
                result = plat.should_restart_daemon()
        assert result is True

    def test_returns_false_on_corrupt_file(self, tmp_path):
        failure_path = str(tmp_path / "tts-daemon-failures")
        with open(failure_path, "w") as f:
            f.write("not-a-number\n")
        with patch.object(plat, "FAILURE_PATH", failure_path):
            with patch.object(plat, "IS_WINDOWS", False):
                result = plat.should_restart_daemon()
        assert result is False


# ---------------------------------------------------------------------------
# daemon_is_responsive() — Windows branch
# ---------------------------------------------------------------------------


class TestDaemonIsResponsiveWindows:
    def _run(self, urlopen_side_effect=None, urlopen_return=None):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"ok"

        if urlopen_side_effect is not None:
            side_effect = urlopen_side_effect
        else:
            side_effect = None

        with patch.object(plat, "IS_WINDOWS", True):
            with patch.object(plat, "SERVICE_URL", "http://localhost:5678"):
                with patch(
                    "urllib.request.urlopen",
                    return_value=urlopen_return or mock_resp,
                    side_effect=side_effect,
                ):
                    return plat.daemon_is_responsive(timeout=1.0)

    def test_returns_true_when_health_responds(self):
        assert self._run() is True

    def test_returns_false_on_exception(self):
        import urllib.error

        assert self._run(urlopen_side_effect=urllib.error.URLError("refused")) is False

    def test_returns_false_on_os_error(self):
        assert self._run(urlopen_side_effect=OSError("no route")) is False


# ---------------------------------------------------------------------------
# stop_daemon_audio() — Windows branch (fire and forget, must not raise)
# ---------------------------------------------------------------------------


class TestStopDaemonAudioWindows:
    def test_does_not_raise_on_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"ok"

        with patch.object(plat, "IS_WINDOWS", True):
            with patch.object(plat, "SERVICE_URL", "http://localhost:5678"):
                with patch("urllib.request.urlopen", return_value=mock_resp):
                    plat.stop_daemon_audio()

    def test_does_not_raise_on_error(self):
        import urllib.error

        with patch.object(plat, "IS_WINDOWS", True):
            with patch.object(plat, "SERVICE_URL", "http://localhost:5678"):
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
                    plat.stop_daemon_audio()  # must not raise


# ---------------------------------------------------------------------------
# register_signals()
# ---------------------------------------------------------------------------


class TestRegisterSignals:
    def test_registers_sigterm_and_sigint(self):
        import signal as _signal

        handler = MagicMock()
        registered = {}

        def fake_signal(sig, h):
            registered[sig] = h

        with patch("signal.signal", side_effect=fake_signal):
            plat.register_signals(handler)

        assert _signal.SIGTERM in registered
        assert _signal.SIGINT in registered
        assert registered[_signal.SIGTERM] is handler
        assert registered[_signal.SIGINT] is handler

    def test_no_sighup_on_windows(self):
        import signal as _signal

        registered = {}

        def fake_signal(sig, h):
            registered[sig] = h

        with patch.object(plat, "IS_WINDOWS", True):
            with patch("signal.signal", side_effect=fake_signal):
                plat.register_signals(MagicMock())

        assert getattr(_signal, "SIGHUP", None) not in registered


# ---------------------------------------------------------------------------
# Path constants — sanity checks
# ---------------------------------------------------------------------------


class TestPathConstants:
    def test_lock_path_is_string(self):
        assert isinstance(plat.LOCK_PATH, str)

    def test_mute_path_is_string(self):
        assert isinstance(plat.MUTE_PATH, str)

    def test_suppress_path_is_string(self):
        assert isinstance(plat.SUPPRESS_PATH, str)

    def test_failure_path_is_string(self):
        assert isinstance(plat.FAILURE_PATH, str)
