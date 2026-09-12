# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.4: "assigned to you" notices in the morning digest, and the support form."""
from datetime import datetime, timedelta, timezone

import pytest

from app.models import BoardRole, utcnow
from app.services import boards as board_service
from app.services import digest
from app.services import support as support_service
from app.services import tasks as task_service


def _board(db, user, slug="personal"):
    return board_service.get_board_for_user(db, user, slug)


def _sign_in_as(client, email):
    client.cookies.clear()
    client.headers.pop("X-CSRF-Token", None)
    client.login(email)


# --------------------------------------------------------------------------
# assignment notices
# --------------------------------------------------------------------------

def test_assignment_records_who_and_when(db, alice, bob):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    t = task_service.create_task(db, board, {"title": "Hand this over"})
    assert t.assigned_at is None and t.assigned_by_id is None
    task_service.update_task(db, board, t, {"assigned_to_id": bob.id}, actor_id=alice.id)
    assert t.assigned_to_id == bob.id and t.assigned_by_id == alice.id and t.assigned_at is not None
    first = t.assigned_at
    task_service.update_task(db, board, t, {"assigned_to_id": None}, actor_id=alice.id)
    assert t.assigned_at is None and t.assigned_by_id is None
    task_service.update_task(db, board, t, {"assigned_to_id": bob.id}, actor_id=bob.id)
    assert t.assigned_by_id == bob.id and t.assigned_at >= first  # re-assignment re-stamps


def test_digest_reports_what_was_assigned_to_you(db, alice, bob, monkeypatch):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    for b in board_service.list_boards_for_user(db, bob):      # quiet tutorial board
        for t in task_service.list_tasks(db, b, include_done=True):
            task_service.delete_task(db, b, t)
    mine = task_service.create_task(db, board, {"title": "Write the migration", "estimate_min": 60})
    also = task_service.create_task(db, board, {"title": "Review the PR", "estimate_min": 30})
    old = task_service.create_task(db, board, {"title": "Ancient handover", "estimate_min": 15})
    self_assigned = task_service.create_task(db, board, {"title": "I took this myself", "estimate_min": 20})
    for t in (mine, also, old):
        task_service.update_task(db, board, t, {"assigned_to_id": bob.id}, actor_id=alice.id)
    task_service.update_task(db, board, self_assigned, {"assigned_to_id": bob.id}, actor_id=bob.id)
    old.assigned_at = utcnow() - timedelta(days=3)             # before the last digest
    digest.save(db, bob, {"enabled": "on", "hour": "7"})
    digest.mark_sent(db, bob, digest.clock.local_today(bob) - timedelta(days=1))
    db.flush()

    data = digest.gather(db, bob)
    titles = [t.title for t, _b in data["newly_assigned"]]
    assert titles == ["Write the migration", "Review the PR"], "only what someone else gave you since the last digest"
    assert data["has_content"] is True
    lines = "\n".join(digest.assignment_lines(data))
    assert "2 tasks were assigned to you on Personal by alice" in lines
    assert "Write the migration (60 min)" in lines
    body = digest.plain_text(data)
    assert "assigned to you" in body and "I took this myself" not in body
    subject, full, built = digest.build(db, bob, use_ai=False)
    assert "2 newly assigned" in subject and "Write the migration" in full

    # nothing new -> no assignment section at all
    for t in (mine, also):
        t.assigned_at = utcnow() - timedelta(days=3)
    db.flush()
    later = digest.gather(db, bob)
    assert later["newly_assigned"] == [] and digest.assignment_lines(later) == []
    assert "assigned to you" not in digest.plain_text(later)


def test_assignment_alone_is_enough_to_send_an_only_if_due_digest(db, alice, bob, monkeypatch):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    for b in board_service.list_boards_for_user(db, bob):
        for t in task_service.list_tasks(db, b, include_done=True):
            task_service.delete_task(db, b, t)
    digest.save(db, bob, {"enabled": "on", "hour": "7", "only_if_due": "on"})
    assert digest.build(db, bob, use_ai=False) is None          # nothing at all yet
    t = task_service.create_task(db, board, {"title": "Urgent handover"})
    task_service.update_task(db, board, t, {"assigned_to_id": bob.id}, actor_id=alice.id)
    db.flush()
    sent = []
    monkeypatch.setattr(digest.mail_service, "send_mail", lambda to, s, b, **kw: sent.append((to, s)) or True)
    assert digest.send_one(db, bob, force=True)["status"] == "sent"
    assert sent and "1 newly assigned" in sent[0][1]


