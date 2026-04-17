"""Bluetooth-based drone fingerprinting — name, manufacturer data, Remote ID BLE UUID."""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import BaseSubtool
from .signatures import BT_DRONE_PATTERNS, BT_MANUFACTURER_IDS, REMOTE_ID_SERVICE_UUID
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)


def _check_bt_name(name: str) -> str | None:
    if not name:
        return None
    name_upper = name.upper()
    for pattern in BT_DRONE_PATTERNS:
        if pattern.upper() in name_upper:
            return pattern
    return None


def _check_manufacturer(manufacturer_id: int | None) -> str | None:
    if manufacturer_id is None:
        return None
    return BT_MANUFACTURER_IDS.get(manufacturer_id)


def classify_bt_device(
    addr: str,
    name: str,
    manufacturer_id: int | None = None,
    service_uuids: list[str] | None = None,
    rssi: float = 0.0,
) -> CUASDetection | None:
    make = _check_manufacturer(manufacturer_id)
    pattern = _check_bt_name(name)
    is_remote_id = any(
        REMOTE_ID_SERVICE_UUID.lower() in (u or "").lower()
        for u in (service_uuids or [])
    )

    if not make and not pattern and not is_remote_id:
        return None

    if is_remote_id:
        det_type = "BLE_REMOTE_ID"
        confidence = "CONFIRMED"
    elif make:
        det_type = "BT_DRONE_DEVICE"
        confidence = "HIGH"
    else:
        det_type = "BT_DRONE_DEVICE"
        confidence = "MEDIUM"

    return CUASDetection(
        subtool="bt_fingerprint",
        detection_type=det_type,
        bt_addr=addr,
        rssi_dbm=rssi or None,
        drone_make=make or "Unknown",
        confidence=confidence,
        source_tool="bt_scanner",
        raw_payload={
            "name": name,
            "manufacturer_id": manufacturer_id,
            "matched_make": make,
            "matched_pattern": pattern,
            "is_remote_id": is_remote_id,
        },
    )


class BTFingerprint(BaseSubtool):
    name = "bt_fingerprint"

    def _run(self, **kwargs: Any) -> None:
        store = get_cuas_store()
        self.emit("status", {"msg": "Bluetooth drone fingerprinting active"})
        seen: set[str] = set()

        while self._running:
            try:
                self._scan_cycle(store, seen)
            except Exception as exc:
                logger.debug("BT fingerprint scan error: %s", exc)
            time.sleep(20)

    def _scan_cycle(self, store: Any, seen: set[str]) -> None:
        try:
            from utils.bluetooth import get_aggregator
            agg = get_aggregator()
            devices = agg.get_all_devices() if agg else []
        except Exception:
            devices = []

        for device in devices:
            addr = getattr(device, "address", "") or ""
            if addr in seen:
                continue

            name = getattr(device, "name", "") or ""
            rssi = float(getattr(device, "rssi", 0) or 0)
            manufacturer_id = getattr(device, "manufacturer_id", None)
            service_uuids = getattr(device, "service_uuids", []) or []

            detection = classify_bt_device(addr, name, manufacturer_id, service_uuids, rssi)
            if detection:
                seen.add(addr)
                store.add(detection)
                self.emit("detection", detection.to_dict())
