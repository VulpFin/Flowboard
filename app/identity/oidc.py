# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""OpenID Connect relying-party client (authorization code + PKCE).

Works with any compliant provider; configured for TG11 through
    TG11_OIDC_ISSUER, TG11_OIDC_CLIENT_ID, TG11_OIDC_CLIENT_SECRET, TG11_OIDC_SCOPES

Flow:
  1. /auth/tg11/login   -> build state + nonce + PKCE, store in signed cookie, redirect
  2. /auth/tg11/callback-> verify state, exchange code (with code_verifier),
                           validate ID token (signature via JWKS, iss, aud, exp,
                           nonce), fetch userinfo, hand claims to the auth service

The stable cross-service identifier is the `sub` claim (TG11 user UUID).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from joserfc import jwt as _jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from ..config import settings

PROVIDER_ID = "tg11"


class OIDCError(Exception):
    pass


@dataclass
class OIDCClaims:
    subject: str
    email: str
    email_verified: bool
    preferred_username: str
    name: str
    raw: Dict[str, Any]
    access_token: str = ""
    scope: str = ""


class OIDCClient:
    def __init__(self, issuer: str, client_id: str, client_secret: str, scopes: str, redirect_uri: str, http: Optional[httpx.Client] = None):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes or "openid profile email"
        self.redirect_uri = redirect_uri
        self.http = http or httpx.Client(timeout=15.0)
        self._metadata: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._jwks_fetched = 0.0

    @classmethod
    def from_settings(cls) -> "OIDCClient":
        if not settings.oidc_configured:
            raise OIDCError("TG11 OIDC is not configured")
        return cls(settings.TG11_OIDC_ISSUER, settings.TG11_OIDC_CLIENT_ID, settings.TG11_OIDC_CLIENT_SECRET, settings.TG11_OIDC_SCOPES, settings.absolute_url("/auth/tg11/callback"))

    # ---- discovery ------------------------------------------------------
    @property
    def metadata(self) -> Dict[str, Any]:
        if self._metadata is None:
            try:
                resp = self.http.get(f"{self.issuer}/.well-known/openid-configuration")
            except httpx.HTTPError as exc:
                raise OIDCError(f"TG11 is unreachable ({exc.__class__.__name__})")
            if resp.status_code != 200:
                raise OIDCError(f"discovery failed ({resp.status_code})")
            md = resp.json()
            if md.get("issuer", "").rstrip("/") != self.issuer:
                raise OIDCError("issuer mismatch in discovery document")
            self._metadata = md
        return self._metadata

    def jwks(self, force: bool = False) -> Dict[str, Any]:
        if self._jwks is None or force or time.time() - self._jwks_fetched > 3600:
            try:
                resp = self.http.get(self.metadata["jwks_uri"])
            except httpx.HTTPError as exc:
                raise OIDCError(f"could not fetch JWKS ({exc.__class__.__name__})")
            if resp.status_code != 200:
                raise OIDCError("could not fetch JWKS")
            self._jwks = resp.json()
            self._jwks_fetched = time.time()
        return self._jwks

    # ---- authorization request -----------------------------------------
    @staticmethod
    def new_flow_state() -> Dict[str, str]:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "code_verifier": verifier, "code_challenge": challenge}

    def authorization_url(self, flow: Dict[str, str], *, prompt: Optional[str] = None) -> str:
        q = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": flow["code_challenge"],
            "code_challenge_method": "S256",
        }
        if prompt:
            q["prompt"] = prompt
        return f"{self.metadata['authorization_endpoint']}?{urlencode(q)}"

    # ---- token exchange + validation -----------------------------------
    def exchange(self, code: str, flow: Dict[str, str]) -> OIDCClaims:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri, "client_id": self.client_id, "code_verifier": flow["code_verifier"]}
        auth = (self.client_id, self.client_secret) if self.client_secret else None
        try:
            resp = self.http.post(self.metadata["token_endpoint"], data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise OIDCError(f"token exchange failed ({exc.__class__.__name__})")
        if resp.status_code != 200:
            raise OIDCError(f"token exchange failed ({resp.status_code}): {resp.text[:200]}")
        tokens = resp.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise OIDCError("no id_token in token response")
        claims = self.validate_id_token(id_token, flow["nonce"])
        # userinfo (optional but gives freshest profile)
        if tokens.get("access_token") and self.metadata.get("userinfo_endpoint"):
            ui = self.http.get(self.metadata["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if ui.status_code == 200:
                info = ui.json()
                if info.get("sub") == claims.get("sub"):
                    for k in ("email", "email_verified", "preferred_username", "name"):
                        if k in info:
                            claims[k] = info[k]
        return OIDCClaims(
            subject=str(claims["sub"]),
            email=str(claims.get("email") or ""),
            email_verified=bool(claims.get("email_verified", False)),
            preferred_username=str(claims.get("preferred_username") or ""),
            name=str(claims.get("name") or ""),
            raw=dict(claims),
            access_token=str(tokens.get("access_token") or ""),
            scope=str(tokens.get("scope") or self.scopes),
        )

    def validate_id_token(self, id_token: str, nonce: str) -> Dict[str, Any]:
        algs = self.metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        algs = [a for a in algs if a != "none"] or ["RS256"]

        def _decode(force: bool) -> Dict[str, Any]:
            key_set = KeySet.import_key_set(self.jwks(force=force))
            return dict(_jwt.decode(id_token, key_set, algorithms=algs).claims)

        try:
            claims = _decode(False)
        except (JoseError, ValueError):
            try:
                claims = _decode(True)  # key rotation
            except (JoseError, ValueError) as exc:
                raise OIDCError(f"invalid id_token signature: {exc}")
        now = time.time()
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or now > exp + 60:
            raise OIDCError("id_token expired")
        iat = claims.get("iat")
        if isinstance(iat, (int, float)) and iat > now + 60:
            raise OIDCError("id_token issued in the future")
        if str(claims.get("iss", "")).rstrip("/") != self.issuer:
            raise OIDCError("id_token issuer mismatch")
        aud = claims.get("aud")
        if (isinstance(aud, list) and self.client_id not in aud) or (isinstance(aud, str) and aud != self.client_id) or aud is None:
            raise OIDCError("id_token audience mismatch")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") not in (None, self.client_id):
            raise OIDCError("id_token azp mismatch")
        if claims.get("nonce") != nonce:
            raise OIDCError("id_token nonce mismatch")
        if not claims.get("sub"):
            raise OIDCError("id_token missing sub")
        return claims

    def end_session_url(self, post_logout_redirect: str) -> Optional[str]:
        ep = self.metadata.get("end_session_endpoint")
        if not ep:
            return None
        return f"{ep}?{urlencode({'post_logout_redirect_uri': post_logout_redirect, 'client_id': self.client_id})}"