def test_assign_endpoint_stamps_provenance(client, db, alice, bob):
    board = _board(db, alice)
    board_service.add_member(db, board, bob, BoardRole.EDITOR)
    db.commit()
    _sign_in_as(client, "alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Via the UI"}, headers={"HX-Request": "true"})
    import re

    tid = re.findall(r'data-task="([0-9a-f-]{36})"', r.text)[-1]
    r = client.post(f"/boards/personal/tasks/{tid}/assign", data={"assigned_to_id": bob.id}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    db.expire_all()
    t = task_service.get_task(db, _board(db, alice), tid)
    assert t.assigned_to_id == bob.id and t.assigned_by_id == alice.id and t.assigned_at is not None
    assert t.scheduled_date is None and t.scheduled_start is None


# --------------------------------------------------------------------------
# support form
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_rate_limit():
    support_service._recent.clear()
    yield
    support_service._recent.clear()


def test_support_message_is_sent_with_reply_to_and_diagnostics(client, db, alice, monkeypatch):
    from app import __version__

    sent = {}
    monkeypatch.setattr(support_service.mail_service, "send_mail",
                        lambda to, subject, body, **kw: sent.update({"to": to, "subject": subject, "body": body, **kw}) or True)
    db.commit()
    _sign_in_as(client, "alice@example.com")
    page = client.get("/support?from_page=/boards/personal/")
    assert page.status_code == 200 and "Contact support" in page.text and 'value="alice@example.com"' in page.text

    r = client.post("/support", data={"email": "alice@example.com", "category": "bug", "from_page": "/boards/personal/",
                                      "message": "The Done button does nothing on my phone."})
    assert r.status_code == 200 and "on its way" in r.text
    assert sent["to"] == support_service.settings.support_email
    assert sent["reply_to"] == "alice@example.com"
    assert "Something is broken" in sent["subject"]
    assert "The Done button does nothing" in sent["body"]
    assert f"Flowboard: {__version__}" in sent["body"] and "/boards/personal/" in sent["body"] and "alice" in sent["body"]


def test_support_works_signed_out_and_validates(client, db, monkeypatch):
    sent = []
    monkeypatch.setattr(support_service.mail_service, "send_mail", lambda to, s, b, **kw: sent.append(s) or True)
    page = client.get("/support")
    assert page.status_code == 200 and "Contact support" in page.text
    csrf = page.cookies.get("fb_csrf") or client.cookies.get("fb_csrf")
    data = {"email": "stranger@example.com", "category": "account", "message": "I cannot sign in, the reset email never arrives.", "csrf_token": csrf}
    r = client.post("/support", data=data)
    assert r.status_code == 200 and "on its way" in r.text and len(sent) == 1

    bad = client.post("/support", data={**data, "message": "help"})
    assert bad.status_code == 400 and "sentence or two" in bad.text and len(sent) == 1
    bad = client.post("/support", data={**data, "email": "not-an-address"})
    assert bad.status_code == 400 and "email address" in bad.text and len(sent) == 1
    # the honeypot silently drops bots
    r = client.post("/support", data={**data, "website": "http://spam.example"})
    assert r.status_code == 200 and len(sent) == 1


def test_support_is_rate_limited_and_header_safe(db, alice, monkeypatch):
    sent = []
    monkeypatch.setattr(support_service.mail_service, "send_mail", lambda to, s, b, **kw: sent.append((s, kw)) or True)
    for i in range(support_service.settings.FLOWBOARD_SUPPORT_MAX_PER_HOUR):
        support_service.submit(user=alice, email=alice.email, category="bug", message=f"Report number {i} with detail.")
    with pytest.raises(support_service.SupportError) as exc:
        support_service.submit(user=alice, email=alice.email, category="bug", message="One too many reports today.")
    assert "wait a little" in str(exc.value)
    assert len(sent) == support_service.settings.FLOWBOARD_SUPPORT_MAX_PER_HOUR

    # a newline in the address can never reach a header
    support_service._recent.clear()
    from app.services.mail import _header_safe

    assert _header_safe("a@b.c\r\nBcc: victim@example.com") == "a@b.c Bcc: victim@example.com"
    with pytest.raises(support_service.SupportError):
        support_service.submit(user=None, email="a@b.c\nBcc: victim@example.com", category="bug", message="Injection attempt here.")


def test_backup_command_captures_wal_content(db, alice, tmp_path, capsys):
    """`cp` of the main file can miss everything still in the -wal; the backup
    command must produce a snapshot that has the newest rows."""
    from app.cli import main
    from app.config import settings as cfg
    import sqlite3

    board = _board(db, alice)
    task_service.create_task(db, board, {"title": "Written just before the backup"})
    db.commit()
    dest = tmp_path / "snapshot.sqlite3"
    assert main(["backup", "--to", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "integrity=ok" in out and "sha256=" in out
    assert (tmp_path / "snapshot.sqlite3.sha256").exists()
    c = sqlite3.connect(dest)
    titles = [r[0] for r in c.execute("select title from tasks")]
    assert "Written just before the backup" in titles
    c.close()


def test_footer_links_to_support_on_every_page(client, db, alice):
    db.commit()
    assert 'href="/support' in client.get("/login").text
    _sign_in_as(client, "alice@example.com")
    for path in ("/boards", "/boards/personal/", "/settings/account"):
        assert 'href="/support' in client.get(path).text, path
