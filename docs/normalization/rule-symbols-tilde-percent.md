# Rule: Symbols — Tilde, Multiplication, Percent, Progress Fractions

**What it does**: Converts `~N` approximation notation, the `×` multiplication sign, `N%` percentages, and `N/M` / `N-M/M` progress fractions to natural spoken English.

**Pipeline position**: steps 1a and 1b — run after the dictionary (step 1) and before URL/path rules (steps 2–4). The fraction rule (1b) must run before the slash-to-speech rules in step 4b; otherwise the slash in `4/10` would already be gone.

## Examples

| Input        | Output                                            |
| ------------ | ------------------------------------------------- |
| `~10`        | `around 10`                                       |
| `~0.5`       | `around 0.5`                                      |
| `~ 3`        | `around 3` (optional space between `~` and digit) |
| `10×`        | `10 times `                                       |
| `20%`        | `20 percent`                                      |
| `3.5%`       | `3.5 percent`                                     |
| `4/10`       | `4 of 10`                                         |
| `1-4/10`     | `1 to 4 of 10`                                    |
| `12/01/2024` | `12/01/2024` (date — not converted)               |

## Pattern / logic

### Tilde approximation (step 1a)

```
~\s*(\d)  →  around \1
```

Matches `~` followed by optional whitespace and a digit. Replaces with `around` followed by the digit (and the rest of the number is preserved because only the first digit is in the capture group — the substitution replaces `~` and the optional space with `around `).

### Multiplication sign (step 1a)

Plain `str.replace`: `×` → `times`.

### Percentages (step 1a2)

```
(\d+(?:\.\d+)?)\s*%  →  \1 percent
```

Matches an integer or decimal number followed by optional whitespace and `%`. Handles `20%`, `3.5%`, `100 %`.

### Progress fractions (step 1b)

Two patterns, applied in order:

**Range form** `N-M/Total` (e.g., `1-4/10` → `1 to 4 of 10`):

```
\b(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\b
```

**Simple form** `N/M` (e.g., `4/10` → `4 of 10`):

```
(?<!/)\b(\d+)\s*/\s*(\d+)\b(?!\s*/\s*\d)
```

The negative lookbehind `(?<!/)` and negative lookahead `(?!\s*/\s*\d)` together skip date-like patterns of the form `12/01/2024` (which have another `/digit` following).

## Known edge cases / limitations

- The tilde rule (`~\s*(\d)`) replaces `~` and the space before the digit but the replacement string is `around \1`, which inserts only `around ` before the digit. The rest of the number (e.g., `10` in `~10`) is not inside the group but is preserved in the output because the substitution only removes the `~` and optional space, not the remaining digits.
- `~text` (tilde before a non-digit) is not converted — e.g., `~/.ssh` is handled by the tilde-path rule (step 4a), not here.
- The date exclusion for `N/M` uses a negative lookahead that only checks for one further `/digit` segment, so a two-part date like `12/01` (without year) would still be converted to `12 of 1`.
- `N/M` fractions where N or M are large (e.g., `1024/4096`) are indistinguishable from progress fractions. This is accepted behaviour — they read well as "1024 of 4096".
- `×` (Unicode U+00D7 multiplication sign) is handled but `*` (ASCII asterisk) is only handled in the operators block for `*=`.

## Future improvements

- Exclude common date patterns `\d{1,2}/\d{1,2}/\d{2,4}` more robustly.
- Handle `N × M` grid notation (e.g., `3 × 4`) — currently converts to `3  times  4` which adds extra spaces.
- Consider handling `N:M` ratios.
