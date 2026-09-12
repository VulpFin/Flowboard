# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.3: unambiguous board references for shared boards, and the invitation flow."""
import re

import pytest

from app.models import BoardInvite, BoardRole, Task
from app.services import boards as board_service
from app.services import invites as invite_service
from app.services import tasks as task_service


def _board(db, user, slug="personal"):
    return board_service.get_board_for_user(db, user, slug)


def _owned(db, user, name):
    return board_service.create_board(db, user, name=name)


def _sign_in_as(client, email):
    """conftest's Client.login is a no-op when a session already exists, so a test
    that switches between two people starts from a clean browser instead."""
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.login(email)


# --------------------------------------------------------------------------
# board references
# --------------------------------------------------------------------------

def test_own_slug_wins_over_a_shared_board_with_the_same_slug(db, alice, bob):
    """Both of them have a board called "personal"; alice shares hers with bob.
    /boards/personal/ must stay *bob's own* board, and alice's must still be
    reachable - that is what the qualified reference is for."""
    alice_board = _board(db, alice)
    bob_board = _board(db, bob)
    assert alice_board.slug == bob_board.slug == "personal"
    board_service.add_member(db, alice_board, bob, BoardRole.EDITOR)
    db.flush()

    assert board_service.get_board_for_user(db, bob, "personal").id == bob_board.id
    assert board_service.get_board_for_user(db, bob, f"{alice.username}~personal").id == alice_board.id
    assert board_service.get_board_for_user(db, bob, alice_board.id).id == alice_board.id  # by UUID
    # and the reference each of them should publish
    assert board_service.ref_for(alice_board, alice) == "personal"
    assert board_service.ref_for(alice_board, bob) == f"{alice.username}~personal"
    assert board_service.ref_for(bob_board, bob) == "personal"


def test_qualified_reference_still_enforces_membership(db, alice, bob):
    alice_board = _board(db, alice)
    with pytest.raises(board_service.BoardNotFound):
        board_service.get_board_for_user(db, bob, f"{alice.username}~personal")
    with pytest.raises(board_service.BoardNotFound):
        board_service.get_board_for_user(db, bob, f"{alice.username}~nope")
    with pytest.raises(board_service.BoardNotFound):
        board_service.get_board_for_user(db, bob, "nosuchuser~personal")
    with pytest.raises(board_service.BoardNotFound):
        board_service.get_board_for_user(db, bob, alice_board.id)


