"""Ground Station REST API + SSE + WebSocket endpoints.

Phases implemented here:
  1 — Profile CRUD, scheduler control, observation history, SSE stream
  3 — SigMF recording browser (list / download / delete)
  5 — /ws/satellite_waterfall WebSocket
  6 — Rotator config / status / point / park endpoints
"""

from __future__ import annotations

import json
import queue
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

from utils.logging import get_logger
from utils.sse import sse_stream_fanout

logger = get_logger('intercept.ground_station.routes')

ground_station_bp = Blueprint('ground_station', __name__, url_prefix='/ground_station')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_scheduler():
    from utils.ground_station.scheduler import get_ground_station_scheduler
    return get_ground_station_scheduler()


def _get_queue():
    import app as _app
    return getattr(_app, 'ground_station_queue', None) or queue.Queue()


# ---------------------------------------------------------------------------
# Phase 1 — Observation Profiles
# ---------------------------------------------------------------------------


@ground_station_bp.route('/profiles', methods=['GET'])
def list_profiles():
    from utils.ground_station.observation_profile import list_profiles as _list
    return jsonify([p.to_dict() for p in _list()])


@ground_station_bp.route('/profiles/<int:norad_id>', methods=['GET'])
def get_profile(norad_id: int):
    from utils.ground_station.observation_profile import get_profile as _get
    p = _get(norad_id)
    if not p:
        return jsonify({'error': f'No profile for NORAD {norad_id}'}), 404
    return jsonify(p.to_dict())


