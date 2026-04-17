"""Tool check and signal identification routes."""

from __future__ import annotations

from flask import Response, jsonify, request

from . import (
    find_ffmpeg,
    find_rtl_fm,
    find_rtl_power,
    find_rx_fm,
    logger,
    receiver_bp,
)

# ============================================
# TOOL CHECK ENDPOINT
# ============================================

@receiver_bp.route('/tools')
def check_tools() -> Response:
    """Check for required tools."""
    rtl_fm = find_rtl_fm()
    rtl_power = find_rtl_power()
    rx_fm = find_rx_fm()
    ffmpeg = find_ffmpeg()

    # Determine which SDR types are supported
    supported_sdr_types = []
    if rtl_fm:
        supported_sdr_types.append('rtlsdr')
    if rx_fm:
        # rx_fm from SoapySDR supports these types
        supported_sdr_types.extend(['hackrf', 'airspy', 'limesdr', 'sdrplay'])

    return jsonify({
        'rtl_fm': rtl_fm is not None,
        'rtl_power': rtl_power is not None,
        'rx_fm': rx_fm is not None,
        'ffmpeg': ffmpeg is not None,
        'available': (rtl_fm is not None or rx_fm is not None) and ffmpeg is not None,
        'supported_sdr_types': supported_sdr_types
    })


# ============================================
# SIGNAL IDENTIFICATION ENDPOINT
# ============================================

@receiver_bp.route('/signal/guess', methods=['POST'])
def guess_signal() -> Response:
    """Identify a signal based on frequency, modulation, and other parameters."""
    data = request.json or {}

    freq_mhz = data.get('frequency_mhz')
    if freq_mhz is None:
        return jsonify({'status': 'error', 'message': 'frequency_mhz is required'}), 400

    try:
        freq_mhz = float(freq_mhz)
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'Invalid frequency_mhz'}), 400

    if freq_mhz <= 0:
        return jsonify({'status': 'error', 'message': 'frequency_mhz must be positive'}), 400

    frequency_hz = int(freq_mhz * 1e6)

    modulation = data.get('modulation')
    bandwidth_hz = data.get('bandwidth_hz')
    if bandwidth_hz is not None:
        try:
            bandwidth_hz = int(bandwidth_hz)
        except (ValueError, TypeError):
            bandwidth_hz = None

    region = data.get('region', 'UK/EU')

    try:
        from utils.signal_guess import guess_signal_type_dict
        result = guess_signal_type_dict(
            frequency_hz=frequency_hz,
            modulation=modulation,
            bandwidth_hz=bandwidth_hz,
            region=region,
        )
        return jsonify({'status': 'ok', **result})
    except Exception as e:
        logger.error(f"Signal guess error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
