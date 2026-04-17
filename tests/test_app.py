"""Tests for main application routes."""


def test_index_page(client):
    """Test that index page loads."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    response = client.get("/")
    assert response.status_code == 200
    assert b"INTERCEPT Lab" in response.data


def test_dependencies_endpoint(client):
    """Test dependencies endpoint returns valid JSON."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    response = client.get("/dependencies")
    assert response.status_code == 200
    data = response.get_json()
    assert "modes" in data
    assert "os" in data


def test_devices_endpoint(client):
    """Test devices endpoint returns list."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    response = client.get("/devices")
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)
