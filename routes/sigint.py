"""SIGINT distributed signal intelligence routes.

Inspired by github.com/arall/sigint — autonomous SDR band scanning with
multi-protocol signal detection (keyfobs, TPMS, pagers, PMR voice, ISM devices)
and CoT/ATAK export for tactical integration.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, jsonify, request

from utils.responses import api_error, api_success
from utils.sse import format_sse
from utils.validation import validate_device_index, validate_gain

logger = logging.getLogger(__name__)

sigint_bp = Blueprint("sigint", __name__, url_prefix="/sigint")

# ─────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────

PROTOCOL_META = {
    "KEYFOB":    {"label": "Key Fob",    "freq_hint": "315/433 MHz", "risk": "low"},
    "TPMS":      {"label": "TPMS",       "freq_hint": "315/433 MHz", "risk": "low"},
    "PAGER":     {"label": "Pager",      "freq_hint": "152/466 MHz", "risk": "medium"},
    "PMR":       {"label": "PMR Voice",  "freq_hint": "446 MHz",     "risk": "medium"},
    "ISM":       {"label": "ISM Device", "freq_hint": "433/868 MHz", "risk": "low"},
    "UNKNOWN":   {"label": "Unknown",    "freq_hint": "",            "risk": "unknown"},
}

TTL_SECONDS = 600


@dataclass
class SIGINTDetection:
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts_utc: float = field(default_factory=time.time)
    protocol: str = "UNKNOWN"
    freq_mhz: float = 0.0
    rssi_dbm: float = 0.0
    modulation: str = ""
    bandwidth_khz: float = 0.0
    message: str = ""
    decoded_data: dict = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    node_id: str = "local"
    source_tool: str = ""

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "ts_utc": self.ts_utc,
            "protocol": self.protocol,
            "freq_mhz": self.freq_mhz,
            "rssi_dbm": self.rssi_dbm,
            "modulation": self.modulation,
            "bandwidth_khz": self.bandwidth_khz,
            "message": self.message,
            "decoded_data": self.decoded_data,
            "lat": self.lat,
            "lon": self.lon,
            "node_id": self.node_id,
            "source_tool": self.source_tool,
            "protocol_meta": PROTOCOL_META.get(self.protocol, PROTOCOL_META["UNKNOWN"]),
        }


# ─────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────

_state = {
    "running": False,
    "scan_band": "ism_433",
    "device_index": 0,
    "gain": 40,
}
_detections: list[SIGINTDetection] = []
_detections_lock = threading.Lock()
_sse_queue: queue.Queue = queue.Queue(maxsize=500)
_scan_thread: threading.Thread | None = None
_stop_event = threading.Event()

SCAN_BANDS = {
    "ism_433": {"start_mhz": 433.050, "stop_mhz": 434.790, "label": "ISM 433 MHz"},
    "pmr_446": {"start_mhz": 446.000, "stop_mhz": 446.200, "label": "PMR 446 MHz"},
    "ism_868": {"start_mhz": 868.000, "stop_mhz": 868.600, "label": "ISM 868 MHz"},
    "pager":   {"start_mhz": 152.000, "stop_mhz": 174.000, "label": "Pager VHF"},
    "keyfob":  {"start_mhz": 315.000, "stop_mhz": 315.100, "label": "Keyfob 315 MHz"},
    "full_ism": {"start_mhz": 433.000, "stop_mhz": 434.800, "label": "Full ISM Sweep"},
}


# ─────────────────────────────────────────────────────────
# Scanner logic
# ─────────────────────────────────────────────────────────

def _emit(event_type: str, data: dict) -> None:
    try:
        _sse_queue.put_nowait({"type": event_type, **data})
    except queue.Full:
        pass


def _cleanup_detections() -> None:
    cutoff = time.time() - TTL_SECONDS
    with _detections_lock:
        global _detections
        _detections = [d for d in _detections if d.ts_utc > cutoff]


def _classify_rtl433_output(data: dict) -> SIGINTDetection:
    """Map rtl_433 JSON output to a SIGINTDetection."""
    model = data.get("model", "")
    freq = float(data.get("freq", 0)) / 1_000_000 if data.get("freq") else float(data.get("frequency", 0))

    protocol = "UNKNOWN"
    if any(k in model.lower() for k in ["tpms", "tire"]):
        protocol = "TPMS"
    elif any(k in model.lower() for k in ["key", "remote", "fob"]):
        protocol = "KEYFOB"
    elif freq and 445.8 <= freq <= 446.2:
        protocol = "PMR"
    else:
        protocol = "ISM"

    return SIGINTDetection(
        protocol=protocol,
        freq_mhz=freq,
        rssi_dbm=float(data.get("rssi", 0) or 0),
        modulation=data.get("mod", ""),
        message=model,
        decoded_data=data,
        source_tool="rtl_433",
    )


def _scan_loop(band: str, device_index: int, gain: int) -> None:
    band_cfg = SCAN_BANDS.get(band, SCAN_BANDS["ism_433"])
    start = band_cfg["start_mhz"]
    stop = band_cfg["stop_mhz"]

    _emit("status", {"msg": f"Scanning {band_cfg['label']}"})

    if not shutil.which("rtl_433"):
        _emit("status", {"msg": "rtl_433 not found — install rtl-433 package"})
        while not _stop_event.is_set():
            time.sleep(5)
        return

    freq_arg = f"{start}M" if abs(stop - start) < 0.5 else f"{start}M:{stop}M"
    cmd = [
        "rtl_433",
        "-f", freq_arg,
        "-g", str(gain),
        "-d", str(device_index),
        "-F", "json",
        "-M", "level",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        _emit("status", {"msg": f"rtl_433 started (pid {proc.pid})"})

        for line in iter(proc.stdout.readline, b""):
            if _stop_event.is_set():
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                detection = _classify_rtl433_output(data)
                _cleanup_detections()
                with _detections_lock:
                    _detections.append(detection)
                _emit("detection", detection.to_dict())
            except (json.JSONDecodeError, Exception) as exc:
                logger.debug("rtl_433 parse error: %s", exc)

        proc.terminate()
    except Exception as exc:
        logger.error("SIGINT scan error: %s", exc)
        _emit("error", {"msg": str(exc)})


# ─────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────

@sigint_bp.route("/status", methods=["GET"])
def status():
    _cleanup_detections()
    with _detections_lock:
        count = len(_detections)
        protocols = {}
        for d in _detections:
            protocols[d.protocol] = protocols.get(d.protocol, 0) + 1

    return jsonify({
        "running": _state["running"],
        "scan_band": _state["scan_band"],
        "detections_count": count,
        "protocols": protocols,
        "bands": list(SCAN_BANDS.keys()),
    })


@sigint_bp.route("/start", methods=["POST"])
def start_scan():
    global _scan_thread, _stop_event

    if _state["running"]:
        return api_error("Already running")

    data = request.get_json() or {}
    try:
        device_index = validate_device_index(data.get("device_index", 0))
        gain = validate_gain(data.get("gain", 40))
    except Exception as exc:
        return api_error(str(exc))

    band = data.get("scan_band", "ism_433")
    if band not in SCAN_BANDS:
        return api_error(f"Unknown band: {band}. Valid: {list(SCAN_BANDS.keys())}")

    _state.update({"running": True, "scan_band": band, "device_index": device_index, "gain": gain})
    _stop_event.clear()

    _scan_thread = threading.Thread(
        target=_scan_loop, args=(band, device_index, gain), daemon=True
    )
    _scan_thread.start()
    return api_success("SIGINT scan started")


@sigint_bp.route("/stop", methods=["POST"])
def stop_scan():
    global _scan_thread
    _stop_event.set()
    if _scan_thread and _scan_thread.is_alive():
        _scan_thread.join(timeout=5)
    _state["running"] = False
    return api_success("SIGINT scan stopped")


@sigint_bp.route("/detections", methods=["GET"])
def get_detections():
    _cleanup_detections()
    protocol_filter = request.args.get("protocol")
    with _detections_lock:
        results = list(_detections)
    if protocol_filter:
        results = [d for d in results if d.protocol == protocol_filter.upper()]
    return jsonify([d.to_dict() for d in sorted(results, key=lambda x: x.ts_utc, reverse=True)])


@sigint_bp.route("/clear", methods=["POST"])
def clear():
    with _detections_lock:
        _detections.clear()
    return api_success("Detections cleared")


@sigint_bp.route("/bands", methods=["GET"])
def list_bands():
    return jsonify([{"id": k, **v} for k, v in SCAN_BANDS.items()])


@sigint_bp.route("/export/cot", methods=["GET"])
def export_cot():
    """Export detections as CoT (Cursor on Target) XML for ATAK/TAK integration."""
    _cleanup_detections()
    with _detections_lock:
        dets = list(_detections)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<events>']
    for d in dets:
        if d.lat and d.lon:
            ts = datetime.utcfromtimestamp(d.ts_utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(
                f'  <event version="2.0" uid="{d.signal_id}" type="a-u-G-E" '
                f'time="{ts}" start="{ts}" stale="{ts}" how="m-g">'
                f'<point lat="{d.lat}" lon="{d.lon}" hae="0" ce="50" le="50"/>'
                f'<detail><remarks>{d.protocol}: {d.message} @ {d.freq_mhz:.3f} MHz</remarks></detail>'
                f'</event>'
            )
    lines.append('</events>')

    return Response(
        "\n".join(lines),
        mimetype="application/xml",
        headers={"Content-Disposition": "attachment; filename=sigint_cot.xml"}
    )


@sigint_bp.route("/stream", methods=["GET"])
def stream():
    def generate():
        while True:
            try:
                event = _sse_queue.get(timeout=1.0)
                yield format_sse(event, event_type=event.get("type", "event"))
            except queue.Empty:
                yield format_sse({"type": "keepalive", "ts": time.time()}, event_type="keepalive")

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
