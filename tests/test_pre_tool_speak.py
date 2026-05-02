from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    path = (
        Path(__file__).resolve().parents[1] / "integrations" / "claude-code" / "pre-tool-speak.py"
    )
    hooks_dir = str(path.parent)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)
    spec = importlib.util.spec_from_file_location("pre_tool_speak", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pretool = _load_module()


class TestTranscriptExtraction:
    def test_extracts_assistant_message_content_list(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Let me check."}]},
            },
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        assert pretool._get_unsent_assistant_texts(str(transcript)) == ["Let me check."]

    def test_extracts_root_level_content_string(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "assistant", "content": "Top level content"},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        assert pretool._get_unsent_assistant_texts(str(transcript)) == ["Top level content"]

    def test_no_user_line_still_uses_assistant_text(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        lines = [
            {"type": "assistant", "message": {"content": "First assistant text"}},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        assert pretool._get_unsent_assistant_texts(str(transcript)) == ["First assistant text"]


class TestPayloadFallback:
    def test_uses_last_assistant_message(self):
        payload = {"last_assistant_message": "Fallback text"}
        assert pretool._get_payload_assistant_text(payload) == "Fallback text"

    def test_uses_nested_data_message_content(self):
        payload = {
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "skip"},
                        {"type": "text", "text": "Nested text"},
                    ]
                }
            }
        }
        assert pretool._get_payload_assistant_text(payload) == "Nested text"

    def test_uses_root_level_content_blocks(self):
        payload = {
            "content": [
                {"type": "text", "text": "One"},
                {"type": "text", "text": "Two"},
            ]
        }
        assert pretool._get_payload_assistant_text(payload) == "One Two"
