"""Display subsystem: a background thread that renders the status to the LCD."""

from __future__ import annotations

import threading
from typing import Optional

from ..status import Status
from .framebuffer import open_framebuffer
from .ui import StatusScreen


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

    def start(self):
        self._thread.start()

    def _run(self):
        period = 1.0 / self.fps
        while not self._stop.is_set():
            snap = self.status.snapshot()
            if snap.updated_at != self._last_update:
                self._last_update = snap.updated_at
                try:
                    self.fb.blit(self.screen.render(snap))
                except Exception:
                    pass  # never let a draw error take down the device
            self._stop.wait(period)

    def stop(self):
        self._stop.set()
        try:
            self._thread.join(timeout=2)
        finally:
            self.fb.close()
