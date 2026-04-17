# tests/test_map_utils.py


def test_map_utils_js_is_served(client):
    """map-utils.js is accessible as a static file."""
    resp = client.get("/static/js/map-utils.js")
    assert resp.status_code == 200
    data = resp.data.decode()
    assert "MapUtils" in data
    assert "MapUtils.init" in data
    assert "addTacticalOverlays" in data


def test_map_utils_css_is_served(client):
    """map-utils.css is accessible as a static file."""
    resp = client.get("/static/css/core/map-utils.css")
    assert resp.status_code == 200
    data = resp.data.decode()
    assert "map-hud-panel" in data


def test_index_includes_map_utils(client):
    """Main SPA index.html loads map-utils.js and uses it for APRS and GPS maps."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "map-utils.js" in html
    assert "MapUtils.init" in html
