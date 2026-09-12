# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.2: reflection nudges, focus->reflection, calendar-aware capacity, energy
curve (declared + learned), shared-board assignment, morning digest, mobile."""
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models import BoardMembership, BoardRole
from app.services import boards as board_service
from app.services import clock, digest, reflections, schedule, tasks as task_service

REPO = Path(__file__).resolve().parent.parent
MONDAY = date(2026, 9, 14)


def _board(db, user, slug="personal"):
    return board_service.get_board_for_user(db, user, slug)


def _set_schedule(db, user, **over):
    form = {}
    for i in range(7):
        form.update({
            f"enabled_{i}": "on" if i < 5 else "",
            f"start_{i}": "09:00", f"end_{i}": "17:00",
            f"max_min_{i}": "480", f"max_high_min_{i}": "240",
            f"high_end_{i}": over.pop(f"high_end_{i}", "12:00"),
            f"medium_end_{i}": over.pop(f"medium_end_{i}", "15:00"),
        })
    form.update({"auto_rollover": "on", "horizon_days": "14", "calendar_busy": "on"})
    form.update(over)
    return schedule.save_schedule(db, user, form)


def _only_board(db, user, keep):
    """Drop the tutorial board's tasks so day plans in these tests are about the
    tasks the test itself created (the tutorial seeds scheduled work)."""
    for b in board_service.list_boards_for_user(db, user):
        if b.id == keep.id:
            continue
        for t in task_service.list_tasks(db, b, include_done=True):
            task_service.delete_task(db, b, t)
    db.flush()


def _reflect_many(db, user, board, n, *, estimate=30, actual=30, context="Email", energy="medium", hour=None):
    for i in range(n):
        t = task_service.create_task(db, board, {"title": f"{context} {i}", "estimate_min": estimate, "contexts": [context], "energy": energy})
        task_service.complete_task(db, board, t)
        reflections.record(db, user, board, t, actual_min=actual, actual_energy=energy, clarity=4, difficulty=2, hour_of_day=hour)


# --------------------------------------------------------------------------
# 1. reflection nudges
# --------------------------------------------------------------------------

def test_nudge_replaces_the_modal_once_calibrated(db, alice):
    board = _board(db, alice)
    quick = task_service.create_task(db, board, {"title": "Quick mail", "estimate_min": 20, "contexts": ["Email"]})
    # not enough data yet -> always the full questionnaire
    assert reflections.should_ask_full(alice, quick) is True
    _reflect_many(db, alice, board, reflections.NUDGE_AFTER, estimate=30, actual=30, context="Email")
    assert reflections.get_profile(alice)["samples"] >= reflections.NUDGE_AFTER
    assert reflections.should_ask_full(alice, quick) is False  # well calibrated, short, no instructions

    long_task = task_service.create_task(db, board, {"title": "Long", "estimate_min": reflections.LONG_TASK_MIN, "contexts": ["Email"]})
    assert reflections.should_ask_full(alice, long_task) is True
    with_instructions = task_service.create_task(db, board, {"title": "Guided", "estimate_min": 20, "contexts": ["Email"]})
    task_service.update_task(db, board, with_instructions, {"instructions": "1. do it"})
    assert reflections.should_ask_full(alice, with_instructions) is True
    # a context we consistently underestimate keeps asking
    _reflect_many(db, alice, board, 4, estimate=30, actual=90, context="Coding")
    coding = task_service.create_task(db, board, {"title": "Code", "estimate_min": 20, "contexts": ["Coding"]})
    assert reflections.should_ask_full(alice, coding) is True
    # the opt-out puts the questionnaire back for everything
    _set_schedule(db, alice, always_full_reflection="on")
    assert reflections.should_ask_full(alice, quick) is True


def test_nudge_endpoints(client, db, alice):
    board = _board(db, alice)
    _reflect_many(db, alice, board, reflections.NUDGE_AFTER, estimate=30, actual=30, context="Email")
    db.commit()
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Nudge me", "estimate_min": "30"}, headers={"HX-Request": "true"})
    tid = re.findall(r'data-task="([0-9a-f-]{36})"', r.text)[-1]
    r = client.post(f"/boards/personal/tasks/{tid}/done", headers={"HX-Request": "true"})
    assert "reflectnudge" in r.text and "reflectmodal" not in r.text
    assert "took about as long as planned" in r.text

    before = reflections.get_profile(alice)["samples"]
    r = client.post(f"/boards/personal/tasks/{tid}/reflect", data={"quick": "1"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "calibration profile was updated" in r.text
    db.expire_all()
    rows = reflections.list_for_user(db, alice, limit=1)
    assert reflections.get_profile(alice)["samples"] == before + 1
    assert rows[0].actual_min == rows[0].estimate_min == 30  # "as long as planned"
    assert rows[0].hour_of_day is not None

    # "No, tell me more" -> the full form for the same task
    r = client.post(f"/boards/personal/tasks/{tid}/reflect/full", headers={"HX-Request": "true"})
    assert "reflectmodal" in r.text and "How hard was it?" in r.text


# --------------------------------------------------------------------------
# 2. focus timer -> reflection
# --------------------------------------------------------------------------

def test_focus_timer_nudge_records_the_measured_minutes(client, db, alice):
    """A calibrated user gets the nudge, but when the focus timer measured the real
    duration the single click must record *that*, not the estimate."""
    board = _board(db, alice)
    _reflect_many(db, alice, board, reflections.NUDGE_AFTER, estimate=30, actual=30, context="Email")
    db.commit()
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Focus then nudge", "estimate_min": "30"}, headers={"HX-Request": "true"})
    tid = re.findall(r'data-task="([0-9a-f-]{36})"', r.text)[-1]
    r = client.post(f"/boards/personal/tasks/{tid}/done", data={"actual_min": "52"}, headers={"HX-Request": "true"})
    assert "reflectnudge" in r.text and "you focused for 52 min" in r.text
    client.post(f"/boards/personal/tasks/{tid}/reflect", data={"quick": "1", "actual_min": "52"}, headers={"HX-Request": "true"})
    db.expire_all()
    assert reflections.list_for_user(db, alice, limit=1)[0].actual_min == 52
    # and "No, tell me more" carries the measured value into the full form
    r = client.post(f"/boards/personal/tasks/{tid}/reflect/full", data={"actual_min": "52"}, headers={"HX-Request": "true"})
    assert "reflectmodal" in r.text and 'value="52"' in r.text

def test_focus_timer_marks_done_and_prefills_actual_minutes(client, db, alice):
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Focus me", "estimate_min": "30"}, headers={"HX-Request": "true"})
    tid = re.search(r'data-task="([0-9a-f-]{36})"', r.text).group(1)
    r = client.post(f"/boards/personal/tasks/{tid}/done", data={"actual_min": "42"}, headers={"HX-Request": "true"})
    assert "reflectmodal" in r.text and 'value="42"' in r.text and "focus timer: 42 min" in r.text
    db.expire_all()
    t = task_service.get_task(db, _board(db, alice), tid)
    assert t.done and t.last_actual_min == 42
    js = (REPO / "app" / "static" / "app.js").read_text()
    assert "How many minutes did this actually take" not in js  # the old dialog is gone
    assert "Yes, mark done" in js and "htmx.ajax('POST'" in js


# --------------------------------------------------------------------------
# 3. calendar-aware capacity
# --------------------------------------------------------------------------

def _connect_fake_calendar(db, user, monkeypatch, intervals, *, fail=False, counter=None):
    from app.calendars import busy as busy_mod
    from app.calendars import links as cal_links

    class FakeProvider:
        def free_busy(self, token, calendar_id, start, end):
            if counter is not None:
                counter.append(1)
            if fail:
                raise RuntimeError("provider is down")
            return intervals

    monkeypatch.setattr(busy_mod, "get_provider", lambda pid: FakeProvider())
    monkeypatch.setattr(busy_mod, "fresh_token", lambda db, conn, provider=None: {"access_token": "x"})
    return cal_links.store_connection(db, user, "google", {"access_token": "x", "expires_at": 9999999999}, {"email": "a@g", "id": "1"}, "s")


def test_busy_time_is_subtracted_from_capacity_and_cached(db, alice, monkeypatch):
    from app.calendars import busy as busy_mod

    _only_board(db, alice, _board(db, alice))
    calls = []
    utc = timezone.utc
    _connect_fake_calendar(db, alice, monkeypatch, [
        (datetime(2026, 9, 14, 10, 0, tzinfo=utc), datetime(2026, 9, 14, 11, 30, tzinfo=utc)),   # 90 min inside the day
        (datetime(2026, 9, 14, 20, 0, tzinfo=utc), datetime(2026, 9, 14, 21, 0, tzinfo=utc)),    # evening: outside 09-17
        (datetime(2026, 9, 15, 8, 30, tzinfo=utc), datetime(2026, 9, 15, 9, 30, tzinfo=utc)),    # clipped to 30 min
    ], counter=calls)
    sched = _set_schedule(db, alice)
    windows = busy_mod.busy_windows(db, alice, MONDAY, 3, sched)
    assert windows[MONDAY]["minutes"] == 90
    assert windows[MONDAY + timedelta(days=1)]["minutes"] == 30
    assert len(calls) == 1
    busy_mod.busy_windows(db, alice, MONDAY, 3, sched)  # within the 15 min TTL
    assert len(calls) == 1, "busy times must be cached per connection"

    cap = schedule.capacity_for(alice, MONDAY, sched, busy=windows)
    assert cap.max_min == 480 and cap.busy_min == 90 and cap.available_min == 390 and cap.free_min == 390
    # and the day is not planned into the meeting
    t = task_service.create_task(db, _board(db, alice), {"title": "Deep work", "estimate_min": 120, "energy": "medium"})
    monkeypatch.setattr(schedule, "busy_map", lambda *a, **k: windows)
    schedule.auto_schedule(db, alice, _board(db, alice), today=MONDAY)
    assert t.scheduled_date == MONDAY and t.scheduled_start
    start = clock.minutes_of_day(t.scheduled_start)
    assert not (start < 11 * 60 + 30 and start + 120 > 10 * 60), "task was planned on top of the meeting"


def test_busy_failures_degrade_silently(db, alice, monkeypatch):
    _connect_fake_calendar(db, alice, monkeypatch, [], fail=True)
    sched = _set_schedule(db, alice)
    assert schedule.busy_map(db, alice, MONDAY, 3, sched) == {}
    cap = schedule.capacity_for(alice, MONDAY, sched, busy=schedule.busy_map(db, alice, MONDAY, 3, sched))
    assert cap.available_min == 480  # full capacity, no error


def test_busy_can_be_switched_off_per_connection(client, db, alice, monkeypatch):
    conn = _connect_fake_calendar(db, alice, monkeypatch, [])
    assert conn.busy_enabled is True
    db.commit()
    client.login("alice@example.com")
    r = client.post(f"/calendar/connections/{conn.id}/busy", data={}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(type(conn), conn.id).busy_enabled is False
    assert "no+longer+affects" in r.headers["location"]
    page = client.get("/settings/calendars")
    assert "Subtract this calendar" in page.text


# --------------------------------------------------------------------------
# 4. energy curve
# --------------------------------------------------------------------------

def test_declared_curve_defaults_and_blocks():
    blocks = schedule.declared_blocks({"start": "09:00", "end": "17:00", "high_end": "", "medium_end": ""})
    assert blocks[0][2] == "high" and blocks[-1][2] == "low"
    assert blocks[0][0] == 9 * 60 and blocks[-1][1] == 17 * 60
    custom = schedule.declared_blocks({"start": "09:00", "end": "17:00", "high_end": "12:00", "medium_end": "15:00"})
    assert custom == [(540, 720, "high"), (720, 900, "medium"), (900, 1020, "low")]
    assert schedule.level_at(custom, 10 * 60) == "high" and schedule.level_at(custom, 16 * 60) == "low"


def test_auto_schedule_places_high_energy_work_in_high_energy_blocks(db, alice):
    board = _board(db, alice)
    _only_board(db, alice, board)
    _set_schedule(db, alice)
    high = task_service.create_task(db, board, {"title": "Hard thinking", "estimate_min": 60, "energy": "high", "importance": 5})
    low = task_service.create_task(db, board, {"title": "Filing", "estimate_min": 60, "energy": "low", "importance": 1})
    schedule.auto_schedule(db, alice, board, today=MONDAY)
    assert high.scheduled_date == MONDAY and high.scheduled_start == "09:00"
    assert low.scheduled_date == MONDAY
    assert clock.minutes_of_day(low.scheduled_start) >= 15 * 60, "low-energy work belongs in the low-energy block"
    # the slot survives into the ICS export
    from app.calendars import ics

    body = ics.board_ics([high], board)
    assert "BEGIN:VEVENT" in body and "DTSTART:20260914T090000" in body


def test_learned_curve_from_reflections(db, alice, client):
    board = _board(db, alice)
    _set_schedule(db, alice)
    assert reflections.learned_curve(alice) == {}
    _reflect_many(db, alice, board, 8, context="Morning", energy="high", hour=9)
    _reflect_many(db, alice, board, 8, context="Evening", energy="low", hour=16)
    curve = reflections.learned_curve(alice)
    assert curve["samples"] == 16
    assert curve["levels"]["9"] == "high" and curve["levels"]["16"] == "low"
    blocks = schedule.learned_blocks(curve["levels"], {"start": "09:00", "end": "17:00"})
    assert (540, 600, "high") in blocks and (960, 1020, "low") in blocks
    _set_schedule(db, alice, use_learned_curve="on")
    used = schedule.energy_blocks(alice, MONDAY)
    assert used == blocks
    db.commit()
    client.login("alice@example.com")
    page = client.get("/settings/schedule")
    assert "Learned from 16 reflections" in page.text and "Use learned energy curve" in page.text


# --------------------------------------------------------------------------
# 5. shared boards
# --------------------------------------------------------------------------

def _share(db, board, user, role=BoardRole.EDITOR):
    db.add(BoardMembership(board_id=board.id, user_id=user.id, role=role.value, accepted=True))
    db.flush()


def test_shared_board_assignment_uses_the_assignees_capacity(db, alice, bob, client):
    board = _board(db, alice)
    _only_board(db, alice, board)
    bob_board = board_service.create_board(db, bob, name="Bob only")
    _only_board(db, bob, bob_board)
    _share(db, board, bob)
    _set_schedule(db, alice)
    # bob only works Wednesdays, 2 hours
    bob_form = {f"enabled_{i}": ("on" if i == 2 else "") for i in range(7)}
    for i in range(7):
        bob_form.update({f"start_{i}": "09:00", f"end_{i}": "11:00", f"max_min_{i}": "120", f"max_high_min_{i}": "120"})
    bob_form["horizon_days"] = "14"
    schedule.save_schedule(db, bob, bob_form)

    t = task_service.create_task(db, board, {"title": "Shared work", "estimate_min": 60})
    t.assigned_to_id = bob.id
    db.flush()
    schedule.auto_schedule(db, alice, board, today=MONDAY)
    assert t.scheduled_date == MONDAY + timedelta(days=2), "placed on bob's only working day"
    assert schedule.effective_owner_id(t, board) == bob.id

    rows = schedule.free_minutes_by_day(db, bob, MONDAY, 7)
    wednesday = [r for r in rows if r["day"] == MONDAY + timedelta(days=2)][0]
    assert wednesday["free_min"] == 60 and rows[0]["enabled"] is False

    # "who has room this week" shows minutes, never other boards' task titles
    secret = task_service.create_task(db, bob_board, {"title": "Bobs private errand", "estimate_min": 30})
    schedule.auto_schedule(db, bob, bob_board, today=MONDAY)
    db.commit()
    client.login("alice@example.com")
    page = client.get(f"/boards/{board.slug}/schedule?start={MONDAY.isoformat()}&days=7")
    assert page.status_code == 200 and "Who has room this week" in page.text
    assert "bob" in page.text.lower() and secret.title not in page.text

    # the assistant is told about members and their free minutes, ids included
    summary = schedule.team_summary_for_ai(db, board, MONDAY, 7)
    assert bob.id in summary and "free minutes" in summary


def test_assign_endpoint_rejects_non_members(client, db, alice, bob):
    board = _board(db, alice)
    db.commit()
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Assign me"}, headers={"HX-Request": "true"})
    tid = re.search(r'data-task="([0-9a-f-]{36})"', r.text).group(1)
    assert client.post(f"/boards/personal/tasks/{tid}/assign", data={"assigned_to_id": bob.id}, headers={"HX-Request": "true"}).status_code == 400
    _share(db, board, bob)
    db.commit()
    r = client.post(f"/boards/personal/tasks/{tid}/assign", data={"assigned_to_id": bob.id}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "👤" in r.text
    db.expire_all()
    assert task_service.get_task(db, _board(db, alice), tid).assigned_to_id == bob.id


def test_ai_proposal_cannot_assign_to_a_stranger(db, alice, bob):
    from app.ai import assistant

    board = _board(db, alice)
    t = task_service.create_task(db, board, {"title": "Task"})
    ops = assistant._validate_operations(
        [{"op": "update_task", "task_id": t.id, "fields": {"assigned_to_id": bob.id, "estimate_min": 45}}],
        {t.id: t}, {alice.id},
    )
    assert ops[0]["fields"] == {"estimate_min": 45}


# --------------------------------------------------------------------------
# 6. morning digest
# --------------------------------------------------------------------------

def test_digest_timing_respects_the_users_timezone(db, alice):
    alice.profile.timezone = "America/New_York"  # UTC-4 in September
    digest.save(db, alice, {"enabled": "on", "hour": "7"})
    assert digest.load(alice)["enabled"] is True and digest.load(alice)["hour"] == 7
    assert digest.is_due(alice, now=datetime(2026, 9, 14, 11, 5, tzinfo=timezone.utc)) is True   # 07:05 local
    assert digest.is_due(alice, now=datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc)) is False  # 06:30 local
    assert digest.is_due(alice, now=datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)) is False   # 16:00, too late
    digest.mark_sent(db, alice, date(2026, 9, 14))
    assert digest.is_due(alice, now=datetime(2026, 9, 14, 11, 5, tzinfo=timezone.utc)) is False   # once a day
    assert digest.is_due(alice, now=datetime(2026, 9, 15, 11, 5, tzinfo=timezone.utc)) is True


def test_digest_content_and_delivery(db, alice, monkeypatch):
    board = _board(db, alice)
    _only_board(db, alice, board)
    _set_schedule(db, alice, **{f"enabled_{i}": "on" for i in range(7)})  # so "today" is a working day whenever this runs
    today = clock.local_today(alice)
    scheduled = task_service.create_task(db, board, {"title": "Write the report", "estimate_min": 90, "energy": "high"})
    scheduled.scheduled_date, scheduled.scheduled_start = today, "09:00"
    late = task_service.create_task(db, board, {"title": "Overdue thing", "due": "2020-01-01", "estimate_min": 15})
    db.flush()
    digest.save(db, alice, {"enabled": "on", "hour": "7"})

    data = digest.gather(db, alice)
    assert [t.title for t, _b in data["scheduled"]] == ["Write the report"]
    assert [t.title for t, _b in data["overdue"]] == ["Overdue thing"]
    assert data["capacity"].used_min == 90 and data["top"] is not None
    text = digest.plain_text(data)
    assert "Write the report" in text and "Overdue thing" in text and "min free" in text and "Start with:" in text

    subject, body, built = digest.build(db, alice)
    assert "1 scheduled" in subject and "1 overdue" in subject and built["source"] == "plain"  # no AI provider configured

    sent = []
    monkeypatch.setattr(digest.mail_service, "send_mail", lambda to, s, b: sent.append((to, s, b)) or True)
    res = digest.send_one(db, alice, force=True)
    assert res["status"] == "sent" and sent and sent[0][0] == alice.email
    assert digest.load(alice)["last_sent_on"] == today.isoformat()
    assert digest.send_one(db, alice)["status"] == "not-due"  # already sent today


def test_digest_only_if_due_skips_empty_days(db, alice, monkeypatch):
    digest.save(db, alice, {"enabled": "on", "hour": "7", "only_if_due": "on"})
    for t in task_service.list_tasks(db, _board(db, alice)):
        task_service.delete_task(db, _board(db, alice), t)
    for b in board_service.list_boards_for_user(db, alice):
        for t in task_service.list_tasks(db, b):
            task_service.delete_task(db, b, t)
    assert digest.build(db, alice) is None
    sent = []
    monkeypatch.setattr(digest.mail_service, "send_mail", lambda to, s, b: sent.append(to) or True)
    assert digest.send_one(db, alice, force=True)["status"] == "skipped-nothing-due"
    assert sent == []


def test_digest_settings_page_and_cli(client, db, alice, monkeypatch):
    db.commit()
    client.login("alice@example.com")
    page = client.get("/settings/digest")
    assert page.status_code == 200 and "Morning digest" in page.text
    r = client.post("/settings/digest", data={"enabled": "on", "hour": "6", "timezone": "Europe/Berlin", "only_if_due": "on", "channel_email": "on"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    cfg = digest.load(alice)
    assert cfg["enabled"] and cfg["hour"] == 6 and cfg["only_if_due"] and cfg["channels"] == ["email"]
    assert alice.profile.timezone == "Europe/Berlin"
    preview = client.post("/settings/digest/preview")
    assert preview.status_code == 200
    db.commit()

    from app.cli import main

    assert main(["send-digests", "--user", "alice@example.com", "--dry-run", "--no-ai"]) == 0


# --------------------------------------------------------------------------
# 7. mobile layout
# --------------------------------------------------------------------------

def test_mobile_affordances_are_present(client, db, alice):
    db.commit()
    client.login("alice@example.com")
    page = client.get("/boards/personal/")
    assert 'class="linkbtn menutoggle"' in page.text and 'id="addtask"' in page.text
    css = (REPO / "app" / "static" / "styles.css").read_text()
    mobile = css.split("@media (max-width: 700px)")[-1]
    assert "min-height: 44px" in mobile
    assert ".cols { grid-template-columns: 1fr; }" in mobile
    assert ".topextra.open" in mobile and ".reflectcard" in mobile
    js = (REPO / "app" / "static" / "app.js").read_text()
    assert "max-width: 700px" in js and "fb:mobileinit:" in js


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("stop_at", ["0002", "head"])
def test_migrations_apply_to_an_existing_database(tmp_path, stop_at):
    """0003 must apply on top of a 0002 database that already has rows - the
    failure mode that bit 0002 in production (SQLite table rebuilds)."""
    dbfile = tmp_path / f"m-{stop_at}.sqlite3"
    env = dict(os.environ, FLOWBOARD_DATABASE_URL=f"sqlite:///{dbfile}", FLOWBOARD_DATA_DIR=str(tmp_path))

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, capture_output=True, text=True)

    r = alembic("upgrade", stop_at)
    assert r.returncode == 0, r.stderr
    if stop_at == "0002":
        import sqlite3

        con = sqlite3.connect(dbfile)
        con.execute("INSERT INTO users (id, email, username, display_name, is_active, is_staff, state, created_at, updated_at) VALUES ('u1','a@b.c','u','U',1,0,'active',datetime('now'),datetime('now'))")
        con.execute("INSERT INTO user_profiles (id, user_id, timezone, week_start, work_day_minutes, default_ai_model, ai_fallback_enabled, ai_fallback_model, ai_auto_tag_on_create, ai_usage_tracking, calendar_default_duration_min, board_defaults_json, created_at, updated_at, work_schedule_json, calibration_json, tutorial_seeded) VALUES ('p1','u1','UTC',1,480,'',0,'',0,1,60,'{}',datetime('now'),datetime('now'),'{}','{}',0)")
        # a profile pointing at a board that no longer exists: the dangling FK
        con.execute("UPDATE user_profiles SET default_board_id='gone' WHERE id='p1'")
        con.commit()
        con.close()
        r = alembic("upgrade", "head")
        assert r.returncode == 0, r.stderr
        r = alembic("upgrade", "head")  # idempotent
        assert r.returncode == 0, r.stderr

    import sqlite3

    con = sqlite3.connect(dbfile)
    cols = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    prof = {row[1] for row in con.execute("PRAGMA table_info(user_profiles)")}
    conn_cols = {row[1] for row in con.execute("PRAGMA table_info(calendar_connections)")}
    refl = {row[1] for row in con.execute("PRAGMA table_info(task_reflections)")}
    rev = con.execute("select version_num from alembic_version").fetchone()[0]
    con.close()
    if stop_at == "head" or True:
        assert {"scheduled_start", "assigned_to_id"} <= cols
        assert "digest_json" in prof
        assert {"busy_enabled", "busy_cache_json", "busy_fetched_at"} <= conn_cols
        assert "hour_of_day" in refl
        # whatever the newest migration is, "upgrade head" must land on it
        revisions = sorted(re.findall(r"^revision = '(\d+)'", (REPO / "migrations" / "versions" / f.name).read_text(), re.M)[0]
                           for f in (REPO / "migrations" / "versions").glob("[0-9]*.py"))
        assert rev == revisions[-1]
