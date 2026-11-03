import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from dockergo.config import Config, Site
from dockergo.docker_ops import (
    _load_archive, local_client, load_managed_images, prepare_archive,
    remove_cached_image, save_managed_images, transfer,
)
from dockergo.orchestrator import Orchestrator
from dockergo.status import State, Status
from dockergo.summary import fetch_pending_images


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.home = Site("HOME", "home", "", "", "", home=True)
        self.remote = Site("REMOTE", "remote", "", "", "https://summary")
        self.notifier = Mock()
        archives = tempfile.TemporaryDirectory()
        self.addCleanup(archives.cleanup)
        self.archive_dir = Path(archives.name)

    def make_orchestrator(self, sites=None):
        cfg = Config(sites=sites or [self.home, self.remote])
        orchestrator = Orchestrator(cfg, Status("test"), self.notifier)
        orchestrator._local = object()
        return orchestrator

    @patch("dockergo.docker_ops._docker")
    def test_local_client_allows_long_image_exports(self, docker_sdk):
        client = local_client()

        docker_sdk.return_value.from_env.assert_called_once_with(timeout=600)
        self.assertIs(client, docker_sdk.return_value.from_env.return_value)

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
                orchestrator._connect_home = workflow.idle_home

                orchestrator.cycle()

                self.assertEqual(workflow.mock_calls, [
                    call.home(self.home),
                    call.remote(self.remote),
                    call.idle_home(self.home),
                ])

    def test_cycle_returns_home_only_if_visible_after_remote_work(self):
        for home_visible in (True, False):
            with self.subTest(home_visible=home_visible):
                home = Site("HOME", "home", "password", "", "", home=True,
                            wireguard_connection="home-vpn")
                orchestrator = self.make_orchestrator([home, self.remote])
                orchestrator.wifi = Mock()
                orchestrator.wifi.scan.side_effect = [
                    {"remote": 80}, {"home": 90} if home_visible else {"remote": 80},
                ]
                orchestrator._do_remote = Mock()
                orchestrator._sync_home = Mock()

                orchestrator.cycle()

                orchestrator._do_remote.assert_called_once_with(self.remote)
                orchestrator._sync_home.assert_not_called()
                self.assertEqual(orchestrator.wifi.scan.call_count, 2)
                if home_visible:
                    orchestrator.wifi.connect.assert_called_once_with("home", "password")
                    orchestrator.wifi.connect_wireguard.assert_called_once_with("home-vpn")
                else:
                    orchestrator.wifi.connect.assert_not_called()
                    orchestrator.wifi.connect_wireguard.assert_not_called()

    def test_cycle_tries_next_visible_home_when_idle_connection_fails(self):
        second_home = Site("HOME2", "home2", "", "", "", home=True)
        orchestrator = self.make_orchestrator([self.home, second_home, self.remote])
        orchestrator.wifi = Mock()
        orchestrator.wifi.scan.side_effect = [
            {"remote": 80}, {"home": 90, "home2": 85},
        ]
        orchestrator._do_remote = Mock()
        orchestrator._connect_home = Mock(side_effect=[False, True])

        orchestrator.cycle()

        self.assertEqual(orchestrator._connect_home.call_args_list, [
            call(self.home), call(second_home),
        ])

    def test_home_only_cycle_stays_home_without_reconnecting(self):
        orchestrator = self.make_orchestrator()
        orchestrator.wifi = Mock()
        orchestrator.wifi.scan.return_value = {"home": 90}
        orchestrator._sync_home = Mock()

        orchestrator.cycle()

        orchestrator.wifi.scan.assert_called_once()
        orchestrator.wifi.connect.assert_called_once_with("home", "")
        orchestrator._sync_home.assert_called_once_with(self.home)

    @patch("dockergo.orchestrator.dk.prepare_archive")
    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.pull")
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           return_value=["needed:latest"])
    def test_home_sync_removes_only_no_longer_needed_managed_refs(
        self, _fetch, _load, pull, remove, save, prepare
    ):
        orchestrator = self.make_orchestrator()
        orchestrator._sync_home(self.home)

        self.assertEqual(orchestrator.status.snapshot().state, State.HOME_SYNC)
        self.assertEqual(orchestrator.status.snapshot().headline, "Home sync complete")
        pull.assert_called_once()
        self.assertEqual(pull.call_args.args[1], "needed:latest")
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[1], "needed:latest")
        remove.assert_called_once()
        self.assertEqual(remove.call_args.args[1], "stale:latest")
        save.assert_called_once_with({"needed:latest"})

    @patch("dockergo.orchestrator.dk.prepare_archive")
    @patch("dockergo.orchestrator.dk.save_managed_images")
    @patch("dockergo.orchestrator.dk.remove_cached_image")
    @patch("dockergo.orchestrator.dk.pull")
    @patch("dockergo.orchestrator.dk.load_managed_images",
           return_value={"needed:latest", "stale:latest"})
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           side_effect=[["needed:latest"], RuntimeError("unreachable")])
    def test_home_sync_skips_pruning_when_any_summary_fails(
        self, _fetch, _load, _pull, remove, save, _prepare
    ):
        second_remote = Site("REMOTE2", "remote2", "", "", "https://summary2")
        orchestrator = self.make_orchestrator([self.home, self.remote, second_remote])
        orchestrator._sync_home(self.home)

        self.assertEqual(orchestrator.status.snapshot().state, State.ERROR)
        self.assertEqual(orchestrator.status.snapshot().headline, "Home sync incomplete")
        remove.assert_not_called()
        save.assert_called_once_with({"needed:latest", "stale:latest"})

    def test_home_sync_cache_errors_are_not_reported_as_success(self):
        for operation in ("load", "remove", "save"):
            with self.subTest(operation=operation), \
                 patch("dockergo.orchestrator.summary_api.fetch_pending_images", return_value=["app"]), \
                 patch("dockergo.orchestrator.dk.pull"), \
                 patch("dockergo.orchestrator.dk.prepare_archive"), \
                 patch("dockergo.orchestrator.dk.load_managed_images",
                       side_effect=RuntimeError("invalid state") if operation == "load" else None,
                       return_value={"stale"}) as load, \
                 patch("dockergo.orchestrator.dk.remove_cached_image",
                       side_effect=RuntimeError("in use") if operation == "remove" else None), \
                 patch("dockergo.orchestrator.dk.save_managed_images",
                       side_effect=OSError("disk full") if operation == "save" else None), \
                 self.assertLogs("dockergo", level="INFO"):
                orchestrator = self.make_orchestrator()
                orchestrator._sync_home(self.home)
                load.assert_called_once()
                self.assertEqual(orchestrator.status.snapshot().state, State.ERROR)
                self.assertIn("1/1 ready", orchestrator.status.snapshot().detail)

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
        local.api.get_image.return_value = [b"image archive"]
        remote.api.load_image.return_value = [{"stream": "Loaded image"}]
        remote.images.get.return_value.id = "sha256:expected"

        archive = prepare_archive(local, "app:latest", self.archive_dir)
        self.assertEqual(
            _load_archive(remote, "app:latest", "sha256:expected", archive, Mock(), Mock()),
            "sha256:expected",
        )
        remote.images.get.assert_called_once_with("app:latest")

    def test_archive_is_reused_across_home_syncs_and_remote_sites(self):
        local = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.api.get_image.return_value = [b"image ", b"archive"]

        archive = prepare_archive(local, "app:latest", self.archive_dir)
        self.assertEqual(prepare_archive(local, "app:latest", self.archive_dir), archive)
        for remote in (Mock(), Mock()):
            remote.images.get.return_value.id = "sha256:expected"
            payloads = []

            def load_image(stream):
                payloads.append(stream.read())
                return []

            remote.api.load_image.side_effect = load_image
            self.assertEqual(
                _load_archive(remote, "app:latest", "sha256:expected", archive, Mock(), Mock()),
                "sha256:expected",
            )
            self.assertEqual(payloads, [b"image archive"])

        local.api.get_image.assert_called_once_with("app:latest", chunk_size=64 * 1024)

    def test_changed_image_gets_new_archive_and_removes_old_version(self):
        local = Mock()
        local.images.get.return_value.id = "sha256:old"
        local.api.get_image.return_value = [b"old archive"]
        old = prepare_archive(local, "app:latest", self.archive_dir)

        local.images.get.return_value.id = "sha256:new"
        local.api.get_image.return_value = [b"new archive"]
        new = prepare_archive(local, "app:latest", self.archive_dir)

        self.assertNotEqual(old, new)
        self.assertFalse(old.exists())
        self.assertEqual(new.read_bytes(), b"new archive")
        self.assertEqual(local.api.get_image.call_count, 2)

    def test_failed_export_keeps_old_archive_and_removes_partial_file(self):
        local = Mock()
        local.images.get.return_value.id = "sha256:old"
        local.api.get_image.return_value = [b"old archive"]
        old = prepare_archive(local, "app:latest", self.archive_dir)
        local.images.get.return_value.id = "sha256:new"

        def broken_export(_ref, **_kwargs):
            yield b"partial archive"
            raise RuntimeError("export interrupted")

        local.api.get_image.side_effect = broken_export
        with self.assertRaisesRegex(RuntimeError, "export interrupted"):
            prepare_archive(local, "app:latest", self.archive_dir)

        self.assertEqual(list(self.archive_dir.iterdir()), [old])
        with self.assertRaisesRegex(RuntimeError, "Archive not prepared"):
            transfer(local, Mock(), "app:latest", archive_dir=self.archive_dir)

    def test_export_rejects_reference_changing_during_save(self):
        local = Mock()
        local.images.get.side_effect = [Mock(id="sha256:old"), Mock(id="sha256:new")]
        local.api.get_image.return_value = [b"archive"]

        with self.assertRaisesRegex(RuntimeError, "Image changed during export"):
            prepare_archive(local, "app:latest", self.archive_dir)
        self.assertEqual(list(self.archive_dir.iterdir()), [])

    def test_transfer_does_not_export_when_archive_is_missing(self):
        local = Mock()
        local.images.get.return_value.id = "sha256:expected"
        remote = Mock()

        with self.assertRaisesRegex(RuntimeError, "Archive not prepared"):
            transfer(local, remote, "app:latest", archive_dir=self.archive_dir)
        local.api.get_image.assert_not_called()
        remote.api.load_image.assert_not_called()

    def test_removing_cached_reference_removes_only_its_archives(self):
        local = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.api.get_image.return_value = [b"archive"]
        removed = prepare_archive(local, "app:latest", self.archive_dir)
        kept = prepare_archive(local, "other:latest", self.archive_dir)

        remove_cached_image(local, "app:latest", self.archive_dir)

        local.images.get.return_value.remove.assert_called_once_with(force=False)
        self.assertFalse(removed.exists())
        self.assertTrue(kept.exists())

    def test_transfer_rejects_loaded_image_id_mismatch(self):
        local = Mock()
        remote = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.api.get_image.return_value = [b"image archive"]
        remote.api.load_image.return_value = []
        remote.images.get.return_value.id = "sha256:old"

        archive = prepare_archive(local, "app:latest", self.archive_dir)
        with self.assertRaisesRegex(RuntimeError, "Image verification failed"):
            _load_archive(remote, "app:latest", "sha256:expected", archive, Mock(), Mock())

    def test_transfer_rejects_load_error(self):
        local = Mock()
        remote = Mock()
        local.images.get.return_value.id = "sha256:expected"
        local.api.get_image.return_value = [b"image archive"]
        remote.api.load_image.return_value = [{"error": "load failed"}]

        archive = prepare_archive(local, "app:latest", self.archive_dir)
        with self.assertRaisesRegex(RuntimeError, "Image load failed"):
            _load_archive(remote, "app:latest", "sha256:expected", archive, Mock(), Mock())
        remote.images.get.assert_not_called()

    @patch("dockergo.orchestrator.dk.recreate")
    @patch("dockergo.orchestrator.dk.containers_using")
    @patch("dockergo.orchestrator.dk.transfer")
    @patch("dockergo.orchestrator.dk.remote_client")
    @patch("dockergo.orchestrator.dk.have_image", return_value=True)
    @patch("dockergo.orchestrator.dk.prepared_archive")
    @patch("dockergo.orchestrator.summary_api.fetch_pending_images",
           return_value=["app:latest"])
    @patch.object(Orchestrator, "_connect", return_value=True)
    def test_remote_recreates_only_with_verified_image_id(
        self, _connect, _fetch, archive, _have, remote_client, transfer_image, targets, recreate
    ):
        archive.return_value.stat.return_value.st_size = 100
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
