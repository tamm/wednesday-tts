# Rule: Numbers and Units

**What it does**: Converts time and unit abbreviations (`300ms`, `0.5s`, `2-4s`), small decimals (`0.022`), and leading-dot decimals (`.5`) to natural spoken form with digit-by-digit fractional expansion.

**Pipeline position**: steps 4c and 4d — run after path rules (step 4b2) and before the version-string rule (step 5). Unit rules must run before small-decimal so that `0.5s` is consumed whole and does not produce `zero point fives`. Both must run before the version-string rule (step 5) so that `0.022` is not misread as a version number.

## Examples

| Input       | Output                    |
| ----------- | ------------------------- |
| `300ms`     | `300 milliseconds`        |
| `1.5ms`     | `1 point 5 milliseconds`  |
| `0.5s`      | `zero point 5 seconds`    |
| `2-4s`      | `2 to 4 seconds`          |
| `100-300ms` | `100 to 300 milliseconds` |
| `5min`      | `5 minutes`               |
| `0.022`     | `zero point oh two two`   |
| `0.5`       | `zero point five`         |
| `.5`        | `point five`              |
| `.022`      | `point oh two two`        |

## Pattern / logic

### 4c. Time/unit ranges — `N-Ms` / `N-Mms`

Pattern: `\b(\d+)-(\d+)(ms|min|s)\b`

`UNIT_MAP = {'ms': 'milliseconds', 's': 'seconds', 'min': 'minutes'}`

Produces: `{lo} to {hi} {unit_spoken}`.

Must run before the single-value rule below, as it is more specific.

### 4c. Single unit values — `0.5s`, `300ms`

Pattern: `\b(\d+(?:\.\d+)?)(ms|min|s)\b`

`unit_to_speech()` logic:

- If the number contains a decimal, splits at `.` into integer and fractional parts.
  - Fractional part is expanded digit-by-digit via `DIGIT_WORDS`.
  - If integer part is `0`, produces `zero point {frac_digits}`.
  - Otherwise produces `{integer} point {frac_digits}`.
- If no decimal, the number is passed through unchanged.
- Result: `{num_spoken} {unit_spoken}`.

Comment in source: must also run before the 3-digit HTTP-code rule so `503s` → `503 seconds` and is not ambiguously treated as an HTTP code.

### 4d. Small decimals — `0.NNN`

Pattern: `\b(0)\.(\d+)\b`

`small_decimal_to_speech()` expands each fractional digit via `DIGIT_WORDS` (0→"oh", 1→"one", …, 9→"nine") and produces `zero point {digits}`.

So `0.022` → `zero point oh two two`, not `zero point twenty-two`.

### 4d. Leading-dot decimals — `.NNN`

Pattern: `(?<!\w)(?<!\d)\.(\d+)\b`

Negative lookbehind for both word chars and digits prevents matching inside `v1.5` or `main.py`.

Produces: `point {digit words}`.

## Known edge cases / limitations

- The unit patterns only cover `ms`, `s`, and `min`. Other units (`kg`, `km`, `Hz`, `kHz`, `px`, `pt`, etc.) pass through unchanged.
- `503s` (a unit value like "five hundred and three seconds") triggers the unit rule correctly because the unit rule runs before the 3-digit code rule. However, `503 s` with a space will not match because the pattern requires the unit to be directly adjacent (`\b(\d+(?:\.\d+)?)(ms|min|s)\b` with no space).
- The small-decimal rule fires on ALL `0.NNN` patterns, including things like price values (`$0.50`) or version segments. However, version segments are usually caught by model-version or version rules earlier in the pipeline, and prices — if they have a `$` — are not affected because the leading `$` is not a word character and the pattern starts with `\b(0)`.
- Leading-dot decimals (`.5`) will match in isolation but the lookbehind does not account for all contexts. For example, `.5` at the start of a sentence is fine, but `.5em` (a CSS unit) would match and the `em` would be left as raw text.
- `DIGIT_WORDS` maps `'0'` → `'oh'` (not `'zero'`). This is intentional for fractional digits (so `0.022` reads "zero point oh two two", not "zero point zero two two") but may feel inconsistent with the integer part which says "zero".

## Future improvements

- Add more unit suffixes: `Hz`, `kHz`, `MHz`, `GHz`, `px`, `pt`, `em`, `rem`, `kb`, `mb`, `gb`.
- Handle `$0.50` price notation explicitly.
- Consider whether leading-zero fractional digits should say "zero" vs "oh" — currently "oh" is used.