@ground_station_bp.route('/profiles', methods=['POST'])
def create_profile():
    data = request.get_json(force=True) or {}
    try:
        _validate_profile(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    from utils.ground_station.observation_profile import (
        ObservationProfile,
        legacy_decoder_to_tasks,
        normalize_tasks,
        save_profile,
        tasks_to_legacy_decoder,
    )
    tasks = normalize_tasks(data.get('tasks'))
    if not tasks:
        tasks = legacy_decoder_to_tasks(
            str(data.get('decoder_type', 'fm')),
            bool(data.get('record_iq', False)),
        )
    profile = ObservationProfile(
        norad_id=int(data['norad_id']),
        name=str(data['name']),
        frequency_mhz=float(data['frequency_mhz']),
        decoder_type=tasks_to_legacy_decoder(tasks),
        gain=float(data.get('gain', 40.0)),
        bandwidth_hz=int(data.get('bandwidth_hz', 200_000)),
        min_elevation=float(data.get('min_elevation', 10.0)),
        enabled=bool(data.get('enabled', True)),
        record_iq=bool(data.get('record_iq', False)) or ('record_iq' in tasks),
        iq_sample_rate=int(data.get('iq_sample_rate', 2_400_000)),
        tasks=tasks,
    )
    saved = save_profile(profile)
    return jsonify(saved.to_dict()), 201


@ground_station_bp.route('/profiles/<int:norad_id>', methods=['PUT'])
def update_profile(norad_id: int):
    data = request.get_json(force=True) or {}
    from utils.ground_station.observation_profile import (
        get_profile as _get,
    )
    from utils.ground_station.observation_profile import (
        legacy_decoder_to_tasks,
        normalize_tasks,
        save_profile,
        tasks_to_legacy_decoder,
    )
    existing = _get(norad_id)
    if not existing:
        return jsonify({'error': f'No profile for NORAD {norad_id}'}), 404

    # Apply updates
    for field, cast in [
        ('name', str), ('frequency_mhz', float), ('decoder_type', str),
        ('gain', float), ('bandwidth_hz', int), ('min_elevation', float),
    ]:
        if field in data:
            setattr(existing, field, cast(data[field]))
    for field in ('enabled', 'record_iq'):
        if field in data:
            setattr(existing, field, bool(data[field]))
    if 'iq_sample_rate' in data:
        existing.iq_sample_rate = int(data['iq_sample_rate'])
    if 'tasks' in data:
        existing.tasks = normalize_tasks(data['tasks'])
    elif 'decoder_type' in data:
        existing.tasks = legacy_decoder_to_tasks(
            str(data.get('decoder_type', existing.decoder_type)),
            bool(data.get('record_iq', existing.record_iq)),
        )

    existing.decoder_type = tasks_to_legacy_decoder(existing.tasks)
    existing.record_iq = bool(existing.record_iq) or ('record_iq' in existing.tasks)

    saved = save_profile(existing)
    return jsonify(saved.to_dict())


@ground_station_bp.route('/profiles/<int:norad_id>', methods=['DELETE'])
def delete_profile(norad_id: int):
    from utils.ground_station.observation_profile import delete_profile as _del
    ok = _del(norad_id)
    if not ok:
        return jsonify({'error': f'No profile for NORAD {norad_id}'}), 404
    return jsonify({'status': 'deleted', 'norad_id': norad_id})


# ---------------------------------------------------------------------------
# Phase 1 — Scheduler control
# ---------------------------------------------------------------------------


@ground_station_bp.route('/scheduler/status', methods=['GET'])
def scheduler_status():
    return jsonify(_get_scheduler().get_status())


@ground_station_bp.route('/scheduler/enable', methods=['POST'])
def scheduler_enable():
    data = request.get_json(force=True) or {}
    try:
        lat = float(data.get('lat', 0.0))
        lon = float(data.get('lon', 0.0))
        device = int(data.get('device', 0))
        sdr_type = str(data.get('sdr_type', 'rtlsdr'))
    except (TypeError, ValueError) as e:
        return jsonify({'error': str(e)}), 400

    status = _get_scheduler().enable(lat=lat, lon=lon, device=device, sdr_type=sdr_type)
    return jsonify(status)


@ground_station_bp.route('/scheduler/disable', methods=['POST'])
def scheduler_disable():
    return jsonify(_get_scheduler().disable())


@ground_station_bp.route('/scheduler/observations', methods=['GET'])
def get_observations():
    return jsonify(_get_scheduler().get_scheduled_observations())


@ground_station_bp.route('/scheduler/trigger/<int:norad_id>', methods=['POST'])
def trigger_manual(norad_id: int):
    ok, msg = _get_scheduler().trigger_manual(norad_id)
    if not ok:
        return jsonify({'error': msg}), 400
    return jsonify({'status': 'started', 'message': msg})


@ground_station_bp.route('/scheduler/stop', methods=['POST'])
def stop_active():
    return jsonify(_get_scheduler().stop_active())


# ---------------------------------------------------------------------------
# Phase 1 — Observation history (from DB)
# ---------------------------------------------------------------------------


@ground_station_bp.route('/observations', methods=['GET'])
def observation_history():
    limit = min(int(request.args.get('limit', 50)), 200)
    try:
        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                '''SELECT * FROM ground_station_observations
                   ORDER BY created_at DESC LIMIT ?''',
                (limit,),
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Failed to fetch observation history: {e}")
        return jsonify([])


# ---------------------------------------------------------------------------
# Phase 1 — SSE stream
# ---------------------------------------------------------------------------


@ground_station_bp.route('/stream')
def sse_stream():
    gs_queue = _get_queue()
    return Response(
        sse_stream_fanout(gs_queue, 'ground_station'),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ---------------------------------------------------------------------------
# Phase 3 — SigMF recording browser
# ---------------------------------------------------------------------------


@ground_station_bp.route('/recordings', methods=['GET'])
def list_recordings():
    try:
        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                'SELECT * FROM sigmf_recordings ORDER BY created_at DESC LIMIT 100'
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        logger.error(f"Failed to fetch recordings: {e}")
        return jsonify([])


@ground_station_bp.route('/recordings/<int:rec_id>', methods=['GET'])
def get_recording(rec_id: int):
    try:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                'SELECT * FROM sigmf_recordings WHERE id=?', (rec_id,)
            ).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(dict(row))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ground_station_bp.route('/recordings/<int:rec_id>', methods=['DELETE'])
def delete_recording(rec_id: int):
    try:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                'SELECT sigmf_data_path, sigmf_meta_path FROM sigmf_recordings WHERE id=?',
                (rec_id,),
            ).fetchone()
            if not row:
                return jsonify({'error': 'Not found'}), 404
            # Remove files
            for path_col in ('sigmf_data_path', 'sigmf_meta_path'):
                p = Path(row[path_col])
                if p.exists():
                    p.unlink(missing_ok=True)
            conn.execute('DELETE FROM sigmf_recordings WHERE id=?', (rec_id,))
        return jsonify({'status': 'deleted', 'id': rec_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ground_station_bp.route('/recordings/<int:rec_id>/download/<file_type>')
def download_recording(rec_id: int, file_type: str):
    if file_type not in ('data', 'meta'):
        return jsonify({'error': 'file_type must be data or meta'}), 400
    try:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                'SELECT sigmf_data_path, sigmf_meta_path FROM sigmf_recordings WHERE id=?',
                (rec_id,),
            ).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404

        col = 'sigmf_data_path' if file_type == 'data' else 'sigmf_meta_path'
        p = Path(row[col])
        if not p.exists():
            return jsonify({'error': 'File not found on disk'}), 404

        mimetype = 'application/octet-stream' if file_type == 'data' else 'application/json'
        return send_file(p, mimetype=mimetype, as_attachment=True, download_name=p.name)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ground_station_bp.route('/outputs', methods=['GET'])
def list_outputs():
    try:
        query = '''
            SELECT * FROM ground_station_outputs
            WHERE (? IS NULL OR norad_id = ?)
              AND (? IS NULL OR observation_id = ?)
              AND (? IS NULL OR output_type = ?)
            ORDER BY created_at DESC
            LIMIT 200
        '''
        norad_id = request.args.get('norad_id', type=int)
        observation_id = request.args.get('observation_id', type=int)
        output_type = request.args.get('type')

        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                query,
                (
                    norad_id, norad_id,
                    observation_id, observation_id,
                    output_type, output_type,
                ),
            ).fetchall()

        results = []
        for row in rows:
            item = dict(row)
            metadata_raw = item.get('metadata_json')
            if metadata_raw:
                try:
                    item['metadata'] = json.loads(metadata_raw)
                except json.JSONDecodeError:
                    item['metadata'] = {}
            else:
                item['metadata'] = {}
            item.pop('metadata_json', None)
            results.append(item)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ground_station_bp.route('/outputs/<int:output_id>/download', methods=['GET'])
