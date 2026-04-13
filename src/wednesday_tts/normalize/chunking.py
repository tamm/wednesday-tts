"""Text chunking for streaming TTS — split text at natural break points."""

import re

# Sentence-end pattern: one or more of .!? followed by whitespace.
# The negative lookbehind refuses to match when the period is preceded by a
# word-boundary + single letter — i.e. list labels like "A." or initials like
# "U.S." — so "Next pending: A. foo" does NOT split at "A." and get stranded
# as a tiny first chunk that takes forever for the next one to land.
#
# Both chunk_text_intelligently and chunk_text_server use this, so the rule
# is enforced in exactly one place. The string form is used by re.split in
# chunk_text_server where a capturing group is needed.
_SENTENCE_END = re.compile(r"(?<!\b[A-Za-z])[.!?]+\s+")
_SENTENCE_END_CAPTURE = re.compile(r"((?<!\b[A-Za-z])[.!?]+(?:\s+|$))")


def _find_sentence_end(text, start, end):
    """Return the index just past the last sentence-end in text[start:end], or -1.

    Uses the shared _SENTENCE_END pattern so that list labels like "A." and
    initials like "U.S." are not treated as sentence ends.
    """
    last = -1
    for m in _SENTENCE_END.finditer(text, start, min(end, len(text))):
        last = m.end()
    return last


def chunk_text_intelligently(
    text,
    first_chunk_min=40,
    first_chunk_max=150,
    second_third_min=80,
    second_third_max=150,
    chunk_min=200,
    chunk_max=400,
):
    """Split text into chunks optimized for streaming TTS.

    First chunk is shorter for fast initial response. Subsequent chunks
    get progressively larger. Break points prefer sentence boundaries,
    then clause boundaries, then word boundaries.
    """
    chunks = []
    remaining = text.strip()

    def find_break_point(text, start, end):
        """Find best natural break point in range."""
        sentence_end = _find_sentence_end(text, start, end)
        if sentence_end > 0:
            return sentence_end

        clause_breaks = []
        for i in range(start, min(end, len(text))):
            # Colons are deliberately excluded — "Next pending: ..." must not
            # split there, or the listener hears a tiny first chunk and waits
            # an eternity for the next.
            if text[i] in ",;" and i + 1 < len(text) and text[i + 1] in " \n\t":
                clause_breaks.append(i + 1)
        if clause_breaks:
            return clause_breaks[-1]

        for i in range(min(end, len(text)) - 1, start - 1, -1):
            if text[i] in " \n\t":
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


def chunk_text_server(text, min_size=200, max_size=400, backend_name=None):
    """Server-side chunking: split text into chunks for streaming TTS.

    First chunk is 60-120 chars with natural breaks for fast initial response.
    """
    if backend_name == "qwen3":
        min_size = min(min_size, 90)
        max_size = min(max_size, 180)

    first_chunk = ""
    rest_text = text

    # Preference order for first-chunk break: sentence end, clause, whitespace.
    # Each entry is (compiled_pattern, None) for precompiled or re.compile(...).
    # \W(?=\s) is deliberately NOT used — it matches a bare "." as a word
    # character and would re-introduce the "U." split bug.
    _CLAUSE_BREAK = re.compile(r"[,;]\s+")
    _WHITESPACE = re.compile(r"\s+")

    def _first_break(region, break_patterns):
        for pattern in break_patterns:
            m = pattern.search(region)
            if m:
                return m.end()
        return -1

    if len(text) > 60:
        region = text[60 : min(120, len(text))]
        end = _first_break(region, [_SENTENCE_END, _CLAUSE_BREAK, _WHITESPACE])
        if end > 0:
            split_pos = 60 + end
            first_chunk = text[:split_pos].strip()
            rest_text = text[split_pos:].strip()

        if not first_chunk and len(text) > 120:
            region = text[60 : min(150, len(text))]
            end = _first_break(region, [_SENTENCE_END, _CLAUSE_BREAK, _WHITESPACE])
            if end > 0:
                split_pos = 60 + end
                first_chunk = text[:split_pos].strip()
                rest_text = text[split_pos:].strip()

        if not first_chunk:
            # No suitable break in the search window — take everything up to
            # (and including) the first real sentence end anywhere in text.
            m = _SENTENCE_END.search(text)
            if m:
                first_chunk = text[: m.end()].strip()
                rest_text = text[m.end() :].strip()
    elif len(text) > 30:
        region = text[30 : min(60, len(text))]
        end = _first_break(region, [_SENTENCE_END, _CLAUSE_BREAK])
        if end > 0:
            split_pos = 30 + end
            first_chunk = text[:split_pos].strip()
            rest_text = text[split_pos:].strip()

    chunks = [first_chunk] if first_chunk else []

    sentences = _SENTENCE_END_CAPTURE.split(rest_text)
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

        if len(current_chunk) >= min_size and punctuation.strip() in [".", "!", "?"]:
            chunks.append(current_chunk.strip())
            current_chunk = ""

    # Pick up the tail: re.split with a capturing group always returns an
    # odd-length list, where the final element is whatever came after the
    # last sentence-end match. If there were NO matches at all, the list is
    # [rest_text] and the loop above ran zero times — we must still consume
    # that tail. Without this, any trailing text without a terminal period
    # (or any rest_text with zero sentence-ends) is silently lost.
    if len(sentences) % 2 == 1 and sentences[-1]:
        tail = sentences[-1]
        if current_chunk and len(current_chunk) + len(tail) > max_size:
            chunks.append(current_chunk.strip())
            current_chunk = tail
        else:
            current_chunk += tail

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks if chunks else [text]
