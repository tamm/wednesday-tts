"""URL and domain normalization for TTS."""

import re


def normalize_urls(text):
    """Convert URLs and domain/path patterns to spoken form.

    Must run early — consume whole URLs before any other rule mangles their internals.
    "https://ta.mw/unwatch" -> "ta dot m w slash unwatch"
    """

    def url_to_speech(m):
        url = m.group(1).rstrip('.,;:!?)>')
        slash_idx = url.find('/')
        if slash_idx != -1:
            domain = url[:slash_idx]
            path = url[slash_idx + 1:]
        else:
            domain, path = url, ''
        spoken = domain.replace('.', ' dot ')
        if path:
            spoken += ' slash ' + path.replace('/', ' slash ').replace('.', ' dot ')
        return spoken

    text = re.sub(r'https?://([^\s\)\]>"]+)', url_to_speech, text)

    # Bare domain/path patterns: "ta.mw/unwatch" -> "ta dot m w slash unwatch"
    def rel_url_to_speech(m):
        domain = m.group(1).replace('.', ' dot ')
        path = m.group(2).lstrip('/').replace('/', ' slash ').replace('.', ' dot ')
        return f'{domain} slash {path}'

    text = re.sub(
        r'\b([a-zA-Z][a-zA-Z0-9-]*\.[a-zA-Z]{2,6})(/[^\s\)\]>,;"]+)',
        rel_url_to_speech, text
    )

    return text
