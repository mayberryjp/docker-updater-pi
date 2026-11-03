"""Supervise uploads in a disposable process, including blocked SDK/socket calls."""

from __future__ import annotations

import logging
import multiprocessing
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger(__name__)


@dataclass
class TransferProgress:
    phase: str
    current: int
    total: int
    rate: float
    elapsed: float
    idle: float

    @property
    def text(self) -> str:
        return (
            f"{self.phase}: {self.current / 1048576:.1f}/{self.total / 1048576:.1f} MiB"
            f" | {self.rate / 1048576:.2f} MiB/s"
            f" | {int(self.elapsed)}s | idle {int(self.idle)}s"
        )


class ArchiveReader:
    """Bound reads and report bytes handed to the HTTP transport, not server ACKs."""

    def __init__(self, stream, total: int, report: Callable[[int], None]):
        self.stream = stream
        self.total = total
        self.report = report
        self.current = 0
        self.reported = 0
        self.last_report = time.monotonic()

    def __len__(self):
        return self.total

    def read(self, size=-1):
        chunk = self.stream.read(min(size, 1024 * 1024) if size >= 0 else 1024 * 1024)
        if chunk:
            self.current += len(chunk)
            now = time.monotonic()
            if (not self.reported or self.current == self.total
                    or self.current - self.reported >= 1024 * 1024
                    or now - self.last_report >= 0.25):
                self.report(self.current)
                self.reported = self.current
                self.last_report = now
        return chunk


def _worker(host: str, ref: str, image_id: str, archive: Path, connection,
            request_timeout: int):
    from . import docker_ops as dk

    remote = None
    try:
        remote = dk.remote_client(host, timeout=request_timeout)
        dk._load_archive(
            remote, ref, image_id, archive,
            report=lambda current: connection.send(("bytes", current)),
            event=lambda text: connection.send(("event", text)),
        )
        connection.send(("done", image_id))
    except Exception as exc:
        connection.send(("error", (str(exc), traceback.format_exc())))
    finally:
        if remote is not None:
            remote.close()
        connection.close()


def run_transfer(host: str, ref: str, image_id: str, archive: Path,
                 progress: Optional[Callable[[TransferProgress], None]] = None,
                 *, stall_timeout: int = 120, load_timeout: int = 600,
                 total_timeout: int = 7200) -> str:
    if min(stall_timeout, load_timeout, total_timeout) <= 0:
        raise ValueError("Transfer timeouts must be positive")
    total = archive.stat().st_size
    if not total:
        raise ValueError(f"Empty image archive for {ref}")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(
        target=_worker,
        args=(host, ref, image_id, archive, sender, total_timeout + 1),
        name="image-upload", daemon=True,
    )
    started = last_activity = last_sample = last_log = time.monotonic()
    current = sampled = 0
    loading_at = None
    LOG.info("Starting upload %s: %d bytes", ref, total)
    try:
        worker.start()
        sender.close()
        while True:
            if receiver.poll(0.1):
                try:
                    kind, value = receiver.recv()
                except EOFError as exc:
                    raise RuntimeError(
                        f"Upload worker exited without a result for {ref}"
                    ) from exc
                if kind == "done":
                    LOG.info("Verified upload %s (%s)", ref, value)
                    return value
                if kind == "error":
                    message, trace = value
                    LOG.error("Upload worker failed for %s:\n%s", ref, trace)
                    raise RuntimeError(f"Upload worker failed for {ref}: {message}")
                if kind == "bytes" and value > current:
                    current = value
                    last_activity = time.monotonic()
                    if current >= total and loading_at is None:
                        loading_at = last_activity
                elif kind == "event":
                    last_activity = time.monotonic()
                    LOG.info("%s: %s", ref, value)
            now = time.monotonic()
            elapsed = now - started
            idle = now - last_activity
            if elapsed >= total_timeout:
                raise TimeoutError(f"{ref}: transfer exceeded {total_timeout}s overall deadline")
            if loading_at is not None:
                if now - loading_at >= load_timeout:
                    raise TimeoutError(f"{ref}: daemon load/verification exceeded {load_timeout}s")
            elif idle >= stall_timeout:
                raise TimeoutError(f"{ref}: upload stalled for {stall_timeout}s at {current}/{total} bytes")
            if now - last_sample >= 1:
                rate = (current - sampled) / (now - last_sample)
                update = TransferProgress(
                    "Loading/verifying" if loading_at is not None else "Uploading",
                    current, total, rate, elapsed, idle,
                )
                if progress:
                    progress(update)
                if now - last_log >= 10:
                    LOG.info("%s: %s", ref, update.text)
                    last_log = now
                last_sample, sampled = now, current
    except Exception:
        LOG.exception("Transfer failed: %s (%d/%d bytes)", ref, current, total)
        raise
    finally:
        if worker.pid is not None:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=2)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=2)
            worker.close()
        receiver.close()
        sender.close()
