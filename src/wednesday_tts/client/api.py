"""Wednesday TTS — client API.

Thin HTTP client for the Wednesday TTS service (localhost:5678).

All functions handle connection errors gracefully: they return False or an
empty string rather than raising, so callers don't need try/except.
"""
from __future__ import annotations

import urllib.error
import urllib.request


def speak(
    text: str,
    content_type: str = "markdown",
    server: str = "http://localhost:5678",
    voice: str | None = None,
) -> bool:
    """Send text to the TTS service for synthesis.

    Args:
        text:         Text to speak. Interpreted according to content_type.
        content_type: "markdown" (default) | "plain" | "normalized"
        server:       Base URL of the TTS service.
        voice:        Optional voice override (e.g. "sam") for this request only.

    Returns:
        True if the request was accepted, False on any error.
    """
    if not text:
        return False

    # Wrap text with voice tags if a per-request override is requested
    if voice:
        text = voice_tag(text, voice)

    url = f"{server}/speak?content_type={content_type}"
    data = text.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError, ConnectionError):
        return False


def normalize(
    text: str,
    content_type: str = "markdown",
    server: str = "http://localhost:5678",
) -> str:
    """Normalize text via the TTS service without synthesizing audio.

    Args:
        text:         Raw text to normalize.
        content_type: "markdown" (default) | "plain" | "normalized"
        server:       Base URL of the TTS service.

    Returns:
        Normalized text string, or empty string on any error.
    """
    if not text:
        return ""

    url = f"{server}/normalize?content_type={content_type}"
    data = text.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ConnectionError):
        return ""


def voice_tag(text: str, voice: str = "sam") -> str:
    """Wrap text with voice override tags for the daemon.

    Example::

        tagged = voice_tag("Exterminate", "sam")
        # "««Exterminate»»"
        tagged = voice_tag("Hello", "neural")
        # "««neural»Hello»»"
    """
    if not voice or voice == "sam":
        return f"\u00ab\u00ab{text}\u00bb\u00bb"
    return f"\u00ab\u00ab{voice}\u00bb{text}\u00bb\u00bb"


def is_server_running(server: str = "http://localhost:5678") -> bool:
    """Check whether the TTS service is reachable.

    Args:
        server: Base URL of the TTS service.

    Returns:
        True if the service responds to GET /health, False otherwise.
    """
    url = f"{server}/health"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, OSError, ConnectionError):
        return False
