"""Shared constants for TTS normalization."""

DIGIT_WORDS = {
    '0': 'oh', '1': 'one', '2': 'two', '3': 'three', '4': 'four',
    '5': 'five', '6': 'six', '7': 'seven', '8': 'eight', '9': 'nine',
}

LETTER_NAMES = {
    'a': 'ay', 'b': 'bee', 'c': 'see', 'd': 'dee', 'e': 'ee',
    'f': 'ef', 'g': 'gee', 'h': 'aitch', 'i': 'eye', 'j': 'jay',
    'k': 'kay', 'l': 'el', 'm': 'em', 'n': 'en', 'o': 'oh',
    'p': 'pee', 'q': 'cue', 'r': 'ar', 's': 'ess', 't': 'tee',
    'u': 'you', 'v': 'vee', 'w': 'double you', 'x': 'ex', 'y': 'why',
    'z': 'zee',
}

# Known acronyms that appear lowercase in code identifiers
LOWERCASE_ACRONYMS = {
    'aec': 'Ae ee see', 'tts': 'tee tee ess', 'api': 'Ae pee eye',
    'url': 'you ar el', 'uri': 'you ar eye', 'http': 'aitch tee tee pee',
    'sql': 'ess cue el', 'cli': 'see el eye', 'ui': 'you eye', 'ux': 'you ex',
    'llm': 'el el em', 'mcp': 'em see pee', 'cwd': 'see double-you dee',
    'gpu': 'jee pee you', 'cpu': 'see pee you', 'ram': 'ram',
    'ssh': 'ess ess aitch', 'ci': 'see eye', 'cd': 'see dee',
    'ttl': 'tee tee el', 'vad': 'vee Ae dee', 'rms': 'ar em ess',
    'id': 'eye dee', 'ids': 'eye dees',
    'io': 'eye oh', 'os': 'oh ess', 'fs': 'ef ess',
    'rpc': 'ar pee see', 'ipc': 'eye pee see', 'abi': 'Ae bee eye',
    'ms': 'milliseconds',
    'fn': 'function', 'cfg': 'config', 'db': 'dee bee',
    'dir': 'dir', 'src': 'source', 'dst': 'destination',
    'msg': 'message', 'err': 'error', 'num': 'number',
    'str': 'string', 'buf': 'buffer', 'len': 'length',
    'idx': 'index', 'ref': 'ref', 'req': 'request',
    'res': 'response', 'ret': 'return', 'tmp': 'temp',
    'var': 'var', 'ctx': 'context', 'arg': 'arg', 'args': 'args',
    'kwargs': 'keyword args', 'stdout': 'standard out',
    'stdin': 'standard in', 'stderr': 'standard error',
}

UNIT_MAP = {'ms': 'milliseconds', 's': 'seconds', 'min': 'minutes'}

STORAGE_MAP = {
    'TB': 'terabytes', 'GB': 'gigs', 'MB': 'megs', 'KB': 'kilobytes',
    'tb': 'terabytes', 'gb': 'gigs', 'mb': 'megs', 'kb': 'kilobytes',
    'PB': 'petabytes', 'pb': 'petabytes',
}

CAPS_EXCLAMATIONS = {
    'HI': 'Hi', 'OH': 'Oh', 'NO': 'No', 'YES': 'Yes',
    'UM': 'Um', 'AH': 'Ah', 'UH': 'Uh', 'WOW': 'Wow',
    'HEY': 'Hey', 'BYE': 'Bye', 'AWW': 'Aww', 'GOD': 'God',
    'OMG': 'oh my god',
}

MODIFIER_NAMES = {
    'ctrl': 'Control', 'cmd': 'Command', 'alt': 'Alt',
    'option': 'Option', 'shift': 'Shift', 'fn': 'Function',
    'meta': 'Meta', 'super': 'Super',
}


def digits_to_spoken(n):
    """'404' -> 'four oh four'"""
    return ' '.join(DIGIT_WORDS.get(c, c) for c in str(n))


def spell_chars(s):
    """Spell out each character using letter/digit names."""
    parts = []
    for c in s.lower():
        if c in LETTER_NAMES:
            parts.append(LETTER_NAMES[c])
        elif c in DIGIT_WORDS:
            parts.append(DIGIT_WORDS[c])
        else:
            parts.append(c)
    return ' '.join(parts)


def decimal_to_spoken(num_str):
    """Convert a decimal number string to spoken form.

    '0.022' -> 'zero point zero two two'
    '3.14' -> '3 point 14' (only sub-1 decimals get digit-by-digit)
    """
    if '.' not in num_str:
        return num_str
    integer_part, frac_part = num_str.split('.', 1)
    frac_spoken = ' '.join(DIGIT_WORDS.get(c, c) for c in frac_part)
    if integer_part == '0':
        return f'zero point {frac_spoken}'
    return f'{integer_part} point {frac_spoken}'
