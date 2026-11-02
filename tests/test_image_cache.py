import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from dockergo.config import Config, Site
from dockergo.docker_ops import load_managed_images, save_managed_images, transfer
from dockergo.orchestrator import Orchestrator
from dockergo.status import Status
from dockergo.summary import fetch_pending_images


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.home = Site("HOME", "home", "", "", "", home=True)
        self.remote = Site("REMOTE", "remote", "", "", "https://summary")
        self.notifier = Mock()

    def make_orchestrator(self, sites=None):
        cfg = Config(sites=sites or [self.home, self.remote])
        orchestrator = Orchestrator(cfg, Status("test"), self.notifier)
        orchestrator._local = object()
        return orchestrator

    def test_cycle_processes_visible_remote_after_home_attempt(self):
        for home_connected in (True, False):
            with self.subTest(home_connected=home_connected):
                orchestrator = self.make_orchestrator()
                orchestrator.wifi = Mock()
                orchestrator.wifi.scan.return_value = {"home": 90, "remote": 80}
                workflow = Mock()
                workflow.home.return_value = home_connected
                orchestrator._do_home = workflow.home
                orchestrator._do_remote = workflow.remote

                orchestrator.cycle()

                self.assertEqual(workflow.mock_calls, [
                    call.home(self.home),
                    call.remote(self.remote),
                ])

    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.pull")
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           return_value=["needed:latest"])
    def test_home_sync_removes_only_no_longer_needed_managed_refs(
        self, _fetch, _load, pull, remove, save
    ):
        self.make_orchestrator()._sync_home(self.home)

        pull.assert_called_once()
        self.assertEqual(pull.call_args.args[1], "needed:latest")
        remove.assert_called_once()
        self.assertEqual(remove.call_args.args[1], "stale:latest")
        save.assert_called_once_with({"needed:latest"})

    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.pull")
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           side_effect=[["needed:latest"], RuntimeError("unreachable")])
    def test_home_sync_skips_pruning_when_any_summary_fails(
        self, _fetch, _load, _pull, remove, save
    ):
        second_remote = Site("REMOTE2", "remote2", "", "", "https://summary2")
        self.make_orchestrator([self.home, self.remote, second_remote])._sync_home(self.home)

        remove.assert_not_called()
        save.assert_called_once_with({"needed:latest", "stale:latest"})

    def test_cache_state_round_trips_and_rejects_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cached-images.json"
            self.assertEqual(load_managed_images(path), set())
            save_managed_images({"b:latest", "a:latest"}, path)
            self.assertEqual(load_managed_images(path), {"a:latest", "b:latest"})
            path.write_text('{"not": "a list"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid DockerGo image cache state"):
                load_managed_images(path)

    @patch("dockergo.summary.requests.get")
    def test_summary_uses_three_minute_timeout(self, get):
        get.return_value.json.return_value = {
            "checks": {"docker_updater": {"pending_images": ["app:latest"]}}
        }

        self.assertEqual(fetch_pending_images("https://summary"), ["app:latest"])
        get.assert_called_once_with("https://summary", timeout=180)

    def test_transfer_verifies_loaded_image_id(self):
        local = Mock()
        remote = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.images.get.return_value.save.return_value = [b"image archive"]
        remote.api.load_image.return_value = [{"stream": "Loaded image"}]
        remote.images.get.return_value.id = "sha256:expected"

        self.assertEqual(transfer(local, remote, "app:latest"), "sha256:expected")
        remote.images.get.assert_called_once_with("app:latest")

    def test_transfer_rejects_loaded_image_id_mismatch(self):
        local = Mock()
        remote = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.images.get.return_value.save.return_value = [b"image archive"]
        remote.api.load_image.return_value = []
        remote.images.get.return_value.id = "sha256:old"

        with self.assertRaisesRegex(RuntimeError, "Image verification failed"):
            transfer(local, remote, "app:latest")

    def test_transfer_rejects_load_error(self):
        local = Mock()
        remote = Mock()
        local.images.get.return_value.save.return_value = [b"image archive"]
        remote.api.load_image.return_value = [{"error": "load failed"}]

        with self.assertRaisesRegex(RuntimeError, "Image load failed"):
            transfer(local, remote, "app:latest")
        remote.images.get.assert_not_called()

    @patch("dockergo.orchestrator.dk.recreate")
    @patch("dockergo.orchestrator.dk.containers_using")
    @patch("dockergo.orchestrator.dk.transfer")
    @patch("dockergo.orchestrator.dk.remote_client")
    @patch("dockergo.orchestrator.dk.have_image", return_value=True)
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           return_value=["app:latest"])
    @patch.object(Orchestrator, "_connect", return_value=True)
    def test_remote_recreates_only_with_verified_image_id(
        self, _connect, _fetch, _have, remote_client, transfer_image, targets, recreate
    ):
        container = Mock()
        container.attrs = {"Name": "/app"}
        targets.return_value = [container]
        for verified in (True, False):
            with self.subTest(verified=verified):
                recreate.reset_mock()
                targets.reset_mock()
                transfer_image.return_value = "sha256:verified"
                transfer_image.side_effect = (
                    None if verified else RuntimeError("Image verification failed")
                )

                self.make_orchestrator()._do_remote(self.remote)

                if verified:
                    recreate.assert_called_once()
                    self.assertEqual(
                        recreate.call_args.args,
                        (remote_client.return_value, container, "sha256:verified"),
                    )
                else:
                    targets.assert_not_called()
                    recreate.assert_not_called()

    @patch("dockergo.summary.requests.get")
    def test_summary_rejects_missing_pending_images_field(self, get):
        response = Mock()
        response.json.return_value = {"checks": {"docker_updater": {}}}
        get.return_value = response

        with self.assertRaisesRegex(ValueError, "missing.*pending_images"):
            fetch_pending_images("https://summary")


if __name__ == "__main__":
    unittest.main()