def download_output(output_id: int):
    try:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                'SELECT file_path FROM ground_station_outputs WHERE id=?',
                (output_id,),
            ).fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        p = Path(row['file_path'])
        if not p.exists():
            return jsonify({'error': 'File not found on disk'}), 404
        return send_file(p, as_attachment=True, download_name=p.name)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ground_station_bp.route('/decode-jobs', methods=['GET'])
def list_decode_jobs():
    try:
        query = '''
            SELECT * FROM ground_station_decode_jobs
            WHERE (? IS NULL OR norad_id = ?)
              AND (? IS NULL OR observation_id = ?)
              AND (? IS NULL OR backend = ?)
            ORDER BY created_at DESC
            LIMIT ?
        '''
        norad_id = request.args.get('norad_id', type=int)
        observation_id = request.args.get('observation_id', type=int)
        backend = request.args.get('backend')
        limit = min(request.args.get('limit', 20, type=int) or 20, 200)

        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                query,
                (
                    norad_id, norad_id,
                    observation_id, observation_id,
                    backend, backend,
                    limit,
                ),
            ).fetchall()

        results = []
        for row in rows:
            item = dict(row)
            details_raw = item.get('details_json')
            if details_raw:
                try:
                    item['details'] = json.loads(details_raw)
                except json.JSONDecodeError:
                    item['details'] = {}
            else:
                item['details'] = {}
            item.pop('details_json', None)
            results.append(item)
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Phase 5 — Live waterfall WebSocket
# ---------------------------------------------------------------------------


