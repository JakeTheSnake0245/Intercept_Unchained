"""
TSCM (Technical Surveillance Countermeasures) Routes Package

Provides endpoints for counter-surveillance sweeps, baseline management,
threat detection, and reporting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Blueprint, Response, jsonify, request

from data.tscm_frequencies import (
    SWEEP_PRESETS,
    get_all_sweep_presets,
    get_sweep_preset,
)
from utils.database import (
    acknowledge_tscm_threat,
    add_device_timeline_entry,
    add_tscm_threat,
    cleanup_old_timeline_entries,
    create_tscm_schedule,
    create_tscm_sweep,
    delete_tscm_baseline,
    delete_tscm_schedule,
    get_active_tscm_baseline,
    get_all_tscm_baselines,
    get_all_tscm_schedules,
    get_tscm_baseline,
    get_tscm_schedule,
    get_tscm_sweep,
    get_tscm_threat_summary,
    get_tscm_threats,
    set_active_tscm_baseline,
    update_tscm_schedule,
    update_tscm_sweep,
)
from utils.event_pipeline import process_event
from utils.sse import sse_stream_fanout
from utils.tscm.baseline import (
    BaselineComparator,
    BaselineRecorder,
    get_comparison_for_active_baseline,
)
from utils.tscm.correlation import (
    CorrelationEngine,
    get_correlation_engine,
    reset_correlation_engine,
)
from utils.tscm.detector import ThreatDetector
from utils.tscm.device_identity import (
    get_identity_engine,
    ingest_ble_dict,
    ingest_wifi_dict,
    reset_identity_engine,
)

# Import unified Bluetooth scanner helper for TSCM integration
try:
    from routes.bluetooth_v2 import get_tscm_bluetooth_snapshot
    _USE_UNIFIED_BT_SCANNER = True
except ImportError:
    _USE_UNIFIED_BT_SCANNER = False

logger = logging.getLogger('intercept.tscm')

tscm_bp = Blueprint('tscm', __name__, url_prefix='/tscm')

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - fallback for older Python
    ZoneInfo = None

# =============================================================================
# Global State (will be initialized from app.py)
# =============================================================================

# These will be set by app.py
tscm_queue: queue.Queue | None = None
tscm_lock: threading.Lock | None = None

# Local state
_sweep_thread: threading.Thread | None = None
_sweep_running = False
_current_sweep_id: int | None = None
_baseline_recorder = BaselineRecorder()
_schedule_thread: threading.Thread | None = None
_schedule_running = False


def init_tscm_state(tscm_q: queue.Queue, lock: threading.Lock) -> None:
    """Initialize TSCM state from app.py."""
    global tscm_queue, tscm_lock
    tscm_queue = tscm_q
    tscm_lock = lock
    start_tscm_scheduler()


def _emit_event(event_type: str, data: dict) -> None:
    """Emit an event to the SSE queue."""
    if tscm_queue:
        try:
            tscm_queue.put_nowait({
                'type': event_type,
                'timestamp': datetime.now().isoformat(),
                **data
            })
        except queue.Full:
            logger.warning("TSCM queue full, dropping event")


# =============================================================================
# Schedule Helpers
# =============================================================================

def _get_schedule_timezone(zone_name: str | None) -> Any:
    """Resolve schedule timezone from a zone name or fallback to local."""
    if zone_name and ZoneInfo:
        try:
            return ZoneInfo(zone_name)
        except Exception:
            logger.warning(f"Invalid timezone '{zone_name}', using local time")
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_cron_field(field: str, min_value: int, max_value: int) -> set[int]:
    """Parse a single cron field into a set of valid integers."""
    field = field.strip()
    if not field:
        raise ValueError("Empty cron field")

    values: set[int] = set()
    parts = field.split(',')
    for part in parts:
        part = part.strip()
        if part == '*':
            values.update(range(min_value, max_value + 1))
            continue
        if part.startswith('*/'):
            step = int(part[2:])
            if step <= 0:
                raise ValueError("Invalid step value")
            values.update(range(min_value, max_value + 1, step))
            continue
        range_part = part
        step = 1
        if '/' in part:
            range_part, step_str = part.split('/', 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError("Invalid step value")
        if '-' in range_part:
            start_str, end_str = range_part.split('-', 1)
            start = int(start_str)
            end = int(end_str)
            if start > end:
                start, end = end, start
            values.update(range(start, end + 1, step))
        else:
            values.add(int(range_part))

    return {v for v in values if min_value <= v <= max_value}


def _parse_cron_expression(expr: str) -> tuple[dict[str, set[int]], dict[str, bool]]:
    """Parse a cron expression into value sets and wildcard flags."""
    fields = (expr or '').split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have 5 fields")

    minute_field, hour_field, dom_field, month_field, dow_field = fields

    sets = {
        'minute': _parse_cron_field(minute_field, 0, 59),
        'hour': _parse_cron_field(hour_field, 0, 23),
        'dom': _parse_cron_field(dom_field, 1, 31),
        'month': _parse_cron_field(month_field, 1, 12),
        'dow': _parse_cron_field(dow_field, 0, 7),
    }

    # Normalize Sunday (7 -> 0)
    if 7 in sets['dow']:
        sets['dow'].add(0)
        sets['dow'].discard(7)

    wildcards = {
        'dom': dom_field.strip() == '*',
        'dow': dow_field.strip() == '*',
    }
    return sets, wildcards


def _cron_matches(dt: datetime, sets: dict[str, set[int]], wildcards: dict[str, bool]) -> bool:
    """Check if a datetime matches cron sets."""
    if dt.minute not in sets['minute']:
        return False
    if dt.hour not in sets['hour']:
        return False
    if dt.month not in sets['month']:
        return False

    dom_match = dt.day in sets['dom']
    # Cron DOW: Sunday=0
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in sets['dow']

    if wildcards['dom'] and wildcards['dow']:
        return True
    if wildcards['dom']:
        return dow_match
    if wildcards['dow']:
        return dom_match
    return dom_match or dow_match


def _next_run_from_cron(expr: str, after_dt: datetime) -> datetime | None:
    """Calculate next run time from cron expression after a given datetime."""
    sets, wildcards = _parse_cron_expression(expr)
    # Round to next minute
    candidate = after_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Search up to 366 days ahead
    for _ in range(366 * 24 * 60):
        if _cron_matches(candidate, sets, wildcards):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def _parse_schedule_timestamp(value: Any) -> datetime | None:
    """Parse stored schedule timestamp to aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _schedule_loop() -> None:
    """Background loop to trigger scheduled sweeps."""
    global _schedule_running

    while _schedule_running:
        try:
            schedules = get_all_tscm_schedules(enabled=True, limit=200)
            now_utc = datetime.now(timezone.utc)

            for schedule in schedules:
                schedule_id = schedule.get('id')
                cron_expr = schedule.get('cron_expression') or ''
                tz = _get_schedule_timezone(schedule.get('zone_name'))
                now_local = datetime.now(tz)

                next_run = _parse_schedule_timestamp(schedule.get('next_run'))

                if not next_run:
                    try:
                        computed = _next_run_from_cron(cron_expr, now_local)
                    except Exception as e:
                        logger.error(f"Schedule {schedule_id} cron parse error: {e}")
                        continue
                    if computed:
                        update_tscm_schedule(
                            schedule_id,
                            next_run=computed.astimezone(timezone.utc).isoformat()
                        )
                    continue

                if next_run <= now_utc:
                    if _sweep_running:
                        logger.info(f"Schedule {schedule_id} due but sweep running; skipping")
                        try:
                            computed = _next_run_from_cron(cron_expr, now_local)
                        except Exception as e:
                            logger.error(f"Schedule {schedule_id} cron parse error: {e}")
                            continue
                        if computed:
                            update_tscm_schedule(
                                schedule_id,
                                next_run=computed.astimezone(timezone.utc).isoformat()
                            )
                        continue

                    # Trigger sweep
                    result = _start_sweep_internal(
                        sweep_type=schedule.get('sweep_type') or 'standard',
                        baseline_id=schedule.get('baseline_id'),
                        wifi_enabled=True,
                        bt_enabled=True,
                        rf_enabled=True,
                        wifi_interface='',
                        bt_interface='',
                        sdr_device=None,
                        verbose_results=False
                    )

                    if result.get('status') == 'success':
                        try:
                            computed = _next_run_from_cron(cron_expr, now_local)
                        except Exception as e:
                            logger.error(f"Schedule {schedule_id} cron parse error: {e}")
                            computed = None

                        update_tscm_schedule(
                            schedule_id,
                            last_run=now_utc.isoformat(),
                            next_run=computed.astimezone(timezone.utc).isoformat() if computed else None
                        )
                        logger.info(f"Scheduled sweep started for schedule {schedule_id}")
                    else:
                        try:
                            computed = _next_run_from_cron(cron_expr, now_local)
                        except Exception as e:
                            logger.error(f"Schedule {schedule_id} cron parse error: {e}")
                            computed = None
                        if computed:
                            update_tscm_schedule(
                                schedule_id,
                                next_run=computed.astimezone(timezone.utc).isoformat()
                            )
                        logger.warning(f"Scheduled sweep failed for schedule {schedule_id}: {result.get('message')}")

        except Exception as e:
            logger.error(f"TSCM schedule loop error: {e}")

        time.sleep(30)


