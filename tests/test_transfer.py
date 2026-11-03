import io
import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from dockergo.config import Config, ConfigError, Site, _parse
from dockergo.docker_ops import transfer
from dockergo.logging_setup import configure_logging
from dockergo.orchestrator import Orchestrator
from dockergo.status import State, Status
from dockergo.transfer import ArchiveReader, TransferProgress, run_transfer


class TransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.archive = Path(directory.name) / "image.tar"
        self.archive.write_bytes(b"archive" * 100)
        self.release = threading.Event()
        self.received = bytearray()

    def server(self, mode="success"):
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                payload = (
                    {"ApiVersion": "1.45"} if self.path == "/version"
                    else {"Id": "sha256:expected" if mode != "mismatch" else "sha256:old"}
                )
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if mode == "blocked_upload":
                    test.release.wait(15)
                    return
                remaining = int(self.headers["Content-Length"])
                while remaining:
                    chunk = self.rfile.read(min(8192, remaining))
                    if not chunk:
                        return
                    test.received.extend(chunk)
                    remaining -= len(chunk)
                if mode == "blocked_headers":
                    test.release.wait(15)
                    return
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    while True:
                        payload = (
                            {"errorDetail": {"message": "bad archive"}}
                            if mode == "error" else {"stream": "Loading layer"}
                        )
                        data = json.dumps(payload).encode() + b"\n"
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                        if mode == "blocked_stream":
                            test.release.wait(15)
                            return
                        if mode != "chatty":
                            self.wfile.write(b"0\r\n\r\n")
                            return
                        if test.release.wait(0.2):
                            return
                except (ConnectionError, OSError):
                    return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.addCleanup(cleanup)
        return f"tcp://127.0.0.1:{server.server_port}"

    def run_upload(self, host, **kwargs):
        before = {p.pid for p in multiprocessing.active_children()}
        try:
            return run_transfer(host, "app:latest", "sha256:expected", self.archive, **kwargs)
        finally:
            self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_real_sdk_upload_content_length_payload_and_verification(self):
        updates = []
        with self.assertLogs("dockergo.transfer", level="INFO") as logs:
            result = self.run_upload(self.server(), progress=updates.append)
        self.assertEqual(result, "sha256:expected")
        self.assertEqual(bytes(self.received), self.archive.read_bytes())
        self.assertTrue(any("Verified upload" in entry for entry in logs.output))
        self.assertEqual(updates[0].current, 0)
        self.assertEqual(updates[-1].current, len(self.received))
        self.assertTrue(any("stall_timeout_s=120" in entry for entry in logs.output))

    def test_blocked_upload_is_killed_and_rate_drops_to_zero(self):
        self.archive.write_bytes(b"x" * (16 * 1024 * 1024))
        updates = []
        started = time.monotonic()
        with self.assertLogs("dockergo.transfer", level="ERROR"):
            with self.assertRaisesRegex(TimeoutError, "upload stalled"):
                self.run_upload(
                    self.server("blocked_upload"), progress=updates.append,
                    stall_timeout=3, load_timeout=10, total_timeout=15,
                )
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(any(u.current > 0 and u.current < u.total for u in updates))
        self.assertTrue(any(u.rate == 0 and u.idle >= 1 for u in updates))

    def test_load_deadline_covers_headers_silent_stream_and_chatty_stream(self):
        for mode in ("blocked_headers", "blocked_stream", "chatty"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                updates = []
                with self.assertLogs("dockergo.transfer", level="ERROR"):
                    with self.assertRaisesRegex(TimeoutError, "daemon load/verification"):
                        self.run_upload(
                            self.server(mode), progress=updates.append,
                            stall_timeout=5, load_timeout=2, total_timeout=10,
                        )
                self.assertLess(time.monotonic() - started, 7)
                self.assertTrue(any(u.phase == "Loading/verifying" for u in updates))

    def test_overall_deadline_is_independent_of_activity(self):
        with self.assertLogs("dockergo.transfer", level="ERROR"):
            with self.assertRaisesRegex(TimeoutError, "overall deadline"):
                self.run_upload(
                    self.server("chatty"), stall_timeout=10,
                    load_timeout=10, total_timeout=2,
                )

    def test_real_worker_propagates_daemon_errors_and_id_mismatches(self):
        for mode, message in (("error", "Image load failed"), ("mismatch", "Image verification failed")):
            with self.subTest(mode=mode), self.assertLogs("dockergo.transfer", level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_upload(self.server(mode))

    def test_reader_bounds_reads_and_counts_bytes(self):
        size = 2 * 1024 * 1024 + 17
        report = Mock()
        reader = ArchiveReader(io.BytesIO(b"x" * size), size, report)
        self.assertEqual(len(reader), size)
        self.assertEqual(len(reader.read()), 1024 * 1024)
        self.assertEqual(len(reader.read(size)), 1024 * 1024)
        self.assertEqual(len(reader.read()), 17)
        self.assertEqual(reader.read(), b"")
        self.assertEqual([c.args[0] for c in report.call_args_list], [1048576, 2097152, size])

    def test_lcd_renders_rate_bytes_and_inactivity(self):
        from PIL import ImageDraw
        from dockergo.display.ui import StatusScreen

        status = Status("test")
        status.set(
            detail="Loading/verifying", progress=1.0,
            transfer=TransferProgress("Loading/verifying", 2097152, 2097152, 0, 12, 4),
        )
        draw_factory = ImageDraw.Draw
        for size in ((480, 320), (320, 240)):
            drawings = []

            def draw(image):
                result = Mock(wraps=draw_factory(image))
                drawings.append(result)
                return result

            with patch("dockergo.display.ui.ImageDraw.Draw", side_effect=draw):
                frame = StatusScreen(size).render(status.snapshot())
            self.assertEqual(frame.size, size)
            text = [c.args[1] for c in drawings[0].text.call_args_list]
            self.assertIn("2,097,152 / 2,097,152 B", text)
            self.assertIn("0.00 MiB/s (0 B/s)", text)
            self.assertIn("elapsed 12s  no bytes 4s", text)

    @patch("dockergo.docker_ops.run_transfer", return_value="sha256:expected")
    @patch("dockergo.docker_ops.prepared_archive")
    def test_transfer_wires_supervisor_deadlines_and_callback(self, prepared, run):
        local, remote, callback = Mock(), Mock(), Mock()
        local.images.get.return_value.id = "sha256:expected"
        with patch("dockergo.docker_ops._archive_path", return_value=self.archive):
            prepared.return_value = self.archive
            self.assertEqual(
                transfer(local, remote, "app:latest", callback,
                         stall_timeout=4, load_timeout=5, total_timeout=6),
                "sha256:expected",
            )
        run.assert_called_once_with(
            remote.api.base_url, "app:latest", "sha256:expected", self.archive, callback,
            stall_timeout=4, load_timeout=5, total_timeout=6,
        )

    def test_remote_sorts_by_archive_size_and_continues_after_failure(self):
        home = Site("HOME", "home", "", "", "", home=True)
        site = Site("REMOTE", "remote", "", "tcp://remote:2375", "https://summary")
        status = Status("test")
        orchestrator = Orchestrator(Config(sites=[home, site]), status, Mock(), Mock())
        orchestrator._local = Mock()
        orchestrator._connect = Mock(return_value=True)
        sizes = {"large": 1000, "small": 10, "medium": 100}

        def prepared(_local, ref):
            if ref == "missing":
                raise RuntimeError("Archive not prepared")
            result = Mock()
            result.stat.return_value.st_size = sizes[ref]
            return result

        def upload(_local, _remote, ref, **kwargs):
            kwargs["progress"](TransferProgress("Uploading", 5, sizes[ref], 5, 1, 0))
            self.assertEqual(status.snapshot().progress, 5 / sizes[ref])
            if ref == "medium":
                raise TimeoutError("stalled")
            return "sha256:expected"

        with patch("dockergo.orchestrator.summary_api.fetch_pending_images",
                   return_value=["large", "uncached", "medium", "missing", "small"]), \
             patch("dockergo.orchestrator.dk.have_image",
                   side_effect=lambda _local, ref: ref != "uncached"), \
             patch("dockergo.orchestrator.dk.prepared_archive", side_effect=prepared), \
             patch("dockergo.orchestrator.dk.remote_client") as remote, \
             patch("dockergo.orchestrator.dk.transfer", side_effect=upload) as uploads, \
             patch("dockergo.orchestrator.dk.containers_using", return_value=[]) as targets:
            orchestrator._do_remote(site)

        self.assertEqual([c.args[2] for c in uploads.call_args_list], ["small", "medium", "large"])
        self.assertEqual([c.args[1] for c in targets.call_args_list], ["small", "large"])
        remote.return_value.close.assert_called_once()
        self.assertEqual(status.snapshot().state, State.ERROR)
        self.assertIsNone(status.snapshot().transfer)
        self.assertIn("incomplete", status.snapshot().headline)
        self.assertFalse(any("up to date" in line for line in status.snapshot().log))

    def test_timeout_config_defaults_overrides_and_validation(self):
        raw = {"sites": [{"name": "home", "ssid": "home"}]}
        cfg = _parse(raw, Path("config.json"))
        self.assertEqual((cfg.upload_stall_timeout, cfg.image_load_timeout, cfg.transfer_timeout),
                         (120, 600, 7200))
        for key in ("upload_stall_timeout", "image_load_timeout", "transfer_timeout"):
            self.assertEqual(getattr(_parse({**raw, key: 42}, Path("config.json")), key), 42)
            for invalid in (0, -1, True, "10", 1.5, None):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ConfigError):
                    _parse({**raw, key: invalid}, Path("config.json"))

    def test_persistent_logging_and_rotation(self):
        path = self.archive.parent / "logs" / "dockergo.log"
        logger = configure_logging(str(path))
        handlers = logger.handlers[-2:]
        try:
            file_handler = handlers[0]
            file_handler.maxBytes = 100
            self.assertEqual(file_handler.backupCount, 3)
            status = Status("test")
            status.log("a status message written to disk")
            for handler in handlers:
                handler.flush()
            self.assertIn("a status message written to disk", path.read_text(encoding="utf-8"))
            for i in range(10):
                logger.info("rotation message %d %s", i, "x" * 80)
            self.assertTrue(path.with_name("dockergo.log.1").is_file())
            self.assertFalse(path.with_name("dockergo.log.4").exists())
        finally:
            for handler in handlers:
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
