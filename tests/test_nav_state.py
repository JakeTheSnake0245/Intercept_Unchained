"""Tests for nav group localStorage persistence (JS logic verified via structure check)."""


def _logged_in_get(client, path):
    """Make a GET request with a pre-seeded logged-in session."""
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client.get(path)


def test_index_page_includes_nav_state_init(client):
    """nav group init function must be present in the index page."""
    resp = _logged_in_get(client, "/")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "initNavGroupState" in html
    assert "localStorage" in html


def test_nav_groups_have_data_group_attributes(client):
    """Each nav group must have a data-group attribute for state keying."""
    resp = _logged_in_get(client, "/")
    html = resp.data.decode()
    for group in ["signals", "tracking", "space", "wireless", "intel", "system"]:
        assert f'data-group="{group}"' in html, f"Missing data-group={group}"
