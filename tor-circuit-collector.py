#!/usr/bin/env python3
"""
tor-circuit-collector.py

Maintain five controller-built Tor circuits, keep them warm with periodic
keep-alive requests, and let the user pick a numbered circuit for a streamed
HTTP(S) download. The terminal UI redraws in place using curses.

Requirements:
  python3 -m pip install stem "requests[socks]"

Tor must expose a SOCKS port and a ControlPort, for example in torrc:
  SocksPort 9050 IsolateSOCKSAuth
  ControlPort 9051
  CookieAuthentication 1

Typical use:
  python3 tor-circuit-collector.py
  python3 tor-circuit-collector.py --config /path/to/tor-circuit-collector.json
  python3 tor-circuit-collector.py --control-password 'secret'
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import random
import re
import signal
import string
import sys
import threading
import time
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlsplit

# Imported lazily so --help works even before dependencies are installed.
requests = None
CircStatus = None
StreamStatus = None
Controller = None
EventType = None


def ensure_dependencies() -> None:
    global requests, CircStatus, StreamStatus, Controller, EventType
    try:
        import requests as _requests
    except ImportError as exc:  # pragma: no cover - import guard for users
        print(
            "Missing dependency: requests. Install with: python3 -m pip install 'requests[socks]'",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    try:
        from stem import CircStatus as _CircStatus, StreamStatus as _StreamStatus
        from stem.control import Controller as _Controller, EventType as _EventType
    except ImportError as exc:  # pragma: no cover - import guard for users
        print("Missing dependency: stem. Install with: python3 -m pip install stem", file=sys.stderr)
        raise SystemExit(2) from exc

    requests = _requests
    CircStatus = _CircStatus
    StreamStatus = _StreamStatus
    Controller = _Controller
    EventType = _EventType


HTTP_CHUNK_SIZE = 256 * 1024
DEFAULT_KEEPALIVE_URL = "https://check.torproject.org/api/ip"
USER_AGENT = "tor-circuit-collector/1.0"


@dataclass
class DownloadState:
    url: str
    filename: str
    started_at: float = field(default_factory=time.monotonic)
    bytes_done: int = 0
    total_bytes: Optional[int] = None
    cancel_requested: bool = False
    done: bool = False
    error: Optional[str] = None


@dataclass
class Slot:
    index: int
    circuit_id: Optional[str] = None
    exit_fingerprint: str = ""
    exit_nickname: str = ""
    exit_ip: str = ""
    path_summary: str = ""
    created_at: Optional[float] = None
    status: str = "starting"
    last_keepalive_at: Optional[float] = None
    last_ok_at: Optional[float] = None
    last_error: str = ""
    rebuilding: bool = False
    keepalive_active: bool = False
    consecutive_keepalive_failures: int = 0
    bytes_since_last_url: int = 0
    last_url: str = ""
    session_output: List[str] = field(default_factory=list)
    download: Optional[DownloadState] = None
    io_lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class RelayCandidate:
    fingerprint: str
    nickname: str
    flags: Set[str]
    can_keepalive: bool = True

    def has_flag(self, flag: str) -> bool:
        return flag.lower() in self.flags

    @property
    def server_spec(self) -> str:
        return f"${self.fingerprint}"


class TorCircuitCollector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output_dir = Path(args.output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.message = "Starting Tor circuit collector..."

        self.controller: Optional[Controller] = None
        self.original_leave_streams_unattached: Optional[str] = None
        self.run_id = self._random_token(8)
        self.socks_password = self._random_token(12)
        self.slots: List[Slot] = [Slot(i) for i in range(1, args.circuits + 1)]
        self.sessions: Dict[int, requests.Session] = {}
        self.username_to_slot: Dict[str, int] = {
            self.slot_username(slot.index): slot.index for slot in self.slots
        }
        self.threads: List[threading.Thread] = []
        self.reserved_exit_fingerprints: Set[str] = set()

        # Build a couple of circuits at a time, but never hold control_lock
        # while waiting for circuit construction. Stream attachment must stay
        # responsive or SOCKS requests time out while circuits are building.
        self.build_semaphore = threading.BoundedSemaphore(2)

    @staticmethod
    def _random_token(length: int) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choice(alphabet) for _ in range(length))

    def slot_username(self, slot_index: int) -> str:
        return f"tcc-{self.run_id}-slot-{slot_index}"

    def set_message(self, text: str) -> None:
        with self.lock:
            self.message = text

    def append_slot_output(self, slot_index: int, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {text}"
        with self.lock:
            slot = self.slots[slot_index - 1]
            slot.session_output.append(line)
            if len(slot.session_output) > 1000:
                slot.session_output = slot.session_output[-1000:]

    def terminate_slot_download(self, slot_index: int) -> bool:
        with self.lock:
            slot = self.slots[slot_index - 1]
            state = slot.download
            if state is None or state.done or state.error:
                return False
            state.cancel_requested = True
            slot.status = "terminating"
        self.append_slot_output(slot_index, "Terminate requested by user.")
        self.set_message(f"Terminate requested for circuit {slot_index} download.")
        return True

    def start(self) -> None:
        self.connect_controller()
        monitor = threading.Thread(target=self.monitor_loop, name="tcc-monitor", daemon=True)
        monitor.start()
        self.threads.append(monitor)

    def connect_controller(self) -> None:
        try:
            controller = Controller.from_port(address=self.args.control_host, port=self.args.control_port)
            controller.authenticate(password=self.args.control_password)
            with self.control_lock:
                try:
                    current = controller.get_conf("__LeaveStreamsUnattached", None)
                    self.original_leave_streams_unattached = current
                except Exception:
                    self.original_leave_streams_unattached = None

                # We must own stream attachment to pin selected requests to selected circuits.
                try:
                    controller.enable_feature("EXTENDED_EVENTS")
                except Exception:
                    # Modern Tor versions have this always enabled; continue if it refuses.
                    pass
                controller.set_conf("__LeaveStreamsUnattached", "1")
                controller.add_event_listener(self.on_stream_event, EventType.STREAM)
                controller.add_event_listener(self.on_circ_event, EventType.CIRC)

            self.controller = controller
            self.set_message(
                f"Connected to Tor control port {self.args.control_host}:{self.args.control_port}. Building circuits..."
            )
        except Exception as exc:
            raise RuntimeError(
                "Could not connect/authenticate to Tor ControlPort. Ensure Tor is running with "
                "ControlPort enabled and use --control-password if needed. "
                f"Original error: {exc}"
            ) from exc

    def shutdown(self) -> None:
        self.stop_event.set()
        controller = self.controller
        if controller is not None:
            with self.control_lock:
                try:
                    controller.remove_event_listener(self.on_stream_event)
                    controller.remove_event_listener(self.on_circ_event)
                except Exception:
                    pass

                if not self.args.leave_circuits:
                    for slot in self.slots:
                        if slot.circuit_id:
                            try:
                                controller.close_circuit(slot.circuit_id, flag="IfUnused")
                            except Exception:
                                pass

                try:
                    if self.original_leave_streams_unattached is None:
                        controller.reset_conf("__LeaveStreamsUnattached")
                    else:
                        controller.set_conf(
                            "__LeaveStreamsUnattached", self.original_leave_streams_unattached
                        )
                except Exception:
                    pass

                try:
                    controller.close()
                except Exception:
                    pass

        for session in list(self.sessions.values()):
            try:
                session.close()
            except Exception:
                pass

    def monitor_loop(self) -> None:
        # Stagger initial builds a little so Tor is not hit with five simultaneous requests.
        while not self.stop_event.is_set():
            for slot in self.slots:
                if self.stop_event.is_set():
                    break
                with self.lock:
                    needs_build = slot.circuit_id is None and not slot.rebuilding
                    has_circuit = slot.circuit_id is not None
                    active_download = slot.download is not None and not slot.download.done and not slot.download.error
                    keepalive_due = (
                        has_circuit
                        and not active_download
                        and not slot.keepalive_active
                        and (
                            slot.last_keepalive_at is None
                            or time.monotonic() - slot.last_keepalive_at >= self.args.keepalive_interval
                        )
                    )

                if needs_build:
                    self._start_thread(
                        target=self.replace_circuit,
                        name=f"tcc-build-{slot.index}",
                        args=(slot.index, "initial/build replacement"),
                    )
                elif keepalive_due:
                    self._start_thread(
                        target=self.keepalive_once,
                        name=f"tcc-keepalive-{slot.index}",
                        args=(slot.index,),
                    )
            self.stop_event.wait(1.0)

    def _start_thread(self, target, name: str, args: Tuple = ()) -> None:
        t = threading.Thread(target=target, name=name, args=args, daemon=True)
        t.start()
        self.threads.append(t)

    def replace_circuit(self, slot_index: int, reason: str) -> None:
        slot = self.slots[slot_index - 1]
        planned_exit = ""
        acquired_build_slot = False

        with self.lock:
            if slot.rebuilding:
                return
            slot.rebuilding = True
            slot.status = "queued"
            slot.last_error = ""
            old_circuit = slot.circuit_id
            slot.circuit_id = None
            slot.exit_fingerprint = ""
            slot.exit_nickname = ""
            slot.exit_ip = ""
            slot.path_summary = ""
            slot.created_at = None

        if old_circuit and self.controller is not None:
            try:
                # This is quick, so it is fine to serialize it with other short
                # control-port actions.
                with self.control_lock:
                    self.controller.close_circuit(old_circuit, flag="IfUnused")
            except Exception:
                pass

        try:
            if self.controller is None:
                raise RuntimeError("controller is not connected")

            self.build_semaphore.acquire()
            acquired_build_slot = True

            with self.lock:
                slot.status = "building"

            # Choose the path before circuit construction. This performs only
            # short controller queries. The long wait below intentionally does
            # not hold self.control_lock, because the STREAM event handler must
            # be able to attach SOCKS streams while other circuits are building.
            path, planned_exit = self.choose_circuit_path(slot_index)

            with self.lock:
                if planned_exit:
                    self.reserved_exit_fingerprints.add(planned_exit)
                slot.path_summary = "planned: " + " > ".join(path)

            try:
                circuit_id = self.controller.new_circuit(
                    path=path,
                    await_build=True,
                    purpose="controller",
                    timeout=self.args.circuit_build_timeout,
                )
            except Exception as first_exc:
                if "551" not in str(first_exc):
                    raise
                circuit_id = self.controller.new_circuit(
                    path=path,
                    await_build=True,
                    purpose="general",
                    timeout=self.args.circuit_build_timeout,
                )

            # A quick lookup after the build completes.
            with self.control_lock:
                circuit = self.controller.get_circuit(circuit_id)

            exit_fingerprint = ""
            exit_nickname = ""
            exit_ip = ""
            path_summary = ""
            if getattr(circuit, "path", None):
                path_parts = []
                for fingerprint, nickname in circuit.path:
                    nickname = nickname or fingerprint[:8]
                    path_parts.append(nickname)
                path_summary = " > ".join(path_parts)
                exit_fingerprint, exit_nickname = circuit.path[-1]
                exit_nickname = exit_nickname or exit_fingerprint[:8]
                if exit_fingerprint:
                    try:
                        with self.control_lock:
                            exit_status = self.controller.get_network_status(exit_fingerprint)
                        exit_ip = str(getattr(exit_status, "address", "") or "")
                    except Exception:
                        exit_ip = ""

            now = time.monotonic()
            with self.lock:
                if planned_exit:
                    self.reserved_exit_fingerprints.discard(planned_exit)
                slot.circuit_id = str(circuit_id)
                slot.exit_fingerprint = exit_fingerprint or "?"
                slot.exit_nickname = exit_nickname or "?"
                slot.exit_ip = exit_ip or "?"
                slot.path_summary = path_summary or "built"
                slot.created_at = now
                slot.status = "ready"
                slot.rebuilding = False
                slot.consecutive_keepalive_failures = 0
                slot.last_error = ""

                # Do not immediately launch a keep-alive on a just-built circuit.
                # It can race with the remaining initial circuit builds and makes
                # the UI look like the collector stopped. The first keep-alive
                # will happen after keepalive_interval seconds.
                slot.last_keepalive_at = now
                slot.last_ok_at = now

            self.set_message(f"Circuit {slot.index} ready as Tor circuit {circuit_id}.")
        except Exception as exc:
            with self.lock:
                if planned_exit:
                    self.reserved_exit_fingerprints.discard(planned_exit)
                slot.rebuilding = False
                slot.status = "build failed"
                slot.last_error = self._short_error(exc)
            self.set_message(f"Circuit {slot.index} build failed: {self._short_error(exc)}")
        finally:
            if acquired_build_slot:
                try:
                    self.build_semaphore.release()
                except ValueError:
                    pass

    def relay_candidates(self) -> List[RelayCandidate]:
        if self.controller is None:
            raise RuntimeError("controller is not connected")

        parsed_keepalive = urlsplit(self.args.keepalive_url)
        keepalive_port = parsed_keepalive.port or (443 if parsed_keepalive.scheme == "https" else 80)

        # These control-port operations are quick. They can take the lock briefly,
        # unlike circuit construction, which may block for many seconds.
        with self.control_lock:
            bootstrap = self.controller.get_info("status/bootstrap-phase", "")
            statuses = list(self.controller.get_network_statuses())

        if bootstrap and "PROGRESS=100" not in bootstrap:
            raise RuntimeError(f"Tor is not fully bootstrapped yet: {bootstrap}")

        candidates: List[RelayCandidate] = []
        for desc in statuses:
            fingerprint = str(getattr(desc, "fingerprint", "") or "").strip().upper()
            nickname = str(getattr(desc, "nickname", "") or fingerprint[:8])
            flags = {str(flag).lower() for flag in getattr(desc, "flags", [])}

            if not fingerprint:
                continue
            if "running" not in flags or "valid" not in flags:
                continue
            if "badexit" in flags:
                continue

            can_keepalive = True
            exit_policy = getattr(desc, "exit_policy", None)
            if exit_policy is not None and hasattr(exit_policy, "can_exit_to"):
                try:
                    can_keepalive = bool(exit_policy.can_exit_to(port=keepalive_port))
                except Exception:
                    can_keepalive = True

            candidates.append(
                RelayCandidate(
                    fingerprint=fingerprint,
                    nickname=nickname,
                    flags=flags,
                    can_keepalive=can_keepalive,
                )
            )

        if len(candidates) < 3:
            raise RuntimeError("Tor does not have enough relay consensus entries yet; wait for bootstrap/descriptors")
        return candidates

    def choose_circuit_path(self, slot_index: int) -> Tuple[List[str], str]:
        candidates = self.relay_candidates()

        with self.lock:
            avoid_exits = {
                slot.exit_fingerprint.upper()
                for slot in self.slots
                if slot.index != slot_index and slot.exit_fingerprint and slot.exit_fingerprint != "?"
            }
            avoid_exits.update(self.reserved_exit_fingerprints)

        guards = [relay for relay in candidates if relay.has_flag("guard") and relay.has_flag("fast")]
        if not guards:
            guards = [relay for relay in candidates if relay.has_flag("guard")]

        exits = [
            relay
            for relay in candidates
            if relay.has_flag("exit")
            and relay.can_keepalive
            and relay.fingerprint not in avoid_exits
            and relay.has_flag("fast")
        ]
        if not exits:
            exits = [
                relay
                for relay in candidates
                if relay.has_flag("exit")
                and relay.can_keepalive
                and relay.fingerprint not in avoid_exits
            ]
        if not exits:
            # Last-resort fallback: preserving operation is better than spinning
            # forever if there are fewer available exits than slots.
            exits = [relay for relay in candidates if relay.has_flag("exit") and relay.can_keepalive]
        if not exits:
            exits = [relay for relay in candidates if relay.has_flag("exit")]

        middles = [relay for relay in candidates if relay.has_flag("fast") and not relay.has_flag("exit")]
        if not middles:
            middles = [relay for relay in candidates if not relay.has_flag("exit")]

        if not guards or not middles or not exits:
            raise RuntimeError("Could not find suitable Guard/Middle/Exit relay pools from Tor consensus")

        for _ in range(200):
            guard = random.choice(guards)
            exit_relay = random.choice(exits)
            middle_pool = [
                relay
                for relay in middles
                if relay.fingerprint not in {guard.fingerprint, exit_relay.fingerprint}
            ]
            if not middle_pool:
                continue
            middle = random.choice(middle_pool)
            return (
                [guard.server_spec, middle.server_spec, exit_relay.server_spec],
                exit_relay.fingerprint,
            )

        raise RuntimeError("Could not choose a unique three-hop circuit path")

    def on_circ_event(self, event) -> None:
        status = str(getattr(event, "status", ""))
        if status not in {str(CircStatus.CLOSED), str(CircStatus.FAILED), "CLOSED", "FAILED"}:
            return
        circuit_id = str(getattr(event, "id", ""))
        reason = getattr(event, "reason", "") or getattr(event, "remote_reason", "") or status
        with self.lock:
            for slot in self.slots:
                if slot.circuit_id == circuit_id:
                    slot.circuit_id = None
                    slot.status = "dropped"
                    slot.last_error = f"{status}: {reason}"
                    slot.created_at = None
                    slot.exit_ip = ""
                    self.set_message(f"Circuit {slot.index} dropped ({reason}); rebuilding.")
                    break

    def on_stream_event(self, event) -> None:
        status = str(getattr(event, "status", ""))
        if status not in {str(StreamStatus.NEW), str(StreamStatus.NEWRESOLVE), "NEW", "NEWRESOLVE"}:
            return

        username = self._event_kv(event, "SOCKS_USERNAME")
        stream_id = getattr(event, "id", None)
        if stream_id is None:
            return

        try:
            slot_index = self.username_to_slot.get(username or "")
            if slot_index is not None:
                with self.lock:
                    circuit_id = self.slots[slot_index - 1].circuit_id
                if circuit_id:
                    with self.control_lock:
                        self.controller.attach_stream(stream_id, circuit_id)  # type: ignore[union-attr]
                    return

            # Avoid breaking unrelated applications while __LeaveStreamsUnattached=1.
            with self.control_lock:
                self.controller.attach_stream(stream_id, 0)  # type: ignore[union-attr]
        except Exception as exc:
            self.set_message(f"Stream attach failed: {self._short_error(exc)}")

    @staticmethod
    def _event_kv(event, key: str) -> Optional[str]:
        # Stem exposes extra STREAM fields differently across versions. Try common forms,
        # then fall back to parsing raw control-protocol text.
        attr_names = [key.lower(), key.lower().replace("_", ""), key]
        for attr in attr_names:
            value = getattr(event, attr, None)
            if isinstance(value, str):
                return value

        for dict_attr in ("keyword_args", "kwargs", "_kwargs"):
            data = getattr(event, dict_attr, None)
            if isinstance(data, dict):
                value = data.get(key) or data.get(key.lower())
                if isinstance(value, str):
                    return value.strip('"')

        raw = ""
        for raw_attr in ("raw_content", "_raw_content"):
            candidate = getattr(event, raw_attr, None)
            try:
                raw = candidate() if callable(candidate) else candidate or ""
            except Exception:
                raw = ""
            if raw:
                break
        if not raw:
            raw = str(event)

        match = re.search(rf"\b{re.escape(key)}=(?:\"((?:\\.|[^\"])*)\"|(\S+))", raw)
        if not match:
            return None
        value = match.group(1) if match.group(1) is not None else match.group(2)
        return value.replace(r"\"", '"')

    def session_for_slot(self, slot_index: int) -> requests.Session:
        session = self.sessions.get(slot_index)
        if session is not None:
            return session
        username = quote(self.slot_username(slot_index), safe="")
        password = quote(self.socks_password, safe="")
        proxy = f"socks5h://{username}:{password}@{self.args.socks_host}:{self.args.socks_port}"
        session = requests.Session()
        session.proxies = {"http": proxy, "https": proxy}
        session.headers.update({"User-Agent": USER_AGENT})
        self.sessions[slot_index] = session
        return session

    def keepalive_once(self, slot_index: int) -> None:
        slot = self.slots[slot_index - 1]
        with self.lock:
            if slot.keepalive_active or slot.circuit_id is None:
                return
            if slot.download is not None and not slot.download.done and not slot.download.error:
                return
            slot.keepalive_active = True
            slot.status = "keep-alive"
            slot.last_keepalive_at = time.monotonic()

        try:
            with slot.io_lock:
                session = self.session_for_slot(slot_index)
                try:
                    response = session.head(
                        self.args.keepalive_url,
                        allow_redirects=True,
                        timeout=(self.args.connect_timeout, self.args.read_timeout),
                        headers={"Connection": "keep-alive"},
                    )
                    # Some endpoints reject HEAD; fall back to a tiny GET.
                    if response.status_code >= 400:
                        response.close()
                        response = session.get(
                            self.args.keepalive_url,
                            allow_redirects=True,
                            timeout=(self.args.connect_timeout, self.args.read_timeout),
                            headers={"Connection": "keep-alive"},
                        )
                    response.raise_for_status()
                    response.close()
                except requests.RequestException:
                    raise

            with self.lock:
                slot.status = "ready"
                slot.last_ok_at = time.monotonic()
                slot.last_error = ""
                slot.consecutive_keepalive_failures = 0
        except Exception as exc:
            rebuild = False
            with self.lock:
                slot.last_error = self._short_error(exc)
                slot.consecutive_keepalive_failures += 1
                rebuild = slot.consecutive_keepalive_failures >= self.args.max_keepalive_failures
                slot.status = "rebuilding" if rebuild else "ready"
            self.set_message(f"Circuit {slot.index} keep-alive failed: {self._short_error(exc)}")
            if rebuild:
                self.replace_circuit(slot_index, "keep-alive failures")
        finally:
            with self.lock:
                slot.keepalive_active = False
                if slot.circuit_id and slot.status == "keep-alive":
                    slot.status = "ready"

    def start_download(self, slot_index: int, url: str) -> None:
        url = url.strip()
        if not url:
            self.set_message("Download cancelled: empty URL.")
            return
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            self.set_message("Only absolute http:// and https:// URLs are supported.")
            return

        slot = self.slots[slot_index - 1]
        with self.lock:
            active = slot.download is not None and not slot.download.done and not slot.download.error
            if active:
                self.set_message(f"Circuit {slot_index} is already downloading.")
                return
            if slot.circuit_id is None:
                self.set_message(f"Circuit {slot_index} is not ready yet.")
                return
            slot.bytes_since_last_url = 0
            slot.last_url = url
            slot.session_output = []
            slot.download = None

        self.append_slot_output(slot_index, f"URL submitted: {url}")

        self._start_thread(
            target=self.download_worker,
            name=f"tcc-download-{slot_index}",
            args=(slot_index, url),
        )

    def download_worker(self, slot_index: int, url: str) -> None:
        slot = self.slots[slot_index - 1]
        guessed_name = self.filename_from_url(url)
        state = DownloadState(url=url, filename=guessed_name)
        with self.lock:
            slot.download = state
            slot.status = "downloading"
            slot.last_error = ""

        self.append_slot_output(slot_index, "Download session started.")

        part_path: Optional[Path] = None
        final_path: Optional[Path] = None
        try:
            with slot.io_lock:
                session = self.session_for_slot(slot_index)
                with session.get(
                    url,
                    stream=True,
                    timeout=(self.args.connect_timeout, self.args.download_read_timeout),
                    headers={"Connection": "keep-alive"},
                ) as response:
                    response.raise_for_status()
                    total_header = response.headers.get("Content-Length")
                    content_length = int(total_header) if total_header and total_header.isdigit() else None
                    name = self.filename_from_response(url, response.headers, guessed_name)
                    final_path = self.unique_path(self.output_dir / name)
                    part_path = final_path.with_suffix(final_path.suffix + ".part")
                    state.filename = final_path.name
                    state.total_bytes = content_length

                    self.append_slot_output(
                        slot_index,
                        f"HTTP {response.status_code}; writing to {final_path.name}; total {human_bytes(content_length)}.",
                    )

                    next_progress_log = 1024 * 1024

                    with part_path.open("wb") as fh:
                        for chunk in response.iter_content(chunk_size=HTTP_CHUNK_SIZE):
                            if self.stop_event.is_set():
                                raise RuntimeError("stopped")
                            if not chunk:
                                continue
                            with self.lock:
                                if state.cancel_requested:
                                    raise RuntimeError("terminated by user")
                            fh.write(chunk)
                            with self.lock:
                                state.bytes_done += len(chunk)
                                slot.bytes_since_last_url += len(chunk)
                                bytes_done = state.bytes_done
                            if bytes_done >= next_progress_log:
                                self.append_slot_output(slot_index, f"Downloaded {human_bytes(bytes_done)}...")
                                next_progress_log += 1024 * 1024
                    os.replace(part_path, final_path)

            with self.lock:
                state.done = True
                slot.status = "ready"
                slot.last_ok_at = time.monotonic()
                slot.last_error = ""
            self.append_slot_output(
                slot_index,
                f"Download complete: {final_path.name if final_path else state.filename} ({human_bytes(state.bytes_done)}).",
            )
            self.set_message(f"Circuit {slot_index} download complete: {final_path.name if final_path else state.filename}")
        except Exception as exc:
            if part_path is not None and part_path.exists() and not self.args.keep_partials:
                try:
                    part_path.unlink()
                except Exception:
                    pass
            with self.lock:
                state.error = self._short_error(exc)
                slot.status = "download failed"
                slot.last_error = state.error
            self.append_slot_output(slot_index, f"Download failed: {state.error}")
            self.set_message(f"Circuit {slot_index} download failed: {state.error}")

    @staticmethod
    def filename_from_url(url: str) -> str:
        path_name = Path(unquote(urlsplit(url).path)).name
        return sanitize_filename(path_name or "download.bin")

    def filename_from_response(self, url: str, headers, fallback: str) -> str:
        disposition = headers.get("Content-Disposition", "")
        if disposition:
            msg = Message()
            msg["Content-Disposition"] = disposition
            filename = msg.get_filename()
            if filename:
                return sanitize_filename(filename)
        return sanitize_filename(fallback or self.filename_from_url(url))

    @staticmethod
    def unique_path(path: Path) -> Path:
        path = path.with_name(sanitize_filename(path.name))
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if not candidate.exists() and not candidate.with_suffix(candidate.suffix + ".part").exists():
                return candidate
            counter += 1

    @staticmethod
    def _short_error(exc: Exception, limit: int = 120) -> str:
        text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
        return text[: limit - 1] + "…" if len(text) > limit else text


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    if name in {"", ".", ".."}:
        return "download.bin"
    return name[:180]


def human_bytes(value: Optional[int]) -> str:
    if value is None:
        return "?"
    amount = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def elapsed_text(start: Optional[float]) -> str:
    if start is None:
        return "-"
    seconds = max(0, int(time.monotonic() - start))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def ago_text(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    seconds = max(0, int(time.monotonic() - ts))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def download_summary(download: Optional[DownloadState]) -> str:
    if download is None:
        return "-"
    if download.error:
        return f"ERR {download.filename}: {download.error}"
    if download.done:
        return f"DONE {download.filename} ({human_bytes(download.bytes_done)})"
    pct = ""
    if download.total_bytes:
        pct = f" {download.bytes_done * 100.0 / download.total_bytes:5.1f}%"
    elapsed = max(0.001, time.monotonic() - download.started_at)
    rate = download.bytes_done / elapsed
    return (
        f"{download.filename}:{pct} "
        f"{human_bytes(download.bytes_done)}/{human_bytes(download.total_bytes)} "
        f"@ {human_bytes(int(rate))}/s"
    )


def add_line(stdscr, row: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if 0 <= row < height:
        stdscr.addnstr(row, 0, text.ljust(max(0, width - 1)), max(0, width - 1), attr)


def draw_ui(
    stdscr,
    app: TorCircuitCollector,
    prompt_slot: Optional[int],
    prompt_text: str,
    viewer_slot: Optional[int],
    viewer_scroll: int,
) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 12 or width < 80:
        add_line(stdscr, 0, "Terminal too small. Please use at least 80x12.", curses.A_BOLD)
        stdscr.refresh()
        return 0

    with app.lock:
        slots_snapshot = [slot for slot in app.slots]
        message = app.message

    title = (
        f"Tor Circuit Collector | q quit | 1-{app.args.circuits} download | "
        f"refresh {app.args.status_interval}s | output {app.output_dir}"
    )
    add_line(stdscr, 0, title[: width - 1], curses.A_BOLD)
    add_line(stdscr, 1, "─" * (width - 1))
    add_line(
        stdscr,
        2,
        "#  CircID   Exit relay           Exit IP         Age     Last OK  KA fail  KB URL  Status              Download",
        curses.A_BOLD,
    )

    split_row = height // 2 if viewer_slot is not None else height - 5
    table_bottom = max(4, split_row - 1)

    row = 3
    for slot in slots_snapshot:
        if row >= table_bottom:
            break
        with app.lock:
            circuit_id = slot.circuit_id or "-"
            exit_label = slot.exit_nickname or "-"
            if slot.exit_fingerprint and slot.exit_fingerprint != "?":
                exit_label = f"{exit_label}/{slot.exit_fingerprint[:8]}"
            exit_ip = slot.exit_ip or "-"
            age = elapsed_text(slot.created_at)
            last_ok = ago_text(slot.last_ok_at)
            failures = str(slot.consecutive_keepalive_failures)
            kb_since_url = f"{slot.bytes_since_last_url / 1024.0:.1f}"
            status = slot.status
            summary = download_summary(slot.download)
            path_summary = slot.path_summary
            last_error = slot.last_error

        line = (
            f"{slot.index:<2} {circuit_id:<8.8} {exit_label:<20.20} {exit_ip:<15.15} "
            f"{age:<7} {last_ok:<8} {failures:<7} {kb_since_url:<7} {status:<19.19} {summary}"
        )
        add_line(stdscr, row, line)
        row += 1
        if last_error and row < table_bottom:
            add_line(stdscr, row, f"   ↳ {last_error}"[: width - 1])
            row += 1
        if path_summary and app.args.show_paths and row < table_bottom:
            add_line(stdscr, row, f"   path: {path_summary}"[: width - 1])
            row += 1

    if viewer_slot is None:
        footer_row = height - 4
        add_line(stdscr, footer_row, "─" * (width - 1))
        add_line(stdscr, footer_row + 1, f"Status: {message}"[: width - 1])
        if prompt_slot is None:
            add_line(stdscr, footer_row + 2, "Press 1-5 to enter a download URL for that circuit, or q to quit.")
        else:
            prompt = f"URL for circuit {prompt_slot} (Enter submits, Esc cancels): {prompt_text}"
            add_line(stdscr, footer_row + 2, prompt[: width - 1], curses.A_BOLD)
        add_line(stdscr, footer_row + 3, "")
        stdscr.refresh()
        return 0

    # Lower-half session output viewer.
    add_line(stdscr, split_row - 1, "─" * (width - 1))
    with app.lock:
        selected = app.slots[viewer_slot - 1]
        lines = list(selected.session_output)
        selected_url = selected.last_url or "(no URL)"

    title = (
        f"Session output for circuit {viewer_slot} | URL: {selected_url} | "
        "c continue | t terminate | Up/Down scroll"
    )
    add_line(stdscr, split_row, title[: width - 1], curses.A_BOLD)

    body_top = split_row + 1
    body_bottom = height - 2
    body_height = max(0, body_bottom - body_top + 1)
    max_scroll = max(0, len(lines) - body_height)
    viewer_scroll = max(0, min(viewer_scroll, max_scroll))

    visible = lines[viewer_scroll : viewer_scroll + body_height]
    for idx in range(body_height):
        text = visible[idx] if idx < len(visible) else ""
        add_line(stdscr, body_top + idx, text[: width - 1])

    if lines:
        position = f"lines {viewer_scroll + 1}-{min(viewer_scroll + body_height, len(lines))} of {len(lines)}"
    else:
        position = "no session output yet"
    add_line(stdscr, height - 1, f"Status: {message} | {position}"[: width - 1])
    stdscr.refresh()
    return viewer_scroll


def curses_main(stdscr, app: TorCircuitCollector) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    prompt_slot: Optional[int] = None
    prompt_text = ""
    viewer_slot: Optional[int] = None
    viewer_scroll = 0
    next_draw = 0.0

    while not app.stop_event.is_set():
        now = time.monotonic()
        if now >= next_draw:
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = now + app.args.status_interval

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            break

        if key == -1:
            time.sleep(0.05)
            continue

        if viewer_slot is not None:
            if key in (ord("c"), ord("C"), 27):
                viewer_slot = None
                viewer_scroll = 0
                app.set_message("Session viewer closed.")
            elif key in (ord("t"), ord("T")):
                if not app.terminate_slot_download(viewer_slot):
                    app.set_message(f"Circuit {viewer_slot} has no active download to terminate.")
            elif key in (curses.KEY_UP, ord("k"), ord("K")):
                viewer_scroll = max(0, viewer_scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                viewer_scroll += 1
            elif key == curses.KEY_PPAGE:
                viewer_scroll = max(0, viewer_scroll - 10)
            elif key == curses.KEY_NPAGE:
                viewer_scroll += 10
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if prompt_slot is None:
            if key in (ord("q"), ord("Q")):
                break
            if ord("1") <= key <= ord(str(min(9, app.args.circuits))):
                value = key - ord("0")
                if 1 <= value <= app.args.circuits:
                    with app.lock:
                        slot = app.slots[value - 1]
                        active_download = (
                            slot.download is not None
                            and not slot.download.done
                            and not slot.download.error
                        )
                    if active_download:
                        viewer_slot = value
                        viewer_scroll = 0
                        curses.curs_set(0)
                    else:
                        prompt_slot = value
                        prompt_text = ""
                        curses.curs_set(1)
                    viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
                    next_draw = time.monotonic() + app.args.status_interval
            continue

        # URL prompt editing.
        if key in (27,):  # Esc
            prompt_slot = None
            prompt_text = ""
            curses.curs_set(0)
            app.set_message("Download prompt cancelled.")
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = time.monotonic() + app.args.status_interval
        elif key in (curses.KEY_ENTER, 10, 13):
            selected = prompt_slot
            url = prompt_text.strip()
            prompt_slot = None
            prompt_text = ""
            curses.curs_set(0)
            if selected is not None:
                app.start_download(selected, url)
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = time.monotonic() + app.args.status_interval
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            prompt_text = prompt_text[:-1]
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = time.monotonic() + app.args.status_interval
        elif 32 <= key <= 126:
            prompt_text += chr(key)
            viewer_scroll = draw_ui(stdscr, app, prompt_slot, prompt_text, viewer_slot, viewer_scroll)
            next_draw = time.monotonic() + app.args.status_interval


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


BUILTIN_DEFAULTS = {
    "circuits": 5,
    "socks_host": "127.0.0.1",
    "socks_port": 9050,
    "control_host": "127.0.0.1",
    "control_port": 9051,
    "control_password": None,
    "output_dir": ".",
    "status_interval": 5,
    "keepalive_interval": 60,
    "keepalive_url": DEFAULT_KEEPALIVE_URL,
    "max_keepalive_failures": 2,
    "circuit_build_timeout": 90,
    "connect_timeout": 20,
    "read_timeout": 30,
    "download_read_timeout": 120,
    "keep_partials": False,
    "leave_circuits": False,
    "show_paths": False,
}


CONFIG_KEY_MAP = {
    ("tor", "socks_host"): "socks_host",
    ("tor", "socks_port"): "socks_port",
    ("tor", "control_host"): "control_host",
    ("tor", "control_port"): "control_port",
    ("tor", "control_password"): "control_password",
    ("collector", "circuits"): "circuits",
    ("collector", "keepalive_url"): "keepalive_url",
    ("collector", "keepalive_interval"): "keepalive_interval",
    ("collector", "max_keepalive_failures"): "max_keepalive_failures",
    ("collector", "circuit_build_timeout"): "circuit_build_timeout",
    ("collector", "leave_circuits"): "leave_circuits",
    ("collector", "show_paths"): "show_paths",
    ("downloads", "output_dir"): "output_dir",
    ("downloads", "keep_partials"): "keep_partials",
    ("ui", "status_interval"): "status_interval",
    ("timeouts", "connect_timeout"): "connect_timeout",
    ("timeouts", "read_timeout"): "read_timeout",
    ("timeouts", "download_read_timeout"): "download_read_timeout",
}


def default_config_path() -> Path:
    """Return the default config path next to this Python script."""
    try:
        return Path(__file__).resolve().with_name("tor-circuit-collector.json")
    except NameError:
        return Path.cwd() / "tor-circuit-collector.json"


def load_config_file(config_path: Path) -> Dict[str, object]:
    """Load nested JSON config into argparse-compatible defaults."""
    if not config_path.exists():
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {config_path}: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"Could not read config file {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SystemExit(f"Config file {config_path} must contain a JSON object at the top level.")

    flattened: Dict[str, object] = {}
    for (section, key), dest in CONFIG_KEY_MAP.items():
        section_value = raw.get(section, {})
        if isinstance(section_value, dict) and key in section_value:
            flattened[dest] = section_value[key]

    # Resolve output_dir relative to the config file location, not the caller's cwd.
    output_dir = flattened.get("output_dir")
    if isinstance(output_dir, str) and output_dir and not Path(output_dir).expanduser().is_absolute():
        flattened["output_dir"] = str((config_path.parent / output_dir).resolve())

    return flattened


def merged_defaults(config_path: Path) -> Dict[str, object]:
    defaults = dict(BUILTIN_DEFAULTS)
    defaults.update(load_config_file(config_path))
    defaults["config"] = str(config_path)
    return defaults


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="path to tor-circuit-collector.json",
    )
    pre_args, _ = pre_parser.parse_known_args(argv)
    config_path = Path(pre_args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()

    defaults = merged_defaults(config_path)

    parser = argparse.ArgumentParser(
        description="Maintain Tor circuits and download URLs through selected circuits.",
        parents=[pre_parser],
    )
    parser.set_defaults(**defaults)
    parser.add_argument("--circuits", type=positive_int, help="number of circuits to maintain")
    parser.add_argument("--socks-host", help="Tor SOCKS host")
    parser.add_argument("--socks-port", type=int, help="Tor SOCKS port")
    parser.add_argument("--control-host", help="Tor control host")
    parser.add_argument("--control-port", type=int, help="Tor control port")
    parser.add_argument("--control-password", help="Tor control password, if not using cookie auth")
    parser.add_argument("--output-dir", help="directory for completed downloads")
    parser.add_argument("--status-interval", type=positive_int, help="CLI refresh interval in seconds")
    parser.add_argument("--keepalive-interval", type=positive_int, help="seconds between keep-alive requests per idle circuit")
    parser.add_argument("--keepalive-url", help="URL used for circuit keep-alive checks")
    parser.add_argument("--max-keepalive-failures", type=positive_int, help="rebuild a circuit after this many keep-alive failures")
    parser.add_argument("--circuit-build-timeout", type=positive_int, help="seconds to wait for a new Tor circuit")
    parser.add_argument("--connect-timeout", type=positive_int, help="HTTP connect timeout in seconds")
    parser.add_argument("--read-timeout", type=positive_int, help="keep-alive read timeout in seconds")
    parser.add_argument("--download-read-timeout", type=positive_int, help="download read timeout in seconds")
    parser.add_argument(
        "--keep-partials",
        action=argparse.BooleanOptionalAction,
        help="keep .part files after failed downloads",
    )
    parser.add_argument(
        "--leave-circuits",
        action=argparse.BooleanOptionalAction,
        help="do not close controller-built circuits on exit",
    )
    parser.add_argument(
        "--show-paths",
        action=argparse.BooleanOptionalAction,
        help="show Tor relay nickname path lines in the UI",
    )

    args = parser.parse_args(argv)

    # Normalize after CLI overrides too. CLI relative output-dir remains relative to cwd,
    # unlike config relative output_dir which is already resolved relative to the config file.
    args.config = str(Path(args.config).expanduser().resolve())
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    ensure_dependencies()
    if args.circuits > 9:
        print("This UI supports selecting circuits with single digits; use --circuits 1..9.", file=sys.stderr)
        return 2

    app = TorCircuitCollector(args)

    def handle_signal(signum, frame):  # noqa: ARG001
        app.stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        app.start()
        curses.wrapper(curses_main, app)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())