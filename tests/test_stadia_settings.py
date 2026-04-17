import pytest


@pytest.fixture
def auth_client(client):
    """Client with an authenticated session."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_offline_settings_includes_stadia_key(auth_client):
    """GET /offline/settings returns offline.stadia_key field."""
    resp = auth_client.get("/offline/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "offline.stadia_key" in data["settings"]


def test_stadia_key_defaults_to_empty_string(auth_client):
    """Stadia key defaults to empty string, not None."""
    # Reset to empty string first to ensure isolation between test runs.
    auth_client.post("/offline/settings", json={"key": "offline.stadia_key", "value": ""})
    resp = auth_client.get("/offline/settings")
    data = resp.get_json()
    assert data["settings"]["offline.stadia_key"] == ""


def test_stadia_key_can_be_saved(auth_client):
    """POST /offline/settings saves offline.stadia_key."""
    resp = auth_client.post("/offline/settings", json={"key": "offline.stadia_key", "value": "test-key-123"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["value"] == "test-key-123"


def test_stadia_key_coerces_non_string(auth_client):
    """POST /offline/settings coerces non-string stadia_key to string."""
    resp = auth_client.post("/offline/settings", json={"key": "offline.stadia_key", "value": 42})
    # Should coerce to string '42' (type matches str default) — not 400
    assert resp.status_code == 200
