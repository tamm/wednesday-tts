# Rule: Operators

**What it does**: Converts programming comparison and assignment operators to their spoken English equivalents.

**Pipeline position**: step 0b — runs after regex conversion (step 0b-pre) and before the dictionary (step 1). Multi-character operators must be matched before the bare `=` replacement, which is why the order within this block is critical.

## Examples

| Input         | Output                           |
| ------------- | -------------------------------- |
| `x !== y`     | `x  not equals  y`               |
| `x === y`     | `x  equals  y`                   |
| `x == y`      | `x  equals  y`                   |
| `x != y`      | `x  not equals  y`               |
| `a <= b`      | `a  less than or equal to  b`    |
| `a >= b`      | `a  greater than or equal to  b` |
| `fn => value` | `fn  to  value`                  |
| `count += 1`  | `count  plus equals  1`          |
| `count -= 1`  | `count  minus equals  1`         |
| `val *= 2`    | `val  times equals  2`           |
| `x = 5`       | `x  equals  5`                   |

## Pattern / logic

Seven `re.sub` calls in strict order, followed by one `str.replace`:

1. `!==?` → `not equals` — catches both `!==` and `!=`
2. `===?` → `equals` — catches both `===` and `==`
3. `<=` → `less than or equal to`
4. `>=` → `greater than or equal to`
5. `=>` → `to` (fat arrow / arrow function)
6. `\+=` → `plus equals`
7. `-=` → `minus equals`
8. `\*=` → `times equals`
9. `=` (bare `str.replace`, not regex) → `equals` — any `=` not already consumed

The comment in the source notes that all compound operators are replaced before the bare `=` step, so by step 9 every remaining `=` is a standalone assignment.

### Step 0b2 — Negative numbers

After all compound operators are consumed, step 0b2 handles negative numbers:

Pattern: `(?<![a-zA-Z0-9-])-(\d)` → `negative \1`

Matches a minus sign directly adjacent to a digit (no space between them), not preceded by a word character, digit, or another minus. The key safety properties:

- `-=` — already consumed in step 7 above, won't be seen here
- `x - 3` — safe because there is a space after `-`, so `-` is not directly adjacent to `3` and `-(\d)` never matches
- `vm-01` — safe because `m` (alpha) before `-` is excluded by the lookbehind
- `min: -3` — fires correctly, becomes `min: negative 3` (space before `-` is not excluded)

| Input  | Output         |
| ------ | -------------- |
| `-3.5` | `negative 3.5` |
| `-4`   | `negative 4`   |
| `-0.5` | `negative 0.5` |

## Known edge cases / limitations

- Step 9 is a global `str.replace`, not a whole-word match, so `=` inside a string like `--flag=value` or a URL query parameter `?a=1` will also be converted. The path/URL rules (step 2, step 3) run after operators, but URLs processed by the regex match in step 2 (`https?://`) have their content already converted before this, so the `=` in a query string like `?foo=bar` will already have been eaten by the URL rule if the URL started with `https://`. Bare paths and non-URL `=` are affected.
- `/=` (divide-assign) is not handled.
- `<` and `>` (bare less-than and greater-than) are not converted.
- `&&`, `||`, `!` (boolean operators) are not converted.
- Arrow notation `->` (common in Rust, C, Python type hints) is not converted.
- The replacements add spaces on both sides, which can create double-spaces in surrounding text. These are collapsed later by `clean_text_for_speech()`.

## Future improvements

- Use word-boundary or context-aware matching for bare `=` to avoid false-positives in `--flag=value` patterns.
- Add `/=`, `<`, `>`, `&&`, `||`.
- Add `->` for Rust/C/Python return type hints.
