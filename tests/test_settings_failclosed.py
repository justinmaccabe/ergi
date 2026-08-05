"""Settings must fail closed.

The build this replaces had an authentication gate whose first line was, in
effect, "if no passcode is configured, grant access". A missing environment
variable therefore published a personal portfolio to a public URL. Every test
here asserts that some flavour of missing or contradictory configuration raises
instead of degrading to something weaker.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from desk.settings import AppEnv, AuthMode, Settings, SettingsError, get_settings

VALID_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHR2YWx1ZQ$abcdef0123456789abcdef0123456789"
SECRET = "0123456789abcdef0123456789abcdef"


def build(**kwargs: object) -> Settings:
    return Settings(**kwargs)  # type: ignore[arg-type]


class TestAuthModeIsRequired:
    def test_missing_auth_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DESK_AUTH_MODE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(SettingsError, match="DESK_AUTH_MODE is required"):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_unknown_auth_mode_raises(self) -> None:
        with pytest.raises(ValidationError):
            build(auth_mode="allow_everyone")


class TestPasscodeMode:
    def test_passcode_mode_without_a_hash_raises(self) -> None:
        """The reference's exact bug: no secret configured meant no gate."""
        with pytest.raises(Exception, match="DESK_PASSCODE_HASH"):
            build(auth_mode=AuthMode.PASSCODE, session_secret=SECRET)

    def test_passcode_mode_without_a_session_secret_raises(self) -> None:
        with pytest.raises(Exception, match="DESK_SESSION_SECRET"):
            build(auth_mode=AuthMode.PASSCODE, passcode_hash=VALID_HASH)

    def test_fully_configured_passcode_mode_is_accepted(self) -> None:
        s = build(auth_mode=AuthMode.PASSCODE, passcode_hash=VALID_HASH, session_secret=SECRET)
        assert s.auth_mode is AuthMode.PASSCODE

    def test_secrets_do_not_appear_in_repr(self) -> None:
        s = build(auth_mode=AuthMode.PASSCODE, passcode_hash=VALID_HASH, session_secret=SECRET)
        rendered = repr(s) + str(s)
        assert VALID_HASH not in rendered
        assert SECRET not in rendered


class TestOidcMode:
    def test_oidc_without_an_allowlist_raises(self) -> None:
        """An OIDC login with an empty allowlist authenticates the whole internet."""
        with pytest.raises(Exception, match="DESK_ALLOWED_EMAILS"):
            build(
                auth_mode=AuthMode.OIDC,
                oidc_client_id="cid",
                oidc_client_secret="csecret",
                session_secret=SECRET,
            )

    def test_allowlist_is_normalised(self) -> None:
        s = build(
            auth_mode=AuthMode.OIDC,
            oidc_client_id="cid",
            oidc_client_secret="csecret",
            session_secret=SECRET,
            allowed_emails=" Person@Example.COM , other@example.com ",
        )
        assert s.allowed_email_set == frozenset({"person@example.com", "other@example.com"})


class TestNoAuthMode:
    def test_no_auth_is_refused_in_prod(self) -> None:
        with pytest.raises(Exception, match="refused when DESK_APP_ENV=prod"):
            build(
                auth_mode=AuthMode.NONE,
                app_env=AppEnv.PROD,
                database_url="postgresql://u:p@host/db",
            )

    def test_no_auth_is_allowed_locally(self) -> None:
        s = build(auth_mode=AuthMode.NONE, app_env=AppEnv.LOCAL)
        assert s.auth_mode is AuthMode.NONE


class TestDemoInterlock:
    def test_demo_env_refuses_a_real_database(self) -> None:
        """A public demo must be structurally incapable of serving real data."""
        with pytest.raises(Exception, match="must not be configured with a non-demo database"):
            build(
                auth_mode=AuthMode.DEMO,
                app_env=AppEnv.DEMO,
                database_url="postgresql://user:pw@prod-host/portfolio",
            )

    def test_demo_env_accepts_a_demo_database(self) -> None:
        s = build(
            auth_mode=AuthMode.DEMO,
            app_env=AppEnv.DEMO,
            database_url="postgresql://user:pw@host/desk_demo",
        )
        assert s.app_env is AppEnv.DEMO


class TestProductionDatabase:
    def test_missing_database_url_in_prod_raises(self) -> None:
        """Silently creating an empty SQLite file in prod is indistinguishable
        from having lost every snapshot you ever recorded."""
        with pytest.raises(Exception, match="DESK_DATABASE_URL is required"):
            build(
                auth_mode=AuthMode.PASSCODE,
                app_env=AppEnv.PROD,
                passcode_hash=VALID_HASH,
                session_secret=SECRET,
            )

    def test_sqlite_in_prod_raises(self) -> None:
        with pytest.raises(Exception, match="SQLite database is refused in prod"):
            build(
                auth_mode=AuthMode.PASSCODE,
                app_env=AppEnv.PROD,
                passcode_hash=VALID_HASH,
                session_secret=SECRET,
                database_url="sqlite:///portfolio.db",
            )
