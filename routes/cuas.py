"""C-UAS (Counter-UAS) detection routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from flask import Blueprint, Response, jsonify, request

from utils.cuas.bt_fingerprint import BTFingerprint
from utils.cuas.dji_droneid import DJIDropneIDDecoder
from utils.cuas.fpv_detector import FPVDetector
from utils.cuas.gps_anomaly import GPSAnomalyDetector
from utils.cuas.remote_id import RemoteIDReceiver
from utils.cuas.spectrum import SpectrumSurveillance
from utils.cuas.store import get_cuas_store
from utils.cuas.wifi_fingerprint import WiFiFingerprint
from utils.responses import api_error, api_success
from utils.sse import format_sse

logger = logging.getLogger(__name__)

cuas_bp = Blueprint("cuas", __name__, url_prefix="/cuas")

# One instance per subtool
_subtools: dict[str, Any] = {
    "remote_id": RemoteIDReceiver(),
    "dji_droneid": DJIDropneIDDecoder(),
    "spectrum": SpectrumSurveillance(),
    "wifi_fingerprint": WiFiFingerprint(),
    "bt_fingerprint": BTFingerprint(),
    "fpv_detector": FPVDetector(),
    "gps_anomaly": GPSAnomalyDetector(),
}


# ─────────────────────────────────────────────────────────
# Status / Info
# ─────────────────────────────────────────────────────────

@cuas_bp.route("/status", methods=["GET"])
def status():
    store = get_cuas_store()
    detections = store.get_all()
    threat_level = store.get_highest_confidence()

    subtool_status = {name: tool.status() for name, tool in _subtools.items()}
    running_count = sum(1 for s in subtool_status.values() if s.get("running"))

    return jsonify({
        "running": running_count > 0,
        "active_subtools": running_count,
        "detections_count": len(detections),
        "drone_count": len({d.drone_serial for d in detections if d.drone_serial}),
        "threat_level": threat_level,
        "last_seen": detections[0].ts_utc if detections else None,
        "subtools": subtool_status,
    })


@cuas_bp.route("/detections", methods=["GET"])
def get_detections():
    store = get_cuas_store()
    return jsonify([d.to_dict() for d in store.get_all()])


@cuas_bp.route("/clear", methods=["POST"])
def clear_detections():
    get_cuas_store().clear()
    return api_success("Detections cleared")


# ─────────────────────────────────────────────────────────
# Subtool control
# ─────────────────────────────────────────────────────────

@cuas_bp.route("/subtool/<subtool_name>/start", methods=["POST"])
def start_subtool(subtool_name: str):
    tool = _subtools.get(subtool_name)
    if not tool:
        return api_error(f"Unknown subtool: {subtool_name}", 404)
    data = request.get_json() or {}
    result = tool.start(**data)
    return jsonify(result)


@cuas_bp.route("/subtool/<subtool_name>/stop", methods=["POST"])
def stop_subtool(subtool_name: str):
    tool = _subtools.get(subtool_name)
    if not tool:
        return api_error(f"Unknown subtool: {subtool_name}", 404)
    return jsonify(tool.stop())


@cuas_bp.route("/subtool/<subtool_name>/status", methods=["GET"])
def subtool_status(subtool_name: str):
    tool = _subtools.get(subtool_name)
    if not tool:
        return api_error(f"Unknown subtool: {subtool_name}", 404)
    return jsonify(tool.status())


# ─────────────────────────────────────────────────────────
# Start / Stop ALL subtools
# ─────────────────────────────────────────────────────────

@cuas_bp.route("/start", methods=["POST"])
def start_all():
    data = request.get_json() or {}
    scan_mode = data.get("scan_mode", "passive")
    results = {}

    subtools_to_run = list(_subtools.keys())
    if scan_mode == "passive":
        subtools_to_run = ["remote_id", "wifi_fingerprint", "bt_fingerprint", "gps_anomaly"]
    elif scan_mode == "wifi_bt":
        subtools_to_run = ["remote_id", "wifi_fingerprint", "bt_fingerprint"]

    for name in subtools_to_run:
        tool = _subtools[name]
        results[name] = tool.start(**data)

    return jsonify({"status": "started", "subtools": results})


@cuas_bp.route("/stop", methods=["POST"])
def stop_all():
    results = {name: tool.stop() for name, tool in _subtools.items()}
    return jsonify({"status": "stopped", "subtools": results})


# ─────────────────────────────────────────────────────────
# SSE streams
# ─────────────────────────────────────────────────────────

@cuas_bp.route("/stream", methods=["GET"])
def stream_all():
    """Aggregate SSE stream from all subtools."""

    def generate():
        store = get_cuas_store()
        last_count = 0
        while True:
            # Emit any new detections from subtool queues
            for name, tool in _subtools.items():
                event = tool.get_event(timeout=0.05)
                if event:
                    yield format_sse(event, event=event.get("type", "event"))

            # Periodic summary
            count = store.count()
            if count != last_count:
                last_count = count
                yield format_sse({
                    "type": "summary",
                    "detections_count": count,
                    "threat_level": store.get_highest_confidence(),
                }, event="summary")

            yield format_sse({"type": "keepalive", "ts": time.time()}, event="keepalive")
            time.sleep(1)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@cuas_bp.route("/subtool/<subtool_name>/stream", methods=["GET"])
def stream_subtool(subtool_name: str):
    tool = _subtools.get(subtool_name)
    if not tool:
        return api_error(f"Unknown subtool: {subtool_name}", 404)

    def generate():
        while True:
            event = tool.get_event(timeout=1.0)
            if event:
                yield format_sse(event, event=event.get("type", "event"))
            else:
                yield format_sse({"type": "keepalive", "ts": time.time()}, event="keepalive")

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