def test_pages_link_to_the_qualified_reference(client, db, alice, bob):
    alice_board = _board(db, alice)
    board_service.add_member(db, alice_board, bob, BoardRole.EDITOR)
    task_service.create_task(db, alice_board, {"title": "Shared task"})
    db.commit()
    client.login("bob@example.com")
    ref = f"{alice.username}~personal"
    page = client.get(f"/boards/{ref}/")
    assert page.status_code == 200 and "Shared task" in page.text
    assert f"/boards/{ref}/tasks" in page.text        # forms post to the qualified ref
    assert f'value="{ref}"' in page.text              # board switcher option
    assert "/boards/personal/tasks" not in page.text  # never bob's own board's URL
    # bob's own board is still his at the bare slug
    own = client.get("/boards/personal/")
    assert own.status_code == 200 and "Shared task" not in own.text
    # and the task routes work through the qualified reference
    r = client.post(f"/boards/{ref}/tasks", data={"title": "Added by bob"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Added by bob" in r.text


def test_shared_board_never_becomes_your_default(db, alice, bob):
    from app.services import auth as auth_service

    alice_board = _board(db, alice)
    board_service.add_member(db, alice_board, bob, BoardRole.EDITOR)
    bob.profile.default_board_id = None
    db.flush()
    assert auth_service.ensure_default_board(db, bob).owner_id == bob.id


# --------------------------------------------------------------------------
# invitations
# --------------------------------------------------------------------------

def test_invite_accept_makes_a_member_who_can_be_assigned_work(db, alice, bob, client):
    board = _board(db, alice)
    invite, token = invite_service.create(db, board, alice, "BOB@example.com ", BoardRole.EDITOR, message="join me")
    assert invite.email == "bob@example.com" and invite.token_prefix and invite.is_open
    assert invite.token_hash != token and len(token) >= 40
    assert [i.id for i in invite_service.for_user(db, bob)] == [invite.id]
    assert invite_service.resolve(db, token).id == invite.id
    assert invite_service.resolve(db, "nonsense-token") is None

    membership = invite_service.accept(db, invite, bob)
    assert membership.role == BoardRole.EDITOR.value and invite.state == "accepted"
    assert board_service.get_board_for_user(db, bob, f"{alice.username}~personal").id == board.id
    assert bob.id in {m["id"] for m in board_service.member_list(db, board)}
    # single use
    with pytest.raises(invite_service.InviteError):
        invite_service.accept(db, invite, bob)


def test_invitation_is_bound_to_its_address_and_expires(db, alice, bob):
    from datetime import timedelta

    from app.models import utcnow

    board = _board(db, alice)
    invite, _tok = invite_service.create(db, board, alice, "someone-else@example.com")
    with pytest.raises(invite_service.InviteError) as exc:
        invite_service.accept(db, invite, bob)   # bob may not redeem a link sent elsewhere
    assert "was sent to" in str(exc.value)
    assert board_service.membership_for(db, board, bob) is None

    inv2, _ = invite_service.create(db, board, alice, bob.email)
    inv2.expires_at = utcnow() - timedelta(days=1)
    db.flush()
    assert inv2.state == "expired" and not inv2.is_open
    assert invite_service.for_user(db, bob) == []
    with pytest.raises(invite_service.InviteError):
        invite_service.accept(db, inv2, bob)


def test_reinviting_rotates_the_token_and_members_are_refused(db, alice, bob):
    board = _board(db, alice)
    inv1, tok1 = invite_service.create(db, board, alice, bob.email)
    inv2, tok2 = invite_service.create(db, board, alice, bob.email)
    assert tok1 != tok2 and inv1.state == "revoked" and inv2.is_open
    assert len(invite_service.for_user(db, bob)) == 1
    invite_service.accept(db, inv2, bob)
    with pytest.raises(invite_service.InviteError) as exc:
        invite_service.create(db, board, alice, bob.email)
    assert "already a member" in str(exc.value)
    with pytest.raises(invite_service.InviteError):
        invite_service.create(db, board, alice, alice.email)      # the owner
    with pytest.raises(invite_service.InviteError):
        invite_service.create(db, board, alice, "not-an-email")


def test_invite_endpoints_end_to_end(client, db, alice, bob, monkeypatch):
    sent = []
    monkeypatch.setattr(invite_service.mail_service, "send_mail", lambda to, s, b: sent.append((to, s, b)) or True)
    db.commit()
    _sign_in_as(client, "alice@example.com")
    r = client.post("/boards/personal/members/invite", data={"email": "bob@example.com", "role": "editor", "message": "come help"}, follow_redirects=False)
    assert r.status_code == 303 and "Invitation+sent" in r.headers["location"]
    assert sent and sent[0][0] == "bob@example.com" and "invited you to" in sent[0][1]
    token = re.search(r"/invites/([A-Za-z0-9_-]{20,})", sent[0][2]).group(1)
    page = client.get("/boards/personal/settings")
    assert "Pending invitations" in page.text and "bob@example.com" in page.text

    # alice cannot accept an invitation addressed to bob
    r = client.get(f"/invites/{token}")
    assert r.status_code == 200 and "you are signed in as" in r.text

    _sign_in_as(client, "bob@example.com")
    assert "✉ 1" in client.get("/boards").text          # the nav badge
    assert "personal" in client.get("/invites").text
    landing = client.get(f"/invites/{token}")
    assert "Accept invitation" in landing.text and "come help" in landing.text
    r = client.post(f"/invites/{token}/accept", follow_redirects=False)
    assert r.status_code == 303 and f"/boards/{alice.username}~personal/" in r.headers["location"]
    db.expire_all()
    assert board_service.membership_for(db, _board(db, alice), bob) is not None
    assert client.get(f"/boards/{alice.username}~personal/").status_code == 200
    assert "✉" not in client.get("/boards").text        # badge gone


def test_accept_from_the_invites_list_without_the_link(client, db, alice, bob):
    """Being signed in as the invited address is the same proof the token carries."""
    board = _board(db, alice)
    inv, _token = invite_service.create(db, board, alice, bob.email)
    db.commit()
    _sign_in_as(client, "bob@example.com")
    page = client.get("/invites")
    assert f"/invites/id/{inv.id}/accept" in page.text
    r = client.post(f"/invites/id/{inv.id}/accept", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert board_service.membership_for(db, board, bob) is not None
    # somebody else's invitation is not actionable by id either
    inv2, _ = invite_service.create(db, board, alice, "third@example.com")
    db.commit()
    r = client.post(f"/invites/id/{inv2.id}/accept", follow_redirects=False)
    assert r.status_code == 303 and "not+yours" in r.headers["location"]


def test_decline_and_revoke(client, db, alice, bob, monkeypatch):
    sent = []
    monkeypatch.setattr(invite_service.mail_service, "send_mail", lambda to, s, b: sent.append(b) or True)
    board = _board(db, alice)
    inv, token = invite_service.create(db, board, alice, bob.email)
    db.commit()
    _sign_in_as(client, "bob@example.com")
    r = client.post(f"/invites/{token}/decline", follow_redirects=False)
    assert r.status_code == 303 and "declined" in r.headers["location"]
    db.expire_all()
    assert db.get(BoardInvite, inv.id).state == "declined"
    assert board_service.membership_for(db, board, bob) is None
    # a revoked invitation cannot be opened
    inv2, token2 = invite_service.create(db, board, alice, bob.email)
    db.commit()
    _sign_in_as(client, "alice@example.com")
    r = client.post(f"/boards/personal/invites/{inv2.id}/revoke", follow_redirects=False)
    assert r.status_code == 303
    _sign_in_as(client, "bob@example.com")
    assert "revoked" in client.get(f"/invites/{token2}").text


def test_only_admins_manage_members(client, db, alice, bob, monkeypatch):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    db.commit()
    ref = f"{alice.username}~personal"
    client.login("bob@example.com")
    assert client.get(f"/boards/{ref}/settings").status_code == 403
    assert client.post(f"/boards/{ref}/members/invite", data={"email": "x@example.com"}).status_code == 403
    assert client.post(f"/boards/{ref}/members/{alice.id}/remove").status_code == 403
    # an editor may still leave by themselves
    r = client.post(f"/boards/{ref}/members/leave", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert board_service.membership_for(db, board, bob) is None


def test_role_changes_and_removal_clean_up_assigned_work(db, alice, bob):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    t = task_service.create_task(db, board, {"title": "Bob's job"})
    t.assigned_to_id = bob.id
    t.scheduled_start = "09:00"
    bob.profile.default_board_id = board.id
    db.flush()

    board_service.set_member_role(db, board, bob.id, BoardRole.ADMIN)
    assert board_service.membership_for(db, board, bob).role == "admin"
    with pytest.raises(ValueError):
        board_service.set_member_role(db, board, alice.id, BoardRole.VIEWER)   # the owner
    with pytest.raises(ValueError):
        board_service.set_member_role(db, board, bob.id, BoardRole.OWNER)
    with pytest.raises(ValueError):
        board_service.remove_member(db, board, alice.id)

    board_service.remove_member(db, board, bob.id)
    db.expire_all()
    again = db.get(Task, t.id)
    assert again.assigned_to_id is None and again.scheduled_date is None and again.scheduled_start is None
    assert bob.profile.default_board_id is None
    assert board_service.membership_for(db, board, bob) is None


def test_viewer_cannot_edit_but_can_read(client, db, alice, bob):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.VIEWER)
    task_service.create_task(db, board, {"title": "Read only"})
    db.commit()
    ref = f"{alice.username}~personal"
    client.login("bob@example.com")
    page = client.get(f"/boards/{ref}/")
    assert page.status_code == 200 and "Read only" in page.text
    assert client.post(f"/boards/{ref}/tasks", data={"title": "Nope"}, headers={"HX-Request": "true"}).status_code == 403
