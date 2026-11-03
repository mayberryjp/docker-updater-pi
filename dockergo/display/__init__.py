"""Display subsystem: a background thread that renders the status to the LCD."""

from __future__ import annotations

import threading
import logging
import time
from typing import Optional

from ..status import Status
from .framebuffer import open_framebuffer
from .ui import StatusScreen

LOG = logging.getLogger(__name__)

class DisplayLoop:
    """Renders the shared :class:`Status` to the framebuffer on a background thread."""

    def __init__(self, status: Status, device: Optional[str] = None,
                 mock: bool = False, fps: float = 4.0):
        self.status = status
        self.fb = open_framebuffer(device, mock=mock)
        self.screen = StatusScreen(self.fb.size)
        self.fps = fps
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="display", daemon=True)
        self._last_update = None
        self._last_error = None

    def start(self):
        LOG.info("Starting status display: size=%s fps=%s", self.fb.size, self.fps)
        self._thread.start()

    def _run(self):
        period = 1.0 / self.fps
        while not self._stop.is_set():
            snap = self.status.snapshot()
            if snap.updated_at != self._last_update:
                try:
                    self.fb.blit(self.screen.render(snap))
                except Exception:
                    now = time.monotonic()
                    if self._last_error is None or now - self._last_error >= 10:
                        LOG.exception("Status display render/write failed; retrying")
                        self._last_error = now
                else:
                    self._last_update = snap.updated_at
                    self._last_error = None
            self._stop.wait(period)

    def stop(self):
        self._stop.set()
        try:
            self._thread.join(timeout=2)
        finally:
            self.fb.close()
