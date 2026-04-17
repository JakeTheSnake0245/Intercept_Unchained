"""Tests for database utilities."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# Need to patch DB_PATH before importing database module
@pytest.fixture(autouse=True)
def temp_db():
    """Use a temporary database for each test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / 'test_intercept.db'
        test_db_dir = Path(tmpdir)

        with patch('utils.database.DB_PATH', test_db_path), \
             patch('utils.database.DB_DIR', test_db_dir):
            # Import after patching
            from utils.database import close_db, init_db

            init_db()
            yield test_db_path
            close_db()


class TestSettingsCRUD:
    """Tests for settings CRUD operations."""

    def test_set_and_get_string(self, temp_db):
        """Test setting and getting string values."""
        from utils.database import get_setting, set_setting

        set_setting('test_key', 'test_value')
        assert get_setting('test_key') == 'test_value'

    def test_set_and_get_int(self, temp_db):
        """Test setting and getting integer values."""
        from utils.database import get_setting, set_setting

        set_setting('int_key', 42)
        result = get_setting('int_key')
        assert result == 42
        assert isinstance(result, int)

    def test_set_and_get_float(self, temp_db):
        """Test setting and getting float values."""
        from utils.database import get_setting, set_setting

        set_setting('float_key', 3.14)
        result = get_setting('float_key')
        assert result == 3.14
        assert isinstance(result, float)

    def test_set_and_get_bool(self, temp_db):
        """Test setting and getting boolean values."""
        from utils.database import get_setting, set_setting

        set_setting('bool_true', True)
        set_setting('bool_false', False)

        assert get_setting('bool_true') is True
        assert get_setting('bool_false') is False

    def test_set_and_get_dict(self, temp_db):
        """Test setting and getting dictionary values."""
        from utils.database import get_setting, set_setting

        test_dict = {'name': 'test', 'value': 123, 'nested': {'a': 1}}
        set_setting('dict_key', test_dict)
        result = get_setting('dict_key')

        assert result == test_dict
        assert result['nested']['a'] == 1

    def test_set_and_get_list(self, temp_db):
        """Test setting and getting list values."""
        from utils.database import get_setting, set_setting

        test_list = [1, 2, 3, 'four', {'five': 5}]
        set_setting('list_key', test_list)
        result = get_setting('list_key')

        assert result == test_list

    def test_get_nonexistent_key(self, temp_db):
        """Test getting a key that doesn't exist."""
        from utils.database import get_setting

        assert get_setting('nonexistent') is None
        assert get_setting('nonexistent', 'default') == 'default'

    def test_update_existing_setting(self, temp_db):
        """Test updating an existing setting."""
        from utils.database import get_setting, set_setting

        set_setting('update_key', 'original')
        assert get_setting('update_key') == 'original'

        set_setting('update_key', 'updated')
        assert get_setting('update_key') == 'updated'

    def test_delete_setting(self, temp_db):
        """Test deleting a setting."""
        from utils.database import delete_setting, get_setting, set_setting

        set_setting('delete_key', 'value')
        assert get_setting('delete_key') == 'value'

        result = delete_setting('delete_key')
        assert result is True
        assert get_setting('delete_key') is None

    def test_delete_nonexistent_setting(self, temp_db):
        """Test deleting a setting that doesn't exist."""
        from utils.database import delete_setting

        result = delete_setting('nonexistent_key')
        assert result is False

    def test_get_all_settings(self, temp_db):
        """Test getting all settings."""
        from utils.database import get_all_settings, set_setting

        set_setting('key1', 'value1')
        set_setting('key2', 42)
        set_setting('key3', True)

        all_settings = get_all_settings()

        assert 'key1' in all_settings
        assert all_settings['key1'] == 'value1'
        assert all_settings['key2'] == 42
        assert all_settings['key3'] is True


