"""Tests for file path and extension normalization."""

from wednesday_tts.normalize.paths import (
    normalize_file_extensions, normalize_tilde_paths, normalize_slash_paths,
)


def test_file_extension_py(sample_filenames_dict):
    result = normalize_file_extensions("speak-response.py", sample_filenames_dict)
    assert "dot pie" in result


def test_file_extension_md(sample_filenames_dict):
    result = normalize_file_extensions("claude.md", sample_filenames_dict)
    assert "dot em-dee" in result


def test_file_extension_json(sample_filenames_dict):
    result = normalize_file_extensions("config.json", sample_filenames_dict)
    assert "dot jason" in result


def test_bare_extension(sample_filenames_dict):
    result = normalize_file_extensions(".sh files", sample_filenames_dict)
    assert "dot shuh" in result


def test_tilde_path():
    result = normalize_tilde_paths("~/.claude/hooks/")
    assert "tilde" in result
    assert "dot claude" in result
    assert "hooks" in result


def test_tilde_path_dotfile():
    result = normalize_tilde_paths("~/.bashrc")
    assert "tilde" in result
    assert "dot bashrc" in result


def test_slash_path():
    result = normalize_slash_paths("src/components/App")
    assert "src slash components slash App" in result


def test_slash_path_trailing():
    result = normalize_slash_paths("hooks/")
    assert "hooks" in result
