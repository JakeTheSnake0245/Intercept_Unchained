"""
TSCM Meeting Window Routes

Handles /meeting/* endpoints for time correlation during sensitive periods.
"""

from __future__ import annotations

import logging
from datetime import datetime

from flask import jsonify, request

from routes.tscm import (
    _current_sweep_id,
    _emit_event,
    tscm_bp,
)
from utils.tscm.correlation import get_correlation_engine

logger = logging.getLogger('intercept.tscm')


@tscm_bp.route('/meeting/start', methods=['POST'])
def start_meeting():
    """
    Mark the start of a sensitive period (meeting, briefing, etc.).

    Devices detected during this window will receive additional scoring
    for meeting-correlated activity.
    """
    correlation = get_correlation_engine()
    correlation.start_meeting_window()

    _emit_event('meeting_started', {
        'timestamp': datetime.now().isoformat(),
        'message': 'Sensitive period monitoring active'
    })

    return jsonify({
        'status': 'success',
        'message': 'Meeting window started - devices detected now will be flagged'
    })


@tscm_bp.route('/meeting/end', methods=['POST'])
def end_meeting():
    """Mark the end of a sensitive period."""
    correlation = get_correlation_engine()
    correlation.end_meeting_window()

    _emit_event('meeting_ended', {
        'timestamp': datetime.now().isoformat()
    })

    return jsonify({
        'status': 'success',
        'message': 'Meeting window ended'
    })


@tscm_bp.route('/meeting/status')
def meeting_status():
    """Check if currently in a meeting window."""
    correlation = get_correlation_engine()
    in_meeting = correlation.is_during_meeting()

    return jsonify({
        'status': 'success',
        'in_meeting': in_meeting,
        'windows': [
            {
                'start': start.isoformat(),
                'end': end.isoformat() if end else None
            }
            for start, end in correlation.meeting_windows
        ]
    })


# =============================================================================
# Meeting Window Enhanced Endpoints
# =============================================================================

@tscm_bp.route('/meeting/start-tracked', methods=['POST'])
def start_tracked_meeting():
    """
    Start a tracked meeting window with database persistence.

    Tracks devices first seen during meeting and behavior changes.
    """
    from utils.database import start_meeting_window
    from utils.tscm.advanced import get_timeline_manager

    data = request.get_json() or {}

    meeting_id = start_meeting_window(
        sweep_id=_current_sweep_id,
        name=data.get('name'),
        location=data.get('location'),
        notes=data.get('notes')
    )

    # Start meeting in correlation engine
    correlation = get_correlation_engine()
    correlation.start_meeting_window()

    # Start in timeline manager
    manager = get_timeline_manager()
    manager.start_meeting_window()

    _emit_event('meeting_started', {
        'meeting_id': meeting_id,
        'timestamp': datetime.now().isoformat(),
        'name': data.get('name'),
    })

    return jsonify({
        'status': 'success',
        'message': 'Tracked meeting window started',
        'meeting_id': meeting_id
    })


@tscm_bp.route('/meeting/<int:meeting_id>/end', methods=['POST'])
def end_tracked_meeting(meeting_id: int):
    """End a tracked meeting window."""
    from utils.database import end_meeting_window
    from utils.tscm.advanced import get_timeline_manager

    success = end_meeting_window(meeting_id)
    if not success:
        return jsonify({'status': 'error', 'message': 'Meeting not found or already ended'}), 404

    # End in correlation engine
    correlation = get_correlation_engine()
    correlation.end_meeting_window()

    # End in timeline manager
    manager = get_timeline_manager()
    manager.end_meeting_window()

    _emit_event('meeting_ended', {
        'meeting_id': meeting_id,
        'timestamp': datetime.now().isoformat()
    })

    return jsonify({
        'status': 'success',
        'message': 'Meeting window ended'
    })


@tscm_bp.route('/meeting/<int:meeting_id>/summary')
def get_meeting_summary_endpoint(meeting_id: int):
    """Get detailed summary of device activity during a meeting."""
    try:
        from routes.tscm import _current_sweep_id
        from utils.database import get_meeting_windows
        from utils.tscm.advanced import generate_meeting_summary, get_timeline_manager

        # Get meeting window
        windows = get_meeting_windows(_current_sweep_id or 0)
        meeting = None
        for w in windows:
            if w.get('id') == meeting_id:
                meeting = w
                break

        if not meeting:
            return jsonify({'status': 'error', 'message': 'Meeting not found'}), 404

        # Get timelines and profiles
        manager = get_timeline_manager()
        timelines = manager.get_all_timelines()

        correlation = get_correlation_engine()
        profiles = [p.to_dict() for p in correlation.device_profiles.values()]

        summary = generate_meeting_summary(meeting, timelines, profiles)

        return jsonify({
            'status': 'success',
            'summary': summary.to_dict()
        })

    except Exception as e:
        logger.error(f"Get meeting summary error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@tscm_bp.route('/meeting/active')
def get_active_meeting():
    """Get currently active meeting window."""
    from utils.database import get_active_meeting_window

    meeting = get_active_meeting_window(_current_sweep_id)

    return jsonify({
        'status': 'success',
        'meeting': meeting,
        'is_active': meeting is not None
    })
