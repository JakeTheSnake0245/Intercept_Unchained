"""Alerting API endpoints."""

from __future__ import annotations

from collections.abc import Generator

from flask import Blueprint, Response, request

from utils.alerts import get_alert_manager
from utils.responses import api_error, api_success
from utils.sse import format_sse

alerts_bp = Blueprint('alerts', __name__, url_prefix='/alerts')


@alerts_bp.route('/rules', methods=['GET'])
def list_rules():
    manager = get_alert_manager()
    include_disabled = request.args.get('all') in ('1', 'true', 'yes')
    return api_success(data={'rules': manager.list_rules(include_disabled=include_disabled)})


@alerts_bp.route('/rules', methods=['POST'])
def create_rule():
    data = request.get_json() or {}
    if not isinstance(data.get('match', {}), dict):
        return api_error('match must be a JSON object', 400)

    manager = get_alert_manager()
    rule_id = manager.add_rule(data)
    return api_success(data={'rule_id': rule_id})


@alerts_bp.route('/rules/<int:rule_id>', methods=['PUT', 'PATCH'])
def update_rule(rule_id: int):
    data = request.get_json() or {}
    manager = get_alert_manager()
    ok = manager.update_rule(rule_id, data)
    if not ok:
        return api_error('Rule not found or no changes', 404)
    return api_success()


@alerts_bp.route('/rules/<int:rule_id>', methods=['DELETE'])
def delete_rule(rule_id: int):
    manager = get_alert_manager()
    ok = manager.delete_rule(rule_id)
    if not ok:
        return api_error('Rule not found', 404)
    return api_success()


@alerts_bp.route('/events', methods=['GET'])
def list_events():
    manager = get_alert_manager()
    limit = request.args.get('limit', default=100, type=int)
    mode = request.args.get('mode')
    severity = request.args.get('severity')
    events = manager.list_events(limit=limit, mode=mode, severity=severity)
    return api_success(data={'events': events})


@alerts_bp.route('/stream', methods=['GET'])
def stream_alerts() -> Response:
    manager = get_alert_manager()

    def generate() -> Generator[str, None, None]:
        for event in manager.stream_events(timeout=1.0):
            yield format_sse(event)

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response
