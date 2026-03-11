"""Table (markdown pipe / box-drawing) to semantic speech."""

import random
import re

# Unicode box-drawing chars used in fancy tables
_BOX_CHARS = re.compile(
    r'[┌┐└┘├┤┬┴┼─│╭╮╯╰╶╴╵╷┃┄┅┆┇┈┉┊┋╌╍╎╏═║'
    r'╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬]'
)

# Markdown pipe table pattern
MARKDOWN_TABLE_RE = re.compile(
    r'(?:^[ \t]*\|.+\|[ \t]*$\n?){2,}',
    re.MULTILINE
)

# Unicode box-drawing table pattern
UNICODE_TABLE_RE = re.compile(
    r'(?:^[ \t]*[┌┐└┘├┤┬┴┼─│╭╮╯╰═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬].*$\n?){2,}',
    re.MULTILINE
)


def parse_table_rows(lines):
    """Extract cell values from pipe-delimited or box-drawing table lines.
    Returns (headers, data_rows) where each is a list of string lists.
    Returns (None, None) if no table structure detected."""
    rows = []
    for line in lines:
        cleaned = re.sub(r'│', '|', line)
        cleaned = _BOX_CHARS.sub('', cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        if re.match(r'^[-:\s|]+$', cleaned):
            continue
        cleaned = cleaned.strip('|')
        cells = [c.strip() for c in cleaned.split('|')]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)

    if len(rows) < 2:
        return None, None

    headers = rows[0]
    data = rows[1:]
    return headers, data


_KNOWN_TOPICS = {
    'word', 'term', 'name', 'entry', 'item', 'key',
    'command', 'file', 'pattern', 'variable', 'var',
    'setting', 'option', 'flag', 'param', 'parameter',
    'hotkey', 'shortcut', 'preference', 'hook', 'service',
    'step', 'action', 'field', 'property', 'module',
    'endpoint', 'route', 'method', 'tool', 'plugin',
}

_PREAMBLES_TOPIC = [
    None,
    None,
    '{topic}.',
    '{topic}.',
    'Table of {topic}.',
    'Got some {topic}.',
    'A few {topic}.',
    'The {topic}.',
]

_PREAMBLES_NONE = [None, None, None, None, None]


def table_to_speech(table_text):
    """Convert a table (markdown or unicode box) to natural spoken form.

    Varies the preamble for natural speech. Recognised first-column headers
    get a topic word; unrecognised headers skip the preamble entirely.
    First 1-2 data rows include column headers for context, the rest don't.
    """
    lines = table_text.strip().split('\n')
    headers, data = parse_table_rows(lines)

    if not headers or not data:
        text = _BOX_CHARS.sub('', table_text)
        text = text.replace('|', ' ')
        return text

    first_hdr = headers[0].lower().rstrip('s')
    is_known = first_hdr in _KNOWN_TOPICS

    if is_known:
        topic = headers[0].lower()
        if not topic.endswith('s'):
            topic += 's'
        preamble_tpl = random.choice(_PREAMBLES_TOPIC)
    else:
        topic = None
        preamble_tpl = random.choice(_PREAMBLES_NONE)

    headed_rows = 1 if random.random() < 0.70 else 2

    parts = []
    if preamble_tpl is not None:
        preamble = preamble_tpl.format(topic=topic)
        parts.append(preamble)

    for idx, row in enumerate(data):
        row_parts = []
        use_headers = idx < headed_rows
        for i, cell in enumerate(row):
            if use_headers and i < len(headers):
                row_parts.append(f'{headers[i]}: {cell}')
            else:
                row_parts.append(cell)
        parts.append('. '.join(row_parts) + '.')

    return '\n'.join(parts)
