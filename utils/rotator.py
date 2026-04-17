"""Hamlib rotctld TCP client for antenna rotator control.

Communicates with a running ``rotctld`` daemon over TCP using the simple
line-based Hamlib protocol::

    Client → ``P <azimuth> <elevation>\\n``
    Server → ``RPRT 0\\n``          (success)

If ``rotctld`` is not reachable the controller silently operates in a
disabled state — the rest of the system functions normally.

Usage::

    rotator = get_rotator()
    if rotator.connect('127.0.0.1', 4533):
        rotator.point_to(az=180.0, el=30.0)
        rotator.park()
        rotator.disconnect()
"""

from __future__ import annotations

import socket
import threading

from utils.logging import get_logger

logger = get_logger('intercept.rotator')

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 4533
DEFAULT_TIMEOUT = 2.0  # seconds


class RotatorController:
    """Thin wrapper around the rotctld TCP protocol."""

    def __init__(self):
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._host = DEFAULT_HOST
        self._port = DEFAULT_PORT
        self._enabled = False
        self._current_az: float = 0.0
        self._current_el: float = 0.0

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
        """Connect to rotctld.  Returns True on success."""
        with self._lock:
            self._host = host
            self._port = port
            try:
                s = socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT)
                s.settimeout(DEFAULT_TIMEOUT)
                self._sock = s
                self._enabled = True
                logger.info(f"Rotator connected to rotctld at {host}:{port}")
                return True
            except OSError as e:
                logger.warning(f"Could not connect to rotctld at {host}:{port}: {e}")
                self._sock = None
                self._enabled = False
                return False

    def disconnect(self) -> None:
        """Close the TCP connection."""
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._enabled = False
        logger.info("Rotator disconnected")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def point_to(self, az: float, el: float) -> bool:
        """Send a ``P`` (set position) command.

        Azimuth and elevation are clamped to valid ranges before sending.

        Returns True if the command was acknowledged.
        """
        az = max(0.0, min(360.0, float(az)))
        el = max(0.0, min(90.0, float(el)))

        ok = self._send_command(f'P {az:.1f} {el:.1f}')
        if ok:
            self._current_az = az
            self._current_el = el
        return ok

    def park(self) -> bool:
        """Send rotator to park position (0° az, 0° el)."""
        return self.point_to(0.0, 0.0)

    def get_position(self) -> tuple[float, float] | None:
        """Query current position.  Returns (az, el) or None on failure."""
        with self._lock:
            if not self._enabled or self._sock is None:
                return None
            try:
                self._sock.sendall(b'p\n')
                resp = self._recv_line()
                if resp and 'RPRT' not in resp:
                    parts = resp.split()
                    if len(parts) >= 2:
                        return float(parts[0]), float(parts[1])
            except Exception as e:
                logger.warning(f"Rotator get_position failed: {e}")
                self._enabled = False
                self._sock = None
        return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_status(self) -> dict:
        return {
            'enabled': self._enabled,
            'host': self._host,
            'port': self._port,
            'current_az': self._current_az,
            'current_el': self._current_el,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send_command(self, cmd: str) -> bool:
        with self._lock:
            if not self._enabled or self._sock is None:
                return False
            try:
                self._sock.sendall((cmd + '\n').encode())
                resp = self._recv_line()
                if resp and 'RPRT 0' in resp:
                    return True
                logger.warning(f"Rotator unexpected response to '{cmd}': {resp!r}")
                return False
            except Exception as e:
                logger.warning(f"Rotator command '{cmd}' failed: {e}")
                self._enabled = False
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                return False

    def _recv_line(self, max_bytes: int = 256) -> str:
        """Read until newline (already holding _lock)."""
        buf = b''
        assert self._sock is not None
        while len(buf) < max_bytes:
            c = self._sock.recv(1)
            if not c:
                break
            buf += c
            if c == b'\n':
                break
        return buf.decode('ascii', errors='replace').strip()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_rotator: RotatorController | None = None
_rotator_lock = threading.Lock()


def get_rotator() -> RotatorController:
    """Get or create the global rotator controller instance."""
    global _rotator
    if _rotator is None:
        with _rotator_lock:
            if _rotator is None:
                _rotator = RotatorController()
    return _rotator
