# Rule: HTTP Status Codes

**What it does**: Converts 3-digit HTTP-style codes to digit-by-digit spoken form, but only when context clearly indicates a status or error code — not when the number is a plain quantity.

**Pipeline position**: step 6b — runs after version strings (step 5) and the time/unit rules (step 4c). The comment in the source explicitly notes that the unit rule (`503s` → "503 seconds") and the version rule must run first so those patterns are already consumed before this rule fires.

## Examples

| Input            | Output                                                           |
| ---------------- | ---------------------------------------------------------------- |
| `returned a 404` | `returned a four oh four`                                        |
| `error 500`      | `error five oh oh`                                               |
| `HTTP 200`       | `H T T P two oh oh` (after dictionary converts HTTP)             |
| `status 301:`    | `status three oh one:` (colon following triggers digit-by-digit) |
| `got a 503`      | `got a five oh three`                                            |
| `404:`           | `four oh four:` (colon immediately following)                    |
| `500 files`      | `500 files` (quantity — left unchanged)                          |
| `200 people`     | `200 people` (quantity — left unchanged)                         |

## Pattern / logic

Pattern: `\b([1-9]\d{2})\b`

Before converting, `code_to_speech()` applies two checks:

1. **First-digit range**: only digits 1–5 are valid HTTP code ranges. A 3-digit number like `600` or `900` passes through unchanged. (Technically `6xx` is not standard HTTP, but the range `1–5` covers all real HTTP status families: 1xx informational, 2xx success, 3xx redirect, 4xx client error, 5xx server error.)

2. **Context check**: looks at up to 30 characters before the match position and checks against `CODE_BEFORE`:

   ```
   (?:error|status|code|returned|return|response|HTTP|H T T P|threw|raised|got|received|with)\s+(?:a\s+)?
   ```

   (case-insensitive, requires the code-word to be at the end of the preceding slice)

   OR checks if the 3 characters after the match start with `:` (e.g., `404: not found`).

If either condition is met, `digits_to_spoken()` converts each digit individually via `DIGIT_WORDS` (0→"oh", 1→"one", …, 9→"nine"). Otherwise the number is returned unchanged.

## Known edge cases / limitations

- The lookback window is fixed at 30 characters. If a code-related word is more than 30 characters before the number (e.g., due to a long intervening phrase) the context is missed and the code reads as a plain number.
- `H T T P` (already dictionary-expanded form of `HTTP`) is included in `CODE_BEFORE` to handle the case where the dictionary fires before this rule. This is a layered dependency.
- Numbers 600–999 starting with 6–9 are never converted even if they appear after `error`. This is intentional — only valid HTTP ranges are treated as codes.
- The rule fires on any 3-digit number that happens to appear after a trigger word, not just genuine HTTP codes. For example, `got a 302 redirect` works correctly, but `returned 100 items` would also trigger (though `100` is in the 1xx range, it reads "one oh oh" which is usually correct for an informational response code).
- The `with` keyword in `CODE_BEFORE` is quite broad — `with 200 apples` would trigger digit-by-digit conversion. However `with` is listed last in the pattern; in practice `200 apples` is more likely to appear in prose without a code-related word.
- After version strings are expanded in step 5, version numbers like `3.11` no longer contain a bare 3-digit sequence at word boundaries, so they are safe from this rule.

## Future improvements

- Expand `CODE_BEFORE` to include `exited with`, `exits with`, `failed with`.
- Consider a forward-context check: if the number is followed by a word like `files`, `items`, `records`, `users`, treat it as a quantity regardless of preceding context.
- Widen the lookback window from 30 to 50 characters.
