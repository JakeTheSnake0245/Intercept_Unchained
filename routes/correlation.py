"""Device correlation routes."""

from __future__ import annotations

from flask import Blueprint, Response, request

import app as app_module
from utils.correlation import get_correlations
from utils.logging import get_logger
from utils.responses import api_error, api_success

logger = get_logger('intercept.correlation')

correlation_bp = Blueprint('correlation', __name__, url_prefix='/correlation')


@correlation_bp.route('', methods=['GET'])
def get_device_correlations() -> Response:
    """
    Get device correlations between WiFi and Bluetooth.

    Query params:
        min_confidence: Minimum confidence threshold (default 0.5)
        include_historical: Include database correlations (default true)
    """
    min_confidence = request.args.get('min_confidence', 0.5, type=float)
    include_historical = request.args.get('include_historical', 'true').lower() == 'true'

    try:
        # Get current device data
        wifi_devices = dict(app_module.wifi_networks)
        wifi_devices.update(dict(app_module.wifi_clients))
        bt_devices = dict(app_module.bt_devices)

        # Calculate correlations
        correlations = get_correlations(
            wifi_devices=wifi_devices,
            bt_devices=bt_devices,
            min_confidence=min_confidence,
            include_historical=include_historical
        )

        return api_success(data={
            'correlations': correlations,
            'wifi_count': len(wifi_devices),
            'bt_count': len(bt_devices)
        })
    except Exception as e:
        logger.error(f"Error calculating correlations: {e}")
        return api_error(str(e), 500)


@correlation_bp.route('/analyze', methods=['POST'])
def analyze_correlation() -> Response:
    """
    Analyze specific device pair for correlation.

    Request body:
        wifi_mac: WiFi device MAC address
        bt_mac: Bluetooth device MAC address
    """
    data = request.json or {}
    wifi_mac = data.get('wifi_mac')
    bt_mac = data.get('bt_mac')

    if not wifi_mac or not bt_mac:
        return api_error('wifi_mac and bt_mac are required', 400)

    try:
        # Get device data
        wifi_device = app_module.wifi_networks.get(wifi_mac)
        if not wifi_device:
            wifi_device = app_module.wifi_clients.get(wifi_mac)

        bt_device = app_module.bt_devices.get(bt_mac)

        if not wifi_device:
            return api_error(f'WiFi device {wifi_mac} not found', 404)

        if not bt_device:
            return api_error(f'Bluetooth device {bt_mac} not found', 404)

        # Calculate correlation for this specific pair
        correlations = get_correlations(
            wifi_devices={wifi_mac: wifi_device},
            bt_devices={bt_mac: bt_device},
            min_confidence=0.0,  # Show even low confidence for analysis
            include_historical=True
        )

        if correlations:
            return api_success(data={'correlation': correlations[0]})
        else:
            return api_success(data={'correlation': None}, message='No correlation detected between these devices')
    except Exception as e:
        logger.error(f"Error analyzing correlation: {e}")
        return api_error(str(e), 500)
