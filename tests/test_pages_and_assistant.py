# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke-render every page and exercise the assistant change-set flow with a
mocked OpenAI-compatible provider."""
import json
import re

import httpx
import pytest

from app.ai import credentials as cred_service
from app.ai.providers.openai_compat import OpenAICompatAdapter
from app.services import boards as board_service
from app.services import tasks as task_service


def test_register_login_and_pages(client, db):
    page = client.get("/register")
    csrf = page.cookies.get("fb_csrf")
    r = client.post("/register", data={"email": "new@example.com", "username": "newbie", "password": "a-long-password-1", "password2": "a-long-password-1", "csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    client.login("newbie", "a-long-password-1")
    for url in ["/", "/boards", "/boards/personal/", "/boards/personal/settings", "/boards/personal/activity", "/boards/personal/ai/history",
                "/settings/account", "/settings/ai-providers", "/settings/ai-providers/openai", "/settings/ai-providers/anthropic", "/settings/ai-providers/replicate",
                "/settings/ai-defaults", "/settings/ai-usage", "/settings/calendars", "/settings/security", "/settings/board-defaults", "/api/ai/models", "/boards/personal/export.ics", "/boards/personal/plan.ics", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:300])
    assert client.get("/settings/ai-providers/nope").status_code == 404
    # board CRUD
    r = client.post("/boards", data={"name": "TG11 Work", "icon": "🛠️", "description": "d"}, follow_redirects=False)
    assert r.status_code == 303 and "/boards/tg11-work/" in r.headers["location"]
    r = client.post("/boards/tg11-work/settings", data={"name": "TG11 Work", "description": "", "icon": "", "color": "", "ai_model": "", "default_context": "Coding"}, follow_redirects=False)
    assert r.status_code == 303
    # tasks via HTMX
    r = client.post("/boards/tg11-work/tasks", data={"title": "Write docs", "description": "in Word", "estimate_min": "40", "importance": "4", "due": "2026-12-01", "energy": "high"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Write docs" in r.text and "Docs" in r.text
    tid = re.search(r'data-task="([0-9a-f-]{36})"', r.text).group(1)
    r = client.get(f"/boards/tg11-work/tasks/{tid}/edit")
    assert r.status_code == 200
    r = client.post(f"/boards/tg11-work/tasks/{tid}/edit", data={"title": "Write docs v2", "description": "", "estimate_min": "45", "importance": "5", "due": "2026-12-02 10:00", "energy": "high", "contexts": "Docs, Email", "depends_on": "", "status": "blocked"}, headers={"HX-Request": "true"})
    assert "Write docs v2" in r.text and "blocked" in r.text
    r = client.post("/boards/tg11-work/plan", data={"window": "120"})
    assert r.status_code == 200 and "Write docs v2" in r.text
    r = client.get(f"/boards/tg11-work/tasks/{tid}.ics")
    assert "BEGIN:VEVENT" in r.text
    r = client.post("/boards/tg11-work/tasks/reorder", json={"context": "Email", "order": [tid]})
    assert r.status_code == 200
    r = client.post(f"/boards/tg11-work/tasks/{tid}/done", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Write docs v2" not in r.text
    r = client.get("/boards/tg11-work/activity")
    assert "Completed" in r.text
    # security page actions
    r = client.post("/settings/security/password", data={"current_password": "a-long-password-1", "new_password": "another-long-pass-2", "new_password2": "another-long-pass-2"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert client.get("/settings/security").status_code == 200  # current session survived
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/boards", follow_redirects=False).status_code == 303


def _fake_openai(monkeypatch, responder):
    """Patch adapter construction so the 'openai' provider talks to a mock."""
    import app.ai.credentials as cm

    def build(provider_id, secrets, config):
        from app.ai.registry import get_spec

        return OpenAICompatAdapter(get_spec(provider_id), secrets, config, http=httpx.Client(transport=httpx.MockTransport(responder)))

    monkeypatch.setattr(cm, "build_adapter", build)


def test_provider_settings_flow_and_assistant_changeset(client, db, alice, monkeypatch):
    calls = []

    def responder(request: httpx.Request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-5"}]})
        body = json.loads(request.content)
        tool = (body.get("tools") or [{}])[0].get("function", {}).get("name")
        if tool == "enrich_task":
            args = {"contexts": ["Coding"], "estimate_min": 50, "importance": 4, "energy": "high"}
        else:
            # propose: split task into two subtasks + complete an existing one
            tasks = json.loads(body["messages"][1]["content"].split("Tasks (JSON): ", 1)[1])
            existing = tasks[0]["id"]
            args = {
                "message": "Here is a plan.",
                "operations": [
                    {"op": "create_task", "temp_id": "s1", "fields": {"title": "Sub A", "estimate_min": 20, "due": "2026-12-05"}, "reason": "first"},
                    {"op": "create_task", "temp_id": "s2", "fields": {"title": "Sub B", "depends_on": ["s1"]}},
                    {"op": "update_task", "task_id": existing, "fields": {"importance": 5}},
                    {"op": "delete_task", "task_id": "does-not-exist"},
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 30}, "model": body["model"]})

    _fake_openai(monkeypatch, responder)
    client.login("alice@example.com")
    # add provider through the settings form
    r = client.post("/settings/ai-providers/openai", data={"secret_api_key": "sk-proj-testkey1234", "config_organization": "", "config_project": "", "config_base_url": "", "label": "Test"}, follow_redirects=False)
    assert r.status_code == 303 and "Saved+and+verified" in r.headers["location"], r.headers["location"]
    page = client.get("/settings/ai-providers")
    assert "sk-proj-••••••••1234" in page.text and "sk-proj-testkey1234" not in page.text and "valid" in page.text
    models = client.get("/api/ai/models").json()["providers"]
    assert models[0]["provider"] == "openai" and any(m["ref"] == "openai:gpt-5" for g in models[0]["groups"] for m in g["models"])
    board = board_service.list_boards_for_user(db, alice)[0]
    # AI-enriched task creation
    r = client.post(f"/boards/{board.slug}/tasks", data={"title": "Refactor auth", "description": "", "estimate_min": "30", "importance": "3", "energy": "medium", "use_ai": "on", "use_ai_fields": "on"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Coding" in r.text and "36m" in r.text  # 50 bounded to +20% of 30
    # assistant proposal
    r = client.post(f"/boards/{board.slug}/ai/ask", data={"prompt": "split refactor into subtasks", "history": "[]"})
    assert r.status_code == 200 and "Here is a plan." in r.text and "Sub A" in r.text and "does-not-exist" not in r.text
    cs_id = re.search(r'id="cs-([0-9a-f-]{36})"', r.text).group(1)
    # nothing applied yet
    assert len(task_service.list_tasks(db, board, include_done=True)) == 1
    r = client.post(f"/boards/{board.slug}/ai/changesets/{cs_id}/apply", data={"op": ["0", "1", "2"]})
    assert r.status_code == 200 and "applied 3" in r.text
    db.expire_all()
    tasks = {t.title: t for t in task_service.list_tasks(db, board, include_done=True)}
    assert set(tasks) == {"Refactor auth", "Sub A", "Sub B"}
    assert tasks["Sub B"].depends_on == [tasks["Sub A"].id] and tasks["Sub A"].ai_generated and tasks["Refactor auth"].importance == 5
    # applying twice is a no-op
    r = client.post(f"/boards/{board.slug}/ai/changesets/{cs_id}/apply", data={})
    assert "already processed" in r.text
    # usage recorded
    usage = client.get("/settings/ai-usage").text
    assert "openai:gpt-4o-mini" in usage and "assistant" in usage
    # provider error surfaces as a friendly message
    def broken(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    _fake_openai(monkeypatch, broken)
    r = client.post(f"/boards/{board.slug}/ai/ask", data={"prompt": "hi", "history": "[]"})
    assert r.status_code == 200 and "API key" in r.text and "Open AI providers" in r.text
    # remove credential
    r = client.post("/settings/ai-providers/openai/delete", follow_redirects=False)
    assert r.status_code == 303
    assert cred_service.list_credentials(db, alice) == [] or all(c.provider != "openai" for c in cred_service.list_credentials(db, alice))
