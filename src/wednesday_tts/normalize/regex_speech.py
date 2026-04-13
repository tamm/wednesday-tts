"""Regex pattern to spoken description conversion."""

import re

# Heuristic: a string is "regex-like" if it contains at least one of these
REGEX_SIGNALS = re.compile(
    r"\(\?[<!=]|"  # lookaround
    r"\\[dwWsSbB]|"  # char class shorthand
    r"\[\^?(?:[^\]]*[\\^]|[^\]]*\w-\w)[^\]]*\]|"  # char class with metachar/range
    r"\{[0-9,]+\}|"  # quantifier {n,m}
    r"(?<!\\)\(\?:|"  # non-capturing group
    r"\(\?[imsxLu]"  # flags group
)


def regex_to_speech(pattern):
    """Convert a regex string to a rough spoken description."""
    s = pattern

    # Lookarounds
    s = s.replace(r"(?<!", " negative lookbehind ")
    s = s.replace(r"(?<=", " positive lookbehind ")
    s = s.replace(r"(?!", " negative lookahead ")
    s = s.replace(r"(?=", " positive lookahead ")
    s = s.replace(r"(?:", " group ")

    # Common char class shorthands
    s = s.replace(r"\d", " digit ")
    s = s.replace(r"\D", " non-digit ")
    s = s.replace(r"\w", " word-char ")
    s = s.replace(r"\W", " non-word-char ")
    s = s.replace(r"\s", " whitespace ")
    s = s.replace(r"\S", " non-whitespace ")
    s = s.replace(r"\b", " word-boundary ")
    s = s.replace(r"\B", " non-word-boundary ")
    s = s.replace(r"\n", " newline ")
    s = s.replace(r"\t", " tab ")

    # Escaped literal chars
    s = re.sub(r"\\([^a-zA-Z])", r" \1 ", s)

    # Char classes — BEFORE anchor replacement so [^...] doesn't get ^ mangled.
    def _char_class(m):
        content = m.group(1)
        if content.startswith("^"):
            return f"not one of {content[1:]}"
        return f"one of {content}"

    s = re.sub(r"\[([^\]]+)\]", _char_class, s)

    # Anchors
    s = s.replace("^", "start ")
    s = s.replace("$", " end")

    # Quantifiers
    s = re.sub(r"\{(\d+),(\d+)\}", r" \1 to \2 times ", s)
    s = re.sub(r"\{(\d+),\}", r" \1 or more times ", s)
    s = re.sub(r"\{(\d+)\}", r" exactly \1 times ", s)
    s = s.replace("+", " one or more ")
    s = s.replace("*", " zero or more ")
    s = s.replace("?", " optional ")

    # Groups/parens
    s = s.replace("(", "").replace(")", "")

    # Pipe = or
    s = s.replace("|", " or ")

    # Dot
    s = s.replace(".", "any-char")

    # Tidy up
    s = re.sub(r"\s+", " ", s).strip()
    return f"regex: {s}"


def normalize_regex(text):
    """Detect and convert regex patterns in text to spoken descriptions.

    Handles: r'...' Python raw strings, /.../ JS literals, and bare regex chunks.
    Must run early so gobbledygook like regex metacharacters doesn't get read raw.
    """
    # Match r'...' or r"..." style raw strings (common in Python code blocks)
    text = re.sub(r'\br([\'"])((?:(?!\1).)+)\1', lambda m: regex_to_speech(m.group(2)), text)

    # /regex/ style (JS/Ruby style literals)
    text = re.sub(
        r"(?<![`\w])/([^/\n]{4,60})/(?:gi?|i|g)?(?=\s|[,.\)]|$)",
        lambda m: regex_to_speech(m.group(1)) if REGEX_SIGNALS.search(m.group(1)) else m.group(0),
        text,
    )

    # Bare regex chunks in prose/comments
    text = re.sub(
        r"(?<!\w)((?:\\[dwWsSbB]|"
        r"\(\?(?:[<!=][^)]*|:)|"
        r"\\[^a-zA-Z ]|"
        r"\[\^?(?:[^\]]*[\\^]|[^\]]*\w-\w)[^\]]*\]|"
        r"[+*?{}|^$])"
        r"(?:[^,\s]{0,40})?)",
        lambda m: regex_to_speech(m.group(1)) if REGEX_SIGNALS.search(m.group(1)) else m.group(0),
        text,
    )

    return text


def normalize_html_tags(text):
    """Convert HTML/XML tags to spoken form.

    <div> -> "div tag", </div> -> "end div", <br/> -> "self closing br"
    """
    text = re.sub(r"</([a-zA-Z][a-zA-Z0-9]*)\s*>", r" end \1 ", text)
    text = re.sub(r"<([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?\s*/>", r" self closing \1 ", text)
    text = re.sub(r"<([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>", r" \1 tag ", text)
    return text


def normalize_hotkeys(text):
    """Convert keyboard shortcuts to spoken form.

    Ctrl+Option+Q -> "Control Option Q"
    """
    modifier_names = {
        "ctrl": "Control",
        "cmd": "Command",
        "alt": "Alt",
        "option": "Option",
        "shift": "Shift",
        "fn": "Function",
        "meta": "Meta",
        "super": "Super",
    }

    def _hotkey_to_speech(m):
        parts = m.group(0).split("+")
        spoken = []
        for p in parts:
            spoken.append(modifier_names.get(p.lower(), p))
        return " ".join(spoken)

    text = re.sub(
        r"(?i)\b(?:Ctrl|Cmd|Alt|Option|Shift|Fn|Meta|Super)"
        r"(?:\+(?:Ctrl|Cmd|Alt|Option|Shift|Fn|Meta|Super))*\+[A-Za-z0-9]+\b",
        _hotkey_to_speech,
        text,
    )
    return text