def start_tscm_scheduler() -> None:
    """Start background scheduler thread for TSCM sweeps."""
    global _schedule_thread, _schedule_running
    if _schedule_thread and _schedule_thread.is_alive():
        return
    _schedule_running = True
    _schedule_thread = threading.Thread(target=_schedule_loop, daemon=True)
    _schedule_thread.start()


# =============================================================================
# Sweep Helpers (used by sweep routes and schedule loop)
# =============================================================================

def _check_available_devices(wifi: bool, bt: bool, rf: bool) -> dict:
    """Check which scanning devices are available."""
    import os
    import platform
    import shutil
    import subprocess

    available = {
        'wifi': False,
        'bluetooth': False,
        'rf': False,
        'wifi_reason': 'Not checked',
        'bt_reason': 'Not checked',
        'rf_reason': 'Not checked',
    }

    # Check WiFi - use the same scanner singleton that performs actual scans
    if wifi:
        try:
            from utils.wifi.scanner import get_wifi_scanner
            scanner = get_wifi_scanner()
            interfaces = scanner._detect_interfaces()
            if interfaces:
                available['wifi'] = True
                available['wifi_reason'] = f'WiFi available ({interfaces[0]["name"]})'
            else:
                available['wifi_reason'] = 'No wireless interfaces found'
        except Exception as e:
            available['wifi_reason'] = f'WiFi detection error: {e}'

    # Check Bluetooth
    if bt:
        if platform.system() == 'Darwin':
            # macOS: Check for Bluetooth via system_profiler
            try:
                result = subprocess.run(
                    ['system_profiler', 'SPBluetoothDataType'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if 'Bluetooth' in result.stdout and result.returncode == 0:
                    available['bluetooth'] = True
                    available['bt_reason'] = 'macOS Bluetooth available'
                else:
                    available['bt_reason'] = 'Bluetooth not available'
            except (subprocess.TimeoutExpired, FileNotFoundError):
                available['bt_reason'] = 'Cannot detect Bluetooth'
        else:
            # Linux: Check for Bluetooth tools
            if shutil.which('bluetoothctl') or shutil.which('hcitool') or shutil.which('hciconfig'):
                try:
                    result = subprocess.run(
                        ['hciconfig'],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if 'hci' in result.stdout.lower():
                        available['bluetooth'] = True
                        available['bt_reason'] = 'Bluetooth adapter detected'
                    else:
                        available['bt_reason'] = 'No Bluetooth adapters found'
                except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
                    # Try bluetoothctl as fallback
                    try:
                        result = subprocess.run(
                            ['bluetoothctl', 'list'],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )
                        if result.stdout.strip():
                            available['bluetooth'] = True
                            available['bt_reason'] = 'Bluetooth adapter detected'
                        else:
                            # Check /sys for Bluetooth
                            try:
                                import glob
                                bt_devs = glob.glob('/sys/class/bluetooth/hci*')
                                if bt_devs:
                                    available['bluetooth'] = True
                                    available['bt_reason'] = 'Bluetooth adapter detected'
                                else:
                                    available['bt_reason'] = 'No Bluetooth adapters found'
                            except Exception:
                                available['bt_reason'] = 'No Bluetooth adapters found'
                    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
                        # Check /sys for Bluetooth
                        try:
                            import glob
                            bt_devs = glob.glob('/sys/class/bluetooth/hci*')
                            if bt_devs:
                                available['bluetooth'] = True
                                available['bt_reason'] = 'Bluetooth adapter detected'
                            else:
                                available['bt_reason'] = 'Cannot detect Bluetooth adapters'
                        except Exception:
                            available['bt_reason'] = 'Cannot detect Bluetooth adapters'
            else:
                # Fallback: check /sys even without tools
                try:
                    import glob
                    bt_devs = glob.glob('/sys/class/bluetooth/hci*')
                    if bt_devs:
                        available['bluetooth'] = True
                        available['bt_reason'] = 'Bluetooth adapter detected (no scan tools)'
                    else:
                        available['bt_reason'] = 'Bluetooth tools not installed (bluez)'
                except Exception:
                    available['bt_reason'] = 'Bluetooth tools not installed (bluez)'

    # Check RF/SDR
    if rf:
        try:
            from utils.sdr import SDRFactory
            devices = SDRFactory.detect_devices()
            if devices:
                available['rf'] = True
                available['rf_reason'] = f'{len(devices)} SDR device(s) detected'
            else:
                available['rf_reason'] = 'No SDR devices found'
        except ImportError:
            available['rf_reason'] = 'SDR detection unavailable'

    return available


def _start_sweep_internal(
    sweep_type: str,
    baseline_id: int | None,
    wifi_enabled: bool,
    bt_enabled: bool,
    rf_enabled: bool,
    wifi_interface: str = '',
    bt_interface: str = '',
    sdr_device: int | None = None,
    verbose_results: bool = False,
    custom_ranges: list[dict] | None = None,
) -> dict:
    """Start a TSCM sweep without request context."""
    global _sweep_running, _sweep_thread, _current_sweep_id

    if _sweep_running:
        return {'status': 'error', 'message': 'Sweep already running', 'http_status': 409}

    # Check for available devices
    devices = _check_available_devices(wifi_enabled, bt_enabled, rf_enabled)

    warnings = []
    if wifi_enabled and not devices['wifi']:
        warnings.append(f"WiFi: {devices['wifi_reason']}")
    if bt_enabled and not devices['bluetooth']:
        warnings.append(f"Bluetooth: {devices['bt_reason']}")
    if rf_enabled and not devices['rf']:
        warnings.append(f"RF: {devices['rf_reason']}")

    # If no devices available at all, return error
    if not any([devices['wifi'], devices['bluetooth'], devices['rf']]):
        return {
            'status': 'error',
            'message': 'No scanning devices available',
            'details': warnings,
            'http_status': 400,
        }

    # Create sweep record
    _current_sweep_id = create_tscm_sweep(
        sweep_type=sweep_type,
        baseline_id=baseline_id,
        wifi_enabled=wifi_enabled,
        bt_enabled=bt_enabled,
        rf_enabled=rf_enabled
    )

    _sweep_running = True

    # Start sweep thread
    _sweep_thread = threading.Thread(
        target=_run_sweep,
        args=(sweep_type, baseline_id, wifi_enabled, bt_enabled, rf_enabled,
              wifi_interface, bt_interface, sdr_device, verbose_results, custom_ranges),
        daemon=True
    )
    _sweep_thread.start()

    logger.info(f"Started TSCM sweep: type={sweep_type}, id={_current_sweep_id}")

    return {
        'status': 'success',
        'message': 'Sweep started',
        'sweep_id': _current_sweep_id,
        'sweep_type': sweep_type,
        'warnings': warnings if warnings else None,
        'devices': {
            'wifi': devices['wifi'],
            'bluetooth': devices['bluetooth'],
            'rf': devices['rf']
        }
    }


def _scan_wifi_networks(interface: str) -> list[dict]:
    """
    Scan for WiFi networks using the unified WiFi scanner.

    This is a facade that maintains backwards compatibility with TSCM
    while using the new unified scanner module.

    Automatically detects monitor mode interfaces and uses deep scan
    (airodump-ng) when appropriate.

    Args:
        interface: WiFi interface name (optional).

    Returns:
        List of network dicts with: bssid, essid, power, channel, privacy
    """
    try:
        from utils.wifi import get_wifi_scanner

        scanner = get_wifi_scanner()

        # Check if interface is in monitor mode
        is_monitor = False
        if interface:
            is_monitor = scanner._is_monitor_mode_interface(interface)

        if is_monitor:
            # Use deep scan for monitor mode interfaces
            logger.info(f"Interface {interface} is in monitor mode, using deep scan")

            # Check if airodump-ng is available
            caps = scanner.check_capabilities()
            if not caps.has_airodump_ng:
                logger.warning("airodump-ng not available for monitor mode scanning")
                return []

            # Start a short deep scan
            if not scanner.is_scanning:
                scanner.start_deep_scan(interface=interface, band='all')

            # Wait briefly for some results
            import time
            time.sleep(5)

            # Get current access points
            networks = []
            for ap in scanner.access_points:
                networks.append(ap.to_legacy_dict())

            logger.info(f"WiFi deep scan found {len(networks)} networks")
            return networks
        else:
            # Use quick scan for managed mode interfaces
            result = scanner.quick_scan(interface=interface, timeout=15)

            if result.error:
                logger.warning(f"WiFi scan error: {result.error}")

            # Convert to legacy format for TSCM
            networks = []
            for ap in result.access_points:
                networks.append(ap.to_legacy_dict())

            logger.info(f"WiFi scan found {len(networks)} networks")
            return networks

    except ImportError as e:
        logger.error(f"Failed to import wifi scanner: {e}")
        return []
    except Exception as e:
        logger.exception(f"WiFi scan failed: {e}")
        return []


def _scan_wifi_clients(interface: str) -> list[dict]:
    """
    Get WiFi client observations from the unified WiFi scanner.

    Clients are only available when monitor-mode scanning is active.
    """
    try:
        from utils.wifi import get_wifi_scanner

        scanner = get_wifi_scanner()
        if interface:
            try:
                if not scanner._is_monitor_mode_interface(interface):
                    return []
            except Exception:
                return []

        return [client.to_dict() for client in scanner.clients]
    except ImportError as e:
        logger.error(f"Failed to import wifi scanner: {e}")
        return []
    except Exception as e:
        logger.exception(f"WiFi client scan failed: {e}")
        return []


def _scan_bluetooth_devices(interface: str, duration: int = 10) -> list[dict]:
    """
    Scan for Bluetooth devices with manufacturer data detection.

    Uses the BLE scanner module (bleak library) for proper manufacturer ID
    detection, with fallback to system tools if bleak is unavailable.
    """
    import os
    import platform
    import re
    import shutil
    import subprocess

    devices = []
    seen_macs = set()

    logger.info(f"Starting Bluetooth scan (duration={duration}s, interface={interface})")

    # Try the BLE scanner module first (uses bleak for proper manufacturer detection)
    try:
        from utils.tscm.ble_scanner import get_ble_scanner, scan_ble_devices

        logger.info("Using BLE scanner module with manufacturer detection")
        ble_devices = scan_ble_devices(duration)

        for ble_dev in ble_devices:
            mac = ble_dev.get('mac', '').upper()
            if mac and mac not in seen_macs:
                seen_macs.add(mac)

                device = {
                    'mac': mac,
                    'name': ble_dev.get('name', 'Unknown'),
                    'rssi': ble_dev.get('rssi'),
                    'type': 'ble',
                    'manufacturer': ble_dev.get('manufacturer_name'),
                    'manufacturer_id': ble_dev.get('manufacturer_id'),
                    'is_tracker': ble_dev.get('is_tracker', False),
                    'tracker_type': ble_dev.get('tracker_type'),
                    'is_airtag': ble_dev.get('is_airtag', False),
                    'is_tile': ble_dev.get('is_tile', False),
                    'is_smarttag': ble_dev.get('is_smarttag', False),
                    'is_espressif': ble_dev.get('is_espressif', False),
                    'service_uuids': ble_dev.get('service_uuids', []),
                }
                devices.append(device)

        if devices:
            logger.info(f"BLE scanner found {len(devices)} devices")
            trackers = [d for d in devices if d.get('is_tracker')]
            if trackers:
                logger.info(f"Trackers detected: {[d.get('tracker_type') for d in trackers]}")
            return devices

    except ImportError:
        logger.warning("BLE scanner module not available, using fallback")
    except Exception as e:
        logger.warning(f"BLE scanner failed: {e}, using fallback")

    if platform.system() == 'Darwin':
        # macOS: Use system_profiler for basic Bluetooth info
        try:
            result = subprocess.run(
                ['system_profiler', 'SPBluetoothDataType', '-json'],
                capture_output=True, text=True, timeout=15
            )
            import json
            data = json.loads(result.stdout)
            bt_data = data.get('SPBluetoothDataType', [{}])[0]

            # Get connected/paired devices
            for section in ['device_connected', 'device_title']:
                section_data = bt_data.get(section, {})
                if isinstance(section_data, dict):
                    for name, info in section_data.items():
                        if isinstance(info, dict):
                            mac = info.get('device_address', '')
                            if mac and mac not in seen_macs:
                                seen_macs.add(mac)
                                devices.append({
                                    'mac': mac.upper(),
                                    'name': name,
                                    'type': info.get('device_minorType', 'unknown'),
                                    'connected': section == 'device_connected'
                                })
            logger.info(f"macOS Bluetooth scan found {len(devices)} devices")
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError) as e:
            logger.warning(f"macOS Bluetooth scan failed: {e}")

    else:
        # Linux: Try multiple methods
        iface = interface or 'hci0'

        # Method 1: Try hcitool scan (simpler, more reliable)
        if shutil.which('hcitool'):
            try:
                logger.info("Trying hcitool scan...")
                result = subprocess.run(
                    ['hcitool', '-i', iface, 'scan', '--flush'],
                    capture_output=True, text=True, timeout=duration + 5
                )
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if line and '\t' in line:
                        parts = line.split('\t')
                        if len(parts) >= 1 and ':' in parts[0]:
                            mac = parts[0].strip().upper()
                            name = parts[1].strip() if len(parts) > 1 else 'Unknown'
                            if mac not in seen_macs:
                                seen_macs.add(mac)
                                devices.append({'mac': mac, 'name': name})
                logger.info(f"hcitool scan found {len(devices)} classic BT devices")
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                logger.warning(f"hcitool scan failed: {e}")

        # Method 2: Try btmgmt for BLE devices
        if shutil.which('btmgmt'):
            try:
                logger.info("Trying btmgmt find...")
                result = subprocess.run(
                    ['btmgmt', 'find'],
                    capture_output=True, text=True, timeout=duration + 5
                )
                for line in result.stdout.split('\n'):
                    # Parse btmgmt output: "dev_found: XX:XX:XX:XX:XX:XX type LE..."
                    if 'dev_found' in line.lower() or ('type' in line.lower() and ':' in line):
                        mac_match = re.search(
                            r'([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:'
                            r'[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})',
                            line
                        )
                        if mac_match:
                            mac = mac_match.group(1).upper()
                            if mac not in seen_macs:
                                seen_macs.add(mac)
                                # Try to extract name
                                name_match = re.search(r'name\s+(.+?)(?:\s|$)', line, re.I)
                                name = name_match.group(1) if name_match else 'Unknown BLE'
                                devices.append({
                                    'mac': mac,
                                    'name': name,
                                    'type': 'ble' if 'le' in line.lower() else 'classic'
                                })
                logger.info(f"btmgmt found {len(devices)} total devices")
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                logger.warning(f"btmgmt find failed: {e}")

        # Method 3: Try bluetoothctl as last resort
        if not devices and shutil.which('bluetoothctl'):
            try:
                import pty
                import select

                logger.info("Trying bluetoothctl scan...")
                master_fd, slave_fd = pty.openpty()
                process = subprocess.Popen(
                    ['bluetoothctl'],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True
                )
                os.close(slave_fd)

                # Start scanning
                time.sleep(0.3)
                os.write(master_fd, b'power on\n')
                time.sleep(0.3)
                os.write(master_fd, b'scan on\n')

                # Collect devices for specified duration
                scan_end = time.time() + min(duration, 10)  # Cap at 10 seconds
                buffer = ''

                while time.time() < scan_end:
                    readable, _, _ = select.select([master_fd], [], [], 1.0)
                    if readable:
                        try:
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            buffer += data.decode('utf-8', errors='replace')

                            while '\n' in buffer:
                                line, buffer = buffer.split('\n', 1)
                                line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()

                                if 'Device' in line:
                                    match = re.search(
                                        r'([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:'
                                        r'[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})\s*(.*)',
                                        line
                                    )
                                    if match:
                                        mac = match.group(1).upper()
                                        name = match.group(2).strip()
                                        # Remove RSSI from name if present
                                        name = re.sub(r'\s*RSSI:\s*-?\d+\s*', '', name).strip()

                                        if mac not in seen_macs:
                                            seen_macs.add(mac)
                                            devices.append({
                                                'mac': mac,
                                                'name': name or '[Unknown]'
                                            })
                        except OSError:
                            break

                # Stop scanning and cleanup
                try:
                    os.write(master_fd, b'scan off\n')
                    time.sleep(0.2)
                    os.write(master_fd, b'quit\n')
                except OSError:
                    pass

                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

                with contextlib.suppress(OSError):
                    os.close(master_fd)

                logger.info(f"bluetoothctl scan found {len(devices)} devices")

            except (FileNotFoundError, subprocess.SubprocessError) as e:
                logger.warning(f"bluetoothctl scan failed: {e}")

    return devices


def _scan_rf_signals(
    sdr_device: int | None,
    duration: int = 30,
    stop_check: callable | None = None,
    sweep_ranges: list[dict] | None = None
) -> list[dict]:
    """
    Scan for RF signals using SDR (rtl_power or hackrf_sweep).

    Scans common surveillance frequency bands:
    - 88-108 MHz: FM broadcast (potential FM bugs)
    - 315 MHz: Common ISM band (wireless devices)
    - 433 MHz: ISM band (European wireless devices, car keys)
    - 868 MHz: European ISM band
    - 915 MHz: US ISM band
    - 1.2 GHz: Video transmitters
    - 2.4 GHz: WiFi, Bluetooth, video transmitters

    Args:
        sdr_device: SDR device index
        duration: Scan duration per band
        stop_check: Optional callable that returns True if scan should stop.
                   Defaults to checking module-level _sweep_running.
        sweep_ranges: Optional preset ranges (MHz) from SWEEP_PRESETS.
    """
    # Default stop check uses module-level _sweep_running
    if stop_check is None:
        def stop_check():
            return not _sweep_running
    import os
    import shutil
    import subprocess
    import tempfile

    signals = []

    logger.info(f"Starting RF scan (device={sdr_device})")

    # Detect available SDR devices and sweep tools
    rtl_power_path = shutil.which('rtl_power')
    hackrf_sweep_path = shutil.which('hackrf_sweep')

    sdr_type = None
    sweep_tool_path = None

    try:
        from utils.sdr import SDRFactory
        from utils.sdr.base import SDRType
        devices = SDRFactory.detect_devices()
        rtlsdr_available = any(d.sdr_type == SDRType.RTL_SDR for d in devices)
        hackrf_available = any(d.sdr_type == SDRType.HACKRF for d in devices)
    except ImportError:
        rtlsdr_available = False
        hackrf_available = False

    # Pick the best available SDR + sweep tool combo
    if rtlsdr_available and rtl_power_path:
        sdr_type = 'rtlsdr'
        sweep_tool_path = rtl_power_path
        logger.info(f"Using RTL-SDR with rtl_power at: {rtl_power_path}")
    elif hackrf_available and hackrf_sweep_path:
        sdr_type = 'hackrf'
        sweep_tool_path = hackrf_sweep_path
        logger.info(f"Using HackRF with hackrf_sweep at: {hackrf_sweep_path}")
    elif rtl_power_path:
        # Tool exists but no device detected — try anyway (detection may have failed)
        sdr_type = 'rtlsdr'
        sweep_tool_path = rtl_power_path
        logger.info("No SDR detected but rtl_power found, attempting RTL-SDR scan")
    elif hackrf_sweep_path:
        sdr_type = 'hackrf'
        sweep_tool_path = hackrf_sweep_path
        logger.info("No SDR detected but hackrf_sweep found, attempting HackRF scan")

    if not sweep_tool_path:
        logger.warning("No supported sweep tool found (rtl_power or hackrf_sweep)")
        _emit_event('rf_status', {
            'status': 'error',
            'message': 'No SDR sweep tool installed. Install rtl-sdr (rtl_power) or HackRF (hackrf_sweep) for RF scanning.',
        })
        return signals

    # Define frequency bands to scan (in Hz)
    # Format: (start_freq, end_freq, bin_size, description)
    scan_bands: list[tuple[int, int, int, str]] = []

    if sweep_ranges:
        for rng in sweep_ranges:
            try:
                start_mhz = float(rng.get('start', 0))
                end_mhz = float(rng.get('end', 0))
                step_mhz = float(rng.get('step', 0.1))
                name = rng.get('name') or f"{start_mhz:.1f}-{end_mhz:.1f} MHz"
                if start_mhz > 0 and end_mhz > start_mhz:
                    bin_size = max(1000, int(step_mhz * 1_000_000))
                    scan_bands.append((
                        int(start_mhz * 1_000_000),
                        int(end_mhz * 1_000_000),
                        bin_size,
                        name
                    ))
            except (TypeError, ValueError):
                continue

    if not scan_bands:
        # Fallback: focus on common bug frequencies
        scan_bands = [
            (88000000, 108000000, 100000, 'FM Broadcast'),       # FM bugs
            (315000000, 316000000, 10000, '315 MHz ISM'),        # US ISM
            (433000000, 434000000, 10000, '433 MHz ISM'),        # EU ISM
            (868000000, 869000000, 10000, '868 MHz ISM'),        # EU ISM
            (902000000, 928000000, 100000, '915 MHz ISM'),       # US ISM
            (1200000000, 1300000000, 100000, '1.2 GHz Video'),   # Video TX
            (2400000000, 2500000000, 500000, '2.4 GHz ISM'),     # WiFi/BT/Video
        ]

    # Create temp file for output
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Build device argument
        device_idx = sdr_device if sdr_device is not None else 0

        # Scan each band and look for strong signals
        for start_freq, end_freq, bin_size, band_name in scan_bands:
            if stop_check():
                break

            logger.info(f"Scanning {band_name} ({start_freq/1e6:.1f}-{end_freq/1e6:.1f} MHz)")

            try:
                # Build sweep command based on SDR type
                if sdr_type == 'hackrf':
                    cmd = [
                        sweep_tool_path,
                        '-f', f'{int(start_freq / 1e6)}:{int(end_freq / 1e6)}',
                        '-w', str(bin_size),
                        '-1',  # Single sweep
                    ]
                    output_mode = 'stdout'
                else:
                    cmd = [
                        sweep_tool_path,
                        '-f', f'{start_freq}:{end_freq}:{bin_size}',
                        '-g', '40',           # Gain
                        '-i', '1',            # Integration interval (1 second)
                        '-1',                 # Single shot mode
                        '-c', '20%',          # Crop 20% of edges
                        '-d', str(device_idx),
                        tmp_path,
                    ]
                    output_mode = 'file'

                logger.debug(f"Running: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.warning(f"{os.path.basename(sweep_tool_path)} returned {result.returncode}: {result.stderr}")

                # For HackRF, write stdout CSV data to temp file for unified parsing
                if output_mode == 'stdout' and result.stdout:
                    with open(tmp_path, 'w') as f:
                        f.write(result.stdout)

                # Parse the CSV output (same format for both rtl_power and hackrf_sweep)
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path) as f:
                        for line in f:
                            parts = line.strip().split(',')
                            if len(parts) >= 7:
                                try:
                                    # CSV format: date, time, hz_low, hz_high, hz_step, samples, db_values...
                                    hz_low = int(parts[2].strip())
                                    int(parts[3].strip())
                                    hz_step = float(parts[4].strip())
                                    db_values = [float(x) for x in parts[6:] if x.strip()]

                                    # Find peaks above noise floor
                                    noise_floor = sum(db_values) / len(db_values) if db_values else -100
                                    threshold = noise_floor + 6  # Signal must be 6dB above noise

                                    for idx, db in enumerate(db_values):
                                        if db > threshold and db > -90:  # Detect signals above -90dBm
                                            freq_hz = hz_low + (idx * hz_step)
                                            freq_mhz = freq_hz / 1000000

                                            signals.append({
                                                'frequency': freq_mhz,
                                                'frequency_hz': freq_hz,
                                                'power': db,
                                                'band': band_name,
                                                'noise_floor': noise_floor,
                                                'signal_strength': db - noise_floor
                                            })
                                except (ValueError, IndexError):
                                    continue

                    # Clear file for next band
                    open(tmp_path, 'w').close()

            except subprocess.TimeoutExpired:
                logger.warning(f"RF scan timeout for band {band_name}")
            except Exception as e:
                logger.warning(f"RF scan error for band {band_name}: {e}")

    finally:
        # Cleanup temp file
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    # Deduplicate nearby frequencies (within 100kHz)
    if signals:
        signals.sort(key=lambda x: x['frequency'])
        deduped = [signals[0]]
        for sig in signals[1:]:
            if sig['frequency'] - deduped[-1]['frequency'] > 0.1:  # 100 kHz
                deduped.append(sig)
            elif sig['power'] > deduped[-1]['power']:
                deduped[-1] = sig  # Keep stronger signal
        signals = deduped

    logger.info(f"RF scan found {len(signals)} signals")
    return signals


def _run_sweep(
    sweep_type: str,
    baseline_id: int | None,
    wifi_enabled: bool,
    bt_enabled: bool,
    rf_enabled: bool,
    wifi_interface: str = '',
    bt_interface: str = '',
    sdr_device: int | None = None,
    verbose_results: bool = False,
    custom_ranges: list[dict] | None = None,
) -> None:
    """
    Run the TSCM sweep in a background thread.

    This orchestrates data collection from WiFi, BT, and RF sources,
    then analyzes results for threats using the correlation engine.
    """
    global _sweep_running, _current_sweep_id

    try:
        # Get baseline for comparison if specified
        baseline = None
        if baseline_id:
            baseline = get_tscm_baseline(baseline_id)

        # Get sweep preset
        preset = get_sweep_preset(sweep_type) or SWEEP_PRESETS.get('standard')
        duration = preset.get('duration_seconds', 300)

        _emit_event('sweep_started', {
            'sweep_id': _current_sweep_id,
            'sweep_type': sweep_type,
            'duration': duration,
            'wifi': wifi_enabled,
            'bluetooth': bt_enabled,
            'rf': rf_enabled,
        })

        # Initialize detector and correlation engine
        detector = ThreatDetector(baseline)
        correlation = get_correlation_engine()
        # Clear old profiles from previous sweeps (keep 24h history)
        correlation.clear_old_profiles(24)

        # Initialize device identity engine for MAC-randomization resistant detection
        identity_engine = get_identity_engine()
        identity_engine.clear()  # Start fresh for this sweep
        from utils.tscm.advanced import get_timeline_manager
        timeline_manager = get_timeline_manager()
        try:
            cleanup_old_timeline_entries(72)
        except Exception as e:
            logger.debug(f"TSCM timeline cleanup skipped: {e}")

        last_timeline_write: dict[str, float] = {}
        timeline_bucket = getattr(timeline_manager, 'bucket_seconds', 30)

        def _maybe_store_timeline(
            identifier: str,
            protocol: str,
            rssi: int | None = None,
            channel: int | None = None,
            frequency: float | None = None,
            attributes: dict | None = None
        ) -> None:
            if not identifier:
                return

            identifier_norm = identifier.upper() if isinstance(identifier, str) else str(identifier)
            key = f"{protocol}:{identifier_norm}"
            now_ts = time.time()
            last_ts = last_timeline_write.get(key)
            if last_ts and (now_ts - last_ts) < timeline_bucket:
                return

            last_timeline_write[key] = now_ts
            try:
                add_device_timeline_entry(
                    device_identifier=identifier_norm,
                    protocol=protocol,
                    sweep_id=_current_sweep_id,
                    rssi=rssi,
                    channel=channel,
                    frequency=frequency,
                    attributes=attributes
                )
            except Exception as e:
                logger.debug(f"TSCM timeline store error: {e}")

        # Collect and analyze data
        threats_found = 0
        severity_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        all_wifi = {}  # Use dict for deduplication by BSSID
        all_wifi_clients = {}  # Use dict for deduplication by client MAC
        all_bt = {}    # Use dict for deduplication by MAC
        all_rf = []

        start_time = time.time()
        last_wifi_scan = 0
        last_bt_scan = 0
        last_rf_scan = 0
        wifi_scan_interval = 15  # Scan WiFi every 15 seconds
        bt_scan_interval = 20   # Scan Bluetooth every 20 seconds
        rf_scan_interval = 30   # Scan RF every 30 seconds

        while _sweep_running and (time.time() - start_time) < duration:
            current_time = time.time()

            # Perform WiFi scan
            if wifi_enabled and (current_time - last_wifi_scan) >= wifi_scan_interval:
                try:
                    wifi_networks = _scan_wifi_networks(wifi_interface)
                    last_wifi_scan = current_time
                    if not wifi_networks and not all_wifi:
                        logger.warning("TSCM WiFi scan returned 0 networks")
                    _emit_event('sweep_progress', {
                        'progress': min(95, int(((current_time - start_time) / duration) * 100)),
                        'status': f'Scanning WiFi... ({len(wifi_networks)} found)',
                        'wifi_count': len(all_wifi) + len([n for n in wifi_networks if n.get('bssid') and n.get('bssid') not in all_wifi]),
                        'bt_count': len(all_bt),
                        'rf_count': len(all_rf),
                    })
                    for network in wifi_networks:
                        try:
                            bssid = network.get('bssid', '')
                            ssid = network.get('essid', network.get('ssid'))
                            try:
                                rssi_val = int(network.get('power', network.get('signal')))
                            except (ValueError, TypeError):
                                rssi_val = None
                            if bssid:
                                try:
                                    timeline_manager.add_observation(
                                        identifier=bssid,
                                        protocol='wifi',
                                        rssi=rssi_val,
                                        channel=network.get('channel'),
                                        name=ssid,
                                        attributes={'ssid': ssid, 'encryption': network.get('privacy')}
                                    )
                                except Exception as e:
                                    logger.debug(f"WiFi timeline observation error: {e}")
                                _maybe_store_timeline(
                                    identifier=bssid,
                                    protocol='wifi',
                                    rssi=rssi_val,
                                    channel=network.get('channel'),
                                    attributes={'ssid': ssid, 'encryption': network.get('privacy')}
                                )
                            if bssid and bssid not in all_wifi:
                                all_wifi[bssid] = network
                                # Emit device event for frontend
                                is_threat = False
                                # Analyze for threats
                                threat = detector.analyze_wifi_device(network)
                                if threat:
                                    _handle_threat(threat)
                                    threats_found += 1
                                    is_threat = True
                                    sev = threat.get('severity', 'low').lower()
                                    if sev in severity_counts:
                                        severity_counts[sev] += 1
                                # Classify device and get correlation profile
                                classification = detector.classify_wifi_device(network)
                                profile = correlation.analyze_wifi_device(network)

                                # Feed to identity engine for MAC-randomization resistant clustering
                                # Note: WiFi APs don't typically use randomized MACs, but clients do
                                try:
                                    wifi_obs = {
                                        'timestamp': datetime.now().isoformat(),
                                        'src_mac': bssid,
                                        'bssid': bssid,
                                        'ssid': network.get('essid'),
                                        'rssi': network.get('power'),
                                        'channel': network.get('channel'),
                                        'encryption': network.get('privacy'),
                                        'frame_type': 'beacon',
                                    }
                                    ingest_wifi_dict(wifi_obs)
                                except Exception as e:
                                    logger.debug(f"Identity engine WiFi ingest error: {e}")

                                # Send device to frontend
                                _emit_event('wifi_device', {
                                    'bssid': bssid,
                                    'ssid': network.get('essid', 'Hidden'),
                                    'channel': network.get('channel', ''),
                                    'signal': network.get('power', ''),
                                    'security': network.get('privacy', ''),
                                    'vendor': network.get('vendor'),
                                    'is_threat': is_threat,
                                    'is_new': not classification.get('in_baseline', False),
                                    'classification': profile.risk_level.value,
                                    'reasons': classification.get('reasons', []),
                                    'score': profile.total_score,
                                    'score_modifier': profile.score_modifier,
                                    'known_device': profile.known_device,
                                    'known_device_name': profile.known_device_name,
                                    'indicators': [{'type': i.type.value, 'desc': i.description} for i in profile.indicators],
                                    'recommended_action': profile.recommended_action,
                                })
                        except Exception as e:
                            logger.error(f"WiFi device processing error for {network.get('bssid', '?')}: {e}")

                    # WiFi clients (monitor mode only)
                    try:
                        wifi_clients = _scan_wifi_clients(wifi_interface)
                        for client in wifi_clients:
                            mac = (client.get('mac') or '').upper()
                            if not mac or mac in all_wifi_clients:
                                continue
                            all_wifi_clients[mac] = client

                            rssi_val = client.get('rssi_current')
                            if rssi_val is None:
                                rssi_val = client.get('rssi_median') or client.get('rssi_ema')

                            client_device = {
                                'mac': mac,
                                'vendor': client.get('vendor'),
                                'name': client.get('vendor') or 'WiFi Client',
                                'rssi': rssi_val,
                                'associated_bssid': client.get('associated_bssid'),
                                'probed_ssids': client.get('probed_ssids', []),
                                'probe_count': client.get('probe_count', len(client.get('probed_ssids', []))),
                                'is_client': True,
                            }

                            try:
                                timeline_manager.add_observation(
                                    identifier=mac,
                                    protocol='wifi',
                                    rssi=rssi_val,
                                    name=client_device.get('vendor') or f'WiFi Client {mac[-5:]}',
                                    attributes={'client': True, 'associated_bssid': client_device.get('associated_bssid')}
                                )
                            except Exception as e:
                                logger.debug(f"WiFi client timeline observation error: {e}")
                            _maybe_store_timeline(
                                identifier=mac,
                                protocol='wifi',
                                rssi=rssi_val,
                                attributes={'client': True, 'associated_bssid': client_device.get('associated_bssid')}
                            )

                            profile = correlation.analyze_wifi_device(client_device)
                            client_device['classification'] = profile.risk_level.value
                            client_device['score'] = profile.total_score
                            client_device['score_modifier'] = profile.score_modifier
                            client_device['known_device'] = profile.known_device
                            client_device['known_device_name'] = profile.known_device_name
                            client_device['indicators'] = [
                                {'type': i.type.value, 'desc': i.description}
                                for i in profile.indicators
                            ]
                            client_device['recommended_action'] = profile.recommended_action

                            # Feed to identity engine for MAC-randomization resistant clustering
                            try:
                                wifi_obs = {
                                    'timestamp': datetime.now().isoformat(),
                                    'src_mac': mac,
                                    'bssid': client_device.get('associated_bssid'),
                                    'rssi': rssi_val,
                                    'frame_type': 'probe_request',
                                    'probed_ssids': client_device.get('probed_ssids', []),
                                }
                                ingest_wifi_dict(wifi_obs)
                            except Exception as e:
                                logger.debug(f"Identity engine WiFi client ingest error: {e}")

                            _emit_event('wifi_client', client_device)
                    except Exception as e:
                        logger.debug(f"WiFi client scan error: {e}")
                except Exception as e:
                    last_wifi_scan = current_time
                    logger.error(f"WiFi scan error: {e}")

            # Perform Bluetooth scan
            if bt_enabled and (current_time - last_bt_scan) >= bt_scan_interval:
                try:
                    # Use unified Bluetooth scanner if available
                    if _USE_UNIFIED_BT_SCANNER:
                        logger.info("TSCM: Using unified BT scanner for snapshot")
                        bt_devices = get_tscm_bluetooth_snapshot(duration=8)
                        logger.info(f"TSCM: Unified scanner returned {len(bt_devices)} devices")
                    else:
                        logger.info(f"TSCM: Using legacy BT scanner on {bt_interface}")
                        bt_devices = _scan_bluetooth_devices(bt_interface, duration=8)
                        logger.info(f"TSCM: Legacy scanner returned {len(bt_devices)} devices")
                    last_bt_scan = current_time
                    for device in bt_devices:
                        try:
                            mac = device.get('mac', '')
                            try:
                                rssi_val = int(device.get('rssi', device.get('signal')))
                            except (ValueError, TypeError):
                                rssi_val = None
                            if mac:
                                try:
                                    timeline_manager.add_observation(
                                        identifier=mac,
                                        protocol='bluetooth',
                                        rssi=rssi_val,
                                        name=device.get('name'),
                                        attributes={'device_type': device.get('type')}
                                    )
                                except Exception as e:
                                    logger.debug(f"BT timeline observation error: {e}")
                                _maybe_store_timeline(
                                    identifier=mac,
                                    protocol='bluetooth',
                                    rssi=rssi_val,
                                    attributes={'device_type': device.get('type')}
                                )
                            if mac and mac not in all_bt:
                                all_bt[mac] = device
                                is_threat = False
                                # Analyze for threats
                                threat = detector.analyze_bt_device(device)
                                if threat:
                                    _handle_threat(threat)
                                    threats_found += 1
                                    is_threat = True
                                    sev = threat.get('severity', 'low').lower()
                                    if sev in severity_counts:
                                        severity_counts[sev] += 1
                                # Classify device and get correlation profile
                                classification = detector.classify_bt_device(device)
                                profile = correlation.analyze_bluetooth_device(device)

                                # Feed to identity engine for MAC-randomization resistant clustering
                                try:
                                    ble_obs = {
                                        'timestamp': datetime.now().isoformat(),
                                        'addr': mac,
                                        'rssi': device.get('rssi'),
                                        'manufacturer_id': device.get('manufacturer_id') or device.get('company_id'),
                                        'manufacturer_data': device.get('manufacturer_data'),
                                        'service_uuids': device.get('services', []),
                                        'local_name': device.get('name'),
                                    }
                                    ingest_ble_dict(ble_obs)
                                except Exception as e:
                                    logger.debug(f"Identity engine BLE ingest error: {e}")

                                # Send device to frontend
                                _emit_event('bt_device', {
                                    'mac': mac,
                                    'name': device.get('name', 'Unknown'),
                                    'device_type': device.get('type', ''),
                                    'rssi': device.get('rssi', ''),
                                    'manufacturer': device.get('manufacturer'),
                                    'tracker': device.get('tracker'),
                                    'tracker_type': device.get('tracker_type'),
                                    'is_threat': is_threat,
                                    'is_new': not classification.get('in_baseline', False),
                                    'classification': profile.risk_level.value,
                                    'reasons': classification.get('reasons', []),
                                    'is_audio_capable': classification.get('is_audio_capable', False),
                                    'score': profile.total_score,
                                    'score_modifier': profile.score_modifier,
                                    'known_device': profile.known_device,
                                    'known_device_name': profile.known_device_name,
                                    'indicators': [{'type': i.type.value, 'desc': i.description} for i in profile.indicators],
                                    'recommended_action': profile.recommended_action,
                                })
                        except Exception as e:
                            logger.error(f"BT device processing error for {device.get('mac', '?')}: {e}")
                except Exception as e:
                    last_bt_scan = current_time
                    import traceback
                    logger.error(f"Bluetooth scan error: {e}\n{traceback.format_exc()}")

            # Perform RF scan using SDR
            if rf_enabled and (current_time - last_rf_scan) >= rf_scan_interval:
                try:
                    _emit_event('sweep_progress', {
                        'progress': min(100, int(((current_time - start_time) / duration) * 100)),
                        'status': 'Scanning RF spectrum...',
                        'wifi_count': len(all_wifi),
                        'bt_count': len(all_bt),
                        'rf_count': len(all_rf),
                    })
                    # Try RF scan even if sdr_device is None (will use device 0)
                    rf_signals = _scan_rf_signals(sdr_device, sweep_ranges=custom_ranges or preset.get('ranges'))

                    # If no signals and this is first RF scan, send info event
                    if not rf_signals and last_rf_scan == 0:
                        _emit_event('rf_status', {
                            'status': 'no_signals',
                            'message': 'RF scan completed - no signals above threshold. This may be normal in a quiet RF environment.',
                        })

                    for signal in rf_signals:
                        freq_key = f"{signal['frequency']:.3f}"
                        try:
                            power_val = int(float(signal.get('power', signal.get('level'))))
                        except (ValueError, TypeError):
                            power_val = None
                        try:
                            timeline_manager.add_observation(
                                identifier=freq_key,
                                protocol='rf',
                                rssi=power_val,
                                frequency=signal.get('frequency'),
                                name=f"{freq_key} MHz",
                                attributes={'band': signal.get('band')}
                            )
                        except Exception as e:
                            logger.debug(f"RF timeline observation error: {e}")
                        _maybe_store_timeline(
                            identifier=freq_key,
                            protocol='rf',
                            rssi=power_val,
                            frequency=signal.get('frequency'),
                            attributes={'band': signal.get('band')}
                        )
                        if freq_key not in [f"{s['frequency']:.3f}" for s in all_rf]:
                            all_rf.append(signal)
                            is_threat = False
                            # Analyze RF signal for threats
                            threat = detector.analyze_rf_signal(signal)
                            if threat:
                                _handle_threat(threat)
                                threats_found += 1
                                is_threat = True
                                sev = threat.get('severity', 'low').lower()
                                if sev in severity_counts:
                                    severity_counts[sev] += 1
                            # Classify signal and get correlation profile
                            classification = detector.classify_rf_signal(signal)
                            profile = correlation.analyze_rf_signal(signal)
                            # Send signal to frontend
                            _emit_event('rf_signal', {
                                'frequency': signal['frequency'],
                                'power': signal['power'],
                                'band': signal['band'],
                                'signal_strength': signal.get('signal_strength', 0),
                                'is_threat': is_threat,
                                'is_new': not classification.get('in_baseline', False),
                                'classification': profile.risk_level.value,
                                'reasons': classification.get('reasons', []),
                                'score': profile.total_score,
                                'score_modifier': profile.score_modifier,
                                'known_device': profile.known_device,
                                'known_device_name': profile.known_device_name,
                                'indicators': [{'type': i.type.value, 'desc': i.description} for i in profile.indicators],
                                'recommended_action': profile.recommended_action,
                            })
                    last_rf_scan = current_time
                except Exception as e:
                    logger.error(f"RF scan error: {e}")

            # Update progress
            elapsed = time.time() - start_time
            progress = min(100, int((elapsed / duration) * 100))

            _emit_event('sweep_progress', {
                'progress': progress,
                'elapsed': int(elapsed),
                'duration': duration,
                'wifi_count': len(all_wifi),
                'bt_count': len(all_bt),
                'rf_count': len(all_rf),
                'threats_found': threats_found,
                'severity_counts': severity_counts,
            })

            time.sleep(2)  # Update every 2 seconds

        # Complete sweep (run even if stopped by user so correlations/clusters are computed)
        if _current_sweep_id:
            # Run cross-protocol correlation analysis
            correlations = correlation.correlate_devices()
            findings = correlation.get_all_findings()

            # Run baseline comparison if a baseline was provided
            baseline_comparison = None
            if baseline:
                comparator = BaselineComparator(baseline)
                baseline_comparison = comparator.compare_all(
                    wifi_devices=list(all_wifi.values()),
                    wifi_clients=list(all_wifi_clients.values()),
                    bt_devices=list(all_bt.values()),
                    rf_signals=all_rf
                )
                logger.info(
                    f"Baseline comparison: {baseline_comparison['total_new']} new, "
                    f"{baseline_comparison['total_missing']} missing"
                )

            # Finalize identity engine and get MAC-randomization resistant clusters
            identity_engine.finalize_all_sessions()
            identity_summary = identity_engine.get_summary()
            identity_clusters = [c.to_dict() for c in identity_engine.get_clusters()]

            if verbose_results:
                wifi_payload = list(all_wifi.values())
                wifi_client_payload = list(all_wifi_clients.values())
                bt_payload = list(all_bt.values())
                rf_payload = list(all_rf)
            else:
                wifi_payload = [
                    {
                        'bssid': d.get('bssid') or d.get('mac'),
                        'essid': d.get('essid') or d.get('ssid'),
                        'ssid': d.get('ssid') or d.get('essid'),
                        'channel': d.get('channel'),
                        'power': d.get('power', d.get('signal')),
                        'privacy': d.get('privacy', d.get('encryption')),
                        'encryption': d.get('encryption', d.get('privacy')),
                    }
                    for d in all_wifi.values()
                ]
                wifi_client_payload = []
                for client in all_wifi_clients.values():
                    mac = client.get('mac') or client.get('address')
                    if isinstance(mac, str):
                        mac = mac.upper()
                    probed_ssids = client.get('probed_ssids') or []
                    rssi = client.get('rssi')
                    if rssi is None:
                        rssi = client.get('rssi_current')
                    if rssi is None:
                        rssi = client.get('rssi_median')
                    if rssi is None:
                        rssi = client.get('rssi_ema')
                    wifi_client_payload.append({
                        'mac': mac,
                        'vendor': client.get('vendor'),
                        'rssi': rssi,
                        'associated_bssid': client.get('associated_bssid'),
                        'is_associated': client.get('is_associated'),
                        'probed_ssids': probed_ssids,
                        'probe_count': client.get('probe_count', len(probed_ssids)),
                    })
                bt_payload = [
                    {
                        'mac': d.get('mac') or d.get('address'),
                        'name': d.get('name'),
                        'rssi': d.get('rssi'),
                        'manufacturer': d.get('manufacturer', d.get('manufacturer_name')),
                    }
                    for d in all_bt.values()
                ]
                rf_payload = [
                    {
                        'frequency': s.get('frequency'),
                        'power': s.get('power', s.get('level')),
                        'modulation': s.get('modulation'),
                        'band': s.get('band'),
                    }
                    for s in all_rf
                ]

            update_tscm_sweep(
                _current_sweep_id,
                status='completed',
                results={
                    'wifi_devices': wifi_payload,
                    'wifi_clients': wifi_client_payload,
                    'bt_devices': bt_payload,
                    'rf_signals': rf_payload,
                    'wifi_count': len(all_wifi),
                    'wifi_client_count': len(all_wifi_clients),
                    'bt_count': len(all_bt),
                    'rf_count': len(all_rf),
                    'severity_counts': severity_counts,
                    'correlation_summary': findings.get('summary', {}),
                    'identity_summary': identity_summary.get('statistics', {}),
                    'baseline_comparison': baseline_comparison,
                    'results_detail_level': 'full' if verbose_results else 'compact',
                },
                threats_found=threats_found,
                completed=True
            )

            # Emit correlation findings
            _emit_event('correlation_findings', {
                'correlations': correlations,
                'high_interest_count': findings['summary'].get('high_interest', 0),
                'needs_review_count': findings['summary'].get('needs_review', 0),
            })

            # Emit baseline comparison if a baseline was used
            if baseline_comparison:
                _emit_event('baseline_comparison', {
                    'baseline_id': baseline.get('id'),
                    'baseline_name': baseline.get('name'),
                    'total_new': baseline_comparison['total_new'],
                    'total_missing': baseline_comparison['total_missing'],
                    'wifi': baseline_comparison.get('wifi'),
                    'wifi_clients': baseline_comparison.get('wifi_clients'),
                    'bluetooth': baseline_comparison.get('bluetooth'),
                    'rf': baseline_comparison.get('rf'),
                })

            # Emit device identity cluster findings (MAC-randomization resistant)
            _emit_event('identity_clusters', {
                'total_clusters': identity_summary.get('statistics', {}).get('total_clusters', 0),
                'high_risk_count': identity_summary.get('statistics', {}).get('high_risk_count', 0),
                'medium_risk_count': identity_summary.get('statistics', {}).get('medium_risk_count', 0),
                'unique_fingerprints': identity_summary.get('statistics', {}).get('unique_fingerprints', 0),
                'clusters': identity_clusters,
            })

            _emit_event('sweep_completed', {
                'sweep_id': _current_sweep_id,
                'threats_found': threats_found,
                'wifi_count': len(all_wifi),
                'wifi_client_count': len(all_wifi_clients),
                'bt_count': len(all_bt),
                'rf_count': len(all_rf),
                'severity_counts': severity_counts,
                'high_interest_devices': findings['summary'].get('high_interest', 0),
                'needs_review_devices': findings['summary'].get('needs_review', 0),
                'correlations_found': len(correlations),
                'identity_clusters': identity_summary['statistics'].get('total_clusters', 0),
                'baseline_new_devices': baseline_comparison['total_new'] if baseline_comparison else 0,
                'baseline_missing_devices': baseline_comparison['total_missing'] if baseline_comparison else 0,
            })

    except Exception as e:
        logger.error(f"Sweep error: {e}")
        _emit_event('sweep_error', {'error': str(e)})
        if _current_sweep_id:
            update_tscm_sweep(_current_sweep_id, status='error', completed=True)

    finally:
        _sweep_running = False


def _handle_threat(threat: dict) -> None:
    """Handle a detected threat."""
    if not _current_sweep_id:
        return

    # Add to database
    threat_id = add_tscm_threat(
        sweep_id=_current_sweep_id,
        threat_type=threat['threat_type'],
        severity=threat['severity'],
        source=threat['source'],
        identifier=threat['identifier'],
        name=threat.get('name'),
        signal_strength=threat.get('signal_strength'),
        frequency=threat.get('frequency'),
        details=threat.get('details')
    )

    # Emit event
    _emit_event('threat_detected', {
        'threat_id': threat_id,
        **threat
    })

    logger.warning(
        f"TSCM threat detected: {threat['threat_type']} - "
        f"{threat['identifier']} ({threat['severity']})"
    )


def _generate_assessment(summary: dict) -> str:
    """Generate an assessment summary based on findings."""
    high = summary.get('high_interest', 0)
    review = summary.get('needs_review', 0)
    correlations = summary.get('correlations_found', 0)

    if high > 0 or correlations > 0:
        return (
            f"ELEVATED CONCERN: {high} high-interest item(s) and "
            f"{correlations} cross-protocol correlation(s) detected. "
            "Professional TSCM inspection recommended."
        )
    elif review > 3:
        return (
            f"MODERATE CONCERN: {review} items requiring review. "
            "Further analysis recommended to characterize unknown devices."
        )
    elif review > 0:
        return (
            f"LOW CONCERN: {review} item(s) flagged for review. "
            "Likely benign but verification recommended."
        )
    else:
        return (
            "BASELINE ENVIRONMENT: No significant anomalies detected. "
            "Environment appears consistent with expected wireless activity."
        )


# =============================================================================
# Import sub-modules to register routes on tscm_bp
# =============================================================================
from routes.tscm import (
    analysis,  # noqa: E402, F401
    baseline,  # noqa: E402, F401
    cases,  # noqa: E402, F401
    meeting,  # noqa: E402, F401
    schedules,  # noqa: E402, F401
    sweep,  # noqa: E402, F401
)
