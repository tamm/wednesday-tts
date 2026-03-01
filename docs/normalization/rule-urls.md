# Rule: URLs

**What it does**: Converts full `https://` URLs and bare `domain/path` patterns to spoken form by replacing `.` with "dot" and `/` with "slash".

**Pipeline position**: steps 2 and 3 — run after fraction/progress rules (step 1b) and before path rules (step 4). Must run before the slash-to-speech rule (step 4b) so that URL slashes are handled with domain-aware logic rather than the generic path splitter.

## Examples

| Input                          | Output                                                                    |
| ------------------------------ | ------------------------------------------------------------------------- |
| `https://ta.mw/unwatch`        | `ta dot m w slash unwatch`                                                |
| `http://localhost:8080/api`    | `local host colon 8080 slash api` (after dictionary converts `localhost`) |
| `https://github.com/user/repo` | `git hub dot com slash user slash repo`                                   |
| `https://example.com`          | `example dot com` (no path)                                               |
| `ta.mw/unwatch`                | `ta dot m w slash unwatch`                                                |
| `docs.python.org/3/library/re` | `docs dot python dot org slash 3 slash library slash re`                  |

## Pattern / logic

### Full URLs (step 2)

Pattern: `https?://([^\s\)\]>"]+)`

The `url_to_speech()` function:

1. Strips trailing punctuation `.,;:!?)>` from the captured URL text.
2. Splits at the first `/` to separate domain from path.
3. Replaces `.` with `dot` in the domain.
4. If a path exists, appends `slash` + path with both `/` and `.` replaced by their spoken forms.

Note: the scheme (`https://` / `http://`) is consumed by the regex and not spoken.

### Bare domain/path patterns (step 3)

Pattern: `\b([a-zA-Z][a-zA-Z0-9-]*\.[a-zA-Z]{2,6})(/[^\s\)\]>,;"]+)`

Requires:

- A domain-like token: starts with a letter, contains a `.`, ends with a 2–6 char TLD.
- Followed immediately by a `/` and a non-whitespace path.

The `rel_url_to_speech()` function replaces `.` in the domain with `dot` and `/` in the path with `slash` and `.` with `dot`.

## Known edge cases / limitations

- The scheme (`https://`) is silently dropped — the spoken form does not say "https".
- Port numbers in URLs (`:8080`) are passed through as-is (the colon and digits remain literal). The TTS engine will read `:8080` as "colon eight thousand and eighty" or similar, which may sound odd.
- Query strings (`?foo=bar`) will have `=` already converted to `equals` by the operator rule (step 0b), since operators run before URLs. The `?` will remain unless it has a digit following it and could be caught by a quantifier rule, which it won't because it only fires in regex context.
- Fragment identifiers (`#section`) pass through unchanged.
- Step 3 (bare domain/path) requires a path segment starting with `/`. A bare domain like `example.com` with no path is not matched — it would need to be written as `https://example.com`.
- Very long URLs will be spoken in full with every path segment. There is no truncation within the URL rule itself.

## Future improvements

- Speak the scheme when it is `http://` (non-secure) to flag it audibly.
- Handle port numbers: `:8080` → "port eight zero eight zero".
- Truncate very long URLs at a sensible depth (e.g., after 3 path segments).
- Handle query strings more cleanly (e.g., `?key=value` → "question mark key equals value" or drop them).
