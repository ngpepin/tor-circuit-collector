# AGENTS.md

This file is the repository-level handoff for automated coding agents. It
applies to the entire repository.

## Project summary

`tor-circuit-collector` is a Python `curses` application that maintains up to
nine controller-owned Tor circuits and routes a selected HTTP(S) download over
a chosen circuit. It uses Stem for Tor control events and Requests with SOCKS
support for network traffic.

The repository also includes a Bash installer for keeping the Tor daemon and
its GeoIP package on the current stable Tor Project APT release.

## Important files

- `tor-circuit-collector.py`: application, TUI, Tor controller integration,
  circuit lifecycle, downloading, filename handling, and persistent history.
- `tor-circuit-collector.json`: runtime configuration. Treat control
  authentication values as secrets.
- `tor-circuit-collector.sh`: location-independent Python launcher.
- `apt-install-tor.sh`: privileged, networked APT installer/updater for `tor`
  and `tor-geoipdb`.
- `README.md`: user documentation and behavioral reference.
- `README.pdf`: binary documentation artifact. There is no documented local
  regeneration command; do not rewrite it unless the user specifically asks.
- `tor-circuit-collector.success-history.jsonl` and
  `tor-circuit-collector.failed-history.jsonl`: live user history, not fixtures.
- `downloads/`: live downloaded content, not test data.

## Working-tree safety

- Expect the worktree to contain user changes and runtime-generated data.
- Inspect `git status --short` before and after work.
- Never discard, normalize, truncate, or reformat the JSONL histories.
- Do not alter files in `downloads/` unless the user explicitly requests it.
- Do not regenerate `README.pdf` merely because `README.md` changed.
- Do not commit, push, reset, or clean the repository unless requested.
- Keep changes scoped to the task; preserve unrelated modifications.

## Secrets and sensitive output

- `tor-circuit-collector.json` may contain a Tor ControlPort password. Do not
  quote it in responses, logs, tests, diffs, or command output.
- Prefer cookie authentication in examples. Use placeholder values when a
  password example is unavoidable.
- Tor exit addresses, requested URLs, filenames, and history entries may be
  sensitive. Do not print or transmit them unless needed for the requested
  diagnosis.
- Never use the collector's configured SOCKS endpoint for unrelated traffic.

## Architecture and invariants

### Configuration

- Defaults are defined in `BUILTIN_DEFAULTS`.
- `CONFIG_KEY_MAP` maps nested JSON keys to `argparse` destinations.
- Precedence is built-in defaults, then JSON config, then CLI flags.
- A config-relative output directory resolves relative to the config file;
  a CLI-relative output directory resolves relative to the caller's cwd.
- Keep the config JSON, CLI flags, built-in defaults, and README examples in
  sync when adding or renaming an option.
- The TUI uses single-digit slot selection, so the supported circuit count is
  one through nine.

### Dependency loading

- Requests and Stem are intentionally imported lazily in
  `ensure_dependencies()` so `--help` and argument errors work before optional
  Python dependencies are installed. Preserve that behavior.

### Threading and Tor control

- `self.lock` protects shared application and slot state.
- `self.control_lock` serializes short Stem control-port operations.
- Never hold `self.control_lock` while waiting for circuit construction,
  performing HTTP requests, sleeping, or doing filesystem I/O. Stream event
  handling must remain able to attach SOCKS streams while circuits build.
- `self.build_semaphore` intentionally limits concurrent circuit builds.
- `Slot.io_lock` protects per-slot download file/session operations.
- Event callbacks must remain fast and must not perform blocking network work.
- Preserve the reservation of exit fingerprints while parallel builds choose
  paths; this prevents slots from selecting the same planned exit.
- On startup the app sets `__LeaveStreamsUnattached=1` so it can attach streams
  to selected circuits. Shutdown must restore the previous setting.
- Unless `leave_circuits` is enabled, shutdown should close controller-created
  circuits when safe.
- Each slot uses unique SOCKS authentication credentials. The username-to-slot
  mapping is what lets Stream events bind a request to its chosen circuit.

### Circuit and bootstrap behavior

- A listening control port does not mean Tor is ready. Circuit selection needs
  a fully bootstrapped client with usable consensus and relay descriptors.
