# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""External calendar providers (Google Calendar, Microsoft Graph).

Each provider implements `CalendarProvider`:
    authorize_url(state, code_verifier) -> str
    exchange_code(code, code_verifier) -> token dict
    refresh(token) -> token dict
    account_info(token) -> {email, id}
    list_calendars(token) -> [{id, name, primary}]
    create_event(token, calendar_id, event) -> {id, url}
    update_event(token, calendar_id, event_id, event) -> None
    delete_event(token, calendar_id, event_id) -> None

`event` is a provider-neutral dict built by `links.event_payload`:
    {summary, description, start: datetime|date, end: datetime|date, all_day: bool}

Minimal scopes are requested: Google `calendar.events` + `calendar.readonly`
(to list calendars); Microsoft `Calendars.ReadWrite offline_access User.Read`.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from ..config import settings


class CalendarProviderError(Exception):
    def __init__(self, message: str, *, reauth: bool = False):
        super().__init__(message)
        self.reauth = reauth


def _rfc3339(dt: datetime) -> str:
    """UTC RFC3339 timestamp ("2026-09-12T00:00:00Z") for provider queries.
    Naive datetimes are assumed to already be UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pkce_pair() -> (str, str):
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code == 401:
        raise CalendarProviderError(f"{what}: authorisation expired", reauth=True)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
            msg = (detail.get("error") or {}).get("message") if isinstance(detail.get("error"), dict) else detail.get("error_description") or detail.get("error") or resp.text[:200]
        except ValueError:
            msg = resp.text[:200]
        raise CalendarProviderError(f"{what}: {msg}")
    return resp.json() if resp.content else {}


class CalendarProvider:
    id = ""
    name = ""

    def __init__(self, http: Optional[httpx.Client] = None):
        self.http = http or httpx.Client(timeout=30.0)

    @property
    def configured(self) -> bool:
        return False

    @property
    def redirect_uri(self) -> str:
        return settings.absolute_url(f"/calendar/connect/{self.id}/callback")

    def free_busy(self, token: Dict[str, Any], calendar_id: str, start: datetime, end: datetime) -> List[tuple]:
        """[(start_utc, end_utc)] the account is busy in.  Used to subtract
        meetings from the daily capacity; providers that cannot answer return []."""
        return []

    # token helpers
    @staticmethod
    def token_expired(token: Dict[str, Any]) -> bool:
        exp = token.get("expires_at")
        return not exp or datetime.utcnow().timestamp() > float(exp) - 60

    @staticmethod
    def _with_expiry(tok: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        out = dict(previous or {})
        out.update(tok)
        if "expires_in" in tok:
            out["expires_at"] = datetime.utcnow().timestamp() + float(tok["expires_in"])
        if previous and not tok.get("refresh_token") and previous.get("refresh_token"):
            out["refresh_token"] = previous["refresh_token"]
        return out


# --------------------------------------------------------------------------
class GoogleCalendarProvider(CalendarProvider):
    id = "google"
    name = "Google Calendar"
    scopes = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly openid email"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    api = "https://www.googleapis.com/calendar/v3"

    @property
    def configured(self) -> bool:
        return settings.google_configured

    def authorize_url(self, state: str, code_challenge: str) -> str:
        q = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.auth_url}?{urlencode(q)}"

    def exchange_code(self, code: str, code_verifier: str) -> Dict[str, Any]:
        resp = self.http.post(self.token_url, data={
            "code": code, "client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier,
        })
        return self._with_expiry(_check(resp, "Google token exchange"))

    def refresh(self, token: Dict[str, Any]) -> Dict[str, Any]:
        if not token.get("refresh_token"):
            raise CalendarProviderError("no refresh token - reconnect Google", reauth=True)
        resp = self.http.post(self.token_url, data={
            "refresh_token": token["refresh_token"], "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET, "grant_type": "refresh_token",
        })
        if resp.status_code == 400:
            raise CalendarProviderError("Google refresh token revoked - reconnect", reauth=True)
        return self._with_expiry(_check(resp, "Google token refresh"), token)

    def _h(self, token: Dict[str, Any]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token.get('access_token', '')}"}

    def account_info(self, token: Dict[str, Any]) -> Dict[str, str]:
        resp = self.http.get("https://openidconnect.googleapis.com/v1/userinfo", headers=self._h(token))
        data = _check(resp, "Google userinfo")
        return {"email": data.get("email", ""), "id": data.get("sub", "")}

    def list_calendars(self, token: Dict[str, Any]) -> List[Dict[str, Any]]:
        resp = self.http.get(f"{self.api}/users/me/calendarList", headers=self._h(token), params={"minAccessRole": "writer"})
        items = _check(resp, "Google calendar list").get("items", [])
        return [{"id": c["id"], "name": c.get("summary", c["id"]), "primary": bool(c.get("primary"))} for c in items]

    def _body(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        if ev["all_day"]:
            start = {"date": ev["start"].isoformat()}
            end = {"date": ev["end"].isoformat()}
        else:
            start = {"dateTime": ev["start"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": ev.get("timezone", "UTC")}
            end = {"dateTime": ev["end"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": ev.get("timezone", "UTC")}
        return {"summary": ev["summary"], "description": ev.get("description", ""), "start": start, "end": end, "extendedProperties": {"private": {"flowboard_task": ev.get("task_id", "")}}}

    def create_event(self, token, calendar_id, ev) -> Dict[str, str]:
        resp = self.http.post(f"{self.api}/calendars/{calendar_id}/events", headers=self._h(token), json=self._body(ev))
        data = _check(resp, "Google create event")
        return {"id": data["id"], "url": data.get("htmlLink", "")}

    def update_event(self, token, calendar_id, event_id, ev) -> None:
        resp = self.http.patch(f"{self.api}/calendars/{calendar_id}/events/{event_id}", headers=self._h(token), json=self._body(ev))
        if resp.status_code in (404, 410):
            raise CalendarProviderError("event no longer exists", reauth=False)
        _check(resp, "Google update event")

    def delete_event(self, token, calendar_id, event_id) -> None:
        resp = self.http.delete(f"{self.api}/calendars/{calendar_id}/events/{event_id}", headers=self._h(token))
        if resp.status_code not in (204, 200, 404, 410):
            _check(resp, "Google delete event")

    def free_busy(self, token, calendar_id, start: datetime, end: datetime) -> List[tuple]:
        from .busy import parse_dt

        body = {"timeMin": _rfc3339(start), "timeMax": _rfc3339(end), "items": [{"id": calendar_id or "primary"}]}
        data = _check(self.http.post(f"{self.api}/freeBusy", headers=self._h(token), json=body), "Google free/busy")
        out = []
        for cal in (data.get("calendars") or {}).values():
            for slot in cal.get("busy") or []:
                s, e = parse_dt(slot.get("start")), parse_dt(slot.get("end"))
                if s and e:
                    out.append((s, e))
        return out


# --------------------------------------------------------------------------
class MicrosoftCalendarProvider(CalendarProvider):
    id = "microsoft"
    name = "Microsoft 365 / Outlook"
    scopes = "offline_access User.Read Calendars.ReadWrite"
    api = "https://graph.microsoft.com/v1.0"

    @property
    def configured(self) -> bool:
        return settings.microsoft_configured

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{settings.MICROSOFT_TENANT_ID or 'common'}/oauth2/v2.0"

    def authorize_url(self, state: str, code_challenge: str) -> str:
        q = {
            "client_id": settings.MICROSOFT_CLIENT_ID, "response_type": "code", "redirect_uri": self.redirect_uri,
            "response_mode": "query", "scope": self.scopes, "state": state,
            "code_challenge": code_challenge, "code_challenge_method": "S256",
        }
        return f"{self._authority}/authorize?{urlencode(q)}"

    def exchange_code(self, code: str, code_verifier: str) -> Dict[str, Any]:
        resp = self.http.post(f"{self._authority}/token", data={
            "client_id": settings.MICROSOFT_CLIENT_ID, "client_secret": settings.MICROSOFT_CLIENT_SECRET, "code": code,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": code_verifier, "scope": self.scopes,
        })
        return self._with_expiry(_check(resp, "Microsoft token exchange"))

    def refresh(self, token: Dict[str, Any]) -> Dict[str, Any]:
        if not token.get("refresh_token"):
            raise CalendarProviderError("no refresh token - reconnect Microsoft", reauth=True)
        resp = self.http.post(f"{self._authority}/token", data={
            "client_id": settings.MICROSOFT_CLIENT_ID, "client_secret": settings.MICROSOFT_CLIENT_SECRET,
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token", "scope": self.scopes,
        })
        if resp.status_code == 400:
            raise CalendarProviderError("Microsoft refresh token invalid - reconnect", reauth=True)
        return self._with_expiry(_check(resp, "Microsoft token refresh"), token)

    def _h(self, token) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token.get('access_token', '')}"}

    def account_info(self, token) -> Dict[str, str]:
        data = _check(self.http.get(f"{self.api}/me", headers=self._h(token)), "Microsoft profile")
        return {"email": data.get("mail") or data.get("userPrincipalName", ""), "id": data.get("id", "")}

    def list_calendars(self, token) -> List[Dict[str, Any]]:
        data = _check(self.http.get(f"{self.api}/me/calendars", headers=self._h(token)), "Microsoft calendar list")
        return [{"id": c["id"], "name": c.get("name", ""), "primary": bool(c.get("isDefaultCalendar"))} for c in data.get("value", []) if c.get("canEdit", True)]

    def _body(self, ev) -> Dict[str, Any]:
        tz = ev.get("timezone", "UTC")
        if ev["all_day"]:
            start = {"dateTime": f"{ev['start'].isoformat()}T00:00:00", "timeZone": tz}
            end = {"dateTime": f"{ev['end'].isoformat()}T00:00:00", "timeZone": tz}
        else:
            start = {"dateTime": ev["start"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz}
            end = {"dateTime": ev["end"].strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": tz}
        return {"subject": ev["summary"], "body": {"contentType": "text", "content": ev.get("description", "")}, "start": start, "end": end, "isAllDay": bool(ev["all_day"])}

    def create_event(self, token, calendar_id, ev) -> Dict[str, str]:
        path = f"{self.api}/me/calendars/{calendar_id}/events" if calendar_id else f"{self.api}/me/events"
        data = _check(self.http.post(path, headers=self._h(token), json=self._body(ev)), "Microsoft create event")
        return {"id": data["id"], "url": data.get("webLink", "")}

    def update_event(self, token, calendar_id, event_id, ev) -> None:
        resp = self.http.patch(f"{self.api}/me/events/{event_id}", headers=self._h(token), json=self._body(ev))
        if resp.status_code == 404:
            raise CalendarProviderError("event no longer exists")
        _check(resp, "Microsoft update event")

    def delete_event(self, token, calendar_id, event_id) -> None:
        resp = self.http.delete(f"{self.api}/me/events/{event_id}", headers=self._h(token))
        if resp.status_code not in (204, 200, 404):
            _check(resp, "Microsoft delete event")

    def free_busy(self, token, calendar_id, start: datetime, end: datetime) -> List[tuple]:
        """`calendarView` over the horizon (same scope we already hold).  Events
        the user is free for, and all-day events, do not reduce capacity."""
        from .busy import parse_dt

        headers = dict(self._h(token))
        headers["Prefer"] = 'outlook.timezone="UTC"'
        params = {
            "startDateTime": _rfc3339(start), "endDateTime": _rfc3339(end),
            "$select": "start,end,isAllDay,showAs,subject", "$top": "250", "$orderby": "start/dateTime",
        }
        path = f"{self.api}/me/calendars/{calendar_id}/calendarView" if calendar_id else f"{self.api}/me/calendarView"
        data = _check(self.http.get(path, headers=headers, params=params), "Microsoft calendar view")
        out = []
        for ev in data.get("value") or []:
            if ev.get("isAllDay") or (ev.get("showAs") or "busy") in ("free", "workingElsewhere"):
                continue
            s, e = parse_dt((ev.get("start") or {}).get("dateTime")), parse_dt((ev.get("end") or {}).get("dateTime"))
            if s and e:
                out.append((s, e))
        return out


CALENDAR_PROVIDERS: Dict[str, type] = {"google": GoogleCalendarProvider, "microsoft": MicrosoftCalendarProvider}


def get_provider(provider_id: str) -> CalendarProvider:
    try:
        return CALENDAR_PROVIDERS[provider_id]()
    except KeyError:
        raise CalendarProviderError(f"unknown calendar provider {provider_id}")
