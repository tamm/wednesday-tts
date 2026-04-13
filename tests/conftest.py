"""Shared fixtures for wednesday-tts tests."""

import pytest


@pytest.fixture
def sample_dictionary():
    """A minimal pronunciation dictionary for testing."""
    return [
        {"pattern": "API", "replacement": "Ae pee eye", "case_sensitive": True, "literal": False},
        {"pattern": "APIs", "replacement": "Ae pee eyes", "case_sensitive": True, "literal": False},
        {"pattern": "URL", "replacement": "you ar el", "case_sensitive": True, "literal": False},
        {"pattern": "JSON", "replacement": "jason", "case_sensitive": True, "literal": False},
        {"pattern": "CLI", "replacement": "see el eye", "case_sensitive": True, "literal": False},
        {"pattern": "npm", "replacement": "en pee em", "case_sensitive": True, "literal": False},
        {
            "pattern": "HTTP",
            "replacement": "aitch tee tee pee",
            "case_sensitive": True,
            "literal": False,
        },
        {"pattern": "TTS", "replacement": "teetee ess", "case_sensitive": False, "literal": False},
        {"pattern": "OAuth", "replacement": "o-auth", "case_sensitive": False, "literal": False},
        {"pattern": "macOS", "replacement": "mac O S", "case_sensitive": True, "literal": False},
        {"pattern": "PR", "replacement": "pull request", "case_sensitive": True, "literal": False},
        {
            "pattern": "PRs",
            "replacement": "pull requests",
            "case_sensitive": True,
            "literal": False,
        },
    ]


@pytest.fixture
def sample_filenames_dict():
    """A minimal file extensions dictionary for testing."""
    return {
        "py": "pie",
        "js": "jay ess",
        "ts": "tee ess",
        "md": "em-dee",
        "json": "jason",
        "sh": "shuh",
        "yaml": "yamel",
        "html": "aitch tee em el",
        "css": "see ess ess",
        "txt": "text",
        "env": "ee en vee",
        "log": "log",
        "wav": "wave",
        "toml": "toml",
    }
