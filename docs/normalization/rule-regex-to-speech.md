# Rule: Regex to Speech

**What it does**: Detects regex patterns in three surface forms (Python raw strings, JS/Ruby `/pattern/` literals, and bare metacharacter sequences in prose) and converts them to a rough English description prefixed with "regex:".

**Pipeline position**: step 0b-pre — runs immediately after backtick processing (step 0), before operators (step 0b). Must precede operators because regex patterns contain `=`, `+`, `*`, `?`, `$` that would otherwise be consumed by the operator rules.

## Examples

| Input                | Output                                                             |
| -------------------- | ------------------------------------------------------------------ |
| `r'\b(\d+)\b'`       | `regex: word-boundary one of  digit  one or more  word-boundary`   |
| `/\w+/g`             | `regex: word-char one or more `                                    |
| `(?<!\w)\.(\d+)`     | `regex: negative lookbehind word-char any-char digit one or more ` |
| `r'^https?://'`      | `regex: start aitch tee tee pee optional  slash slash`             |
| `r'\{(\d+),(\d+)\}'` | `regex: one of  digit  1 to 2 times ` (rough)                      |

## Pattern / logic

### Detection heuristic

A string is treated as regex only if it contains at least one "signal":

```
(?<![<!=]   lookaround syntax
\\[dwWsSbB] char-class shorthand
\[^?...\]   character class
\{N,M\}     quantifier braces
(?:         non-capturing group
(?[imsxLu]  flags group
```

Strings that lack all these signals are left alone (avoids false-positives on ordinary prose).

### Three surface forms matched

1. **Python raw strings** `r'...'` / `r"..."` — pattern `\br(['"])((?:(?!\1).)+)\1`. Any match is passed unconditionally to `regex_to_speech()` (no signal check — the `r''` prefix is itself sufficient signal).

2. **Slash-delimited literals** `/pattern/gi?` — pattern matches 4–60 chars between `/…/` with optional flags. Requires the signal heuristic to fire before converting.

3. **Bare metacharacter runs in prose** — a looser pattern matches runs starting with `\d`, `\w`, lookaround syntax, escaped non-alpha chars, char classes, or quantifiers. Again gated on the signal heuristic.

### Conversion steps inside `regex_to_speech()`

Applied in order:

1. Lookarounds: `(?<!` → `negative lookbehind`, `(?<=` → `positive lookbehind`, `(?!` → `negative lookahead`, `(?=` → `positive lookahead`, `(?:` → `group`
2. Char-class shorthands: `\d` → `digit`, `\D` → `non-digit`, `\w` → `word-char`, `\W` → `non-word-char`, `\s` → `whitespace`, `\S` → `non-whitespace`, `\b` → `word-boundary`, `\B` → `non-word-boundary`, `\n` → `newline`, `\t` → `tab`
3. Escaped literal chars: `\X` (non-alpha) → `X` with surrounding spaces
4. Anchors: `^` → `start `, `$` → ` end`
5. Quantifiers: `{N,M}` → `N to M times`, `{N,}` → `N or more times`, `{N}` → `exactly N times`, `+` → `one or more`, `*` → `zero or more`, `?` → `optional`
6. Char classes: `[...]` → `one of ...`
7. Groups/parens: `(` and `)` removed
8. Pipe: `|` → `or`
9. Dot: `.` → `any-char`
10. Whitespace collapsed

Result is prefixed with `regex: `.

## Known edge cases / limitations

- The conversion is intentionally rough — it conveys intent, not a precise re-reading. Nested quantifiers, backreferences, named groups, and Unicode properties are not handled.
- The `^` anchor replacement happens globally so `^` inside a character class like `[^abc]` becomes `start abc` instead of "not abc".
- Slash-literal matching requires the pattern to be 4–60 characters long; very short or very long patterns escape detection.
- The bare-metacharacter pattern can false-positive on things like shell globs or math expressions that happen to contain `+` or `*` near `\d`.
- After `regex_to_speech()` runs, subsequent pipeline steps (operators, paths, numbers) will not re-process the result because the spoken form no longer matches their triggers.

## Future improvements

- Handle `[^...]` (negated character classes) distinctly from `[...]`.
- Improve named-group detection: `(?P<name>...)` in Python regex.
- Add a length floor on bare-metacharacter matching to reduce false-positives.
