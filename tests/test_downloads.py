"""Offline regression tests; all files and histories live in temporary directories."""
import argparse
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "collector", Path(__file__).resolve().parents[1] / "tor-circuit-collector.py"
)
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def pdf_body():
    prefix = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    return prefix + b"xref\n0 2\n0000000000 65535 f \n0000000009 00000 n \ntrailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n" + str(len(prefix)).encode() + b"\n%%EOF\n"


def epub_body():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr("book.opf", '<package xmlns="http://www.idpf.org/2007/opf"/>')
    return buf.getvalue()


class Response:
    def __init__(self, body, headers=None, status=200, after_body=None):
        self.body = body
        self.headers = headers or {}
        self.status_code = status
        self.url = ""
        self.after_body = after_body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure")

    def iter_content(self, chunk_size):
        yield self.body
        if self.after_body:
            self.after_body()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        args = argparse.Namespace(**dict(collector.BUILTIN_DEFAULTS, output_dir=str(self.root), circuits=2, min_free_space_mb=0, integrity_retries=0))
        with mock.patch.object(collector.TorCircuitCollector, "load_persistent_histories"):
            self.app = collector.TorCircuitCollector(args)
        self.app.success_history_file = self.root / "success.jsonl"
        self.app.failed_history_file = self.root / "failed.jsonl"
        for slot in self.app.slots:
            slot.circuit_id = str(slot.index)
        self.queue = []
        self.app._start_thread = lambda target, name, args=(): self.queue.append((target, args))
        self.responses = []
        self.calls = []
        self.sessions = []

        def build_session(slot_index):
            session = mock.Mock()
            session.get.side_effect = lambda url, **kwargs: self.get_response(kwargs)
            self.sessions.append(session)
            return session

        self.app._build_session = build_session

    def get_response(self, kwargs):
        self.calls.append(dict(kwargs["headers"]))
        return self.responses.pop(0)

    def drain(self):
        count = 0
        while self.queue:
            target, args = self.queue.pop(0)
            target(*args)
            count += 1
            self.assertLess(count, 20, "unbounded retry")

    def download(self, responses, filename="file.bin"):
        self.responses.extend(responses)
        self.app.start_download(1, "https://example.invalid/" + filename)
        self.drain()
        return self.app.slots[0].download

    def test_short_response_never_published(self):
        state = self.download([Response(b"partial", {"Content-Length": "100"})])
        self.assertFalse(state.done)
        self.assertIn("expected 100 bytes, received 7", state.error)
        self.assertFalse(state.final_path.exists())
        self.assertFalse(state.part_path.exists())
        self.assertFalse(self.app.success_history_records)

    def test_installed_requests_silent_short_read(self):
        try:
            import requests
            from urllib3.response import HTTPResponse
        except ImportError:
            self.skipTest("optional Requests/urllib3 not installed")
        response = requests.Response()
        response.status_code = 200
        response.headers = {"Content-Length": "100"}
        response.raw = HTTPResponse(body=io.BytesIO(b"partial"), headers=response.headers, preload_content=False, enforce_content_length=False)
        state = self.download([response])
        self.assertIn("expected 100 bytes, received 7", state.error)
        self.assertFalse(state.done)

    def test_valid_documents_and_truncated_documents(self):
        for name, body in (("file.pdf", pdf_body()), ("file.epub", epub_body())):
            with self.subTest(name=name):
                state = self.download([Response(body)], name)
                self.assertTrue(state.done, state.error)
                self.assertEqual(body, state.final_path.read_bytes())
                state = self.download([Response(body[:40])], name)
                self.assertFalse(state.done)
                self.assertIn("truncated", state.error)

    def test_pdf_bad_xref_rejected(self):
        # Compute a deliberately invalid offset independently of fixture length.
        body = pdf_body().split(b"startxref")[0] + b"startxref\n2\n%%EOF\n"
        state = self.download([Response(body)], "file.pdf")
        self.assertIn("cross-reference", state.error)

    def test_epub_crc_failure(self):
        body = epub_body().replace(b"application/epub+zip", b"application/epub+bad", 1)
        state = self.download([Response(body)], "file.epub")
        self.assertIn("checksum", state.error)

    def test_epub_missing_package(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="missing.opf"/></rootfiles></container>')
        state = self.download([Response(buf.getvalue())], "file.epub")
        self.assertIn("missing package", state.error)

    def test_html_redirect_cannot_evade_pdf_validation(self):
        state = self.download([Response(b"<html>blocked</html>", {"Content-Disposition": 'attachment; filename="error.html"'})], "file.pdf")
        self.assertFalse(state.done)

    def test_recovery_prefers_verified_range(self):
        self.app.args.integrity_retries = 2
        state = self.download([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
            Response(b"def", {"Content-Length": "3", "Content-Range": "bytes 3-5/6", "ETag": '"v1"'}, 206),
        ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.final_path.read_bytes(), b"abcdef")
        self.assertEqual(self.calls[1]["Range"], "bytes=3-")
        self.assertEqual(self.calls[1]["If-Range"], '"v1"')
        self.assertEqual(len(self.sessions), 2)
        self.sessions[0].close.assert_called_once()
        self.assertEqual(len(self.app.success_history_records), 1)
        self.assertFalse(self.app.failed_history_records)

    def test_unknown_length_truncated_pdf_recovers_suffix(self):
        body = pdf_body()
        self.app.args.integrity_retries = 1
        state = self.download([
            Response(body[:40], {"ETag": '"v1"'}),
            Response(body[40:], {"Content-Range": "bytes 40-{}/{}".format(len(body)-1, len(body)), "ETag": '"v1"'}, 206),
        ], "file.pdf")
        self.assertTrue(state.done, state.error)
        self.assertEqual(body, state.final_path.read_bytes())

    def test_changed_etag_restarts_instead_of_mixing_versions(self):
        self.app.args.integrity_retries = 1
        state = self.download([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
            Response(b"XYZ", {"Content-Range": "bytes 3-5/6", "ETag": '"v2"'}, 206),
            Response(b"uvwxyz", {"Content-Length": "6", "ETag": '"v2"'}),
        ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.final_path.read_bytes(), b"uvwxyz")
        self.assertNotIn("Range", self.calls[-1])

    def test_weak_or_absent_etag_requires_full_retry(self):
        self.app.args.integrity_retries = 1
        for etag in ("", 'W/"weak"'):
            with self.subTest(etag=etag):
                state = self.download([Response(b"abc", {"Content-Length": "6", "ETag": etag}), Response(b"uvwxyz", {"Content-Length": "6"})])
                self.assertTrue(state.done, state.error)
                self.assertNotIn("Range", self.calls[-1])

    def test_crc_failure_retries_from_zero(self):
        self.app.args.integrity_retries = 1
        good = epub_body()
        bad = good.replace(b"application/epub+zip", b"application/epub+bad", 1)
        state = self.download([Response(bad, {"ETag": '"v1"'}), Response(good)], "file.epub")
        self.assertTrue(state.done, state.error)
        self.assertNotIn("Range", self.calls[-1])
        self.assertEqual(good, state.final_path.read_bytes())

    def test_retries_bounded_and_keep_partials_respected(self):
        self.app.args.integrity_retries = 2
        self.app.args.keep_partials = True
        state = self.download([Response(b"bad") for _ in range(3)], "file.pdf")
        self.assertFalse(state.done)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(list(self.root.glob("*.part"))), 3)
        self.assertFalse(self.app.success_history_records)

    def test_invalid_resumed_range_restarts(self):
        self.app.args.integrity_retries = 1
        state = self.download([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
            Response(b"d", {"Content-Range": "bytes 3-3/6", "ETag": '"v1"'}, 206),
            Response(b"abcdef", {"Content-Length": "6"}),
        ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.final_path.read_bytes(), b"abcdef")

    def test_416_retries_without_range(self):
        self.app.args.integrity_retries = 1
        state = self.download([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
            Response(b"", status=416),
            Response(b"abcdef", {"Content-Length": "6"}),
        ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(len(self.calls), 3)
        for headers in self.calls[2:]:
            self.assertNotIn("Range", headers)
            self.assertNotIn("If-Range", headers)

    def test_ignored_range_uses_full_body_without_extra_request(self):
        self.app.args.integrity_retries = 1
        state = self.download([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}),
            Response(b"uvwxyz", {"Content-Length": "6", "ETag": '"v2"'}),
        ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.final_path.read_bytes(), b"uvwxyz")
        self.assertEqual(len(self.calls), 2)

    def test_circuit_rebuild_preserves_flushed_prefix_and_retry_budget(self):
        self.app.controller = mock.Mock()
        self.app.controller.new_circuit.return_value = "replacement"
        self.app.controller.get_circuit.return_value.path = []
        self.app.choose_circuit_path = lambda slot: ([], "")
        self.responses.extend([
            Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}, after_body=lambda: self.app.replace_circuit(1, "dropped")),
            Response(b"def", {"Content-Range": "bytes 3-5/6", "ETag": '"v1"'}, 206),
        ])
        self.app.start_download(1, "https://example.invalid/file.bin", integrity_attempt=1)
        self.drain()
        state = self.app.slots[0].download
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.integrity_attempt, 1)
        self.assertEqual(state.final_path.read_bytes(), b"abcdef")
        self.assertEqual(self.calls[-1]["Range"], "bytes=3-")
        self.assertEqual(len(self.app.success_history_records), 1)

    def test_transport_truncation_also_resumes(self):
        try:
            import requests
        except ImportError:
            self.skipTest("optional Requests not installed")
        self.app.args.integrity_retries = 1

        def disconnect():
            raise requests.exceptions.ChunkedEncodingError("incomplete body")

        with mock.patch.object(collector, "requests", requests):
            state = self.download([
                Response(b"abc", {"Content-Length": "6", "ETag": '"v1"'}, after_body=disconnect),
                Response(b"def", {"Content-Range": "bytes 3-5/6", "ETag": '"v1"'}, 206),
            ])
        self.assertTrue(state.done, state.error)
        self.assertEqual(state.final_path.read_bytes(), b"abcdef")

    def test_integrity_retry_config_and_cli_precedence(self):
        config = self.root / "config.json"
        config.write_text('{"downloads": {"integrity_retries": 4}}', encoding="utf-8")
        self.assertEqual(collector.parse_args(["--config", str(config)]).integrity_retries, 4)
        self.assertEqual(collector.parse_args(["--config", str(config), "--integrity-retries", "1"]).integrity_retries, 1)

    def test_unsolicited_partial_rejected(self):
        state = self.download([Response(b"abc", {"Content-Range": "bytes 0-2/6"}, 206)])
        self.assertFalse(state.done)
        self.assertIn("HTTP 206", state.error)

    def test_encoded_body_rejected(self):
        state = self.download([Response(b"decoded", {"Content-Encoding": "gzip"})])
        self.assertFalse(state.done)

    def test_cancel_at_eof_does_not_publish_or_retry(self):
        self.app.args.integrity_retries = 2
        state = self.download([Response(b"body", after_body=lambda: self.app.terminate_slot_download(1))])
        self.assertFalse(state.done)
        self.assertFalse(state.final_path.exists())
        self.assertEqual(len(self.calls), 1)

    def test_stale_worker_cannot_adopt_new_state(self):
        self.app.start_download(1, "https://example.invalid/file.bin")
        old = self.app.slots[0].download
        self.app.clear_slot_state(1)
        self.app.start_download(1, old.url)
        self.responses.append(Response(b"fresh"))
        self.drain()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.app.slots[0].download.final_path.read_bytes(), b"fresh")

    def test_trash_preserves_copy_and_restarts_without_range(self):
        state = self.download([Response(b"original")])
        self.responses.append(Response(b"replacement"))
        self.app.trash_download(1)
        self.drain()
        copies = list((self.root / ".trash").glob("*/*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"original")
        self.assertEqual(state.final_path.read_bytes(), b"replacement")
        self.assertNotIn("Range", self.calls[-1])
        self.sessions[0].close.assert_called_once()

    def test_trash_missing_file_leaves_state_intact(self):
        state = self.download([Response(b"original")])
        state.final_path.unlink()
        self.app.trash_download(1)
        self.drain()
        self.assertIs(self.app.slots[0].download, state)
        self.assertEqual(len(self.calls), 1)

    def test_existing_destination_is_not_overwritten(self):
        (self.root / "file.bin").write_bytes(b"existing")
        state = self.download([Response(b"new")])
        self.assertEqual(state.filename, "file-1.bin")
        self.assertEqual((self.root / "file.bin").read_bytes(), b"existing")

    def test_main_screen_trash_selector_and_escape(self):
        for sequence, expected in (("T1q", [mock.call(1)]), ("T\x1bq", [])):
            with self.subTest(sequence=sequence), mock.patch.object(collector, "init_ui_colors"), mock.patch.object(collector.curses, "curs_set"), mock.patch.object(collector, "draw_ui", return_value=0), mock.patch.object(collector, "read_key_event", side_effect=[collector.KeyEvent(ord(k)) for k in sequence]), mock.patch.object(self.app, "trash_download") as trash:
                collector.curses_main(mock.Mock(), self.app)
                self.assertEqual(trash.call_args_list, expected)


if __name__ == "__main__":
    unittest.main()
