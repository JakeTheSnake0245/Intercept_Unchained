"""Shared C-UAS data store with TTL-based cleanup."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

TTL_SECONDS = 300  # 5 minutes


@dataclass
class CUASDetection:
    detection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts_utc: float = field(default_factory=time.time)
    subtool: str = ""
    detection_type: str = ""
    freq_mhz: float | None = None
    rssi_dbm: float | None = None
    bssid: str | None = None
    bt_addr: str | None = None
    ssid: str | None = None
    drone_make: str = "Unknown"
    drone_model: str = "Unknown"
    drone_serial: str | None = None
    pilot_lat: float | None = None
    pilot_lon: float | None = None
    drone_lat: float | None = None
    drone_lon: float | None = None
    drone_alt_m: float | None = None
    drone_speed_ms: float | None = None
    confidence: str = "LOW"  # LOW | MEDIUM | HIGH | CONFIRMED
    raw_payload: dict = field(default_factory=dict)
    source_tool: str = ""

    def to_dict(self) -> dict:
        return {
            "detection_id": self.detection_id,
            "ts_utc": self.ts_utc,
            "subtool": self.subtool,
            "detection_type": self.detection_type,
            "freq_mhz": self.freq_mhz,
            "rssi_dbm": self.rssi_dbm,
            "bssid": self.bssid,
            "bt_addr": self.bt_addr,
            "ssid": self.ssid,
            "drone_make": self.drone_make,
            "drone_model": self.drone_model,
            "drone_serial": self.drone_serial,
            "pilot_lat": self.pilot_lat,
            "pilot_lon": self.pilot_lon,
            "drone_lat": self.drone_lat,
            "drone_lon": self.drone_lon,
            "drone_alt_m": self.drone_alt_m,
            "drone_speed_ms": self.drone_speed_ms,
            "confidence": self.confidence,
            "source_tool": self.source_tool,
        }


_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CONFIRMED": 3}


class CUASDataStore:
    def __init__(self, ttl: int = TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._detections: dict[str, CUASDetection] = {}
        self._ttl = ttl

    def add(self, detection: CUASDetection) -> None:
        self._cleanup()
        with self._lock:
            existing = self._find_duplicate(detection)
            if existing:
                # Upgrade confidence if new detection is stronger
                if _CONFIDENCE_RANK.get(detection.confidence, 0) > _CONFIDENCE_RANK.get(existing.confidence, 0):
                    existing.confidence = detection.confidence
                existing.ts_utc = detection.ts_utc
                if detection.rssi_dbm is not None:
                    existing.rssi_dbm = detection.rssi_dbm
                if detection.drone_lat is not None:
                    existing.drone_lat = detection.drone_lat
                    existing.drone_lon = detection.drone_lon
                if detection.pilot_lat is not None:
                    existing.pilot_lat = detection.pilot_lat
                    existing.pilot_lon = detection.pilot_lon
            else:
                self._detections[detection.detection_id] = detection

    def _find_duplicate(self, detection: CUASDetection) -> CUASDetection | None:
        if detection.drone_serial:
            for d in self._detections.values():
                if d.drone_serial == detection.drone_serial:
                    return d
        if detection.bssid:
            for d in self._detections.values():
                if d.bssid == detection.bssid:
                    return d
        if detection.bt_addr:
            for d in self._detections.values():
                if d.bt_addr == detection.bt_addr:
                    return d
        return None

    def _cleanup(self) -> None:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [k for k, v in self._detections.items() if v.ts_utc < cutoff]
            for k in expired:
                del self._detections[k]

    def get_all(self) -> list[CUASDetection]:
        self._cleanup()
        with self._lock:
            return sorted(self._detections.values(), key=lambda d: d.ts_utc, reverse=True)

    def get_highest_confidence(self) -> str:
        detections = self.get_all()
        if not detections:
            return "CLEAR"
        best = max(_CONFIDENCE_RANK.get(d.confidence, 0) for d in detections)
        if best == 0:
            return "POSSIBLE"
        if best == 1:
            return "PROBABLE"
        return "CONFIRMED"

    def clear(self) -> None:
        with self._lock:
            self._detections.clear()

    def count(self) -> int:
        self._cleanup()
        with self._lock:
            return len(self._detections)


_store: CUASDataStore | None = None
_store_lock = threading.Lock()


def get_cuas_store() -> CUASDataStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CUASDataStore()
    return _store
