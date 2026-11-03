import io
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dockergo.config import Config, Site
from dockergo.docker_ops import prepare_archive, pull
from dockergo.logging_setup import configure_logging
from dockergo.orchestrator import Orchestrator
from dockergo.status import Status
from dockergo.transfer import TransferProgress


class ProgressTests(unittest.TestCase):
    def test_pull_reports_download_and_extraction_bytes_separately(self):
        client = Mock()
        client.api.pull.return_value = [
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 20, "total": 100}},
            {"id": "b", "status": "Downloading", "progressDetail": {"current": 10, "total": 50}},
            {"id": "a", "status": "Download complete"},
            {"id": "b", "status": "Download complete"},
            {"id": "a", "status": "Extracting", "progressDetail": {"current": 50, "total": 1000}},
            {"id": "a", "status": "Pull complete"},
            {"id": "b", "status": "Already exists"},
        ]
        updates = []
        with self.assertLogs("dockergo.docker_ops", level="DEBUG") as logs:
            image = pull(client, "app:latest", "linux/amd64", transfer_progress=updates.append)
        self.assertIs(image, client.images.get.return_value)
        downloads = [u for u in updates if u.phase == "Downloading"]
        self.assertTrue(any(u.current == 150 and u.total == 150 for u in downloads))
        extracted = [u for u in updates if u.phase == "Extracting"]
        self.assertTrue(any(u.current == 1000 and u.total == 1000 for u in extracted))
        self.assertTrue(any("layer=a" in line and "current_bytes=50" in line for line in logs.output))
        self.assertEqual(updates[-1].phase, "Download complete")

    def test_silent_pull_has_heartbeat_with_zero_rate_and_idle_time(self):
        client = Mock()

        def events():
            yield {"id": "a", "status": "Downloading",
                   "progressDetail": {"current": 20, "total": 100}}
            time.sleep(2.2)
            yield {"id": "a", "status": "Download complete"}

        client.api.pull.return_value = events()
        updates = []
        with self.assertLogs("dockergo.docker_ops", level="INFO") as logs:
            pull(client, "app:latest", "linux/amd64", transfer_progress=updates.append)
        self.assertEqual(updates[0].current, 0)
        self.assertTrue(any(u.current == 20 and u.rate == 0 and u.idle >= 1 for u in updates))
        self.assertTrue(any("current_bytes=20" in line and "no_byte_progress_s=" in line
                            for line in logs.output))

    def test_parallel_extraction_does_not_change_download_fraction(self):
        client, legacy = Mock(), Mock()
        client.api.pull.return_value = [
            {"id": "a", "status": "Downloading", "progressDetail": {"current": 20, "total": 100}},
            {"id": "b", "status": "Extracting", "progressDetail": {"current": 500, "total": 1000}},
            {"id": "a", "status": "Download complete"},
        ]
        pull(client, "app:latest", "linux/amd64", progress=legacy)
        self.assertEqual(legacy.call_args_list[1].args, ("Extracting", 0.2))

    def test_archive_reports_exact_bytes_unknown_total_then_final_size(self):
        client = Mock()
        client.images.get.return_value.id = "sha256:expected"
        client.api.get_image.return_value = [b"x" * 100, b"y" * 200]
        updates = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("dockergo.docker_ops", level="INFO") as logs:
                archive = prepare_archive(
                    client, "app:latest", Path(directory), progress=updates.append,
                )
            self.assertEqual(archive.stat().st_size, 300)
            self.assertTrue(any(u.current == 100 and u.total is None for u in updates))
            self.assertEqual(updates[-1].current, 300)
            self.assertEqual(updates[-1].total, 300)
            self.assertTrue(any("image_id=sha256:expected" in line for line in logs.output))
            updates.clear()
            prepare_archive(client, "app:latest", Path(directory), progress=updates.append)
            self.assertEqual(updates[-1].phase, "Archive cached")
            self.assertEqual(updates[-1].current, 300)
            client.api.get_image.assert_called_once()

    def test_debug_details_go_to_file_without_discord_or_console(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", new_callable=io.StringIO) as console:
            path = Path(directory) / "dockergo.log"
            logger = logging.getLogger("dockergo")
            previous_level = logger.level
            configure_logging(str(path))
            handlers = logger.handlers[-2:]
            try:
                logger.getChild("docker_ops").debug("layer=a current_bytes=123 total_bytes=456")
                Status("test").log("Uploading app")
                for handler in handlers:
                    handler.flush()
                self.assertIn("current_bytes=123", path.read_text(encoding="utf-8"))
                self.assertNotIn("current_bytes=123", console.getvalue())
                self.assertIn("Uploading app", console.getvalue())
            finally:
                for handler in handlers:
                    logger.removeHandler(handler)
                    handler.close()
                logger.setLevel(previous_level)

    def test_unknown_total_is_displayable_without_fake_percentage(self):
        from dockergo.display.ui import StatusScreen

        status = Status("test")
        status.set(transfer=TransferProgress("Preparing archive", 123, None, 0, 5, 4))
        frame = StatusScreen().render(status.snapshot())
        self.assertEqual(frame.size, (480, 320))

    def test_home_wires_download_and_archive_telemetry_without_discord_spam(self):
        home = Site("HOME", "home", "", "", "", home=True)
        remote = Site("REMOTE", "remote", "", "", "https://summary")
        status, notifier = Status("test"), Mock()
        orchestrator = Orchestrator(Config(sites=[home, remote]), status, notifier, Mock())
        orchestrator._local = Mock()

        def download(_client, _ref, _platform, *, transfer_progress):
            transfer_progress(TransferProgress("Downloading", 123, 456, 12, 1, 0))
            self.assertEqual(status.snapshot().transfer.current, 123)
            self.assertEqual(status.snapshot().progress, 123 / 456)

        def export(_client, _ref, *, progress):
            progress(TransferProgress("Preparing archive", 789, None, 12, 1, 0))
            self.assertEqual(status.snapshot().transfer.current, 789)
            self.assertIsNone(status.snapshot().progress)

        with patch("dockergo.orchestrator.summary_api.fetch_pending_images", return_value=["app"]), \
             patch("dockergo.orchestrator.dk.load_managed_images", return_value=set()), \
             patch("dockergo.orchestrator.dk.save_managed_images"), \
             patch("dockergo.orchestrator.dk.pull", side_effect=download), \
             patch("dockergo.orchestrator.dk.prepare_archive", side_effect=export):
            orchestrator._sync_home(home)
        self.assertIsNone(status.snapshot().transfer)
        messages = [c.args[0] for c in notifier.send.call_args_list]
        self.assertFalse(any("123" in message or "789" in message for message in messages))

    def test_failed_display_write_is_logged_and_unchanged_status_is_retried(self):
        from dockergo.display import DisplayLoop

        framebuffer = Mock()
        framebuffer.size = (480, 320)
        framebuffer.blit.side_effect = [OSError("panel not writable"), None]
        with patch("dockergo.display.open_framebuffer", return_value=framebuffer):
            display = DisplayLoop(Status("test"))
        display.screen = Mock()
        display._stop = Mock()
        display._stop.is_set.side_effect = [False, False, True]
        with self.assertLogs("dockergo.display", level="ERROR") as logs:
            display._run()
        self.assertEqual(framebuffer.blit.call_count, 2)
        self.assertTrue(any("panel not writable" in line for line in logs.output))
        self.assertIsNotNone(display._last_update)
        framebuffer.close()


if __name__ == "__main__":
    unittest.main()
