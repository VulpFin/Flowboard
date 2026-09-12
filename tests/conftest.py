# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="fbtest-")
os.environ.update({
    "FLOWBOARD_ENV": "test",
    "FLOWBOARD_DATA_DIR": _tmp,
    "FLOWBOARD_SECRET_KEY": "test-secret-key-not-for-prod-0123456789",
    "FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    "FLOWBOARD_SITE_URL": "http://testserver",
    "FLOWBOARD_COOKIE_SECURE": "false",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402,F401
from app.main import app  # noqa: E402
from app.services import auth as auth_service  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


def make_user(db, email="alice@example.com", username="alice", password="correct-horse-battery"):
    u = auth_service.create_user(db, email=email, username=username, password=password, email_verified=True)
    db.commit()
    return u


@pytest.fixture
def alice(db):
    return make_user(db)


@pytest.fixture
def bob(db):
    return make_user(db, email="bob@example.com", username="bob")


class Client(TestClient):
    """TestClient that logs in and auto-attaches the CSRF token."""

    def refresh_csrf(self):
        import re

        home = self.get("/boards")
        m = re.search(r'data-csrf="([^"]+)"', home.text)
        self.csrf = m.group(1) if m else ""
        self.headers["X-CSRF-Token"] = self.csrf

    def login(self, identifier, password="correct-horse-battery"):
        page = self.get("/login", follow_redirects=False)
        if page.status_code == 200:
            csrf = page.cookies.get("fb_csrf")
            r = self.post("/login", data={"identifier": identifier, "password": password, "csrf_token": csrf, "next": "/"}, follow_redirects=False)
            assert r.status_code == 303, r.text
        else:
            r = page  # already signed in (e.g. right after registering)
        self.refresh_csrf()
        return r


@pytest.fixture
def client():
    with Client(app, base_url="http://testserver") as c:
        yield c
