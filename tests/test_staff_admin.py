"""Regression coverage for staff-only platform account management."""
from app.ai import credentials as credential_service
from app.models import AIProviderCredential, Board, IPBlock, User, UserSession, utcnow
from app.services import auth as auth_service


def make_staff(db, email="staff@example.com", username="staff"):
    user = auth_service.create_user(db, email=email, username=username, password="correct-horse-battery", email_verified=True, is_staff=True)
    db.commit()
    return user


def test_admin_requires_a_staff_session(client):
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_regular_member_cannot_access_admin(client, alice):
    client.login("alice@example.com")
    response = client.get("/admin", headers={"accept": "text/html"})
    assert response.status_code == 403
    assert "Staff access required" in response.text
    assert "alice@example.com" not in response.text


def test_staff_can_find_and_inspect_accounts_without_sensitive_data(db, client, alice):
    staff = make_staff(db)
    client.login(staff.email)
    response = client.get("/admin?query=alice")
    assert response.status_code == 200
    assert "alice@example.com" in response.text
    detail = client.get(f"/admin/users/{alice.id}")
    assert detail.status_code == 200
    assert "Owned boards" in detail.text
    assert "password_hash" not in detail.text
    assert "access_token" not in detail.text
    assert "refresh_token" not in detail.text


def test_staff_can_manage_another_account(db, client, alice):
    staff = make_staff(db)
    alice.email_verified_at = None
    db.commit()
    client.login(staff.email)
    response = client.post(f"/admin/users/{alice.id}", data={"action": "verify_email"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.get(User, alice.id).email_verified_at is not None
    response = client.post(f"/admin/users/{alice.id}", data={"action": "suspend"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    target = db.get(User, alice.id)
    assert target.is_active is False
    assert target.state == "suspended"


def test_staff_cannot_change_own_sign_in_or_staff_access(db, client):
    staff = make_staff(db)
    client.login(staff.email)
    response = client.post(f"/admin/users/{staff.id}", data={"action": "suspend"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Use+account+settings" in response.headers["location"]
    db.expire_all()
    saved = db.get(User, staff.id)
    assert saved.is_active is True
    assert saved.is_staff is True


def test_last_active_staff_cannot_be_removed(db, client):
    staff = make_staff(db)
    second = make_staff(db, email="second@example.com", username="second")
    # A first staff member may remove the second, but the remaining one must be retained.
    client.login(staff.email)
    response = client.post(f"/admin/users/{second.id}", data={"action": "revoke_staff"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.get(User, second.id).is_staff is False


def test_staff_can_sign_out_and_recover_an_account(db, client, alice):
    staff = make_staff(db)
    client.login(staff.email)
    response = client.post(f"/admin/users/{alice.id}", data={"action": "sign_out_all"}, follow_redirects=False)
    assert response.status_code == 303
    response = client.post(f"/admin/users/{alice.id}", data={"action": "delete_account"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    deleted = db.get(User, alice.id)
    assert deleted.is_active is False
    assert deleted.state == "deleted"
    assert deleted.deleted_at is not None
    response = client.post(f"/admin/users/{alice.id}", data={"action": "restore_account"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    restored = db.get(User, alice.id)
    assert restored.is_active is True
    assert restored.deleted_at is None


def test_board_and_credential_removals_are_recoverable(db, client, alice):
    staff = make_staff(db)
    board = db.query(Board).filter_by(owner_id=alice.id).first()
    credential = credential_service.upsert_credential(db, alice, "openai", secrets={"api_key": "sk-test-key-123456"})
    credential_service.delete_credential(db, alice, credential)
    db.commit()
    client.login(staff.email)
    response = client.post(f"/admin/credentials/{credential.id}", data={"action": "restore"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.post("/logout", follow_redirects=False).status_code == 303
    client.headers.pop("X-CSRF-Token", None)
    client.login(alice.email)
    response = client.post(f"/boards/{board.slug}/delete", data={"confirm": board.slug}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Board, board.id).deleted_at is not None
    assert client.post("/logout", follow_redirects=False).status_code == 303
    client.headers.pop("X-CSRF-Token", None)
    client.login(staff.email)
    response = client.post(f"/admin/boards/{board.id}", data={"action": "restore"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Board, board.id).deleted_at is None
    assert db.get(AIProviderCredential, credential.id).deleted_at is None


def test_ip_blocks_deny_the_matching_forwarded_client_address(db, client):
    staff = make_staff(db)
    client.login(staff.email)
    response = client.post("/admin/network/blocks", data={"cidr": "203.0.113.0/24", "reason": "test abuse"}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.query(IPBlock).filter_by(cidr="203.0.113.0/24", is_active=True).one()
    blocked = client.get("/healthz", headers={"x-forwarded-for": "203.0.113.9"})
    assert blocked.status_code == 403


def test_permanent_account_delete_requires_fresh_target_bound_tg11_reauth(db, client, alice):
    staff = make_staff(db)
    alice_id = alice.id
    alice_email = alice.email
    staff.tg11_user_id = "tg11-staff"
    db.commit()
    client.login(staff.email)
    assert client.post(f"/admin/users/{alice_id}", data={"action": "delete_account"}, follow_redirects=False).status_code == 303
    denied = client.post(f"/admin/users/{alice_id}/purge", data={"confirm": alice_email}, follow_redirects=False)
    assert "fresh+TG11+reauthentication" in denied.headers["location"]
    db.expire_all()
    session = db.query(UserSession).filter_by(user_id=staff.id).one()
    session.reauthenticated_at = utcnow()
    session.reauth_target_id = alice_id
    db.commit()
    purged = client.post(f"/admin/users/{alice_id}/purge", data={"confirm": alice_email}, follow_redirects=False)
    assert purged.status_code == 303
    db.expire_all()
    assert db.get(User, alice_id) is None
