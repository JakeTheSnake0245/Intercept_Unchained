"""Minimal Flask-SocketIO compatibility shim.

This is only intended to satisfy radiosonde_auto_rx's optional web UI
dependency in environments where ``flask_socketio`` is not installed.
It provides the small subset of the API that auto_rx imports.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SocketIO:
    """Very small subset of Flask-SocketIO's SocketIO interface."""

    def __init__(self, app, async_mode: str | None = None, *args, **kwargs):
        self.app = app
        self.async_mode = async_mode or "threading"
        self._handlers: dict[tuple[str, str | None], Callable[..., Any]] = {}

    def on(self, event: str, namespace: str | None = None):
        """Register an event handler decorator."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers[(event, namespace)] = func
            return func

        return decorator

    def emit(self, event: str, data: Any = None, namespace: str | None = None, *args, **kwargs) -> None:
        """No-op emit used when the real Socket.IO server is unavailable."""
        return None

    def run(self, app=None, host: str = "127.0.0.1", port: int = 5000, *args, **kwargs) -> None:
        """Fallback to Flask's built-in development server."""
        flask_app = app or self.app
        flask_app.run(
            host=host,
            port=port,
            threaded=True,
            use_reloader=False,
        )
