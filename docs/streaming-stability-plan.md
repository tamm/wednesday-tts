# Streaming Stability Plan

Status: **Active investigation** (Mar 2026)

## The Problem

The TTS daemon wedges after a short period of use. Symptoms:
- Audio works immediately after a fresh restart
- After some number of requests (sometimes 1, sometimes several), audio stops
- Daemon process stays alive, socket responds to PING, but no audio plays
- Logs show repeated `play_streaming hung after 30s` and `Broken pipe`
- Only fix is killing the process and letting launchd restart it

## History Note

wednesday-tts was migrated from `../parent-repo` repo in March 2026. Earlier streaming stability history (pre-migration) exists in that repo under `services/tts-server.py`. The git log in THIS repo only goes back to 2026-03-02 when the migration happened.

To see earlier streaming investigation history: check `parent-repo` git log:
```bash
cd ../parent-repo
git log -- services/tts-server.py
```

## What We Know

### Root cause: PortAudio write() blocks or errors

`out_stream.write()` in `pocket.py` is a blocking C-level call into PortAudio/CoreAudio.
When the audio subsystem is unhappy, this call either:
1. **Blocks forever** — never returns, holds `_streaming_lock`, cascading deadlock
2. **Throws PaErrorCode -9986** (paInternalError) — recoverable per-call, but indicates PortAudio state is broken

Both lead to the same outcome: no audio, daemon appears alive but is useless.

### What triggers the PortAudio failure

Not fully understood. Candidates:
- macOS sleep/wake cycle staling CoreAudio handles
- Bluetooth device disconnect/reconnect (Beats, AirPods)
- Audio device switch (speakers ↔ headphones)
- PortAudio internal state corruption after prolonged uptime
- Multiple rapid requests causing PortAudio resource contention
- The `sd._terminate() / sd._initialize()` cycle we do before each stream may itself be destabilising

### The cascade (before fixes)

1. `out_stream.write()` blocks or errors
2. `_streaming_lock` held by stuck thread — never released
3. Next request blocks on `_streaming_lock` forever
4. 30s daemon timeout fires, calls `abort_stream()` — but can't unblock C-level write
5. `requests_errored` was NOT incremented — watchdog blind
6. Audio health probe skipped because `_active_stream` was set
7. Threads pile up, all blocked, daemon appears alive but does nothing

## What We've Fixed So Far

### Fix 1: `_streaming_lock` timeout (pocket.py)
- Lock acquisition now has a 35s timeout
- If a previous stream is hung, the next request skips instead of blocking forever
- `try/finally` ensures the lock is always released

### Fix 2: Error stats for streaming path (daemon.py)
- `requests_errored` incremented when streaming times out
- `requests_completed` incremented on success
- Watchdog can now see hung streaming requests

### Fix 3: Health probe respects stream age (daemon.py)
- Health probe was skipping checks whenever `_active_stream` was set
- Now checks `_stream_start_time` — if stream has been active >60s, probe runs anyway
- 3 consecutive probe failures → process exit

### Fix 4: Crash on consecutive PortAudio failures (pocket.py)
- Track consecutive stream write errors and open failures
- After 3 consecutive failures → `os._exit(1)` so launchd restarts fresh
- Counter resets to 0 on any successful stream

### Fix 5: Daemon-level crash on repeated streaming hangs (daemon.py)
- If `requests_errored` hits 3, daemon exits for restart
- Catches the case where write() blocks (no exception) rather than errors

## What's Still Happening

After all fixes, the daemon STILL wedges after some requests. A restart always fixes it.
This suggests the fixes handle the *cascade* (lock deadlock, watchdog blindness) but not
the *root cause* (why PortAudio goes bad in the first place).

The crash-on-failure logic should make the daemon self-heal (crash → launchd restart → fresh
PortAudio → works again). If it's NOT crashing, the failure mode is one we haven't instrumented:
- `out_stream.write()` blocking at C level (no Python exception, no timeout within the write)
- The 30s thread join timeout fires, but the thread is still alive holding the lock
- Our lock timeout lets the next request skip, but there's still no audio

## What Else We Could Try

### A. Stop using streaming entirely (quick, nuclear option)
Disable `supports_streaming = True` on the pocket backend. All audio goes through the batch
path: `generate()` → `playback_queue` → `sd.play()`. This uses `sd.play()` which is
non-blocking and has its own watchdog in `_try_play()`. Streaming exists for lower latency
to first sound, but if it's the source of all crashes, batch mode is more stable.

**To test**: set `supports_streaming = False` on PocketTTSBackend, restart daemon.

