"""Runtime secrets and environment. The only module permitted to read the
environment or Streamlit's secret store.

Everything here fails closed. The build this replaces had an auth gate that
returned early when its passcode secret was unset, which meant a missing
configuration value silently published the owner's portfolio to the internet.
The defence is not "remember to set the secret" — it is that there is no code
path from a missing secret to a served page.

Three rules hold throughout:

  1. Nothing security-relevant has a default.
  2. Validation runs at construction, before any UI renders.
  3. An invalid combination raises. It never degrades to something weaker.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Configuration is missing or incoherent. Always fatal."""


class AuthMode(StrEnum):
    """How the app authenticates.

    Required, with no default. An unset or misspelled value raises rather than
    falling back to the most permissive option.
    """

    PASSCODE = "passcode"
    OIDC = "oidc"
    NONE = "none"
    DEMO = "demo"


class AppEnv(StrEnum):
    LOCAL = "local"
    PROD = "prod"
    DEMO = "demo"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DESK_",
        frozen=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_env: AppEnv = AppEnv.LOCAL
    auth_mode: AuthMode = Field(...)
    database_url: SecretStr | None = None

    # Not a secret, but the environment boundary lives here and nowhere else,
    # so the config-file override is read here and passed down as an argument.
    config_path: str | None = None

    passcode_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    session_absolute_hours: int = Field(default=12, ge=1, le=168)
    session_idle_minutes: int = Field(default=60, ge=1, le=1440)
    session_epoch: int = 1

    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    allowed_emails: str = ""

    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_minutes: int = Field(default=15, ge=1, le=1440)

    @property
    def allowed_email_set(self) -> frozenset[str]:
        return frozenset(e.strip().lower() for e in self.allowed_emails.split(",") if e.strip())

    @model_validator(mode="after")
    def _fail_closed(self) -> Settings:
        if self.auth_mode is AuthMode.PASSCODE and not self.passcode_hash:
            raise ValueError(
                "DESK_AUTH_MODE=passcode requires DESK_PASSCODE_HASH "
                "(generate one with `desk hash-passcode`)"
            )
        if self.auth_mode is AuthMode.OIDC:
            missing = [
                name
                for name, value in (
                    ("DESK_OIDC_CLIENT_ID", self.oidc_client_id),
                    ("DESK_OIDC_CLIENT_SECRET", self.oidc_client_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"DESK_AUTH_MODE=oidc requires {', '.join(missing)}")
            if not self.allowed_email_set:
                raise ValueError(
                    "DESK_AUTH_MODE=oidc requires a non-empty DESK_ALLOWED_EMAILS allowlist. "
                    "An OIDC login with no allowlist authenticates the whole internet."
                )

        if self.auth_mode in (AuthMode.PASSCODE, AuthMode.OIDC) and not self.session_secret:
            raise ValueError(
                "DESK_SESSION_SECRET is required whenever a login exists "
                "(32 random bytes, hex or base64)"
            )

        # 'none' is legitimate for a laptop with no listener, and catastrophic
        # anywhere reachable. Allow it only where it cannot be a mistake.
        if self.auth_mode is AuthMode.NONE and self.app_env is AppEnv.PROD:
            raise ValueError(
                "DESK_AUTH_MODE=none is refused when DESK_APP_ENV=prod. "
                "Use 'passcode' or 'oidc', or set DESK_APP_ENV=local for a local-only run."
            )

        # The interlock that makes a public demo safe: a demo build must be
        # incapable of pointing at real data.
        if self.app_env is AppEnv.DEMO or self.auth_mode is AuthMode.DEMO:
            url = self.database_url.get_secret_value() if self.database_url else ""
            if url and not _looks_like_demo_database(url):
                raise ValueError(
                    "a demo deployment must not be configured with a non-demo database. "
                    "Point DESK_DATABASE_URL at the demo database, or unset it to use "
                    "in-memory synthetic data."
                )

        # A missing database URL in production is silent data loss: the app
        # would create an empty local file and look like it had simply lost
        # your history. The reference did exactly this.
        if self.app_env is AppEnv.PROD:
            url = self.database_url.get_secret_value() if self.database_url else ""
            if not url:
                raise ValueError("DESK_DATABASE_URL is required when DESK_APP_ENV=prod")
            if url.startswith("sqlite"):
                raise ValueError(
                    "a SQLite database is refused in prod: hosted app filesystems are "
                    "ephemeral, so your history would vanish on the next restart"
                )
        return self


def _looks_like_demo_database(url: str) -> bool:
    lowered = url.lower()
    return "demo" in lowered or lowered.startswith("sqlite:///:memory:")


def _load_streamlit_secrets_into_env() -> None:
    """Copy Streamlit's secret store into the environment, once, before settings
    are constructed.

    This is the single place the codebase knows Streamlit exists as a secret
    source. Everything downstream reads plain environment variables, which keeps
    it testable and portable off Streamlit Cloud.
    """
    try:
        import streamlit as st

        secrets: Any = st.secrets
    except Exception:
        return
    try:
        items = list(secrets.items())
    except Exception:
        return
    for key, value in items:
        if isinstance(value, str) and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object.

    Raises SettingsError on anything invalid. Callers must not catch this and
    continue — the app is expected to refuse to start.
    """
    _load_streamlit_secrets_into_env()
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = ["configuration is invalid and the app will not start:"]
        for err in exc.errors():
            location = ".".join(str(p) for p in err["loc"]) or "(root)"
            if err["type"] == "missing":
                lines.append(f"  DESK_{location.upper()} is required and was not set")
            else:
                lines.append(f"  {location}: {err['msg']}")
        raise SettingsError("\n".join(lines)) from exc
