# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Central configuration.

Everything is environment driven (see `.env.example`).  Settings are read once
at import time; tests override via environment variables before importing.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _csv(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # --- core ---------------------------------------------------------
    FLOWBOARD_ENV: str = "production"  # production | development | test
    FLOWBOARD_SECRET_KEY: str = Field(default="dev-insecure-change-me")
    FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY: str = Field(default="")
    FLOWBOARD_SITE_URL: str = "https://flowboard.fyi"
    FLOWBOARD_SITE_NAME: str = "Vulpfin Flowboard"
    FLOWBOARD_ALLOWED_HOSTS: str = "flowboard.fyi,flowboard.vulpfin.com,localhost,127.0.0.1,127.0.1.1"
    FLOWBOARD_TRUSTED_ORIGINS: str = "https://flowboard.fyi,https://flowboard.vulpfin.com"
    FLOWBOARD_DATABASE_URL: str = ""
    FLOWBOARD_DATA_DIR: str = ""
    FLOWBOARD_LEGACY_TASKS_JSON: str = ""  # path of pre-2.0 flowboard_tasks.json for migration
    FLOWBOARD_SESSION_COOKIE: str = "fb_session"
    FLOWBOARD_SESSION_MAX_AGE: int = 60 * 60 * 24 * 14
    FLOWBOARD_COOKIE_SECURE: bool = True
    FLOWBOARD_ALLOW_REGISTRATION: bool = True
    FLOWBOARD_AI_LOG_PROMPTS: bool = False  # store prompt/response bodies in ai_usage (off by default)
    FLOWBOARD_PROXY_HEADERS: bool = True  # honour X-Forwarded-* from Apache

    # --- calendars ----------------------------------------------------
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""
    MICROSOFT_TENANT_ID: str = "common"

    # --- TG11 identity (OIDC) ---------------------------------------
    TG11_OIDC_ISSUER: str = ""
    TG11_OIDC_CLIENT_ID: str = ""
    TG11_OIDC_CLIENT_SECRET: str = ""
    TG11_OIDC_SCOPES: str = "openid profile email"
    TG11_OIDC_LOGIN_LABEL: str = "Sign in with TG11"
    TG11_LOCAL_LOGIN_ENABLED: bool = True

    # --- email (password reset) ----------------------------------------
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_FROM: str = "Vulpfin Flowboard <noreply@tg11.org>"

    # ------------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        p = Path(self.FLOWBOARD_DATA_DIR) if self.FLOWBOARD_DATA_DIR else BASE_DIR / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def database_url(self) -> str:
        if self.FLOWBOARD_DATABASE_URL:
            return self.FLOWBOARD_DATABASE_URL
        return f"sqlite:///{self.data_dir / 'flowboard.sqlite3'}"

    @property
    def allowed_hosts(self) -> List[str]:
        return _csv(self.FLOWBOARD_ALLOWED_HOSTS)

    @property
    def trusted_origins(self) -> List[str]:
        return _csv(self.FLOWBOARD_TRUSTED_ORIGINS)

    @property
    def is_test(self) -> bool:
        return self.FLOWBOARD_ENV == "test"

    @property
    def is_dev(self) -> bool:
        return self.FLOWBOARD_ENV in ("development", "test")

    @property
    def oidc_configured(self) -> bool:
        return bool(self.TG11_OIDC_ISSUER and self.TG11_OIDC_CLIENT_ID)

    @property
    def google_configured(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def microsoft_configured(self) -> bool:
        return bool(self.MICROSOFT_CLIENT_ID and self.MICROSOFT_CLIENT_SECRET)

    def absolute_url(self, path: str) -> str:
        return self.FLOWBOARD_SITE_URL.rstrip("/") + "/" + path.lstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
