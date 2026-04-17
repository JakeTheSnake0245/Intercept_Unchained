"""Analog 5.8 GHz FPV video transmitter detector.

Sweeps the 40 standard FPV channels using hackrf_sweep and flags any
channel with power > baseline + 12 dB.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Any

from .base import BaseSubtool
from .signatures import FPV_CHANNELS
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)

ANOMALY_THRESHOLD_DB = 12.0
FPV_BW_MHZ = 9.0  # ±9 MHz around channel center


class FPVDetector(BaseSubtool):
    name = "fpv_detector"

    def __init__(self) -> None:
        super().__init__()
        self._baseline: dict[float, float] = {}

    def _run(self, gain: int = 40, **kwargs: Any) -> None:
        if not shutil.which("hackrf_sweep"):
            self.emit("status", {"msg": "hackrf_sweep not found — FPV detection unavailable"})
            while self._running:
                time.sleep(10)
            return

        store = get_cuas_store()
        self.emit("status", {"msg": "FPV analog video detector active (5.8 GHz)"})

        while self._running:
            readings = self._sweep_fpv_band(gain)
            if readings:
                self._detect_fpv(readings, store)
            time.sleep(15)

    def _sweep_fpv_band(self, gain: int) -> dict[float, float]:
        start_mhz = 5650
        stop_mhz = 5950
        step_hz = 1_000_000  # 1 MHz step

        readings: dict[float, list[float]] = {}
        try:
            proc = subprocess.Popen(
                [
                    "hackrf_sweep",
                    "-f", f"{start_mhz}:{stop_mhz}",
                    "-w", str(step_hz),
                    "-l", str(gain), "-g", str(gain),
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            deadline = time.time() + 12
            for line in proc.stdout:
                if time.time() > deadline:
                    proc.terminate()
                    break
                parts = line.decode("utf-8", errors="replace").strip().split(", ")
                if len(parts) < 6:
                    continue
                try:
                    freq_low = float(parts[2])
                    freq_step = float(parts[4])
                    powers = [float(x) for x in parts[6:] if x.strip()]
                    for i, p in enumerate(powers):
                        freq_mhz = (freq_low + i * freq_step) / 1_000_000
                        readings.setdefault(freq_mhz, []).append(p)
                except (ValueError, IndexError):
                    pass
        except Exception as exc:
            logger.debug("hackrf_sweep FPV error: %s", exc)

        return {f: sum(v) / len(v) for f, v in readings.items() if v}

    def _detect_fpv(self, readings: dict[float, float], store: Any) -> None:
        if not self._baseline:
            self._baseline = dict(readings)
            return

        for channel in FPV_CHANNELS:
            center = channel["freq_mhz"]
            nearby = [
                p for f, p in readings.items()
                if abs(f - center) <= FPV_BW_MHZ
            ]
            if not nearby:
                continue

            mean_power = sum(nearby) / len(nearby)
            baseline_nearby = [
                p for f, p in self._baseline.items()
                if abs(f - center) <= FPV_BW_MHZ
            ]
            base_power = (sum(baseline_nearby) / len(baseline_nearby)) if baseline_nearby else mean_power

            if mean_power > base_power + ANOMALY_THRESHOLD_DB:
                detection = CUASDetection(
                    subtool=self.name,
                    detection_type="FPV_ANALOG_VIDEO",
                    freq_mhz=center,
                    rssi_dbm=mean_power,
                    confidence="MEDIUM",
                    source_tool="hackrf_sweep",
                    raw_payload={
                        "fpv_band": channel["band"],
                        "fpv_channel": channel["ch"],
                        "center_mhz": center,
                        "mean_power_dbm": mean_power,
                        "baseline_dbm": base_power,
                    },
                )
                store.add(detection)
                self.emit("detection", detection.to_dict())
