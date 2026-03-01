# Rule: Paths

**What it does**: Converts filesystem paths (tilde-home paths, slash-separated paths, filenames with extensions, and bare dotfile extensions) to spoken form.

**Pipeline position**: steps 4a–4b2 — run after URL rules (steps 2–3) and before time/unit abbreviations (step 4c). URL rules must run first so that `https://` paths are already gone before the generic slash splitter fires.

## Examples

| Input                   | Output                                                                           |
| ----------------------- | -------------------------------------------------------------------------------- |
| `~/.claude/hooks/`      | `tilde slash dot claude slash hooks`                                             |
| `~/dev/parent-repo` | `tilde slash dev slash parent-repo`                                          |
| `src/main.py`           | `src slash main dot py`                                                          |
| `error/status/config`   | `error slash status slash config`                                                |
| `tts-server.py`         | `tts-server dot py`                                                              |
| `config.json`           | `config dot jason` (after bare-ext rule, then dict would convert `json`→`jason`) |
| `.sh`                   | `dot shuh`                                                                       |
| `.py`                   | `dot pie`                                                                        |
| `.env`                  | `dot ee en vee`                                                                  |
| `.md`                   | `dot markdown`                                                                   |

## Pattern / logic

### 4a. Tilde paths

Pattern: `~/[^\s,;:!?\)]+`

`tilde_path_to_speech()` splits on `/` and for each part:

- `~` → `tilde`
- parts starting with `.` → `dot ` + remainder (stripping the leading `.`)
- other parts pass through unchanged

Parts are then joined with `slash`.

Trailing slashes are stripped before splitting (`path.rstrip('/')`).

### 4b. Generic slash-separated paths

Pattern: `\b\w[\w.-]*(?:/[\w.-]+)+/?`

Requires at least two slash-separated segments. Converts every `/` to `slash` in place. Trailing slashes stripped before replacement.

This is intentionally broad — it also catches alternative-style prose like `error/status/config`.

### 4b (second sub-rule). File extensions — `filename.ext`

Pattern: `\b([a-zA-Z0-9_-]+)\.(<EXT>)\b`

Where `EXT` is a fixed list of known extensions:
`py js ts jsx tsx json jsonl yaml yml md txt sh toml csv pdf html css svg png jpg mp3 mp4 wav db sqlite sql log lock env plist xml`

Replaces `filename.ext` with `filename dot ext`. The extension itself is left as its raw abbreviation here — bare-ext phonetics apply separately (see next sub-rule), and the dictionary may also convert the abbreviation (e.g., `json` → `jason`).

### 4b2. Bare dotfile extensions

Pattern: `(?<!\w)\.(<EXT_NAME_KEYS>)\b`

Requires no preceding word character (so it does not fire inside `main.py`).

Maps extensions to phonetic names via `EXT_NAMES`:

| Extension        | Spoken           |
| ---------------- | ---------------- |
| `.sh`            | `dot shuh`       |
| `.py`            | `dot pie`        |
| `.js`            | `dot jay ess`    |
| `.ts`            | `dot tee ess`    |
| `.jsx`           | `dot jay ess ex` |
| `.tsx`           | `dot tee ess ex` |
| `.md`            | `dot markdown`   |
| `.json`          | `dot jason`      |
| `.jsonl`         | `dot jason ell`  |
| `.yaml` / `.yml` | `dot yamel`      |
| `.toml`          | `dot toml`       |
| `.env`           | `dot ee en vee`  |
| `.log`           | `dot log`        |
| `.lock`          | `dot lock`       |
| `.txt`           | `dot text`       |
| `.csv`           | `dot C S V`      |
| `.sql`           | `dot S Q L`      |
| `.db`            | `dot dee bee`    |
| `.html`          | `dot H T M L`    |
| `.css`           | `dot C S S`      |
| `.xml`           | `dot X M L`      |
| `.plist`         | `dot pee list`   |

## Known edge cases / limitations

- The generic slash rule (4b) matches broadly: any `word/word/word` sequence, including things like `and/or`, `pass/fail`, `true/false`. This is accepted behaviour — the spoken form reads naturally.
- The file-extension rule (4b, filename form) only fires for the fixed `EXT` list. Unknown extensions (e.g., `.wasm`, `.proto`) pass through unchanged.
- The `EXT` list and `EXT_NAMES` dict are separate and slightly inconsistent: `EXT` contains more extensions than `EXT_NAMES` (e.g., `svg`, `png`, `jpg`, `mp3`, `mp4`, `wav`, `sqlite` are in `EXT` but not in `EXT_NAMES`). Those extensions get the `filename dot ext` treatment but the bare `.ext` form is not phonetically expanded.
- Tilde paths with special characters in directory names (spaces, ampersands) are not handled — the pattern stops at whitespace and punctuation.
- Absolute paths (`/usr/local/bin`) are not handled by step 4a or 4b — the generic slash rule (4b) requires the path to start with a word character (`\b\w`), which an absolute `/` path does not.

## Future improvements

- Add support for absolute paths starting with `/`.
- Unify `EXT` and `EXT_NAMES` so every known extension has a phonetic form.
- Add `.wasm`, `.proto`, `.rs`, `.go`, `.rb`, `.c`, `.h` to the extension lists.
