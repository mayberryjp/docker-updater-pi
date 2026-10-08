"""Docker operations via the Docker SDK.

Because the Pi runs a real Docker engine, this replaces the firmware's hand-rolled
Registry-V2 client, content-addressed blob store and OCI archive assembly:

* **pull** images into the local engine at home (the local store *is* the cache);
* **transfer** a cached image into a remote engine (``save`` -> ``load``);
* **recreate** affected containers so they actually run the new image.

``import docker`` is deferred so the UI can be previewed without the SDK installed.
"""

from __future__ import annotations

import tempfile
from typing import Callable, Optional

ProgressCb = Callable[[str, Optional[float]], None]


def _docker():
    import docker
    return docker


def local_client():
    return _docker().from_env()


def remote_client(docker_host: str, timeout: int = 120):
    return _docker().DockerClient(base_url=docker_host, timeout=timeout)


def have_image(client, ref: str) -> bool:
    from docker.errors import APIError, ImageNotFound
    try:
        client.images.get(ref)
        return True
    except (ImageNotFound, APIError):
        return False


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


def transfer(local, remote, ref: str, progress: Optional[ProgressCb] = None):
    """Stream an image from the local engine into a remote engine (``save`` -> ``load``).

    The tar is spooled to a temp file and streamed into the remote, so large
    images never have to fit in RAM.
    """
    image = local.images.get(ref)
    with tempfile.NamedTemporaryFile(suffix=".tar") as tar:
        for chunk in image.save(named=True):
            tar.write(chunk)
        tar.flush()
        tar.seek(0)
        for event in remote.api.load_image(tar):
            if progress and isinstance(event, dict) and event.get("stream"):
                progress(event["stream"].strip(), None)


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
