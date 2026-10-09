"""Wi-Fi scan / prioritized connect via NetworkManager (nmcli).

NetworkManager is the default on Raspberry Pi OS Bookworm. All calls degrade
gracefully (return empty / False) when nmcli is absent, e.g. on a dev laptop.
"""

from __future__ import annotations

import subprocess
from typing import Optional


def _run(args, timeout=25):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class WifiManager:
    def __init__(self, iface: str = "wlan0"):
        self.iface = iface

    def available(self) -> bool:
        try:
            return _run(["nmcli", "--version"]).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def scan(self) -> dict:
        """Return ``{ssid: signal_percent}`` for visible networks (strongest wins on dupes)."""
        try:
            result = _run(["nmcli", "--terse", "--fields", "SSID,SIGNAL",
                           "device", "wifi", "list", "--rescan", "yes"])
        except (OSError, subprocess.SubprocessError):
            return {}
        seen: dict = {}
        for line in result.stdout.splitlines():
            if not line:
                continue
            ssid, _, signal = line.rpartition(":")  # terse escapes ':' inside the SSID as '\:'
            ssid = ssid.replace("\\:", ":").strip()
            if not ssid:
                continue
            try:
                value = int(signal)
            except ValueError:
                continue
            if ssid not in seen or value > seen[ssid]:
                seen[ssid] = value
        return seen

    def connect(self, ssid: str, password: str = "", timeout: int = 45) -> bool:
        args = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        args += ["ifname", self.iface]
        try:
            return _run(args, timeout=timeout).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def connect_wireguard(self, connection: str, timeout: int = 45) -> bool:
        active = self._active_connections(timeout)
        if active is None:
            return False
        if connection in active:
            return True
        try:
            result = _run(["nmcli", "connection", "up", "id", connection],
                          timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            return True
        active = self._active_connections(timeout)
        return active is not None and connection in active

    def disconnect_wireguard(self, connection: str, timeout: int = 25) -> bool:
        active = self._active_connections(timeout)
        if active is None:
            return False
        if connection not in active:
            return True
        try:
            result = _run(["nmcli", "connection", "down", "id", connection],
                          timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode == 0:
            active = self._active_connections(timeout)
            return active is not None and connection not in active
        active = self._active_connections(timeout)
        return active is not None and connection not in active

    def _active_connections(self, timeout: int) -> Optional[set]:
        try:
            active = _run(["nmcli", "--terse", "--escape", "no", "--fields", "NAME",
                           "connection", "show", "--active"], timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        if active.returncode != 0:
            return None
        return set(active.stdout.splitlines())

    def signal(self) -> int:
        """Signal strength (0-100) of the active connection, or 0."""
        try:
            result = _run(["nmcli", "--terse", "--fields", "ACTIVE,SIGNAL",
                           "device", "wifi"])
        except (OSError, subprocess.SubprocessError):
            return 0
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                try:
                    return int(parts[1])
                except ValueError:
                    return 0
        return 0

    def disconnect(self):
        try:
            _run(["nmcli", "device", "disconnect", self.iface])
        except (OSError, subprocess.SubprocessError):
            pass
