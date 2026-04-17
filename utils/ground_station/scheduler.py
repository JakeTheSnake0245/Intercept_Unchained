"""Ground station automated observation scheduler.

Watches enabled :class:`~utils.ground_station.observation_profile.ObservationProfile`
entries, predicts passes for each satellite, fires a capture at AOS, and
stops it at LOS.

During a capture:
* An :class:`~utils.ground_station.iq_bus.IQBus` claims the SDR device.
* Consumers are attached according to ``profile.decoder_type``:
  - ``'iq_only'``  → SigMFConsumer only (if ``record_iq`` is True).
  - ``'fm'``       → FMDemodConsumer (direwolf AX.25) + optional SigMF.
  - ``'afsk'``     → FMDemodConsumer (direwolf AX.25) + optional SigMF.
  - ``'gmsk'``     → FMDemodConsumer (multimon-ng) + optional SigMF.
  - ``'bpsk'``     → GrSatConsumer + optional SigMF.
* A WaterfallConsumer is always attached for the live spectrum panel.
* A Doppler correction thread retunes the IQ bus every 5 s if shift > threshold.
* A rotator control thread points the antenna (if rotctld is available).
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from utils.logging import get_logger

logger = get_logger('intercept.ground_station.scheduler')

# Env-configurable Doppler retune threshold (Hz)
try:
    from config import GS_DOPPLER_THRESHOLD_HZ  # type: ignore[import]
except (ImportError, AttributeError):
    import os
    GS_DOPPLER_THRESHOLD_HZ = int(os.environ.get('INTERCEPT_GS_DOPPLER_THRESHOLD_HZ', 500))

DOPPLER_INTERVAL_SECONDS = 5
SCHEDULE_REFRESH_MINUTES = 30
CAPTURE_BUFFER_SECONDS = 30


# ---------------------------------------------------------------------------
# Scheduled observation (state machine)
# ---------------------------------------------------------------------------

class ScheduledObservation:
    """A single scheduled pass for a profile."""

    def __init__(
        self,
        profile_norad_id: int,
        satellite_name: str,
        aos_iso: str,
        los_iso: str,
        max_el: float,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.profile_norad_id = profile_norad_id
        self.satellite_name = satellite_name
        self.aos_iso = aos_iso
        self.los_iso = los_iso
        self.max_el = max_el
        self.status: str = 'scheduled'
        self._start_timer: threading.Timer | None = None
        self._stop_timer: threading.Timer | None = None

    @property
    def aos_dt(self) -> datetime:
        return _parse_utc_iso(self.aos_iso)

    @property
    def los_dt(self) -> datetime:
        return _parse_utc_iso(self.los_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'norad_id': self.profile_norad_id,
            'satellite': self.satellite_name,
            'aos': self.aos_iso,
            'los': self.los_iso,
            'max_el': self.max_el,
            'status': self.status,
        }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class GroundStationScheduler:
    """Automated ground station observation scheduler."""

    def __init__(self):
        self._enabled = False
        self._lock = threading.Lock()
        self._observations: list[ScheduledObservation] = []
        self._refresh_timer: threading.Timer | None = None
        self._event_callback: Callable[[dict[str, Any]], None] | None = None

        # Active capture state
        self._active_obs: ScheduledObservation | None = None
        self._active_iq_bus = None          # IQBus instance
        self._active_waterfall_consumer = None
        self._doppler_thread: threading.Thread | None = None
        self._doppler_stop = threading.Event()
        self._active_profile = None         # ObservationProfile
        self._active_doppler_tracker = None  # DopplerTracker

        # Shared waterfall queue (consumed by /ws/satellite_waterfall)
        self.waterfall_queue: queue.Queue = queue.Queue(maxsize=120)

        # Observer location
        self._lat: float = 0.0
        self._lon: float = 0.0
        self._device: int = 0
        self._sdr_type: str = 'rtlsdr'

    # ------------------------------------------------------------------
    # Public control API
    # ------------------------------------------------------------------

    def set_event_callback(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        self._event_callback = callback

    def enable(
        self,
        lat: float,
        lon: float,
        device: int = 0,
        sdr_type: str = 'rtlsdr',
    ) -> dict[str, Any]:
        with self._lock:
            self._lat = lat
            self._lon = lon
            self._device = device
            self._sdr_type = sdr_type
            self._enabled = True
        self._refresh_schedule()
        return self.get_status()

    def disable(self) -> dict[str, Any]:
        with self._lock:
            self._enabled = False
            if self._refresh_timer:
                self._refresh_timer.cancel()
                self._refresh_timer = None
            for obs in self._observations:
                if obs._start_timer:
                    obs._start_timer.cancel()
                if obs._stop_timer:
                    obs._stop_timer.cancel()
            self._observations.clear()
        self._stop_active_capture(reason='scheduler_disabled')
        return {'status': 'disabled'}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active_obs.to_dict() if self._active_obs else None
            return {
                'enabled': self._enabled,
                'observer': {'latitude': self._lat, 'longitude': self._lon},
                'device': self._device,
                'sdr_type': self._sdr_type,
                'scheduled_count': sum(
                    1 for o in self._observations if o.status == 'scheduled'
                ),
                'total_observations': len(self._observations),
                'active_observation': active,
                'waterfall_active': self._active_iq_bus is not None
                    and self._active_iq_bus.running,
            }

    def get_scheduled_observations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [o.to_dict() for o in self._observations]

    def trigger_manual(self, norad_id: int) -> tuple[bool, str]:
        """Immediately start a manual observation for the given NORAD ID."""
        from utils.ground_station.observation_profile import get_profile
        profile = get_profile(norad_id)
        if not profile:
            return False, f'No observation profile for NORAD {norad_id}'
        obs = ScheduledObservation(
            profile_norad_id=norad_id,
            satellite_name=profile.name,
            aos_iso=datetime.now(timezone.utc).isoformat(),
            los_iso=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            max_el=90.0,
        )
        self._execute_observation(obs)
        return True, 'Manual observation started'

    def stop_active(self) -> dict[str, Any]:
        """Stop the currently running observation."""
        self._stop_active_capture(reason='manual_stop')
        return self.get_status()

    # ------------------------------------------------------------------
    # Schedule management
    # ------------------------------------------------------------------

    def _refresh_schedule(self) -> None:
        if not self._enabled:
            return

        from utils.ground_station.observation_profile import list_profiles

        profiles = [p for p in list_profiles() if p.enabled]
        if not profiles:
            logger.info("Ground station scheduler: no enabled profiles")
            self._arm_refresh_timer()
            return

        try:
            passes_by_profile = self._predict_passes_for_profiles(profiles)
        except Exception as e:
            logger.error(f"Ground station scheduler: pass prediction failed: {e}")
            self._arm_refresh_timer()
            return

        with self._lock:
            # Cancel existing scheduled timers (keep active/complete)
            for obs in self._observations:
                if obs.status == 'scheduled':
                    if obs._start_timer:
                        obs._start_timer.cancel()
                    if obs._stop_timer:
                        obs._stop_timer.cancel()

            history = [o for o in self._observations if o.status in ('complete', 'capturing', 'failed')]
            self._observations = history

            now = datetime.now(timezone.utc)
            buf = CAPTURE_BUFFER_SECONDS

            for obs in passes_by_profile:
                capture_start = obs.aos_dt - timedelta(seconds=buf)
                capture_end = obs.los_dt + timedelta(seconds=buf)

                if capture_end <= now:
                    continue
                if any(h.id == obs.id for h in history):
                    continue

                delay = max(0.0, (capture_start - now).total_seconds())
                obs._start_timer = threading.Timer(
                    delay, self._execute_observation, args=[obs]
                )
                obs._start_timer.daemon = True
                obs._start_timer.start()
                self._observations.append(obs)

            scheduled = sum(1 for o in self._observations if o.status == 'scheduled')
            logger.info(f"Ground station scheduler refreshed: {scheduled} observations scheduled")

        self._arm_refresh_timer()

    def _arm_refresh_timer(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.cancel()
        if not self._enabled:
            return
        self._refresh_timer = threading.Timer(
            SCHEDULE_REFRESH_MINUTES * 60, self._refresh_schedule
        )
        self._refresh_timer.daemon = True
        self._refresh_timer.start()

    def _predict_passes_for_profiles(
        self, profiles: list
    ) -> list[ScheduledObservation]:
        """Predict passes for each profile and return ScheduledObservation list."""
        from skyfield.api import load, wgs84

        from utils.satellite_predict import predict_passes as _predict_passes

        try:
            ts = load.timescale(builtin=True)
        except Exception:
            from skyfield.api import load as _load
            ts = _load.timescale(builtin=True)

        observer = wgs84.latlon(self._lat, self._lon)
        now = datetime.now(timezone.utc)
        import datetime as _dt
        t0 = ts.utc(now)
        t1 = ts.utc(now + _dt.timedelta(hours=24))

        observations: list[ScheduledObservation] = []

        for profile in profiles:
            tle = _find_tle_by_norad(profile.norad_id)
            if tle is None:
                logger.warning(
                    f"No TLE for NORAD {profile.norad_id} ({profile.name}) — skipping"
                )
                continue
            try:
                passes = _predict_passes(
                    tle_data=tle,
                    observer=observer,
                    ts=ts,
                    t0=t0,
                    t1=t1,
                    min_el=profile.min_elevation,
                    include_trajectory=False,
                    include_ground_track=False,
                )
            except Exception as e:
                logger.warning(f"Pass prediction failed for {profile.name}: {e}")
                continue

            for p in passes:
                obs = ScheduledObservation(
                    profile_norad_id=profile.norad_id,
                    satellite_name=profile.name,
                    aos_iso=p.get('startTimeISO', ''),
                    los_iso=p.get('endTimeISO', ''),
                    max_el=float(p.get('maxEl', 0.0)),
                )
                observations.append(obs)

        return observations

    # ------------------------------------------------------------------
    # Capture execution
    # ------------------------------------------------------------------

    def _execute_observation(self, obs: ScheduledObservation) -> None:
        """Called at AOS (+ buffer) to start IQ capture."""
        if not self._enabled:
            return
        if obs.status == 'scheduled':
            obs.status = 'capturing'
        else:
            return  # already cancelled / complete

        from utils.ground_station.observation_profile import get_profile
        profile = get_profile(obs.profile_norad_id)
        if not profile or not profile.enabled:
            obs.status = 'failed'
            return

        # Claim SDR device
        try:
            import app as _app
            err = _app.claim_sdr_device(self._device, 'ground_station_iq_bus', self._sdr_type)
            if err:
                logger.warning(f"Ground station: SDR busy — skipping {obs.satellite_name}: {err}")
                obs.status = 'failed'
                self._emit_event({'type': 'observation_skipped', 'observation': obs.to_dict(), 'reason': 'device_busy'})
                return
        except ImportError:
            pass

        # Create DB record
        obs_db_id = _insert_observation_record(obs, profile)

        # Build IQ bus
        from utils.ground_station.iq_bus import IQBus
        bus = IQBus(
            center_mhz=profile.frequency_mhz,
            sample_rate=profile.iq_sample_rate,
            gain=profile.gain,
            device_index=self._device,
            sdr_type=self._sdr_type,
        )

        # Attach waterfall consumer (always)
        from utils.ground_station.consumers.waterfall import WaterfallConsumer
        wf_consumer = WaterfallConsumer(output_queue=self.waterfall_queue)
        bus.add_consumer(wf_consumer)

        # Attach decoder consumers
        self._attach_decoder_consumers(bus, profile, obs_db_id, obs)

        # Attach SigMF consumer when explicitly requested or required by tasks
        if _profile_requires_iq_recording(profile):
            self._attach_sigmf_consumer(bus, profile, obs_db_id)

        # Start bus
        ok, err_msg = bus.start()
        if not ok:
            logger.error(f"Ground station: failed to start IQBus for {obs.satellite_name}: {err_msg}")
            obs.status = 'failed'
            try:
                import app as _app
                _app.release_sdr_device(self._device, self._sdr_type)
            except ImportError:
                pass
            self._emit_event({'type': 'observation_failed', 'observation': obs.to_dict(), 'reason': err_msg})
            return

        with self._lock:
            self._active_obs = obs
            self._active_iq_bus = bus
            self._active_waterfall_consumer = wf_consumer
            self._active_profile = profile

        # Emit iq_bus_started SSE event (used by Phase 5 waterfall)
        span_mhz = profile.iq_sample_rate / 1e6
        self._emit_event({
            'type': 'iq_bus_started',
            'observation': obs.to_dict(),
            'center_mhz': profile.frequency_mhz,
            'span_mhz': span_mhz,
        })
        self._emit_event({'type': 'observation_started', 'observation': obs.to_dict()})
        logger.info(f"Ground station: observation started for {obs.satellite_name} (NORAD {obs.profile_norad_id})")

        # Start Doppler correction thread
        self._start_doppler_thread(profile, obs)

        # Schedule stop at LOS + buffer
        now = datetime.now(timezone.utc)
        stop_delay = (obs.los_dt + timedelta(seconds=CAPTURE_BUFFER_SECONDS) - now).total_seconds()
        if stop_delay > 0:
            obs._stop_timer = threading.Timer(
                stop_delay, self._stop_active_capture, kwargs={'reason': 'los'}
            )
            obs._stop_timer.daemon = True
            obs._stop_timer.start()
        else:
            self._stop_active_capture(reason='los_immediate')

    def _stop_active_capture(self, *, reason: str = 'manual') -> None:
        """Stop the currently active capture and release the SDR device."""
        with self._lock:
            bus = self._active_iq_bus
            obs = self._active_obs
            self._active_iq_bus = None
            self._active_obs = None
            self._active_waterfall_consumer = None
            self._active_profile = None
            self._active_doppler_tracker = None

        self._doppler_stop.set()

        if bus and bus.running:
            bus.stop()

        if obs:
            obs.status = 'complete'
            _update_observation_status(obs, 'complete')
            self._emit_event({
                'type': 'observation_complete',
                'observation': obs.to_dict(),
                'reason': reason,
            })
            self._emit_event({'type': 'iq_bus_stopped', 'observation': obs.to_dict()})

        try:
            import app as _app
            _app.release_sdr_device(self._device, self._sdr_type)
        except ImportError:
            pass

        logger.info(f"Ground station: observation stopped ({reason})")

    # ------------------------------------------------------------------
    # Consumer attachment helpers
    # ------------------------------------------------------------------

    def _attach_decoder_consumers(self, bus, profile, obs_db_id: int | None, obs) -> None:
        """Attach consumers for all telemetry tasks on the profile."""
        import shutil

        tasks = _get_profile_tasks(profile)

        if 'telemetry_ax25' in tasks:
            if shutil.which('direwolf'):
                from utils.ground_station.consumers.fm_demod import FMDemodConsumer
                consumer = FMDemodConsumer(
                    decoder_cmd=[
                        'direwolf', '-r', '48000', '-n', '1', '-b', '16', '-',
                    ],
                    modulation='fm',
                    on_decoded=lambda line: self._on_packet_decoded(
                        line, obs_db_id, obs, source='direwolf'
                    ),
                )
                bus.add_consumer(consumer)
                logger.info("Ground station: attached direwolf AX.25 decoder")
            else:
                logger.warning("direwolf not found — AX.25 decoding disabled")

        if 'telemetry_gmsk' in tasks:
            if shutil.which('multimon-ng'):
                from utils.ground_station.consumers.fm_demod import FMDemodConsumer
                consumer = FMDemodConsumer(
                    decoder_cmd=['multimon-ng', '-t', 'raw', '-a', 'GMSK', '-'],
                    modulation='fm',
                    on_decoded=lambda line: self._on_packet_decoded(
                        line, obs_db_id, obs, source='multimon-ng'
                    ),
                )
                bus.add_consumer(consumer)
                logger.info("Ground station: attached multimon-ng GMSK decoder")
            else:
                logger.warning("multimon-ng not found — GMSK decoding disabled")

        if 'telemetry_bpsk' in tasks:
            from utils.ground_station.consumers.gr_satellites import GrSatConsumer
            consumer = GrSatConsumer(
                satellite_name=profile.name,
                on_decoded=lambda pkt: self._on_packet_decoded(
                    pkt,
                    obs_db_id,
                    obs,
                    source='gr_satellites',
                ),
            )
            bus.add_consumer(consumer)

    def _attach_sigmf_consumer(self, bus, profile, obs_db_id: int | None) -> None:
        """Attach a SigMFConsumer for raw IQ recording."""
        from utils.ground_station.consumers.sigmf_writer import SigMFConsumer
        from utils.sigmf import SigMFMetadata

        meta = SigMFMetadata(
            sample_rate=profile.iq_sample_rate,
            center_frequency_hz=profile.frequency_mhz * 1e6,
            satellite_name=profile.name,
            norad_id=profile.norad_id,
            latitude=self._lat,
            longitude=self._lon,
        )

        def _on_recording_complete(meta_path, data_path):
            _insert_recording_record(obs_db_id, meta_path, data_path, profile)
            self._emit_event({
                'type': 'recording_complete',
                'norad_id': profile.norad_id,
                'data_path': str(data_path),
                'meta_path': str(meta_path),
            })
            if 'weather_meteor_lrpt' in _get_profile_tasks(profile):
                try:
                    from utils.ground_station.meteor_backend import launch_meteor_decode
                    launch_meteor_decode(
                        obs_db_id=obs_db_id,
                        norad_id=profile.norad_id,
                        satellite_name=profile.name,
                        sample_rate=profile.iq_sample_rate,
                        data_path=Path(data_path),
                        emit_event=self._emit_event,
                        register_output=_insert_output_record,
                    )
                except Exception as e:
                    logger.warning(f"Failed to launch Meteor decode backend: {e}")
                    self._emit_event({
                        'type': 'weather_decode_failed',
                        'norad_id': profile.norad_id,
                        'satellite': profile.name,
                        'backend': 'meteor_lrpt',
                        'message': str(e),
                    })

        consumer = SigMFConsumer(metadata=meta, on_complete=_on_recording_complete)
        bus.add_consumer(consumer)
        logger.info(f"Ground station: SigMF recording enabled for {profile.name}")

    # ------------------------------------------------------------------
    # Doppler correction (Phase 2)
    # ------------------------------------------------------------------

    def _start_doppler_thread(self, profile, obs: ScheduledObservation) -> None:
        """Start the Doppler tracking/retune thread for an active capture."""
        from utils.doppler import DopplerTracker

        tle = _find_tle_by_norad(profile.norad_id)
        if tle is None:
            logger.info(f"Ground station: no TLE for {profile.name} — Doppler disabled")
            return

        tracker = DopplerTracker(satellite_name=profile.name, tle_data=tle)
        if not tracker.configure(self._lat, self._lon):
            logger.info(f"Ground station: Doppler tracking not available for {profile.name}")
            return

        with self._lock:
            self._active_doppler_tracker = tracker

        self._doppler_stop.clear()
        t = threading.Thread(
            target=self._doppler_loop,
            args=[profile, tracker],
            daemon=True,
            name='gs-doppler',
        )
        t.start()
        self._doppler_thread = t
        logger.info(f"Ground station: Doppler tracking started for {profile.name}")

    def _doppler_loop(self, profile, tracker) -> None:
        """Periodically compute Doppler shift and retune if necessary."""
        while not self._doppler_stop.wait(DOPPLER_INTERVAL_SECONDS):
            with self._lock:
                bus = self._active_iq_bus

            if bus is None or not bus.running:
                break

            info = tracker.calculate(profile.frequency_mhz)
            if info is None:
                continue

            # Retune if shift exceeds threshold
            if abs(info.shift_hz) >= GS_DOPPLER_THRESHOLD_HZ:
                corrected_mhz = info.frequency_hz / 1_000_000
                logger.info(
                    f"Ground station: Doppler retune {info.shift_hz:+.1f} Hz → "
                    f"{corrected_mhz:.6f} MHz (el={info.elevation:.1f}°)"
                )
                bus.retune(corrected_mhz)
                self._emit_event({
                    'type': 'doppler_update',
                    'norad_id': profile.norad_id,
                    **info.to_dict(),
                })

            # Rotator control (Phase 6)
            try:
                from utils.rotator import get_rotator
                rotator = get_rotator()
                if rotator.enabled:
                    rotator.point_to(info.azimuth, info.elevation)
            except Exception:
                pass

        logger.debug("Ground station: Doppler loop exited")

    # ------------------------------------------------------------------
    # Packet / event callbacks
    # ------------------------------------------------------------------

    def _on_packet_decoded(
        self,
        payload,
        obs_db_id: int | None,
        obs: ScheduledObservation,
        *,
        source: str = 'decoder',
    ) -> None:
        """Handle a decoded packet payload from a decoder consumer."""
        if payload is None or payload == '':
            return

        packet_event = _build_packet_event(payload, source)
        _insert_event_record(obs_db_id, 'packet', json.dumps(packet_event))
        self._emit_event({
            'type': 'packet_decoded',
            'norad_id': obs.profile_norad_id,
            'satellite': obs.satellite_name,
            **packet_event,
        })

    def _emit_event(self, event: dict[str, Any]) -> None:
        if self._event_callback:
            try:
                self._event_callback(event)
            except Exception as e:
                logger.debug(f"Event callback error: {e}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _insert_observation_record(obs: ScheduledObservation, profile) -> int | None:
    try:
        from datetime import datetime, timezone

        from utils.database import get_db
        with get_db() as conn:
            cur = conn.execute('''
                INSERT INTO ground_station_observations
                    (profile_id, norad_id, satellite, aos_time, los_time, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                profile.id,
                obs.profile_norad_id,
                obs.satellite_name,
                obs.aos_iso,
                obs.los_iso,
                'capturing',
                datetime.now(timezone.utc).isoformat(),
            ))
            return cur.lastrowid
    except Exception as e:
        logger.warning(f"Failed to insert observation record: {e}")
        return None


def _update_observation_status(obs: ScheduledObservation, status: str) -> None:
    try:
        from utils.database import get_db
        with get_db() as conn:
            conn.execute(
                'UPDATE ground_station_observations SET status=? WHERE norad_id=? AND status=?',
                (status, obs.profile_norad_id, 'capturing'),
            )
    except Exception as e:
        logger.debug(f"Failed to update observation status: {e}")


def _insert_event_record(obs_db_id: int | None, event_type: str, payload: str) -> None:
    if obs_db_id is None:
        return
    try:
        from datetime import datetime, timezone

        from utils.database import get_db
        with get_db() as conn:
            conn.execute('''
                INSERT INTO ground_station_events (observation_id, event_type, payload_json, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (obs_db_id, event_type, payload, datetime.now(timezone.utc).isoformat()))
    except Exception as e:
        logger.debug(f"Failed to insert event record: {e}")


def _get_profile_tasks(profile) -> list[str]:
    get_tasks = getattr(profile, 'get_tasks', None)
    if callable(get_tasks):
        return get_tasks()
    return []


def _profile_requires_iq_recording(profile) -> bool:
    tasks = _get_profile_tasks(profile)
    return bool(getattr(profile, 'record_iq', False) or 'record_iq' in tasks or 'weather_meteor_lrpt' in tasks)


def _build_packet_event(payload, source: str) -> dict[str, Any]:
    event: dict[str, Any] = {
        'source': source,
        'data': payload if isinstance(payload, str) else json.dumps(payload),
        'parsed': None,
    }

    if isinstance(payload, dict):
        event['parsed'] = payload
        event['protocol'] = payload.get('protocol') or payload.get('type') or source
        return event

    text = str(payload).strip()
    event['data'] = text

    parsed = None
    if source == 'gr_satellites':
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = None

    if parsed is None:
        try:
            import base64

            from utils.satellite_telemetry import auto_parse

            for token in text.replace(',', ' ').split():
                cleaned = token.strip()
                if not cleaned or len(cleaned) < 8:
                    continue
                try:
                    raw = base64.b64decode(cleaned, validate=True)
                except Exception:
                    continue
                maybe = auto_parse(raw)
                if maybe:
                    parsed = maybe
                    break
        except Exception:
            parsed = None

    event['parsed'] = parsed
    if isinstance(parsed, dict):
        event['protocol'] = parsed.get('protocol') or source
    return event


def _insert_recording_record(obs_db_id: int | None, meta_path: Path, data_path: Path, profile) -> None:
    try:
        from datetime import datetime, timezone

        from utils.database import get_db
        size = data_path.stat().st_size if data_path.exists() else 0
        with get_db() as conn:
            conn.execute('''
                INSERT INTO sigmf_recordings
                    (observation_id, sigmf_data_path, sigmf_meta_path, size_bytes,
                     sample_rate, center_freq_hz, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                obs_db_id,
                str(data_path),
                str(meta_path),
                size,
                profile.iq_sample_rate,
                int(profile.frequency_mhz * 1e6),
                datetime.now(timezone.utc).isoformat(),
            ))
    except Exception as e:
        logger.warning(f"Failed to insert recording record: {e}")


def _insert_output_record(
    *,
    observation_id: int | None,
    norad_id: int | None,
    output_type: str,
    backend: str,
    file_path: Path,
    preview_path: Path | None = None,
    metadata: dict[str, Any] | None = None,
) -> int | None:
    try:
        from datetime import datetime, timezone

        from utils.database import get_db

        with get_db() as conn:
            cur = conn.execute(
                '''
                INSERT INTO ground_station_outputs
                    (observation_id, norad_id, output_type, backend, file_path,
                     preview_path, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    observation_id,
                    norad_id,
                    output_type,
                    backend,
                    str(file_path),
                    str(preview_path) if preview_path else None,
                    json.dumps(metadata or {}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.lastrowid
    except Exception as e:
        logger.warning(f"Failed to insert output record: {e}")
        return None


# ---------------------------------------------------------------------------
# TLE lookup helpers
# ---------------------------------------------------------------------------


def _find_tle_by_norad(norad_id: int) -> tuple[str, str, str] | None:
    """Search TLE cache for a given NORAD catalog number."""
    # Try live cache first
    sources = []
    try:
        from routes.satellite import _tle_cache  # type: ignore[import]
        if _tle_cache:
            sources.append(_tle_cache)
    except (ImportError, AttributeError):
        pass
    try:
        from data.satellites import TLE_SATELLITES
        sources.append(TLE_SATELLITES)
    except ImportError:
        pass

    target_id = str(norad_id).zfill(5)

    for source in sources:
        for _key, tle in source.items():
            if not isinstance(tle, (tuple, list)) or len(tle) < 3:
                continue
            line1 = str(tle[1])
            # NORAD catalog number occupies chars 2-6 (0-indexed) of TLE line 1
            if len(line1) > 7:
                catalog_str = line1[2:7].strip()
                if catalog_str == target_id:
                    return (str(tle[0]), str(tle[1]), str(tle[2]))

    return None


# ---------------------------------------------------------------------------
# Timestamp parser (mirrors weather_sat_scheduler)
# ---------------------------------------------------------------------------


def _parse_utc_iso(value: str) -> datetime:
    text = str(value).strip().replace('+00:00Z', 'Z')
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_scheduler: GroundStationScheduler | None = None
_scheduler_lock = threading.Lock()


def get_ground_station_scheduler() -> GroundStationScheduler:
    """Get or create the global ground station scheduler."""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = GroundStationScheduler()
    return _scheduler