def init_ground_station_websocket(app) -> None:
    """Register the /ws/satellite_waterfall WebSocket endpoint."""
    try:
        from flask_sock import Sock
    except ImportError:
        logger.warning("flask-sock not installed — satellite waterfall WebSocket disabled")
        return

    sock = Sock(app)

    @sock.route('/ws/satellite_waterfall')
    def satellite_waterfall_ws(ws):
        """Stream binary waterfall frames from the active ground station IQ bus."""
        scheduler = _get_scheduler()
        wf_queue = scheduler.waterfall_queue

        from utils.sse import subscribe_fanout_queue
        sub_queue, unsubscribe = subscribe_fanout_queue(
            source_queue=wf_queue,
            channel_key='gs_waterfall',
            subscriber_queue_size=120,
        )

        try:
            while True:
                try:
                    frame = sub_queue.get(timeout=1.0)
                    try:
                        ws.send(frame)
                    except Exception:
                        break
                except queue.Empty:
                    if not ws.connected:
                        break
        finally:
            unsubscribe()


# ---------------------------------------------------------------------------
# Phase 6 — Rotator
# ---------------------------------------------------------------------------


@ground_station_bp.route('/rotator/status', methods=['GET'])
def rotator_status():
    from utils.rotator import get_rotator
    return jsonify(get_rotator().get_status())


@ground_station_bp.route('/rotator/config', methods=['POST'])
def rotator_config():
    data = request.get_json(force=True) or {}
    host = str(data.get('host', '127.0.0.1'))
    port = int(data.get('port', 4533))
    from utils.rotator import get_rotator
    ok = get_rotator().connect(host, port)
    if not ok:
        return jsonify({'error': f'Could not connect to rotctld at {host}:{port}'}), 503
    return jsonify(get_rotator().get_status())


@ground_station_bp.route('/rotator/point', methods=['POST'])
def rotator_point():
    data = request.get_json(force=True) or {}
    try:
        az = float(data['az'])
        el = float(data['el'])
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({'error': f'az and el required: {e}'}), 400
    from utils.rotator import get_rotator
    ok = get_rotator().point_to(az, el)
    if not ok:
        return jsonify({'error': 'Rotator command failed'}), 503
    return jsonify({'status': 'ok', 'az': az, 'el': el})


@ground_station_bp.route('/rotator/park', methods=['POST'])
def rotator_park():
    from utils.rotator import get_rotator
    ok = get_rotator().park()
    if not ok:
        return jsonify({'error': 'Rotator park failed'}), 503
    return jsonify({'status': 'parked'})


@ground_station_bp.route('/rotator/disconnect', methods=['POST'])
def rotator_disconnect():
    from utils.rotator import get_rotator
    get_rotator().disconnect()
    return jsonify({'status': 'disconnected'})


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_profile(data: dict) -> None:
    if 'norad_id' not in data:
        raise ValueError("norad_id is required")
    if 'name' not in data:
        raise ValueError("name is required")
    if 'frequency_mhz' not in data:
        raise ValueError("frequency_mhz is required")
    try:
        norad_id = int(data['norad_id'])
        if norad_id <= 0:
            raise ValueError("norad_id must be positive")
    except (TypeError, ValueError):
        raise ValueError("norad_id must be a positive integer")
    try:
        freq = float(data['frequency_mhz'])
        if not (0.1 <= freq <= 3000.0):
            raise ValueError("frequency_mhz must be between 0.1 and 3000")
    except (TypeError, ValueError):
        raise ValueError("frequency_mhz must be a number between 0.1 and 3000")
    from utils.ground_station.observation_profile import VALID_TASK_TYPES

    valid_decoders = {'fm', 'afsk', 'gmsk', 'bpsk', 'iq_only'}
    if 'tasks' in data:
        if not isinstance(data['tasks'], list):
            raise ValueError("tasks must be a list")
        invalid = [
            str(task) for task in data['tasks']
            if str(task).strip().lower() not in VALID_TASK_TYPES
        ]
        if invalid:
            raise ValueError(
                f"tasks contains unsupported values: {', '.join(invalid)}"
            )
    else:
        dt = str(data.get('decoder_type', 'fm'))
        if dt not in valid_decoders:
            raise ValueError(f"decoder_type must be one of: {', '.join(sorted(valid_decoders))}")
