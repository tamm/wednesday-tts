"""Backtick code identifier expansion and hash detection."""

import re

from wednesday_tts.normalize.constants import (
    LETTER_NAMES, LOWERCASE_ACRONYMS, spell_chars,
)


def expand_identifier_part(part, is_last=False):
    """Expand a single snake_case token part to spoken form."""
    if not part:
        return None
    low = part.lower()
    if low in LOWERCASE_ACRONYMS:
        return LOWERCASE_ACRONYMS[low]
    if len(part) == 1 and part.isalpha():
        return LETTER_NAMES.get(part.lower(), part)
    return part


def pattern_descriptor_to_speech(content):
    """Detect repeated-char placeholder patterns like 'xxxx xxxx xxxx xxxx'
    or '****-****' and describe their structure instead of reading each char.
    Returns spoken string, or None if content doesn't match."""
    stripped = content.strip()
    sep_match = re.match(r'^([a-zA-Z*#+0-9]+)([ -])([a-zA-Z*#+0-9 -]+)$', stripped)
    if not sep_match and re.match(r'^[a-zA-Z*#+0-9]+$', stripped):
        groups = [stripped]
        sep_word = None
    elif sep_match:
        sep_char = sep_match.group(2)
        sep_word = 'dashes' if sep_char == '-' else None
        groups = re.split(r'[ -]+', stripped)
        groups = [g for g in groups if g]
    else:
        return None

    def is_repeated_char(g):
        return len(g) > 0 and len(set(g)) == 1

    if not all(is_repeated_char(g) for g in groups):
        return None

    chars_used = set(g[0] for g in groups)
    if len(chars_used) != 1:
        return None

    group_sizes = [len(g) for g in groups]
    total = sum(group_sizes)
    unique_sizes = set(group_sizes)

    if len(groups) == 1:
        return f'{total} characters'

    if len(unique_sizes) == 1:
        size = group_sizes[0]
        count = len(groups)
        spoken = f'{count} blocks of {size}'
        if sep_word:
            spoken += f', separated by {sep_word}'
    else:
        spoken = f'{total} characters'
        if sep_word:
            spoken += f', separated by {sep_word}'
    return spoken


def identifier_to_speech(m):
    """Convert backtick-wrapped content to spoken form."""
    content = m.group(1)

    # Prefixed hashes (sha256:abc123...) -> "sha256 hash ending in X Y Z"
    prefix_match = re.match(r'^(sha\d+|md5|blake2[bs]?):([0-9a-fA-F]{7,})$', content)
    if prefix_match:
        tail = spell_chars(prefix_match.group(2)[-3:])
        return f'{prefix_match.group(1)} hash ending in {tail}'

    # Hex hashes (git SHAs, content hashes): 7+ hex chars -> "hash ending in X Y Z"
    if (re.match(r'^[0-9a-fA-F]{7,}$', content)
            and re.search(r'[0-9]', content)
            and re.search(r'[a-fA-F]', content)):
        tail = spell_chars(content[-3:])
        return f'hash ending in {tail}'

    # Repeated-char placeholder patterns
    spoken = pattern_descriptor_to_speech(content)
    if spoken:
        return spoken

    # Snake_case identifiers
    if '_' in content:
        parts = [p for p in content.split('_') if p]
        spoken_parts = [
            expand_identifier_part(p, is_last=(i == len(parts) - 1))
            for i, p in enumerate(parts)
        ]
        return ' '.join(s for s in spoken_parts if s)

    return content


def normalize_identifiers(text):
    """Process inline backtick content as code identifiers, then strip ticks.

    Must run first so we can see underscore-separated names before cleanup.
    """
    text = re.sub(r'`([^`\n]+)`', identifier_to_speech, text)
    return text


def normalize_escape_sequences(text):
    """Convert literal escape sequences to spoken form.

    Must run before backslash cleanup eats them.
    """
    text = re.sub(r'\\n\\n', ' double new line ', text)
    text = re.sub(r'\\n', ' new line ', text)
    text = re.sub(r'\\t', ' tab ', text)
    return text


def normalize_hashes(text):
    """Convert hash-number references and standalone hashes.

    PR #579 -> "number 579", C# -> "see sharp", remaining # -> "hash".
    """
    text = re.sub(r'#(\d+)', r'number \1', text)
    text = re.sub(r'\bC#', 'see sharp', text)
    text = re.sub(r'\bF#', 'eff sharp', text)

    def _hash_to_speech(m):
        start = m.start()
        end = m.end()
        after = text[end] if end < len(text) else '\n'
        is_heading = after in (' ', '\n')
        if start == 0 and is_heading:
            return m.group(0)
        if start >= 2 and text[start - 2:start] == '\n\n' and is_heading:
            return m.group(0)
        return ' hash '

    text = re.sub(r'#{1,}', _hash_to_speech, text)
    return text
