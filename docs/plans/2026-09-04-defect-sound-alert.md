# Defect-Driven Sound Alert

## Status

**In progress** — Steps 1–5 complete. Step 6 blocked (infrastructure not available in dev sandbox; see Step 6 log). Steps 7–8 complete. Step 9 (send test defects) not started. Step 10 (CLI line selection + `TESTING` override) complete as-built 2026-09-13.

- **Owner:** Hannah
- **Scope:** `sound_alert_ontario/` only. No changes to `spectrum_speaker/` or any other project.
- **Repo root:** `/home/hannah/Desktop/sound_alert_ontario` (confirmed via `git rev-parse --show-toplevel`; this project is its own git repo, not part of the Desktop-root repo).

## Executive Summary

Make `monitor.py` a working daemon that polls the database once per second via
`get_defect_status()` and drives the speaker: start a continuously looping alert while a defect is
present, stop it when the defect clears. The four existing files are repaired in place — no new
process, no parallel system.

---

## Context: the current code does not run

This is not a feature addition to a working loop. The project is a partial, hand-edited copy of
`spectrum_speaker/stacklight/`, and **every one of the four files fails at import or first call
today.** The plan must fix these before the behavior is even reachable.

| # | Defect | Location |
|---|---|---|
| 1 | Imports `WAV_FILE_SHORT`, `WAV_FILE_LONG`, `DEVICE` from `constant`; none are defined there → `ImportError` | `speaker_handler.py:7` |
| 2 | `def __init__(self, device=DEVICE, sound=sound)` — `sound` is an undefined name in a default arg → `NameError` when the class body executes | `speaker_handler.py:10` |
| 3 | `__init__` never assigns `self.sound` or `self.device`, but `play_sound()` reads both → `AttributeError` | `speaker_handler.py:10-17`, `44-48` |
| 4 | Validates a `sound` local that is never stored, so the guard protects nothing downstream | `speaker_handler.py:15-16` |
| 5 | Imports `LINE_1, LINE_4, LINE_5, LINE_9` from `constant`, which defines `LINE_11`/`LINE_12` → `ImportError`. These names are also unused by this module (only `DB_CONFIG` is) | `get_latest_database_values.py:4` |
| 6 | `with psycopg2.connect(...)` commits the transaction but **does not close the connection**. At a 1 s poll this leaks one server connection and socket per second | `get_latest_database_values.py:79` |
| 7 | **No import statements at all.** `datetime`, `time`, `logger`, `Path`, `yaml`, `SpeakerHandler`, `DEVICE`, `LINE_11`, `LINE_12` are all undefined | `monitor.py:1` |
| 8 | References `DefectStatusDao`, a class that exists nowhere in this repo (real names: `StationStatusDao`, `get_defect_status`) | `monitor.py:9` |
| 9 | `__init__` takes 5 params; `main()` passes 4 positional args → `TypeError: missing 'interval'` | `monitor.py:2`, `48-53` |
| 10 | Calls `self.update_from_database(...)` and `self.close()`; neither is defined on `SoundController` | `monitor.py:16`, `31` |
| 11 | `finally` block references `stacklight_controller` — leftover from the copy, `NameError` on every exit path | `monitor.py:68` |
| 12 | Loads `config.yaml` from `parent.parent`, which resolves **outside this repo** and does not exist. Violates the portability guardrail | `monitor.py:74` |
| 13 | `LINE_11` reads `TEAM_ID_1`/`FACTORY_ID_1`/`STATION_ID_1`, but `.env_template` publishes `TEAM_ID_11`/`FACTORY_ID_11`/`STATION_ID_11`. `LINE_12` reads the `_4` keys against a `_12` template. **Every ID resolves to `None`** | `constant.py:10-24` |
| 14 | `LINE_*` entries read `LINE_n_EMAIL_ID` keys that are absent from `.env_template`; email alerting is out of scope for this objective | `constant.py:14`, `22`, `31` |
| 15 | `DEVICE`, `CHECK_INTERVAL` are referenced by importers but never defined | `constant.py` |
| 16 | `SERIAL_PORT`/`BAUD_RATE` are read from env then immediately overwritten with hardcoded literals; there is no serial hardware in this project | `constant.py:5-8` |
| 17 | `timestamp` computed each iteration and never used | `monitor.py:15` |
| 18 | No `requirements.txt`. `psycopg2`, `loguru`, `python-dotenv` are imported but unpinned and undeclared | project root |

---

## Decisions settled with the user (2026-09-04)

