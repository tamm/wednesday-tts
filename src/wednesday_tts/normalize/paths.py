"""File path and extension normalization for TTS."""

import re


def normalize_file_extensions(text, filenames_dict):
    """Convert file extensions to spoken form using the filenames dictionary.

    "claude.md" -> "claude dot markdown"
    Must run BEFORE general dictionary so extension pronunciation takes priority.
    """
    def _ext_to_speech(m):
        ext_spoken = filenames_dict.get(m.group(2).lower(), m.group(2))
        return m.group(1) + ' dot ' + ext_spoken

    _KNOWN_EXTS = set(filenames_dict.keys()) | {
        'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'jsonl', 'yaml', 'yml',
        'md', 'txt', 'sh', 'toml', 'csv', 'pdf', 'html', 'css', 'svg',
        'png', 'jpg', 'mp3', 'mp4', 'wav', 'db', 'sqlite', 'sql', 'log',
        'lock', 'env', 'plist', 'xml',
    }
    _EXT_PAT = '|'.join(re.escape(e) for e in sorted(_KNOWN_EXTS, key=len, reverse=True))
    text = re.sub(r'\b([a-zA-Z0-9_-]+)\.(' + _EXT_PAT + r')\b', _ext_to_speech, text)

    # Bare dotfile extensions: ".sh", ".py", ".env"
    def _bare_ext_to_speech(m):
        ext = m.group(1).lower()
        return 'dot ' + filenames_dict.get(ext, ext)

    text = re.sub(
        r'(?<!\w)\.(' + _EXT_PAT + r')\b',
        _bare_ext_to_speech, text
    )

    return text


def normalize_tilde_paths(text):
    """Convert tilde paths to spoken form.

    ~/.claude/hooks/ -> "tilde slash dot claude slash hooks"
    """
    def tilde_path_to_speech(m):
        path = m.group(0).rstrip('/')
        parts = path.split('/')
        spoken = []
        for p in parts:
            if p == '~':
                spoken.append('tilde')
            elif p.startswith('.'):
                spoken.append('dot ' + p[1:])
            else:
                spoken.append(p)
        return ' slash '.join(spoken)

    text = re.sub(r'~/[^\s,;:!?\)]+', tilde_path_to_speech, text)
    return text


def normalize_slash_paths(text):
    """Convert remaining slash-separated content to spoken form.

    paths, alternatives like error/status -> "error slash status"
    """
    def slashes_to_speech(m):
        path = m.group(0).rstrip('/')
        return path.replace('/', ' slash ')

    text = re.sub(r'\b\w[\w.-]*(?:/[\w.-]+)+/?', slashes_to_speech, text)
    return text
