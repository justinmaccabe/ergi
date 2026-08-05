"""The authentication gate.

One rule governs the shape of this module: **`require_auth` has exactly one
`return`, and it is reached only after positive verification.** Every other path
raises or halts the script. The gate it replaces began with a branch that
returned early when no passcode was configured, so a missing environment
variable served a personal portfolio to the public internet.

There is no guest button and no balance-masking mode. Masking is
allowlist-by-omission — every new chart, table and export is another chance to
forget one — and percentages plus a single absolute figure reconstruct a book
anyway. A shareable deployment runs synthetic data instead.
"""

from __future__ import annotations

import streamlit as st

from desk.security import RateLimiter, SessionCodec, SessionError, hash_identifier, verify_passcode
from desk.settings import AuthMode, Settings

_LIMITER_KEY = "_desk_rate_limiter"
_TOKEN_KEY = "_desk_session_token"


def _limiter(settings: Settings) -> RateLimiter:
    if _LIMITER_KEY not in st.session_state:
        st.session_state[_LIMITER_KEY] = RateLimiter(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_window_minutes * 60,
        )
    limiter: RateLimiter = st.session_state[_LIMITER_KEY]
    return limiter


def _codec(settings: Settings) -> SessionCodec:
    if settings.session_secret is None:
        # Unreachable: settings validation requires this whenever a login
        # exists. Raising rather than degrading keeps that guarantee local.
        raise RuntimeError("no session secret configured")
    return SessionCodec(
        settings.session_secret.get_secret_value(),
        absolute_hours=settings.session_absolute_hours,
        idle_minutes=settings.session_idle_minutes,
        epoch=settings.session_epoch,
    )


def _client_key() -> str:
    """A per-client rate-limit key.

    Streamlit does not expose the client address portably, so this falls back
    to the session id. That makes the limiter per-session rather than per-IP —
    weaker, and stated plainly rather than papered over. The argon2 cost and the
    fixed delay floor are what make guessing expensive; this is the cheap layer.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        ctx = get_script_run_ctx()
        if ctx is not None:
            return hash_identifier(ctx.session_id, salt="ratelimit")
    except Exception:
        pass
    return "unknown"


def require_auth(settings: Settings) -> str:
    """Return the authenticated subject, or halt the script.

    Never returns for an unauthenticated caller.
    """
    if settings.auth_mode is AuthMode.DEMO:
        return "demo"

    if settings.auth_mode is AuthMode.NONE:
        # Settings already refuse this combination in production; reaching here
        # means a deliberate local run with no listener exposed.
        return "local"

    if settings.auth_mode is AuthMode.OIDC:
        return _require_oidc(settings)

    return _require_passcode(settings)


def _require_oidc(settings: Settings) -> str:
    user = getattr(st, "user", None)
    email = (getattr(user, "email", "") or "").lower()
    verified = bool(getattr(user, "email_verified", False))

    if not email or not verified:
        st.title("Sign in")
        st.button("Continue with Google", on_click=st.login, type="primary")
        st.stop()

    if email not in settings.allowed_email_set:
        # Do not confirm whether the address exists anywhere; just refuse.
        st.error("This account is not permitted.")
        st.button("Sign out", on_click=st.logout)
        st.stop()

    return email


def _require_passcode(settings: Settings) -> str:
    codec = _codec(settings)

    token = st.session_state.get(_TOKEN_KEY)
    if token:
        try:
            payload = codec.verify(token)
            st.session_state[_TOKEN_KEY] = codec.refresh(payload)
            return str(payload["sub"])
        except SessionError as exc:
            del st.session_state[_TOKEN_KEY]
            st.info(f"Signed out: {exc}.")

    limiter = _limiter(settings)
    key = _client_key()

    if limiter.is_locked(key):
        wait = limiter.retry_after(key)
        st.error(f"Too many attempts. Try again in {wait // 60 + 1} minute(s).")
        st.stop()

    with st.form("signin", clear_on_submit=True):
        st.subheader("Sign in")
        entered = st.text_input("Passcode", type="password")
        submitted = st.form_submit_button("Continue", type="primary")

    if not submitted:
        st.stop()

    if settings.passcode_hash is None:
        raise RuntimeError("no passcode hash configured")

    ok = verify_passcode(entered, settings.passcode_hash.get_secret_value())
    limiter.record(key, ok=ok)

    if not ok:
        remaining = settings.login_max_attempts - limiter.failures(key)
        st.error(
            "Incorrect passcode." + (f" {remaining} attempt(s) remaining." if remaining > 0 else "")
        )
        st.stop()

    st.session_state[_TOKEN_KEY] = codec.issue("owner")
    st.rerun()
    # Unreachable; st.rerun does not return. Present so no path falls through
    # to an implicit None that a caller might read as success.
    raise RuntimeError("unreachable")


def sign_out() -> None:
    st.session_state.pop(_TOKEN_KEY, None)