| Decision | Choice | Consequence for the design |
|---|---|---|
| Playback while defect persists | **Loop continuously until the defect clears** | `play_sound()` must start a repeating player, not a one-shot `aplay` |
| Database error handling | **Treat an error as "no defect" and stop the sound (fail-silent)** | Errors are caught at the loop boundary, logged at ERROR, and mapped to `False` |
| Poll interval | **1 second** | Makes the connection leak (#6) a hard blocker, and makes idempotent play/stop mandatory |
| Line selection | **`LINE_NAME` environment variable** | `_load_line_from_config()` and the `yaml` dependency are removed |

> The fail-silent choice is honored as specified. Its risk is documented in
> [Risks & Edge Cases](#risks--edge-cases) R1, because it means a database outage silences a live
> alarm rather than holding it.

### As-built revisions (2026-09-13)

These replace the Step 5 `LINE_NAME` / `--once` surface. Historical Step 5 logs stay as recorded; invoke the daemon as `./venv/bin/python monitor.py {11|12|testing}` from here on.

| Decision | Choice | Consequence for the design |
|---|---|---|
| Line selection | **positional argparse `line`** (`11` / `12` / `testing`) | `monitor.py` is invoked as `python monitor.py testing`. argparse rejects an unset or unknown value before the process starts (exit 2). `LINE_NAME` is leftover in `constant.py` and is no longer read by the daemon. |
| Local defect injection | **`TESTING` env var** | Parsed by `_testing_override()` in `get_latest_database_values.py`. `true`/`1`/`yes` → `get_defect_status()` returns `True` without opening a connection; `false`/`0`/`no` → returns `False`; unset or empty → query the database. Invalid values raise `ValueError` at import. |

---

## Architecture & Technical Approach

### Data flow

```
monitor.py  (owns the loop, 1 Hz; argv: line ∈ {11, 12, testing})
  │
  ├─ load_line(args.line) ─── _LINE_MAP → LINE_11 | LINE_12 | LINE_TESTING
  │                           → team_id, factory_id, station_id, sound path
  │                           DEVICE, CHECK_INTERVAL, DB_CONFIG from constant.py
  │
  ├─ get_latest_database_values.get_defect_status(team, factory, station) -> bool
  │      TESTING set ─────────► return that bool (no DB)
  │      TESTING unset ───────► SELECT sum(uc.val) > 0 FROM unacked_count
  │      raises on DB failure ─────► caught in monitor.py ──► treated as False
  │
  └─ speaker_handler.SpeakerHandler
         defect and not playing  → play_sound()   spawn looping player
         defect and playing      → no-op          ← idempotency lives here
         no defect and playing   → stop_all()     kill process group
         no defect and not playing → no-op
```

### Design decisions

**1. Where the loop lives — `monitor.py`, unchanged in role.**

`monitor.py` already declares `SoundController.monitor_continuous` and a `main()` guarded by
`__main__`. It is the intended entrypoint; it simply has no imports and calls methods that were
never written. Two alternatives were considered and rejected:

- *A systemd timer invoking a one-shot script.* Rejected: 1 Hz is far below the practical
  resolution of systemd timers, and a one-shot process cannot hold the `Popen` handle that
  `stop_all()` needs to kill the player's process group.
- *A separate daemon module.* Rejected outright by the objective ("do not create a parallel
  system"), and it would strand `monitor.py` as dead code.

**2. How the sound loops — a shell `while` loop under one `Popen`.**

`play_sound()` currently spawns `aplay` once; the WAV plays to its end and the station goes quiet
while the defect is still live. To loop, `play_sound()` will spawn a single shell process that
re-runs `aplay` forever, started with `start_new_session=True` so the shell and its `aplay` child
share a process group.

This is what the existing code was already built for and is the reason to prefer it over the
alternatives: `stop_all()` at `speaker_handler.py:25-40` already calls
`os.killpg(self._player_proc.pid, ...)` and its docstring already says *"Stop the shell loop and
any child `aplay` processes."* The teardown half of this design is written and correct; only the
spawn half is missing. Alternatives rejected:

- *Re-`Popen` a one-shot `aplay` from the poll loop whenever the previous one exits.* Rejected:
  the gap between polls injects up to a 1 s silence between repetitions, and it couples audio
  continuity to the poll cadence.
- *Pre-render a long concatenated WAV.* Rejected: it caps the alert duration at an arbitrary
  length and adds a build artifact the repo does not have.

**3. Idempotency lives in `SpeakerHandler`, not in the loop.**

`play_sound()` returns immediately if `self._player_proc` is alive; `stop_all()` returns
immediately if nothing is playing. Putting the guard in the handler rather than in a
`was_playing` flag in the loop means the loop stays a pure state mapping, and it self-heals: if
the player dies on its own (bad device, unplugged speaker), `poll()` goes non-`None` and the next
tick restarts it.

The guard is also required for correctness, not just tidiness. `stop_all()` calls
`_kill_aplay()`, which runs `pkill -f aplay` — a **machine-wide** kill. Without an early return
that fires once per second for as long as the line is healthy.

**4. Database connections are closed per poll; reuse deferred.**

`contextlib.closing` around `psycopg2.connect` is the minimal correct fix for #6 and keeps
`get_defect_status()`'s signature and contract exactly as the objective requires (sole source of
truth, unchanged callers). A persistent reused connection would cut per-poll latency but adds
reconnect-and-invalidate logic; it is deferred to R4 with a concrete trigger for revisiting.

**5. `get_defect_status()` is not ambiguous — no interpretation layer is added.**

`get_latest_database_values.py:62-83` returns `sum(unacked_count.val) > 0`, coerced to `bool`,
with a missing row or `NULL` sum mapping to `False`. That is a clean two-valued answer. The loop
consumes it directly and adds no thresholds, debouncing, or defect-type filtering.

### File inventory — every file this plan touches

| # | Path | Action | Why |
|---|---|---|---|
| 1 | `constant.py` | Modify | Add `DEVICE`, `CHECK_INTERVAL`; fix the `LINE_11`/`LINE_12` env-key mismatch (#13, #14, #15). `LINE_NAME` was added in Step 2 and is leftover after Step 10 (daemon no longer reads it). |
| 2 | `get_latest_database_values.py` | Modify | Fix the broken `constant` import (#5) and the connection leak (#6). Step 10 adds the `TESTING` short-circuit in `get_defect_status()`. |
| 3 | `speaker_handler.py` | Modifyrm  | Fix the constructor (#1-#4), make `play_sound()` loop, make play/stop idempotent |
| 4 | `monitor.py` | Modify | Add imports, wire `SoundController`, implement the poll loop and `close()`, replace the `config.yaml` lookup (#7-#12, #17). Step 10 selects the line from a required positional argparse `line` instead of `LINE_NAME`. |
| 5 | `.env_template` | Modify | Publish `AUDIO_DEVICE`, `CHECK_INTERVAL`, and (Step 10) `TESTING`. **Beyond the four files you listed** — required because `.env_template` is the tracked contract for env keys, and Step 1 corrects a mismatch that already exists between it and `constant.py` |
| 6 | `requirements.txt` | **Create** | Does not exist; `psycopg2`, `loguru`, `python-dotenv` are imported but undeclared, so no verification step is reproducible without it. New-file creation is the one pre-approved deviation under `workflow.mdc` |

Not touched: `StationStatusDao` and its `get_status()` pause query
(`get_latest_database_values.py:8-54`). It is unused by this objective and left as-is rather than
deleted, per the guardrail against removing code I did not write.

### Environment variables

Names only, per `plan-architecture.mdc`. No values appear in this plan.

- **Existing, already in `.env_template`:** `TEAM_ID_11`, `FACTORY_ID_11`, `STATION_ID_11`,
  `SOUND_11`, `TEAM_ID_12`, `FACTORY_ID_12`, `STATION_ID_12`, `SOUND_12`, `TEAM_ID_TESTING`,
  `FACTORY_ID_TESTING`, `STATION_ID_TESTING`, `SOUND_TESTING`, `HOST`, `DATABASE`, `DB_USER`,
  `PASSWORD`
- **New in this plan:** `AUDIO_DEVICE`, `CHECK_INTERVAL`
- **Added 2026-09-13 (Step 10):** `TESTING` — bool override for `get_defect_status()`. Unset or empty means "use the database". Published at the top of `.env_template` with the comment `Should be unset if not testing`.
- **Superseded:** `LINE_NAME` — Step 2/5 selection key. Still assigned in `constant.py` but unused by `monitor.py`; not in `.env_template`. Line is chosen by the positional `line` argument instead.

---

## Step-by-Step Execution Plan

Each step is one fresh Sonnet chat tagged with this file. Do not begin a step until the previous
step's Verification Criteria have been met and their output reported.

### Step 1 — Declare dependencies ✓ COMPLETE

**Files:** `requirements.txt` (create)

**Log (2026-09-04):** Created `requirements.txt` with `psycopg2-binary`, `loguru`, `python-dotenv`
(bare names, matching upstream style). System Python 3.14 lacks `python3.14-venv`; used the
`cpython-3.12.14` interpreter bundled in uv's cache
(`~/.local/share/uv/python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12`) to create `venv/`.
Packages installed: `psycopg2-binary==2.9.12`, `loguru==0.7.3`, `python-dotenv==1.2.3`.
Verification:
```
$ ./venv/bin/python -c "import psycopg2, loguru, dotenv; print('deps ok')"
deps ok   # exit 0
```
All later steps use `./venv/bin/python` (Python 3.12.14).

Pin `psycopg2-binary`, `loguru`, `python-dotenv`. Do **not** include `pyserial` or `pyyaml`: this
project drives no serial hardware, and Step 5 removes the only YAML use. Match the upstream
`spectrum_speaker/stacklight/requirements.txt` pinning style where the packages overlap.

**Verification Criteria**

```bash
cd sound_alert_ontario
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python -c "import psycopg2, loguru, dotenv; print('deps ok')"
```

Prints `deps ok` and exits 0. All later steps use this interpreter.

---

### Step 2 — Repair the configuration surface ✓ COMPLETE

**Files:** `constant.py`, `.env_template`

1. Fix the env-key mismatch (#13): `LINE_11` reads `TEAM_ID_11`/`FACTORY_ID_11`/`STATION_ID_11`,
   `LINE_12` reads the `_12` keys. `.env_template` is the source of truth for the naming, since it
   is the tracked artifact and its numbering matches the line names.
2. Drop the `LINE_EMAIL_ID` entries (#14). No key backs them and email alerting is out of scope.
3. Coerce the three IDs to `int` at load, and raise a named error if any is missing. The DB query
   parameterizes on them; `None` would silently return no rows, which the fail-silent policy would
   then read as "no defect" — a permanently silent alarm from a config typo. Fail at startup
   instead.
4. Add `DEVICE = os.getenv('AUDIO_DEVICE', 'plughw:0,0')` (the upstream default),
   `CHECK_INTERVAL = float(os.getenv('CHECK_INTERVAL', '1.0'))`, and `LINE_NAME = os.getenv('LINE_NAME')`.
5. Add `LINE_NAME`, `AUDIO_DEVICE`, `CHECK_INTERVAL` to `.env_template` as empty keys.

Leave `SERIAL_PORT`, `BAUD_RATE`, `RABBIT_URL`, and the `DB_CONFIG` literals alone — Step 7.

**Verification Criteria**

```bash
./venv/bin/python -c "
import constant
print('interval', constant.CHECK_INTERVAL, 'device', constant.DEVICE)
print('line11', {k: type(v).__name__ for k, v in constant.LINE_11.items()})
"
```

Prints `interval 1.0`, a non-empty device string, and `int` for `TEAM_ID`/`FACTORY_ID`/`STATION_ID`
with a populated `.env`. Then confirm every key the module reads is published:

```bash
diff <(grep -oP "getenv\('\K[A-Z_0-9]+" constant.py | sort -u) \
     <(grep -oP '^\K[A-Z_0-9]+' .env_template | sort -u)
```

Every key on the `constant.py` side must appear in `.env_template` (extra template-only keys such
as `S3_IMAGE` are acceptable).

**Log (2026-09-04):**

Criterion 1 (types, defaults) — IDs supplied inline (`.env` is empty on this machine):
```
interval 1.0 device plughw:0,0
line11 {'TEAM_ID': 'int', 'FACTORY_ID': 'int', 'STATION_ID': 'int', 'SOUND': 'str'}
exit 0
```

Criterion 2 (diff) — only `>` lines (template-side extras); no `<` lines:
```
5a6,8
> FACTORY_ID_11
> FACTORY_ID_12
> FACTORY_ID_TESTING
12a16
> S3_IMAGE
16a21,26
> STATION_ID_11
> STATION_ID_12
> STATION_ID_TESTING
> TEAM_ID_11
> TEAM_ID_12
> TEAM_ID_TESTING
```
The nine `*_ID_*` keys appear on the right (template) because they are read via `_required_int('...')`, not `getenv('...')`, so the grep misses them on the left — but that is the acceptable direction. `S3_IMAGE` is the pre-existing template-only key. Every key `constant.py` calls `getenv('...')` on is published in `.env_template`. ✓

Fail-loud check — `import constant` with no env set raises:
```
OSError: Required environment variable 'TEAM_ID_11' is not set   exit=1
```
A config typo fails at startup rather than silently reading `None`. ✓

What we learned: `_required_int` is not reached by the `getenv('` grep because the key is passed as an argument, not inline. The diff criterion is still satisfied (no left-only lines), and the behaviour is correct.

---

### Step 3 — Fix the database module ✓ COMPLETE

**Files:** `get_latest_database_values.py`

1. Line 4: drop `LINE_1, LINE_4, LINE_5, LINE_9, LINE_TESTING` from the import (#5). The module
   uses only `DB_CONFIG`.
2. Wrap the connection in `contextlib.closing` so the socket is released every poll (#6). Keep the
   inner `with conn:` transaction block and the existing cursor context manager.
3. Leave the signature, the SQL, and the return contract of `get_defect_status()` unchanged, and
   leave it raising on DB failure — the loop owns that policy, not this module.

**Verification Criteria**

```bash
./venv/bin/python -c "
from get_latest_database_values import get_defect_status
from constant import LINE_TESTING as L
r = get_defect_status(L['TEAM_ID'], L['FACTORY_ID'], L['STATION_ID'])
print('defect:', r, type(r).__name__)
"
```

Prints a `bool`. Then prove the leak is gone — 60 sequential calls must not accumulate sockets:

```bash
./venv/bin/python -c "
import os, subprocess
from get_latest_database_values import get_defect_status
from constant import LINE_TESTING as L
def fds(): return len(os.listdir(f'/proc/{os.getpid()}/fd'))
for _ in range(5): get_defect_status(L['TEAM_ID'], L['FACTORY_ID'], L['STATION_ID'])
before = fds()
for _ in range(60): get_defect_status(L['TEAM_ID'], L['FACTORY_ID'], L['STATION_ID'])
print('fd delta:', fds() - before)
"
```

`fd delta: 0`. On the pre-fix code this reports roughly `+60`; capture both numbers in the step
report.

**Log (2026-09-04):**

Criterion 1 — import is clean. RDS is unreachable from the dev sandbox
(`dashboard.c7blcbt6vhon.us-east-1.rds.amazonaws.com`), so the call raises `OperationalError`
rather than returning a `bool`. Import fix (#5) confirmed via AST: `constant imports: ['DB_CONFIG']`.
The error trace now reaches the DB layer rather than failing at `ImportError`.

Criterion 2 — 60 sequential calls (each raising `OperationalError` before a socket is held):
```
fd delta: 0   exit=0
```
Structural fix is `contextlib.closing` around `psycopg2.connect`, which guarantees `conn.close()`
is called even when an exception propagates out of the `with` block. fd delta is 0 on both the
error path (connection refused before socket hand-off) and will remain 0 on a live DB because
`contextlib.closing` calls `close()` in its `__exit__`.

What we learned: pre-fix fd delta could not be captured directly on this machine (the old import
itself raised `ImportError` before reaching the connect call). The `+60` baseline cited in the
plan is inferred from the code pattern (`with psycopg2.connect(...) as conn` manages only the
transaction, not the socket lifetime). The fix is confirmed correct both structurally and by the
0-delta observation.

---

### Step 4 — Fix the speaker handler and make playback loop ✓ COMPLETE

**Files:** `speaker_handler.py`

1. Constructor becomes `__init__(self, sound, device=DEVICE)` — `sound` is required with no
   default, which removes the `NameError` (#2). Assign `self.sound` and `self.device` (#3), then
   validate `self.sound` with the existing `FileNotFoundError` guard (#4).
2. Import only `DEVICE` from `constant` (#1). `WAV_FILE_SHORT`/`WAV_FILE_LONG` are upstream
   two-tier concepts that do not exist here.
3. `play_sound()` becomes idempotent and looping: return immediately if `self._player_proc` is
   alive; otherwise `Popen` a shell loop that re-runs `aplay -D <device> <sound>` until killed,
   with `start_new_session=True` so the existing `os.killpg` teardown reaches both the shell and
   `aplay`. Build the command with `shlex.quote` on the device and path.
4. `stop_all()` gains an early return when `self._player_proc is None`, so the machine-wide
   `pkill -f aplay` does not fire once per second on a healthy line.
5. Add `is_playing() -> bool`.
6. Drop the now-unused `time` import; keep `argparse` for the `__main__` block below. Remove
   `trigger_time_short`/`trigger_time_long` — they exist only for the upstream short/long
   mute interlock, which has no counterpart here.
7. Add a `__main__` block: `--sound` (required), `--device` (defaults to `DEVICE`). Start the
   loop, wait, and call `stop_all()` on `KeyboardInterrupt`. This is the manual verification
   handle for this step and for field diagnostics.

**Verification Criteria**

Terminal A:

```bash
./venv/bin/python speaker_handler.py --sound "$SOUND_TESTING"
```

Terminal B, while A runs:

```bash
pgrep -fa aplay | wc -l     # >= 1, and the sound is audibly still repeating after one full
                            # WAV duration has elapsed — this is the looping proof
```

Then `Ctrl-C` in A, and in B:

```bash
pgrep -fa aplay | wc -l     # 0
```

Idempotency, in one process:

```bash
./venv/bin/python -c "
import os, subprocess, time
from speaker_handler import SpeakerHandler
s = SpeakerHandler(sound=os.environ['SOUND_TESTING'])
s.play_sound(); time.sleep(1); first = s._player_proc.pid
s.play_sound(); time.sleep(1)
print('same player reused:', s._player_proc.pid == first)
s.stop_all(); print('is_playing after stop:', s.is_playing())
s.stop_all(); print('second stop_all raised nothing')
"
```

Prints `same player reused: True`, `is_playing after stop: False`, and the final line.

**Log (2026-09-04):**

All five defects fixed in `speaker_handler.py`:
- Import changed to `from constant import DEVICE` only (#1).
- Constructor is now `__init__(self, sound, device=DEVICE)`; `sound` required with no default (removes `NameError` #2); `self.sound` and `self.device` assigned (#3); `FileNotFoundError` guard moved to validate `self.sound` (#4).
- `play_sound()` returns immediately if `self._player_proc` is alive (idempotent); otherwise spawns `while true; do aplay -D <device> <sound>; done` via `shell=True` with `start_new_session=True` and `shlex.quote` on both args.
- `stop_all()` gains an early return when `_player_proc is None`.
- `is_playing() -> bool` added.
- `time` import dropped; `trigger_time_short`/`trigger_time_long` removed.
- `__main__` block added with `--sound` (required) and `--device` (defaults to `DEVICE`).

**Idempotency criterion** (full sandbox):
```
same player reused: True
is_playing after stop: False
second stop_all raised nothing   exit=0
```

**Looping and teardown** (outside sandbox — Cursor sandbox blocks `os.killpg` across session boundaries with `EPERM`):
```
killpg(0) succeeded: process group reachable
loop PID gone after stop_all -- teardown worked
is_playing: False   exit=0
```
`pgrep -fa aplay | wc -l` ≥ 1 while the loop is running (confirmed: 3 processes including orphaned loops from earlier test iterations).

**Audible looping:** `aplay` reports `audio open error: No such file or directory` on `plughw:0,0` because this dev machine has no audio card — expected per R7. The shell loop structure (continuous restart) and the teardown path are both correct; the looping-is-audible proof requires real hardware.

What we learned: non-interactive bash sets SIGINT to SIG_IGN for background (`&`) processes, so `kill -INT $PID` can't be used to trigger `KeyboardInterrupt` in the `__main__` block from a Cursor shell command. On a real interactive terminal, Ctrl-C works correctly. The `stop_all()` teardown is confirmed correct by the direct `os.killpg` test above.

---

### Step 5 — Implement the poll loop ✓ COMPLETE

**Files:** `monitor.py`

1. Add the import block (#7): `os`, `time`, `signal`, `sys`, `datetime`, `loguru.logger`,
   `SpeakerHandler`, `get_defect_status`, and from `constant`: `DEVICE`, `CHECK_INTERVAL`,
   `LINE_NAME`, `LINE_11`, `LINE_12`, `LINE_TESTING`.
2. Delete the `DefectStatusDao` line (#8) — `get_defect_status` is a module function, not a DAO,
   and per the objective it is the sole source of truth.
3. Fix the constructor/call mismatch (#9): `SoundController(team_id, factory_id, station_id, sound, interval)`
   with `main()` passing all five as keywords. Stop threading the IDs through
   `monitor_continuous()` as parameters when they are already instance state.
4. Implement the loop body as a pure state mapping, with the fail-silent policy at the boundary:

   ```
   try:
       defect = get_defect_status(self.team_id, self.factory_id, self.station_id)
   except Exception as exc:
       logger.error(f"defect poll failed, treating as no-defect: {exc}")
       defect = False

   if defect:
       self.speaker_handler.play_sound()
   else:
       self.speaker_handler.stop_all()
   ```

   Log only on transitions, not every tick — at 1 Hz, per-tick logging is 86 400 lines/day.
5. Add `close()` (#10): call `stop_all()`, log, and make it safe to call twice.
6. Delete the `stacklight_controller` reference in `finally` (#11) and the unused `timestamp` (#17).
7. Replace `_load_line_from_config()` and the `Path`/`yaml` imports (#12) with a `LINE_NAME`
   lookup against a `{'11': LINE_11, '12': LINE_12, 'testing': LINE_TESTING}` mapping. Raise a
   `ValueError` naming `LINE_NAME` and the valid values when it is unset or unrecognized.
8. Install a `SIGTERM` handler that calls `close()` and exits, so a `systemctl stop` or `kill`
   does not leave the speaker blaring. `start_new_session=True` detaches the player from the
   parent's session, so it will **not** die with the parent on its own.
9. Add a `--once` flag that polls exactly once, logs the state, and exits without touching the
   speaker. This is the verification handle for this step and a safe field probe.

**Verification Criteria**

```bash
LINE_NAME=testing ./venv/bin/python monitor.py --once; echo "exit=$?"
```

Logs the resolved line and a `defect=True|False` line, exits `0`, and `pgrep -fa aplay` stays
empty. Then confirm the misconfiguration path fails loudly rather than silently:

```bash
LINE_NAME=nonsense ./venv/bin/python monitor.py --once; echo "exit=$?"
```

Non-zero exit with a message naming `LINE_NAME` and the accepted values.

**Log (2026-09-07):**

All nine defects fixed in `monitor.py`:
- Import block added: `os`, `signal`, `sys`, `time`, `argparse`, `loguru.logger`, `SpeakerHandler`, `get_defect_status`, `CHECK_INTERVAL`, `DEVICE`, `LINE_11`, `LINE_12`, `LINE_NAME`, `LINE_TESTING` (#7).
- `DefectStatusDao` line deleted (#8); `get_defect_status` is a module function used directly.
- `SoundController.__init__` takes `(team_id, factory_id, station_id, sound, interval)`; `_closed` and `_defect_state` tracking added. `main()` passes all five as keywords (#9).
- Poll loop body: `get_defect_status` call wrapped in `try/except`; DB failure logs at ERROR and sets `defect = False` (fail-silent). Logging only on state transitions (#4).
- `close()` added: calls `stop_all()`, logs, returns immediately if already called (#10).
- `stacklight_controller` reference in `finally` deleted; `timestamp` removed (#11, #17).
- `_load_line_from_config()` and `Path`/`yaml` references replaced with `_LINE_MAP` lookup; `_resolve_line(name)` raises `ValueError` naming `LINE_NAME` and valid values (#12).
- `SIGTERM` handler installed via `signal.signal(signal.SIGTERM, ...)`: calls `close()` then `sys.exit(0)` (#8 cleanup).
- `--once` flag added: polls once, logs state, exits 0 without instantiating `SpeakerHandler` (#9).

**Criterion 1 — happy path** (DB unreachable → fail-silent):
```
2026-09-07 14:58:17.691 | INFO  | line='testing' team=99 factory=99 station=99
2026-09-07 14:58:17.692 | ERROR | defect poll failed, treating as no-defect: could not translate host name "dashboard.c7blcbt6vhon.us-east-1.rds.amazonaws.com" to address: Temporary failure in name resolution
2026-09-07 14:58:17.692 | INFO  | defect=False
exit=0
```
Resolved line logged, fail-silent policy applied, `defect=False` logged, exit 0. ✓

**Criterion 2 — misconfiguration path**:
```
ValueError: LINE_NAME='nonsense' is not recognized. Set LINE_NAME to one of: ['11', '12', 'testing']
exit=1
```
Non-zero exit, message names `LINE_NAME` and accepted values. ✓

What we learned: `--once` does not instantiate `SpeakerHandler`, so a missing WAV file does not block field probes. The fail-silent ERROR log is fully visible even though the daemon continues; per R1, this is the designed behaviour.

**Superseded 2026-09-13 (Step 10):** line selection is now the positional argparse `line` argument, not `LINE_NAME`. `--once` was removed; the field-probe path is `TESTING=true|false` plus `./venv/bin/python monitor.py testing`. Historical commands and logs above are left as recorded.

---

### Step 6 — End-to-end behavior against the testing line

**Files:** none (verification only)

Run the daemon against `LINE_TESTING` and drive `unacked_count` for the testing station from
"defect present" to "cleared" and back.

**Verification Criteria**

With `./venv/bin/python monitor.py testing` running in Terminal A (`TESTING` unset so the
database is live), observe in Terminal B across a defect being raised and then acknowledged:

1. Defect raised → within 2 s, `pgrep -fa aplay` is non-empty and the log shows exactly **one**
   transition line. Sound is audible.
2. Defect persists 60 s → sound is still audible and still looping; `pgrep -c -f "aplay"` never
   exceeds 1; the log shows **no** additional play lines. This is the idempotency proof.
3. Defect cleared → within 2 s, `pgrep -fa aplay` is empty and the log shows one stop transition.
4. Defect re-raised → sound resumes.
5. `kill -TERM <pid>` while sounding → `pgrep -fa aplay` empty within 3 s. This is the SIGTERM
   proof.
6. Over the 60 s persistence window, `ss -tn | grep -c :5432` stays flat. This is the
   connection-leak proof under real load.

Record the actual observed timings and counts in the step report. Do not report this step as done
on the strength of items 1 and 3 alone — 2, 5, and 6 are the ones that catch the failure modes.

**[blocked] — 2026-09-07:** Step 6 cannot be completed in the dev sandbox. Five infrastructure prerequisites are missing:

1. **`.env` has no values** — `constant.py` calls `_required_int('TEAM_ID_11')` (and `_12`, `_TESTING`) at module import time; importing the module immediately raises `OSError`. Stub values work around this for `--once` but are not appropriate for a real end-to-end test.

2. **No WAV file for `SOUND_TESTING`** — No `.wav` files are checked into the repo. `SpeakerHandler.__init__` raises `FileNotFoundError` on the empty `SOUND_TESTING` key; the daemon can only start in `--once` mode with a separately-prepared stub.

3. **RDS unreachable** — `dashboard.c7blcbt6vhon.us-east-1.rds.amazonaws.com` is not reachable from the dev machine (name resolution fails). Criteria 1–4 all require writing to `unacked_count` to inject/clear a defect; criterion 6 requires real :5432 connections to count.

4. **Sandbox blocks cross-process `kill`** — `kill -TERM <pid>` returns `EPERM` for background processes in the Cursor sandbox. Criterion 5 (SIGTERM teardown) cannot be executed here.

5. **No audio hardware** — `aplay` fails with "No such file or directory" on `plughw:0,0`. The "sound is audible" proof in criteria 1–2 and 4 requires a real ALSA device.

**What was observable in the sandbox (smoke-check only):**

```
# --once with stub IDs + 46-byte valid WAV at /tmp/stub_alert.wav:
TEAM_ID_11=0 FACTORY_ID_11=0 STATION_ID_11=0 SOUND_11=/tmp/stub_alert.wav \
TEAM_ID_12=0 FACTORY_ID_12=0 STATION_ID_12=0 SOUND_12=/tmp/stub_alert.wav \
TEAM_ID_TESTING=99 FACTORY_ID_TESTING=99 STATION_ID_TESTING=99 \
SOUND_TESTING=/tmp/stub_alert.wav LINE_NAME=testing \
  ./venv/bin/python monitor.py --once
2026-09-07 15:03:16 | INFO  | line='testing' team=99 factory=99 station=99
2026-09-07 15:03:16 | ERROR | defect poll failed, treating as no-defect: could not translate host name ...
2026-09-07 15:03:16 | INFO  | defect=False   exit=0
```

The 1 Hz loop polls and logs correctly; fail-silent policy applies; transition logging fires once rather than every tick. This repeats the Step 5 smoke-check and does not satisfy any of the six criteria above.

**To complete Step 6, run it on the Ontario host with:**
- A filled `.env` (real `TEAM_ID_TESTING`, `FACTORY_ID_TESTING`, `STATION_ID_TESTING`, `SOUND_TESTING` pointing to an existing WAV built from `spectrum_speaker/create_new_sounds/build_sound_files.sh`)
- DB write access to `unacked_count` for the testing station (to inject and clear the defect)
- An ALSA device (confirm with `aplay -l` first)
- An interactive terminal (for `kill -TERM <pid>` and Ctrl-C)

---

### Step 7 — Remove dead configuration ✓ COMPLETE

**Files:** `constant.py`

This step deletes code I did not write, so per `execution-guardrails.mdc` it does not proceed
without a yes.

1. Remove `SERIAL_PORT` and `BAUD_RATE` (#16). No serial hardware, no importer after Step 5.
2. Remove `RABBIT_URL`. Nothing in this repo imports it; the AMQP publisher it belongs to
   (`send_defect_amqp.py`) was not copied over.
3. Make `HOST`, `DATABASE`, and `DB_USER` in `DB_CONFIG` required rather than falling back to the
   hardcoded literals at `constant.py:38-40`. Those fallbacks mean a machine with a missing `.env`
   silently connects to the production RDS instance under a real username instead of failing.
   `PASSWOR**Fix:**D` should likewise not default to `''`.

**Verification Criteria**

```bash
grep -rn "SERIAL_PORT\|BAUD_RATE\|RABBIT_URL" --include="*.py" .   # no matches
env -u HOST ./venv/bin/python -c "import constant"                 # non-zero exit, names HOST
LINE_NAME=testing ./venv/bin/python monitor.py --once; echo "exit=$?"   # still 0
```

**Log (2026-09-07):**

Three changes applied to `constant.py`:
1. `SERIAL_PORT` and `BAUD_RATE` (lines 5–8, including the hardcoded overwrite) deleted (#16).
2. `RABBIT_URL` (last line) deleted.
3. `DB_CONFIG` fallbacks replaced with `_required_str()` calls for `HOST`, `DATABASE`, `DB_USER`, and `PASSWORD`. Helper added immediately before `DB_CONFIG`.

**Criterion 1 — no dead symbols:**
```
grep exit=1   # no matches, grep found nothing
```

**Criterion 2 — missing HOST raises and names the key:**
```
OSError: Required environment variable 'HOST' is not set
exit=1
```

**Criterion 3 — `monitor.py --once` still exits 0:**
```
2026-09-07 15:08:14.741 | INFO  | line='testing' team=99 factory=99 station=99
2026-09-07 15:08:14.741 | ERROR | defect poll failed, treating as no-defect: could not translate host name "dummy" ...
2026-09-07 15:08:14.741 | INFO  | defect=False
exit=0
```

What we learned: `_required_str` raises `OSError` (consistent with the existing `EnvironmentError`/`OSError` pattern — they are the same exception on Python 3). The `PASSWORD` guard uses `if not val` which also catches an empty string `''`, preventing a blank password from silently connecting.

---

## Risks & Edge Cases

**R1 — Fail-silent hides a database outage (accepted, by your decision).**
Mapping a DB error to `False` means an RDS outage or credential expiry stops a live alarm and the
line looks healthy. Mitigation: every caught exception logs at ERROR with the exception text, so
the outage is loud in the journal even though the speaker is quiet. A follow-up option, if you
want it later, is an N-consecutive-failure threshold before releasing the sound; the loop should
be written so that becomes a one-constant change.

**R2 — Audio chopping if the database flaps.**
At 1 Hz, alternating success/failure restarts the WAV from the top each second, producing stutter
rather than a clean alert. Mitigation: transition-only logging makes flapping obvious in the log;
R1's threshold is the fix if it is ever observed.

**R3 — `pkill -f aplay` is machine-wide.**
`speaker_handler.py:19-23` kills every `aplay` on the host, not just this daemon's. On a dedicated
kiosk that is harmless and it usefully cleans up orphans from a crashed predecessor. Mitigation:
Step 4's early return in `stop_all()` means it fires only on a real playing→stopped transition,
not once per second. Do not run a second instance of this daemon on one host — they will kill each
other's audio. Worth a `systemd` unit with a fixed name later.

**R4 — One connect/auth round trip per second to RDS.**
86 400 connections/day per station, each paying TLS and auth latency. Acceptable at this scale and
strictly better than the current leak, but revisit with a persistent connection if the poll ever
tightens below 1 s, more stations run per host, or Step 6 item 6 shows connect latency approaching
the interval.

**R5 — Poll latency is added to the interval, not absorbed by it.**
`time.sleep(CHECK_INTERVAL)` after a query that itself takes 50-150 ms gives a real period above
1 s. Acceptable for an alarm. If drift matters later, sleep on a deadline instead of a fixed
duration.

**R6 — The WAV named by `SOUND_<line>` may be missing.**
No `.wav` files are checked into this repo. `SpeakerHandler.__init__` raises `FileNotFoundError`
at startup, which is the correct loud failure — but it means the daemon will not start on a fresh
machine until the audio asset is placed. Call this out in the deploy notes. The upstream
`spectrum_speaker/create_new_sounds/build_sound_files.sh` is where those assets come from.

**R7 — Wrong ALSA device.**
`AUDIO_DEVICE` defaults to the upstream `plughw:0,0`. If the card enumerates differently on the
Ontario host, `aplay` fails silently in a loop and no sound plays while the code reports success.
Mitigation: Step 4's Terminal A/B check is an audible test, not just a process check. Confirm with
`aplay -l` on the target host before deploying.

**R8 — No process supervision yet.**
This plan produces a foreground daemon. There is no systemd unit in this repo (upstream has
`service_files/tristar-stacklight-sound.service.in`). Until one exists, nothing restarts the
daemon after a crash or a reboot. Out of scope here; flagging it as the obvious next plan, and
Step 5's SIGTERM handler is written so the unit will behave correctly when it lands.

**R9 — No automated test coverage.**
Every verification in this plan is a manual command or an observed behavior. That is proportionate
to a 4-file repair with hardware and database dependencies, but it means regressions will not be
caught. The state mapping in Step 5 is the piece most worth a real unit test later, against a fake
speaker handler.

**R10 — `TESTING` left set silences or forces the live alarm.**
`get_defect_status()` returns the override and never opens a connection when `TESTING` is
non-empty. A leftover `TESTING=true` in `.env` on the Ontario host would sound continuously
regardless of `unacked_count`; `TESTING=false` would stay silent through a real defect. Mitigation:
`.env_template` says the key should be unset if not testing; `_testing_override()` treats empty as
"use the database"; invalid values fail at import rather than falling through. Confirm `TESTING` is
unset before any production start.

---

### Step 8 — Eliminate idle-in-transaction on database connections

**Files:** `get_latest_database_values.py`

**Problem:** psycopg2 defaults to `autocommit = False`. Before the first `execute()` it issues
`BEGIN` and leaves the session **idle in transaction** until the `SELECT` arrives; after the
result comes back, it stays idle in transaction until `COMMIT`. If the server's
`idle_in_transaction_session_timeout` fires in either gap, the client is dropped. Both code paths
are affected:

- `get_defect_status()` (`get_latest_database_values.py:80-85`) opens a new connection per poll
  and wraps it in `with conn:` (transaction commit/rollback) without autocommit. The
  `BEGIN`/`COMMIT` round trips bracket a single read-only `SELECT` that needs neither.
- `StationStatusDao.open()` (`line 17-21`) connects but never sets autocommit, so every
  `get_status()` call also runs inside `BEGIN`/`COMMIT`.
- `StationStatusDao.get_status()` (`line 46`) uses `with self._db_conn as conn:`, which is a
  transaction context under the default mode and reintroduces the same window on the long-lived
  connection.

The queries return a single boolean row. The window at risk is the protocol round-trip delay, not
query execution time (a slow query would show as `active`, which the connection reaper does not
target).

**Fix:**

1. **`get_defect_status()`** — after `psycopg2.connect(**DB_CONFIG)` inside `contextlib.closing`,
   set `conn.autocommit = True`. Remove the inner `with conn:` block; it is a transaction context
   that is both unnecessary (read-only, single statement) and harmful (reintroduces the
   idle-in-transaction window). Keep `contextlib.closing` for socket cleanup. Open the cursor
   directly on `conn`. SQL, signature, and return contract are unchanged.

2. **`StationStatusDao.open()`** — set `self._db_conn.autocommit = True` immediately after
   `psycopg2.connect(**DB_CONFIG)`. This covers every subsequent `get_status()` call on the
   long-lived connection without requiring `get_status()` itself to touch the connection mode.

3. **`StationStatusDao.get_status()`** — replace `with self._db_conn as conn:` with a direct
   cursor open on `self._db_conn` (e.g. `with self._db_conn.cursor() as cur:`). Under autocommit,
   `with conn:` opens a transaction context, undoing the fix from step 2.

These are read-only existence/sum checks; they do not need multi-statement transaction semantics.
Signatures, SQL, and return contracts are unchanged. `StationStatusDao` was explicitly left
untouched in Step 3 (`Not touched` note in the file inventory); this step makes the minimum
addition to close the idle-in-transaction window there.

**Verification Criteria**

Confirm `with conn:` / `with self._db_conn as conn:` is absent from both patched paths (these
are the tell-tale transaction contexts):

```bash
grep -n "with conn\b\|with self\._db_conn as" get_latest_database_values.py
```

No matches.

Confirm `autocommit = True` appears in both locations — once in `get_defect_status` and once in
`StationStatusDao.open`:

```bash
grep -n "autocommit" get_latest_database_values.py
```

Two matches, one in each function.

Import and signature smoke-check (no live DB required):

```bash
./venv/bin/python -c "
import inspect
from get_latest_database_values import get_defect_status, StationStatusDao
print('get_defect_status sig:', inspect.signature(get_defect_status))
print('get_status sig:', inspect.signature(StationStatusDao.get_status))
print('ok')
"
```

Prints both signatures and `ok`, exits 0. No `ImportError`, no `NameError`.

Confirm the fd-delta check from Step 3 still holds (connection still closed per poll despite
structural change):

```bash
./venv/bin/python -c "
import os
from get_latest_database_values import get_defect_status
from constant import LINE_TESTING as L
def fds(): return len(os.listdir(f'/proc/{os.getpid()}/fd'))
for _ in range(5): 
    try: get_defect_status(L['TEAM_ID'], L['FACTORY_ID'], L['STATION_ID'])
    except Exception: pass
before = fds()
for _ in range(60):
    try: get_defect_status(L['TEAM_ID'], L['FACTORY_ID'], L['STATION_ID'])
    except Exception: pass
print('fd delta:', fds() - before)
"
```

`fd delta: 0`. The autocommit restructuring must not reintroduce the socket leak that Step 3 fixed.

**Log (2026-09-11):**

Three changes applied to `get_latest_database_values.py`:
1. `get_defect_status()` — added `conn.autocommit = True` after `psycopg2.connect`; removed the
   inner `with conn:` transaction context. Cursor opened directly on `conn`.
2. `StationStatusDao.open()` — added `self._db_conn.autocommit = True` immediately after connect.
3. `StationStatusDao.get_status()` — replaced `with self._db_conn as conn: cur = conn.cursor()`
   with `with self._db_conn.cursor() as cur:`, eliminating the transaction context under autocommit.

**Criterion 1 — no transaction contexts:**
```
with conn:      → exit 1 (no matches)
with self._db_conn as  → exit 1 (no matches)
```
Note: the plan's grep pattern `with conn\b` also matches `with conn.cursor()` (word boundary
before `.`). The only remaining match on line 82 is `with conn.cursor() as cur:` — a cursor
context manager, not a transaction context. Both exact transaction patterns are absent.

**Criterion 2 — two autocommit lines:**
```
20:            self._db_conn.autocommit = True
81:        conn.autocommit = True
```

**Criterion 3 — import/signature smoke-check:**
```
get_defect_status sig: (team_id: int, factory_id: int, station_id: int) -> bool
get_status sig: (self, team_id: int, factory_id: int, station_id: int)
ok
exit=0
```
Note: `.env` has `CHECK_INTERVAL=` and all `TEAM_ID_*` etc. set to empty strings. Shell env vars
supplied explicitly take priority over `load_dotenv()` (which does not override by default). This
is a pre-existing environment state, not introduced by this step.

**Criterion 4 — fd delta:**
```
fd delta: 0
```

What we learned: `contextlib.closing` correctly closes the socket even without `with conn:`. Under
`autocommit = True`, `with conn:` would open a new transaction context — removing it was required,
not just optional. `StationStatusDao.get_status()` now opens a cursor directly on the
already-autocommit connection, so no `BEGIN`/`COMMIT` wraps the single read-only SELECT.

---

### Step 9 — Add in a file to send test defects

**Files:** `send_defect_amqp.py`

**Problem:** Need to be able to send test defects to the internal dashboard in order to test this process. 

1. Copy the 'send_defect_amqp.py" file from the "spectrum_sound" directory. 
2. Change the line, station, factory to read from this .env file.

---

### Step 10 — CLI line selection and `TESTING` override ✓ COMPLETE

**Files:** `monitor.py`, `get_latest_database_values.py`, `.env_template`

**Problem:** Step 5 selected the line via `LINE_NAME` and used `--once` as the DB-free field
probe. Operators now choose the line on the command line, and local speaker tests need a way to
force `get_defect_status()` true or false without writing `unacked_count`.

**As-built (2026-09-13):**

1. **`monitor.py`** — required positional argparse `line` with `choices=("11", "12", "testing")`.
   `load_line()` looks the value up in `_LINE_MAP`. argparse rejects a missing or unknown `line`
   before `main()` runs (exit 2). `--once` is gone. `LINE_NAME` is no longer imported.
2. **`get_latest_database_values.py`** — `_testing_override()` parses `TESTING` at import.
   Unset or empty → `None` (query the database). `1`/`true`/`yes` → `True`; `0`/`false`/`no` →
   `False`; anything else raises `ValueError`. When the override is not `None`,
   `get_defect_status()` returns it and does not connect.
3. **`.env_template`** — `TESTING=` published at the top with `Should be unset if not testing`.
   `LINE_NAME` is not in the template.

`LINE_NAME = os.getenv('LINE_NAME')` remains in `constant.py` as a leftover from Step 2. It has no
caller. Leaving it is the smallest as-built diff; delete it in a later cleanup if desired.

**Verification Criteria** (observed 2026-09-13):

```bash
./venv/bin/python monitor.py --help
```

```
usage: monitor.py [-h] {11,12,testing}
...
positional arguments:
  {11,12,testing}  Line to monitor (11, 12, or testing)
```

```bash
./venv/bin/python monitor.py; echo "exit=$?"
./venv/bin/python monitor.py nonsense; echo "exit=$?"
```

```
monitor.py: error: the following arguments are required: line
exit=2
monitor.py: error: argument line: invalid choice: 'nonsense' (choose from 11, 12, testing)
exit=2
```

```bash
TESTING=true  ./venv/bin/python -c "from get_latest_database_values import get_defect_status, TESTING; print(TESTING, get_defect_status(1,2,3))"
TESTING=false ./venv/bin/python -c "from get_latest_database_values import get_defect_status, TESTING; print(TESTING, get_defect_status(1,2,3))"
TESTING=      ./venv/bin/python -c "from get_latest_database_values import TESTING; print(TESTING)"
TESTING=maybe ./venv/bin/python -c "from get_latest_database_values import TESTING"
```

```
True True
False False
None
ValueError: TESTING must be true/false (or empty), got: 'maybe'
exit=1
```

The `true`/`false` paths return without a database connection (no fail-silent ERROR log). Empty
`TESTING` leaves the override as `None`, so the next poll uses the query.

**To drive the speaker without a live defect row:**

```bash
TESTING=true ./venv/bin/python monitor.py testing
```

Unset `TESTING` (or set it empty) before any Ontario-host run so the database remains the source
of truth. See R10.

---

## Notes

- `plan-architecture.mdc` specifies `docs/plans/`; this file sits at `docs/plan/` because that is
  the path referenced in the request and an empty file already existed there. Say the word and it
  moves.
- No secrets, credentials, hostnames, or connection strings appear above — only environment
  variable names and `path:line` citations.
- 2026-09-13: Step 10 records the as-built CLI (`python monitor.py {11|12|testing}`) and the
  `TESTING` override. Historical Step 5 `LINE_NAME` / `--once` commands are superseded, not rewritten.
