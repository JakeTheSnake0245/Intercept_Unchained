"""Generalised Doppler shift calculator for satellite observations.

Extracted from utils/sstv/sstv_decoder.py and generalised to accept any
satellite by name (looked up in the live TLE cache) or by raw TLE tuple.

The sstv_decoder module imports DopplerTracker and DopplerInfo from here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from utils.logging import get_logger

logger = get_logger('intercept.doppler')

# Speed of light in m/s
SPEED_OF_LIGHT = 299_792_458.0

# Default Hz threshold before triggering a retune
DEFAULT_RETUNE_THRESHOLD_HZ = 500


@dataclass
class DopplerInfo:
    """Doppler shift information for a satellite observation."""

    frequency_hz: float
    shift_hz: float
    range_rate_km_s: float
    elevation: float
    azimuth: float
    timestamp: datetime

    def to_dict(self) -> dict:
        return {
            'frequency_hz': self.frequency_hz,
            'shift_hz': round(self.shift_hz, 1),
            'range_rate_km_s': round(self.range_rate_km_s, 3),
            'elevation': round(self.elevation, 1),
            'azimuth': round(self.azimuth, 1),
            'timestamp': self.timestamp.isoformat(),
        }


class DopplerTracker:
    """Real-time Doppler shift calculator for satellite tracking.

    Accepts a satellite by name (looked up in the live TLE cache, falling
    back to static data) **or** a raw TLE tuple ``(name, line1, line2)``
    passed via the constructor or via :meth:`update_tle`.
    """

    def __init__(
        self,
        satellite_name: str = 'ISS',
        tle_data: tuple[str, str, str] | None = None,
    ):
        self._satellite_name = satellite_name
        self._tle_data = tle_data
        self._observer_lat: float | None = None
        self._observer_lon: float | None = None
        self._satellite = None
        self._observer = None
        self._ts = None
        self._enabled = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def configure(self, latitude: float, longitude: float) -> bool:
        """Configure the tracker with an observer location.

        Resolves TLE data, builds the skyfield objects, and marks the
        tracker enabled.  Returns True on success.
        """
        try:
            from skyfield.api import EarthSatellite, load, wgs84
        except ImportError:
            logger.warning("skyfield not available — Doppler tracking disabled")
            return False

        tle = self._resolve_tle()
        if tle is None:
            logger.error(f"No TLE data for satellite: {self._satellite_name}")
            return False

        try:
            ts = load.timescale(builtin=True)
            satellite = EarthSatellite(tle[1], tle[2], tle[0], ts)
            observer = wgs84.latlon(latitude, longitude)
        except Exception as e:
            logger.error(f"Failed to configure DopplerTracker: {e}")
            return False

        with self._lock:
            self._ts = ts
            self._satellite = satellite
            self._observer = observer
            self._observer_lat = latitude
            self._observer_lon = longitude
            self._enabled = True

        logger.info(
            f"DopplerTracker configured for {self._satellite_name} "
            f"at ({latitude}, {longitude})"
        )
        return True

    def update_tle(self, tle_data: tuple[str, str, str]) -> bool:
        """Update TLE data and re-configure if already enabled."""
        self._tle_data = tle_data
        if (
            self._enabled
            and self._observer_lat is not None
            and self._observer_lon is not None
        ):
            return self.configure(self._observer_lat, self._observer_lon)
        return True

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def calculate(self, nominal_freq_mhz: float) -> DopplerInfo | None:
        """Calculate the Doppler-corrected receive frequency.

        Returns a :class:`DopplerInfo` or *None* if the tracker is not
        enabled or the calculation fails.
        """
        with self._lock:
            if not self._enabled or self._satellite is None or self._observer is None:
                return None
            ts = self._ts
            satellite = self._satellite
            observer = self._observer

        try:
            t = ts.now()
            difference = satellite - observer
            topocentric = difference.at(t)
            alt, az, distance = topocentric.altaz()

            dt_seconds = 1.0
            t_future = ts.utc(t.utc_datetime() + timedelta(seconds=dt_seconds))
            topocentric_future = difference.at(t_future)
            _, _, distance_future = topocentric_future.altaz()

            range_rate_km_s = (distance_future.km - distance.km) / dt_seconds
            nominal_freq_hz = nominal_freq_mhz * 1_000_000
            doppler_factor = 1.0 - (range_rate_km_s * 1000.0 / SPEED_OF_LIGHT)
            corrected_freq_hz = nominal_freq_hz * doppler_factor
            shift_hz = corrected_freq_hz - nominal_freq_hz

            return DopplerInfo(
                frequency_hz=corrected_freq_hz,
                shift_hz=shift_hz,
                range_rate_km_s=range_rate_km_s,
                elevation=alt.degrees,
                azimuth=az.degrees,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.error(f"Doppler calculation failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_tle(self) -> tuple[str, str, str] | None:
        """Return the best available TLE tuple."""
        if self._tle_data:
            return self._tle_data

        # Try the live TLE cache maintained by routes/satellite.py
        try:
            from routes.satellite import _tle_cache  # type: ignore[import]
            if _tle_cache:
                tle = _tle_cache.get(self._satellite_name)
                if tle:
                    return tle
        except (ImportError, AttributeError):
            pass

        # Fall back to static bundled data
        try:
            from data.satellites import TLE_SATELLITES
            return TLE_SATELLITES.get(self._satellite_name)
        except ImportError:
            return None
