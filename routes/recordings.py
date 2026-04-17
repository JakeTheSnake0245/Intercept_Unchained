"""Session recording API endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, request, send_file

from utils.recording import RECORDING_ROOT, get_recording_manager
from utils.responses import api_error, api_success

recordings_bp = Blueprint('recordings', __name__, url_prefix='/recordings')


@recordings_bp.route('/start', methods=['POST'])
def start_recording():
    data = request.get_json() or {}
    mode = (data.get('mode') or '').strip()
    if not mode:
        return api_error('mode is required', 400)

    label = data.get('label')
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}

    manager = get_recording_manager()
    session = manager.start_recording(mode=mode, label=label, metadata=metadata)

    return api_success(data={'session': {
        'id': session.id,
        'mode': session.mode,
        'label': session.label,
        'started_at': session.started_at.isoformat(),
        'file_path': str(session.file_path),
    }})


@recordings_bp.route('/stop', methods=['POST'])
def stop_recording():
    data = request.get_json() or {}
    mode = data.get('mode')
    session_id = data.get('id')

    manager = get_recording_manager()
    session = manager.stop_recording(mode=mode, session_id=session_id)
    if not session:
        return api_error('No active recording found', 404)

    return api_success(data={'session': {
        'id': session.id,
        'mode': session.mode,
        'label': session.label,
        'started_at': session.started_at.isoformat(),
        'stopped_at': session.stopped_at.isoformat() if session.stopped_at else None,
        'event_count': session.event_count,
        'size_bytes': session.size_bytes,
        'file_path': str(session.file_path),
    }})


@recordings_bp.route('', methods=['GET'])
def list_recordings():
    manager = get_recording_manager()
    limit = request.args.get('limit', default=50, type=int)
    return api_success(data={
        'recordings': manager.list_recordings(limit=limit),
        'active': manager.get_active(),
    })


@recordings_bp.route('/<session_id>', methods=['GET'])
def get_recording(session_id: str):
    manager = get_recording_manager()
    rec = manager.get_recording(session_id)
    if not rec:
        return api_error('Recording not found', 404)
    return api_success(data={'recording': rec})


@recordings_bp.route('/<session_id>/download', methods=['GET'])
def download_recording(session_id: str):
    manager = get_recording_manager()
    rec = manager.get_recording(session_id)
    if not rec:
        return api_error('Recording not found', 404)

    file_path = Path(rec['file_path'])
    try:
        resolved_root = RECORDING_ROOT.resolve()
        resolved_file = file_path.resolve()
        if resolved_root not in resolved_file.parents:
            return api_error('Invalid recording path', 400)
    except Exception:
        return api_error('Invalid recording path', 400)

    if not file_path.exists():
        return api_error('Recording file missing', 404)

    return send_file(
        file_path,
        mimetype='application/x-ndjson',
        as_attachment=True,
        download_name=file_path.name,
    )


@recordings_bp.route('/<session_id>/events', methods=['GET'])
def get_recording_events(session_id: str):
    """Return parsed events from a recording for in-app replay."""
    manager = get_recording_manager()
    rec = manager.get_recording(session_id)
    if not rec:
        return api_error('Recording not found', 404)

    file_path = Path(rec['file_path'])
    try:
        resolved_root = RECORDING_ROOT.resolve()
        resolved_file = file_path.resolve()
        if resolved_root not in resolved_file.parents:
            return api_error('Invalid recording path', 400)
    except Exception:
        return api_error('Invalid recording path', 400)

    if not file_path.exists():
        return api_error('Recording file missing', 404)

    limit = max(1, min(5000, request.args.get('limit', default=500, type=int)))
    offset = max(0, request.args.get('offset', default=0, type=int))

    events: list[dict] = []
    seen = 0
    with file_path.open('r', encoding='utf-8', errors='replace') as fh:
        for idx, line in enumerate(fh):
            if idx < offset:
                continue
            if seen >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
                seen += 1
            except json.JSONDecodeError:
                continue

    return api_success(data={
        'recording': {
            'id': rec['id'],
            'mode': rec['mode'],
            'started_at': rec['started_at'],
            'stopped_at': rec['stopped_at'],
            'event_count': rec['event_count'],
        },
        'offset': offset,
        'limit': limit,
        'returned': len(events),
        'events': events,
    })
