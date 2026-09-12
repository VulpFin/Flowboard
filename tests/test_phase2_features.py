# SPDX-License-Identifier: AGPL-3.0-or-later
"""2.1: reflections/calibration, scheduling & rollover, tutorial board,
instructions, new providers, TG11 vault import."""
import json
import re
from datetime import date, timedelta

import httpx

from app.ai.registry import PROVIDERS
from app.services import boards as board_service
from app.services import reflections, schedule, tasks as task_service, tutorial


def _board(db, user, slug="personal"):
    return board_service.get_board_for_user(db, user, slug)


def test_tutorial_seeded_and_recreatable(db, alice, client):
    boards = board_service.list_boards_for_user(db, alice)
    tut = [b for b in boards if b.slug.startswith("getting-started")]
    assert len(tut) == 1 and alice.profile.tutorial_seeded
    ts = task_service.list_tasks(db, tut[0])
    assert len(ts) == len(tutorial.TUTORIAL_TASKS)
    assert ts[1].depends_on == [ts[0].id]  # sequenced
    assert tutorial.seed_tutorial(db, alice).id == tut[0].id  # idempotent
    client.login("alice@example.com")
    r = client.post("/tutorial/recreate", follow_redirects=False)
    assert r.status_code == 303 and "getting-started" in r.headers["location"]
    assert len([b for b in board_service.list_boards_for_user(db, alice) if b.slug.startswith("getting-started")]) == 2


def test_reflection_builds_calibration_and_adjusts_estimates(db, alice):
    board = _board(db, alice)
    for i in range(4):
        t = task_service.create_task(db, board, {"title": f"Coding thing {i}", "estimate_min": 30, "contexts": ["Coding"], "energy": "medium"})
        task_service.complete_task(db, board, t)
        reflections.record(db, alice, board, t, actual_min=60, actual_energy="high", clarity=2, difficulty=4, notes="needed VPN")
    prof = reflections.get_profile(alice)
    assert prof["samples"] == 4 and abs(prof["time_ratio"] - 2.0) < 0.01
    assert prof["energy_delta"] == 1.0 and prof["clarity_avg"] == 2.0
    assert reflections.time_multiplier(alice, "Coding") == 2.0
    assert reflections.adjusted_estimate(alice, 30, "Coding") == 60
    assert reflections.time_multiplier(alice, "Email") == 2.0  # falls back to the global ratio
    text = reflections.prompt_summary(alice)
    assert "2.00×" in text and "Coding" in text and "needed VPN" in text
    # out-of-range answers are ignored, not stored
    t = task_service.create_task(db, board, {"title": "x", "estimate_min": 10})
    r = reflections.record(db, alice, board, t, actual_min=99999, actual_energy="ultra", clarity=9, difficulty=0)
    assert r.actual_min is None and r.actual_energy is None and r.clarity is None and r.difficulty is None