### B. Use callback-mode OutputStream instead of blocking write
Replace `out_stream.write(data)` (blocking) with a callback-based OutputStream where we
fill a buffer and PortAudio pulls from it. The callback runs in a PortAudio thread, so our
Python thread never blocks on a C-level write. If PortAudio stalls, the callback just stops
being called — our thread stays free, the lock can be released, and we can detect the stall.

**Complexity**: Medium. Need to rewrite the streaming write loop.

### C. PortAudio reinit before every stream
Currently we do `sd._terminate() / sd._initialize()` before opening an OutputStream.
But maybe we need to be more aggressive — fully destroy and recreate the PortAudio context.
Or maybe the terminate/initialize itself is *causing* issues if done while another thread
still has a reference to PortAudio resources.

**To test**: Remove the terminate/initialize cycle, see if stability improves.

### D. Watchdog that monitors actual audio output
Instead of probing PortAudio with a test stream, monitor whether the daemon has
*successfully played audio recently*. If it's been >60s since the last successful
`play_streaming` completion, and there have been requests in that window, something is wrong.

### E. Move audio playback out of the streaming thread entirely
Generate audio in the streaming path but enqueue it for the playback worker (like batch
mode does). The playback worker uses `sd.play()` which is simpler and has proven more
reliable. Loses the streaming latency benefit but gains stability.

### F. Per-request process isolation
The most nuclear option: fork a child process for each play_streaming call. Child gets
its own PortAudio context. If it wedges, kill the child — parent is unaffected. Expensive
but completely isolates PortAudio failures.

## Fix 6: Skip-chunk-and-advance on streaming hang (daemon.py)

**Key insight from Tamm**: don't regenerate audio when streaming fails. The hook sends
only ONE request (SEQ:0 only) — there are no SEQ:1+ chunks being sent in parallel. When
streaming hangs and we advance `_next_seq` to 1, there's nothing queued and the user gets silence
(not "hears the rest"). The real fix for this is the hook detecting a dying daemon and falling
back to batch.

**Implementation (current, in daemon.py `handle_client`):**
1. Try streaming with a **5-second bail timeout** (was 30s)
2. If the streaming thread is still alive after 5s: abort the stream, increment
   `requests_errored`, advance `_next_seq` to 1 to mark that SEQ:0 is skipped
3. Mark daemon as "dying" (see Fix 7) — it will restart after finishing playback
4. The hook detects dying daemon via PING and falls back to batch (SPEED: mode)

**What was rejected:**
- Re-rendering the same text via batch path on streaming failure (wasteful, adds latency)
- 30s deferred `os._exit(1)` timer — responses can last several minutes, this would kill
  the daemon mid-speech

## Fix 7: Two-phase dying state (daemon.py)

**Problem**: we need to restart after PortAudio goes bad, but we can't kill the process
while audio is still queued/playing — that would cut off speech mid-sentence.

**Solution**: a `_dying` flag with health-probe-driven exit.

**How it works:**
1. When streaming fails, `_mark_dying()` sets `_dying = True` and records the timestamp
2. PING returns `"dying"` instead of `"ok"` — external monitors (hooks) can see the state
3. Health probe loop checks dying state each cycle:
   - If dying AND playback queue empty → `os._exit(1)` (safe to restart)
   - If dying AND grace period (180s) exceeded → force `os._exit(1)` (something else is stuck)
   - If dying but still playing → continue, let it finish
4. Hung-request watchdog also checks: if dying AND queue empty → exit
5. Health probe and watchdog both respect `playback_queue.empty()` — never kill mid-speech

**Helper function**: `_mark_dying()` avoids `global` inside nested functions (Python
SyntaxError). Same pattern as `_record_stream_failure()` in pocket.py.

## Fix 4 (revised): Crash on consecutive PortAudio failures (pocket.py)

Track consecutive stream write errors via `_record_stream_failure()` and
`_reset_stream_failures()` helper functions. After 3 in a row → `os._exit(1)`.
Counter resets to 0 on any successful stream. Covers both write errors (-9986)
and OutputStream open failures.

Helper functions extracted to module level to avoid `global` inside `with` blocks
(Python SyntaxError when `global` declaration follows use in same scope).

## Current State (6 Mar 2026)

### What's done (code written, not yet committed)
- daemon.py: skip-chunk-advance-seq streaming path, dying state mechanism, health probe
  dying checks, watchdog dying checks, all exit paths respect playback queue
- pocket.py: consecutive failure tracking with helper functions, lock timeout, stream
  start time tracking
- test_daemon_streaming.py: 13 tests covering all the above

### What's in progress
- **Two health probe tests are hanging** due to `_dying` flag leaking between tests.
  Fix identified: save/restore `daemon._dying` in each health probe test (same pattern
  already applied to watchdog test). The edit has been made but tests not yet re-run.

