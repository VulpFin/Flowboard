# SPDX-License-Identifier: AGPL-3.0-or-later
import json

from app.calendars import feeds, ics
from app.calendars import links as cal_links
from app.models import CalendarConnection
from app.services import boards as board_service
from app.services import tasks as task_service


def test_ics_export_shapes(db, alice):
    board = board_service.list_boards_for_user(db, alice)[0]
    dated = task_service.create_task(db, board, {"title": "Dated, timed", "due": "2026-10-01 14:30", "estimate_min": 45, "contexts": ["Calls"]})
    allday = task_service.create_task(db, board, {"title": "All day", "due": "2026-10-02"})
    todo = task_service.create_task(db, board, {"title": "No date; semicolon, comma"})
    body = ics.board_ics([dated, allday, todo], board)
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert "DTSTART:20261001T134500" in body and "DTEND:20261001T143000" in body
    assert "DTSTART;VALUE=DATE:20261002" in body
    assert "BEGIN:VTODO" in body and "SUMMARY:[General] No date\\; semicolon\\, comma" in body
    assert f"UID:task-{dated.id}@flowboard.fyi" in body
    single = ics.task_ics(dated, board)
    assert single.count("BEGIN:VEVENT") == 1


def test_feed_token_security(client, db, alice, bob):
    board = board_service.list_boards_for_user(db, alice)[0]
    task_service.create_task(db, board, {"title": "Feed me", "due": "2026-10-03"})
    row, token = feeds.create_or_rotate_feed(db, alice, board)
    db.commit()
    assert row.token_hash != token and len(token) >= 40
    r = client.get(f"/calendar/feed/{token}.ics")
    assert r.status_code == 200 and "Feed me" in r.text and r.headers["content-type"].startswith("text/calendar")
    assert client.get("/calendar/feed/not-a-real-token-at-all-really.ics").status_code == 404
    # rotate => old token dead
    _, token2 = feeds.create_or_rotate_feed(db, alice, board)
    db.commit()
    assert client.get(f"/calendar/feed/{token}.ics").status_code == 404
    assert client.get(f"/calendar/feed/{token2}.ics").status_code == 200
    feeds.revoke_feed(db, feeds.get_feed(db, alice, board))
    db.commit()
    assert client.get(f"/calendar/feed/{token2}.ics").status_code == 404
    # bob cannot rotate alice's board feed via HTTP
    client.login("bob@example.com")
    assert client.post(f"/boards/{board.id}/feed/rotate", data={}).status_code == 404


def test_calendar_connection_ownership_and_token_encryption(db, alice, bob):
    conn = cal_links.store_connection(db, alice, "google", {"access_token": "ya29.secret", "refresh_token": "1//r", "expires_at": 9999999999}, {"email": "a@gmail.com", "id": "123"}, "scope")
    assert b"ya29" not in conn.token_blob
    assert cal_links.get_connection(db, bob, conn.id) is None
    assert cal_links.get_connection(db, alice, conn.id).id == conn.id
    assert cal_links._token(conn)["access_token"] == "ya29.secret"


def test_push_and_sync_with_fake_provider(db, alice, monkeypatch):
    from app.calendars import providers as prov_mod

    events = {}

    class FakeProvider(prov_mod.CalendarProvider):
        id = "google"
        name = "Fake"

        def list_calendars(self, token):
            return [{"id": "primary", "name": "Primary", "primary": True}]

        def create_event(self, token, calendar_id, ev):
            events["e1"] = ev
            return {"id": "e1", "url": "https://cal/e1"}

        def update_event(self, token, calendar_id, event_id, ev):
            events[event_id] = ev

        def delete_event(self, token, calendar_id, event_id):
            events.pop(event_id, None)

    monkeypatch.setattr(cal_links, "get_provider", lambda pid: FakeProvider())
    board = board_service.list_boards_for_user(db, alice)[0]
    t = task_service.create_task(db, board, {"title": "Dentist", "due": "2026-10-05 09:00", "estimate_min": 30})
    conn = cal_links.store_connection(db, alice, "google", {"access_token": "x", "expires_at": 9999999999}, {"email": "a", "id": "1"}, "s")
    link = cal_links.push_task(db, alice, board, t, conn)
    assert link.external_event_id == "e1" and link.sync_status == "synced" and events["e1"]["summary"] == "[General] Dentist"
    task_service.update_task(db, board, t, {"title": "Dentist (moved)", "due": "2026-10-06 09:00"})
    cal_links.sync_task_links(db, t, board)
    assert events["e1"]["summary"] == "[General] Dentist (moved)"
    task_service.update_task(db, board, t, {"due": None})
    cal_links.sync_task_links(db, t, board)
    assert "e1" not in events and cal_links.links_for_task(db, t)[0].sync_status == "orphaned"


def test_legacy_import_creates_default_board_tasks(db, alice, tmp_path):
    from app.cli import main

    legacy = {
        "a0b1c2d3": {"id": "a0b1c2d3", "title": "Dump refs", "description": "d", "estimate_min": 30, "importance": 3, "due": None, "contexts": ["Inbox"], "energy": "low", "depends_on": [], "created": "2025-10-13 10:10", "done": False, "manual_order": 0, "last_actual_min": None, "completions": 0},
        "b1c2d3e4": {"id": "b1c2d3e4", "title": "Create parts bins", "description": "", "estimate_min": 25, "importance": 4, "due": "2025-11-01", "contexts": ["Lab", "Errand"], "energy": "medium", "depends_on": ["a0b1c2d3"], "created": "2025-10-13 10:10", "done": True, "manual_order": 1, "last_actual_min": 20, "completions": 1},
    }
    f = tmp_path / "flowboard_tasks.json"
    f.write_text(json.dumps(legacy))
    db.commit()
    assert main(["import-legacy", "--file", str(f), "--user", "alice@example.com"]) == 0
    assert main(["import-legacy", "--file", str(f), "--user", "alice@example.com"]) == 0  # idempotent
    db.expire_all()
    board = board_service.list_boards_for_user(db, alice)[0]
    tasks = {t.legacy_id: t for t in task_service.list_tasks(db, board, include_done=True)}
    assert set(tasks) == {"a0b1c2d3", "b1c2d3e4"}
    b = tasks["b1c2d3e4"]
    assert b.done and b.completions == 1 and b.last_actual_min == 20 and b.contexts == ["Lab", "Errand"]
    assert b.depends_on == [tasks["a0b1c2d3"].id]
    assert b.created_at.strftime("%Y-%m-%d %H:%M") == "2025-10-13 10:10"
    assert task_service.get_task(db, board, "a0b1c2d3").id == tasks["a0b1c2d3"].id  # legacy id lookup still works
