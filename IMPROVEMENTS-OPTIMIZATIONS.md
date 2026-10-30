# Improvements & Optimizations — `tor-circuit-collector`

**Review date:** 2026-10-29
**Scope:** `tor-circuit-collector.py` (3,218 lines), `apt-install-tor.sh`, `tor-circuit-collector.sh`, `tor-circuit-collector.json`, `README.md`, `.gitignore`.
**Nature:** static review plus targeted dynamic checks. No live Tor session, download, or APT operation was performed.

This document proposes concrete, prioritized improvements. It does **not** change behaviour by
itself. Each item records the evidence (file and line), the impact, a suggested approach, and how
to verify the fix.

---

## How this review was produced

Inspected:

- [x] `tor-circuit-collector.py` in full (constants, dataclasses, controller integration, circuit
  lifecycle, keep-alive, download worker, history persistence, filename handling, table layout,
  curses main loop, config/argparse layer).
- [x] `apt-install-tor.sh`, `tor-circuit-collector.sh`, `README.md`, `.gitignore`.
- [x] `tor-circuit-collector.json` structure only, with the authentication value masked.
- [x] `git status --short`, `git ls-files`, file modes.

Checks actually executed (all passed at review time):

```text
python3 -m py_compile tor-circuit-collector.py     # OK
python3 tor-circuit-collector.py --help >/dev/null # OK
bash -n tor-circuit-collector.sh apt-install-tor.sh# OK
shellcheck -x tor-circuit-collector.sh apt-install-tor.sh  # OK (exit 0)
git diff --check                                   # OK
```

One issue was additionally reproduced in isolation (a minimal harness that mirrors the
`unique_path()` existence check): two concurrent callers both selected the same destination
path. Details under **P0-3**.

---

## Priority definitions

| Priority | Meaning |
|---|---|
| **P0** | Data-safety, secret-exposure, or an invariant in `AGENTS.md` that the code can currently violate. Fix before the next feature. |
| **P1** | Correctness or resource bug with realistic user impact; or a documented behaviour that is not actually implemented. |
| **P2** | Robustness, performance, and diagnosability. Worth scheduling. |
| **P3** | Maintainability and developer experience. Do opportunistically. |

Effort is a rough estimate for one developer familiar with the codebase: **S** ≤ 1 h, **M** ≈ half a day, **L** ≥ 1 day.

---

## Summary

| ID | Title | Priority | Impact | Effort | Risk | Done |
|---|---|---|---|---|---|---|
| P0-1 | Tracked config may contain a plaintext control password (mode `0666`) | P0 | Secret disclosure via git history | S | Low | ⬜ |
| P0-2 | Live history `.jsonl` files are tracked by git | P0 | Private URLs/filenames in git history | S | Low | ⬜ |
| P0-3 | `unique_path()` races across slots and can overwrite a finished file | P0 | Silent data loss | M | Low | ✅ |
| P0-4 | SOCKS isolation credentials generated with `random`, not `secrets` | P0 | Weak capability separation between slots | S | Low | ⬜ |
| P1-1 | `argparse.BooleanOptionalAction` contradicts the documented Python 3.8 floor | P1 | Startup crash on 3.8 | S | Low | ⬜ |
| P1-2 | `self.sessions` mutated without `self.lock` | P1 | Duplicate/leaked sessions, stale proxies | S | Low | ✅ |
| P1-3 | `self.threads` grows without bound | P1 | Slow memory growth in long runs | S | Low | ✅ |
| P1-4 | "Resume" restarts from byte 0; `.part` is truncated with `"wb"` | P1 | Wasted bandwidth, no true resume | M | Medium | ✅ |
| P1-5 | `normalize_history_url()` is a no-op | P1 | Failed entries are not suppressed on success | S | Low | ✅ |
| P1-6 | History files grow forever; whole-file rewrite per success | P1 | Startup latency, corruption window | M | Medium | ◐ |
| P1-7 | RFC 5987 `filename*=` in `Content-Disposition` is ignored | P1 | Wrong filename for non-ASCII downloads | S | Low | ⬜ |
| P1-8 | Custom path selection bypasses Tor's weighting and guard policy | P1 | Slower circuits, weaker anonymity properties | M | Medium | ⬜ |
| P2-1 | 29 `except Exception` handlers; 10 are bare `pass`, most others degrade silently | P2 | Undiagnosable failures | M | Low | ◐ |
| P2-2 | `relay_candidates()` refetches the full consensus per build | P2 | Startup/burst latency | S | Low | ✅ |
| P2-3 | Control-port probing is serial with per-port connect timeouts | P2 | Slow, silent startup when Tor is absent | S | Low | ✅ |
| P2-4 | `draw_ui()` re-acquires `app.lock` per slot and snapshots shallowly | P2 | Torn rows, redundant locking | S | Low | ⬜ |
| P2-5 | Truncation is character-based, not display-width-based | P2 | Misaligned columns on wide/combining chars | S | Low | ⬜ |
| P2-6 | Downloads: no `Accept-Encoding: identity`, size cap, retry, or free-space check | P2 | Wrong progress, disk-full failures | M | Low | ◐ |
| P2-7 | `apt-install-tor.sh`: duplicate `apt-get update`, `madison` ordering assumption, remove-then-install downtime | P2 | Slower, riskier upgrades | S | Low | ⬜ |
| P3-1 | Single 3.2k-line module | P3 | Maintainability | L | Medium | ⬜ |
| P3-2 | No automated tests or CI | P3 | Regressions in pure helpers | M | Low | ⬜ |
| P3-3 | Assorted polish (`--version`, typing, stale comments) | P3 | Developer experience | S | Low | ⬜ |

