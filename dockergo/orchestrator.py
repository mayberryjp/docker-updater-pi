"""The state machine that ties everything together.

One cycle mirrors the firmware flow:

1. scan Wi-Fi;
2. if the **home** SSID is visible: connect, read *every* site's summary and
   ``docker pull`` the **union** of needed images into the local engine (cache);
3. for each visible **remote** site: connect, read its summary and, for every
   needed image already cached, upload it to that daemon and recreate the
   affected containers.

Home is always processed first (download where bandwidth is good), then remotes.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from . import docker_ops as dk
from . import summary as summary_api
from .config import Config, Site
from .discord import DiscordNotifier
from .status import State, Status
from .transfer import TransferProgress
from .wifi import WifiManager


def _short(ref: str) -> str:
    """Human-friendly image name, e.g. 'ghcr.io/foo/frigate:stable' -> 'frigate:stable'."""
    return ref.rsplit("/", 1)[-1]


class Orchestrator:
    def __init__(self, config: Config, status: Status, notifier: DiscordNotifier,
                 wifi: Optional[WifiManager] = None):
        self.cfg = config
        self.status = status
        self.notifier = notifier
        self.wifi = wifi or WifiManager()
        self._local = None

    @property
    def local(self):
        if self._local is None:
            self._local = dk.local_client()
        return self._local

    def announce(self, message, *, state=None, headline=None, detail=None,
                 progress=..., site=None, ssid=None, rssi=None, online=None):
        self.status.set(state=state, headline=headline, detail=detail, progress=progress,
                        site=site, ssid=ssid, rssi=rssi, online=online)
        if message:
            self.status.log(
                message, level=logging.ERROR if state == State.ERROR else logging.INFO,
                exc_info=sys.exc_info()[0] is not None,
            )
            self.notifier.send(message)

    def _transfer_progress(self, update: TransferProgress):
        self.status.set(
            transfer=update, progress=update.fraction,
            detail=f"{update.phase}: {update.detail}" if update.detail else update.phase,
        )

    def cycle(self):
        self.announce("Scanning Wi-Fi", state=State.SCANNING, headline="Scanning Wi-Fi",
                      detail="", progress=None, online=False)
        visible = self.wifi.scan()
        homes = self.cfg.home_sites
        home_visible = any(home.ssid in visible for home in homes)

        for home in homes:
            if home.ssid in visible:
                if self._do_home(home):
                    break

        did_remote = False
        for site in self.cfg.sites:
            if site.home:
                continue
            if site.ssid in visible:
                did_remote = True
                self._do_remote(site)

        if did_remote:
            visible = self.wifi.scan()
            for home in homes:
                if home.ssid in visible and self._connect_home(home):
                    break

        if not home_visible and not did_remote:
            self.announce("No known networks in range", state=State.OFFLINE,
                          headline="No known Wi-Fi", detail="", progress=None, online=False)

    def _connect(self, site: Site) -> bool:
        self.announce(f"Connecting to {site.name}", state=State.CONNECTING,
                      headline=f"Connecting {site.name}", detail=site.ssid,
                      progress=None, site=site.name, ssid=site.ssid)
        if self.wifi.connect(site.ssid, site.password):
            self.announce(f"Connected to {site.name}", rssi=self.wifi.signal(), online=True)
            return True
        self.announce(f"Failed to connect to {site.name}", state=State.ERROR,
                      headline="Wi-Fi failed", detail=site.ssid, online=False)
        return False

    def _connect_home(self, home: Site) -> bool:
        if not self._connect(home):
            return False

        wireguard = home.wireguard_connection
        if wireguard and not self.wifi.connect_wireguard(wireguard):
            self.announce(f"WireGuard failed for {home.name}", state=State.ERROR,
                          headline="VPN failed", detail=home.name, online=False)
            return False

        return True

    def _do_home(self, home: Site) -> bool:
        if not self._connect_home(home):
            return False
        self._sync_home(home)
        return True

    def _sync_home(self, home: Site):
        wanted: list = []
        summaries_complete = True
        for site in self.cfg.sites:
            if site.home:
                continue
            try:
                for ref in summary_api.fetch_pending_images(site.summary_url):
                    if ref not in wanted:
                        wanted.append(ref)
            except Exception as exc:
                summaries_complete = False
                self.announce(f"{site.name} summary failed: {exc}")

        cache_state_available = True
        try:
            managed = dk.load_managed_images()
        except Exception as exc:
            cache_state_available = False
            managed = set()
            self.announce(f"Image cache state unavailable; pruning disabled: {exc}")

        self.announce(f"Home needs {len(wanted)} image(s)", state=State.HOME_SYNC,
                      headline="Downloading images", detail=f"{len(wanted)} queued",
                      progress=0.0 if wanted else None)

        for index, ref in enumerate(wanted, 1):
            short = _short(ref)
            self.announce(f"Downloading {short}", headline=f"Downloading {short}",
                          detail=f"{index}/{len(wanted)}", progress=0.0)
            try:
                dk.pull(self.local, ref, self.cfg.platform_for(home),
                        transfer_progress=self._transfer_progress)
                managed.add(ref)
                self.announce(f"Downloaded {short}")
            except Exception as exc:
                self.announce(f"Download failed {short}: {exc}", state=State.ERROR)
                continue
            finally:
                self.status.set(transfer=None)
            self.announce(f"Preparing {short}", headline=f"Preparing {short}",
                          detail=f"{index}/{len(wanted)}", progress=None)
            try:
                dk.prepare_archive(self.local, ref, progress=self._transfer_progress)
            except Exception as exc:
                self.announce(f"Archive failed {short}: {exc}", state=State.ERROR)
            finally:
                self.status.set(transfer=None)

        if cache_state_available:
            if summaries_complete:
                for ref in sorted(managed - set(wanted)):
                    try:
                        dk.remove_cached_image(self.local, ref)
                        managed.remove(ref)
                        self.announce(f"Removed unused image {_short(ref)}")
                    except Exception as exc:
                        self.announce(f"Image cleanup failed {_short(ref)}: {exc}", state=State.ERROR)
            try:
                dk.save_managed_images(managed)
            except Exception as exc:
                self.announce(f"Could not save image cache state: {exc}", state=State.ERROR)

        self.announce("Home sync complete", state=State.HOME_SYNC,
                      headline="Home sync complete", detail="", progress=None)

    def _do_remote(self, site: Site):
        if not self._connect(site):
            return
        try:
            needed = summary_api.fetch_pending_images(site.summary_url)
        except Exception as exc:
            self.announce(f"{site.name} summary failed: {exc}", state=State.ERROR)
            return
        try:
            remote = dk.remote_client(site.docker_host)
        except Exception as exc:
            self.announce(f"{site.name} daemon unreachable: {exc}", state=State.ERROR)
            return

        self.announce(f"{site.name} needs {len(needed)} image(s)", state=State.APPLYING,
                      headline=f"Applying at {site.name}", detail=f"{len(needed)} images",
                      progress=0.0 if needed else None)

        queued = []
        failed = False
        for ref in needed:
            short = _short(ref)
            if not dk.have_image(self.local, ref):
                self.announce(f"Not cached, skipping {short}")
                failed = True
                continue
            try:
                size = dk.prepared_archive(self.local, ref).stat().st_size
                queued.append((size, ref))
            except Exception as exc:
                failed = True
                self.announce(f"Cannot queue {short}: {exc}", state=State.ERROR)
        queued.sort(key=lambda item: item[0])
        self.status.log("Upload order: " + ", ".join(
            f"{ref} ({size} bytes)" for size, ref in queued
        ))

        for index, (size, ref) in enumerate(queued, 1):
            short = _short(ref)
            self.announce(f"Uploading {short} to {site.name} ({size} bytes)",
                          state=State.APPLYING, headline=f"Uploading {short}",
                          detail=f"{index}/{len(queued)}", progress=0.0)
            try:
                image_id = dk.transfer(
                    self.local, remote, ref, progress=self._transfer_progress,
                    stall_timeout=self.cfg.upload_stall_timeout,
                    load_timeout=self.cfg.image_load_timeout,
                    total_timeout=self.cfg.transfer_timeout,
                    docker_host=site.docker_host,
                )
            except Exception as exc:
                failed = True
                self.announce(f"Upload failed {short}: {exc}", state=State.ERROR,
                              headline=f"Upload failed {short}", detail=str(exc)[:80],
                              progress=None)
                continue
            finally:
                self.status.set(transfer=None)

            try:
                targets = dk.containers_using(remote, ref)
            except Exception as exc:
                failed = True
                self.announce(f"Inspect failed {short}: {exc}", state=State.ERROR)
                targets = []
            for container in targets:
                cname = container.attrs.get("Name", "").lstrip("/")
                self.announce(f"Restarting {cname}", headline=f"Restarting {cname}",
                              detail=short, progress=None)
                try:
                    dk.recreate(remote, container, image_id,
                                progress=lambda text, frac: self.status.set(detail=text[:60]))
                    self.announce(f"Restarted {cname}")
                except Exception as exc:
                    failed = True
                    self.announce(f"Restart failed {cname}: {exc}", state=State.ERROR)

        remote.close()
        self.announce(
            f"{site.name} finished with failures" if failed else f"{site.name} up to date",
            state=State.ERROR if failed else State.APPLYING,
            headline=f"{site.name} incomplete" if failed else f"{site.name} done",
            detail="Retry next cycle" if failed else "", progress=None,
        )
