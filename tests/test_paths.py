"""Tests for file path and extension normalization."""

import random

from wednesday_tts.normalize.paths import (
    normalize_file_extensions,
    normalize_slash_paths,
    normalize_tilde_paths,
)


def _never_elide():
    """RNG that always returns 1.0 — dot is never elided."""
    rng = random.Random()
    rng.random = lambda: 1.0
    return rng


def _always_elide():
    """RNG that always returns 0.0 — dot is always elided."""
    rng = random.Random()
    rng.random = lambda: 0.0
    return rng


def test_file_extension_py_with_dot(sample_filenames_dict):
    result = normalize_file_extensions(
        "speak-response.py", sample_filenames_dict, rng=_never_elide()
    )
    assert "dot pie" in result


def test_file_extension_md_with_dot(sample_filenames_dict):
    result = normalize_file_extensions("claude.md", sample_filenames_dict, rng=_never_elide())
    assert "dot em-dee" in result


def test_file_extension_json_with_dot(sample_filenames_dict):
    result = normalize_file_extensions("config.json", sample_filenames_dict, rng=_never_elide())
    assert "dot jason" in result


def test_file_extension_py_elided(sample_filenames_dict):
    result = normalize_file_extensions(
        "speak-response.py", sample_filenames_dict, rng=_always_elide()
    )
    assert result == "speak-response pie"


def test_file_extension_md_elided(sample_filenames_dict):
    result = normalize_file_extensions("claude.md", sample_filenames_dict, rng=_always_elide())
    assert result == "claude em-dee"


def test_file_extension_elision_both_variants_occur(sample_filenames_dict):
    # Over many runs with the real RNG, both "dot" and no-dot forms should appear.
    results = {normalize_file_extensions("run.py", sample_filenames_dict) for _ in range(40)}
    spoken_forms = {r.split("run")[1].strip() for r in results}
    assert "dot pie" in spoken_forms, "expected dot form at least once in 40 runs"
    assert "pie" in spoken_forms, "expected elided form at least once in 40 runs"


def test_bare_extension_always_has_dot(sample_filenames_dict):
    # Bare dotfiles always keep "dot" — never elided.
    for _ in range(10):
        result = normalize_file_extensions(".sh files", sample_filenames_dict)
        assert result.startswith("dot shuh"), f"bare dotfile dropped dot: {result!r}"


def test_tilde_path():
    result = normalize_tilde_paths("~/.claude/hooks/")
    assert "home" in result
    assert "dot claude" in result
    assert "hooks" in result


def test_tilde_path_dotfile():
    result = normalize_tilde_paths("~/.bashrc")
    assert "home" in result
    assert "dot bashrc" in result


def test_tilde_path_plain():
    result = normalize_tilde_paths("~/dev/foo")
    assert result == "home slash dev slash foo"


def test_bare_tilde():
    result = normalize_tilde_paths("use ~ as shorthand")
    assert "tilde" in result
    assert result == "use tilde as shorthand"


def test_bare_tilde_slash():
    result = normalize_tilde_paths("~/")
    assert result == "home"


def test_slash_path():
    result = normalize_slash_paths("src/components/App")
    assert "src slash components slash App" in result


def test_slash_path_trailing():
    result = normalize_slash_paths("hooks/")
    assert "hooks" in result