Legend: ⬜ not started · ◐ partially landed (see the item's `Status` block for what remains) ·
✅ done and locally verified.

### Progress on the items implemented in this pass

- [x] **P1-4** true resume after a circuit rebuild — `Range` request, `206` + `Content-Range`
      validation, append-mode writes, `416` fallback, partial kept across rebuilds
- [x] **P2-2** relay/consensus cache — `collector.relay_cache_ttl` (default `300`, `0` disables)
- [x] **P2-3** parallel control-port pre-probe — `_tcp_probe_many()` using
      `tor.port_probe_timeout`
- [x] **P0-3** atomic filename reservation — `reserve_download_path()` /
      `_candidate_paths()` under a process-wide `filename_lock`
- [x] **P1-2** sessions under `self.lock` — `_build_session()` + `session_for_slot()` +
      `discard_session()`; `shutdown()` snapshots instead of iterating live
- [x] **P1-3** bounded thread registry — `Set[threading.Thread]` discarded in a `finally`
- [x] **P1-5** real URL normalization — fragment dropped, scheme/host lowered, default port
      removed, query sorted
- [x] **P2-1** in-memory debug sink + `D` viewer — ring buffer, counters, `_note()`
- [x] **P1-6** atomic history rewrite — temp sibling + `fsync` + `os.replace()`
- [x] **P2-6** (partial) `Accept-Encoding: identity`, retry with backoff, free-space
      precheck, strict `Content-Length` parsing
- [ ] Live-Tor validation for the items above (real circuit drop, consensus-fetch count,
      time-to-error with Tor stopped, `NS`-driven cache invalidation)

---

# P0 — fix first

## P0-1. Tracked config can contain a plaintext control password, mode `0666`

**Evidence**

- `tor-circuit-collector.json` is tracked (`git ls-files` lists it).
- Its `tor.control_password` key exists and is **non-empty** (checked without printing the value).
- The file mode is `0666` (world-readable and world-writable), as are both history files.
- `BUILTIN_DEFAULTS["control_password"] = None`, and `load_config_file()` passes the JSON value
  straight into argparse defaults, so nothing rejects or discourages a literal secret.
- `.gitignore` does not ignore the config file.

**Impact.** Anyone with read access to the working tree, a backup, or the git remote can
authenticate to the local Tor ControlPort. Because `__LeaveStreamsUnattached=1` plus
`attach_stream` is used, that is a strong position: an attacker can observe and steer stream
attachment, close circuits, and read `net/listeners/*`. Once committed, the secret stays in git
history even if it is later removed from the working tree.

**Suggested approach**

1. [ ] Prefer cookie authentication. `connect_controller()` calls
   `authenticate(password=self.args.control_password)`; Stem falls back to cookie auth when the
   password is empty. Make an empty/`null` value explicitly select cookie auth, and document
   `CookieAuthentication 1` as the default path.
2. [ ] Warn at startup (in the TUI status line, not stdout) when a non-empty password came from a
   group/world-readable config file, mirroring the existing `port_discovery_line` banner.
3. [ ] Untrack the file and ignore it: `git rm --cached tor-circuit-collector.json` plus a
   `.gitignore` entry. Commit a `tor-circuit-collector.example.json` with
   `"control_password": null` and have `load_config_file()` fall back to built-in defaults when
   the real file is missing. Keep the current "config path defaults next to the script" rule.
4. [ ] Create the config with mode `0600` when the app ever writes one, and add
   `chmod 600 tor-circuit-collector.json` to the README's Tor configuration section.
5. [ ] Rotate the Tor control password, and treat the old value as compromised if the repository has
   ever been pushed.

**Verification**

- [ ] `git check-ignore -v tor-circuit-collector.json` matches; `git ls-files tor-circuit-collector.json`
  is empty.
- [ ] With `"control_password": null` against cookie-auth Tor, the TUI reaches
  `Connected to Tor control port ...` (live check, needs the user's Tor).
- [ ] `stat -c '%a' tor-circuit-collector.json` shows `600`.

**Secret-handling note.** Do not paste the current value into an issue, commit message, or chat.
---

## P0-2. Live history `.jsonl` files are tracked by git

**Evidence**

- `git ls-files` lists `tor-circuit-collector.success-history.jsonl` (≈555 KB) and
  `tor-circuit-collector.failed-history.jsonl` (≈223 KB).
- `git status --short` shows both as modified: the app appends during normal use, so every run
  dirties the repository.
- `.gitignore` lists only `Tor Circuit Collector Script_files`, `__pycache__`, `downloads`, `.loupe`.
- `AGENTS.md` explicitly calls these "live user history, not fixtures."

**Impact.** Requested URLs, filenames, byte counts, and error strings are private browsing data.
Tracking them publishes that data and makes `git diff` noisy and merge-prone.
`rewrite_history_file()` rewrites the whole failed-history file on every success, producing large,
unreviewable diffs of private data.

**Suggested approach**

- [ ] `git rm --cached tor-circuit-collector.success-history.jsonl tor-circuit-collector.failed-history.jsonl`
- [ ] Add to `.gitignore`: `tor-circuit-collector.*-history.jsonl`
- [ ] Optionally add committed empty fixtures under `tests/fixtures/` if tests are added (P3-2).
  Do **not** rewrite or truncate the working copies — only untrack them.
- [ ] Consider honouring `XDG_STATE_HOME` (default `~/.local/state/tor-circuit-collector/`) so the tool
  works from a read-only or shared install directory. Keep the script-directory location as a
  fallback and document the precedence, since the README promises "in the script directory."

**Verification**

- [ ] `git ls-files '*history.jsonl'` is empty.
- [ ] `git status --short` no longer lists the histories after a download.
- [ ] File contents and line counts are unchanged (`wc -l` before/after).

---

## P0-3. `unique_path()` races across slots and can overwrite a finished file

**Evidence.** `unique_path()` (`tor-circuit-collector.py:1476-1489`) does a check-then-act:

```python
if not path.exists():
    return path
...
while True:
    candidate = parent / f"{stem}-{counter}{suffix}"
    if not candidate.exists() and not candidate.with_suffix(candidate.suffix + ".part").exists():
        return candidate
```

The call site is `download_worker()` (`:1313`), which runs on a per-slot thread guarded only by
`slot.io_lock` — a **per-slot** lock. Two slots downloading the same URL (a normal workflow: the
README encourages retrying a failed URL on another circuit) can both observe the name as free and
return the same `final_path`. Each writes its own `<name>.part` and calls
`os.replace(part_path, final_path)` (`:1342`). The second replace silently clobbers the first
completed file, and the success history records two entries for one surviving file. I reproduced
the selection race with a two-thread harness mirroring the same exists-then-return logic: both
threads selected the identical path.

**Impact.** Silent data loss of a successfully downloaded file, plus a success-history entry that
points at content produced by a different transfer. This violates the documented invariant "Never
overwrite an existing final file."

**Suggested approach.** Add a process-wide `self.filename_lock` and reserve atomically with
`O_EXCL` inside it:

```python
def reserve_download_path(self, desired: Path) -> Path:
    with self.filename_lock:
        for candidate in self._candidate_paths(desired):
            part = candidate.with_suffix(candidate.suffix + ".part")
            try:
                fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            os.close(fd)
            return candidate
    raise RuntimeError("could not reserve a unique download filename")
```

`O_EXCL` makes reservation atomic across threads and processes. Keep the numeric-suffix sequence
and `sanitize_filename()` unchanged, and keep avoiding names whose `.part` exists. Keep the
failure-path `part_path.unlink()`; note that with `keep_partials` a failed `.part` stays reserved,
so the next identical download takes the next suffix — confirm and document that.

**Status: DONE** — `unique_path()` is gone. `_candidate_paths()` yields `desired` then `-1`, `-2`, …;
`reserve_download_path()` creates the **`.part`** with `O_CREAT|O_EXCL|O_WRONLY` (mode `0600`) under
`self.filename_lock` and returns the final name, so the reservation is atomic across threads *and*
processes. `download_worker()` reserves for fresh transfers and reuses the already-reserved `.part`
on resume, which is what makes the `.part` the correct reservation key. `paths_reserved` is counted
in the diagnostic sink.

**Verification.** Unit test the helper from N threads with the same desired name, asserting N
distinct paths. Integration without Tor: stub the HTTP layer to return the same body twice, run two
slots, assert two distinct final files and two distinct success-history filenames.

Verified locally with a Tor-free harness (`/tmp/tcc_reliability_test.py`): 16 threads released by a
barrier against one desired name produced 16 distinct paths with 16 distinct existing `.part` files;
a pre-existing final file was skipped (`done.bin` → `done-1.bin`); a second `O_EXCL` open of a
reserved `.part` raised `FileExistsError`. The cross-slot HTTP integration check still needs a Tor-free
stub of `download_worker()` and has not been written.

---

## P0-4. SOCKS isolation credentials use `random`, not `secrets`

**Evidence**

```python
self.run_id = self._random_token(8)
self.socks_password = self._random_token(12)

@staticmethod
def _random_token(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))
```

`random` is a Mersenne Twister, not a CSPRNG, and its state can be recovered from enough outputs.
`run_id` (8 chars, 36^8 ≈ 2.8e12) is embedded in every SOCKS username
`tcc-<run_id>-slot-<n>`, and `socks_password` is shared by all slots.

**Impact.** The username is the only thing binding a `STREAM` event to a slot
(`username_to_slot`, `on_stream_event`). Anyone who can guess or recover the run id and reach the
SOCKS port with `IsolateSOCKSAuth` can present `tcc-<run_id>-slot-3` and have their stream attached
to slot 3's circuit. On a single-user desktop the practical risk is low, but this is a capability
token and the project's threat model is about traffic separation.

**Suggested approach**

```python
import secrets

@staticmethod
def _random_token(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
```

- [ ] Raise the run id to ≥16 characters and the password to ≥24.
- [ ] Optionally derive a distinct password per slot
  (`hmac.new(base, str(slot).encode(), "sha256").hexdigest()`), so one leaked credential does not
  unlock every slot. `session_for_slot()` already builds the proxy URL per slot, so this is small:
  `self.socks_password` becomes a `slot_password(index)` helper.
- [ ] Keep the values out of the session-output log and the TUI (they currently are).

**Verification.** `grep -n "random\." tor-circuit-collector.py` shows no credential use left.
Assert in a unit test that two runs differ and meet the minimum length.

---

# P1 — correctness and documented behaviour

## P1-1. `argparse.BooleanOptionalAction` contradicts the stated Python 3.8 floor

**Evidence.** `README.md:93` says "Python 3.8 or newer is recommended", and `AGENTS.md` says to
target the documented version. `BooleanOptionalAction` was added in **Python 3.9** and is used at
`:3162`, `:3167`, `:3172` (`--keep-partials`, `--leave-circuits`, `--show-paths`). On 3.8 the
module raises `AttributeError` while building the parser — before `--help` can work, defeating the
deliberate lazy-import design in `ensure_dependencies()`.

**Suggested approach.** Prefer explicit paired flags, which also read better in `--help`:

```python
parser.add_argument("--keep-partials", dest="keep_partials", action="store_true", help=...)
parser.add_argument("--no-keep-partials", dest="keep_partials", action="store_false", help=...)
```

Repeat for the other two, or add a small `add_bool_flag(parser, "--show-paths", "show_paths", ...)`
helper. Alternatively raise the documented floor to 3.9 in `README.md` and `AGENTS.md`.

**Verification.** `py_compile` under 3.8 is not enough (the failure is at runtime); run
`python3.8 tor-circuit-collector.py --help` if a 3.8 interpreter is available.

---

## P1-2. `self.sessions` is mutated without `self.lock`

**Evidence**

- `session_for_slot()` (`:1127-1138`) reads and writes `self.sessions` with no lock.
- `download_worker()` calls it under `slot.io_lock` (`:1300-1301`) and `keepalive_once()` under the
  same per-slot lock (`:1184-1185`); different slots' locks do not serialize.
- `shutdown()` iterates `list(self.sessions.values())` (`:720`) without the lock while workers may
  still run.
- `replace_circuit()` pops and closes a session under `self.lock` (`:793`, `:807-811`), so one path
  does lock. That inconsistency is the bug: a worker can read a session another thread just closed
  and removed.

**Impact.** Two slots can share a `requests.Session` whose proxy points at one slot's SOCKS
username, or a worker can use a session closed underneath it and fail with a confusing
`ConnectionError`. The keep-alive retry (`:1196-1201`) mitigates only the keep-alive case.

**Suggested approach.** Serialize only the map, never the network call:

```python
def session_for_slot(self, slot_index: int) -> requests.Session:
    with self.lock:
        session = self.sessions.get(slot_index)
        if session is None:
            session = self._build_session(slot_index)
            self.sessions[slot_index] = session
        return session
```

`self.lock` is an `RLock` held for microseconds here, so the "never hold a lock during I/O" rule is
respected. Add `discard_session(slot_index, session)` (identity-compare before popping, as
`:1197-1199` already does) and use it in `replace_circuit()`, `terminate_slot_download()`, and the
keep-alive retry.

**Verification.** Fake session factory: two threads calling `session_for_slot(1)` get the same
object; `discard_session()` then yields a fresh one.

---

## P1-3. `self.threads` grows without bound

**Evidence.** `_start_thread()` (`:760-763`) appends every thread to `self.threads` and nothing
removes entries. `monitor_loop()` starts a build or keep-alive thread roughly every
`keepalive_interval` seconds per slot (default 60 s × 9 slots), and each download adds another.
`shutdown()` never joins them; it only closes sessions and the controller.

**Impact.** A multi-day session accumulates thousands of dead `Thread` objects and keeps their
frames alive. It also makes "wait for workers" impossible at shutdown, so a download can still be
writing when `finally: app.shutdown()` runs.

**Suggested approach.** Use a `Set[threading.Thread]` and remove the thread in a `finally` inside a
wrapper target:

```python
def _start_thread(self, target, name, args=()):
    def run():
        try:
            target(*args)
        finally:
            with self.lock:
                self.threads.discard(threading.current_thread())
    t = threading.Thread(target=run, name=name, daemon=True)
    with self.lock:
        self.threads.add(t)
    t.start()
```

In `shutdown()`, set `stop_event`, then join download threads with a bounded timeout (e.g. 5 s
each) so `.part` cleanup and history appends finish — after closing sessions, which interrupts the
blocking read.

**Verification.** Log `len(app.threads)` in a debug build and run for an hour; it should stay near
`slots + active transfers`.

---

## P1-4. "Resume" restarts from byte 0; the `.part` file is truncated with `"wb"`

**Evidence**

- `replace_circuit()` preserves `pending_resume_url` (`:782-795`) and calls `start_download()` after
  a rebuild (`:913-915`).
- `download_worker()` opens `part_path.open("wb")` (`:1325`), which truncates. There is no `Range`
  header, no `Content-Range` parsing, and no `Accept-Ranges` check.
  `DownloadState.bytes_done` resets to 0 in `start_download()` (`:1274-1276`).
- `keep_partials` only controls whether the `.part` survives a failure; it is never reused.

**Status: DONE** — `start_download()` reuses the kept `.part` and seeds `resumed_from` from its size;
`download_worker()` sends `Range: bytes=<size>-` with `"ab"` append writes, validates `206` plus a
matching `Content-Range`, falls back to a full body on `200`/mismatch, retries once without `Range`
on `416`, and keeps rebuild-interrupted partials while still deleting them on user cancel. Live
range/416 behaviour against a local HTTP server was exercised; real-Tor interruption is not yet
tested.

- [x] Keep the interrupted `.part` when a rebuild replaces the circuit
- [x] Resume with `Range: bytes=<size>-` and append (`"ab"`) instead of truncating (`"wb"`)
- [x] Accept only `206` whose `Content-Range` starts at the requested offset
- [x] Restart from byte 0 when the server ignores `Range` or sends a mismatched range
- [x] Retry once without `Range` on `416`
- [x] Never resume a manually cancelled transfer
- [ ] Verify against a real Tor circuit that drops mid-transfer

**Impact.** The README and docstring say an interrupted download is "resumed", but every rebuild
re-downloads from zero — the most expensive behaviour for large Tor downloads, and it can loop when
a circuit keeps failing mid-transfer. Users may also believe `keep_partials` enables resume.

**Suggested approach**

1. [x] If a `.part` exists for the reserved final path and `pending_resume_url` matched, send
   `Range: bytes=<size>-`. Accept only `206` with a `Content-Range` whose start equals `<size>`;
   otherwise truncate and restart (servers may ignore `Range`).
2. [x] Open `"r+b"` and seek to end when appending, `"wb"` otherwise; keep `os.replace()` to publish.
3. [x] Keep the rule that a user-cancelled transfer is not resumed (`cancel_requested`, and
   `reason != "manual replace"`, already the case).
4. [x] `pending_resume_url` is already per slot, so resumable state fits.
5. [x] Update the README to say precisely what is resumed and whether `keep_partials` affects it.

**Verification.** Unit test header construction and status validation with stub responses (206 with
matching `Content-Range`, 200 fallback, 416 restart). Live: interrupt a range-capable download and
confirm the retry's `bytes_done` starts at the previous size.

---

## P1-5. `normalize_history_url()` is a no-op

**Evidence**

```python
@staticmethod
def normalize_history_url(url: str) -> str:
    return url.strip()
```

`AGENTS.md` says repeated unresolved failures retain only the most recent failure for a
normalized URL, and the README says failed entries disappear once a URL succeeds. Because
normalization only strips whitespace, `https://example.com/a.pdf` and
`https://example.com/a.pdf#page=2` (or different query ordering) are different keys, so a success
with a fragment does not clear the failure recorded without it.

**Impact.** The failed-history viewer shows entries that have actually succeeded, exactly what the
dedup logic exists to prevent, and `history_limit` fills with near-duplicates.

**Suggested approach.** Normalize conservatively:

```python
@staticmethod
def normalize_history_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    query = "&".join(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))  # drop fragment
```

Do **not** strip query parameters (`?id=123` is often the document identity) and do not lower-case
the path (paths are case-sensitive). Add `urlunsplit`/`parse_qsl` to the existing `urllib.parse`
import. `load_persistent_histories()` already recomputes `url_norm`, so old entries get re-keyed;
the stale on-disk `url_norm` is unused after load.

**Verification.** `normalize(x) == normalize(x + "#frag")`;
`normalize("HTTPS://Example.COM:443/a?b=2&a=1") == normalize("https://example.com/a?a=1&b=2")`;
different paths stay distinct; the viewer hides a failed entry after a successful retry with a
fragment appended.

---

## P1-6. History files grow forever and are rewritten wholesale

**Evidence**

- `append_history_file_entry()` appends unconditionally (`:376-381`); nothing truncates the file.
  `history_limit = 2000` applies only to in-memory lists.
- On every success that removes a failed entry, `append_download_history()` calls
  `rewrite_history_file(self.failed_history_file, list(reversed(failed_snapshot)))` (`:265-266`).
  `rewrite_history_file()` opens with `"w"` and writes line by line (`:384-390`).
- The tracked files are already 555 KB and 223 KB.

**Impact.** Startup parses the whole file (`load_history_file()`) before the TUI appears, and both
files grow without bound. The rewrite is non-atomic: a crash mid-write leaves a truncated
failed-history file with no backup, and it rewrites data the app does not re-read during the
session.

**Suggested approach**

- [ ] Compact on startup instead of on every success: after `load_persistent_histories()`, write the
  truncated newest-first lists to a temp file and `os.replace()` it (atomic). Cap at
  `history_limit` entries (or 2× for headroom) and document it. Keep append as the hot path.
- [ ] Make `rewrite_history_file()` atomic (`tmp = path.with_suffix(path.suffix + ".tmp")`, write,
  `os.replace(tmp, path)`) and reuse it for `clear_*_history()`.
- [ ] Gzip rotation (`*.jsonl.1.gz`) is probably unnecessary once capped.

**Verification.** Copy the current histories to a temp dir, run the app, and confirm the line count
is capped with newest first. Kill the process during a compaction and confirm the original file is
intact.

---

## P1-7. RFC 5987 `filename*=` in `Content-Disposition` is ignored

**Evidence.** `filename_from_response()` (`:1389-1408`) does:

```python
msg = Message()
msg["Content-Disposition"] = disposition
filename = msg.get_filename()
```

`email.message.Message.get_filename()` returns only the `filename` parameter; it does not decode
`filename*=UTF-8''...`, which many servers send alone for non-ASCII names.

**Impact.** The download falls back to the URL basename or `download.bin`, losing the correct name
and extension — a PDF with a perfectly good UTF-8 name can land as `download.bin`.

**Suggested approach.** Add a small helper:

```python
def _filename_from_disposition(disposition: str) -> Optional[str]:
    msg = Message()
    msg["Content-Disposition"] = disposition
    raw = msg.get_param("filename*", header="content-disposition")
    if raw:
        charset, _, rest = raw.partition("''")
        value = unquote(rest, encoding=charset or "utf-8", errors="replace")
        if value:
            return value
    return msg.get_filename()
```

`get_param(..., unquote=True)` in newer Pythons handles RFC 2231 — test on the target versions
before relying on it. Keep `sanitize_filename()` downstream; it already handles `/`, control
characters, and length.

**Verification.** Unit test
`attachment; filename="fallback.pdf"; filename*=UTF-8''%E4%B8%AD%E6%96%87.pdf` → `中文.pdf`
(sanitized), plus only-`filename*=` and invalid-charset cases.

---

## P1-8. Custom path selection bypasses Tor's weighting and guard policy

**Evidence.** `choose_circuit_path()` (`:986-1047`) samples guards, middles, and exits uniformly
from `controller.get_network_statuses()`, filtering only on `running`, `valid`, `not badexit`,
`exit`, `fast`, and the keep-alive exit policy, then calls
`new_circuit(path=..., purpose="controller")`. There is no bandwidth weighting, no
`Guard`/`Stable`/`V2Dir` handling beyond `guard`, no family/subnet diversity check, no
`ExcludeNodes`/`StrictNodes` awareness, and `random.choice` is unseeded for path selection.

**Impact.** (a) Tor's path selection is bandwidth-weighted and enforces `EnforceDistinctSubnets` and
family constraints; a uniform pick biases toward slow relays and can select relays in the same /16
or family, an anonymity regression. (b) A pinned path cannot be re-optimized when a relay is
overloaded, so build timeouts are more likely. (c) `purpose="controller"` with an explicit path
also bypasses Tor's own `CircuitBuildTimeout` learning.

**Suggested approach.** Offer both modes, defaulting to Tor's own selection:

- [ ] Add `collector.path_selection: "tor" | "custom"` (`"tor"` default). In `"tor"` mode call
  `new_circuit(await_build=True, timeout=...)` without `path` and read the chosen path from
  `get_circuit()`. `reserved_exit_fingerprints` then matters only in custom mode; in tor mode,
  de-duplicate after the build and rebuild only if two slots share an exit (bounded attempts).
- [ ] In custom mode, weight by `desc.bandwidth`/`observed_bandwidth` and add basic diversity checks
  (distinct IPv4 /16, distinct `desc.family`).
- [ ] Document the trade-off in the README ("custom paths disable Tor's own weighting").

**Verification.** Unit test the weighting helper with synthetic candidates (no relay chosen twice,
weights within tolerance). Live: build 9 circuits per mode and compare median build time and exit
diversity.

---

# P2 — robustness, performance, diagnosability

## P2-1. 29 `except Exception` handlers, 10 of them bare `pass`, hide failures

**Evidence.** `tor-circuit-collector.py` contains 29 `except Exception` handlers. Ten are
immediately followed by `pass`; most of the rest swallow the error into a fallback assignment.
Examples: `:604` (`listener_ports = []`), `:661-669` (feature enable), `:694-718` (shutdown),
`:808-811` (session close), `:970` (`can_keepalive = True`), `:1114` (`raw = ""`), `:1363-1367`
(partial unlink), `:1454` (`return None`), `:1750` (color init), `:1889`/`:1920` (clipboard).

Some are legitimately best-effort (clipboard, colors, `close_circuit` on shutdown). Others mask
real problems: a failed `get_info("net/listeners/socks")` silently degrades to common ports, and a
failed `.part` unlink leaves an orphan that future `unique_path()` calls avoid forever.

**Suggested approach.** Add one bounded diagnostic sink:

```python
def _note(self, area: str, exc: Exception) -> None:
    self.append_debug(f"{area}: {self._short_error(exc)}")
```

where `append_debug` writes to an in-memory ring buffer surfaced by a new `D` key (and optionally
`--log-file`). Keep `pass` only where failure is genuinely irrelevant, with a comment
(`# optional: clipboard tool may be absent`). Prioritise `:604`, `:970`, `:1114`, `:1363`, `:1454`.

**Verification.** `grep -n "except Exception:" -A1` shows a comment or `_note`; running without a
clipboard tool records the missing tool in the debug viewer.

---

## P2-2. `relay_candidates()` refetches the full consensus on every build

**Evidence.** `replace_circuit()` → `choose_circuit_path()` (`:836`) → `relay_candidates()`
(`:987`), which performs `get_info("status/bootstrap-phase")` and
`list(get_network_statuses())` (`:945-947`) every time. Nine initial builds plus every keep-alive
rebuild each pay for a full consensus materialization.

**Status: DONE** — `relay_candidates()` now reuses a snapshot for `collector.relay_cache_ttl`
seconds (default `300`, `0` disables) while `_fetch_relay_candidates()` keeps the original logic and
its bootstrap/relay-count checks. The cache is guarded by `self.lock` and is stored outside
`control_lock`.

- [x] Cache relay candidates behind a TTL
- [x] Add `collector.relay_cache_ttl` to built-in defaults, config map, argparse, JSON, and README
- [x] Keep bootstrap and minimum-relay checks on every fetch
- [ ] Measure the fetch count for a 9-circuit cold start (needs a live Tor instance)

**Impact.** On a ~7k-relay consensus each call costs tens to hundreds of milliseconds of controller
work and object construction, repeated for near-simultaneous builds (bounded to 2 by
`build_semaphore`). It also holds `control_lock` while doing so, briefly delaying `attach_stream`.

**Suggested approach.** Cache with a bounded TTL:

```python
self._relay_cache: Tuple[float, List[RelayCandidate]] = (0.0, [])
...
def relay_candidates(self):
    now = time.monotonic()
    with self.lock:
        ts, cached = self._relay_cache
        if cached and now - ts < self.args.relay_cache_ttl:
            return cached
    candidates = self._fetch_relay_candidates()
    with self.lock:
        self._relay_cache = (now, candidates)
    return candidates
```

Add `collector.relay_cache_ttl` to `BUILTIN_DEFAULTS`, `CONFIG_KEY_MAP`, argparse, the JSON, and
the README (config-sync rule). Invalidate on `EventType.NS` if subscribed; otherwise TTL suffices,
and the bootstrap/`len(candidates) < 3` checks still run on fetch.

**Verification.** Count `_fetch_relay_candidates()` calls; nine builds within the TTL should
produce one fetch.

---

## P2-3. Control-port probing is serial with per-port connect timeouts

**Evidence.** `connect_controller()` (`:634-643`) loops `Controller.from_port(...)` +
`authenticate(...)` sequentially over `_candidate_control_ports()`: base, configured,
±`control_port_scan_radius` (default 10 → 20 ports), then `9051`/`9151` — about 23 ports. Each
failing attempt waits on the TCP connect, and the app's `port_probe_timeout` is used only by
`_tcp_connectable()` for SOCKS, not here.

**Status: DONE** — `connect_controller()` now pre-probes every candidate with
`_tcp_probe_many()` (a `ThreadPoolExecutor` with at most 8 workers) using
`tor.port_probe_timeout`, fails fast with one bounded error when nothing answers, and only calls
`Controller.from_port()`/`authenticate()` for ports that accepted a connection. The status line is
set to `Probing N control port(s)...` before the probe. Wall-clock numbers are not yet measured
against a live system.

- [x] Parallel TCP pre-probe before any Stem connection attempt
- [x] Reuse `tor.port_probe_timeout` for the probe
- [x] Preserve candidate ordering and authenticate only reachable ports
- [x] Report a single bounded error when no control port is listening
- [ ] Compare startup time with Tor stopped against the pre-change baseline

**Impact.** With Tor down or on the wrong host, startup can block for tens of seconds before the
error appears, with a frozen (not yet drawn) TUI.

**Suggested approach**

- [x] Pre-filter with a parallel TCP probe (`_tcp_connectable(host, port, timeout)` in a
  `ThreadPoolExecutor(max_workers=8)`), then authenticate only against ports that accepted,
  preserving original ordering. This reuses `port_probe_timeout`.
- [ ] Pass `timeout=` to `Controller.from_port` if the Stem version supports it, or set the default on
  `stem.socket.BaseSocket`; otherwise the parallel probe is the main win.
- [x] Set `self.message` to "probing control port 9051…" and draw one frame before blocking. (message set before the probe; no separate frame is drawn)

**Verification.** With Tor stopped, compare startup time before/after; expect roughly
`ceil(23/8) * port_probe_timeout` instead of the sum of connects.

---

## P2-4. `draw_ui()` re-acquires `app.lock` per slot and snapshots shallowly

**Evidence.** `draw_ui()` takes `app.lock` for `slots_snapshot = [slot for slot in app.slots]`
(`:2000-2002`), then enters `with app.lock` **again** per slot (`:2017-2033`). `Slot` is mutable, so
the snapshot is a list of live objects and fields can change between reads (e.g. `download`
replaced by `replace_circuit()`), producing rows that mix pre- and post-reset values.

**Impact.** Torn rows (`status` says `building` while `circuit_id` still shows the old circuit) and
needless lock churn on every redraw (up to 9 × 2 acquisitions per frame). Not a crash, but it
undermines the consistent-row expectation and complicates reasoning.

**Suggested approach.** Copy under a single acquisition into immutable views (or build the existing
`table_rows` inside the lock), then render outside it:

```python
with app.lock:
    message = app.message
    rows = [self._slot_view(slot) for slot in app.slots]
```

`draw_ui` runs only on the UI thread, so one acquisition suffices.

**Verification.** Replace a circuit during a download and confirm no row shows a stale circuit id
with a new status; `grep -n "with app.lock" tor-circuit-collector.py` shows one acquisition in
`draw_ui`.

---

## P2-5. Truncation is character-based, not display-width-based

**Evidence.** `add_line()` uses `text.ljust(max(0, width - 1))` and `addnstr(...)` with a character
count (`:1596-1599`). `fit_table_cell()` slices by `len(text)` (`:1602-1611`).
`add_circuit_row_colored()` computes offsets with `len(prefix)` (`:1797-1824`). Relay nicknames,
filenames, and error strings can contain wide CJK characters or combining marks.

**Impact.** Columns drift and the status/suffix colour overlay lands on the wrong cells when a
filename or error contains wide characters. `addnstr` counts characters, so the right edge can wrap
(`curses.error`) when display width exceeds the terminal even though character count does not;
`add_line` does not catch that.

**Suggested approach.** Add `display_width(text)` via `unicodedata.east_asian_width` (`W`/`F` → 2,
combining marks → 0) and `truncate_display(text, width)`; use them in `fit_table_cell()` and
`add_circuit_row_colored()`. Wrap `addnstr` in `try/except curses.error` in `add_line()`. Keep an
ASCII fast path (`text.isascii()`) so the common case does not pay for `unicodedata`.

**Verification.** `display_width("中文") == 4`; `truncate_display("中文abc", 5)` fits;
`fit_table_cell` output has display width equal to `width`. Manual: a CJK filename renders aligned.

---

## P2-6. Downloads lack `Accept-Encoding: identity`, a size cap, retry, and free-space check

**Evidence.** `session.get(url, stream=True, timeout=..., headers={"Connection": "keep-alive"})`
(`:1302-1307`). `requests` sends `Accept-Encoding: gzip, deflate` by default and `iter_content`
decompresses, so `state.bytes_done` counts decompressed bytes while `Content-Length` is the
compressed size — progress can exceed 100% or stall. There is no maximum size, no retry/backoff, and
no `shutil.disk_usage()` check. `Content-Length` is parsed with `str.isdigit()` (`:1310`), which
accepts non-ASCII digits and is not a length validator.

**Impact.** Wrong progress and ETA for compressed content; a hostile or misconfigured server can
fill the disk; a transient read timeout fails the whole transfer despite a 120 s
`download_read_timeout`; disk-full surfaces as an opaque `OSError`.

**Suggested approach**

- [ ] Send `Accept-Encoding: identity` on downloads (and keep-alives, where only status matters), or
  read `Content-Encoding` and suppress the percentage when it is not `identity`.
- [ ] Add `downloads.max_bytes` (0 = unlimited), checked against `Content-Length` and enforced while
  streaming.
- [ ] Add `downloads.retries` (default 1–2) with exponential backoff for `requests.ConnectionError` /
  `Timeout`, reusing the drop-and-recreate pattern from `keepalive_once()` (`:1188-1206`). Never
  retry after `cancel_requested`.
- [ ] Check `shutil.disk_usage(self.output_dir).free` against `content_length` plus a margin and fail
  fast with a clear message.
- [ ] Parse `Content-Length` with `int()` in `try/except ValueError`, rejecting negatives.

**Verification.** Unit test size/space checks with stubs. Live: a gzip-served URL shows progress
consistent with the final file size.

---

## P2-7. `apt-install-tor.sh` refinements

**Evidence**

- `apt-get update` runs at `:71` when prerequisites are missing and again at `:113` — two metadata
  refreshes per invocation.
- `latest_official_version()` (`:115-124`) takes the first `apt-cache madison` line matching
  `deb.torproject.org/torproject.org` and assumes the first is highest. `madison` ordering is not a
  contractual guarantee, and the `awk` match on `$3` is a substring match.
- The upgrade path removes `tor`/`tor-geoipdb` (`:166`) before installing (`:170`), so the daemon is
  absent in between; `apt-get install` can usually replace a held package without `remove`.
- `--allow-downgrades` is unconditional, so it would silently downgrade if the "first" line were
  older.
- `TOR_KEY_URL` is verified by full fingerprint (good), but `curl` lacks `--proto '=https'`.

**Impact.** Slower runs, a small chance of installing the wrong version, and unnecessary daemon
downtime. This script runs as root, so correctness matters more than usual.

**Suggested approach**

- [ ] Keep one `apt-get update` after the repo file is installed (install prerequisites first, then
  write the repo and update once).
- [ ] Select the candidate from `apt-cache policy` for the `deb.torproject.org` origin, or sort
  `madison` output with `dpkg --compare-versions` and take the max (`sort -V` is better than "first
  line" but imperfect for Debian versions).
- [ ] Prefer installing without the preceding `remove`, falling back to `remove` only on a
  hold/conflict error; if `remove` stays, log the downtime.
- [ ] Pass `--allow-downgrades` only when `dpkg --compare-versions "$latest" lt "$installed"`, and log
  it loudly.
- [ ] Add `--proto '=https' --tlsv1.2` to `curl` and consider
  `apt-get -o DPkg::Lock::Timeout=60` to tolerate concurrent unattended-upgrades.

**Verification.** `shellcheck -x`, `bash -n`, and `apt-get -s` in a container. Do **not** run the
real installer as a routine test (per `AGENTS.md`).

---

# P3 — maintainability and developer experience

## P3-1. Split the 3,218-line module

The file mixes constants, dataclasses, controller/circuit logic, HTTP/download logic, history
persistence, filename heuristics, table layout, curses widgets, key decoding, config parsing, and
`main()`. Proposed split (keep `tor-circuit-collector.py` as the executable entry point so the
launcher and README stay valid):

```text
tcc/
  config.py      BUILTIN_DEFAULTS, CONFIG_KEY_MAP, parse_args, validators
  models.py      Slot, DownloadState, RelayCandidate, KeyEvent
  tor.py         controller connection, port discovery, relay candidates, path selection
  downloads.py   session management, download worker, filename helpers
  history.py     JSONL load/append/compact, URL normalization
  ui/
    layout.py    widths, fit/truncate, add_line
    views.py     draw_ui, viewers, prompts
    keys.py      read_key_event, ctrl-sequence parsing
  app.py         TorCircuitCollector orchestration
```

Move pure helpers first (`sanitize_filename`, `human_bytes`, `elapsed_text`,
`normalize_keepalive_urls`, `compute_table_widths`), keep public names re-exported, and run
`py_compile` + `--help` after each move. Keep the lazy `ensure_dependencies()` pattern: `requests`
and `stem` must stay out of module import time.

## P3-2. Add a test suite and CI

There is no test suite or CI (`AGENTS.md` says so). Highest-value, Tor-free tests:

- [ ] `sanitize_filename()`: traversal (`../x`), control chars, Windows-reserved names, multi-suffix
  archives (`.tar.gz`), length overflow, extensionless input.
- [ ] `validate_extension()`, `extension_from_url()` (query params), `apply_extension_hint()`.
- [ ] `unique_path()` / the new reservation helper: concurrency, `.part` collisions, suffixes.
- [ ] `normalize_history_url()` (new), history dedup, compaction, atomic rewrite (temp dir).
- [ ] `_parse_listener_ports()`, `_coerce_port_list()`, `_unique_ports()`.
- [ ] `compute_table_widths()` at 80/40/200 columns; `display_width()`.
- [ ] `_parse_ctrl_digit_sequence()`, `key_to_circuit_number()`.
- [ ] Config precedence: defaults < JSON < CLI; relative `output_dir` from config vs CLI.

Use stdlib `unittest` plus `unittest.mock` for `requests`/`stem`. Structure as `tests/test_*.py`;
add a `Makefile`/`nox` target and a CI workflow running `py_compile`, `--help`, `bash -n`,
`shellcheck`, and `python -m unittest discover`. Importing `tor-circuit-collector.py` from tests
needs a hyphen-free name: use `importlib.util.spec_from_file_location`, or (better) do P3-1 and
import `tcc.*`.

## P3-3. Assorted polish

- [ ] **`--version`**: none exists; `USER_AGENT = "tor-circuit-collector/1.0"` is the only version
  string. Add `__version__`, a `--version` action, and build the UA from it.
- [ ] **Typing**: `Dict`/`List`/`Set`/`Tuple` from `typing` are used throughout; with
  `from __future__ import annotations`, built-in generics work on 3.8 and read better. Not worth a
  mass rewrite unless doing P3-1.
- [ ] **Stale comments**: `monitor_loop()` says "so Tor is not hit with five simultaneous requests"
  (`:727`) but nine slots are the default; `build_semaphore` is 2. Fix the wording.
- [ ] **`self.history_limit`** is hard-coded 2000; expose as `history.limit` for symmetry.
- [ ] **`DownloadState.filename`** is mutated from the worker (`:1315`) without the lock; set it under
  `self.lock` or treat it as worker-owned and copy on read.
- [ ] **`append_slot_output()`** trims to 1000 lines by list copy (`:230-232`); use
  `collections.deque(maxlen=1000)` to avoid the O(n) copy per progress line.
- [ ] **`keepalive_urls`** in the shipped config includes `google.com`, `facebook.com`, etc. — third-
  party requests on every circuit. Document that keep-alive traffic goes to these hosts and prefer
  the three Tor/ip-echo defaults for privacy. Configuration note, not a code bug.
- [ ] **`.gitignore`**: add `*.part`, `*.tmp`, and the history glob from P0-2.
- [ ] **README**: the key table uses `c` for "continue" in the session viewer and `c`/`C` to clear in
  the history viewer; that overload is easy to misread. Add a "Security" section covering config
  permissions, history sensitivity, and cookie auth.

---

# Ordering A — security and data-safety first (default)

1. [ ] **P0-1, P0-2** (untrack secret + histories, ignore them, rotate password) — minutes, and stops
   ongoing exposure. Do these first even if nothing else is scheduled.
2. [ ] **P0-3** (atomic filename reservation) and **P0-4** (`secrets`) — small, contained, high value.
3. [ ] **P1-1, P1-2, P1-3, P1-5** — small correctness fixes that unblock reliable testing.
4. [ ] **P1-4, P1-6, P1-7** — download/history correctness; decide explicitly whether true resume is
   in scope, and update the README either way.
5. [ ] **P2-1 through P2-5** — diagnosability and UI correctness.
6. [ ] **P1-8** — the path-selection decision needs a product call (Tor-managed vs custom); implement
   the default-preserving option first.
7. [ ] **P2-6, P2-7, P3-2** — hardening and tests.
8. [ ] **P3-1, P3-3** — refactor and polish, ideally after tests exist.

# Ordering B — performance and correctness first (alternative)

The same 22 findings with the same IDs, sequenced differently. Use this when the tool
already works well enough day to day and the pain is *responsiveness, download efficiency,
and unpredictable behaviour* rather than secret exposure — for example a long-lived
personal install on a single-user machine, where the config file is not shared and the
repository has never been pushed anywhere.

Ordering A optimizes for "stop any ongoing disclosure, then everything else." Ordering B
optimizes for "make the hot paths fast and correct, then close the exposure." Both end at
the same place; they differ in what a user notices in the first hour.

## Which ordering fits

| Situation | Use |
|---|---|
| Repository is pushed/shared, or the machine has other users | **A** — P0-1/P0-2 are not deferrable there |
| Multi-day sessions, large downloads, many rebuilds | **B** — P1-4/P2-2/P2-6 dominate the experience |
| Circuits build slowly or keep timing out | **B**, with P1-8 early |
| Startup feels dead for many seconds when Tor is down | **B**, P2-3 early |
| Just want the smallest safe diff before a release | **A** |
| Refactoring or adding features next | **B**, after P3-2 lands |

## Step 0 — measure before changing anything

Neither ordering should start with a change. The performance items below are only worth
doing if the cost is real on this machine, and two of the claims in this document are
explicitly unmeasured (P2-2 and P2-3 depend on consensus size and connect timeouts).

1. [ ] Land the **P2-1** debug sink first, but only the in-memory ring buffer and a `D` viewer.
   It is small, it is a prerequisite for seeing retries/fallbacks, and it costs one key.
2. [ ] Add the **P3-2** helper tests for the pure functions (`sanitize_filename`,
   `compute_table_widths`, `_parse_listener_ports`, `normalize_keepalive_urls`). These are
   the functions P0-3, P1-5, P2-5 and P3-1 will touch; without them the refactors below are
   unverified.
3. [ ] Record a baseline with the same units and the same operation, and keep it: time to first
   usable table with Tor up, time to error with Tor down, 9-circuit cold build wall time,
   consensus-fetch count for those 9 builds, and download throughput + `bytes_done` accuracy
   on one large range-capable URL and one gzip-served URL. Compare against that baseline
   only — not against a number quoted from this document.

## Step 1 — bandwidth and build-time wins (no behaviour change)

These four are independent, small, and directly reduce time-on-network. They are the
highest ratio of user-visible improvement to risk in the whole list.

1. [ ] **P1-4** true resume: `Range` + `Content-Range` validation and `"r+b"`/seek instead of
   `"wb"`. A circuit that drops at 90% currently re-downloads 100%; this is the single
   largest bandwidth win available.
2. [ ] **P2-2** cache `relay_candidates()` behind a TTL. Nine builds today pay for nine
   `get_network_statuses()` materializations; this collapses them to one.
3. [ ] **P2-6** `Accept-Encoding: identity` (removes decompression CPU and makes progress
   honest), plus the `Content-Length` parse hardening. Defer the `max_bytes`/retries/
   disk-space parts of P2-6 to Step 2 if you want the smaller diff.
4. [ ] **P2-3** parallel control-port pre-probe so a missing Tor fails in one probe round
   instead of a serial chain.

After this step, re-take the Step 0 baseline. If P2-2 and P2-3 show no measurable change,
say so and leave them in as documentation-only or drop them — do not report an improvement
that was not measured.

## Step 2 — correctness on the hot path

1. [ ] **P0-3** atomic filename reservation. Listed as P0 for data loss, and it belongs here
   too: it is on every download's critical path and it is the only finding that can destroy
   a file.
2. [ ] **P1-2** `self.sessions` under `self.lock`, with a single `discard_session()` helper. A
   stale/closed session is a retry, and a retry is a full re-request.
3. [ ] **P1-3** bounded thread registry + bounded join in `shutdown()`. Long sessions are the
   scenario this ordering is for, so unbounded growth matters more here than under A.
4. [ ] **P1-6** history compaction at startup and atomic rewrite. Startup parses the entire
   history before the UI appears; that is pure latency on every launch.
5. [ ] **P1-5** real URL normalization, then **P1-7** `filename*=` decoding, then **P1-1**
   paired boolean flags (or raise the floor to 3.9 — decide, do not leave it contradictory).

## Step 3 — latency, UI correctness, and hardening

1. [ ] **P1-8** path selection: add `path_selection` with `"tor"` as the default so Tor's own
   weighted selection and subnet/family rules apply, keeping the current custom picker
   available. This is a product decision; in this ordering it comes after the cache because
   P2-2 and P1-8 touch the same code path.
2. [ ] **P2-4** single-lock immutable snapshot in `draw_ui()`. Removes torn rows and halves the
   lock traffic per frame — relevant because redraws happen every `status_interval`.
3. [ ] **P2-5** display-width truncation, so a CJK filename or error cannot shift columns or
   raise `curses.error` at the right edge.
4. [ ] **P2-6** remainder: `max_bytes`, retry with backoff, and the free-space precheck.
5. [ ] **P2-7** `apt-install-tor.sh`: single `apt-get update`, reliable version selection,
   avoid the remove-then-install downtime, and conditional `--allow-downgrades`.

## Step 4 — close the security exposure

Deliberately last in this ordering, and only defensible if the repository is private,
unpushed, and single-user. These are the same three actions as Ordering A step 1 plus P0-4.

1. [ ] **P0-4** `secrets` for the SOCKS tokens (small, and it is a credential, so do not let it
   slip past the end of the work).
2. [ ] **P0-1** untrack the config, ignore it, `chmod 600`, switch the example to cookie auth,
   and rotate the password.
3. [ ] **P0-2** untrack both history files and ignore them. The Step 2 compaction work (P1-6)
   makes this cleaner, which is why it follows.

If at any point before Step 4 the repository is pushed, shared, or accessed by another
user, stop and run Ordering A step 1 immediately instead of continuing.

## Step 5 — structure

1. [ ] **P3-2** expand the test suite beyond the Step 0 helpers to the download/history paths.
2. [ ] **P3-1** module split, now backed by tests and by the measurement harness.
3. [ ] **P3-3** polish (`--version`, `deque` for session output, comment fixes, README security
   section).

## Ordering A vs Ordering B at a glance

| Finding | A position | B position | Note | Done |
|---|---|---|---|---|
| P0-1 config secret | 1 | 4 | Non-negotiable if the repo is shared | ⬜ |
| P0-2 tracked histories | 1 | 4 | Same caveat | ⬜ |
| P0-3 filename race | 2 | 2 | Both orderings treat this as urgent | ⬜ |
| P0-4 `random` tokens | 2 | 4 | One-line change; do not defer past Step 3 | ⬜ |
| P1-1 Python 3.8 flags | 3 | 2 | Blocks startup on 3.8 in both | ⬜ |
| P1-2 session locking | 3 | 2 | | ⬜ |
| P1-3 thread growth | 3 | 2 | | ⬜ |
| P1-4 true resume | 4 | 1 | Largest bandwidth win | ✅ |
| P1-5 URL normalization | 3 | 2 | | ⬜ |
| P1-6 history compaction | 4 | 2 | Startup latency | ⬜ |
| P1-7 `filename*=` | 4 | 2 | | ⬜ |
| P1-8 path selection | 6 | 3 | Needs a product decision either way | ⬜ |
| P2-1 debug sink | 5 | 0 | Prerequisite for measuring in B | ⬜ |
| P2-2 consensus cache | 5 | 1 | Unmeasured until Step 0 baseline | ✅ |
| P2-3 control-port probe | 5 | 1 | Unmeasured until Step 0 baseline | ✅ |
| P2-4 UI snapshot | 5 | 3 | | ⬜ |
| P2-5 display width | 5 | 3 | | ⬜ |
| P2-6 download headers/limits | 7 | 1 then 3 | Split across two steps in B | ⬜ |
| P2-7 apt installer | 7 | 3 | | ⬜ |
| P3-1 module split | 8 | 5 | After tests in both | ⬜ |
| P3-2 tests/CI | 7 | 0 then 5 | Seeded in Step 0 of B | ⬜ |
| P3-3 polish | 8 | 5 | | ⬜ |

# What was verified, and what was not

Verified at review time: `py_compile`, `--help`, `bash -n`, `shellcheck` (exit 0), and
`git diff --check`; the tracking status and modes of the config and history files; that the config
contains a non-empty control password (value never read into output); the existence and line
numbers cited above; and the `unique_path()` race in an isolated harness.

Not verified (needs live systems this review did not touch): real Tor connectivity, circuit build
behaviour and timings, `STREAM` attachment semantics across Stem versions, APT installer
execution, and curses rendering in a real terminal. The P1-8 and P2-6 recommendations in particular
should be validated against the user's Tor version before changing defaults. No files other than
this document were modified, and no history, download, or config content was altered.