- Distinguish collector failures from Tor bootstrap, consensus, SOCKS, and
  destination failures during diagnosis.
- Port discovery should prefer configured ports but retain nearby/common-port
  fallback behavior.
- Keep circuit replacement and interrupted-download resume state consistent:
  do not lose `pending_resume_url`, leak an old Requests session, or resume a
  manually cancelled transfer.

### Downloads and history

- Only absolute HTTP and HTTPS URLs are supported.
- Downloads stream to a `.part` file and move atomically to the final filename
  after success.
- Never overwrite an existing final file; preserve numeric suffix behavior.
- Preserve filename sanitization, maximum-length handling, and extension
  inference from Content-Disposition, URL, and Content-Type.
- Failed partials are deleted unless `keep_partials` is enabled.
- Persistent histories are JSON Lines: one complete JSON object per line.
- A later success suppresses/removes matching failed-history entries.
- Repeated unresolved failures retain only the most recent visible failure for
  a normalized URL.

### Terminal UI

- Rendering is in-place through `curses`; avoid ordinary stdout/stderr writes
  after the UI starts.
- Keep key handling consistent across the main screen, selectors, prompts, and
  history/session viewers.
- Table columns must continue to degrade gracefully in narrow terminals.
- Long status and error strings must be clipped rather than break the layout.

### APT installer

- `apt-install-tor.sh` is intentionally Debian/Ubuntu-family specific.
- Keep strict Bash mode (`set -Eeuo pipefail`) and ShellCheck cleanliness.
- Continue using Tor Project's HTTPS repository and verify the complete primary
  signing-key fingerprint before installing a key.
- Version checks must use packages from `deb.torproject.org`, not a stale
  distribution `universe` package.
- Treat `tor` and `tor-geoipdb` as a pair: temporarily unhold when replacement
  is needed, install and verify both, then hold both again.
- Package removal is deliberately `remove`, not `purge`, so torrc and daemon
  state survive replacement.
- Do not run this installer as a routine test. It updates APT metadata, can
  remove packages, changes holds, and can restart or interrupt Tor. Run it only
  when the user authorizes system changes.

## Coding conventions

- Target the Python version documented in `README.md`; avoid introducing newer
  syntax without updating the stated requirement.
- Follow the existing type-annotated, dataclass-based Python style.
- Prefer small helpers and early validation over duplicating state transitions.
- Use `Path` for application filesystem paths and explicit UTF-8 for text data.
- Preserve actionable, bounded error messages through `_short_error()` where
  errors enter the TUI.
- Do not add a runtime dependency when the standard library or an existing
  dependency is sufficient.
- Bash scripts should quote expansions, use arrays for argument lists, use
  `mktemp` for temporary directories, and validate destructive targets.
- Update `README.md` when user-visible behavior, configuration, shortcuts, or
  installation steps change.

## Validation

Use the narrowest relevant checks first, then broaden as warranted.

Safe static checks:

```bash
python3 -m py_compile tor-circuit-collector.py
python3 tor-circuit-collector.py --help >/dev/null
bash -n tor-circuit-collector.sh apt-install-tor.sh
shellcheck -x tor-circuit-collector.sh apt-install-tor.sh
git diff --check
```

Notes:

- ShellCheck may not be installed; report that rather than installing it
  without permission.
- There is currently no committed automated test suite or CI configuration.
- Do not treat JSONL history or downloaded files as test inputs.
- Live TUI testing requires a real terminal plus an authorized local Tor
  instance. Do not start downloads merely to smoke-test the UI.
- Live Tor diagnostics should begin with read-only checks such as service
  status, bootstrap phase, listener ports, and recent Tor logs.
- Ask before restarting Tor when active collector sessions or downloads may be
  interrupted.

## Definition of done

- The requested behavior is implemented with unrelated user files preserved.
- Relevant static checks pass, or any unavailable/unsafe checks are reported.
- Config, CLI, and documentation remain consistent.
- Concurrency, stream attachment, shutdown restoration, and partial-file
  behavior remain intact for changes touching those paths.
- The final handoff states what changed, what was verified, and any live-system
  action the user still needs to take.
