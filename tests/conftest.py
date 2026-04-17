"""Pytest configuration and fixtures."""

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from app import app as flask_app
from routes import register_blueprints


@pytest.fixture(scope='session')
def app():
    """Create application for testing."""
    flask_app.config['TESTING'] = True
    # Disable CSRF for tests
    flask_app.config['WTF_CSRF_ENABLED'] = False
    # Register blueprints only if not already registered
    if 'pager' not in flask_app.blueprints:
        register_blueprints(flask_app)
    return flask_app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.Popen and subprocess.run with configurable returns.

    Usage:
        def test_example(mock_subprocess):
            mock_subprocess['run'].return_value.stdout = 'output'
            mock_subprocess['run'].return_value.returncode = 0
    """
    with patch('subprocess.Popen') as mock_popen, \
         patch('subprocess.run') as mock_run:
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        mock_process.stdout = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.pid = 12345
        mock_popen.return_value = mock_process

        mock_run.return_value = MagicMock(
            returncode=0, stdout='', stderr=''
        )

        yield {
            'popen': mock_popen,
            'process': mock_process,
            'run': mock_run,
        }


@pytest.fixture
def mock_sdr_device():
    """Return a mock SDRDevice with configurable type and index.

    Usage:
        def test_example(mock_sdr_device):
            device = mock_sdr_device(device_type='rtlsdr', index=0)
    """
    def _factory(device_type='rtlsdr', index=0):
        device = MagicMock()
        device.device_type = device_type
        device.device_index = index
        device.name = f'Mock {device_type} #{index}'
        device.is_available.return_value = True
        device.build_command.return_value = ['rtl_fm', '-f', '100M']
        return device
    return _factory


@pytest.fixture
def mock_app_state():
    """Patch common app module attributes for route tests.

    Provides mock process, queue, and lock objects on the app module.
    """
    import queue

    import app as app_module

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    mock_queue = queue.Queue()
    mock_lock = MagicMock()

    patches = {
        'current_process': mock_process,
        'pager_queue': mock_queue,
        'pager_lock': mock_lock,
    }
    originals = {}
    for attr, value in patches.items():
        originals[attr] = getattr(app_module, attr, None)
        setattr(app_module, attr, value)

    yield {
        'process': mock_process,
        'queue': mock_queue,
        'lock': mock_lock,
        'module': app_module,
    }

    for attr, orig in originals.items():
        if orig is None:
            with contextlib.suppress(AttributeError):
                delattr(app_module, attr)
        else:
            setattr(app_module, attr, orig)


@pytest.fixture
def mock_check_tool():
    """Patch check_tool() to return True for all tools."""
    with patch('utils.dependencies.check_tool', return_value=True) as mock:
        yield mock


@pytest.fixture
def test_db(tmp_path):
    """Provide an isolated in-memory SQLite database for tests."""
    db_path = tmp_path / 'test.db'
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    yield conn
    conn.close()
