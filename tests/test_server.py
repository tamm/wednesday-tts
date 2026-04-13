"""Tests for wednesday_tts.server.app — Flask endpoints and text processing.

Uses Flask's test client so no real HTTP server or audio playback needed.
Heavy dependencies (sounddevice, TTS backends) are mocked.
"""

from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock, patch

import pytest

from wednesday_tts.server import app as server_module  # noqa: E402
from wednesday_tts.server.app import app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Flask test client with clean server state per test."""
    app.config["TESTING"] = True

    # Reset global state
    server_module.is_speaking = False
    server_module.stop_playback = False
    server_module.current_session_id = None
    server_module.last_session_chime_time = 0.0
    server_module.config = {"active_model": "pocket"}

    # Replace the speech queue entirely so join counter is clean
    server_module.speech_queue = queue.Queue()

    with app.test_client() as c:
        yield c


@pytest.fixture
def queued_text(client):
    """Helper: returns a callable that fetches the last item put on the speech queue."""

    def _get() -> str:
        try:
            return server_module.speech_queue.get_nowait()
        except queue.Empty:
            return ""

    return _get


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.data == b"ok"


# ---------------------------------------------------------------------------
# /speak — timestamp stripping
# ---------------------------------------------------------------------------


class TestSpeakTimestampStripping:
    """The hook prepends __t:<wall_clock>__ — server must strip it."""

    def test_strips_timestamp_prefix(self, client, queued_text):
        client.post("/speak", data="__t:1741234567.89__hello world")
        assert queued_text() == "hello world"

    def test_strips_timestamp_with_markdown_content_type(self, client, queued_text):
        client.post(
            "/speak?content_type=markdown",
            data="__t:1741234567.89__hello world",
        )
        assert queued_text() == "__ct:markdown__hello world"

    def test_strips_timestamp_with_plain_content_type(self, client, queued_text):
        client.post(
            "/speak?content_type=plain",
            data="__t:1741234567.89__some text",
        )
        assert queued_text() == "__ct:plain__some text"

    def test_no_timestamp_prefix_still_works(self, client, queued_text):
        client.post("/speak", data="just plain text")
        assert queued_text() == "just plain text"

    def test_no_timestamp_with_markdown_ct(self, client, queued_text):
        client.post("/speak?content_type=markdown", data="no stamp here")
        assert queued_text() == "__ct:markdown__no stamp here"

    def test_integer_timestamp(self, client, queued_text):
        client.post("/speak", data="__t:1741234567__hello")
        assert queued_text() == "hello"

    def test_high_precision_timestamp(self, client, queued_text):
        client.post("/speak", data="__t:1741234567.123456789__hello")
        assert queued_text() == "hello"

    def test_timestamp_not_stripped_mid_text(self, client, queued_text):
        """__t: only stripped at start of text, not in the middle."""
        client.post("/speak", data="before __t:123__ after")
        assert queued_text() == "before __t:123__ after"


# ---------------------------------------------------------------------------
# /speak — regression: old bug where __ct: and __t: merged into ____
# ---------------------------------------------------------------------------


class TestSpeakRegressionTimestampLeak:
    """Regression: the old code re-injected __t: right after __ct:, producing
    __ct:markdown____t:1234.5__text. The \\w+ in process_speech's __ct: regex
    ate into the __t: prefix because _ is a word character. This left
    't:1234.5__text' to be spoken aloud."""

    def test_no_timestamp_reinject_with_markdown(self, client, queued_text):
        """After fix: queued text must be __ct:markdown__<text> with no __t: anywhere."""
        client.post(
            "/speak?content_type=markdown",
            data="__t:1741234567.89__g'day mate",
        )
        result = queued_text()
        assert result == "__ct:markdown__g'day mate"
        assert "__t:" not in result

    def test_no_timestamp_reinject_with_normalized(self, client, queued_text):
        """For normalized content, queued text must be bare — no __ct: or __t:."""
        client.post(
            "/speak?content_type=normalized",
            data="__t:1741234567.89__already normalized",
        )
        result = queued_text()
        assert result == "already normalized"
        assert "__t:" not in result
        assert "__ct:" not in result

    def test_no_double_underscore_run_before_t(self, client, queued_text):
        """Ensure the queued string never has ____t: (the old broken pattern)."""
        client.post(
            "/speak?content_type=markdown",
            data="__t:9999999999.99__test text",
        )
        result = queued_text()
        assert "____t:" not in result


# ---------------------------------------------------------------------------
# /speak — content_type handling
# ---------------------------------------------------------------------------


class TestSpeakContentType:
    def test_default_content_type_is_normalized(self, client, queued_text):
        """No content_type param → normalized → no __ct: prefix injected."""
        client.post("/speak", data="pre-normalized text")
        assert queued_text() == "pre-normalized text"

    def test_markdown_injects_ct_prefix(self, client, queued_text):
        client.post("/speak?content_type=markdown", data="# Header")
        assert queued_text() == "__ct:markdown__# Header"

    def test_plain_injects_ct_prefix(self, client, queued_text):
        client.post("/speak?content_type=plain", data="plain text")
        assert queued_text() == "__ct:plain__plain text"

    def test_normalized_no_ct_prefix(self, client, queued_text):
        client.post("/speak?content_type=normalized", data="already done")
        assert queued_text() == "already done"


# ---------------------------------------------------------------------------
# /speak — basic behaviour
# ---------------------------------------------------------------------------


class TestSpeakBasic:
    def test_returns_ok(self, client):
        resp = client.post("/speak", data="hello")
        assert resp.status_code == 200
        assert b"ok" in resp.data

    def test_empty_body_returns_400(self, client):
        resp = client.post("/speak", data="")
        assert resp.status_code == 400

    def test_whitespace_only_returns_400(self, client):
        resp = client.post("/speak", data="   \n  ")
        assert resp.status_code == 400

    def test_increments_requests_total(self, client):
        before = server_module._stats["requests_total"]
        client.post("/speak", data="hello")
        assert server_module._stats["requests_total"] == before + 1

    def test_tracks_session_id(self, client):
        client.post(
            "/speak",
            data="hello",
            headers={"X-Session-Id": "abc123"},
        )
        assert server_module.current_session_id == "abc123"

    def test_queued_position_when_multiple(self, client):
        """Second request while first is 'playing' shows queue position."""
        # Simulate the first request being in the queue (not consumed)
        server_module.speech_queue.put("first item")
        resp = client.post("/speak", data="second item")
        assert b"queued" in resp.data


# ---------------------------------------------------------------------------
# /speak — session chime
# ---------------------------------------------------------------------------


class TestSpeakSessionChime:
    def test_no_chime_for_same_session(self, client):
        server_module.current_session_id = "sess-A"
        server_module.is_speaking = True

        with patch.object(server_module, "play_chime") as mock_chime:
            client.post(
                "/speak",
                data="hello",
                headers={"X-Session-Id": "sess-A"},
            )
        mock_chime.assert_not_called()

    def test_chime_for_different_session_when_speaking(self, client):
        server_module.current_session_id = "sess-A"
        server_module.is_speaking = True
        server_module.last_session_chime_time = 0.0

        with patch.object(server_module, "play_chime") as mock_chime:
            client.post(
                "/speak",
                data="hello",
                headers={"X-Session-Id": "sess-B"},
            )
        mock_chime.assert_called_once()

    def test_chime_cooldown_prevents_rapid_chimes(self, client):
        server_module.current_session_id = "sess-A"
        server_module.is_speaking = True
        server_module.last_session_chime_time = time.time()  # just chimed

        with patch.object(server_module, "play_chime") as mock_chime:
            client.post(
                "/speak",
                data="hello",
                headers={"X-Session-Id": "sess-B"},
            )
        mock_chime.assert_not_called()


# ---------------------------------------------------------------------------
# /stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_returns_stopped(self, client):
        resp = client.post("/stop")
        assert resp.status_code == 200
        assert b"stopped" in resp.data

    def test_sets_stop_playback_flag(self, client):
        with patch("sounddevice.stop"):
            client.post("/stop")
        assert server_module.stop_playback is True

    def test_clears_queued_items(self, client):
        server_module.speech_queue.put("item1")
        server_module.speech_queue.put("item2")

        with patch("sounddevice.stop"):
            resp = client.post("/stop")

        assert server_module.speech_queue.empty()
        assert b"cleared 2" in resp.data

    def test_aborts_pocket_streaming(self, client):
        mock_backend = MagicMock()
        mock_backend.abort_stream = MagicMock()
        server_module.current_model = mock_backend

        with patch("sounddevice.stop"):
            client.post("/stop")

        mock_backend.abort_stream.assert_called_once()
        server_module.current_model = None  # cleanup


# ---------------------------------------------------------------------------
# /normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_returns_normalized_text(self, client):
        with patch.object(server_module, "run_normalize", return_value="normalized output"):
            resp = client.post("/normalize", data="raw input")
        assert resp.status_code == 200
        assert resp.data == b"normalized output"

    def test_empty_body_returns_400(self, client):
        resp = client.post("/normalize", data="")
        assert resp.status_code == 400

    def test_default_content_type_is_markdown(self, client):
        with patch.object(server_module, "run_normalize", return_value="out") as mock_norm:
            client.post("/normalize", data="input")
        mock_norm.assert_called_once_with("input", content_type="markdown")

    def test_plain_content_type(self, client):
        with patch.object(server_module, "run_normalize", return_value="out") as mock_norm:
            client.post("/normalize?content_type=plain", data="input")
        mock_norm.assert_called_once_with("input", content_type="plain")

    def test_invalid_content_type_returns_400(self, client):
        resp = client.post("/normalize?content_type=bogus", data="input")
        assert resp.status_code == 400

    def test_normalization_error_returns_500(self, client):
        with patch.object(server_module, "run_normalize", side_effect=ValueError("boom")):
            resp = client.post("/normalize", data="input")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_returns_json_by_default(self, client):
        server_module._stats["service_start_time"] = time.time() - 60
        resp = client.get("/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "uptime_s" in data
        assert "requests" in data
        assert data["backend"] == "pocket"

    def test_text_format(self, client):
        server_module._stats["service_start_time"] = time.time() - 60
        resp = client.get("/stats?fmt=text")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        assert b"requests=" in resp.data

    def test_reports_speaking_state(self, client):
        server_module._stats["service_start_time"] = time.time()
        server_module.is_speaking = True
        data = client.get("/stats").get_json()
        assert data["is_speaking"] is True

        server_module.is_speaking = False
        data = client.get("/stats").get_json()
        assert data["is_speaking"] is False


# ---------------------------------------------------------------------------
# /reload
# ---------------------------------------------------------------------------


class TestReload:
    def test_resets_model_and_reloads_config(self, client):
        server_module.current_model = "fake-model"
        server_module.current_model_name = "fake"

        with patch.object(server_module, "load_config"):
            resp = client.post("/reload")

        assert resp.status_code == 200
        assert server_module.current_model is None
        assert server_module.current_model_name is None


# ---------------------------------------------------------------------------
# /drain
# ---------------------------------------------------------------------------


class TestDrain:
    def test_returns_ok(self, client):
        resp = client.post("/drain")
        assert resp.status_code == 200
        assert resp.data == b"ok"


# ---------------------------------------------------------------------------
# process_speech — __ct: prefix parsing
# ---------------------------------------------------------------------------


class TestProcessSpeechTextPrep:
    """Test the text parsing logic in process_speech without running audio.

    We mock get_model, run_normalize, sounddevice, and chunking to isolate
    the prefix-stripping logic.
    """

    def _run(self, text: str) -> dict:
        """Run process_speech and capture what it tried to normalize/synthesize."""
        captured = {"normalize_called": False, "normalize_input": None, "chunks": None}

        def fake_normalize(t, content_type="markdown"):
            captured["normalize_called"] = True
            captured["normalize_input"] = t
            captured["normalize_ct"] = content_type
            return f"normalized({t})"

        mock_backend = MagicMock()
        mock_backend.generate.return_value = None
        mock_backend.supports_streaming = False

        def fake_chunk(t, **_kw):
            captured["chunks"] = [t]
            return [t]

        with (
            patch.object(server_module, "run_normalize", side_effect=fake_normalize),
            patch.object(
                server_module, "get_model", return_value=(mock_backend, "pocket", {"speed": 1.0})
            ),
            patch("wednesday_tts.normalize.chunking.chunk_text_server", side_effect=fake_chunk),
            patch("sounddevice.query_devices", return_value={"name": "test"}),
            patch("sounddevice.play"),
            patch("sounddevice.wait"),
        ):
            server_module.process_speech(text)

        return captured

    def test_no_prefix_treated_as_normalized(self):
        result = self._run("already normalized text")
        assert result["normalize_called"] is False

    def test_ct_markdown_triggers_normalization(self):
        result = self._run("__ct:markdown__# Hello World")
        assert result["normalize_called"] is True
        assert result["normalize_input"] == "# Hello World"
        assert result["normalize_ct"] == "markdown"

    def test_ct_plain_triggers_normalization(self):
        result = self._run("__ct:plain__hello there")
        assert result["normalize_called"] is True
        assert result["normalize_input"] == "hello there"
        assert result["normalize_ct"] == "plain"

    def test_ct_normalized_skips_normalization(self):
        result = self._run("__ct:normalized__already done")
        assert result["normalize_called"] is False

    def test_no_stale_timestamp_in_text(self):
        """Ensure no __t: prefix leaks through to the text that gets chunked."""
        result = self._run("__ct:markdown__hello world")
        assert "__t:" not in (result["chunks"][0] if result["chunks"] else "")

    def test_bare_text_goes_straight_to_chunking(self):
        result = self._run("simple text")
        assert result["chunks"] == ["simple text"]


# ---------------------------------------------------------------------------
# End-to-end: hook payload → /speak → queue → process_speech text prep
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Simulate the full path from hook payload to what process_speech receives."""

    def test_hook_sends_timestamped_markdown(self, client, queued_text):
        """The thin hook sends: __t:<wall>__<markdown>, content_type=markdown.
        Server must queue: __ct:markdown__<markdown> (no timestamp)."""
        client.post(
            "/speak?content_type=markdown",
            data="__t:1741234567.89__Here is some **bold** text.",
        )
        queued = queued_text()
        assert queued == "__ct:markdown__Here is some **bold** text."
        assert "__t:" not in queued

    def test_old_hook_sends_prenormalized(self, client, queued_text):
        """Old hooks send pre-normalized text with no content_type param.
        Server must queue the text as-is (no __ct: prefix)."""
        client.post("/speak", data="already normalized text")
        assert queued_text() == "already normalized text"

    def test_old_hook_with_timestamp(self, client, queued_text):
        """Old hooks that also stamp __t: — timestamp stripped, text passed through."""
        client.post("/speak", data="__t:1741234567.89__pre-normalized text")
        assert queued_text() == "pre-normalized text"
