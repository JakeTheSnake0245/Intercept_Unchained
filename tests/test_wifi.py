import os
import sys
from unittest.mock import MagicMock, mock_open, patch

import pytest
from flask import Flask

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from routes.wifi import parse_airodump_csv, wifi_bp


@pytest.fixture
def mock_app_module(mocker):
    """Mock the app_module imported inside routes.wifi."""
    mock = mocker.patch("routes.wifi.app_module")
    mock.wifi_lock = MagicMock()
    mock.wifi_process = None
    mock.wifi_monitor_interface = None
    mock.wifi_queue = MagicMock()
    mock.wifi_networks = {}
    mock_app_module.wifi_clients = {}
    return mock

@pytest.fixture
def app():
    app = Flask(__name__)
    app.register_blueprint(wifi_bp)
    return app

@pytest.fixture
def client(app):
    return app.test_client()

def test_parse_airodump_csv(mocker):
    """Test parsing logic for airodump CSV format."""
    csv_content = (
        "BSSID, First time seen, Last time seen, channel, Speed, Privacy, Cipher, Authentication, Power, # beacons, # IV, LAN IP, ID-length, ESSID, Key\n"
        "AA:BB:CC:DD:EE:FF, 2023-01-01, 2023-01-01, 6, 54, WPA2, CCMP, PSK, -50, 10, 5, 0.0.0.0, 7, MyWiFi, \n"
        "\n"
        "Station MAC, First time seen, Last time seen, Power, # packets, BSSID, Probes\n"
        "11:22:33:44:55:66, 2023-01-01, 2023-01-01, -60, 20, AA:BB:CC:DD:EE:FF, MyWiFi\n"
    )

    with patch("builtins.open", mock_open(read_data=csv_content)):
        mocker.patch("routes.wifi.get_manufacturer", return_value="Apple")
        networks, clients = parse_airodump_csv("dummy.csv")

        assert "AA:BB:CC:DD:EE:FF" in networks
        assert networks["AA:BB:CC:DD:EE:FF"]["essid"] == "MyWiFi"
        assert "11:22:33:44:55:66" in clients
        assert clients["11:22:33:44:55:66"]["vendor"] == "Apple"

### --- ROUTE TESTS --- ###

def test_get_interfaces(client, mocker):
    """Test the /interfaces endpoint."""
    mocker.patch("routes.wifi.detect_wifi_interfaces", return_value=[{'name': 'wlan0', 'type': 'managed'}])
    mocker.patch("routes.wifi.check_tool", return_value=True)

    response = client.get('/wifi/interfaces')
    data = response.get_json()

    assert response.status_code == 200
    assert len(data['interfaces']) == 1
    assert data['tools']['airmon'] is True

def test_toggle_monitor_start_success(client, mocker):
    """Test enabling monitor mode via airmon-ng."""
    mocker.patch("routes.wifi.validate_network_interface", return_value="wlan0")
    mocker.patch("routes.wifi.check_tool", return_value=True)
    mock_run = mocker.patch("routes.wifi.subprocess.run")
    mock_run.return_value = MagicMock(stdout="enabled on [phy0]wlan0mon", stderr="", returncode=0)

    with patch("os.path.exists", return_value=True):
        response = client.post('/wifi/monitor', json={'action': 'start', 'interface': 'wlan0'})

        assert response.status_code == 200
        assert response.get_json()['status'] == 'success'
        assert response.get_json()['monitor_interface'] == 'wlan0mon'

def test_start_scan_already_running(client, mock_app_module):
    """Test that we can't start a scan if one is already active."""
    mock_app_module.wifi_process = MagicMock()

    response = client.post('/wifi/scan/start', json={'interface': 'wlan0mon'})
    data = response.get_json()
    assert data['status'] == 'error'
    assert 'already running' in data['message']

def test_start_scan_execution(client, mock_app_module, mocker):
    """Test the full command construction of airodump-ng."""
    mock_app_module.wifi_process = None
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("routes.wifi.get_tool_path", return_value="/usr/bin/airodump-ng")

    mock_popen = mocker.patch("routes.wifi.subprocess.Popen")
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_popen.return_value = mock_proc

    payload = {'interface': 'wlan0mon', 'channel': 6, 'band': 'g'}
    response = client.post('/wifi/scan/start', json=payload)

    assert response.status_code == 200
    assert response.get_json()['status'] == 'started'

    args, _ = mock_popen.call_args
    cmd = args[0]
    assert "-c" in cmd and "6" in cmd
    assert "wlan0mon" in cmd

