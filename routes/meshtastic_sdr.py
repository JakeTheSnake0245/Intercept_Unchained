from flask import Blueprint, jsonify, request
import subprocess
import shutil
import os

meshtastic_sdr_bp = Blueprint("meshtastic_sdr", __name__, url_prefix="/meshtastic_sdr")

@meshtastic_sdr_bp.get("/status")
def status():
    """
    Lab-oriented module: provides decoder availability status.
    This does NOT start any capture by itself.
    """
    # Placeholder: if you later install a decoder binary, detect it here.
    decoders = {
        "rtl_decoder": shutil.which("meshtastic_sdr_rtl") is not None,
        "hackrf_decoder": shutil.which("meshtastic_sdr_hackrf") is not None,
    }
    return jsonify({
        "ok": True,
        "module": "meshtastic_sdr",
        "decoders": decoders,
        "note": "Decoder integration is scaffolded. Install/configure your lab decoder to enable decoding.",
    })

@meshtastic_sdr_bp.post("/decode_file")
def decode_file():
    """
    Decode a provided capture file (lab workflow).
    Expected: a file path on disk (server-local), or later an uploaded file.
    This endpoint is intentionally conservative: it only decodes files you point it at.
    """
    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"ok": False, "error": "Missing 'path'"}), 400
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"File not found: {path}"}), 404

    # Placeholder command: replace with your chosen open-source decoder invocation.
    # For now, we just return the file metadata.
    st = os.stat(path)
    return jsonify({
        "ok": True,
        "path": path,
        "size_bytes": st.st_size,
        "note": "Wire your decoder here (RTL/HackRF). This scaffold avoids live capture automation.",
    })
