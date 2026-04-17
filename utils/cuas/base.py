"""Abstract base class for C-UAS detection subtools."""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from typing import Any


class BaseSubtool(ABC):
    """Common interface for all C-UAS detection subtools."""

    name: str = "base"

    def __init__(self) -> None:
        self._running = False
        self._thread: threading.Thread | None = None
        self._event_queue: queue.Queue = queue.Queue(maxsize=500)
        self._lock = threading.Lock()
        self._error: str | None = None

    def start(self, **kwargs: Any) -> dict:
        with self._lock:
            if self._running:
                return {"status": "already_running"}
            self._error = None
            self._running = True
            self._thread = threading.Thread(target=self._run, kwargs=kwargs, daemon=True)
            self._thread.start()
        return {"status": "started", "subtool": self.name}

    def stop(self) -> dict:
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        return {"status": "stopped", "subtool": self.name}

    def status(self) -> dict:
        return {
            "subtool": self.name,
            "running": self._running,
            "error": self._error,
        }

    def emit(self, event_type: str, data: dict) -> None:
        try:
            self._event_queue.put_nowait({"type": event_type, "subtool": self.name, **data})
        except queue.Full:
            pass

    def get_event(self, timeout: float = 1.0) -> dict | None:
        try:
            return self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @abstractmethod
    def _run(self, **kwargs: Any) -> None:
        """Main detection loop — runs in a daemon thread."""
        ...
