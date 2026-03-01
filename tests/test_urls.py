"""Tests for URL normalization."""

from wednesday_tts.normalize.urls import normalize_urls


def test_https_url():
    result = normalize_urls("https://ta.mw/unwatch")
    assert "ta dot mw slash unwatch" in result


def test_http_url():
    result = normalize_urls("http://example.com/path")
    assert "example dot com slash path" in result


def test_url_with_trailing_punct():
    result = normalize_urls("see https://example.com/page.")
    assert "dot com" in result
    # Trailing period should not be in the spoken URL
    assert result.count("dot com slash page") == 1


def test_bare_domain_path():
    result = normalize_urls("ta.mw/unwatch")
    assert "ta dot mw slash unwatch" in result


def test_url_with_subdomain():
    result = normalize_urls("https://docs.python.org/3/library")
    assert "docs dot python dot org" in result
    assert "slash 3 slash library" in result
