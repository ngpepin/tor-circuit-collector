#!/usr/bin/env python3
"""
tor-circuit-collector.py

Maintain nine controller-built Tor circuits, keep them warm with periodic
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
import mimetypes
import os
import random
import re
import shutil
import socket
import signal
import subprocess
import string
import sys
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
DEFAULT_KEEPALIVE_URLS = [
    "https://check.torproject.org/api/ip",
    "https://api.ipify.org?format=json",
    "https://icanhazip.com",
]
DEFAULT_KEEPALIVE_URL = DEFAULT_KEEPALIVE_URLS[0]
USER_AGENT = "tor-circuit-collector/1.0"
COMMON_CONTROL_PORTS = (9051, 9151)
COMMON_SOCKS_PORTS = (9050, 9150)
DEFAULT_FILENAME_MAX_LENGTH = 120

CONTENT_TYPE_EXTENSION_HINTS = {
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
    "application/x-mobipocket-ebook": ".mobi",
    "application/zip": ".zip",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/html": ".html",
    "text/plain": ".txt",
}


@dataclass
class DownloadState:
    url: str
    filename: str
    started_at: float = field(default_factory=time.monotonic)
    ended_at: Optional[float] = None
    bytes_done: int = 0
    total_bytes: Optional[int] = None
    cancel_requested: bool = False
    done: bool = False
    error: Optional[str] = None
    part_path: Optional[Path] = None
    final_path: Optional[Path] = None
    resumed_from: int = 0
    # Set when a circuit rebuild interrupts the transfer: the partial file is
    # kept so the rebuilt circuit can continue it with a Range request.
    keep_part_for_resume: bool = False


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
    pending_resume_url: str = ""
    resume_part_path: Optional[Path] = None
    resume_final_path: Optional[Path] = None
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


@dataclass(frozen=True)
class KeyEvent:
    key: int
    alt: bool = False
    ctrl: bool = False


class TorCircuitCollector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output_dir = Path(args.output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.keepalive_urls: List[str] = list(getattr(args, "keepalive_urls", []) or [])
        if not self.keepalive_urls:
            fallback = str(getattr(args, "keepalive_url", "") or "").strip()
            self.keepalive_urls = [fallback] if fallback else list(DEFAULT_KEEPALIVE_URLS)

        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.control_lock = threading.RLock()
        self.message = "Starting Tor circuit collector..."
        self.selected_control_port: Optional[int] = int(args.control_port)
        self.selected_socks_port: Optional[int] = int(args.socks_port)
        self.port_discovery_note = "initial"
        self.port_discovery_line = self._build_port_discovery_line()

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
        self.download_history: List[str] = []
        self.failed_download_history: List[str] = []
        self.success_history_records: List[Dict[str, object]] = []
        self.failed_history_records: List[Dict[str, object]] = []
        self.successful_urls: Set[str] = set()
        self.history_limit = 2000

        try:
            self.script_dir = Path(__file__).resolve().parent
        except NameError:
            self.script_dir = Path.cwd()
        self.success_history_file = self.script_dir / "tor-circuit-collector.success-history.jsonl"
        self.failed_history_file = self.script_dir / "tor-circuit-collector.failed-history.jsonl"
        self.load_persistent_histories()

        # Build a couple of circuits at a time, but never hold control_lock
        # while waiting for circuit construction. Stream attachment must stay
        # responsive or SOCKS requests time out while circuits are building.
        self.build_semaphore = threading.BoundedSemaphore(2)

        # Consensus materialization is expensive and nine near-simultaneous
        # builds do not need nine copies of it. Guarded by self.lock; the TTL
        # comes from collector.relay_cache_ttl (0 disables the cache).
        self.relay_cache: Tuple[float, List[RelayCandidate]] = (0.0, [])

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

    def append_download_history(self, slot_index: int, url: str, filename: str, bytes_done: int) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        normalized_url = self.normalize_history_url(url)
        entry = {
            "timestamp": timestamp,
            "slot": int(slot_index),
            "url": url,
            "url_norm": normalized_url,
            "filename": filename,
            "bytes_done": int(bytes_done),
        }

        removed_failed = False
        failed_snapshot: Optional[List[Dict[str, object]]] = None
        with self.lock:
            self.success_history_records.insert(0, entry)
            if len(self.success_history_records) > self.history_limit:
                self.success_history_records = self.success_history_records[: self.history_limit]

            self.successful_urls.add(normalized_url)
            before = len(self.failed_history_records)
            self.failed_history_records = [
                rec for rec in self.failed_history_records if str(rec.get("url_norm", "")) != normalized_url
            ]
            removed_failed = len(self.failed_history_records) != before
            if removed_failed:
                failed_snapshot = list(self.failed_history_records)

            self.rebuild_history_views_locked()

        self.append_history_file_entry(self.success_history_file, entry)
        if removed_failed and failed_snapshot is not None:
            self.rewrite_history_file(self.failed_history_file, list(reversed(failed_snapshot)))

    def append_failed_download_history(
        self,
        slot_index: int,
        url: str,
        filename: str,
        bytes_done: int,
        error: str,
    ) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        normalized_url = self.normalize_history_url(url)
        entry = {
            "timestamp": timestamp,
            "slot": int(slot_index),
            "url": url,
            "url_norm": normalized_url,
            "filename": filename,
            "bytes_done": int(bytes_done),
            "error": error,
        }

        should_persist = True
        with self.lock:
            # Do not show/persist failures for URLs that already eventually succeeded.
            if normalized_url in self.successful_urls:
                should_persist = False
            else:
                self.failed_history_records.insert(0, entry)
                if len(self.failed_history_records) > self.history_limit:
                    self.failed_history_records = self.failed_history_records[: self.history_limit]
                self.rebuild_history_views_locked()

        if should_persist:
            self.append_history_file_entry(self.failed_history_file, entry)

    def clear_success_history(self) -> None:
        with self.lock:
            self.success_history_records = []
            self.download_history = []
            self.successful_urls = set()
            self.rebuild_history_views_locked()
        self.rewrite_history_file(self.success_history_file, [])

    def clear_failed_history(self) -> None:
        with self.lock:
            self.failed_history_records = []
            self.failed_download_history = []
        self.rewrite_history_file(self.failed_history_file, [])

    @staticmethod
    def normalize_history_url(url: str) -> str:
        return url.strip()

    @staticmethod
    def build_history_line(entry: Dict[str, object], failed: bool) -> str:
        timestamp = str(entry.get("timestamp", ""))
        slot = int(entry.get("slot", 0) or 0)
        filename = str(entry.get("filename", "download.bin"))
        bytes_done = int(entry.get("bytes_done", 0) or 0)
        url = str(entry.get("url", ""))
        line = f"[{timestamp}] circuit {slot}: {filename} ({human_bytes(bytes_done)}) <- {url}"
        if failed:
            error = str(entry.get("error", ""))
            if error:
                line += f" | ERR {error}"
        return line

    def rebuild_history_views_locked(self) -> None:
        self.download_history = [self.build_history_line(rec, failed=False) for rec in self.success_history_records]
        deduped_failed: List[Dict[str, object]] = []
        seen_urls: Set[str] = set()
        for rec in self.failed_history_records:
            url_norm = str(rec.get("url_norm", ""))
            if url_norm in self.successful_urls:
                continue
            if url_norm and url_norm in seen_urls:
                continue
            if url_norm:
                seen_urls.add(url_norm)
            deduped_failed.append(rec)

        self.failed_download_history = [
            self.build_history_line(rec, failed=True)
            for rec in deduped_failed
        ]

    @staticmethod
    def load_history_file(path: Path) -> List[Dict[str, object]]:
        if not path.exists():
            return []

        records: List[Dict[str, object]] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        raw = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(raw, dict):
                        records.append(raw)
        except OSError:
            return []
        return records

    @staticmethod
    def append_history_file_entry(path: Path, entry: Dict[str, object]) -> None:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            pass

    @staticmethod
    def rewrite_history_file(path: Path, entries: List[Dict[str, object]]) -> None:
        try:
            with path.open("w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=True) + "\n")
        except OSError:
            pass

    def load_persistent_histories(self) -> None:
        success_raw = self.load_history_file(self.success_history_file)
        failed_raw = self.load_history_file(self.failed_history_file)

        success_records: List[Dict[str, object]] = []
        for raw in success_raw:
            url = str(raw.get("url", "")).strip()
            if not url:
                continue
            normalized_url = self.normalize_history_url(url)
            success_records.append(
                {
                    "timestamp": str(raw.get("timestamp", "")) or time.strftime("%Y-%m-%d %H:%M:%S"),
                    "slot": int(raw.get("slot", 0) or 0),
                    "url": url,
                    "url_norm": normalized_url,
                    "filename": str(raw.get("filename", "download.bin")),
                    "bytes_done": int(raw.get("bytes_done", 0) or 0),
                }
            )

        failed_records: List[Dict[str, object]] = []
        for raw in failed_raw:
            url = str(raw.get("url", "")).strip()
            if not url:
                continue
            normalized_url = self.normalize_history_url(url)
            failed_records.append(
                {
                    "timestamp": str(raw.get("timestamp", "")) or time.strftime("%Y-%m-%d %H:%M:%S"),
                    "slot": int(raw.get("slot", 0) or 0),
                    "url": url,
                    "url_norm": normalized_url,
                    "filename": str(raw.get("filename", "download.bin")),
                    "bytes_done": int(raw.get("bytes_done", 0) or 0),
                    "error": str(raw.get("error", "")),
                }
            )

        # Files are append-only chronological logs; convert to newest-first in memory.
        success_newest_first = list(reversed(success_records))[: self.history_limit]
        successful_urls = {str(rec.get("url_norm", "")) for rec in success_newest_first}
        failed_newest_first = [
            rec for rec in reversed(failed_records)
            if str(rec.get("url_norm", "")) not in successful_urls
        ][: self.history_limit]

        with self.lock:
            self.success_history_records = success_newest_first
            self.failed_history_records = failed_newest_first
            self.successful_urls = successful_urls
            self.rebuild_history_views_locked()

    def terminate_slot_download(self, slot_index: int) -> bool:
        session_to_close = None
        with self.lock:
            slot = self.slots[slot_index - 1]
            state = slot.download
            if state is None or state.done or state.error:
                return False
            state.cancel_requested = True
            slot.status = "terminating"
            session_to_close = self.sessions.pop(slot_index, None)
        if session_to_close is not None:
            try:
                session_to_close.close()
            except Exception:
                pass
        self.append_slot_output(slot_index, "Terminate requested by user.")
        self.set_message(f"Terminate requested for circuit {slot_index} download.")
        return True

    def clear_slot_state(self, slot_index: int) -> bool:
        interrupted = self.terminate_slot_download(slot_index)
        with self.lock:
            slot = self.slots[slot_index - 1]
            slot.download = None
            slot.bytes_since_last_url = 0
            slot.last_url = ""
            slot.pending_resume_url = ""
            slot.last_error = ""
            slot.session_output = []
            slot.status = "ready"
            slot.consecutive_keepalive_failures = 0
        return interrupted

    def start(self) -> None:
        self.connect_controller()
        monitor = threading.Thread(target=self.monitor_loop, name="tcc-monitor", daemon=True)
        monitor.start()
        self.threads.append(monitor)

    @staticmethod
    def _unique_ports(ports: List[int]) -> List[int]:
        result: List[int] = []
        seen: Set[int] = set()
        for port in ports:
            if 1 <= int(port) <= 65535 and int(port) not in seen:
                seen.add(int(port))
                result.append(int(port))
        return result

    @staticmethod
    def _extract_port_from_listener_token(token: str) -> Optional[int]:
        candidate = token.strip()
        if not candidate:
            return None
        if candidate.startswith("[") and "]:" in candidate:
            _, _, tail = candidate.rpartition("]:")
            return int(tail) if tail.isdigit() else None
        if ":" in candidate:
            _, _, tail = candidate.rpartition(":")
            return int(tail) if tail.isdigit() else None
        return int(candidate) if candidate.isdigit() else None

    @classmethod
    def _parse_listener_ports(cls, raw_listeners: str) -> List[int]:
        if not raw_listeners:
            return []
        tokens = re.findall(r'"([^"]+)"', raw_listeners)
        if not tokens:
            tokens = raw_listeners.split()
        ports: List[int] = []
        for token in tokens:
            parsed = cls._extract_port_from_listener_token(token)
            if parsed is not None:
                ports.append(parsed)
        return cls._unique_ports(ports)

    @staticmethod
    def _tcp_connectable(host: str, port: int, timeout: float = 0.6) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except Exception:
            return False

    @classmethod
    def _tcp_probe_many(cls, host: str, ports: List[int], timeout: float = 0.6, max_workers: int = 8) -> List[int]:
        """Return the ports that accepted a TCP connection, preserving input order."""
        if not ports:
            return []
        workers = max(1, min(max_workers, len(ports)))
        reachable: Set[int] = set()
        with futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tcc-portprobe") as pool:
            future_ports = {
                pool.submit(cls._tcp_connectable, host, port, timeout): port for port in ports
            }
            for future in futures.as_completed(future_ports):
                port = future_ports[future]
                try:
                    if future.result():
                        reachable.add(port)
                except Exception:
                    continue
        return [port for port in ports if port in reachable]

    @classmethod
    def _coerce_port_list(cls, raw_ports: object) -> List[int]:
        if raw_ports is None:
            return []

        values: List[object]
        if isinstance(raw_ports, str):
            values = [item for item in re.split(r"[,\s]+", raw_ports.strip()) if item]
        elif isinstance(raw_ports, (list, tuple, set)):
            values = list(raw_ports)
        else:
            values = [raw_ports]

        ports: List[int] = []
        for value in values:
            try:
                ports.append(int(value))
            except (TypeError, ValueError):
                continue
        return cls._unique_ports(ports)

    def _port_probe_timeout(self) -> float:
        try:
            timeout = float(self.args.port_probe_timeout)
        except (TypeError, ValueError):
            return 0.6
        return max(0.05, min(5.0, timeout))

    def _build_port_discovery_line(self) -> str:
        control_candidates = self._coerce_port_list(getattr(self.args, "control_port_candidates", []))
        socks_candidates = self._coerce_port_list(getattr(self.args, "socks_port_candidates", []))
        scan_radius = max(0, int(getattr(self.args, "control_port_scan_radius", 10) or 0))
        timeout = self._port_probe_timeout()
        active_control = self.selected_control_port if self.selected_control_port is not None else int(self.args.control_port)
        active_socks = self.selected_socks_port if self.selected_socks_port is not None else int(self.args.socks_port)

        control_text = ",".join(str(port) for port in control_candidates) if control_candidates else "-"
        socks_text = ",".join(str(port) for port in socks_candidates) if socks_candidates else "-"
        return (
            "Port discovery | "
            f"control active={active_control} base={self.args.control_port} candidates=[{control_text}] radius={scan_radius} | "
            f"socks active={active_socks} base={self.args.socks_port} candidates=[{socks_text}] | "
            f"probe_timeout={timeout:.2f}s | {self.port_discovery_note}"
        )

    def _refresh_port_discovery_line(self, note: Optional[str] = None) -> None:
        if note is not None:
            self.port_discovery_note = note
        self.port_discovery_line = self._build_port_discovery_line()

    def _candidate_control_ports(self) -> List[int]:
        base = int(self.args.control_port)
        configured = self._coerce_port_list(getattr(self.args, "control_port_candidates", []))
        candidates: List[int] = [base, *configured]
        scan_radius = max(0, int(getattr(self.args, "control_port_scan_radius", 10) or 0))

        # Probe near the requested control port first, then common Tor ports.
        for delta in range(1, scan_radius + 1):
            candidates.append(base + delta)
            candidates.append(base - delta)
        candidates.extend(list(COMMON_CONTROL_PORTS))
        return self._unique_ports(candidates)

    def _auto_select_socks_port(self, controller) -> None:
        configured_port = int(self.args.socks_port)
        probe_timeout = self._port_probe_timeout()
        if self._tcp_connectable(self.args.socks_host, configured_port, timeout=probe_timeout):
            self.selected_socks_port = configured_port
            self._refresh_port_discovery_line(f"using configured socks {configured_port}")
            return

        listener_ports: List[int] = []
        try:
            raw = controller.get_info("net/listeners/socks", "")
            listener_ports = self._parse_listener_ports(raw)
        except Exception:
            listener_ports = []

        configured = self._coerce_port_list(getattr(self.args, "socks_port_candidates", []))
        candidates = self._unique_ports([configured_port, *configured, *listener_ports, *COMMON_SOCKS_PORTS])
        for port in candidates:
            if self._tcp_connectable(self.args.socks_host, port, timeout=probe_timeout):
                self.args.socks_port = port
                self.selected_socks_port = port
                if port != configured_port:
                    self._refresh_port_discovery_line(f"discovered socks {port}")
                    self.set_message(
                        f"Configured SOCKS port {configured_port} unreachable; using discovered port {port}."
                    )
                else:
                    self._refresh_port_discovery_line(f"using configured socks {port}")
                return

        self.selected_socks_port = configured_port
        self._refresh_port_discovery_line("no reachable socks discovered")
        self.set_message(
            f"Warning: no reachable SOCKS port found on {self.args.socks_host}; keeping configured {configured_port}."
        )

    def connect_controller(self) -> None:
        controller = None
        tried_ports: List[int] = []
        connect_errors: List[str] = []
        selected_control_port: Optional[int] = None

        candidates = self._candidate_control_ports()
        probe_timeout = self._port_probe_timeout()
        self.set_message(
            f"Probing {len(candidates)} control port(s) on {self.args.control_host}..."
        )

        # A parallel TCP pre-probe collapses a serial chain of connect timeouts
        # (about 23 ports with the default scan radius) into roughly one
        # timeout round when Tor is absent, and skips ports with no listener.
        reachable = self._tcp_probe_many(self.args.control_host, candidates, timeout=probe_timeout)
        if not reachable:
            raise RuntimeError(
                "Could not connect/authenticate to Tor ControlPort. Ensure Tor is running with "
                "ControlPort enabled and use --control-password if needed. "
                f"Tried ports {candidates}; no TCP listener answered within "
                f"{probe_timeout:.2f}s on {self.args.control_host}."
            )

        self.set_message(f"Authenticating with control port(s) {reachable}...")
        for port in reachable:
            tried_ports.append(port)
            try:
                candidate = Controller.from_port(address=self.args.control_host, port=port)
                candidate.authenticate(password=self.args.control_password)
                controller = candidate
                selected_control_port = port
                break
            except Exception as exc:
                connect_errors.append(f"{port}: {self._short_error(exc)}")

        if controller is None:
            detail = "; ".join(connect_errors[:6])
            raise RuntimeError(
                "Could not connect/authenticate to Tor ControlPort. Ensure Tor is running with "
                "ControlPort enabled and use --control-password if needed. "
                f"Tried ports {tried_ports}. Errors: {detail}"
            )

        self.args.control_port = selected_control_port
        self.selected_control_port = selected_control_port
        self._refresh_port_discovery_line(f"selected control {selected_control_port}")
        try:
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
            self._auto_select_socks_port(controller)
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
        session_to_close = None

        with self.lock:
            if slot.rebuilding:
                return
            slot.rebuilding = True
            slot.status = "queued"
            slot.last_error = ""
            old_circuit = slot.circuit_id
            allow_auto_resume = reason != "manual replace"

            # If an active download was interrupted by a dropped/broken circuit,
            # preserve its URL so we can resume after rebuilding succeeds.
            resume_url = slot.pending_resume_url.strip() if allow_auto_resume else ""
            active_state = slot.download
            active_download = (
                active_state is not None
                and not active_state.done
                and not active_state.error
                and not active_state.cancel_requested
            )
            if allow_auto_resume and active_download and active_state is not None and active_state.url.strip():
                resume_url = active_state.url.strip()
                active_state.cancel_requested = True
                # Keep whatever bytes already landed so the rebuilt circuit can
                # continue the transfer with a Range request instead of
                # re-downloading from zero.
                active_state.keep_part_for_resume = True
                slot.resume_part_path = active_state.part_path
                slot.resume_final_path = active_state.final_path
                session_to_close = self.sessions.pop(slot_index, None)

            slot.pending_resume_url = resume_url
            if not resume_url:
                slot.resume_part_path = None
                slot.resume_final_path = None
            slot.circuit_id = None
            slot.exit_fingerprint = ""
            slot.exit_nickname = ""
            slot.exit_ip = ""
            slot.path_summary = ""
            slot.created_at = None
            # Reset slot transfer/error state so a newly built circuit starts clean.
            slot.download = None
            slot.bytes_since_last_url = 0
            slot.last_url = ""

        if session_to_close is not None:
            try:
                session_to_close.close()
            except Exception:
                pass

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
            resume_url = ""
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
                slot.download = None
                slot.bytes_since_last_url = 0
                slot.last_url = ""
                resume_url = slot.pending_resume_url.strip()
                slot.pending_resume_url = ""

                # Do not immediately launch a keep-alive on a just-built circuit.
                # It can race with the remaining initial circuit builds and makes
                # the UI look like the collector stopped. The first keep-alive
                # will happen after keepalive_interval seconds.
                slot.last_keepalive_at = now
                slot.last_ok_at = now

            self.set_message(f"Circuit {slot.index} ready as Tor circuit {circuit_id}.")
            if resume_url and not self.stop_event.is_set():
                self.append_slot_output(slot_index, f"Circuit rebuilt; resuming download: {resume_url}")
                self.start_download(slot_index, resume_url)
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
        """Return relay candidates, reusing a recent consensus snapshot.

        Nine initial builds plus every keep-alive rebuild would otherwise each
        materialize the whole consensus from the control port. The cache holds
        only relay flags, so a stale entry at worst makes Tor reject one
        planned path and trigger a rebuild.
        """
        if self.controller is None:
            raise RuntimeError("controller is not connected")

        ttl = self._relay_cache_ttl()
        if ttl > 0:
            with self.lock:
                cached_at, cached = self.relay_cache
                if cached and 0.0 <= time.monotonic() - cached_at < ttl:
                    return cached

        candidates = self._fetch_relay_candidates()
        if ttl > 0:
            with self.lock:
                self.relay_cache = (time.monotonic(), candidates)
        return candidates

    def _relay_cache_ttl(self) -> float:
        try:
            ttl = float(getattr(self.args, "relay_cache_ttl", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, ttl)

    def _fetch_relay_candidates(self) -> List[RelayCandidate]:
        if self.controller is None:
            raise RuntimeError("controller is not connected")

        keepalive_ports: Set[int] = set()
        for endpoint in self.keepalive_urls:
            parsed_keepalive = urlsplit(endpoint)
            port = parsed_keepalive.port or (443 if parsed_keepalive.scheme == "https" else 80)
            keepalive_ports.add(int(port))
        if not keepalive_ports:
            keepalive_ports.add(443)

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
                    can_keepalive = any(bool(exit_policy.can_exit_to(port=port)) for port in keepalive_ports)
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
            def probe_endpoint(session: requests.Session, endpoint: str) -> None:
                response = session.head(
                    endpoint,
                    allow_redirects=True,
                    timeout=(self.args.connect_timeout, self.args.read_timeout),
                    headers={"Connection": "keep-alive"},
                )
                # Some endpoints reject HEAD; fall back to a tiny GET.
                if response.status_code >= 400:
                    response.close()
                    response = session.get(
                        endpoint,
                        allow_redirects=True,
                        timeout=(self.args.connect_timeout, self.args.read_timeout),
                        headers={"Connection": "keep-alive"},
                    )
                response.raise_for_status()
                response.close()

            def run_keepalive_probe(session: requests.Session) -> str:
                last_error: Optional[Exception] = None
                for endpoint in self.keepalive_urls:
                    try:
                        probe_endpoint(session, endpoint)
                        return endpoint
                    except requests.RequestException as exc:
                        last_error = exc
                        continue
                if last_error is not None:
                    raise last_error
                raise RuntimeError("No keep-alive endpoints are configured.")

            with slot.io_lock:
                session = self.session_for_slot(slot_index)
                try:
                    endpoint_used = run_keepalive_probe(session)
                except requests.RequestException as first_exc:
                    # A stale pooled connection can trigger transient SOCKS pool failures.
                    # Drop and recreate the per-slot session, then retry once.
                    try:
                        session.close()
                    except Exception:
                        pass

                    with self.lock:
                        cached = self.sessions.get(slot_index)
                        if cached is session:
                            self.sessions.pop(slot_index, None)

                    retry_session = self.session_for_slot(slot_index)
                    try:
                        endpoint_used = run_keepalive_probe(retry_session)
                    except requests.RequestException as retry_exc:
                        raise retry_exc from first_exc
                    self.append_slot_output(slot_index, "Keep-alive succeeded after session refresh.")

            with self.lock:
                slot.last_ok_at = time.monotonic()
                slot.consecutive_keepalive_failures = 0
                active_download = slot.download is not None and not slot.download.done and not slot.download.error
                failed_download = slot.download is not None and bool(slot.download.error)
                if active_download:
                    slot.status = "downloading"
                elif failed_download:
                    slot.status = "download failed"
                else:
                    slot.status = "ready"
                    slot.last_error = ""
            if endpoint_used != self.keepalive_urls[0]:
                self.append_slot_output(slot_index, f"Keep-alive fallback endpoint used: {endpoint_used}")
        except Exception as exc:
            rebuild = False
            with self.lock:
                keepalive_error = self._short_error(exc)
                slot.consecutive_keepalive_failures += 1
                rebuild = slot.consecutive_keepalive_failures >= self.args.max_keepalive_failures
                active_download = slot.download is not None and not slot.download.done and not slot.download.error
                failed_download = slot.download is not None and bool(slot.download.error)
                if rebuild:
                    slot.status = "rebuilding"
                    slot.last_error = keepalive_error
                elif active_download:
                    slot.status = "downloading"
                elif failed_download:
                    slot.status = "download failed"
                else:
                    slot.status = "ready"
                    slot.last_error = keepalive_error
            self.set_message(f"Circuit {slot.index} keep-alive failed: {keepalive_error}")
            if rebuild:
                self.replace_circuit(slot_index, "keep-alive failures")
        finally:
            with self.lock:
                slot.keepalive_active = False
                if slot.circuit_id and slot.status == "keep-alive":
                    active_download = slot.download is not None and not slot.download.done and not slot.download.error
                    failed_download = slot.download is not None and bool(slot.download.error)
                    if active_download:
                        slot.status = "downloading"
                    elif failed_download:
                        slot.status = "download failed"
                    else:
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
            if slot.circuit_id is None:
                self.set_message(f"Circuit {slot_index} is not ready.")
                return
            if slot.download is not None and not slot.download.done and not slot.download.error:
                self.set_message(f"Circuit {slot_index} already downloading.")
                return

            # A rebuild may have handed us the paths of an interrupted transfer.
            # Reuse them only while the partial still exists and the final file
            # was never published; otherwise there is nothing to resume.
            resume_part = slot.resume_part_path
            resume_final = slot.resume_final_path
            resumed_from = 0
            if resume_part is not None and resume_final is not None:
                try:
                    if resume_part.exists() and not resume_final.exists():
                        resumed_from = max(0, int(resume_part.stat().st_size))
                    else:
                        resume_part = None
                        resume_final = None
                except OSError:
                    resume_part = None
                    resume_final = None
                    resumed_from = 0
            slot.resume_part_path = None
            slot.resume_final_path = None

            slot.last_url = url
            slot.bytes_since_last_url = 0
            slot.download = DownloadState(
                url=url,
                filename=resume_final.name if resume_final is not None else self.filename_from_url(url),
                bytes_done=resumed_from,
                part_path=resume_part,
                final_path=resume_final,
                resumed_from=resumed_from,
            )
            slot.status = "downloading"
            slot.last_error = ""

        if resumed_from > 0:
            self.append_slot_output(
                slot_index,
                f"URL queued: {url} (resuming at {human_bytes(resumed_from)})",
            )
            self.set_message(
                f"Circuit {slot_index} download resuming at {human_bytes(resumed_from)}."
            )
        else:
            self.append_slot_output(slot_index, f"URL queued: {url}")
            self.set_message(f"Circuit {slot_index} download started.")
        self._start_thread(target=self.download_worker, name=f"tcc-download-{slot_index}", args=(slot_index, url))

    @staticmethod
    def _content_range_start(value: str) -> Optional[int]:
        """Return the first byte offset of a ``Content-Range`` header.

        Accepts the ``bytes <start>-<end>/<total>`` form and returns ``None``
        for anything malformed or for an unsatisfied ``bytes */<total>``.
        """
        match = re.fullmatch(r"\s*bytes\s+(\d+)-\d+/(\d+|\*)\s*", value or "")
        if not match:
            return None
        return int(match.group(1))

    def _resume_response_is_usable(self, response, resume_from: int) -> bool:
        """True only for a 206 whose ``Content-Range`` starts at ``resume_from``.

        Anything else (200 because the server ignored Range, a 206 for a
        different range, or a missing/unparsable header) means the kept bytes
        are not a prefix of this body and must not simply be appended to.
        """
        if response.status_code != 206:
            return False
        start = self._content_range_start(response.headers.get("Content-Range", ""))
        return start == resume_from

    def _open_download_response(self, session: "requests.Session", url: str, resume_from: int):
        """Open the download response, retrying once without ``Range`` on HTTP 416.

        A 416 means the partial file is not a usable prefix of the current
        resource (it changed, shrank, or was written by an older run). In that
        case the only correct action is to discard it and fetch from byte 0.
        """
        timeout = (self.args.connect_timeout, self.args.download_read_timeout)
        headers = {"Connection": "keep-alive"}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
        response = session.get(url, stream=True, timeout=timeout, headers=headers)
        if resume_from > 0 and response.status_code == 416:
            response.close()
            headers.pop("Range", None)
            response = session.get(url, stream=True, timeout=timeout, headers=headers)
        return response

    def download_worker(self, slot_index: int, url: str) -> None:
        slot = self.slots[slot_index - 1]
        guessed_name = self.filename_from_url(url)
        with self.lock:
            state = slot.download
            if state is None or state.url != url or state.done or state.error:
                state = DownloadState(url=url, filename=guessed_name)
                slot.download = state
            slot.status = "downloading"
            slot.last_error = ""
            # Only a state prepared by start_download() carries resume paths.
            resume_part = state.part_path
            resume_final = state.final_path
            resumed_from = max(0, int(state.resumed_from or 0)) if resume_part is not None else 0

        self.append_slot_output(slot_index, "Download session started.")

        part_path: Optional[Path] = resume_part
        final_path: Optional[Path] = resume_final
        try:
            with slot.io_lock:
                session = self.session_for_slot(slot_index)
                attempt_from = resumed_from
                if attempt_from > 0 and part_path is not None:
                    # The interrupted worker may have flushed one last chunk (or
                    # the partial may have been truncated) before releasing
                    # io_lock. The Range offset must match the bytes actually on
                    # disk, not the size sampled by start_download().
                    try:
                        attempt_from = part_path.stat().st_size
                    except OSError:
                        attempt_from = 0
                if final_path is not None and final_path.exists():
                    # The final file was published after the resume was queued;
                    # there is nothing left to continue, so reserve a new name.
                    attempt_from = 0
                    part_path = None
                    final_path = None
                # At most one downgrade to a full body: a server that ignores or
                # rejects Range must still produce a correct file, and a second
                # rejection means the resource itself is unusable.
                for attempt in range(2):
                    with self._open_download_response(session, url, attempt_from) as response:
                        if attempt_from > 0 and not self._resume_response_is_usable(response, attempt_from):
                            if attempt == 0:
                                self.append_slot_output(
                                    slot_index,
                                    "Server did not honor the resume request; restarting from byte 0.",
                                )
                                attempt_from = 0
                                continue
                            raise RuntimeError("server rejected the resume request")

                        response.raise_for_status()
                        total_header = response.headers.get("Content-Length")
                        content_length = int(total_header) if total_header and total_header.isdigit() else None
                        if content_length is not None and attempt_from > 0:
                            content_length += attempt_from

                        if part_path is None or final_path is None:
                            # No partial reserved (fresh transfer), so pick a
                            # destination. When a partial already exists the
                            # paths are reused, letting the truncating write
                            # below replace it in place instead of orphaning it.
                            attempt_from = 0
                            resolved_url = response.url or url
                            name = self.filename_from_response(resolved_url, response.headers, guessed_name)
                            final_path = self.unique_path(self.output_dir / name)
                            part_path = final_path.with_suffix(final_path.suffix + ".part")
                        if final_path is None or part_path is None:
                            raise RuntimeError("could not reserve a destination path")

                        state.filename = final_path.name
                        state.total_bytes = content_length
                        state.part_path = part_path
                        state.final_path = final_path
                        state.resumed_from = attempt_from
                        with self.lock:
                            state.bytes_done = attempt_from
                            if slot.download is state:
                                slot.bytes_since_last_url = 0

                        if attempt_from > 0:
                            self.append_slot_output(
                                slot_index,
                                f"HTTP {response.status_code}; resuming {final_path.name} at "
                                f"{human_bytes(attempt_from)}; total {human_bytes(content_length)}.",
                            )
                        else:
                            self.append_slot_output(
                                slot_index,
                                f"HTTP {response.status_code}; writing to {final_path.name}; total {human_bytes(content_length)}.",
                            )

                        next_progress_log = 1024 * 1024

                        # "ab" appends to the kept partial so already-downloaded
                        # bytes survive; "wb" truncates for a fresh transfer.
                        write_mode = "ab" if attempt_from > 0 else "wb"
                        with part_path.open(write_mode) as fh:
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
                        break

            with self.lock:
                if slot.download is state:
                    state.done = True
                    state.ended_at = time.monotonic()
                    slot.status = "ready"
                    slot.last_ok_at = time.monotonic()
                    slot.last_error = ""
            self.append_download_history(
                slot_index,
                state.url,
                final_path.name if final_path else state.filename,
                state.bytes_done,
            )
            self.append_slot_output(
                slot_index,
                f"Download complete: {final_path.name if final_path else state.filename} ({human_bytes(state.bytes_done)}).",
            )
            self.set_message(f"Circuit {slot_index} download complete: {final_path.name if final_path else state.filename}")
        except Exception as exc:
            # A rebuild-interrupted transfer keeps its .part so the rebuilt
            # circuit can continue it with a Range request; every other failure
            # follows --keep-partials.
            with self.lock:
                preserve_partial = state.keep_part_for_resume
            if (
                part_path is not None
                and part_path.exists()
                and not preserve_partial
                and not self.args.keep_partials
            ):
                try:
                    part_path.unlink()
                except Exception:
                    pass
            with self.lock:
                state.error = self._short_error(exc)
                state.ended_at = time.monotonic()
                if slot.download is state:
                    slot.status = "download failed"
                    slot.last_error = state.error
            self.append_failed_download_history(
                slot_index,
                state.url,
                final_path.name if final_path else state.filename,
                state.bytes_done,
                state.error,
            )
            self.append_slot_output(slot_index, f"Download failed: {state.error}")
            self.set_message(f"Circuit {slot_index} download failed: {state.error}")

    @staticmethod
    def filename_from_url(url: str) -> str:
        path_name = Path(unquote(urlsplit(url).path)).name
        return sanitize_filename(path_name or "download.bin")

    def filename_from_response(self, url: str, headers, fallback: str) -> str:
        candidate = fallback or self.filename_from_url(url)
        disposition = headers.get("Content-Disposition", "")
        if disposition:
            msg = Message()
            msg["Content-Disposition"] = disposition
            filename = msg.get_filename()
            if filename:
                candidate = filename

        normalized = sanitize_filename(candidate)
        content_type = headers.get("Content-Type", "")
        extension_hint = self.extension_from_content_type(content_type) or self.extension_from_url(url)
        if extension_hint:
            normalized = self.apply_extension_hint(normalized, extension_hint)

        # Never keep extensionless filenames; force a safe fallback extension.
        if not Path(normalized).suffix:
            normalized = sanitize_filename(f"{normalized}.bin")
        return normalized

    @staticmethod
    def apply_extension_hint(filename: str, extension_hint: str) -> str:
        extension_hint = extension_hint.lower()
        if not extension_hint.startswith("."):
            extension_hint = f".{extension_hint}"

        name_path = Path(filename)
        current_suffix = name_path.suffix.lower()

        if not current_suffix:
            return sanitize_filename(f"{filename}{extension_hint}")
        if current_suffix == extension_hint:
            return filename
        if current_suffix == ".bin" and extension_hint != ".bin":
            return sanitize_filename(f"{name_path.stem}{extension_hint}")
        return filename

    @staticmethod
    def extension_from_content_type(content_type: str) -> Optional[str]:
        mime = content_type.split(";", 1)[0].strip().lower()
        if not mime:
            return None

        extension = CONTENT_TYPE_EXTENSION_HINTS.get(mime)
        if extension is None:
            extension = mimetypes.guess_extension(mime, strict=False)
        if extension == ".jpe":
            extension = ".jpg"
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        return extension

    @staticmethod
    def extension_from_url(url: str) -> Optional[str]:
        parsed = urlsplit(url)

        # Try path extension first.
        path_ext = TorCircuitCollector.validate_extension(Path(unquote(parsed.path)).suffix.lower())
        if path_ext:
            return path_ext

        # Then common query params carrying filename-like values.
        try:
            query = parse_qs(parsed.query, keep_blank_values=False)
        except Exception:
            return None

        for key in ("filename", "file", "name", "download", "attachment"):
            values = query.get(key)
            if not values:
                continue
            for value in reversed(values):
                ext = TorCircuitCollector.validate_extension(Path(unquote(value)).suffix.lower())
                if ext:
                    return ext
        return None

    @staticmethod
    def validate_extension(ext: str) -> Optional[str]:
        if not ext:
            return None
        ext = ext.strip().lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
            return None
        return ext

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
    name = re.sub(r"[<>:\"|?*]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    if name in {"", ".", ".."}:
        return "download.bin"

    path_name = Path(name)
    suffixes = path_name.suffixes
    if len(suffixes) >= 2 and suffixes[-1].lower() in {".gz", ".bz2", ".xz", ".zst"}:
        suffix = "".join(suffixes[-2:])
        stem = path_name.name[: -len(suffix)]
    else:
        suffix = path_name.suffix
        stem = path_name.stem if suffix else path_name.name

    stem = stem.strip(" .") or "download"
    max_length = DEFAULT_FILENAME_MAX_LENGTH
    stem_budget = max(1, max_length - len(suffix))
    if len(stem) > stem_budget:
        if stem_budget > 3:
            stem = stem[: stem_budget - 3].rstrip(" ._-") + "..."
        else:
            stem = stem[:stem_budget]

    candidate = f"{stem}{suffix}"
    if candidate in {"", ".", ".."}:
        return "download.bin"
    return candidate


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


def average_mbps_text(download: Optional[DownloadState]) -> str:
    if download is None or download.bytes_done <= 0:
        return "-"

    end_time = download.ended_at if download.ended_at is not None else time.monotonic()
    elapsed = max(0.001, end_time - download.started_at)
    mbps = (download.bytes_done * 8.0) / 1_000_000.0 / elapsed
    return f"{mbps:.2f}"


def add_line(stdscr, row: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if 0 <= row < height:
        stdscr.addnstr(row, 0, text.ljust(max(0, width - 1)), max(0, width - 1), attr)


def fit_table_cell(value: str, width: int) -> str:
    text = str(value)
    if width <= 0:
        return ""
    if len(text) > width:
        if width >= 4:
            text = text[: width - 3] + "..."
        else:
            text = text[:width]
    return text.ljust(width)


def _proportional_adjust(
    values: Dict[str, int],
    limits: Dict[str, int],
    delta: int,
    shrink: bool,
) -> Dict[str, int]:
    if delta <= 0:
        return values

    updated = dict(values)
    for _ in range(delta):
        candidates = []
        for key, current in updated.items():
            limit = limits[key]
            room = (current - limit) if shrink else (limit - current)
            if room > 0:
                # Use current width as weight so resizing stays proportional.
                candidates.append((key, room, current))
        if not candidates:
            break

        total_weight = sum(weight for _, _, weight in candidates)
        best_key = candidates[0][0]
        best_score = -1.0
        for key, room, weight in candidates:
            score = room * (weight / max(1, total_weight))
            if score > best_score:
                best_score = score
                best_key = key

        if shrink:
            updated[best_key] -= 1
        else:
            updated[best_key] += 1

    return updated


def compute_table_widths(rows: List[Dict[str, str]], terminal_width: int) -> Dict[str, int]:
    headers = {
        "index": "#",
        "circuit_id": "CircID",
        "exit_label": "Exit relay",
        "exit_ip": "Exit IP",
        "age": "Age",
        "last_ok": "Last OK",
        "failures": "KA fail",
        "kb_since_url": "KB URL",
        "avg_mbps": "Avg Mbps",
        "status": "Status",
    }

    min_widths = {
        "index": 2,
        "circuit_id": 6,
        "exit_label": 10,
        "exit_ip": 8,
        "age": 5,
        "last_ok": 7,
        "failures": 7,
        "kb_since_url": 7,
        "avg_mbps": 8,
        "status": 10,
    }

    preferred_caps = {
        "index": 2,
        "circuit_id": 12,
        "exit_label": 30,
        "exit_ip": 15,
        "age": 8,
        "last_ok": 8,
        "failures": 8,
        "kb_since_url": 10,
        "avg_mbps": 8,
        "status": 22,
    }

    preferred_widths: Dict[str, int] = {}
    for key, header in headers.items():
        data_max = max((len(str(row.get(key, ""))) for row in rows), default=0)
        preferred_widths[key] = min(preferred_caps[key], max(len(header), data_max, min_widths[key]))

    keys = list(headers.keys())
    separators = len(keys) - 1
    reserved_download = len(" Download")
    available = max(1, terminal_width - reserved_download)
    target_sum = max(sum(min_widths.values()), available - separators)

    widths = dict(preferred_widths)
    current_sum = sum(widths.values())
    if current_sum > target_sum:
        widths = _proportional_adjust(widths, min_widths, current_sum - target_sum, shrink=True)
    elif current_sum < target_sum:
        extra = target_sum - current_sum

        # Keep most extra space for the trailing Download field.
        # Only allow modest expansion for Exit relay to improve readability.
        exit_room = max(0, preferred_caps["exit_label"] - widths["exit_label"])
        exit_growth = min(exit_room, extra // 6, 12)
        widths["exit_label"] += exit_growth

    return widths


UI_COLOR_ATTRS: Dict[str, int] = {}


def init_ui_colors() -> None:
    global UI_COLOR_ATTRS
    UI_COLOR_ATTRS = {}

    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        palette = [
            ("ok", curses.COLOR_GREEN),
            ("warn", curses.COLOR_YELLOW),
            ("error", curses.COLOR_RED),
            ("info", curses.COLOR_CYAN),
            ("idle", curses.COLOR_BLUE),
        ]
        pair_index = 1
        for name, fg in palette:
            try:
                curses.init_pair(pair_index, fg, -1)
                UI_COLOR_ATTRS[name] = curses.color_pair(pair_index)
            except curses.error:
                UI_COLOR_ATTRS[name] = 0
            pair_index += 1
    except Exception:
        UI_COLOR_ATTRS = {}


def banner_attr(note: str) -> int:
    text = (note or "").lower()
    if "warning" in text or "no reachable" in text:
        return UI_COLOR_ATTRS.get("error", 0) | curses.A_BOLD
    if "discovered" in text:
        return UI_COLOR_ATTRS.get("ok", 0) | curses.A_BOLD
    if "using configured" in text:
        return UI_COLOR_ATTRS.get("ok", 0)
    if "selected control" in text:
        return UI_COLOR_ATTRS.get("info", 0)
    return curses.A_DIM


def circuit_status_attr(status: str) -> int:
    text = (status or "").strip().lower()
    if any(token in text for token in ("failed", "dropped", "error")):
        return UI_COLOR_ATTRS.get("error", 0) | curses.A_BOLD
    if text == "ready":
        return UI_COLOR_ATTRS.get("ok", 0) | curses.A_BOLD
    if "downloading" in text:
        return UI_COLOR_ATTRS.get("info", 0) | curses.A_BOLD
    if "keep-alive" in text:
        return UI_COLOR_ATTRS.get("idle", 0)
    if text in {"building", "queued", "rebuilding", "terminating", "starting"}:
        return UI_COLOR_ATTRS.get("warn", 0) | curses.A_BOLD
    return 0


def add_circuit_row(stdscr, row: int, prefix: str, status_text: str, suffix: str, status_attr: int) -> None:
    height, width = stdscr.getmaxyx()
    if not (0 <= row < height):
        return

    max_width = max(0, width - 1)
    combined = f"{prefix}{status_text}{suffix}"
    stdscr.addnstr(row, 0, combined.ljust(max_width), max_width)

    if status_attr and len(prefix) < max_width:
        status_width = min(len(status_text), max_width - len(prefix))
        if status_width > 0:
            stdscr.addnstr(row, len(prefix), status_text[:status_width], status_width, status_attr)


def add_circuit_row_colored(
    stdscr,
    row: int,
    prefix: str,
    status_text: str,
    suffix: str,
    status_attr: int,
    suffix_attr: int = 0,
) -> None:
    height, width = stdscr.getmaxyx()
    if not (0 <= row < height):
        return

    max_width = max(0, width - 1)
    combined = f"{prefix}{status_text}{suffix}"
    stdscr.addnstr(row, 0, combined.ljust(max_width), max_width)

    if status_attr and len(prefix) < max_width:
        status_width = min(len(status_text), max_width - len(prefix))
        if status_width > 0:
            stdscr.addnstr(row, len(prefix), status_text[:status_width], status_width, status_attr)

    if suffix_attr:
        suffix_start = len(prefix) + len(status_text)
        if suffix_start < max_width:
            suffix_width = min(len(suffix), max_width - suffix_start)
            if suffix_width > 0:
                stdscr.addnstr(row, suffix_start, suffix[:suffix_width], suffix_width, suffix_attr)


def next_available_circuit_slot(app: TorCircuitCollector, after_slot: int = 0) -> Optional[int]:
    total = int(app.args.circuits)
    if total <= 0:
        return None

    start = max(0, min(total, after_slot))
    order = list(range(start + 1, total + 1)) + list(range(1, start + 1))
    with app.lock:
        for index in order:
            slot = app.slots[index - 1]
            active_download = slot.download is not None and not slot.download.done and not slot.download.error
            if slot.circuit_id and not active_download:
                return index
    return None


def failed_retry_candidates(app: TorCircuitCollector) -> List[Tuple[int, str]]:
    candidates: List[Tuple[int, str]] = []
    with app.lock:
        for slot in app.slots:
            if slot.download is None:
                continue
            if not slot.download.error:
                continue
            if not slot.last_url:
                continue
            candidates.append((slot.index, slot.last_url))
    return candidates


def download_summary_attr(download: Optional[DownloadState]) -> int:
    if download is None:
        return 0
    if download.error:
        return UI_COLOR_ATTRS.get("error", 0) | curses.A_BOLD
    return 0


def clipboard_url_text() -> str:
    # Prefer Wayland clipboard first, then X11 clipboard providers.
    commands = [
        ["wl-paste", "-n"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ]

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
            text = (completed.stdout or "").replace("\r\n", "\n").replace("\r", "\n")
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if lines:
                return lines[0]
        except Exception:
            continue
    return ""


def set_clipboard_text(value: str) -> bool:
    if not value:
        return False

    # Prefer Wayland first, then X11 providers.
    commands = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]

    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            completed = subprocess.run(
                command,
                check=False,
                input=value,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
            if completed.returncode == 0:
                return True
        except Exception:
            continue
    return False


def _parse_ctrl_digit_sequence(chars: str) -> Optional[int]:
    # xterm/kitty modifyOtherKeys-style Ctrl+digit sequence: ESC [ <code>;5u
    match = re.fullmatch(r"\[(\d+);5u", chars)
    if not match:
        return None
    code = int(match.group(1))
    if 48 <= code <= 57:
        return code - 48
    return None


def read_key_event(stdscr) -> Optional[KeyEvent]:
    key = stdscr.getch()
    if key == -1:
        return None
    if key != 27:
        return KeyEvent(key)

    # ESC may be plain Escape or a modified key sequence (Alt/Ctrl with modern protocols).
    nxt = stdscr.getch()
    if nxt == -1:
        return KeyEvent(27)

    if ord("0") <= nxt <= ord("9"):
        return KeyEvent(nxt, alt=True)

    seq = [nxt]
    if nxt == ord("["):
        for _ in range(12):
            ch = stdscr.getch()
            if ch == -1:
                break
            seq.append(ch)
            if 64 <= ch <= 126:
                break

        seq_text = "".join(chr(ch) for ch in seq if 0 <= ch <= 255)
        ctrl_digit = _parse_ctrl_digit_sequence(seq_text)
        if ctrl_digit is not None:
            return KeyEvent(ord(str(ctrl_digit)), ctrl=True)

        # Preserve basic arrows/page navigation if emitted as raw CSI.
        if seq_text == "[A":
            return KeyEvent(curses.KEY_UP)
        if seq_text == "[B":
            return KeyEvent(curses.KEY_DOWN)
        if seq_text == "[5~":
            return KeyEvent(curses.KEY_PPAGE)
        if seq_text == "[6~":
            return KeyEvent(curses.KEY_NPAGE)

    return KeyEvent(27)


def draw_ui(
    stdscr,
    app: TorCircuitCollector,
    prompt_slot: Optional[int],
    prompt_text: str,
    retry_prompt_active: bool,
    retry_prompt_text: str,
    viewer_slot: Optional[int],
    viewer_scroll: int,
    copy_prompt_active: bool = False,
    history_view_active: bool = False,
    clear_prompt_active: bool = False,
    failed_history_view_active: bool = False,
) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 13 or width < 80:
        add_line(stdscr, 0, "Terminal too small. Please use at least 80x13.", curses.A_BOLD)
        stdscr.refresh()
        return 0

    with app.lock:
        slots_snapshot = [slot for slot in app.slots]
        message = app.message

    title = (
        f"Tor Circuit Collector | q quit | {circuit_key_hint(app.args.circuits)} download | "
        f"refresh {app.args.status_interval}s | output {app.output_dir}"
    )
    add_line(stdscr, 0, title[: width - 1], curses.A_BOLD)
    add_line(stdscr, 1, app.port_discovery_line[: width - 1], banner_attr(app.port_discovery_note))
    add_line(stdscr, 2, "─" * (width - 1))

    split_row = height // 2 if (viewer_slot is not None or history_view_active or failed_history_view_active) else height - 5
    table_bottom = max(4, split_row - 1)

    table_rows: List[Dict[str, str]] = []
    for slot in slots_snapshot:
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
            download_state = slot.download
            avg_mbps = average_mbps_text(download_state)
            status = slot.status
            summary = download_summary(download_state)
            path_summary = slot.path_summary
            last_error = slot.last_error

        display_status = status
        if download_state is not None and download_state.error:
            display_status = "download failed"
            if not last_error:
                last_error = download_state.error

        table_rows.append(
            {
                "index": str(slot.index),
                "circuit_id": circuit_id,
                "exit_label": exit_label,
                "exit_ip": exit_ip,
                "age": age,
                "last_ok": last_ok,
                "failures": failures,
                "kb_since_url": kb_since_url,
                "avg_mbps": avg_mbps,
                "status": display_status,
                "summary": summary,
                "last_error": last_error,
                "path_summary": path_summary,
                "status_attr": str(circuit_status_attr(display_status)),
                "summary_attr": str(download_summary_attr(download_state)),
            }
        )

    widths = compute_table_widths(table_rows, width - 1)
    header_prefix = " ".join(
        [
            fit_table_cell("#", widths["index"]),
            fit_table_cell("CircID", widths["circuit_id"]),
            fit_table_cell("Exit relay", widths["exit_label"]),
            fit_table_cell("Exit IP", widths["exit_ip"]),
            fit_table_cell("Age", widths["age"]),
            fit_table_cell("Last OK", widths["last_ok"]),
            fit_table_cell("KA fail", widths["failures"]),
            fit_table_cell("KB URL", widths["kb_since_url"]),
            fit_table_cell("Avg Mbps", widths["avg_mbps"]),
        ]
    )
    header_line = f"{header_prefix} {fit_table_cell('Status', widths['status'])} Download"
    add_line(stdscr, 3, header_line, curses.A_BOLD)

    row = 4
    for data in table_rows:
        if row >= table_bottom:
            break
        prefix = " ".join(
            [
                fit_table_cell(data["index"], widths["index"]),
                fit_table_cell(data["circuit_id"], widths["circuit_id"]),
                fit_table_cell(data["exit_label"], widths["exit_label"]),
                fit_table_cell(data["exit_ip"], widths["exit_ip"]),
                fit_table_cell(data["age"], widths["age"]),
                fit_table_cell(data["last_ok"], widths["last_ok"]),
                fit_table_cell(data["failures"], widths["failures"]),
                fit_table_cell(data["kb_since_url"], widths["kb_since_url"]),
                fit_table_cell(data["avg_mbps"], widths["avg_mbps"]),
            ]
        ) + " "
        status_text = fit_table_cell(data["status"], widths["status"])
        add_circuit_row_colored(
            stdscr,
            row,
            prefix,
            status_text,
            data["summary"],
            int(data["status_attr"]),
            int(data["summary_attr"]),
        )
        row += 1
        if data["last_error"] and row < table_bottom:
            add_line(stdscr, row, f"   ↳ {data['last_error']}"[: width - 1], UI_COLOR_ATTRS.get("error", 0))
            row += 1
        if data["path_summary"] and app.args.show_paths and row < table_bottom:
            add_line(stdscr, row, f"   path: {data['path_summary']}"[: width - 1])
            row += 1

    if history_view_active:
        add_line(stdscr, split_row - 1, "─" * (width - 1))
        with app.lock:
            lines = list(app.download_history)

        title = "Successful download history (most recent first) | h/Esc close | c clear | Up/Down scroll"
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
            position = f"history {viewer_scroll + 1}-{min(viewer_scroll + body_height, len(lines))} of {len(lines)}"
        else:
            position = "no successful downloads yet"
        add_line(stdscr, height - 1, f"Status: {message} | {position}"[: width - 1])
        stdscr.refresh()
        return viewer_scroll

    if failed_history_view_active:
        add_line(stdscr, split_row - 1, "─" * (width - 1))
        with app.lock:
            lines = list(app.failed_download_history)

        title = "Failed download history (excludes eventually successful URLs) | f/Esc close | c clear | Up/Down scroll"
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
            position = f"failed {viewer_scroll + 1}-{min(viewer_scroll + body_height, len(lines))} of {len(lines)}"
        else:
            position = "no unresolved failed downloads"
        add_line(stdscr, height - 1, f"Status: {message} | {position}"[: width - 1])
        stdscr.refresh()
        return viewer_scroll

    if viewer_slot is None:
        footer_row = height - 4
        add_line(stdscr, footer_row, "─" * (width - 1))
        add_line(stdscr, footer_row + 1, f"Status: {message}"[: width - 1])
        if retry_prompt_active:
            prompt = "Retry circuit number (press digit to retry, Esc cancels):"
            add_line(stdscr, footer_row + 2, prompt[: width - 1], curses.A_BOLD)
        elif copy_prompt_active:
            prompt = "Copy URL from circuit (press digit to copy, Esc cancels):"
            add_line(stdscr, footer_row + 2, prompt[: width - 1], curses.A_BOLD)
        elif clear_prompt_active:
            prompt = "Clear circuit slot (press digit to clear, Esc cancels):"
            add_line(stdscr, footer_row + 2, prompt[: width - 1], curses.A_BOLD)
        elif prompt_slot is None:
            add_line(
                stdscr,
                footer_row + 2,
                f"Press {circuit_key_hint(app.args.circuits)} for URL prompt, R retry failed URL, N replace, X clear slot, C copy URL, P paste clipboard, H success history, F failed history, or q to quit.",
            )
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
    init_ui_colors()
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    prompt_slot: Optional[int] = None
    prompt_text = ""
    retry_prompt_active = False
    retry_prompt_text = ""
    copy_prompt_active = False
    replace_prompt_active = False
    clear_prompt_active = False
    history_view_active = False
    failed_history_view_active = False
    viewer_slot: Optional[int] = None
    viewer_scroll = 0
    last_paste_slot = 0
    next_draw = 0.0

    while not app.stop_event.is_set():
        now = time.monotonic()
        if now >= next_draw:
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
                history_view_active,
                clear_prompt_active,
                failed_history_view_active,
            )
            next_draw = now + app.args.status_interval

        try:
            key_event = read_key_event(stdscr)
        except KeyboardInterrupt:
            break

        if key_event is None:
            time.sleep(0.05)
            continue

        key = key_event.key

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
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if clear_prompt_active:
            if key == 27:
                clear_prompt_active = False
                curses.curs_set(0)
                app.set_message("Clear prompt cancelled.")
            else:
                selected = key_to_circuit_number(key, app.args.circuits)
                if selected is None:
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                        history_view_active,
                        clear_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue
                clear_prompt_active = False
                curses.curs_set(0)
                interrupted = app.clear_slot_state(selected)
                if interrupted:
                    app.set_message(
                        f"Cleared circuit {selected}; active download interrupted."
                    )
                else:
                    app.set_message(f"Cleared circuit {selected}.")

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
                history_view_active,
                clear_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if history_view_active:
            if key in (ord("h"), ord("H"), 27):
                history_view_active = False
                viewer_scroll = 0
                app.set_message("History viewer closed.")
            elif key in (ord("c"), ord("C")):
                app.clear_success_history()
                viewer_scroll = 0
                app.set_message("Successful download history cleared.")
            elif key in (curses.KEY_UP, ord("k"), ord("K")):
                viewer_scroll = max(0, viewer_scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                viewer_scroll += 1
            elif key == curses.KEY_PPAGE:
                viewer_scroll = max(0, viewer_scroll - 10)
            elif key == curses.KEY_NPAGE:
                viewer_scroll += 10

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
                history_view_active,
                clear_prompt_active,
                failed_history_view_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if failed_history_view_active:
            if key in (ord("f"), ord("F"), 27):
                failed_history_view_active = False
                viewer_scroll = 0
                app.set_message("Failed history viewer closed.")
            elif key in (ord("c"), ord("C")):
                app.clear_failed_history()
                viewer_scroll = 0
                app.set_message("Failed download history cleared.")
            elif key in (curses.KEY_UP, ord("k"), ord("K")):
                viewer_scroll = max(0, viewer_scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                viewer_scroll += 1
            elif key == curses.KEY_PPAGE:
                viewer_scroll = max(0, viewer_scroll - 10)
            elif key == curses.KEY_NPAGE:
                viewer_scroll += 10

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
                history_view_active,
                clear_prompt_active,
                failed_history_view_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if retry_prompt_active:
            if key == 27:
                retry_prompt_active = False
                retry_prompt_text = ""
                curses.curs_set(0)
                app.set_message("Retry prompt cancelled.")
            else:
                selected = key_to_circuit_number(key, app.args.circuits)
                if selected is None:
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue
                retry_prompt_active = False
                retry_prompt_text = ""
                curses.curs_set(0)
                with app.lock:
                    slot = app.slots[selected - 1]
                    failed_download = slot.download is not None and bool(slot.download.error)
                    retry_url = slot.last_url
                if failed_download and retry_url:
                    app.set_message(f"Retrying failed URL on circuit {selected}.")
                    app.start_download(selected, retry_url)
                else:
                    app.set_message(
                        f"Circuit {selected} has no failed download URL to retry."
                    )

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if copy_prompt_active:
            if key == 27:
                copy_prompt_active = False
                curses.curs_set(0)
                app.set_message("Copy prompt cancelled.")
            else:
                selected = key_to_circuit_number(key, app.args.circuits)
                if selected is None:
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue
                copy_prompt_active = False
                curses.curs_set(0)
                with app.lock:
                    slot = app.slots[selected - 1]
                    url_to_copy = (slot.last_url or (slot.download.url if slot.download else "")).strip()
                if not url_to_copy:
                    app.set_message(f"Circuit {selected} has no URL to copy yet.")
                elif set_clipboard_text(url_to_copy):
                    app.set_message(f"Copied URL from circuit {selected} to clipboard.")
                else:
                    app.set_message(
                        "Could not write clipboard. Install wl-copy, xclip, or xsel."
                    )

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if replace_prompt_active:
            if key == 27:
                replace_prompt_active = False
                curses.curs_set(0)
                app.set_message("Replace prompt cancelled.")
            else:
                selected = key_to_circuit_number(key, app.args.circuits)
                if selected is None:
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue
                replace_prompt_active = False
                curses.curs_set(0)
                with app.lock:
                    already_rebuilding = app.slots[selected - 1].rebuilding
                if already_rebuilding:
                    app.set_message(f"Circuit {selected} is already rebuilding.")
                else:
                    interrupted = app.terminate_slot_download(selected)
                    app.append_slot_output(selected, "Manual circuit replace requested.")
                    app._start_thread(
                        target=app.replace_circuit,
                        name=f"tcc-manual-replace-{selected}",
                        args=(selected, "manual replace"),
                    )
                    if interrupted:
                        app.set_message(
                            f"Replacing circuit {selected}; active download interrupted."
                        )
                    else:
                        app.set_message(f"Replacing circuit {selected}.")

            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
            continue

        if prompt_slot is None:
            if key in (ord("q"), ord("Q")):
                break
            if key in (ord("h"), ord("H")):
                history_view_active = True
                failed_history_view_active = False
                viewer_scroll = 0
                curses.curs_set(0)
                app.set_message("Showing successful download history.")
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                    history_view_active,
                    clear_prompt_active,
                    failed_history_view_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("f"), ord("F")):
                failed_history_view_active = True
                history_view_active = False
                viewer_scroll = 0
                curses.curs_set(0)
                app.set_message("Showing failed download history (excluding eventually successful URLs).")
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                    history_view_active,
                    clear_prompt_active,
                    failed_history_view_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("x"), ord("X")):
                clear_prompt_active = True
                curses.curs_set(0)
                app.set_message(
                    f"Press {circuit_key_hint(app.args.circuits)} to clear that slot to ready."
                )
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                    history_view_active,
                    clear_prompt_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("r"), ord("R")):
                candidates = failed_retry_candidates(app)
                if len(candidates) == 1:
                    selected, retry_url = candidates[0]
                    app.set_message(f"Retrying failed URL on circuit {selected}.")
                    app.start_download(selected, retry_url)
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue

                if len(candidates) == 0:
                    app.set_message("No failed download URL available to retry.")
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
                    continue

                retry_prompt_active = True
                retry_prompt_text = ""
                curses.curs_set(1)
                app.set_message(
                    f"Multiple failed circuits. Press {circuit_key_hint(app.args.circuits)} to retry."
                )
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("n"), ord("N")):
                replace_prompt_active = True
                curses.curs_set(0)
                app.set_message(
                    f"Press {circuit_key_hint(app.args.circuits)} to replace that circuit now (active download will be interrupted)."
                )
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("c"), ord("C")):
                copy_prompt_active = True
                curses.curs_set(0)
                app.set_message(
                    f"Press {circuit_key_hint(app.args.circuits)} to copy that slot URL to clipboard."
                )
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            if key in (ord("p"), ord("P")):
                clip_url = clipboard_url_text().strip()
                if not clip_url:
                    app.set_message(
                        "Clipboard unavailable or empty. Install wl-paste, xclip, or xsel and copy a URL first."
                    )
                else:
                    selected = next_available_circuit_slot(app, after_slot=last_paste_slot)
                    if selected is None:
                        app.set_message("No available ready circuit for clipboard URL paste.")
                    else:
                        last_paste_slot = selected
                        app.set_message(f"Clipboard URL sent to circuit {selected}.")
                        app.start_download(selected, clip_url)
                viewer_scroll = draw_ui(
                    stdscr,
                    app,
                    prompt_slot,
                    prompt_text,
                    retry_prompt_active,
                    retry_prompt_text,
                    viewer_slot,
                    viewer_scroll,
                    copy_prompt_active,
                )
                next_draw = time.monotonic() + app.args.status_interval
                continue
            value = key_to_circuit_number(key, app.args.circuits)
            if value is not None:
                if 1 <= value <= app.args.circuits:
                    if key_event.ctrl:
                        with app.lock:
                            slot = app.slots[value - 1]
                            failed_download = slot.download is not None and bool(slot.download.error)
                            retry_url = slot.last_url
                        if failed_download and retry_url:
                            app.set_message(f"Retrying failed URL on circuit {value}.")
                            app.start_download(value, retry_url)
                        else:
                            app.set_message(
                                f"Circuit {value} has no failed download URL to retry with the Ctrl shortcut."
                            )
                        viewer_scroll = draw_ui(
                            stdscr,
                            app,
                            prompt_slot,
                            prompt_text,
                            retry_prompt_active,
                            retry_prompt_text,
                            viewer_slot,
                            viewer_scroll,
                            copy_prompt_active,
                        )
                        next_draw = time.monotonic() + app.args.status_interval
                        continue

                    if key_event.alt:
                        prompt_slot = value
                        prompt_text = clipboard_url_text()
                        curses.curs_set(1)
                        if prompt_text:
                            app.set_message(f"Clipboard pasted into URL prompt for circuit {value}.")
                        else:
                            app.set_message(
                                "Clipboard unavailable or empty. Enter URL manually (install wl-paste, xclip, or xsel)."
                            )
                        viewer_scroll = draw_ui(
                            stdscr,
                            app,
                            prompt_slot,
                            prompt_text,
                            retry_prompt_active,
                            retry_prompt_text,
                            viewer_slot,
                            viewer_scroll,
                            copy_prompt_active,
                        )
                        next_draw = time.monotonic() + app.args.status_interval
                        continue

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
                    viewer_scroll = draw_ui(
                        stdscr,
                        app,
                        prompt_slot,
                        prompt_text,
                        retry_prompt_active,
                        retry_prompt_text,
                        viewer_slot,
                        viewer_scroll,
                        copy_prompt_active,
                    )
                    next_draw = time.monotonic() + app.args.status_interval
            continue

        # URL prompt editing.
        if key in (27,):  # Esc
            prompt_slot = None
            prompt_text = ""
            curses.curs_set(0)
            app.set_message("Download prompt cancelled.")
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
        elif key in (curses.KEY_ENTER, 10, 13):
            selected = prompt_slot
            url = prompt_text.strip()
            prompt_slot = None
            prompt_text = ""
            curses.curs_set(0)
            if selected is not None:
                app.start_download(selected, url)
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            prompt_text = prompt_text[:-1]
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval
        elif 32 <= key <= 126:
            prompt_text += chr(key)
            viewer_scroll = draw_ui(
                stdscr,
                app,
                prompt_slot,
                prompt_text,
                retry_prompt_active,
                retry_prompt_text,
                viewer_slot,
                viewer_scroll,
                copy_prompt_active,
            )
            next_draw = time.monotonic() + app.args.status_interval


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def parse_port_list(value: str) -> List[int]:
    ports = TorCircuitCollector._coerce_port_list(value)
    if not ports:
        raise argparse.ArgumentTypeError("must contain at least one valid port")
    return ports


def _coerce_keepalive_urls(value: object) -> List[str]:
    candidates: List[str] = []
    if value is None:
        return candidates

    if isinstance(value, str):
        raw_parts = value.replace("\n", ",").split(",")
        candidates.extend(part.strip() for part in raw_parts if part.strip())
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if not isinstance(item, str):
                continue
            raw_parts = item.replace("\n", ",").split(",")
            candidates.extend(part.strip() for part in raw_parts if part.strip())

    return candidates


def normalize_keepalive_urls(primary_url: object, roster_value: object) -> List[str]:
    ordered: List[str] = []

    primary = str(primary_url or "").strip() if isinstance(primary_url, str) else ""
    if primary:
        ordered.append(primary)

    ordered.extend(_coerce_keepalive_urls(roster_value))

    # Keep built-in fallbacks available unless they were explicitly duplicated.
    ordered.extend(DEFAULT_KEEPALIVE_URLS)

    deduped: List[str] = []
    seen: Set[str] = set()
    for endpoint in ordered:
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise argparse.ArgumentTypeError(f"invalid keep-alive endpoint: {endpoint}")
        if endpoint in seen:
            continue
        seen.add(endpoint)
        deduped.append(endpoint)

    if not deduped:
        raise argparse.ArgumentTypeError("at least one keep-alive endpoint is required")
    return deduped


def key_to_circuit_number(key: int, max_circuits: int) -> Optional[int]:
    if ord("1") <= key <= ord("9"):
        value = key - ord("0")
        return value if value <= max_circuits else None
    return None


def circuit_key_hint(max_circuits: int) -> str:
    return f"1-{max_circuits}"


BUILTIN_DEFAULTS = {
    "circuits": 9,
    "socks_host": "127.0.0.1",
    "socks_port": 9050,
    "socks_port_candidates": [],
    "control_host": "127.0.0.1",
    "control_port": 9051,
    "control_port_candidates": [],
    "control_port_scan_radius": 10,
    "port_probe_timeout": 0.6,
    "control_password": None,
    "relay_cache_ttl": 300.0,
    "output_dir": ".",
    "status_interval": 5,
    "keepalive_interval": 60,
    "keepalive_url": DEFAULT_KEEPALIVE_URL,
    "keepalive_urls": list(DEFAULT_KEEPALIVE_URLS),
    "max_keepalive_failures": 2,
    "circuit_build_timeout": 108,
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
    ("tor", "socks_port_candidates"): "socks_port_candidates",
    ("tor", "control_host"): "control_host",
    ("tor", "control_port"): "control_port",
    ("tor", "control_port_candidates"): "control_port_candidates",
    ("tor", "control_port_scan_radius"): "control_port_scan_radius",
    ("tor", "port_probe_timeout"): "port_probe_timeout",
    ("tor", "control_password"): "control_password",
    ("collector", "circuits"): "circuits",
    ("collector", "relay_cache_ttl"): "relay_cache_ttl",
    ("collector", "keepalive_url"): "keepalive_url",
    ("collector", "keepalive_urls"): "keepalive_urls",
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
    parser.add_argument(
        "--socks-port-candidates",
        type=parse_port_list,
        help="comma-separated extra SOCKS ports to probe (example: 9150,9050)",
    )
    parser.add_argument("--control-host", help="Tor control host")
    parser.add_argument("--control-port", type=int, help="Tor control port")
    parser.add_argument(
        "--control-port-candidates",
        type=parse_port_list,
        help="comma-separated extra control ports to probe (example: 9151,9051)",
    )
    parser.add_argument(
        "--control-port-scan-radius",
        type=nonnegative_int,
        help="probe +/- this many ports around --control-port",
    )
    parser.add_argument(
        "--port-probe-timeout",
        type=positive_float,
        help="TCP connect timeout in seconds for port probes",
    )
    parser.add_argument("--control-password", help="Tor control password, if not using cookie auth")
    parser.add_argument(
        "--relay-cache-ttl",
        type=nonnegative_float,
        help="seconds to reuse a fetched relay/consensus snapshot across circuit builds (0 disables)",
    )
    parser.add_argument("--output-dir", help="directory for completed downloads")
    parser.add_argument("--status-interval", type=positive_int, help="CLI refresh interval in seconds")
    parser.add_argument("--keepalive-interval", type=positive_int, help="seconds between keep-alive requests per idle circuit")
    parser.add_argument("--keepalive-url", help="URL used for circuit keep-alive checks")
    parser.add_argument(
        "--keepalive-urls",
        help="comma-separated keep-alive endpoints tried in order as fallbacks",
    )
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
    try:
        keepalive_urls = normalize_keepalive_urls(args.keepalive_url, getattr(args, "keepalive_urls", None))
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    args.keepalive_urls = keepalive_urls
    args.keepalive_url = keepalive_urls[0]
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