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


def table_to_speech(table_text):
    """Convert a table (markdown or unicode box) to natural spoken form.

    Reads like a person would: announce the topic, use column headers on the
    first 1-3 data rows (weighted random), then just read cell values with
    pauses between them for the rest.
    """
    lines = table_text.strip().split('\n')
    headers, data = parse_table_rows(lines)

    if not headers or not data:
        text = _BOX_CHARS.sub('', table_text)
        text = text.replace('|', ' ')
        return text

    first_hdr = headers[0].lower().rstrip('s')
    if first_hdr in ('word', 'term', 'name', 'entry', 'item', 'key',
                      'command', 'file', 'pattern', 'variable', 'var',
                      'setting', 'option', 'flag', 'param', 'parameter'):
        topic = f'{headers[0].lower()}s'
    else:
        topic = 'entries'

    r = random.random()
    headed_rows = 1 if r < 0.60 else (2 if r < 0.85 else 3)

    parts = []
    parts.append(f'Table of {topic}.')

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
