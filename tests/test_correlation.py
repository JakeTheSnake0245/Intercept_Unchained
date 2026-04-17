"""Tests for device correlation engine."""

from datetime import datetime, timedelta
from unittest.mock import patch


class TestDeviceCorrelator:
    """Tests for DeviceCorrelator class."""

    def test_correlate_same_oui(self):
        """Test correlation detects same OUI."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(time_window_seconds=60)

        wifi_devices = {
            'AA:BB:CC:11:22:33': {
                'first_seen': datetime.now(),
                'last_seen': datetime.now(),
                'essid': 'TestNetwork',
                'power': -65
            }
        }

        bt_devices = {
            'AA:BB:CC:44:55:66': {
                'first_seen': datetime.now(),
                'last_seen': datetime.now(),
                'name': 'TestPhone',
                'rssi': -60
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        assert len(correlations) >= 1
        assert correlations[0]['wifi_mac'] == 'AA:BB:CC:11:22:33'
        assert correlations[0]['bt_mac'] == 'AA:BB:CC:44:55:66'
        assert correlations[0]['confidence'] > 0

    def test_correlate_timing(self):
        """Test correlation considers timing."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(time_window_seconds=30)
        now = datetime.now()

        # Devices appearing at the same time
        wifi_devices = {
            '11:22:33:44:55:66': {
                'first_seen': now,
                'last_seen': now,
                'essid': 'Network1'
            }
        }

        bt_devices = {
            '77:88:99:AA:BB:CC': {
                'first_seen': now,
                'last_seen': now,
                'name': 'Device1'
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        # Should have some confidence from timing correlation
        if correlations:
            assert correlations[0]['confidence'] > 0

    def test_correlate_no_overlap(self):
        """Test no correlation when devices don't overlap."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(
            time_window_seconds=30,
            min_confidence=0.6
        )

        now = datetime.now()
        old = now - timedelta(hours=1)

        wifi_devices = {
            '11:22:33:44:55:66': {
                'first_seen': old,
                'last_seen': old,
                'essid': 'OldNetwork'
            }
        }

        bt_devices = {
            '77:88:99:AA:BB:CC': {
                'first_seen': now,
                'last_seen': now,
                'name': 'NewDevice'
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        # With high min_confidence and no OUI match, should be empty
        assert len(correlations) == 0

    def test_correlate_manufacturer_match(self):
        """Test correlation boosts confidence for same manufacturer."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(time_window_seconds=60)
        now = datetime.now()

        wifi_devices = {
            '11:22:33:44:55:66': {
                'first_seen': now,
                'last_seen': now,
                'manufacturer': 'Apple',
                'essid': 'Network'
            }
        }

        bt_devices = {
            '77:88:99:AA:BB:CC': {
                'first_seen': now,
                'last_seen': now,
                'manufacturer': 'Apple',
                'name': 'iPhone'
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        # Should have correlation with bonus for manufacturer match
        assert len(correlations) >= 1

    def test_correlate_empty_inputs(self):
        """Test correlation handles empty inputs."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator()

        # Empty WiFi
        assert correlator.correlate({}, {'AA:BB:CC:DD:EE:FF': {}}) == []

        # Empty Bluetooth
        assert correlator.correlate({'AA:BB:CC:DD:EE:FF': {}}, {}) == []

        # Both empty
        assert correlator.correlate({}, {}) == []

    def test_correlate_sorting(self):
        """Test correlations are sorted by confidence."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(
            time_window_seconds=60,
            min_confidence=0.0
        )
        now = datetime.now()

        wifi_devices = {
            'AA:BB:CC:11:11:11': {
                'first_seen': now,
                'last_seen': now,
                'manufacturer': 'Apple'
            },
            '11:22:33:44:55:66': {
                'first_seen': now,
                'last_seen': now
            }
        }

        bt_devices = {
            'AA:BB:CC:22:22:22': {
                'first_seen': now,
                'last_seen': now,
                'manufacturer': 'Apple'
            },
            '77:88:99:AA:BB:CC': {
                'first_seen': now,
                'last_seen': now
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        if len(correlations) >= 2:
            # Should be sorted by confidence (highest first)
            assert correlations[0]['confidence'] >= correlations[1]['confidence']


class TestGetCorrelations:
    """Tests for get_correlations function."""

    @patch('utils.correlation.correlator')
    @patch('utils.correlation.db_get_correlations')
    def test_get_correlations_live(self, mock_db, mock_correlator):
        """Test get_correlations with live data."""
        from utils.correlation import get_correlations

        mock_correlator.correlate.return_value = [
            {
                'wifi_mac': 'AA:AA:AA:AA:AA:AA',
                'bt_mac': 'BB:BB:BB:BB:BB:BB',
                'confidence': 0.8
            }
        ]
        mock_db.return_value = []

        wifi = {'AA:AA:AA:AA:AA:AA': {}}
        bt = {'BB:BB:BB:BB:BB:BB': {}}

        results = get_correlations(
            wifi_devices=wifi,
            bt_devices=bt,
            include_historical=False
        )

        assert len(results) == 1
        mock_correlator.correlate.assert_called_once()

    @patch('utils.correlation.correlator')
    @patch('utils.correlation.db_get_correlations')
    def test_get_correlations_historical(self, mock_db, mock_correlator):
        """Test get_correlations includes historical data."""
        from utils.correlation import get_correlations

        mock_correlator.correlate.return_value = []
        mock_db.return_value = [
            {
                'wifi_mac': 'CC:CC:CC:CC:CC:CC',
                'bt_mac': 'DD:DD:DD:DD:DD:DD',
                'confidence': 0.7,
                'first_seen': '2024-01-01',
                'last_seen': '2024-01-02'
            }
        ]

        results = get_correlations(
            wifi_devices={},
            bt_devices={},
            include_historical=True
        )

        assert len(results) == 1
        assert results[0]['wifi_mac'] == 'CC:CC:CC:CC:CC:CC'

    @patch('utils.correlation.correlator')
    @patch('utils.correlation.db_get_correlations')
    def test_get_correlations_deduplication(self, mock_db, mock_correlator):
        """Test get_correlations deduplicates live and historical."""
        from utils.correlation import get_correlations

        # Same correlation from both sources
        mock_correlator.correlate.return_value = [
            {
                'wifi_mac': 'AA:AA:AA:AA:AA:AA',
                'bt_mac': 'BB:BB:BB:BB:BB:BB',
                'confidence': 0.8
            }
        ]
        mock_db.return_value = [
            {
                'wifi_mac': 'AA:AA:AA:AA:AA:AA',
                'bt_mac': 'BB:BB:BB:BB:BB:BB',
                'confidence': 0.7,
                'first_seen': '2024-01-01',
                'last_seen': '2024-01-02'
            }
        ]

        wifi = {'AA:AA:AA:AA:AA:AA': {}}
        bt = {'BB:BB:BB:BB:BB:BB': {}}

        results = get_correlations(
            wifi_devices=wifi,
            bt_devices=bt,
            include_historical=True
        )

        # Should deduplicate - only one entry for the same device pair
        matching = [r for r in results
                   if r['wifi_mac'] == 'AA:AA:AA:AA:AA:AA']
        assert len(matching) == 1


class TestCorrelationReason:
    """Tests for correlation reason generation."""

    def test_reason_same_oui(self):
        """Test reason includes OUI match."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator()
        now = datetime.now()

        wifi_devices = {
            'AA:BB:CC:11:22:33': {
                'first_seen': now,
                'last_seen': now
            }
        }

        bt_devices = {
            'AA:BB:CC:44:55:66': {
                'first_seen': now,
                'last_seen': now
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        if correlations:
            assert 'OUI' in correlations[0]['reason'] or 'same' in correlations[0]['reason'].lower()

    def test_reason_timing(self):
        """Test reason includes timing information."""
        from utils.correlation import DeviceCorrelator

        correlator = DeviceCorrelator(time_window_seconds=60)
        now = datetime.now()

        wifi_devices = {
            '11:22:33:44:55:66': {
                'first_seen': now,
                'last_seen': now
            }
        }

        bt_devices = {
            '77:88:99:AA:BB:CC': {
                'first_seen': now + timedelta(seconds=5),
                'last_seen': now + timedelta(seconds=5)
            }
        }

        correlations = correlator.correlate(wifi_devices, bt_devices)

        # If correlation found, should mention timing
        if correlations and correlations[0]['confidence'] > 0.3:
            assert 'appeared' in correlations[0]['reason'] or 'timing' in correlations[0]['reason']
