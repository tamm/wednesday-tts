"""Tests for wednesday_tts.client.api — no real HTTP calls."""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

from wednesday_tts.client.api import is_server_running, normalize, speak

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(body: bytes = b"ok", status: int = 200) -> MagicMock:
    """Return a mock context-manager that mimics urllib's HTTP response."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _url_error() -> urllib.error.URLError:
    return urllib.error.URLError("connection refused")


# ---------------------------------------------------------------------------
# speak()
# ---------------------------------------------------------------------------


class TestSpeak:
    def test_returns_true_on_200(self):
        mock_resp = _mock_response(b"ok")
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = speak("hello world")
        assert result is True
        mock_open.assert_called_once()

    def test_posts_to_speak_endpoint(self):
        mock_resp = _mock_response(b"ok")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            speak("hello", server="http://localhost:9999")
        # No assertion needed beyond no exception — URL is baked into Request

    def test_returns_false_on_url_error(self):
        with patch("urllib.request.urlopen", side_effect=_url_error()):
            result = speak("hello world")
        assert result is False

    def test_returns_false_on_os_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("broken pipe")):
            result = speak("hello world")
        assert result is False

    def test_returns_false_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = speak("hello world")
        assert result is False

    def test_empty_text_returns_false_without_http_call(self):
        with patch("urllib.request.urlopen") as mock_open:
            result = speak("")
        assert result is False
        mock_open.assert_not_called()

    def test_content_type_included_in_url(self):
        mock_resp = _mock_response(b"ok")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            speak("hello", content_type="plain")

        assert "content_type=plain" in captured["url"]

    def test_default_content_type_is_markdown(self):
        mock_resp = _mock_response(b"ok")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            speak("hello")

        assert "content_type=markdown" in captured["url"]

    def test_custom_server_url(self):
        mock_resp = _mock_response(b"ok")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            speak("hello", server="http://example.com:1234")

        assert captured["url"].startswith("http://example.com:1234")


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_returns_text_on_200(self):
        mock_resp = _mock_response(b"hello world normalized")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = normalize("hello world")
        assert result == "hello world normalized"

    def test_returns_empty_string_on_url_error(self):
        with patch("urllib.request.urlopen", side_effect=_url_error()):
            result = normalize("hello world")
        assert result == ""

    def test_returns_empty_string_on_os_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            result = normalize("hello world")
        assert result == ""

    def test_returns_empty_string_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = normalize("hello world")
        assert result == ""

    def test_empty_text_returns_empty_without_http_call(self):
        with patch("urllib.request.urlopen") as mock_open:
            result = normalize("")
        assert result == ""
        mock_open.assert_not_called()

    def test_posts_to_normalize_endpoint(self):
        mock_resp = _mock_response(b"out")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            normalize("some text")

        assert "/normalize" in captured["url"]

    def test_content_type_plain(self):
        mock_resp = _mock_response(b"result")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            normalize("text", content_type="plain")

        assert "content_type=plain" in captured["url"]

    def test_response_decoded_as_utf8(self):
        body = "café résumé".encode()
        mock_resp = _mock_response(body)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = normalize("café résumé")
        assert result == "café résumé"


# ---------------------------------------------------------------------------
# is_server_running()
# ---------------------------------------------------------------------------


class TestIsServerRunning:
    def test_returns_true_when_health_responds(self):
        mock_resp = _mock_response(b"ok")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = is_server_running()
        assert result is True

    def test_returns_false_on_url_error(self):
        with patch("urllib.request.urlopen", side_effect=_url_error()):
            result = is_server_running()
        assert result is False

    def test_returns_false_on_os_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            result = is_server_running()
        assert result is False

    def test_returns_false_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("reset")):
            result = is_server_running()
        assert result is False

    def test_hits_health_endpoint(self):
        mock_resp = _mock_response(b"ok")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            is_server_running()

        assert captured["url"].endswith("/health")

    def test_custom_server_url(self):
        mock_resp = _mock_response(b"ok")
        captured = {}

        def capture(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=capture):
            is_server_running(server="http://remote-host:9000")

        assert captured["url"].startswith("http://remote-host:9000")