### What's next (in order)
1. Re-run streaming tests to confirm all 13 pass
2. Run full test suite to check for regressions
3. Restart daemon with latest code
4. Commit and push all changes
5. Smoke test — send a few TTS requests, verify audio plays

### What's NOT done yet (future work)
- Root cause of WHY PortAudio goes bad is still unknown
- If skip-chunk + dying-state still doesn't self-heal reliably:
  1. **Option B** — callback-mode OutputStream (avoids blocking write entirely)
  2. **Option F** — per-request child process (full PortAudio isolation)
  3. Investigate whether `sd._terminate() / sd._initialize()` cycle before each
     stream is itself destabilising PortAudio

## What We're Fixing Now

Hook-level fallback with daemon-side infrastructure:

1. **Hook-level PING before streaming** — check daemon health before sending SEQ:0
   - If daemon returns `"dying"`, skip streaming and use SPEED: (batch) instead
   - Graceful degradation: user gets full audio from batch, no silence

2. **Streaming circuit-breaker** — disable streaming at runtime on repeated failure
   - Don't kill daemon, just mark it "dying" and let it self-heal
   - Hook detects this and switches to batch automatically
   - Daemon restarts cleanly when queue is empty

3. **Health probe refinement** — kills only streaming path, not daemon, unless batch is also broken
   - Single probe failure on streaming path → disable streaming but keep batch running
   - Three consecutive failures → process exit (daemon is genuinely broken)

4. **Callback-mode OutputStream** — replaces blocking write() loop
   - Python thread never blocks on C-level call
   - PortAudio pulls data via callback; if stalled, callback just stops being called
   - Thread stays free, lock can be released, stall is detectable

## Tamm's Decisions (6 Mar 2026)

These decisions override earlier approaches documented above. They define the target architecture.

### 1. Hook sends SEQ:0 blindly — no upfront PING

The hook does NOT ping the daemon before sending its request. It sends `SEQ:0:...` and expects a response. If no response arrives within 10 seconds, THEN it pings to diagnose what's wrong. The daemon is responsible for all fallback behaviour internally. This removes the PING-before-send round trip and the `dying`/`stream-disabled` response handling from the hook.

### 2. Streaming failure = escape to batch for THAT chunk only

When streaming fails on a given request, the daemon falls back to batch rendering for that specific chunk. It does NOT permanently disable streaming for all future requests. The `_streaming_disabled` flag is architecturally wrong — it turns a transient failure into a permanent degradation. Must be removed. Each request gets a fresh chance at streaming.

### 3. Streaming MUST route through the playback queue

`play_streaming()` must NOT call `sd.play()` or open its own `OutputStream` directly. All audio output goes through `playback_queue` and `playback_worker`. The playback_worker is the singleton audio output path — nothing else touches sounddevice for playback. This eliminates the root cause of audio glitching (two voices at once) and the fragile coordination between streaming and batch paths.

### 4. UserPromptSubmit hook MUST send STOP

When the user submits a new prompt, the hook sends STOP to kill any playing audio. This is critical UX — it's how Tamm silences speech during video calls or when they want to interrupt. This was accidentally broken and must be restored. Without it, previous audio plays over the new response.

### 5. SPEED: legacy command to be removed

The `SPEED:speed:text` command has no remaining use cases. It was used as a fallback when the hook detected a dying daemon, but with decision #1 (no upfront PING) and decision #2 (per-chunk fallback), there's no need for a separate unsequenced render path. Remove it from the daemon.

### 6. _dying flag and _mark_dying() to be removed

The daemon does not need to flag itself as dying on streaming failure. With decision #2 (per-chunk batch fallback), a streaming failure is handled locally within that request. The daemon stays healthy and continues accepting requests normally. Remove `_dying`, `_dying_since`, `_DYING_GRACE_S`, `_mark_dying()`, and all code that checks or sets them.

### 7. Audio glitching root cause identified

The "two voices at once" glitch happens because `play_streaming()` opens its own `OutputStream` and plays audio directly, while `playback_worker` may also be calling `sd.play()` on queued items. Two concurrent PortAudio outputs produce garbled overlapping audio. Decision #3 (queue-only playback) eliminates this entirely.

## Test Coverage

13 tests in `tests/test_daemon_streaming.py` covering:
- Lock release after normal completion, exception, and abort
- abort_stream clears state correctly
- Streaming timeout increments requests_errored
- Successful streaming increments requests_completed
- Streaming timeout skips chunk and advances _next_seq to 1
- Watchdog detects prolonged hangs (with _dying flag isolation)
- Health probe runs when no active stream
- Health probe skips during short active stream
- Second request not blocked by first timeout (cascading failure prevention)
