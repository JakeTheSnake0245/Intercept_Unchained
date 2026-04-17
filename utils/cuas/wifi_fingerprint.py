"""WiFi-based drone fingerprinting using SSID prefix and OUI matching."""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import BaseSubtool
from .signatures import DRONE_OUI_MAP, WIFI_DRONE_PATTERNS
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)


def _check_ssid(ssid: str) -> str | None:
    """Return matched drone pattern or None."""
    if not ssid:
        return None
    for pattern in WIFI_DRONE_PATTERNS:
        if ssid.upper().startswith(pattern.upper()) or pattern.upper() in ssid.upper():
            return pattern
    return None


def _check_oui(bssid: str) -> str | None:
    """Return drone make from OUI or None."""
    if not bssid or len(bssid) < 8:
        return None
    oui = bssid[:8].upper()
    return DRONE_OUI_MAP.get(oui)


def classify_wifi_ap(bssid: str, ssid: str, rssi: float = 0.0) -> CUASDetection | None:
    """Return a CUASDetection if the AP matches drone signatures, else None."""
    make = _check_oui(bssid)
    pattern = _check_ssid(ssid)

    if not make and not pattern:
        return None

    confidence = "HIGH" if make else "MEDIUM"

    return CUASDetection(
        subtool="wifi_fingerprint",
        detection_type="WIFI_DRONE_AP",
        bssid=bssid,
        ssid=ssid,
        rssi_dbm=rssi or None,
        drone_make=make or "Unknown",
        confidence=confidence,
        source_tool="wifi_scanner",
        raw_payload={"matched_oui": make, "matched_pattern": pattern},
    )


class WiFiFingerprint(BaseSubtool):
    name = "wifi_fingerprint"

    def _run(self, **kwargs: Any) -> None:
        """Poll the WiFi scanner for new APs and classify them."""
        try:
            from utils.wifi import get_wifi_scanner
            scanner = get_wifi_scanner()
        except Exception as exc:
            self.emit("status", {"msg": f"Cannot access WiFi scanner: {exc}"})
            while self._running:
                time.sleep(10)
            return

        store = get_cuas_store()
        self.emit("status", {"msg": "WiFi drone fingerprinting active"})
        seen: set[str] = set()

        while self._running:
            try:
                result = scanner.quick_scan(timeout=10)
                for network in result.networks:
                    bssid = getattr(network, "bssid", "") or ""
                    ssid = getattr(network, "ssid", "") or ""
                    rssi = float(getattr(network, "signal_strength", 0) or 0)

                    cache_key = f"{bssid}:{ssid}"
                    if cache_key in seen:
                        continue

                    detection = classify_wifi_ap(bssid, ssid, rssi)
                    if detection:
                        seen.add(cache_key)
                        store.add(detection)
                        self.emit("detection", detection.to_dict())
            except Exception as exc:
                logger.debug("WiFi fingerprint scan error: %s", exc)

            time.sleep(30)
