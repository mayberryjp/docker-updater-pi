"""Supervise uploads in a disposable process, including blocked SDK/socket calls."""

from __future__ import annotations

import logging
import multiprocessing
import threading
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
    total: Optional[int]
    rate: float
    elapsed: float
    idle: float
    detail: str = ""

    @property
    def fraction(self) -> Optional[float]:
        return min(1.0, self.current / self.total) if self.total else None

    @property
    def text(self) -> str:
        total = str(self.total) if self.total is not None else "unknown"
        percent = f"{self.fraction * 100:.1f}" if self.fraction is not None else "unknown"
        return (
            f"phase={self.phase} current_bytes={self.current} total_bytes={total}"
            f" rate_Bps={self.rate:.1f} percent={percent}"
            f" elapsed_s={self.elapsed:.1f} no_byte_progress_s={self.idle:.1f}"
            f" detail={self.detail}"
        )


class ProgressReporter:
    """Publish byte telemetry even while an SDK call is blocked waiting for data."""

    def __init__(self, logger: logging.Logger, ref: str, phase: str,
                 progress: Optional[Callable[[TransferProgress], None]] = None,
                 *, total: Optional[int] = None, heartbeat: bool = True):
        self.logger = logger
        self.ref = ref
        self.progress = progress
        self.phase = phase
        self.current = 0
        self.total = total
        self.detail = ""
        self.started = self.last_byte = self.sampled_at = time.monotonic()
        self.sampled = 0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._error: Optional[Exception] = None
        self._thread = (
            threading.Thread(target=self._run, name="byte-progress", daemon=True)
            if heartbeat else None
        )

    def __enter__(self):
        self.tick(force=True)
        if self._thread is not None:
            self._thread.start()
        return self

    def update(self, *, current=None, total=..., phase=None, detail=None):
        with self._lock:
            if self._error is not None:
                raise self._error
            changed_phase = phase is not None and phase != self.phase
            first_bytes = current is not None and current > 0 and self.current == 0
            changed_bytes = current is not None and current != self.current
            now = time.monotonic()
            if changed_phase:
                self.phase = phase
                self.sampled = current if current is not None else self.current
                self.sampled_at = now
            if current is not None:
                if current != self.current:
                    self.last_byte = now
                self.current = current
            if total is not ...:
                self.total = total
            if detail is not None:
                self.detail = detail
            self.tick(force=changed_phase or first_bytes or (
                changed_bytes and self.total is not None and current >= self.total
            ))

    def tick(self, *, force=False):
        with self._lock:
            now = time.monotonic()
            interval = now - self.sampled_at
            if not force and interval < 1:
                return
            update = TransferProgress(
                self.phase, self.current, self.total,
                max(0, self.current - self.sampled) / interval if interval > 0 else 0,
                now - self.started, now - self.last_byte, self.detail,
            )
            self.sampled_at, self.sampled = now, self.current
            self.logger.info("%s: %s", self.ref, update.text)
            if self.progress is not None:
                self.progress(update)

    def _run(self):
        try:
            while not self._stop.wait(1):
                self.tick()
        except Exception as exc:
            with self._lock:
                self._error = exc
            self.logger.exception("Progress reporting failed for %s", self.ref)
            self._stop.set()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if exc_type is not None:
            self.logger.error(
                "Operation failed for %s: phase=%s current_bytes=%d total_bytes=%s",
                self.ref, self.phase, self.current, self.total,
                exc_info=(exc_type, exc_value, exc_traceback),
            )
        elif self._error is not None:
            raise self._error
        else:
            self.tick(force=True)


class ArchiveReader:
    """Bound reads and report bytes handed to the HTTP transport, not server ACKs."""

    def __init__(self, stream, total: int, report: Callable[[int], None]):
        self.stream = stream
        self.total = total
        self.report = report
        self.current = 0
        self.reported = 0
        self.last_report = time.monotonic()

    @property
    def len(self) -> int:
        # Requests accepts .len without the Py_ssize_t limit of __len__ on 32-bit Python.
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
        connection.send(("diagnostic", f"Daemon connected; API version={remote.api._version}"))
        dk._load_archive(
            remote, ref, image_id, archive,
            report=lambda current: connection.send(("bytes", current)),
            event=lambda text: connection.send(("event", text)),
            stage=lambda phase: connection.send(("phase", phase)),
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
    started = last_activity = time.monotonic()
    current = 0
    loading_at = None
    completed = False
    LOG.info(
        "Starting upload %s: expected_id=%s archive=%s total_bytes=%d"
        " stall_timeout_s=%d load_timeout_s=%d overall_timeout_s=%d",
        ref, image_id, archive, total, stall_timeout, load_timeout, total_timeout,
    )
    try:
        with ProgressReporter(LOG, ref, "Connecting daemon", progress,
                              total=total, heartbeat=False) as meter:
            worker.start()
            LOG.info("%s: upload worker started pid=%d", ref, worker.pid)
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
                        completed = True
                        meter.update(current=total, phase="Upload verified")
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
                            meter.update(
                                current=current, phase="Loading/verifying",
                                detail="Payload handed off; waiting for daemon response",
                            )
                        else:
                            meter.update(current=current)
                    elif kind == "phase":
                        LOG.info("%s: daemon stage=%s", ref, value)
                        meter.update(phase=value)
                    elif kind == "event":
                        LOG.info("%s: daemon output=%s", ref, value)
                        meter.update(detail=value)
                    elif kind == "diagnostic":
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
                meter.tick()
    except Exception:
        LOG.exception("Transfer failed: %s (%d/%d bytes)", ref, current, total)
        raise
    finally:
        if worker.pid is not None:
            if completed:
                worker.join(timeout=2)
            if worker.is_alive():
                LOG.warning("%s: terminating upload worker pid=%d", ref, worker.pid)
                worker.terminate()
            worker.join(timeout=2)
            if worker.is_alive():
                LOG.error("%s: killing unresponsive upload worker pid=%d", ref, worker.pid)
                worker.kill()
                worker.join(timeout=2)
            LOG.info("%s: upload worker exitcode=%s", ref, worker.exitcode)
            worker.close()
        receiver.close()
        sender.close()