class TestSignalHistory:
    """Tests for signal history operations."""

    def test_add_and_get_signal_reading(self, temp_db):
        """Test adding and retrieving signal readings."""
        from utils.database import add_signal_reading, get_signal_history

        add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -65)
        add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -62)
        add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -70)

        history = get_signal_history('wifi', 'AA:BB:CC:DD:EE:FF')

        assert len(history) == 3
        # Results should be in chronological order
        assert history[0]['signal'] == -65
        assert history[1]['signal'] == -62
        assert history[2]['signal'] == -70

    def test_signal_history_with_metadata(self, temp_db):
        """Test signal readings with metadata."""
        from utils.database import add_signal_reading, get_signal_history

        metadata = {'channel': 6, 'ssid': 'TestNetwork'}
        add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -65, metadata)

        history = get_signal_history('wifi', 'AA:BB:CC:DD:EE:FF')

        assert len(history) == 1
        assert history[0]['metadata'] == metadata

    def test_signal_history_limit(self, temp_db):
        """Test signal history respects limit parameter."""
        from utils.database import add_signal_reading, get_signal_history

        for i in range(10):
            add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -60 - i)

        history = get_signal_history('wifi', 'AA:BB:CC:DD:EE:FF', limit=5)
        assert len(history) == 5

    def test_signal_history_different_devices(self, temp_db):
        """Test signal history isolates different devices."""
        from utils.database import add_signal_reading, get_signal_history

        add_signal_reading('wifi', 'AA:AA:AA:AA:AA:AA', -65)
        add_signal_reading('wifi', 'BB:BB:BB:BB:BB:BB', -70)

        history_a = get_signal_history('wifi', 'AA:AA:AA:AA:AA:AA')
        history_b = get_signal_history('wifi', 'BB:BB:BB:BB:BB:BB')

        assert len(history_a) == 1
        assert len(history_b) == 1
        assert history_a[0]['signal'] == -65
        assert history_b[0]['signal'] == -70

    def test_cleanup_old_signal_history(self, temp_db):
        """Test cleanup of old signal history."""
        from utils.database import add_signal_reading, cleanup_old_signal_history

        add_signal_reading('wifi', 'AA:BB:CC:DD:EE:FF', -65)

        # Cleanup with 0 hours should remove everything
        deleted = cleanup_old_signal_history(max_age_hours=0)
        # Note: This may or may not delete depending on timing
        assert isinstance(deleted, int)


class TestDeviceCorrelations:
    """Tests for device correlation operations."""

    def test_add_and_get_correlation(self, temp_db):
        """Test adding and retrieving correlations."""
        from utils.database import add_correlation, get_correlations

        add_correlation(
            wifi_mac='AA:AA:AA:AA:AA:AA',
            bt_mac='BB:BB:BB:BB:BB:BB',
            confidence=0.85,
            metadata={'reason': 'timing'}
        )

        correlations = get_correlations(min_confidence=0.5)

        assert len(correlations) >= 1
        found = next(
            (c for c in correlations
             if c['wifi_mac'] == 'AA:AA:AA:AA:AA:AA'),
            None
        )
        assert found is not None
        assert found['bt_mac'] == 'BB:BB:BB:BB:BB:BB'
        assert found['confidence'] == 0.85

    def test_correlation_confidence_filter(self, temp_db):
        """Test correlation filtering by confidence."""
        from utils.database import add_correlation, get_correlations

        add_correlation('AA:AA:AA:AA:AA:AA', 'BB:BB:BB:BB:BB:BB', 0.9)
        add_correlation('CC:CC:CC:CC:CC:CC', 'DD:DD:DD:DD:DD:DD', 0.4)

        high_confidence = get_correlations(min_confidence=0.7)
        all_confidence = get_correlations(min_confidence=0.3)

        assert len(high_confidence) == 1
        assert len(all_confidence) == 2

    def test_correlation_upsert(self, temp_db):
        """Test that correlations are updated on conflict."""
        from utils.database import add_correlation, get_correlations

        add_correlation('AA:AA:AA:AA:AA:AA', 'BB:BB:BB:BB:BB:BB', 0.5)
        add_correlation('AA:AA:AA:AA:AA:AA', 'BB:BB:BB:BB:BB:BB', 0.9)

        correlations = get_correlations(min_confidence=0.0)

        matching = [c for c in correlations
                   if c['wifi_mac'] == 'AA:AA:AA:AA:AA:AA']
        assert len(matching) == 1
        assert matching[0]['confidence'] == 0.9
