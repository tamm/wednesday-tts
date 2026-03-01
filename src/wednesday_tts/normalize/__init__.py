"""TTS text normalization pipeline.

Usage:
    from wednesday_tts.normalize import normalize

    text = normalize("Check `my_var` — it returned a 404.", content_type="markdown")
"""

from wednesday_tts.normalize.pipeline import normalize

__all__ = ["normalize"]
