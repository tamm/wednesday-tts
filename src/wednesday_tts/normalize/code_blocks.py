"""Code block to spoken form conversion."""

import re


def code_block_to_speech(m):
    """Convert a fenced code block to spoken form instead of silently dropping it."""
    code = m.group(1).strip()
    if not code:
        return ' '

    lines = code.split('\n')

    # Strip tree-drawing characters
    cleaned = []
    for line in lines:
        line = re.sub(r'[├└│─┬┤┐┘┌┼╮╯╰╭]+', '', line)
        line = line.strip()
        if line:
            cleaned.append(line)

    if not cleaned:
        return ' '

    # Truncate long code blocks — read first ~8 lines max
    if len(cleaned) > 8:
        cleaned = cleaned[:8]
        cleaned.append('and more.')

    spoken = '. '.join(cleaned)
    return f' Code: {spoken}. '


def normalize_code_blocks(text):
    """Process fenced code blocks into spoken form.

    Must run before other normalization so code content doesn't get mangled.
    """
    text = re.sub(r'```[a-zA-Z]*\n?([\s\S]*?)```', code_block_to_speech, text)
    return text
