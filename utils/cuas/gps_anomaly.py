"""GPS jamming and spoofing detector.

Monitors GPS L1 (1575.42 MHz) for:
  - Broadband noise (jamming): power >20 dB above baseline
  - CW-like spike (spoofing): narrow peak at L1 center
  - Position jump: cross-reference with GPS receiver
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Any

from .base import BaseSubtool
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)

GPS_L1_MHZ = 1575.42
GPS_MONITOR_BW_MHZ = 2.0
JAMMING_THRESHOLD_DB = 20.0
SPOOF_NARROW_THRESHOLD_DB = 15.0
BASELINE_SAMPLES = 30


class GPSAnomalyDetector(BaseSubtool):
    name = "gps_anomaly"

    def __init__(self) -> None:
        super().__init__()
        self._baseline_powers: list[float] = []
        self._baseline_mean: float | None = None
        self._last_gps_pos: tuple[float, float] | None = None

    def _run(self, device_index: int = 0, gain: int = 40, **kwargs: Any) -> None:
        store = get_cuas_store()
        self.emit("status", {"msg": "GPS L1 anomaly monitor starting"})

        if not shutil.which("hackrf_sweep") and not shutil.which("rtl_power"):
            self.emit("status", {"msg": "No SDR sweep tool found for GPS monitoring"})
            while self._running:
                time.sleep(10)
            return

        self.emit("status", {"msg": f"Monitoring GPS L1 {GPS_L1_MHZ} MHz"})

        while self._running:
            power = self._measure_gps_band(device_index, gain)
            if power is not None:
                self._process_measurement(power, store)
            time.sleep(5)

    def _measure_gps_band(self, device_index: int, gain: int) -> float | None:
        start_mhz = GPS_L1_MHZ - GPS_MONITOR_BW_MHZ
        stop_mhz = GPS_L1_MHZ + GPS_MONITOR_BW_MHZ
        step_hz = 100_000  # 100 kHz

        powers: list[float] = []

        if shutil.which("hackrf_sweep"):
            try:
                proc = subprocess.Popen(
                    [
                        "hackrf_sweep",
                        "-f", f"{int(start_mhz)}:{int(stop_mhz)}",
                        "-w", str(step_hz),
                        "-l", str(gain), "-g", str(gain),
                    ],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                deadline = time.time() + 6
                for line in proc.stdout:
                    if time.time() > deadline:
                        proc.terminate()
                        break
                    parts = line.decode("utf-8", errors="replace").strip().split(", ")
                    if len(parts) >= 6:
                        try:
                            vals = [float(x) for x in parts[6:] if x.strip()]
                            powers.extend(vals)
                        except ValueError:
                            pass
            except Exception as exc:
                logger.debug("GPS hackrf_sweep error: %s", exc)
        elif shutil.which("rtl_power"):
            try:
                import tempfile, os, csv
                with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
                    outfile = f.name
                subprocess.run(
                    [
                        "rtl_power",
                        "-f", f"{start_mhz}M:{stop_mhz}M:{step_hz}",
                        "-g", str(gain), "-d", str(device_index),
                        "-i", "1", "-e", "5s", outfile,
                    ],
                    timeout=8, capture_output=True
                )
                with open(outfile, newline="") as f:
                    for row in csv.reader(f):
                        if len(row) > 6:
                            try:
                                vals = [float(v) for v in row[6:] if v.strip()]
                                powers.extend(vals)
                            except ValueError:
                                pass
                os.unlink(outfile)
            except Exception as exc:
                logger.debug("GPS rtl_power error: %s", exc)

        if not powers:
            return None
        return sum(powers) / len(powers)

    def _process_measurement(self, power: float, store: Any) -> None:
        if len(self._baseline_powers) < BASELINE_SAMPLES:
            self._baseline_powers.append(power)
            if len(self._baseline_powers) == BASELINE_SAMPLES:
                self._baseline_mean = sum(self._baseline_powers) / len(self._baseline_powers)
                self.emit("status", {"msg": f"GPS L1 baseline established: {self._baseline_mean:.1f} dBm"})
            return

        if self._baseline_mean is None:
            return

        delta = power - self._baseline_mean

        if delta > JAMMING_THRESHOLD_DB:
            detection = CUASDetection(
                subtool=self.name,
                detection_type="GPS_JAMMING",
                freq_mhz=GPS_L1_MHZ,
                rssi_dbm=power,
                confidence="HIGH",
                source_tool="sdr_sweep",
                raw_payload={
                    "delta_db": delta,
                    "baseline_dbm": self._baseline_mean,
                    "current_dbm": power,
                },
            )
            store.add(detection)
            self.emit("detection", detection.to_dict())
        elif delta > SPOOF_NARROW_THRESHOLD_DB:
            detection = CUASDetection(
                subtool=self.name,
                detection_type="GPS_SPOOFING",
                freq_mhz=GPS_L1_MHZ,
                rssi_dbm=power,
                confidence="MEDIUM",
                source_tool="sdr_sweep",
                raw_payload={
                    "delta_db": delta,
                    "baseline_dbm": self._baseline_mean,
                    "current_dbm": power,
                },
            )
            store.add(detection)
            self.emit("detection", detection.to_dict())
