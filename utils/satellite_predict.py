"""Shared satellite pass prediction utility.

Used by both the satellite tracking dashboard and the weather satellite scheduler.
Uses Skyfield's find_events() for accurate AOS/TCA/LOS event detection.
"""

from __future__ import annotations

from typing import Any

from utils.logging import get_logger

logger = get_logger('intercept.satellite_predict')


def predict_passes(
    tle_data: tuple,
    observer,        # skyfield wgs84.latlon object
    ts,              # skyfield timescale
    t0,              # skyfield Time start
    t1,              # skyfield Time end
    min_el: float = 10.0,
    include_trajectory: bool = True,
    include_ground_track: bool = True,
) -> list[dict[str, Any]]:
    """Predict satellite passes over an observer location.

    Args:
        tle_data: (name, line1, line2) tuple
        observer: Skyfield wgs84.latlon observer
        ts: Skyfield timescale
        t0: Start time (Skyfield Time)
        t1: End time (Skyfield Time)
        min_el: Minimum peak elevation in degrees to include pass
        include_trajectory: Include 30-point az/el trajectory for polar plot
        include_ground_track: Include 60-point lat/lon ground track for map

    Returns:
        List of pass dicts sorted by AOS time. Each dict contains:
            aosTime, aosAz, aosEl,
            tcaTime, tcaEl, tcaAz,
            losTime, losAz, losEl,
            duration (minutes, float),
            startTime (human-readable UTC),
            startTimeISO (ISO string),
            endTimeISO (ISO string),
            maxEl (float, same as tcaEl),
            trajectory (list of {az, el} if include_trajectory),
            groundTrack (list of {lat, lon} if include_ground_track)
    """
    from skyfield.api import EarthSatellite, wgs84

    # Filter decaying satellites by checking ndot from TLE line1 chars 33-43
    try:
        line1 = tle_data[1]
        ndot_str = line1[33:43].strip()
        ndot = float(ndot_str)
        if abs(ndot) > 0.01:
            logger.debug(
                'Skipping decaying satellite %s (ndot=%s)', tle_data[0], ndot
            )
            return []
    except (ValueError, IndexError):
        # Don't skip on parse error
        pass

    # Create EarthSatellite object
    try:
        satellite = EarthSatellite(tle_data[1], tle_data[2], tle_data[0], ts)
    except Exception as exc:
        logger.debug('Failed to create EarthSatellite for %s: %s', tle_data[0], exc)
        return []

    # Find events using Skyfield's native find_events()
    # Event types: 0=AOS, 1=TCA, 2=LOS
    try:
        times, events = satellite.find_events(
            observer, t0, t1, altitude_degrees=min_el
        )
    except Exception as exc:
        logger.debug('find_events failed for %s: %s', tle_data[0], exc)
        return []

    # Group events into AOS->TCA->LOS triplets
    passes = []
    i = 0
    total = len(events)

    # Skip any leading non-AOS events (satellite already above horizon at t0)
    while i < total and events[i] != 0:
        i += 1

    while i < total:
        # Expect AOS (0)
        if events[i] != 0:
            i += 1
            continue

        aos_time = times[i]
        i += 1

        # Collect TCA and LOS, watching for premature next AOS
        tca_time = None
        los_time = None

        while i < total and events[i] != 0:
            if events[i] == 1:
                tca_time = times[i]
            elif events[i] == 2:
                los_time = times[i]
            i += 1

        # Must have both AOS and LOS to form a valid pass
        if los_time is None:
            # Incomplete pass — skip
            continue

        # If TCA is missing, derive from midpoint between AOS and LOS
        if tca_time is None:
            aos_tt = aos_time.tt
            los_tt = los_time.tt
            tca_time = ts.tt_jd((aos_tt + los_tt) / 2.0)

        # Compute topocentric positions at AOS, TCA, LOS
        try:
            aos_topo = (satellite - observer).at(aos_time)
            tca_topo = (satellite - observer).at(tca_time)
            los_topo = (satellite - observer).at(los_time)

            aos_alt, aos_az, _ = aos_topo.altaz()
            tca_alt, tca_az, _ = tca_topo.altaz()
            los_alt, los_az, _ = los_topo.altaz()

            aos_dt = aos_time.utc_datetime()
            tca_dt = tca_time.utc_datetime()
            los_dt = los_time.utc_datetime()

            duration = (los_dt - aos_dt).total_seconds() / 60.0

            pass_dict: dict[str, Any] = {
                'aosTime': aos_dt.isoformat(),
                'aosAz': round(float(aos_az.degrees), 1),
                'aosEl': round(float(aos_alt.degrees), 1),
                'tcaTime': tca_dt.isoformat(),
                'tcaAz': round(float(tca_az.degrees), 1),
                'tcaEl': round(float(tca_alt.degrees), 1),
                'losTime': los_dt.isoformat(),
                'losAz': round(float(los_az.degrees), 1),
                'losEl': round(float(los_alt.degrees), 1),
                'duration': round(duration, 1),
                # Backwards-compatible fields
                'startTime': aos_dt.strftime('%Y-%m-%d %H:%M UTC'),
                'startTimeISO': aos_dt.isoformat(),
                'endTimeISO': los_dt.isoformat(),
                'maxEl': round(float(tca_alt.degrees), 1),
            }

            # Build 30-point az/el trajectory for polar plot
            if include_trajectory:
                trajectory = []
                for step in range(30):
                    frac = step / 29.0
                    t_pt = ts.tt_jd(
                        aos_time.tt + frac * (los_time.tt - aos_time.tt)
                    )
                    try:
                        pt_alt, pt_az, _ = (satellite - observer).at(t_pt).altaz()
                        trajectory.append({
                            'az': round(float(pt_az.degrees), 1),
                            'el': round(float(max(0.0, pt_alt.degrees)), 1),
                        })
                    except Exception as pt_exc:
                        logger.debug(
                            'Trajectory point error for %s: %s', tle_data[0], pt_exc
                        )
                pass_dict['trajectory'] = trajectory

            # Build 60-point lat/lon ground track for map
            if include_ground_track:
                ground_track = []
                for step in range(60):
                    frac = step / 59.0
                    t_pt = ts.tt_jd(
                        aos_time.tt + frac * (los_time.tt - aos_time.tt)
                    )
                    try:
                        geocentric = satellite.at(t_pt)
                        subpoint = wgs84.subpoint(geocentric)
                        ground_track.append({
                            'lat': round(float(subpoint.latitude.degrees), 4),
                            'lon': round(float(subpoint.longitude.degrees), 4),
                        })
                    except Exception as gt_exc:
                        logger.debug(
                            'Ground track point error for %s: %s', tle_data[0], gt_exc
                        )
                pass_dict['groundTrack'] = ground_track

            passes.append(pass_dict)

        except Exception as exc:
            logger.debug(
                'Failed to compute pass details for %s: %s', tle_data[0], exc
            )
            continue

    passes.sort(key=lambda p: p['startTimeISO'])
    return passes
