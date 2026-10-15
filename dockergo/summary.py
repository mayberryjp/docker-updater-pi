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
    if not isinstance(data, dict):
        raise ValueError("Summary response must be a JSON object")
    checks = data.get("checks")
    updater = checks.get("docker_updater") if isinstance(checks, dict) else None
    images = updater.get("pending_images") if isinstance(updater, dict) else None
    if not isinstance(images, list):
        raise ValueError("Summary response is missing checks.docker_updater.pending_images")
    if any(not isinstance(ref, str) or not ref.strip() for ref in images):
        raise ValueError("Summary pending_images must contain non-empty image references")
    return [ref.strip() for ref in images]
