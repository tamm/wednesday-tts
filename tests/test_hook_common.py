"""Tests for integrations/claude-code/hook_common.py — is_subagent filter."""
from __future__ import annotations

import json
import os
import sys

# hook_common lives outside the installed package; add its directory to sys.path.
_HOOKS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "integrations", "claude-code"
)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

import hook_common  # noqa: E402

# ---------------------------------------------------------------------------
# _transcript_is_teammate
# ---------------------------------------------------------------------------

class TestTranscriptIsTeammate:
    def _write_transcript(self, lines: list[dict], tmp_path) -> str:
        path = str(tmp_path / "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return path

    def test_teammate_transcript_teamName(self, tmp_path):
        path = self._write_transcript(
            [{"teamName": "tts-bargein-review-2", "agentName": "bargein-reviewer",
              "type": "user", "content": "hello"}],
            tmp_path,
        )
        assert hook_common._transcript_is_teammate(path) is True

    def test_teammate_transcript_agentName_only(self, tmp_path):
        path = self._write_transcript(
            [{"agentName": "some-teammate", "type": "assistant"}],
            tmp_path,
        )
        assert hook_common._transcript_is_teammate(path) is True

    def test_lead_transcript_no_team_fields(self, tmp_path):
        path = self._write_transcript(
            [{"type": "user", "content": "hello"},
             {"type": "assistant", "content": "world"}],
            tmp_path,
        )
        assert hook_common._transcript_is_teammate(path) is False

    def test_missing_transcript_returns_false(self):
        assert hook_common._transcript_is_teammate("/nonexistent/path.jsonl") is False

    def test_none_transcript_returns_false(self):
        assert hook_common._transcript_is_teammate(None) is False

    def test_empty_transcript_returns_false(self, tmp_path):
        path = str(tmp_path / "empty.jsonl")
        open(path, "w").close()
        assert hook_common._transcript_is_teammate(path) is False

    def test_malformed_lines_skipped(self, tmp_path):
        path = str(tmp_path / "bad.jsonl")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"type": "user"}) + "\n")
        assert hook_common._transcript_is_teammate(path) is False

    def test_team_field_on_later_line(self, tmp_path):
        path = self._write_transcript(
            [{"type": "user"},
             {"type": "assistant"},
             {"teamName": "my-team", "agentName": "worker"}],
            tmp_path,
        )
        assert hook_common._transcript_is_teammate(path) is True


# ---------------------------------------------------------------------------
# is_subagent — payload-level signals (existing behaviour)
# ---------------------------------------------------------------------------

class TestIsSubagentPayloadFields:
    def test_agent_id_blocks(self):
        assert hook_common.is_subagent({"agent_id": "abc", "session_id": "x"}) is True

    def test_agent_type_blocks(self):
        assert hook_common.is_subagent({"agent_type": "Explore"}) is True

    def test_team_name_blocks(self):
        assert hook_common.is_subagent({"team_name": "my-team"}) is True

    def test_teammate_name_blocks(self):
        assert hook_common.is_subagent({"teammate_name": "worker"}) is True

    def test_empty_payload_is_not_subagent(self):
        assert hook_common.is_subagent({}) is False

    def test_lead_payload_no_agent_fields(self):
        payload = {
            "session_id": "lead-session-id",
            "hook_event_name": "Stop",
            "permission_mode": "acceptEdits",
        }
        assert hook_common.is_subagent(payload) is False


# ---------------------------------------------------------------------------
# is_subagent — transcript-based teammate detection
# ---------------------------------------------------------------------------

class TestIsSubagentTranscript:
    def test_teammate_transcript_blocks(self, tmp_path):
        path = str(tmp_path / "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"teamName": "foo", "agentName": "bar",
                                "type": "user"}) + "\n")
        payload = {"session_id": "some-id", "transcript_path": path}
        assert hook_common.is_subagent(payload) is True

    def test_lead_transcript_does_not_block(self, tmp_path):
        path = str(tmp_path / "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "content": "hi"}) + "\n")
        payload = {"session_id": "lead-id", "transcript_path": path}
        assert hook_common.is_subagent(payload) is False

    def test_missing_transcript_does_not_block(self):
        payload = {"session_id": "lead-id",
                   "transcript_path": "/does/not/exist.jsonl"}
        assert hook_common.is_subagent(payload) is False
