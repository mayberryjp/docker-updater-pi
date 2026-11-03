"""Docker operations via the Docker SDK.

Because the Pi runs a real Docker engine, this replaces the firmware's hand-rolled
Registry-V2 client, content-addressed blob store and OCI archive assembly:

* **pull** images into the local engine at home (the local store *is* the cache);
* **prepare** reusable image archives at home;
* **transfer** prepared archives into remote engines (``load``);
* **recreate** affected containers so they actually run the new image.

``import docker`` is deferred so the UI can be previewed without the SDK installed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .transfer import ArchiveReader, TransferProgress, run_transfer

ProgressCb = Callable[[str, Optional[float]], None]
CACHE_STATE_PATH = Path("/var/lib/dockergo/cached-images.json")
ARCHIVE_DIR = Path("/var/lib/dockergo/image-archives")


def _docker():
    import docker
    return docker


def local_client():
    return _docker().from_env(timeout=600)


def remote_client(docker_host: str, timeout: int = 120):
    return _docker().DockerClient(base_url=docker_host, timeout=timeout)


def have_image(client, ref: str) -> bool:
    from docker.errors import APIError, ImageNotFound
    try:
        client.images.get(ref)
        return True
    except (ImageNotFound, APIError):
        return False


def load_managed_images(path: Path = CACHE_STATE_PATH) -> set[str]:
    """Load image refs DockerGo has previously cached; a missing file means none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    if not isinstance(data, list) or any(
        not isinstance(ref, str) or not ref for ref in data
    ):
        raise ValueError(f"Invalid DockerGo image cache state in {path}")
    return set(data)


