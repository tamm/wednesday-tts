from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[1] / "integrations" / "codex" / "codex_notify.py"
    spec = importlib.util.spec_from_file_location("codex_notify", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


notify = _load_module()


def test_extract_message_from_kebab_case_payload() -> None:
    message = notify._extract_message(
        {
            "type": "agent-turn-complete",
            "last-assistant-message": "Speak this.",
        }
    )
    assert message == "Speak this."


def test_extract_message_from_snake_case_payload() -> None:
    message = notify._extract_message(
        {
            "type": "turn-complete",
            "last_assistant_message": "Speak this too.",
        }
    )
    assert message == "Speak this too."


def test_extract_message_ignores_unrelated_event() -> None:
    message = notify._extract_message(
        {
            "type": "tool-call",
            "last-assistant-message": "Do not speak.",
        }
    )
    assert message is None


def test_extract_message_from_session_idle_string_content() -> None:
    message = notify._extract_message(
        {
            "type": "session.idle",
            "data": {
                "message": {
                    "content": "Speak the idle event.",
                }
            },
        }
    )
    assert message == "Speak the idle event."


def test_extract_message_from_session_idle_content_parts() -> None:
    message = notify._extract_message(
        {
            "type": "session.idle",
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "skip this"},
                        {"type": "text", "text": "Speak the text part."},
                    ],
                }
            },
        }
    )
    assert message == "Speak the text part."
