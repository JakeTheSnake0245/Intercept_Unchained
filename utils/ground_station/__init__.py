"""Ground station automation subpackage.

Provides unattended satellite observation, Doppler correction, IQ recording
(SigMF), parallel multi-decoder pipelines, live spectrum, and optional
antenna rotator control.

Public API::

    from utils.ground_station.scheduler import get_ground_station_scheduler
    from utils.ground_station.observation_profile import ObservationProfile
    from utils.ground_station.iq_bus import IQBus
"""