def save_managed_images(refs: set[str], path: Path = CACHE_STATE_PATH):
    """Atomically persist DockerGo-managed image refs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name, suffix=".tmp", delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(sorted(refs), temp, indent=2)
            temp.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def remove_cached_image(client, ref: str, archive_dir: Path = ARCHIVE_DIR):
    """Remove a managed image ref without forcing removal of in-use images."""
    from docker.errors import ImageNotFound
    try:
        image = client.images.get(ref)
    except ImageNotFound:
        image = None
    if image is not None:
        image.remove(force=False)
    _prune_archives(ref, archive_dir)


def pull(client, ref: str, platform: str, progress: Optional[ProgressCb] = None):
    """Pull an image, reporting coarse 0..1 progress aggregated across layers."""
    from docker.errors import APIError
    layers: dict = {}
    for event in client.api.pull(ref, platform=platform, stream=True, decode=True):
        if "error" in event:
            raise APIError(event["error"])
        layer_id = event.get("id")
        detail = event.get("progressDetail") or {}
        if layer_id and detail.get("total"):
            layers[layer_id] = (detail.get("current", 0), detail["total"])
        if progress and layers:
            current = sum(c for c, _ in layers.values())
            total = sum(t for _, t in layers.values())
            progress(event.get("status", ""), (current / total) if total else None)
    return client.images.get(ref)


def _archive_path(ref: str, image_id: str, archive_dir: Path) -> Path:
    ref_key = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    image_key = hashlib.sha256(image_id.encode("utf-8")).hexdigest()
    return archive_dir / f"{ref_key}-{image_key}.tar"


def _prune_archives(ref: str, archive_dir: Path, keep: Optional[Path] = None):
    ref_key = hashlib.sha256(ref.encode("utf-8")).hexdigest()
    for archive in archive_dir.glob(f"{ref_key}-*.tar"):
        if archive != keep:
            archive.unlink()


def prepare_archive(client, ref: str, archive_dir: Path = ARCHIVE_DIR) -> Path:
    """Export a reference once per exact image ID, publishing only complete archives."""
    image = client.images.get(ref)
    archive = _archive_path(ref, image.id, archive_dir)
    if archive.is_file():
        return archive
    archive_dir.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=archive_dir, suffix=".tmp", delete=False) as tar:
            temp_path = Path(tar.name)
            for chunk in client.api.get_image(ref):
                tar.write(chunk)
        if client.images.get(ref).id != image.id:
            raise RuntimeError(f"Image changed during export: {ref}")
        os.replace(temp_path, archive)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    _prune_archives(ref, archive_dir, keep=archive)
    return archive


def prepared_archive(local, ref: str, archive_dir: Path = ARCHIVE_DIR) -> Path:
    image = local.images.get(ref)
    archive = _archive_path(ref, image.id, archive_dir)
    if not archive.is_file():
        raise RuntimeError(f"Archive not prepared for {ref}; run home sync first")
    return archive


def transfer(local, remote, ref: str,
             progress: Optional[Callable[[TransferProgress], None]] = None,
             archive_dir: Path = ARCHIVE_DIR, *, stall_timeout: int = 120,
             load_timeout: int = 600, total_timeout: int = 7200,
             docker_host: Optional[str] = None):
    """Stream an archive with hard deadlines and verify before recreating containers."""
    archive = prepared_archive(local, ref, archive_dir)
    image_id = local.images.get(ref).id
    if archive != _archive_path(ref, image_id, archive_dir):
        raise RuntimeError(f"Image changed before transfer: {ref}")
    return run_transfer(
        docker_host or remote.api.base_url, ref, image_id, archive, progress,
        stall_timeout=stall_timeout, load_timeout=load_timeout,
        total_timeout=total_timeout,
    )


def _load_archive(remote, ref: str, image_id: str, archive: Path,
                  report: Callable[[int], None], event: Callable[[str], None]):
    with archive.open("rb") as tar:
        reader = ArchiveReader(tar, archive.stat().st_size, report)
        events = remote.api.load_image(reader)
        try:
            for item in events:
                if isinstance(item, dict) and (item.get("error") or item.get("errorDetail")):
                    raise RuntimeError(f"Image load failed: {item}")
                if isinstance(item, dict) and item.get("stream"):
                    event(item["stream"].strip())
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()
    loaded = remote.images.get(ref)
    if loaded.id != image_id:
        raise RuntimeError(
            f"Image verification failed for {ref}: expected {image_id}, got {loaded.id}"
        )
    return image_id


def containers_using(remote, ref: str) -> list:
    """Return containers whose image matches ``ref`` (by tag or resolved image id)."""
    from docker.errors import APIError, ImageNotFound
    try:
        target = remote.images.get(ref)
    except (ImageNotFound, APIError):
        target = None
    matches = []
    for container in remote.containers.list(all=True):
        image_field = container.attrs.get("Image", "")
        config_image = (container.attrs.get("Config", {}) or {}).get("Image", "")
        if ref in (image_field, config_image):
            matches.append(container)
        elif target is not None and image_field == target.id:
            matches.append(container)
    return matches


def recreate(remote, container, new_ref: str, progress: Optional[ProgressCb] = None):
    """Recreate ``container`` so it runs ``new_ref``, preserving config/host-config/networks.

    A plain restart would reuse the old image id, so we stop + remove + create a
    fresh container with the same settings (best-effort for the common fields).
    """
    from docker.errors import APIError
    attrs = container.attrs
    config = attrs.get("Config", {}) or {}
    host_config = attrs.get("HostConfig", {}) or {}
    name = attrs.get("Name", "").lstrip("/")
    networks = (attrs.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
    short_id = container.id[:12]
    api = remote.api

    if progress:
        progress(f"stop {name}", None)
    try:
        container.stop(timeout=20)
    except APIError:
        pass
    container.remove(force=True)

    networking_config = None
    if networks:
        first = next(iter(networks))
        endpoint = networks[first] or {}
        aliases = [a for a in (endpoint.get("Aliases") or []) if a != short_id] or None
        ipam = endpoint.get("IPAMConfig") or {}
        networking_config = api.create_networking_config({
            first: api.create_endpoint_config(
                aliases=aliases, ipv4_address=ipam.get("IPv4Address") or None
            )
        })

    if progress:
        progress(f"create {name}", None)
    created = api.create_container(
        image=new_ref,
        command=config.get("Cmd"),
        entrypoint=config.get("Entrypoint"),
        environment=config.get("Env"),
        labels=config.get("Labels") or {},
        working_dir=config.get("WorkingDir") or "",
        user=config.get("User") or "",
        hostname=config.get("Hostname") or None,
        tty=config.get("Tty", False),
        stdin_open=config.get("OpenStdin", False),
        ports=list((config.get("ExposedPorts") or {}).keys()) or None,
        name=name,
        detach=True,
        host_config=host_config,
        networking_config=networking_config,
    )
    new_id = created["Id"]

    for extra_name, endpoint in list(networks.items())[1:]:
        endpoint = endpoint or {}
        aliases = [a for a in (endpoint.get("Aliases") or []) if a != short_id] or None
        ipam = endpoint.get("IPAMConfig") or {}
        try:
            api.connect_container_to_network(
                new_id, extra_name, aliases=aliases,
                ipv4_address=ipam.get("IPv4Address") or None,
            )
        except APIError:
            pass

    if progress:
        progress(f"start {name}", None)
    api.start(new_id)
    return new_id
