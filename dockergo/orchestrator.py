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

from typing import Optional

from . import docker_ops as dk
from . import summary as summary_api
from .config import Config, Site
from .discord import DiscordNotifier
from .status import State, Status
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
            self.status.log(message)
            self.notifier.send(message)

    def cycle(self):
        self.announce("Scanning Wi-Fi", state=State.SCANNING, headline="Scanning Wi-Fi",
                      detail="", progress=None, online=False)
        visible = self.wifi.scan()
        home = self.cfg.home_site

        if home and home.ssid in visible:
            self._do_home(home)

        did_remote = False
        for site in self.cfg.sites:
            if site is home:
                continue
            if site.ssid in visible:
                did_remote = True
                self._do_remote(site)

        home_visible = bool(home and home.ssid in visible)
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

    def _do_home(self, home: Site):
        if not self._connect(home):
            return

        wanted: list = []
        for site in self.cfg.sites:
            if site.home:
                continue
            try:
                for ref in summary_api.fetch_pending_images(site.summary_url):
                    if ref not in wanted:
                        wanted.append(ref)
            except Exception as exc:
                self.announce(f"{site.name} summary failed: {exc}")

        self.announce(f"Home needs {len(wanted)} image(s)", state=State.HOME_SYNC,
                      headline="Downloading images", detail=f"{len(wanted)} queued",
                      progress=0.0 if wanted else None)

        for index, ref in enumerate(wanted, 1):
            short = _short(ref)
            if dk.have_image(self.local, ref):
                self.announce(f"Cached {short}")
                continue
            self.announce(f"Downloading {short}", headline=f"Downloading {short}",
                          detail=f"{index}/{len(wanted)}", progress=0.0)
            try:
                dk.pull(self.local, ref, self.cfg.platform_for(home),
                        progress=lambda text, frac: self.status.set(detail=text[:60], progress=frac))
                self.announce(f"Downloaded {short}")
            except Exception as exc:
                self.announce(f"Download failed {short}: {exc}", state=State.ERROR)

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

        for index, ref in enumerate(needed, 1):
            short = _short(ref)
            if not dk.have_image(self.local, ref):
                self.announce(f"Not cached, skipping {short}")
                continue
            self.announce(f"Uploading {short} to {site.name}", headline=f"Uploading {short}",
                          detail=f"{index}/{len(needed)}", progress=(index - 1) / max(1, len(needed)))
            try:
                dk.transfer(self.local, remote, ref,
                            progress=lambda text, frac: self.status.set(detail=text[:60]))
            except Exception as exc:
                self.announce(f"Upload failed {short}: {exc}", state=State.ERROR)
                continue

            try:
                targets = dk.containers_using(remote, ref)
            except Exception as exc:
                self.announce(f"Inspect failed {short}: {exc}")
                targets = []
            for container in targets:
                cname = container.attrs.get("Name", "").lstrip("/")
                self.announce(f"Restarting {cname}", headline=f"Restarting {cname}", detail=short)
                try:
                    dk.recreate(remote, container, ref,
                                progress=lambda text, frac: self.status.set(detail=text[:60]))
                    self.announce(f"Restarted {cname}")
                except Exception as exc:
                    self.announce(f"Restart failed {cname}: {exc}", state=State.ERROR)

        self.announce(f"{site.name} up to date", state=State.APPLYING,
                      headline=f"{site.name} done", detail="", progress=None)
