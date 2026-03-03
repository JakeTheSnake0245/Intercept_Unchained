# Routes package - registers all blueprints with the Flask app

def register_blueprints(app):
    """Register all route blueprints with the Flask app."""
    from .acars import acars_bp
    from .alerts import alerts_bp
    from .bluetooth import bluetooth_bp
    from .bluetooth_v2 import bluetooth_v2_bp
    from .bt_locate import bt_locate_bp
    from .controller import controller_bp
    from .correlation import correlation_bp
    from .dsc import dsc_bp
    from .listening_post import receiver_bp
    from .meshtastic import meshtastic_bp
    from .meshtastic_sdr import meshtastic_sdr_bp
    from .morse import morse_bp
    from .offline import offline_bp
    from .pager import pager_bp
    from .recordings import recordings_bp
    from .rtlamr import rtlamr_bp
    from .sensor import sensor_bp
    from .settings import settings_bp
    from .signalid import signalid_bp
    from .subghz import subghz_bp
    from .system import system_bp
    from .tscm import init_tscm_state, tscm_bp
    from .updater import updater_bp
    from .vdl2 import vdl2_bp
    from .wifi import wifi_bp
    from .wifi_v2 import wifi_v2_bp

    app.register_blueprint(pager_bp)
    app.register_blueprint(sensor_bp)
    app.register_blueprint(rtlamr_bp)
    app.register_blueprint(wifi_bp)
    app.register_blueprint(wifi_v2_bp)  # New unified WiFi API
    app.register_blueprint(bluetooth_bp)
    app.register_blueprint(bluetooth_v2_bp)  # New unified Bluetooth API
    app.register_blueprint(dsc_bp)  # VHF DSC maritime distress
    app.register_blueprint(acars_bp)
    app.register_blueprint(vdl2_bp)
    app.register_blueprint(correlation_bp)
    app.register_blueprint(receiver_bp)
    app.register_blueprint(meshtastic_bp)
    app.register_blueprint(meshtastic_sdr_bp)
    app.register_blueprint(tscm_bp)
    app.register_blueprint(offline_bp)  # Offline mode settings
    app.register_blueprint(updater_bp)  # GitHub update checking
    app.register_blueprint(alerts_bp)  # Cross-mode alerts
    app.register_blueprint(recordings_bp)  # Session recordings
    app.register_blueprint(subghz_bp)  # SubGHz transceiver (HackRF)
    app.register_blueprint(bt_locate_bp)  # BT Locate SAR device tracking
    app.register_blueprint(signalid_bp)  # External signal ID enrichment
    app.register_blueprint(morse_bp)  # CW/Morse code decoder
    app.register_blueprint(system_bp)  # System health monitoring

    # Initialize TSCM state with queue and lock from app
    import app as app_module
    if hasattr(app_module, 'tscm_queue') and hasattr(app_module, 'tscm_lock'):
        init_tscm_state(app_module.tscm_queue, app_module.tscm_lock)