def test_stop_scan(client, mock_app_module):
    """Test terminating the scanning process."""
    mock_proc = MagicMock()
    mock_app_module.wifi_process = mock_proc

    response = client.post('/wifi/scan/stop')

    assert response.status_code == 200
    assert response.get_json()['status'] == 'stopped'
    mock_proc.terminate.assert_called_once()

def test_send_deauth_success(client, mock_app_module, mocker):
    """Verify deauth command construction and execution."""
    mocker.patch("routes.wifi.check_tool", return_value=True)
    mocker.patch("routes.wifi.get_tool_path", return_value="/usr/bin/aireplay-ng")
    mock_run = mocker.patch("routes.wifi.subprocess.run")
    mock_run.return_value = MagicMock(returncode=0)

    payload = {
        'bssid': 'AA:BB:CC:DD:EE:FF',
        'count': 10,
        'interface': 'wlan0mon'
    }
    response = client.post('/wifi/deauth', json=payload)

    assert response.status_code == 200
    args, _ = mock_run.call_args
    cmd = args[0]
    assert "--deauth" in cmd
    assert "10" in cmd
    assert "AA:BB:CC:DD:EE:FF" in cmd

### --- HANDSHAKE TESTS --- ###

def test_capture_handshake_start(client, mock_app_module, mocker):
    """Test starting airodump-ng for handshake capture."""
    mock_app_module.wifi_process = None
    mocker.patch("routes.wifi.get_tool_path", return_value="/usr/bin/airodump-ng")
    mock_popen = mocker.patch("routes.wifi.subprocess.Popen")

    payload = {'bssid': 'AA:BB:CC:DD:EE:FF', 'channel': '6', 'interface': 'wlan0mon'}
    response = client.post('/wifi/handshake/capture', json=payload)

    assert response.status_code == 200
    assert 'capture_file' in response.get_json()
    assert mock_popen.called

def test_check_handshake_status_found(client, mocker):
    """Verify detection of 'KEY FOUND' in aircrack output."""
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.path.getsize", return_value=1024)
    mocker.patch("routes.wifi.get_tool_path", return_value="aircrack-ng")

    mock_run = mocker.patch("routes.wifi.subprocess.run")
    mock_run.return_value = MagicMock(stdout="WPA (1 handshake)", stderr="", returncode=0)

    payload = {'file': '/tmp/intercept_handshake_test.cap', 'bssid': 'AA:BB:CC:DD:EE:FF'}
    response = client.post('/wifi/handshake/status', json=payload)

    assert response.get_json()['handshake_found'] is True

### --- PMKID TESTS --- ###

def test_capture_pmkid_path_traversal_prevention(client):
    """Ensure the status check rejects invalid paths."""
    payload = {'file': '/etc/passwd'} # Malicious path
    response = client.post('/wifi/pmkid/status', json=payload)

    assert response.status_code == 400
    assert response.get_json()['status'] == 'error'
    assert 'Invalid capture file path' in response.get_json()['message']

### --- CRACKING TESTS --- ###

def test_crack_handshake_success(client, mocker):
    """Test successful password extraction using Regex."""
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("routes.wifi.get_tool_path", return_value="aircrack-ng")

    mock_run = mocker.patch("routes.wifi.subprocess.run")
    # Simulate the actual aircrack-ng success output
    mock_run.return_value = MagicMock(
        stdout="KEY FOUND! [ secret123 ]",
        stderr="",
        returncode=0
    )

    payload = {
        'capture_file': '/tmp/intercept_handshake_test.cap',
        'wordlist': '/home/user/passwords.txt',
        'bssid': 'AA:BB:CC:DD:EE:FF'
    }
    response = client.post('/wifi/handshake/crack', json=payload)

    data = response.get_json()
    assert data['status'] == 'success'
    assert data['password'] == 'secret123'

### --- DATA FETCHING TESTS --- ###

def test_get_wifi_networks(client, mock_app_module):
    """Test that the networks endpoint correctly formats internal data."""
    mock_app_module.wifi_networks = {
        'AA:BB:CC:DD:EE:FF': {'essid': 'Home-WiFi', 'bssid': 'AA:BB:CC:DD:EE:FF'}
    }
    mock_app_module.wifi_handshakes = ['AA:BB:CC:DD:EE:FF']

    response = client.get('/wifi/networks')
    data = response.get_json()

    assert len(data['networks']) == 1
    assert data['networks'][0]['essid'] == 'Home-WiFi'
    assert 'AA:BB:CC:DD:EE:FF' in data['handshakes']