def test_done_shows_reflection_and_reflect_endpoint(client, db, alice):
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Reflect me", "estimate_min": "20"}, headers={"HX-Request": "true"})
    tid = re.search(r'data-task="([0-9a-f-]{36})"', r.text).group(1)
    r = client.post(f"/boards/personal/tasks/{tid}/done", headers={"HX-Request": "true"})
    assert "reflectmodal" in r.text and f"/tasks/{tid}/reflect" in r.text
    r = client.post(f"/boards/personal/tasks/{tid}/reflect", data={"actual_min": "35", "actual_energy": "low", "clarity": "5", "difficulty": "1", "notes": "easy"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "reflectmodal" not in r.text and "calibration profile was updated" in r.text
    db.expire_all()
    assert reflections.get_profile(alice)["samples"] == 1
    # skip records nothing
    r2 = client.post("/boards/personal/tasks", data={"title": "Skip me"}, headers={"HX-Request": "true"})
    tid2 = re.search(r'data-task="([0-9a-f-]{36})"', r2.text).group(1)
    client.post(f"/boards/personal/tasks/{tid2}/done", headers={"HX-Request": "true"})
    r = client.post(f"/boards/personal/tasks/{tid2}/reflect", data={"skip": "1"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    db.expire_all()
    assert reflections.get_profile(alice)["samples"] == 1
    page = client.get("/settings/schedule")
    assert page.status_code == 200 and "Reflections recorded" in page.text


def test_schedule_capacity_rollover_and_pages(client, db, alice):
    board = _board(db, alice)
    today = date(2026, 9, 14)  # a Monday
    # Mon: 60 min, Tue: 60 min, Wed off
    form = {}
    for i in range(7):
        form.update({f"enabled_{i}": "on" if i not in (2, 5, 6) else "", f"start_{i}": "09:00", f"end_{i}": "12:00", f"max_min_{i}": "60", f"max_high_min_{i}": "30"})
    form["days_off"] = "2026-09-17"  # Thursday off explicitly
    form["auto_rollover"] = "on"
    form["horizon_days"] = "14"
    schedule.save_schedule(db, alice, form)
    sched = schedule.load_schedule(alice)
    assert sched["days"]["2"]["enabled"] is False and "2026-09-17" in sched["days_off"]
    assert not schedule.capacity_for(alice, date(2026, 9, 17)).enabled
    a = task_service.create_task(db, board, {"title": "A", "estimate_min": 40, "importance": 5, "energy": "high"})
    b = task_service.create_task(db, board, {"title": "B", "estimate_min": 40, "importance": 4})
    c = task_service.create_task(db, board, {"title": "C", "estimate_min": 20, "importance": 3})
    big = task_service.create_task(db, board, {"title": "Big", "estimate_min": 300, "importance": 1})
    n = schedule.auto_schedule(db, alice, board, today=today)
    assert n == 4
    assert a.scheduled_date == today  # high-energy 40 > max_high 30 ...
    # NOTE: A is high energy 40 min with max_high 30 -> does not fit anywhere -> overflow to first enabled day
    assert b.scheduled_date in (today, today + timedelta(days=1))
    assert c.scheduled_date is not None and c.scheduled_date.weekday() not in (2, 5, 6)
    assert big.scheduled_date is not None  # overflow placement, never lost
    # capacity per day never exceeded for fitting tasks: B (40) and C (20) share a 60-min day at most
    used = {}
    for t in (b, c):
        used[t.scheduled_date] = used.get(t.scheduled_date, 0) + t.estimate_min
    assert all(v <= 60 for v in used.values())
    # rollover: pretend B was scheduled last week
    b.scheduled_date = today - timedelta(days=7)
    db.flush()
    moved = schedule.rollover(db, alice, board, today=today)
    assert moved == 1 and b.scheduled_date >= today and b.rollover_count == 1
    db.commit()
    # pages
    client.login("alice@example.com")
    r = client.get("/boards/personal/schedule?start=2026-09-14&days=7")
    assert r.status_code == 200 and "day off" in r.text and "Big" in r.text
    r = client.post(f"/boards/personal/tasks/{c.id}/schedule", data={"scheduled_date": "2026-09-21"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    db.expire_all()
    assert task_service.get_task(db, board, c.id).scheduled_date == date(2026, 9, 21)
    r = client.post("/boards/personal/schedule/auto", headers={"HX-Request": "true"})
    assert r.status_code == 200
    r = client.get("/settings/schedule")
    assert r.status_code == 200 and "Work schedule" in r.text
    r = client.post("/settings/schedule", data={"enabled_0": "on", "max_min_0": "90", "horizon_days": "10"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert schedule.load_schedule(alice)["days"]["0"]["max_min"] == 90 and schedule.load_schedule(alice)["horizon_days"] == 10


def test_board_partial_is_collapsible_and_ai_context(client, db, alice):
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Collapsible", "estimate_min": "20"}, headers={"HX-Request": "true"})
    assert 'class="colhead"' in r.text and 'class="cardhead"' in r.text and "Auto-schedule" in r.text and "How do I do this?" in r.text
    from app.ai import assistant

    board = _board(db, alice)
    msgs = assistant.build_messages(board, task_service.list_tasks(db, board), "hi", user=alice)
    assert any("Work schedule" in m.content for m in msgs)


def test_instructions_endpoint_with_mock_provider(client, db, alice, monkeypatch):
    from app.ai import credentials as cred_service
    from app.ai.providers.openai_compat import OpenAICompatAdapter
    from app.ai.registry import get_spec

    def responder(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
        body = json.loads(request.content)
        assert "instructions" in body["messages"][0]["content"].lower()
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "1. Open the file\n2. **Edit** it\n3. Save"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": body["model"]})

    monkeypatch.setattr(cred_service, "build_adapter", lambda pid, s, c: OpenAICompatAdapter(get_spec(pid), s, c, http=httpx.Client(transport=httpx.MockTransport(responder))))
    cred = cred_service.upsert_credential(db, alice, "openai", secrets={"api_key": "sk-test"}, config={})
    cred.default_model = "gpt-4o-mini"
    alice.profile.default_ai_model = "openai:gpt-4o-mini"
    db.commit()
    client.login("alice@example.com")
    r = client.post("/boards/personal/tasks", data={"title": "Needs steps"}, headers={"HX-Request": "true"})
    tid = re.search(r'data-task="([0-9a-f-]{36})"', r.text).group(1)
    r = client.post(f"/boards/personal/tasks/{tid}/instructions", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<ol>" in r.text and "<b>Edit</b>" in r.text and f'data-expand="{tid}"' in r.text
    db.expire_all()
    assert task_service.get_task(db, _board(db, alice), tid).instructions.startswith("1. Open")
    r = client.post(f"/boards/personal/tasks/{tid}/instructions/clear", headers={"HX-Request": "true"})
    db.expire_all()
    assert task_service.get_task(db, _board(db, alice), tid).instructions == ""


def test_new_providers_registered():
    for pid in ("azure_openai", "huggingface", "fireworks", "deepinfra", "ollama", "lmstudio", "vllm"):
        assert pid in PROVIDERS
    from app.ai.registry import build_adapter

    az = build_adapter("azure_openai", {"api_key": "k"}, {"base_url": "https://r.openai.azure.com", "deployments": "gpt-4o, o3"})
    assert az.base_url.endswith("/openai/v1") and az._headers()["api-key"] == "k" and "Authorization" not in az._headers()
    assert [m.id for m in az.list_models()] == ["gpt-4o", "o3"]


def test_tg11_vault_import(db, alice):
    from app.identity import tg11_api
    from app.ai import credentials as cred_service

    res = tg11_api.import_vault(db, alice, [
        {"provider": "anthropic", "label": "", "secrets": {"api_key": "sk-ant-1"}, "config": {}},
        {"provider": "unknown_thing", "secrets": {"api_key": "x"}},
        {"provider": "openai", "secrets": {"api_key": ""}},
    ])
    assert res["imported"] == ["anthropic"] and set(res["skipped"]) == {"unknown_thing", "openai"}
    cred = cred_service.get_credential(db, alice, "anthropic")
    assert cred is not None and cred.label == "from TG11 vault"
    assert alice.profile.tg11_vault_synced_at is not None
