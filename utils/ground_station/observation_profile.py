"""Observation profile dataclass and DB CRUD.

An ObservationProfile describes *how* to capture a particular satellite:
frequency, decoder type, gain, bandwidth, minimum elevation, and whether
to record raw IQ in SigMF format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from utils.logging import get_logger

logger = get_logger('intercept.ground_station.profile')


VALID_TASK_TYPES = {
    'telemetry_ax25',
    'telemetry_gmsk',
    'telemetry_bpsk',
    'weather_meteor_lrpt',
    'record_iq',
}


def legacy_decoder_to_tasks(decoder_type: str | None, record_iq: bool = False) -> list[str]:
    decoder = (decoder_type or 'fm').lower()
    tasks: list[str] = []
    if decoder in ('fm', 'afsk'):
        tasks.append('telemetry_ax25')
    elif decoder == 'gmsk':
        tasks.append('telemetry_gmsk')
    elif decoder == 'bpsk':
        tasks.append('telemetry_bpsk')
    elif decoder == 'iq_only':
        tasks.append('record_iq')

    if record_iq and 'record_iq' not in tasks:
        tasks.append('record_iq')
    return tasks


def tasks_to_legacy_decoder(tasks: list[str]) -> str:
    normalized = normalize_tasks(tasks)
    if 'telemetry_bpsk' in normalized:
        return 'bpsk'
    if 'telemetry_gmsk' in normalized:
        return 'gmsk'
    if 'telemetry_ax25' in normalized:
        return 'afsk'
    return 'iq_only'


def normalize_tasks(tasks: list[str] | None) -> list[str]:
    result: list[str] = []
    for task in tasks or []:
        value = str(task or '').strip().lower()
        if value and value in VALID_TASK_TYPES and value not in result:
            result.append(value)
    return result


@dataclass
class ObservationProfile:
    """Per-satellite capture configuration."""

    norad_id: int
    name: str                          # Human-readable label
    frequency_mhz: float
    decoder_type: str                  # 'fm', 'afsk', 'bpsk', 'gmsk', 'iq_only'
    gain: float = 40.0
    bandwidth_hz: int = 200_000
    min_elevation: float = 10.0
    enabled: bool = True
    record_iq: bool = False
    iq_sample_rate: int = 2_400_000
    tasks: list[str] = field(default_factory=list)
    id: int | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        normalized_tasks = self.get_tasks()
        return {
            'id': self.id,
            'norad_id': self.norad_id,
            'name': self.name,
            'frequency_mhz': self.frequency_mhz,
            'decoder_type': self.decoder_type,
            'legacy_decoder_type': self.decoder_type,
            'gain': self.gain,
            'bandwidth_hz': self.bandwidth_hz,
            'min_elevation': self.min_elevation,
            'enabled': self.enabled,
            'record_iq': self.record_iq,
            'iq_sample_rate': self.iq_sample_rate,
            'tasks': normalized_tasks,
            'created_at': self.created_at,
        }

    def get_tasks(self) -> list[str]:
        tasks = normalize_tasks(self.tasks)
        if not tasks:
            tasks = legacy_decoder_to_tasks(self.decoder_type, self.record_iq)
        if self.record_iq and 'record_iq' not in tasks:
            tasks.append('record_iq')
        if 'weather_meteor_lrpt' in tasks and 'record_iq' not in tasks:
            tasks.append('record_iq')
        return tasks

    @classmethod
    def from_row(cls, row) -> ObservationProfile:
        tasks = []
        raw_tasks = row.get('tasks_json', None)
        if raw_tasks:
            try:
                tasks = normalize_tasks(json.loads(raw_tasks))
            except (TypeError, ValueError, json.JSONDecodeError):
                tasks = []
        return cls(
            id=row['id'],
            norad_id=row['norad_id'],
            name=row['name'],
            frequency_mhz=row['frequency_mhz'],
            decoder_type=row['decoder_type'],
            gain=row['gain'],
            bandwidth_hz=row['bandwidth_hz'],
            min_elevation=row['min_elevation'],
            enabled=bool(row['enabled']),
            record_iq=bool(row['record_iq']),
            iq_sample_rate=row['iq_sample_rate'],
            tasks=tasks,
            created_at=row['created_at'],
        )


# ---------------------------------------------------------------------------
# DB CRUD
# ---------------------------------------------------------------------------


def list_profiles() -> list[ObservationProfile]:
    """Return all observation profiles from the database."""
    from utils.database import get_db
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM observation_profiles ORDER BY created_at DESC'
        ).fetchall()
    return [ObservationProfile.from_row(r) for r in rows]


def get_profile(norad_id: int) -> ObservationProfile | None:
    """Return the profile for a NORAD ID, or None if not found."""
    from utils.database import get_db
    with get_db() as conn:
        row = conn.execute(
            'SELECT * FROM observation_profiles WHERE norad_id = ?', (norad_id,)
        ).fetchone()
    return ObservationProfile.from_row(row) if row else None


def save_profile(profile: ObservationProfile) -> ObservationProfile:
    """Insert or replace an observation profile.  Returns the saved profile."""
    from utils.database import get_db
    normalized_tasks = profile.get_tasks()
    profile.tasks = normalized_tasks
    profile.record_iq = 'record_iq' in normalized_tasks
    profile.decoder_type = tasks_to_legacy_decoder(normalized_tasks)
    with get_db() as conn:
        conn.execute('''
            INSERT INTO observation_profiles
                (norad_id, name, frequency_mhz, decoder_type, tasks_json, gain,
                 bandwidth_hz, min_elevation, enabled, record_iq,
                 iq_sample_rate, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(norad_id) DO UPDATE SET
                name=excluded.name,
                frequency_mhz=excluded.frequency_mhz,
                decoder_type=excluded.decoder_type,
                tasks_json=excluded.tasks_json,
                gain=excluded.gain,
                bandwidth_hz=excluded.bandwidth_hz,
                min_elevation=excluded.min_elevation,
                enabled=excluded.enabled,
                record_iq=excluded.record_iq,
                iq_sample_rate=excluded.iq_sample_rate
        ''', (
            profile.norad_id,
            profile.name,
            profile.frequency_mhz,
            profile.decoder_type,
            json.dumps(normalized_tasks),
            profile.gain,
            profile.bandwidth_hz,
            profile.min_elevation,
            int(profile.enabled),
            int(profile.record_iq),
            profile.iq_sample_rate,
            profile.created_at,
        ))
    return get_profile(profile.norad_id) or profile


def delete_profile(norad_id: int) -> bool:
    """Delete a profile by NORAD ID.  Returns True if a row was deleted."""
    from utils.database import get_db
    with get_db() as conn:
        cur = conn.execute(
            'DELETE FROM observation_profiles WHERE norad_id = ?', (norad_id,)
        )
    return cur.rowcount > 0
