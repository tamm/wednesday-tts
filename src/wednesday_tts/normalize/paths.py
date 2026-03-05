"""File path and extension normalization for TTS."""

import random as _random
import re

# Probability of eliding "dot" in "filename dot ext" -> "filename ext".
# Bare dotfiles (.sh, .py) always keep "dot" — that's how they're said aloud.
_DOT_ELIDE_PROB = 0.4

_DEFAULT_RNG = _random.Random()


def normalize_file_extensions(text, filenames_dict, rng=None):
    """Convert file extensions to spoken form using the filenames dictionary.

    "claude.md" -> "claude dot markdown"  (or occasionally "claude markdown")
    Must run BEFORE general dictionary so extension pronunciation takes priority.

    Args:
        rng: Optional random.Random instance for deterministic testing.
             Defaults to a shared module-level RNG.
    """
    _rng = rng if rng is not None else _DEFAULT_RNG

    _KNOWN_EXTS = set(filenames_dict.keys()) | {
        'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'jsonl', 'yaml', 'yml',
        'md', 'txt', 'sh', 'toml', 'csv', 'pdf', 'html', 'css', 'svg',
        'png', 'jpg', 'mp3', 'mp4', 'wav', 'db', 'sqlite', 'sql', 'log',
        'lock', 'env', 'plist', 'xml',
    }
    _EXT_PAT = '|'.join(re.escape(e) for e in sorted(_KNOWN_EXTS, key=len, reverse=True))

    def _ext_to_speech(m):
        ext_spoken = filenames_dict.get(m.group(2).lower(), m.group(2))
        sep = ' ' if _rng.random() < _DOT_ELIDE_PROB else ' dot '
        return m.group(1) + sep + ext_spoken

    text = re.sub(r'\b([a-zA-Z0-9_-]+)\.(' + _EXT_PAT + r')\b', _ext_to_speech, text)

    # Bare dotfiles: ".sh", ".py", ".env" — always say "dot", never elide.
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

    ~/dev/foo -> "home slash dev slash foo"
    ~/.claude/hooks/ -> "home slash dot claude slash hooks"
    ~ alone -> "tilde"
    """
    def tilde_path_to_speech(m):
        path = m.group(0).rstrip('/')
        parts = path.split('/')
        spoken = []
        for p in parts:
            if p == '~':
                spoken.append('home')
            elif p.startswith('.'):
                spoken.append('dot ' + p[1:])
            else:
                spoken.append(p)
        return ' slash '.join(spoken)

    # ~/path or bare ~/
    text = re.sub(r'~/[^\s,;:!?\)]*', tilde_path_to_speech, text)

    # Bare ~ not followed by /
    text = re.sub(r'(?<!\w)~(?!/)', 'tilde', text)
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
