"""
TSCM Case Management Routes

Handles /cases/* endpoints.
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from routes.tscm import tscm_bp

logger = logging.getLogger('intercept.tscm')


@tscm_bp.route('/cases', methods=['GET'])
def list_cases():
    """List all TSCM cases."""
    from utils.database import get_all_tscm_cases

    status = request.args.get('status')
    limit = request.args.get('limit', 50, type=int)

    cases = get_all_tscm_cases(status=status, limit=limit)

    return jsonify({
        'status': 'success',
        'count': len(cases),
        'cases': cases
    })


@tscm_bp.route('/cases', methods=['POST'])
def create_case():
    """Create a new TSCM case."""
    from utils.database import create_tscm_case

    data = request.get_json() or {}

    name = data.get('name')
    if not name:
        return jsonify({'status': 'error', 'message': 'name is required'}), 400

    case_id = create_tscm_case(
        name=name,
        description=data.get('description'),
        location=data.get('location'),
        priority=data.get('priority', 'normal'),
        created_by=data.get('created_by'),
        metadata=data.get('metadata')
    )

    return jsonify({
        'status': 'success',
        'message': 'Case created',
        'case_id': case_id
    })


@tscm_bp.route('/cases/<int:case_id>', methods=['GET'])
def get_case(case_id: int):
    """Get a TSCM case with all linked sweeps, threats, and notes."""
    from utils.database import get_tscm_case

    case = get_tscm_case(case_id)
    if not case:
        return jsonify({'status': 'error', 'message': 'Case not found'}), 404

    return jsonify({
        'status': 'success',
        'case': case
    })


@tscm_bp.route('/cases/<int:case_id>', methods=['PUT'])
def update_case(case_id: int):
    """Update a TSCM case."""
    from utils.database import update_tscm_case

    data = request.get_json() or {}

    success = update_tscm_case(
        case_id=case_id,
        status=data.get('status'),
        priority=data.get('priority'),
        assigned_to=data.get('assigned_to'),
        notes=data.get('notes')
    )

    if not success:
        return jsonify({'status': 'error', 'message': 'Case not found'}), 404

    return jsonify({
        'status': 'success',
        'message': 'Case updated'
    })


@tscm_bp.route('/cases/<int:case_id>/sweeps/<int:sweep_id>', methods=['POST'])
def link_sweep_to_case(case_id: int, sweep_id: int):
    """Link a sweep to a case."""
    from utils.database import add_sweep_to_case

    success = add_sweep_to_case(case_id, sweep_id)

    return jsonify({
        'status': 'success' if success else 'error',
        'message': 'Sweep linked to case' if success else 'Already linked or not found'
    })


@tscm_bp.route('/cases/<int:case_id>/threats/<int:threat_id>', methods=['POST'])
def link_threat_to_case(case_id: int, threat_id: int):
    """Link a threat to a case."""
    from utils.database import add_threat_to_case

    success = add_threat_to_case(case_id, threat_id)

    return jsonify({
        'status': 'success' if success else 'error',
        'message': 'Threat linked to case' if success else 'Already linked or not found'
    })


@tscm_bp.route('/cases/<int:case_id>/notes', methods=['POST'])
def add_note_to_case(case_id: int):
    """Add a note to a case."""
    from utils.database import add_case_note

    data = request.get_json() or {}

    content = data.get('content')
    if not content:
        return jsonify({'status': 'error', 'message': 'content is required'}), 400

    note_id = add_case_note(
        case_id=case_id,
        content=content,
        note_type=data.get('note_type', 'general'),
        created_by=data.get('created_by')
    )

    return jsonify({
        'status': 'success',
        'message': 'Note added',
        'note_id': note_id
    })
