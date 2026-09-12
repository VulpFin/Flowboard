# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""AI routes: board assistant (propose / review / apply), provider settings,
model picker data, usage page."""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...ai import assistant, credentials as cred_service, usage as usage_service
from ...ai.client import available_models, resolve_model
from ...ai.registry import PROVIDERS, get_spec, list_specs
from ...ai.types import ProviderError
from ...db import get_db
from ...models import Board, User
from .. import deps

router = APIRouter(tags=["ai"])


# --- assistant ---------------------------------------------------------------

@router.post("/boards/{slug}/ai/ask", dependencies=[Depends(deps.csrf_protect)])
def ai_ask(request: Request, prompt: str = Form(...), model_ref: str = Form(""), history: str = Form("[]"), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    try:
        hist = json.loads(history or "[]")
        if not isinstance(hist, list):
            hist = []
    except ValueError:
        hist = []
    try:
        cs = assistant.propose(db, user, board, prompt, model_ref=model_ref or None, history=hist[-8:])
    except ProviderError as exc:
        return deps.render(request, "ai/_assistant_error.html", {"error": exc.friendly(), "kind": exc.kind, "prompt": prompt}, status_code=200)
    return deps.render(request, "ai/_changeset.html", {"cs": cs, "ops": json.loads(cs.operations_json), "prompt": prompt})


@router.post("/boards/{slug}/ai/changesets/{cs_id}/apply", dependencies=[Depends(deps.csrf_protect)])
async def ai_apply(request: Request, cs_id: str, board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cs = assistant.get_change_set(db, user, board, cs_id)
    if cs is None:
        raise HTTPException(404, "Change set not found")
    form = await request.form()
    selected: Optional[List[int]] = None
    if "op" in form:
        selected = [int(x) for x in form.getlist("op") if str(x).isdigit()]
    result = assistant.apply_change_set(db, user, board, cs, selected)
    resp = deps.render(request, "ai/_changeset.html", {"cs": cs, "ops": json.loads(cs.operations_json), "result": result, "prompt": cs.request_text})
    resp.headers["HX-Trigger"] = "boardChanged"
    return resp


@router.post("/boards/{slug}/ai/changesets/{cs_id}/reject", dependencies=[Depends(deps.csrf_protect)])
def ai_reject(request: Request, cs_id: str, board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cs = assistant.get_change_set(db, user, board, cs_id)
    if cs is None:
        raise HTTPException(404, "Change set not found")
    assistant.reject_change_set(db, cs)
    return deps.render(request, "ai/_changeset.html", {"cs": cs, "ops": json.loads(cs.operations_json), "prompt": cs.request_text})


@router.get("/boards/{slug}/ai/history")
def ai_history(request: Request, board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import select
    from ...models import AIChangeSet

    rows = list(db.scalars(select(AIChangeSet).where(AIChangeSet.board_id == board.id, AIChangeSet.user_id == user.id).order_by(AIChangeSet.created_at.desc()).limit(50)))
    return deps.render(request, "ai/history.html", {"rows": rows}, db=db)


# --- model picker data --------------------------------------------------------

@router.get("/api/ai/models")
def api_models(user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    return JSONResponse({"providers": available_models(db, user)})


# --- provider settings -------------------------------------------------------

@router.get("/settings/ai-providers")
def providers_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    creds = {c.provider: c for c in cred_service.list_credentials(db, user)}
    rows = []
    for spec in list_specs():
        cred = creds.get(spec.id)
        rows.append({"spec": spec, "cred": cred, "models": cred_service.cached_models(cred) if cred else [], "config": cred_service.config_of(cred) if cred else {}})
    rows.sort(key=lambda r: (r["cred"] is None, r["spec"].status == "legacy", r["spec"].name.lower()))
    return deps.render(request, "settings/ai_providers.html", {"rows": rows, "section": "ai-providers"}, db=db)


@router.get("/settings/ai-providers/{provider}")
def provider_form(request: Request, provider: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    try:
        spec = get_spec(provider)
    except KeyError:
        raise HTTPException(404, "Unknown provider")
    cred = cred_service.get_credential(db, user, provider)
    models = cred_service.models_for_credential(db, cred) if cred else []
    groups = {}
    for m in models:
        groups.setdefault(m.family, []).append(m)
    return deps.render(request, "settings/ai_provider_form.html", {"spec": spec, "cred": cred, "config": cred_service.config_of(cred) if cred else {}, "groups": groups, "section": "ai-providers"}, db=db)


@router.post("/settings/ai-providers/{provider}", dependencies=[Depends(deps.csrf_protect)])
async def provider_save(request: Request, provider: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    try:
        spec = get_spec(provider)
    except KeyError:
        raise HTTPException(404, "Unknown provider")
    form = await request.form()
    secrets = {f.key: str(form.get(f"secret_{f.key}", "")) for f in spec.credential_fields}
    config = {f.key: str(form.get(f"config_{f.key}", "")) for f in spec.config_fields}
    try:
        cred = cred_service.upsert_credential(db, user, provider, secrets=secrets, config=config, label=str(form.get("label", "")))
    except ValueError as exc:
        return deps.redirect(f"/settings/ai-providers/{provider}?err={exc}")
    if form.get("default_model") is not None:
        cred.default_model = str(form.get("default_model", ""))[:160]
    result = cred_service.test_credential(db, cred)
    if result["ok"]:
        if not cred.default_model:
            models = cred_service.cached_models(cred)
            chat_models = [m for m in models if "embedding" not in m.tags and "image" not in m.tags and "audio" not in m.tags]
            if chat_models:
                cred.default_model = chat_models[0].id
        if user.profile and not user.profile.default_ai_model and cred.default_model:
            user.profile.default_ai_model = f"{provider}:{cred.default_model}"
        return deps.redirect(f"/settings/ai-providers/{provider}?msg=Saved+and+verified:+{result['message']}")
    return deps.redirect(f"/settings/ai-providers/{provider}?err=Saved,+but+the+test+failed:+{result['message']}")


@router.post("/settings/ai-providers/{provider}/test", dependencies=[Depends(deps.csrf_protect)])
def provider_test(request: Request, provider: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cred = cred_service.get_credential(db, user, provider)
    if cred is None:
        raise HTTPException(404, "No credentials for that provider")
    result = cred_service.test_credential(db, cred)
    if deps.is_htmx(request):
        return deps.render(request, "settings/_provider_status.html", {"cred": cred, "result": result})
    return deps.redirect(f"/settings/ai-providers/{provider}?{'msg' if result['ok'] else 'err'}={result['message']}")


@router.post("/settings/ai-providers/{provider}/toggle", dependencies=[Depends(deps.csrf_protect)])
def provider_toggle(provider: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cred = cred_service.get_credential(db, user, provider)
    if cred is None:
        raise HTTPException(404, "No credentials for that provider")
    cred.enabled = not cred.enabled
    return deps.redirect(f"/settings/ai-providers?msg={get_spec(provider).name}+{'enabled' if cred.enabled else 'disabled'}")


@router.post("/settings/ai-providers/{provider}/default-model", dependencies=[Depends(deps.csrf_protect)])
def provider_default_model(provider: str, default_model: str = Form(""), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cred = cred_service.get_credential(db, user, provider)
    if cred is None:
        raise HTTPException(404, "No credentials for that provider")
    cred.default_model = default_model.strip()[:160]
    return deps.redirect(f"/settings/ai-providers/{provider}?msg=Default+model+saved")


@router.post("/settings/ai-providers/{provider}/delete", dependencies=[Depends(deps.csrf_protect)])
def provider_delete(provider: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    cred = cred_service.get_credential(db, user, provider)
    if cred is None:
        raise HTTPException(404, "No credentials for that provider")
    cred_service.delete_credential(db, user, cred)
    if user.profile and user.profile.default_ai_model.startswith(provider + ":"):
        user.profile.default_ai_model = ""
    return deps.redirect("/settings/ai-providers?msg=Credentials+removed")


# --- usage --------------------------------------------------------------------

@router.get("/settings/ai-usage")
def usage_page(request: Request, days: int = 30, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    return deps.render(request, "settings/ai_usage.html", {"summary": usage_service.usage_summary(db, user, days), "recent": usage_service.recent_usage(db, user, 100), "days": days, "section": "ai-usage"}, db=db)
