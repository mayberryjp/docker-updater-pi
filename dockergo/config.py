"""Configuration model and loader for DockerGo.

Config lives as JSON (human-editable). On the Pi the recommended home is
``/boot/firmware/dockergo/config.json`` so it can be edited from any PC by
mounting the boot partition.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_PLATFORM = "linux/amd64"

CONFIG_SEARCH_PATHS = (
    "/boot/firmware/dockergo/config.json",  # Bookworm: editable via the boot partition
    "/boot/dockergo/config.json",           # older Raspberry Pi OS
    "/etc/dockergo/config.json",
    "config.json",
)


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass
class Site:
    name: str
    ssid: str
    password: str
    docker_api: str
    summary_url: str
    home: bool = False
    image_platform: Optional[str] = None

    @property
    def docker_host(self) -> str:
        return normalize_docker_host(self.docker_api)


@dataclass
class Config:
    device_name: str = "dockergo"
    image_platform: str = DEFAULT_PLATFORM
    discord_webhook: Optional[str] = None
    poll_interval: int = 60
    sites: list = field(default_factory=list)

    @property
    def home_site(self) -> Optional[Site]:
        for site in self.sites:
            if site.home:
                return site
        return self.sites[0] if self.sites else None

    def platform_for(self, site: Site) -> str:
        return site.image_platform or self.image_platform


def normalize_docker_host(api: str) -> str:
    """Accept http(s)://host:port, tcp://host:port or bare host:port; return a tcp:// URL."""
    api = api.strip()
    if api.startswith(("tcp://", "unix://", "ssh://")):
        return api
    if api.startswith("http://"):
        return "tcp://" + api[len("http://"):]
    if api.startswith("https://"):
        return "tcp://" + api[len("https://"):]
    return "tcp://" + api


def find_config_path(explicit: Optional[str] = None) -> Optional[Path]:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("DOCKERGO_CONFIG")
    if env:
        candidates.append(env)
    candidates.extend(CONFIG_SEARCH_PATHS)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def load_config(path: Optional[str] = None) -> Config:
    cfg_path = find_config_path(path)
    if cfg_path is None:
        looked = [p for p in (path, os.environ.get("DOCKERGO_CONFIG")) if p]
        looked += list(CONFIG_SEARCH_PATHS)
        raise ConfigError("No config.json found. Looked in: " + ", ".join(looked))
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Cannot read {cfg_path}: {exc}") from exc
    return _parse(raw, cfg_path)


def _parse(raw, source: Path) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top-level JSON must be an object")

    sites_raw = raw.get("sites") or []
    if not isinstance(sites_raw, list) or not sites_raw:
        raise ConfigError(f"{source}: 'sites' must be a non-empty array")

    sites = []
    for i, entry in enumerate(sites_raw):
        try:
            sites.append(
                Site(
                    name=str(entry["name"]),
                    ssid=str(entry["ssid"]),
                    password=str(entry.get("password", "")),
                    docker_api=str(entry["docker_api"]),
                    summary_url=str(entry["summary_url"]),
                    home=bool(entry.get("home", False)),
                    image_platform=entry.get("image_platform"),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"{source}: sites[{i}] missing required key {exc}") from exc

    homes = [s for s in sites if s.home]
    if len(homes) > 1:
        raise ConfigError(f"{source}: more than one site has home=true")
    if not homes:
        sites[0].home = True  # fall back: the first site is treated as home

    return Config(
        device_name=str(raw.get("device_name", "dockergo")),
        image_platform=str(raw.get("image_platform", DEFAULT_PLATFORM)),
        discord_webhook=(raw.get("discord_webhook") or None),
        poll_interval=int(raw.get("poll_interval", 60)),
        sites=sites,
    )
