# Rule: Homographs (proposed, not yet implemented)

**Status**: Proposed — not in pipeline yet

---

## The problem

"Read" is a homograph — same spelling, two different pronunciations depending on tense:

- Present / infinitive: /riːd/ — "I read books every day", "read the docs", "to read a file"
- Past tense / past participle: /rɛd/ — "the file was read", "it has been read", "she had read it"

Pocket TTS has no SSML support and no grammatical parser, so it defaults to one pronunciation. In practice it picks the present-tense /riːd/ for all occurrences. This means every passive construction and perfect aspect usage sounds wrong.

Claude's output about code and documentation uses the past-participle form heavily: "the file was read", "the config can be read", "errors were read from stderr". These are all mispronounced.

**Proposed fix**: Before sending text to TTS, detect past-tense / past-participle "read" using surrounding grammatical context and substitute the phonetically-equivalent spelling "red", which TTS reliably pronounces /rɛd/.

---

## Reliable past-tense signals

These patterns identify "read" as past participle / past tense with high confidence. They all require a grammatical trigger word immediately adjacent to "read" — no intervening content except whitespace.

### Pattern A — Passive voice auxiliaries

The "be" family of auxiliaries followed directly by "read" signals passive voice. In passive voice "read" is always the past participle /rɛd/.

Triggers: `was`, `were`, `is`, `are`, `am`, `be`, `been`, `being`

Examples:

- "the file **was read**" — passive simple past
- "it **is read**" — passive present (still past participle pronunciation)
- "**being read** aloud" — progressive passive
- "has **been read**" — perfect passive (the "been" triggers this pattern; "has" triggers pattern B separately)
- "**to be read**" — infinitive passive ("to be" contains "be" which triggers)

### Pattern B — Perfect aspect auxiliaries

The "have" family followed directly by "read" signals perfect aspect. Perfect aspect past participle is always /rɛd/.

Triggers: `has`, `have`, `had`

Examples:

- "she **has read** the file"
- "they **have read** the documentation"
- "we **had read** it before"

### Pattern C — Modal + "be" + "read"

A modal auxiliary followed by "be" followed directly by "read" signals modal passive. The intermediate "be" is required — without it, modals + "read" signal the infinitive /riːd/ (see false positives below).

Triggers: `(can|could|will|would|shall|should|may|might|must) be read`

Examples:

- "it **can be read**"
- "this **should be read** carefully"
- "the output **will be read** by the parser"

---

## False positive risks

These are cases where substituting "red" would produce the wrong pronunciation. Any proposed regex must not fire on them.

### 1. Imperative / command form

"Read the file", "Read this carefully", "Read me the output" — imperative is present tense /riːd/. All three proposed patterns require a grammatical trigger word _before_ "read", so bare imperatives at the start of a sentence are safe. The risk would be if "read" appeared after a coincidental trigger — unlikely in practice.

### 2. Infinitive after modals (no "be")

"You should read this", "you must read the docs", "will read next" — modal + "read" without an intervening "be" is infinitive /riːd/, not passive. Pattern C specifically requires "be" between the modal and "read" to avoid this.

"I need to read it", "want to read" — the preposition "to" preceding "read" is also infinitive. Neither pattern A, B, nor C fires on "to read" because "to" is not in any trigger list.

### 3. Present tense / habitual

"I read books every day" — no auxiliary, no trigger. Safe.

"She reads / he reads" — different conjugation, "reads" not "read". Safe.

### 4. "read-only" and hyphenated compounds

In Python's `re` module, `\b` treats `-` as a non-word character, so `\bread\b` _does_ match "read" in "read-only" (the word boundary is between "d" and "-"). If a passive construction happened to precede "read-only" — "was read-only" — pattern A would fire and produce "red-only".

Mitigation: negative lookahead `(?!-)` after the "read" match. This prevents substitution when "read" is immediately followed by a hyphen.

### 5. Simple past without an auxiliary (ambiguous)

"I read it yesterday" — "read" here is past tense /rɛd/ but there is no auxiliary trigger in the immediate vicinity. This case is NOT caught by any proposed pattern.

This is intentional. Without an auxiliary, distinguishing simple past "read" from present "read" requires broader temporal context ("yesterday", "last week", "in 2020") or semantic analysis. A regex that attempts this would have an unacceptable false positive rate. Accepting the miss is safer than substituting incorrectly.

### 6. "read" as a noun or adjective

"a good read", "an easy read" — noun form, /riːd/. No auxiliary precedes it in natural usage. Safe.

"a must-read article" — "must" precedes "read" but is hyphenated, so `\bmust\b` followed by whitespace does not match "must-" in "must-read". Safe.

### 7. Adverbs between auxiliary and "read"

"was recently read", "has already read", "had never read" — the proposed patterns require the auxiliary to be immediately adjacent to "read" (only whitespace between). These cases are NOT caught.

"was recently read" is a miss (incorrectly pronounced as present tense). "has already read" and "had never read" are also misses.

This is acceptable: inserting `\w+\s+` between auxiliary and "read" to allow one adverb would also catch false positives in complex sentences. The direct-adjacency constraint is the right tradeoff for a stateless regex rule.

---

## Proposed implementation

### Where in the pipeline

Inside `normalize()` in `src/wednesday_tts/normalize/pipeline.py`, immediately after the `apply_dictionary()` call at step 1. At that point:

- Code blocks have already been converted to speech form (so "read" inside code snippets is unlikely but possible — see constraints below)
- Markdown has NOT yet been stripped, so surrounding auxiliary words in prose are still intact
- The dictionary has already run, so any dictionary substitutions for other words have already happened

