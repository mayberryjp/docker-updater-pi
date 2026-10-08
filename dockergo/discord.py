"""Optional Discord webhook mirror for status lines.

Messages are queued and flushed in batches on a background thread so a burst of
updates becomes a single webhook POST, with basic 429 (rate-limit) handling.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

import requests


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], username: str = "DockerGo",
                 flush_interval: float = 4.0):
        self.webhook_url = webhook_url or None
        self.username = username
        self.flush_interval = flush_interval
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.webhook_url:
            self._thread = threading.Thread(target=self._run, name="discord", daemon=True)
            self._thread.start()

    def send(self, message: str):
        if self.webhook_url:
            self._queue.put(message)

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(self.flush_interval)
            lines = []
            try:
                while True:
                    lines.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            if lines:
                self._post("\n".join(lines)[:1900])

    def _post(self, content: str):
        for _ in range(4):
            try:
                response = requests.post(
                    self.webhook_url,
                    json={"username": self.username, "content": content},
                    timeout=10,
                )
            except requests.RequestException:
                return
            if response.status_code == 429:
                try:
                    time.sleep(float(response.json().get("retry_after", 1.0)))
                except (ValueError, KeyError, requests.RequestException):
                    time.sleep(1.0)
                continue
            return

    def close(self):
        self._stop.set()
