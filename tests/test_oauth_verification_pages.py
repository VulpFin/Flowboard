"""Public-site and OAuth-verification regression coverage."""
from urllib.parse import parse_qs, urlparse

from app.calendars.providers import GOOGLE_CALENDAR_SCOPES
from app.config import settings
from app.services import auth as auth_service


def test_public_pages_are_available_without_login_and_do_not_leak_user_data(client):
    for path, marker in [
        ("/", "Flowboard"),
        ("/privacy", "Privacy Policy"),
        ("/terms", "Terms of Service"),
        ("/third-party-services", "Third-Party Services"),
        ("/oauth-review", "Google Calendar OAuth Review Guide"),
    ]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200
        assert marker in response.text
        assert "alice@example.com" not in response.text


def test_root_is_a_member_dashboard_after_login(client, alice):
    client.login("alice@example.com")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Welcome back" in response.text
    assert "Open my board" in response.text
    assert "Planning workspace" not in response.text


def test_calendar_controls_require_login(client):
    assert client.get("/settings/calendars", follow_redirects=False).status_code == 303
    assert client.get("/calendar/connect/google", follow_redirects=False).status_code == 303


def test_google_connect_url_uses_only_reviewed_calendar_scopes(client, alice, monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "google-client-for-test")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "google-secret-for-test")
    client.login("alice@example.com")
    explainer = client.get("/calendar/connect/google")
    assert explainer.status_code == 200
    assert "Connecting Google Calendar" in explainer.text
    response = client.post("/calendar/connect/google", follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert tuple(query["scope"][0].split()) == GOOGLE_CALENDAR_SCOPES
    assert "openid" not in query["scope"][0]
    assert "email" not in query["scope"][0]
    assert query["code_challenge_method"] == ["S256"]


def test_normal_reviewer_account_has_no_elevated_role_or_feature_gate(db, client):
    reviewer = auth_service.create_user(
        db,
        email="reviewer@example.com",
        username="reviewer",
        password="reviewer-password-123",
        email_verified=True,
    )
    db.commit()
    assert reviewer.is_active and reviewer.email_verified_at is not None
    assert reviewer.is_staff is False
    assert reviewer.profile is not None and reviewer.profile.default_board_id
    client.login("reviewer@example.com", "reviewer-password-123")
    assert client.get("/settings/calendars").status_code == 200


def test_public_integration_page_never_includes_tokens_or_provider_secrets(client):
    response = client.get("/third-party-services")
    assert response.status_code == 200
    assert "GOOGLE_CLIENT_SECRET" not in response.text
    assert "access_token" not in response.text
    assert "refresh_token" not in response.text


def test_privacy_policy_explicitly_discloses_google_data_use_and_sharing(client):
    response = client.get("/privacy")

    assert response.status_code == 200
    assert "Google user data we access" in response.text
    assert "How Flowboard uses Google user data" in response.text
    assert "Sharing, transfer, and disclosure of Google user data" in response.text
    assert "does not sell, rent, or share raw Google Calendar data" in response.text
    assert "Storage, retention, and deletion" in response.text


def test_robots_sitemap_and_public_metadata(client):
    robots = client.get("/robots.txt")
    sitemap = client.get("/sitemap.xml")
    home = client.get("/")

    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert f"Sitemap: {settings.absolute_url('/sitemap.xml')}" in robots.text
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].startswith("application/xml")
    assert f"<loc>{settings.absolute_url('/')}</loc>" in sitemap.text
    assert f"<loc>{settings.absolute_url('/privacy')}</loc>" in sitemap.text
    assert '<meta name="robots" content="index,follow"' in home.text
    assert f'<link rel="canonical" href="{settings.absolute_url("/")}"' in home.text
