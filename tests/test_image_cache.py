import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dockergo.config import Config, Site
from dockergo.docker_ops import load_managed_images, save_managed_images
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

    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.have_image", return_value=True)
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           return_value=["needed:latest"])
    def test_home_sync_removes_only_no_longer_needed_managed_refs(
        self, _fetch, _load, _have, remove, save
    ):
        self.make_orchestrator()._sync_home(self.home)

        remove.assert_called_once()
        self.assertEqual(remove.call_args.args[1], "stale:latest")
        save.assert_called_once_with({"needed:latest"})

    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.have_image", return_value=True)
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           side_effect=[["needed:latest"], RuntimeError("unreachable")])
    def test_home_sync_skips_pruning_when_any_summary_fails(
        self, _fetch, _load, _have, remove, save
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
    def test_summary_rejects_missing_pending_images_field(self, get):
        response = Mock()
        response.json.return_value = {"checks": {"docker_updater": {}}}
        get.return_value = response

        with self.assertRaisesRegex(ValueError, "missing.*pending_images"):
            fetch_pending_images("https://summary")


if __name__ == "__main__":
    unittest.main()
