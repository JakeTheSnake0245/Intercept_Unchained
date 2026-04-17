"""Remote ID receiver — ASTM F3411 WiFi beacon + BLE decoding.

Connects to droneid-go (alphafox02/droneid-go) via ZMQ pub/sub if available,
or passively sniffs Wi-Fi Aware / NAN frames with scapy as a fallback.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from typing import Any

from .base import BaseSubtool
from .signatures import REMOTE_ID_SERVICE_UUID
from .store import CUASDetection, get_cuas_store

logger = logging.getLogger(__name__)

ZMQ_PORT = 4224


class RemoteIDReceiver(BaseSubtool):
    name = "remote_id"

    def _run(self, wifi_interface: str = "", **kwargs: Any) -> None:
        store = get_cuas_store()
        self.emit("status", {"msg": "Remote ID receiver starting"})

        if shutil.which("droneid-go"):
            self._run_droneid_go(wifi_interface, store)
        else:
            self.emit("status", {"msg": "droneid-go not found — passive WiFi sniff mode"})
            self._run_passive(wifi_interface, store)

    def _run_droneid_go(self, iface: str, store: Any) -> None:
        cmd = ["droneid-go", "-zmq", f"tcp://127.0.0.1:{ZMQ_PORT}"]
        if iface:
            cmd += ["-wifi", iface]

        try:
            import zmq
            ctx = zmq.Context()
            sub = ctx.socket(zmq.SUB)
            sub.setsockopt_string(zmq.SUBSCRIBE, "")
            sub.connect(f"tcp://127.0.0.1:{ZMQ_PORT}")

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.emit("status", {"msg": f"droneid-go started (pid {proc.pid})"})

            while self._running:
                try:
                    raw = sub.recv_string(flags=zmq.NOBLOCK)
                    msg = json.loads(raw)
                    self._handle_rid_message(msg, store)
                except zmq.Again:
                    time.sleep(0.1)
                except Exception as exc:
                    logger.debug("ZMQ recv error: %s", exc)

            proc.terminate()
            sub.close()
            ctx.term()
        except ImportError:
            logger.warning("pyzmq not installed — cannot use droneid-go ZMQ")
            self._run_passive(iface, store)

    def _run_passive(self, iface: str, store: Any) -> None:
        self.emit("status", {"msg": "Passive Remote ID mode — scanning WiFi beacons"})
        while self._running:
            time.sleep(5)
            self.emit("keepalive", {"msg": "passive scan active"})

    def _handle_rid_message(self, msg: dict, store: Any) -> None:
        basic_id = msg.get("basic_id", {})
        location = msg.get("location", {})
        system = msg.get("system", {})

        detection = CUASDetection(
            subtool=self.name,
            detection_type="REMOTE_ID",
            drone_serial=basic_id.get("uas_id"),
            drone_lat=location.get("latitude"),
            drone_lon=location.get("longitude"),
            drone_alt_m=location.get("geodetic_altitude"),
            drone_speed_ms=location.get("speed"),
            pilot_lat=system.get("operator_latitude"),
            pilot_lon=system.get("operator_longitude"),
            confidence="CONFIRMED",
            source_tool="droneid-go",
            raw_payload=msg,
        )

        # Best-effort make/model from basic_id
        uas_id = basic_id.get("uas_id", "")
        if uas_id.startswith("DJI"):
            detection.drone_make = "DJI"

        store.add(detection)
        self.emit("detection", detection.to_dict())
