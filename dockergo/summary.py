"""Summary-API client: fetch the images a site still needs."""

from __future__ import annotations

import requests


def fetch_pending_images(summary_url: str, timeout: int = 15) -> list:
    """GET the summary endpoint and return ``checks.docker_updater.pending_images``.

    Raises on network/HTTP errors; callers decide how to surface them.
    """
    response = requests.get(summary_url, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    checks = data.get("checks", {}) if isinstance(data, dict) else {}
    updater = checks.get("docker_updater", {}) if isinstance(checks, dict) else {}
    images = updater.get("pending_images", []) if isinstance(updater, dict) else []
    return [str(ref) for ref in images if ref]
