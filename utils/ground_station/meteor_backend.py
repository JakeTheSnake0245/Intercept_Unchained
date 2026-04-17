"""Meteor LRPT offline decode backend for ground-station observations."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from utils.logging import get_logger
from utils.weather_sat import WeatherSatDecoder

logger = get_logger('intercept.ground_station.meteor_backend')

OUTPUT_ROOT = Path('instance/ground_station/weather_outputs')
DECODE_TIMEOUT_SECONDS = 30 * 60

_NORAD_TO_SAT_KEY = {
    57166: 'METEOR-M2-3',
    59051: 'METEOR-M2-4',
}


def resolve_meteor_satellite_key(norad_id: int, satellite_name: str) -> str | None:
    if norad_id in _NORAD_TO_SAT_KEY:
        return _NORAD_TO_SAT_KEY[norad_id]

    upper = str(satellite_name or '').upper()
    if 'M2-4' in upper:
        return 'METEOR-M2-4'
    if 'M2-3' in upper or 'METEOR' in upper:
        return 'METEOR-M2-3'
    return None


def launch_meteor_decode(
    *,
    obs_db_id: int | None,
    norad_id: int,
    satellite_name: str,
    sample_rate: int,
    data_path: Path,
    emit_event,
    register_output,
) -> None:
    """Run Meteor LRPT offline decode in a background thread."""
    decode_job_id = _create_decode_job(
        observation_id=obs_db_id,
        norad_id=norad_id,
        backend='meteor_lrpt',
        input_path=data_path,
    )
    emit_event({
        'type': 'weather_decode_queued',
        'decode_job_id': decode_job_id,
        'norad_id': norad_id,
        'satellite': satellite_name,
        'backend': 'meteor_lrpt',
        'input_path': str(data_path),
    })
    t = threading.Thread(
        target=_run_decode,
        kwargs={
            'decode_job_id': decode_job_id,
            'obs_db_id': obs_db_id,
            'norad_id': norad_id,
            'satellite_name': satellite_name,
            'sample_rate': sample_rate,
            'data_path': data_path,
            'emit_event': emit_event,
            'register_output': register_output,
        },
        daemon=True,
        name=f'gs-meteor-decode-{norad_id}',
    )
    t.start()


def _run_decode(
    *,
    decode_job_id: int | None,
    obs_db_id: int | None,
    norad_id: int,
    satellite_name: str,
    sample_rate: int,
    data_path: Path,
    emit_event,
    register_output,
) -> None:
    latest_status: dict[str, str | int | None] = {
        'message': None,
        'status': None,
        'phase': None,
    }
    sat_key = resolve_meteor_satellite_key(norad_id, satellite_name)
    if not sat_key:
        _update_decode_job(
            decode_job_id,
            status='failed',
            error_message='No Meteor satellite mapping is available for this observation.',
            details={'reason': 'unknown_satellite_mapping'},
            completed=True,
        )
        emit_event({
            'type': 'weather_decode_failed',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            'message': 'No Meteor satellite mapping is available for this observation.',
        })
        return

    output_dir = OUTPUT_ROOT / f'{norad_id}_{int(time.time())}'
    decoder = WeatherSatDecoder(output_dir=output_dir)
    if decoder.decoder_available is None:
        _update_decode_job(
            decode_job_id,
            status='failed',
            error_message='SatDump backend is not available for Meteor LRPT decode.',
            details={'reason': 'backend_unavailable', 'output_dir': str(output_dir)},
            completed=True,
        )
        emit_event({
            'type': 'weather_decode_failed',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            'message': 'SatDump backend is not available for Meteor LRPT decode.',
        })
        return

    def _progress_cb(progress):
        latest_status['message'] = progress.message or latest_status.get('message')
        latest_status['status'] = progress.status
        latest_status['phase'] = progress.capture_phase or latest_status.get('phase')
        progress_event = progress.to_dict()
        progress_event.pop('type', None)
        emit_event({
            'type': 'weather_decode_progress',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            **progress_event,
        })

    decoder.set_callback(_progress_cb)
    _update_decode_job(
        decode_job_id,
        status='decoding',
        output_dir=output_dir,
        details={
            'sample_rate': sample_rate,
            'input_path': str(data_path),
            'satellite': satellite_name,
        },
        started=True,
    )
    emit_event({
        'type': 'weather_decode_started',
        'decode_job_id': decode_job_id,
        'norad_id': norad_id,
        'satellite': satellite_name,
        'backend': 'meteor_lrpt',
        'input_path': str(data_path),
    })

    ok, error = decoder.start_from_file(
        satellite=sat_key,
        input_file=data_path,
        sample_rate=sample_rate,
    )
    if not ok:
        details = _build_failure_details(
            data_path=data_path,
            sample_rate=sample_rate,
            decoder=decoder,
            latest_status=latest_status,
        )
        emit_event({
            'type': 'weather_decode_failed',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            'message': error or details['message'],
            'failure_reason': details['reason'],
            'details': details,
        })
        _update_decode_job(
            decode_job_id,
            status='failed',
            error_message=error or details['message'],
            details=details,
            completed=True,
        )
        return

    started = time.time()
    while decoder.is_running and (time.time() - started) < DECODE_TIMEOUT_SECONDS:
        time.sleep(1.0)

    if decoder.is_running:
        decoder.stop()
        details = _build_failure_details(
            data_path=data_path,
            sample_rate=sample_rate,
            decoder=decoder,
            latest_status=latest_status,
            override_reason='timeout',
            override_message='Meteor decode timed out.',
        )
        emit_event({
            'type': 'weather_decode_failed',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            'message': details['message'],
            'failure_reason': details['reason'],
            'details': details,
        })
        _update_decode_job(
            decode_job_id,
            status='failed',
            error_message=details['message'],
            details=details,
            completed=True,
        )
        return

    images = decoder.get_images()
    if not images:
        details = _build_failure_details(
            data_path=data_path,
            sample_rate=sample_rate,
            decoder=decoder,
            latest_status=latest_status,
        )
        emit_event({
            'type': 'weather_decode_failed',
            'decode_job_id': decode_job_id,
            'norad_id': norad_id,
            'satellite': satellite_name,
            'backend': 'meteor_lrpt',
            'message': details['message'],
            'failure_reason': details['reason'],
            'details': details,
        })
        _update_decode_job(
            decode_job_id,
            status='failed',
            error_message=details['message'],
            details=details,
            completed=True,
        )
        return

    outputs = []
    for image in images:
        metadata = {
            'satellite': image.satellite,
            'mode': image.mode,
            'frequency': image.frequency,
            'product': image.product,
            'timestamp': image.timestamp.isoformat(),
            'size_bytes': image.size_bytes,
        }
        output_id = register_output(
            observation_id=obs_db_id,
            norad_id=norad_id,
            output_type='image',
            backend='meteor_lrpt',
            file_path=image.path,
            preview_path=image.path,
            metadata=metadata,
        )
        outputs.append({
            'id': output_id,
            'file_path': str(image.path),
            'filename': image.filename,
            'product': image.product,
        })

    completion_details = {
        'sample_rate': sample_rate,
        'input_path': str(data_path),
        'output_dir': str(output_dir),
        'output_count': len(outputs),
    }
    _update_decode_job(
        decode_job_id,
        status='complete',
        details=completion_details,
        completed=True,
    )

    emit_event({
        'type': 'weather_decode_complete',
        'decode_job_id': decode_job_id,
        'norad_id': norad_id,
        'satellite': satellite_name,
        'backend': 'meteor_lrpt',
        'outputs': outputs,
    })


def _build_failure_details(
    *,
    data_path: Path,
    sample_rate: int,
    decoder: WeatherSatDecoder,
    latest_status: dict[str, str | int | None],
    override_reason: str | None = None,
    override_message: str | None = None,
) -> dict[str, str | int | None]:
    file_size = data_path.stat().st_size if data_path.exists() else 0
    status = decoder.get_status()
    last_error = str(status.get('last_error') or latest_status.get('message') or '').strip()
    return_code = status.get('last_returncode')

    if override_reason:
        reason = override_reason
    elif sample_rate < 200_000:
        reason = 'sample_rate_too_low'
    elif not data_path.exists():
        reason = 'missing_recording'
    elif file_size < 5_000_000:
        reason = 'recording_too_small'
    elif return_code not in (None, 0):
        reason = 'satdump_failed'
    elif 'samplerate' in last_error.lower() or 'sample rate' in last_error.lower():
        reason = 'invalid_sample_rate'
    elif 'not found' in last_error.lower():
        reason = 'input_missing'
    elif 'permission' in last_error.lower():
        reason = 'permission_error'
    else:
        reason = 'no_imagery_produced'

    if override_message:
        message = override_message
    elif reason == 'sample_rate_too_low':
        message = f'Sample rate {sample_rate} Hz is too low for Meteor LRPT decoding.'
    elif reason == 'missing_recording':
        message = 'The recording file was not found when decode started.'
    elif reason == 'recording_too_small':
        message = (
            f'Recording is very small ({_format_bytes(file_size)}); this usually means the pass '
            'ended early or little usable IQ was captured.'
        )
    elif reason == 'satdump_failed':
        message = last_error or f'SatDump exited with code {return_code}.'
    elif reason == 'invalid_sample_rate':
        message = last_error or 'SatDump rejected the recording sample rate.'
    elif reason == 'input_missing':
        message = last_error or 'Input recording was not accessible to the decoder.'
    elif reason == 'permission_error':
        message = last_error or 'Decoder could not access the recording or output path.'
    else:
        message = (
            last_error or
            'Decode completed without any image outputs. This usually indicates weak signal, '
            'incorrect sample rate, or a SatDump pipeline mismatch.'
        )

    return {
        'reason': reason,
        'message': message,
        'sample_rate': sample_rate,
        'file_size_bytes': file_size,
        'file_size_human': _format_bytes(file_size),
        'last_error': last_error or None,
        'last_returncode': return_code,
        'capture_phase': status.get('capture_phase') or latest_status.get('phase'),
        'input_path': str(data_path),
    }


def _format_bytes(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f'{size_bytes} B'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    if size_bytes < 1024 * 1024 * 1024:
        return f'{size_bytes / (1024 * 1024):.1f} MB'
    return f'{size_bytes / (1024 * 1024 * 1024):.2f} GB'


def _create_decode_job(
    *,
    observation_id: int | None,
    norad_id: int,
    backend: str,
    input_path: Path,
) -> int | None:
    try:
        from utils.database import get_db

        with get_db() as conn:
            cur = conn.execute(
                '''
                INSERT INTO ground_station_decode_jobs
                    (observation_id, norad_id, backend, status, input_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    observation_id,
                    norad_id,
                    backend,
                    'queued',
                    str(input_path),
                    _utcnow_iso(),
                    _utcnow_iso(),
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.warning("Failed to create decode job: %s", e)
        return None


def _update_decode_job(
    decode_job_id: int | None,
    *,
    status: str,
    output_dir: Path | None = None,
    error_message: str | None = None,
    details: dict | None = None,
    started: bool = False,
    completed: bool = False,
) -> None:
    if decode_job_id is None:
        return
    try:
        from utils.database import get_db

        fields = ['status = ?', 'updated_at = ?']
        values: list[object] = [status, _utcnow_iso()]

        if output_dir is not None:
            fields.append('output_dir = ?')
            values.append(str(output_dir))
        if error_message is not None:
            fields.append('error_message = ?')
            values.append(error_message)
        if details is not None:
            fields.append('details_json = ?')
            values.append(json.dumps(details))
        if started:
            fields.append('started_at = ?')
            values.append(_utcnow_iso())
        if completed:
            fields.append('completed_at = ?')
            values.append(_utcnow_iso())

        values.append(decode_job_id)
        with get_db() as conn:
            conn.execute(
                f'''
                UPDATE ground_station_decode_jobs
                SET {", ".join(fields)}
                WHERE id = ?
                ''',
                values,
            )
    except Exception as e:
        logger.warning("Failed to update decode job %s: %s", decode_job_id, e)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
