# SPDX-License-Identifier: AGPL-3.0-or-later
"""Object-level authorization: users must never reach each other's data."""
import pytest

from app.services import boards as board_service
from app.services import tasks as task_service
from app.services.boards import BoardAccessDenied, BoardNotFound
from app.models import BoardRole, BoardMembership


def test_default_board_created_on_signup(db, alice):
    boards = board_service.list_boards_for_user(db, alice)
    assert len(boards) == 1 and boards[0].slug == "personal"
    assert alice.profile.default_board_id == boards[0].id


def test_slug_uniqueness_per_owner(db, alice, bob):
    a = board_service.create_board(db, alice, name="Work")
    a2 = board_service.create_board(db, alice, name="Work")
    b = board_service.create_board(db, bob, name="Work")
    assert a.slug == "work" and a2.slug == "work-2" and b.slug == "work"


def test_cannot_resolve_other_users_board(db, alice, bob):
    work = board_service.create_board(db, alice, name="Work")
    with pytest.raises(BoardNotFound):
        board_service.get_board_for_user(db, bob, "work")
    with pytest.raises(BoardNotFound):
        board_service.get_board_for_user(db, bob, work.id)  # even by UUID
    assert board_service.get_board_for_user(db, alice, work.id).id == work.id


def test_task_lookup_is_board_scoped(db, alice, bob):
    a_board = board_service.list_boards_for_user(db, alice)[0]
    b_board = board_service.list_boards_for_user(db, bob)[0]
    t = task_service.create_task(db, a_board, {"title": "Alice secret"})
    assert task_service.get_task(db, b_board, t.id) is None
    assert task_service.get_task(db, a_board, t.id) is not None


def test_viewer_role_cannot_edit(db, alice, bob):
    work = board_service.create_board(db, alice, name="Shared")
    db.add(BoardMembership(board_id=work.id, user_id=bob.id, role=BoardRole.VIEWER.value))
    db.flush()
    assert board_service.get_board_for_user(db, bob, work.id, require=BoardRole.VIEWER).id == work.id
    with pytest.raises(BoardAccessDenied):
        board_service.get_board_for_user(db, bob, work.id, require=BoardRole.EDITOR)


def test_http_isolation(client, db, alice, bob):
    a_board = board_service.create_board(db, alice, name="Alice Secret")
    t = task_service.create_task(db, a_board, {"title": "private"})
    db.commit()
    client.login("bob@example.com")
    assert client.get(f"/boards/{a_board.slug}/").status_code == 404
    assert client.get(f"/boards/{a_board.id}/").status_code == 404
    assert client.post(f"/boards/{a_board.slug}/tasks/{t.id}/done", headers={"HX-Request": "true"}).status_code == 404
    assert client.get(f"/boards/{a_board.slug}/tasks/{t.id}.ics").status_code == 404
    # bob's own board works
    b_board = board_service.list_boards_for_user(db, bob)[0]
    assert client.get(f"/boards/{b_board.slug}/").status_code == 200


def test_login_required_redirect(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_csrf_required(client, db, alice):
    client.login("alice@example.com")
    board = board_service.list_boards_for_user(db, alice)[0]
    client.headers.pop("X-CSRF-Token")
    r = client.post(f"/boards/{board.slug}/tasks", data={"title": "x"})
    assert r.status_code == 403
    client.headers["X-CSRF-Token"] = client.csrf
    r = client.post(f"/boards/{board.slug}/tasks", data={"title": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "x" in r.text
