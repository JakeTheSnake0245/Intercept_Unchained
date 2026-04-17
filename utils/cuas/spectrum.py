"""RF spectrum surveillance for UAS frequency bands.

Uses rtl_power (RTL-SDR) and hackrf_sweep (HackRF) to sweep known
UAS control, telemetry, and GPS bands. Detects persistent power anomalies.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

from .base import BaseSubtool
from .signatures import UAS_BANDS
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)

BASELINE_SECONDS = 60
ANOMALY_THRESHOLD_DB = 15.0
PERSIST_COUNT_THRESHOLD = 3


class SpectrumSurveillance(BaseSubtool):
    name = "spectrum"

    def __init__(self) -> None:
        super().__init__()
        self._baselines: dict[str, dict[float, float]] = {}  # band -> {freq: power}
        self._hit_counters: dict[str, dict[float, int]] = {}  # band -> {freq: count}

    def _run(self, device_index: int = 0, gain: int = 40, **kwargs: Any) -> None:
        store = get_cuas_store()
        self.emit("status", {"msg": "RF spectrum surveillance starting"})

        hackrf_available = bool(shutil.which("hackrf_sweep"))
        rtl_available = bool(shutil.which("rtl_power"))

        if not hackrf_available and not rtl_available:
            self.emit("status", {"msg": "No sweep tools found (rtl_power or hackrf_sweep required)"})
            while self._running:
                time.sleep(10)
            return

        while self._running:
            for band in UAS_BANDS:
                if not self._running:
                    break
                self._sweep_band(band, device_index, gain, store, hackrf_available)
                time.sleep(1)

    def _sweep_band(
        self, band: dict, device_index: int, gain: int,
        store: Any, hackrf_available: bool
    ) -> None:
        name = band["name"]
        start = band["start_mhz"]
        stop = band["stop_mhz"]
        step = band["step_khz"] * 1000  # Hz

        readings = {}

        if start >= 1000 and hackrf_available:
            readings = self._sweep_hackrf(start, stop, step, gain)
        elif shutil.which("rtl_power"):
            readings = self._sweep_rtl(start, stop, step, gain, device_index)
        else:
            return

        self._process_readings(name, readings, store)

    def _sweep_rtl(
        self, start_mhz: float, stop_mhz: float, step_hz: int,
        gain: int, device_index: int
    ) -> dict[float, float]:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            outfile = f.name

        try:
            subprocess.run(
                [
                    "rtl_power",
                    "-f", f"{start_mhz}M:{stop_mhz}M:{step_hz}",
                    "-g", str(gain),
                    "-d", str(device_index),
                    "-i", "1", "-e", "5s",
                    "-F", "9",
                    outfile,
                ],
                timeout=8, capture_output=True
            )
            return self._parse_rtl_power_csv(outfile)
        except Exception as exc:
            logger.debug("rtl_power error: %s", exc)
            return {}
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass

    def _parse_rtl_power_csv(self, filepath: str) -> dict[float, float]:
        readings: dict[float, list[float]] = {}
        try:
            with open(filepath, newline="") as f:
                for row in csv.reader(f):
                    if len(row) < 7:
                        continue
                    try:
                        start_hz = float(row[2])
                        step_hz = float(row[4])
                        samples = [float(v) for v in row[6:] if v.strip()]
                        for i, power in enumerate(samples):
                            freq = (start_hz + i * step_hz) / 1_000_000
                            readings.setdefault(freq, []).append(power)
                    except (ValueError, IndexError):
                        pass
        except Exception:
            pass
        return {f: sum(v) / len(v) for f, v in readings.items() if v}

    def _sweep_hackrf(
        self, start_mhz: float, stop_mhz: float, step_hz: int, gain: int
    ) -> dict[float, float]:
        readings: dict[float, list[float]] = {}
        try:
            proc = subprocess.Popen(
                [
                    "hackrf_sweep",
                    "-f", f"{int(start_mhz)}:{int(stop_mhz)}",
                    "-w", str(step_hz),
                    "-l", str(gain), "-g", str(gain),
                    "-n", "8192",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            deadline = time.time() + 6
            for line in proc.stdout:
                if time.time() > deadline:
                    proc.terminate()
                    break
                line = line.decode("utf-8", errors="replace").strip()
                parts = line.split(", ")
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
            logger.debug("hackrf_sweep error: %s", exc)
        return {f: sum(v) / len(v) for f, v in readings.items() if v}

    def _process_readings(
        self, band_name: str, readings: dict[float, float], store: Any
    ) -> None:
        if not readings:
            return

        baseline = self._baselines.get(band_name)
        if baseline is None or len(baseline) < len(readings) * 0.5:
            self._baselines[band_name] = dict(readings)
            self._hit_counters[band_name] = {}
            return

        counters = self._hit_counters.setdefault(band_name, {})

        for freq, power in readings.items():
            base_power = baseline.get(freq, power)
            if power > base_power + ANOMALY_THRESHOLD_DB:
                counters[freq] = counters.get(freq, 0) + 1
                if counters[freq] >= PERSIST_COUNT_THRESHOLD:
                    detection = CUASDetection(
                        subtool=self.name,
                        detection_type="RF_PERSISTENT_ANOMALY",
                        freq_mhz=freq,
                        rssi_dbm=power,
                        confidence="MEDIUM",
                        source_tool="rtl_power/hackrf_sweep",
                        raw_payload={"band": band_name, "baseline_dbm": base_power, "current_dbm": power},
                    )
                    store.add(detection)
                    self.emit("detection", detection.to_dict())
                    counters[freq] = 0
            else:
                counters[freq] = max(0, counters.get(freq, 0) - 1)
