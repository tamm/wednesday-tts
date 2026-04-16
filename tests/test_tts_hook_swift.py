"""Integration tests for the Swift tts-hook binary.

Runs the compiled binary with canned payloads and verifies it sends correct
JSON to a mock Unix domain socket. No Python hooks in the loop — these test
the Swift binary directly against hand-computed expected values.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import threading

import pytest

SWIFT_BINARY = os.path.join(
    os.path.dirname(__file__), "..", "integrations", "claude-code", "tts-hook"
)
TEST_CWD = "/Users/tamm/dev/wednesday-tts"
EXPECTED_VOICE_HASH = hashlib.sha256(TEST_CWD.encode()).hexdigest()[:8]


def _accept_one(sock: socket.socket, timeout: float = 5.0) -> str | None:
    """Accept a single connection, return the received data as a string."""
    sock.settimeout(timeout)
    try:
        conn, _ = sock.accept()
    except TimeoutError:
        return None
    try:
        conn.sendall(b"ok\n")
        data = conn.recv(65536).decode("utf-8").strip()
        return data
    finally:
        conn.close()


def _run_hook(
    payload: dict,
    mode: str = "stop",
    env_extra: dict | None = None,
    sock_path: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the Swift binary with a JSON payload on stdin."""
    env = os.environ.copy()
    env.pop("ITERM_SESSION_ID", None)
    env.pop("TTS_MUTE", None)
    if sock_path:
        env["TTS_SOCKET_PATH"] = sock_path
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [SWIFT_BINARY, "--mode", mode],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )


@pytest.fixture
def sock_path():
    path = f"/tmp/tts-test-{os.getpid()}.sock"
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def listener(sock_path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    s.listen(1)
    yield s
    s.close()


class TestStopHookGoldenPath:
    def test_sends_speak_with_correct_fields(self, sock_path, listener):
        payload = {
            "session_id": "test-sess",
            "cwd": TEST_CWD,
            "last_assistant_message": "Hello from test. This is a golden path check.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(payload, sock_path=sock_path)
        t.join(timeout=6)

        assert result.returncode == 0
        assert received[0] is not None
        msg = json.loads(received[0])
        assert msg["command"] == "speak"
        assert msg["text"] == payload["last_assistant_message"]
        assert msg["normalization"] == "markdown"
        assert msg["voice_hash"] == EXPECTED_VOICE_HASH
        assert msg["session_id"] == "test-sess"
        assert "timestamp" in msg

    def test_voice_hash_matches_sha256(self, sock_path, listener):
        payload = {
            "session_id": "hash-test",
            "cwd": TEST_CWD,
            "last_assistant_message": "Voice hash parity verification.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener)

        t = threading.Thread(target=accept)
        t.start()
        _run_hook(payload, sock_path=sock_path)
        t.join(timeout=6)

        msg = json.loads(received[0])
        assert msg["voice_hash"] == EXPECTED_VOICE_HASH


class TestSubagentFilter:
    def test_agent_id_blocks(self, sock_path, listener):
        payload = {
            "agent_id": "abc-123",
            "session_id": "x",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(payload, sock_path=sock_path)
        t.join(timeout=3)

        assert result.returncode == 0
        assert received[0] is None

    def test_agent_type_blocks(self, sock_path, listener):
        payload = {
            "agent_type": "Explore",
            "session_id": "x",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(payload, sock_path=sock_path)
        t.join(timeout=3)

        assert result.returncode == 0
        assert received[0] is None

    def test_team_name_blocks(self, sock_path, listener):
        payload = {
            "team_name": "my-team",
            "session_id": "x",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        _run_hook(payload, sock_path=sock_path)
        t.join(timeout=3)

        assert received[0] is None


class TestTeammateFilter:
    def test_transcript_with_teamName_blocks(self, sock_path, listener, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"teamName": "review-team", "agentName": "reviewer", "type": "user"})
            + "\n"
        )
        payload = {
            "session_id": "teammate-test",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
            "transcript_path": str(transcript),
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        _run_hook(payload, sock_path=sock_path)
        t.join(timeout=3)

        assert received[0] is None


class TestMute:
    def test_tts_mute_env_exits_silently(self, sock_path, listener):
        payload = {
            "session_id": "mute-test",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(payload, sock_path=sock_path, env_extra={"TTS_MUTE": "1"})
        t.join(timeout=3)

        assert result.returncode == 0
        assert received[0] is None

    def test_mute_file_exits_silently(self, sock_path, listener, tmp_path):
        mute_file = tmp_path / "tts-mute"
        mute_file.touch()
        payload = {
            "session_id": "mute-test",
            "cwd": TEST_CWD,
            "last_assistant_message": "Should not reach daemon.",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(
            payload,
            sock_path=sock_path,
            env_extra={"TTS_MUTE_PATH": str(mute_file)},
        )
        t.join(timeout=3)

        assert result.returncode == 0
        assert received[0] is None


class TestDaemonDown:
    def test_missing_socket_exits_zero(self):
        payload = {
            "session_id": "dead-daemon",
            "cwd": TEST_CWD,
            "last_assistant_message": "Daemon is down, should exit cleanly.",
        }
        result = _run_hook(
            payload,
            sock_path="/tmp/nonexistent-tts-test-sock-" + str(os.getpid()),
        )
        assert result.returncode == 0


class TestPreToolMode:
    def test_pretool_sends_combined_texts(self, sock_path, listener, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"content": "What's up?"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check that file."},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Found the issue."},
                    ]
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        payload = {
            "session_id": "pretool-test",
            "cwd": TEST_CWD,
            "transcript_path": str(transcript),
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener)

        t = threading.Thread(target=accept)
        t.start()
        _run_hook(payload, mode="pretool", sock_path=sock_path)
        t.join(timeout=6)

        assert received[0] is not None
        msg = json.loads(received[0])
        assert msg["command"] == "speak"
        assert "Let me check that file." in msg["text"]
        assert "Found the issue." in msg["text"]
        assert msg["voice_hash"] == EXPECTED_VOICE_HASH


class TestShortTextSkipped:
    def test_too_short_text_not_sent(self, sock_path, listener):
        payload = {
            "session_id": "short",
            "cwd": TEST_CWD,
            "last_assistant_message": "Hi",
        }
        received = [None]

        def accept():
            received[0] = _accept_one(listener, timeout=2)

        t = threading.Thread(target=accept)
        t.start()
        result = _run_hook(payload, sock_path=sock_path)
        t.join(timeout=3)

        assert result.returncode == 0
        assert received[0] is None
