"""DJI DroneID decoder — OcuSync 2.0/3.0 proprietary protocol.

Supports:
  - Offline IQ analysis via samples2djidroneid Docker image
  - Real-time via AntSDR E200 / ZMQ (antsdr_dji_droneid)
  - HackRF IQ capture + offline decode
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .base import BaseSubtool
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)

ZMQ_PORT = 4225
CAPTURE_DIR = "/tmp/cuas/iq"
HACKRF_SAMPLE_RATE = 20_000_000  # 20 MSPS
HACKRF_FREQ_HZ = 2_437_000_000  # 2.4 GHz ISM center


class DJIDropneIDDecoder(BaseSubtool):
    name = "dji_droneid"

    def _run(self, sdr_type: str = "hackrf", **kwargs: Any) -> None:
        store = get_cuas_store()
        self.emit("status", {"msg": "DJI DroneID decoder starting"})

        if shutil.which("antsdr_dji_droneid"):
            self._run_antsdr(store)
        elif self._docker_image_available():
            self._run_hackrf_offline(store)
        else:
            self.emit("status", {
                "msg": "No DJI DroneID decoder found. Install antsdr_dji_droneid or "
                       "docker pull ghcr.io/anarkiwi/samples2djidroneid:latest"
            })
            while self._running:
                time.sleep(10)

    def _docker_image_available(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            result = subprocess.run(
                ["docker", "images", "-q", "ghcr.io/anarkiwi/samples2djidroneid:latest"],
                capture_output=True, text=True, timeout=5
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _run_antsdr(self, store: Any) -> None:
        try:
            import zmq
            ctx = zmq.Context()
            sub = ctx.socket(zmq.SUB)
            sub.setsockopt_string(zmq.SUBSCRIBE, "")
            sub.connect(f"tcp://127.0.0.1:{ZMQ_PORT}")

            proc = subprocess.Popen(
                ["antsdr_dji_droneid", "--zmq", f"tcp://*:{ZMQ_PORT}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.emit("status", {"msg": f"AntSDR DroneID decoder running (pid {proc.pid})"})

            while self._running:
                try:
                    raw = sub.recv_string(flags=zmq.NOBLOCK)
                    self._handle_droneid(json.loads(raw), store)
                except zmq.Again:
                    time.sleep(0.1)

            proc.terminate()
            sub.close()
            ctx.term()
        except ImportError:
            logger.warning("pyzmq not installed")

    def _run_hackrf_offline(self, store: Any) -> None:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        self.emit("status", {"msg": "HackRF IQ capture mode — 10s sweeps"})

        while self._running:
            capture_file = os.path.join(CAPTURE_DIR, f"dji_{int(time.time())}.raw")
            self._capture_hackrf(capture_file)
            if os.path.exists(capture_file):
                self._decode_offline(capture_file, store)
                try:
                    os.unlink(capture_file)
                except OSError:
                    pass
            time.sleep(2)

    def _capture_hackrf(self, output_file: str) -> None:
        if not shutil.which("hackrf_transfer"):
            time.sleep(10)
            return
        try:
            subprocess.run(
                [
                    "hackrf_transfer", "-r", output_file,
                    "-f", str(HACKRF_FREQ_HZ),
                    "-s", str(HACKRF_SAMPLE_RATE),
                    "-n", str(HACKRF_SAMPLE_RATE * 10),  # 10 seconds
                    "-l", "40", "-g", "40",
                ],
                timeout=15, capture_output=True
            )
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            logger.debug("hackrf_transfer error: %s", exc)

    def _decode_offline(self, iq_file: str, store: Any) -> None:
        try:
            result = subprocess.run(
                ["docker", "run", "--rm",
                 "-v", f"{CAPTURE_DIR}:/tmp",
                 "ghcr.io/anarkiwi/samples2djidroneid:latest",
                 f"/tmp/{os.path.basename(iq_file)}"],
                capture_output=True, text=True, timeout=60
            )
            for line in result.stdout.splitlines():
                try:
                    self._handle_droneid(json.loads(line), store)
                except (json.JSONDecodeError, Exception):
                    pass
        except Exception as exc:
            logger.debug("Offline decode error: %s", exc)

    def _handle_droneid(self, msg: dict, store: Any) -> None:
        detection = CUASDetection(
            subtool=self.name,
            detection_type="DJI_DRONEID",
            drone_serial=msg.get("serial_no") or msg.get("uuid"),
            drone_make="DJI",
            drone_lat=msg.get("latitude"),
            drone_lon=msg.get("longitude"),
            drone_alt_m=msg.get("altitude"),
            pilot_lat=msg.get("phone_app_latitude"),
            pilot_lon=msg.get("phone_app_longitude"),
            freq_mhz=2437.0,
            confidence="CONFIRMED",
            source_tool="dji_droneid",
            raw_payload=msg,
        )

        product_type = msg.get("product_type", "")
        if product_type:
            detection.drone_model = product_type

        store.add(detection)
        self.emit("detection", detection.to_dict())
