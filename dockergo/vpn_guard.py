"""Keep configured WireGuard connections limited to their home Wi-Fi SSIDs."""

from __future__ import annotations

import subprocess
import sys

from .config import ConfigError, load_config
from .wifi import WifiManager


def active_ssid() -> str:
    result = subprocess.run(
        ["nmcli", "--terse", "--escape", "no", "--fields", "IN-USE,SSID",
         "device", "wifi"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not query active Wi-Fi")
    for line in result.stdout.splitlines():
        in_use, separator, ssid = line.partition(":")
        if separator and in_use in ("*", "yes"):
            return ssid
    return ""


def reconcile() -> None:
    config = load_config()
    homes = [site for site in config.home_sites if site.wireguard_connection]
    if not homes:
        return

    current_ssid = active_ssid()
    desired = next((site.wireguard_connection for site in homes
                    if site.ssid == current_ssid), None)
    wifi = WifiManager()
    for home in homes:
        connection = home.wireguard_connection
        if connection != desired and not wifi.disconnect_wireguard(connection):
            raise RuntimeError(f"could not deactivate WireGuard connection {connection}")


def main() -> int:
    try:
        reconcile()
    except (ConfigError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"dockergo WireGuard guard: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())