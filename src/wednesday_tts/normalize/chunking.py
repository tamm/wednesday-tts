"""Text chunking for streaming TTS — split text at natural break points."""

import re


def chunk_text_intelligently(text, first_chunk_min=40, first_chunk_max=150,
                             second_third_min=80, second_third_max=150,
                             chunk_min=200, chunk_max=400):
    """Split text into chunks optimized for streaming TTS.

    First chunk is shorter for fast initial response. Subsequent chunks
    get progressively larger. Break points prefer sentence boundaries,
    then clause boundaries, then word boundaries.
    """
    chunks = []
    remaining = text.strip()

    def find_break_point(text, start, end):
        """Find best natural break point in range."""
        sentence_breaks = []
        for i in range(start, min(end, len(text))):
            if text[i] in '.!?' and i + 1 < len(text) and text[i + 1] in ' \n\t':
                sentence_breaks.append(i + 1)
        if sentence_breaks:
            return sentence_breaks[-1]

        clause_breaks = []
        for i in range(start, min(end, len(text))):
            if text[i] in ',;:' and i + 1 < len(text) and text[i + 1] in ' \n\t':
                clause_breaks.append(i + 1)
        if clause_breaks:
            return clause_breaks[-1]

        for i in range(min(end, len(text)) - 1, start - 1, -1):
            if text[i] in ' \n\t':
                return i + 1
        return min(end, len(text))

    if remaining:
        first_target = min(first_chunk_max, len(remaining))
        if first_target >= 50:
            break_point = find_break_point(remaining, 50, first_target)
        else:
            break_point = find_break_point(remaining, 0, first_target)

        if break_point < first_chunk_min and len(remaining) > first_chunk_min:
            extended_max = min(first_chunk_max * 2, len(remaining))
            extended_break = find_break_point(remaining, first_chunk_min, extended_max)
            if extended_break > break_point:
                break_point = extended_break

        chunks.append(remaining[:break_point].strip())
        remaining = remaining[break_point:].strip()

    for _ in range(2):
        if not remaining:
            break
        if len(remaining) <= second_third_max:
            chunks.append(remaining)
            remaining = ""
            break
        break_point = find_break_point(remaining, second_third_min, second_third_max)
        chunks.append(remaining[:break_point].strip())
        remaining = remaining[break_point:].strip()

    while remaining:
        if len(remaining) <= chunk_max:
            chunks.append(remaining)
            break
        break_point = find_break_point(remaining, chunk_min, chunk_max)
        chunks.append(remaining[:break_point].strip())
        remaining = remaining[break_point:].strip()

    return [c for c in chunks if c]


def chunk_text_server(text, min_size=200, max_size=400):
    """Server-side chunking: split text into chunks for streaming TTS.

    First chunk is 60-120 chars with natural breaks for fast initial response.
    """
    first_chunk = ""
    rest_text = text

    if len(text) > 60:
        search_region = text[60:min(120, len(text))]
        for pattern in [r'[.!?]+\s+', r'[,;:]\s+', r'\W(?=\s)']:
            match = re.search(pattern, search_region)
            if match:
                split_pos = 60 + match.end()
                first_chunk = text[:split_pos].strip()
                rest_text = text[split_pos:].strip()
                break

        if not first_chunk and len(text) > 120:
            search_region = text[60:min(150, len(text))]
            for pattern in [r'[.!?]+\s+', r'[,;:]\s+', r'\W(?=\s)']:
                match = re.search(pattern, search_region)
                if match:
                    split_pos = 60 + match.end()
                    first_chunk = text[:split_pos].strip()
                    rest_text = text[split_pos:].strip()
                    break

        if not first_chunk:
            sentence_match = re.search(r'^[^.!?]+[.!?]+', text)
            if sentence_match:
                first_chunk = sentence_match.group(0).strip()
                rest_text = text[len(first_chunk):].strip()
    elif len(text) > 30:
        search_region = text[30:min(60, len(text))]
        for pattern in [r'[.!?]+\s+', r'[,;:]\s+']:
            match = re.search(pattern, search_region)
            if match:
                split_pos = 30 + match.end()
                first_chunk = text[:split_pos].strip()
                rest_text = text[split_pos:].strip()
                break

    chunks = [first_chunk] if first_chunk else []

    sentences = re.split(r'([.!?]+(?:\s+|$))', rest_text)
    current_chunk = ""

    for i in range(0, len(sentences) - 1, 2):
        sentence = sentences[i]
        punctuation = sentences[i + 1] if i + 1 < len(sentences) else ""
        full_sentence = sentence + punctuation

        if current_chunk and len(current_chunk) + len(full_sentence) > max_size:
            chunks.append(current_chunk.strip())
            current_chunk = full_sentence
        else:
            current_chunk += full_sentence

        if len(current_chunk) >= min_size and punctuation.strip() in ['.', '!', '?']:
            chunks.append(current_chunk.strip())
            current_chunk = ""

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text]