The substitution must run before step 1a (tilde expansion) but after step 1 (dictionary). It operates on prose text and is unaffected by later numeric/path rules.

Because `pre-tool-speak.py` imports `tts_normalize` directly from `speak-response.py`, any addition to `tts_normalize()` automatically applies to both the Stop hook and the PreToolUse hook with no additional changes.

### The regex substitution

```python
def fix_read_homograph(text):
    """
    Substitute past-tense/passive 'read' → 'red' so TTS pronounces it /rɛd/.
    Only fires when a grammatical trigger (auxiliary verb) immediately precedes 'read'.
    Patterns:
      A: passive be-family:   was/were/is/are/am/be/been/being + read
      B: perfect have-family: has/have/had + read
      C: modal passive:       (modal) + be + read  [requires 'be', not just modal]
    Excludes 'read-only' and similar hyphenated compounds via (?!-) lookahead.
    """
    # Pattern A — passive voice
    text = re.sub(
        r'(?i)\b(was|were|is|are|am|be|been|being)(\s+)read\b(?!-)',
        r'\1\2red',
        text
    )
    # Pattern B — perfect aspect
    text = re.sub(
        r'(?i)\b(has|have|had)(\s+)read\b(?!-)',
        r'\1\2red',
        text
    )
    # Pattern C — modal passive (requires 'be' between modal and 'read')
    text = re.sub(
        r'(?i)\b(can|could|will|would|shall|should|may|might|must)(\s+be\s+)read\b(?!-)',
        r'\1\2red',
        text
    )
    return text
```

Call site in `tts_normalize()`:

```python
# 1. Apply custom dictionary (exact whole-word replacements)
text = apply_dictionary(text, dictionary)

# 1-homograph. Fix context-sensitive homographs
text = fix_read_homograph(text)

# 1a. Tilde (approximation): ~10 → "around 10", ...
text = re.sub(r'~\s*(\d)', r'around \1', text)
```

### Confidence level

**High** for patterns A and B in normal prose. The auxiliary-immediately-before-read constraint produces very few false positives and catches the majority of cases Claude generates (passive descriptions of what code does, perfect aspect references to prior actions).

**Medium** for pattern C (modal + be + read). The "be" requirement is strong but the variety of modal combinations is large and some edge cases may exist.

**Not addressed** (by design): simple past without auxiliary ("I read it"), past with intervening adverb ("was already read"), ambiguous present/past.

---

## Alternative approaches considered

### 1. LLM post-processing (TTS_USE_LLM=1 path)

`ollama_normalize()` already exists in the pipeline. A small model (qwen2.5:0.5b) could in principle resolve homographs by understanding full sentence context, including simple-past-without-auxiliary cases.

Rejected for this rule because: (a) the LLM path is opt-in and disabled by default; (b) qwen2.5:0.5b does not reliably perform grammatical disambiguation; (c) the regex approach covers the highest-frequency cases with zero latency overhead.

### 2. Lookbehind in a single regex

Python's `re` module requires fixed-width lookbehinds, so `(?<=was|were|been)` is not valid (variable width). Could use `(?:(?<=was )|(?<=were )|...)` but this becomes unwieldy for multi-word triggers like "been being" and breaks on multiple spaces. The three-pass capture-group approach is cleaner and more maintainable.

### 3. Extend tts-dictionary.json with phrase entries

The current dictionary supports only whole-word patterns via `\b` + `re.escape(pattern)`. Multi-word context patterns ("was read" → "was red") would require modifying `apply_dictionary()` to support phrase entries, changing the data format. This would be a larger change to core infrastructure for a single rule. Not worth it until phrase matching is needed by multiple rules.

### 4. "Already read" pattern

Adding `already\s+read\b` as a trigger catches some adverb-separated cases ("I've already read it" is caught by pattern B via "I've" → "have"; "already read" standalone could catch "already read the file"). Rejected because "already read the file" is too likely to be imperative in certain contexts ("Please already read the file before asking"). The gain is minimal and the false-positive risk is real.

---

## Recommendation

Implement `fix_read_homograph()` as described. Add it to `tts_normalize()` after `apply_dictionary()`. Run manually against a sample of recent Claude outputs to verify no surprising false positives before committing to the live pipeline.

Test cases to verify:

| Input                            | Expected output                      | Pattern          |
| -------------------------------- | ------------------------------------ | ---------------- |
| `"the file was read"`            | `"the file was red"`                 | A                |
| `"errors were read from stderr"` | `"errors were red from stderr"`      | A                |
| `"it is read line by line"`      | `"it is red line by line"`           | A                |
| `"to be read aloud"`             | `"to be red aloud"`                  | A                |
| `"she has read the file"`        | `"she has red the file"`             | B                |
| `"had read it before"`           | `"had red it before"`                | B                |
| `"can be read as JSON"`          | `"can be red as JSON"`               | C                |
| `"should be read carefully"`     | `"should be red carefully"`          | C                |
| `"read the file"` (imperative)   | `"read the file"` (unchanged)        | none             |
| `"to read a file"`               | `"to read a file"` (unchanged)       | none             |
| `"I read books daily"`           | `"I read books daily"` (unchanged)   | none             |
| `"you should read this"`         | `"you should read this"` (unchanged) | none             |
| `"read-only mode"`               | `"read-only mode"` (unchanged)       | none (lookahead) |
| `"was read-only before"`         | `"was read-only before"` (unchanged) | none (lookahead) |
| `"was recently read"`            | `"was recently read"` (known miss)   | none             |
