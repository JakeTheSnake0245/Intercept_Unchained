"""Comprehensive tests for validation utilities."""

import pytest

from utils.validation import (
    validate_device_index,
    validate_frequency,
    validate_gain,
    validate_rtl_tcp_host,
    validate_rtl_tcp_port,
)


class TestFrequencyValidation:
    """Tests for frequency validation."""

    def test_valid_frequencies(self):
        """Test valid frequency values."""
        assert validate_frequency('152.0') == '152.0'
        assert validate_frequency(152.0) == '152.0'
        assert validate_frequency('1090') == '1090'
        assert validate_frequency(433.92) == '433.92'

    def test_frequency_range(self):
        """Test frequency range limits."""
        # RTL-SDR typical range: 24MHz - 1766MHz
        assert validate_frequency('24') == '24'
        assert validate_frequency('1700') == '1700'

    def test_invalid_frequencies(self):
        """Test invalid frequency values."""
        with pytest.raises(ValueError):
            validate_frequency('')
        with pytest.raises(ValueError):
            validate_frequency('abc')
        with pytest.raises(ValueError):
            validate_frequency(-100)
        with pytest.raises(ValueError):
            validate_frequency(0)


class TestGainValidation:
    """Tests for gain validation."""

    def test_valid_gains(self):
        """Test valid gain values."""
        assert validate_gain('0') == '0'
        assert validate_gain('40') == '40'
        assert validate_gain(49.6) == '49.6'
        assert validate_gain('auto') == 'auto'

    def test_invalid_gains(self):
        """Test invalid gain values."""
        with pytest.raises(ValueError):
            validate_gain(-10)
        with pytest.raises(ValueError):
            validate_gain(100)
        with pytest.raises(ValueError):
            validate_gain('invalid')


class TestDeviceIndexValidation:
    """Tests for device index validation."""

    def test_valid_indices(self):
        """Test valid device indices."""
        assert validate_device_index('0') == '0'
        assert validate_device_index(0) == '0'
        assert validate_device_index('1') == '1'
        assert validate_device_index(3) == '3'

    def test_invalid_indices(self):
        """Test invalid device indices."""
        with pytest.raises(ValueError):
            validate_device_index(-1)
        with pytest.raises(ValueError):
            validate_device_index('abc')
        with pytest.raises(ValueError):
            validate_device_index(100)


class TestRtlTcpHostValidation:
    """Tests for RTL-TCP host validation."""

    def test_valid_hosts(self):
        """Test valid host values."""
        assert validate_rtl_tcp_host('localhost') == 'localhost'
        assert validate_rtl_tcp_host('127.0.0.1') == '127.0.0.1'
        assert validate_rtl_tcp_host('192.168.1.1') == '192.168.1.1'
        assert validate_rtl_tcp_host('server.example.com') == 'server.example.com'

    def test_invalid_hosts(self):
        """Test invalid host values."""
        with pytest.raises(ValueError):
            validate_rtl_tcp_host('')
        with pytest.raises(ValueError):
            validate_rtl_tcp_host('invalid host with spaces')
        with pytest.raises(ValueError):
            validate_rtl_tcp_host('host;rm -rf /')


class TestRtlTcpPortValidation:
    """Tests for RTL-TCP port validation."""

    def test_valid_ports(self):
        """Test valid port values."""
        assert validate_rtl_tcp_port(1234) == 1234
        assert validate_rtl_tcp_port('1234') == 1234
        assert validate_rtl_tcp_port(30003) == 30003
        assert validate_rtl_tcp_port(65535) == 65535

    def test_invalid_ports(self):
        """Test invalid port values."""
        with pytest.raises(ValueError):
            validate_rtl_tcp_port(0)
        with pytest.raises(ValueError):
            validate_rtl_tcp_port(-1)
        with pytest.raises(ValueError):
            validate_rtl_tcp_port(70000)
        with pytest.raises(ValueError):
            validate_rtl_tcp_port('abc')
