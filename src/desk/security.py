"""Passcode verification, rate limiting, and session tokens.

Design notes, each answering a specific defect in the build this replaces:

  * Passcodes are argon2id hashes. The reference stored a plaintext string in
    its secret store and compared with `==`, which is both readable by anyone
    with secret-store access and timing-observable.
  * `verify_passcode` returns a bool but never short-circuits: every attempt
    pays the same hash cost plus a fixed floor, so a wrong answer takes as long
    as a right one.
  * There is no "no passcode configured" branch. Absence of a hash is caught in
    settings construction, so this module cannot be reached in that state.
  * Sessions are signed tokens with a server-checked expiry, not a session_state
    flag. Streamlit's session_state resets on reconnect and is not an
    authenticator.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

# Interactive-login parameters: ~64 MiB, which is a meaningful cost to an
# offline cracker and imperceptible to a person signing in.
_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# Every verification takes at least this long, successful or not.
_MIN_VERIFY_SECONDS = 0.25

SESSION_SALT = "desk.session.v1"


def hash_passcode(passcode: str) -> str:
    """Produce the argon2id PHC string to store in DESK_PASSCODE_HASH."""
    if len(passcode) < 12:
        raise ValueError("passcode must be at least 12 characters")
    return _HASHER.hash(passcode)


def verify_passcode(passcode: str, stored_hash: str) -> bool:
    """Constant-ish time verification. Never raises on a wrong passcode."""
    started = time.monotonic()
    try:
        _HASHER.verify(stored_hash, passcode)
        ok = True
    except (VerifyMismatchError, InvalidHashError):
        ok = False
    except Exception:
        # An unexpected argon2 failure is a failure, not an opening.
        ok = False
    finally:
        elapsed = time.monotonic() - started
        if elapsed < _MIN_VERIFY_SECONDS:
            time.sleep(_MIN_VERIFY_SECONDS - elapsed)
    return ok


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True, slots=True)
class Attempt:
    at: float
    ok: bool


class RateLimiter:
    """Sliding-window limiter keyed by caller identity.

    In-process, which is the right scope for a single-replica app. Note that a
    determined attacker who can force a restart clears it; the argon2 cost and
    the fixed delay floor are what make the underlying guessing expensive, and
    this limiter is the cheap layer on top.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[Attempt]] = {}

    def _recent(self, key: str, now: float) -> list[Attempt]:
        cutoff = now - self.window_seconds
        kept = [a for a in self._attempts.get(key, []) if a.at >= cutoff]
        self._attempts[key] = kept
        return kept

    def failures(self, key: str, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return sum(1 for a in self._recent(key, now) if not a.ok)

    def is_locked(self, key: str, *, now: float | None = None) -> bool:
        return self.failures(key, now=now) >= self.max_attempts

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Seconds until the oldest failure ages out of the window."""
        now = time.time() if now is None else now
        fails = [a for a in self._recent(key, now) if not a.ok]
        if len(fails) < self.max_attempts:
            return 0
        return max(0, int(fails[0].at + self.window_seconds - now))

    def record(self, key: str, *, ok: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent(key, now)
        self._attempts.setdefault(key, []).append(Attempt(at=now, ok=ok))
        if ok:
            # A success clears the window so one fat-fingered evening does not
            # lock out the only legitimate user.
            self._attempts[key] = []


class SessionError(Exception):
    """A session token is absent, malformed, expired, or from a prior epoch."""


class SessionCodec:
    """Signed session tokens with both an absolute and an idle expiry.

    `epoch` is an integer stamped into every token and compared on read.
    Incrementing it in configuration invalidates every outstanding session
    without rotating the signing key.
    """

    def __init__(
        self,
        secret: str,
        *,
        absolute_hours: int = 12,
        idle_minutes: int = 60,
        epoch: int = 1,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("session secret must be at least 16 characters")
        self._serializer = URLSafeTimedSerializer(secret, salt=SESSION_SALT)
        self.absolute_seconds = absolute_hours * 3600
        self.idle_seconds = idle_minutes * 60
        self.epoch = epoch

    def issue(self, subject: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        return self._serializer.dumps(
            {
                "sub": subject,
                "iat": now,
                "seen": now,
                "epoch": self.epoch,
                "nonce": secrets.token_urlsafe(8),
            }
        )

    def verify(self, token: str, *, now: float | None = None) -> dict[str, object]:
        """Validate a token and return its payload, or raise SessionError.

        Returns a payload with `seen` refreshed; the caller is expected to
        re-issue so the idle window slides.
        """
        now = time.time() if now is None else now
        try:
            payload = self._serializer.loads(token, max_age=self.absolute_seconds)
        except SignatureExpired as exc:
            raise SessionError("session expired") from exc
        except BadSignature as exc:
            raise SessionError("session signature is invalid") from exc

        if not isinstance(payload, dict):
            raise SessionError("session payload is malformed")
        if int(payload.get("epoch", -1)) != self.epoch:
            raise SessionError("session was issued under a previous epoch")

        issued = float(payload.get("iat", 0))
        seen = float(payload.get("seen", issued))
        if now - issued > self.absolute_seconds:
            raise SessionError("session expired")
        if now - seen > self.idle_seconds:
            raise SessionError("session idle timeout")

        payload["seen"] = now
        return payload

    def refresh(self, payload: dict[str, object], *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        renewed = dict(payload)
        renewed["seen"] = now
        renewed["epoch"] = self.epoch
        return self._serializer.dumps(renewed)


def hash_identifier(value: str, *, salt: str = "") -> str:
    """One-way, truncated digest of a client identifier for the auth log.

    Enough to spot a burst of failures from one source; not enough to hold an
    IP address in a table alongside a portfolio.
    """
    digest = hmac.new(salt.encode(), value.encode(), "sha256").digest()
    return base64.urlsafe_b64encode(digest)[:16].decode()
