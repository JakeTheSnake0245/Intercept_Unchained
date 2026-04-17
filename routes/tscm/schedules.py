"""
TSCM Schedule Routes

Handles /schedules/* endpoints for automated sweep scheduling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from flask import jsonify, request

from routes.tscm import (
    _get_schedule_timezone,
    _next_run_from_cron,
    _start_sweep_internal,
    tscm_bp,
)
from utils.database import (
    create_tscm_schedule,
    delete_tscm_schedule,
    get_all_tscm_schedules,
    get_tscm_schedule,
    update_tscm_schedule,
)

logger = logging.getLogger('intercept.tscm')


@tscm_bp.route('/schedules', methods=['GET'])
def list_schedules():
    """List all TSCM sweep schedules."""
    enabled_param = request.args.get('enabled')
    enabled = None
    if enabled_param is not None:
        enabled = enabled_param.lower() in ('1', 'true', 'yes')

    schedules = get_all_tscm_schedules(enabled=enabled, limit=200)
    return jsonify({
        'status': 'success',
        'count': len(schedules),
        'schedules': schedules,
    })


@tscm_bp.route('/schedules', methods=['POST'])
def create_schedule():
    """Create a new sweep schedule."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    cron_expression = (data.get('cron_expression') or '').strip()
    sweep_type = data.get('sweep_type', 'standard')
    baseline_id = data.get('baseline_id')
    zone_name = data.get('zone_name')
    enabled = bool(data.get('enabled', True))
    notify_on_threat = bool(data.get('notify_on_threat', True))
    notify_email = data.get('notify_email')

    if not name:
        return jsonify({'status': 'error', 'message': 'Schedule name required'}), 400
    if not cron_expression:
        return jsonify({'status': 'error', 'message': 'cron_expression required'}), 400

    next_run = None
    if enabled:
        try:
            tz = _get_schedule_timezone(zone_name)
            next_local = _next_run_from_cron(cron_expression, datetime.now(tz))
            next_run = next_local.astimezone(timezone.utc).isoformat() if next_local else None
        except Exception as e:
            return jsonify({'status': 'error', 'message': f'Invalid cron: {e}'}), 400

    schedule_id = create_tscm_schedule(
        name=name,
        cron_expression=cron_expression,
        sweep_type=sweep_type,
        baseline_id=baseline_id,
        zone_name=zone_name,
        enabled=enabled,
        notify_on_threat=notify_on_threat,
        notify_email=notify_email,
        next_run=next_run,
    )
    schedule = get_tscm_schedule(schedule_id)
    return jsonify({
        'status': 'success',
        'message': 'Schedule created',
        'schedule': schedule
    })


@tscm_bp.route('/schedules/<int:schedule_id>', methods=['PUT', 'PATCH'])
def update_schedule(schedule_id: int):
    """Update a sweep schedule."""
    schedule = get_tscm_schedule(schedule_id)
    if not schedule:
        return jsonify({'status': 'error', 'message': 'Schedule not found'}), 404

    data = request.get_json() or {}
    updates: dict[str, Any] = {}

    for key in ('name', 'cron_expression', 'sweep_type', 'baseline_id', 'zone_name', 'notify_email'):
        if key in data:
            updates[key] = data[key]

    if 'baseline_id' in updates and updates['baseline_id'] in ('', None):
        updates['baseline_id'] = None

    if 'enabled' in data:
        updates['enabled'] = 1 if data['enabled'] else 0
    if 'notify_on_threat' in data:
        updates['notify_on_threat'] = 1 if data['notify_on_threat'] else 0

    # Recalculate next_run when cron/zone/enabled changes
    if any(k in updates for k in ('cron_expression', 'zone_name', 'enabled')):
        if updates.get('enabled', schedule.get('enabled', 1)):
            cron_expr = updates.get('cron_expression', schedule.get('cron_expression', ''))
            zone_name = updates.get('zone_name', schedule.get('zone_name'))
            try:
                tz = _get_schedule_timezone(zone_name)
                next_local = _next_run_from_cron(cron_expr, datetime.now(tz))
                updates['next_run'] = next_local.astimezone(timezone.utc).isoformat() if next_local else None
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'Invalid cron: {e}'}), 400
        else:
            updates['next_run'] = None

    if not updates:
        return jsonify({'status': 'error', 'message': 'No updates provided'}), 400

    update_tscm_schedule(schedule_id, **updates)
    schedule = get_tscm_schedule(schedule_id)
    return jsonify({'status': 'success', 'schedule': schedule})


@tscm_bp.route('/schedules/<int:schedule_id>', methods=['DELETE'])
def delete_schedule(schedule_id: int):
    """Delete a sweep schedule."""
    success = delete_tscm_schedule(schedule_id)
    if not success:
        return jsonify({'status': 'error', 'message': 'Schedule not found'}), 404
    return jsonify({'status': 'success', 'message': 'Schedule deleted'})


@tscm_bp.route('/schedules/<int:schedule_id>/run', methods=['POST'])
def run_schedule_now(schedule_id: int):
    """Trigger a scheduled sweep immediately."""
    schedule = get_tscm_schedule(schedule_id)
    if not schedule:
        return jsonify({'status': 'error', 'message': 'Schedule not found'}), 404

    result = _start_sweep_internal(
        sweep_type=schedule.get('sweep_type') or 'standard',
        baseline_id=schedule.get('baseline_id'),
        wifi_enabled=True,
        bt_enabled=True,
        rf_enabled=True,
        wifi_interface='',
        bt_interface='',
        sdr_device=None,
        verbose_results=False,
    )

    if result.get('status') != 'success':
        status_code = result.pop('http_status', 400)
        return jsonify(result), status_code

    # Update schedule run timestamps
    cron_expr = schedule.get('cron_expression') or ''
    tz = _get_schedule_timezone(schedule.get('zone_name'))
    now_utc = datetime.now(timezone.utc)
    try:
        next_local = _next_run_from_cron(cron_expr, datetime.now(tz))
    except Exception:
        next_local = None

    update_tscm_schedule(
        schedule_id,
        last_run=now_utc.isoformat(),
        next_run=next_local.astimezone(timezone.utc).isoformat() if next_local else None,
    )

    return jsonify(result)
