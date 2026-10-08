"""Thread-safe status shared between the orchestrator (writer) and the display (reader)."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class State(Enum):
    BOOT = "BOOT"
    SCANNING = "SCANNING"
    CONNECTING = "CONNECTING"
    HOME_SYNC = "HOME SYNC"
    APPLYING = "APPLYING"
    IDLE = "IDLE"
    ERROR = "ERROR"
    OFFLINE = "OFFLINE"


# RGB accent per state — mirrors the dongle's APA102 status LED.
STATE_COLOR = {
    State.BOOT: (80, 120, 255),
    State.SCANNING: (0, 200, 220),
    State.CONNECTING: (240, 200, 0),
    State.HOME_SYNC: (0, 210, 90),
    State.APPLYING: (230, 110, 230),
    State.IDLE: (120, 140, 160),
    State.ERROR: (235, 60, 60),
    State.OFFLINE: (90, 90, 90),
}


@dataclass
class Snapshot:
    device_name: str
    state: State
    site: str
    ssid: str
    rssi: int
    headline: str
    detail: str
    progress: Optional[float]
    online: bool
    log: tuple
    updated_at: float

    @property
    def color(self):
        return STATE_COLOR.get(self.state, (255, 255, 255))


class Status:
    """A small mutable status record guarded by a lock."""

    def __init__(self, device_name: str, log_lines: int = 60):
        self._lock = threading.Lock()
        self._device_name = device_name
        self._state = State.BOOT
        self._site = ""
        self._ssid = ""
        self._rssi = 0
        self._headline = "Starting"
        self._detail = ""
        self._progress: Optional[float] = None
        self._online = False
        self._log: deque = deque(maxlen=log_lines)
        self._updated = time.time()

    def set(self, *, state=None, site=None, ssid=None, rssi=None,
            headline=None, detail=None, progress=..., online=None):
        with self._lock:
            if state is not None:
                self._state = state
            if site is not None:
                self._site = site
            if ssid is not None:
                self._ssid = ssid
            if rssi is not None:
                self._rssi = rssi
            if headline is not None:
                self._headline = headline
            if detail is not None:
                self._detail = detail
            if progress is not ...:
                self._progress = progress
            if online is not None:
                self._online = online
            self._updated = time.time()

    def log(self, message: str):
        line = time.strftime("%H:%M:%S ") + message
        with self._lock:
            self._log.append(line)
            self._updated = time.time()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                device_name=self._device_name,
                state=self._state,
                site=self._site,
                ssid=self._ssid,
                rssi=self._rssi,
                headline=self._headline,
                detail=self._detail,
                progress=self._progress,
                online=self._online,
                log=tuple(self._log),
                updated_at=self._updated,
            )
