"""Tests for version string normalization."""

from wednesday_tts.normalize.versions import normalize_model_versions, normalize_semver


def test_model_version_with_colon():
    result = normalize_model_versions("qwen2.5:0.5b")
    assert "qwen 2 point 5" in result
    assert "0 point 5 b" in result


def test_model_version_simple():
    result = normalize_model_versions("llama3.1:8b")
    assert "llama 3 point 1" in result
    assert "8 b" in result


def test_name_version_no_colon():
    result = normalize_model_versions("qwen2.5")
    assert "qwen 2 point 5" in result


def test_semver_v_prefix():
    result = normalize_semver("v1.2.3")
    assert "v1 dot 2 dot 3" in result


def test_semver_triple():
    result = normalize_semver("1.2.3")
    assert "1 dot 2 dot 3" in result


def test_semver_double_with_v():
    result = normalize_semver("v3.10")
    assert "v3 dot 10" in result
