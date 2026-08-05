"""Passcode hashing, rate limiting, and session token behaviour."""

from __future__ import annotations

import time

import pytest

from desk.security import (
    RateLimiter,
    SessionCodec,
    SessionError,
    hash_identifier,
    hash_passcode,
    verify_passcode,
)

PASSCODE = "correct horse battery staple"
SECRET = "0123456789abcdef0123456789abcdef"


class TestPasscodeHashing:
    def test_round_trip(self) -> None:
        assert verify_passcode(PASSCODE, hash_passcode(PASSCODE)) is True

    def test_wrong_passcode_is_rejected(self) -> None:
        assert verify_passcode("not the passcode", hash_passcode(PASSCODE)) is False

    def test_hash_is_salted(self) -> None:
        assert hash_passcode(PASSCODE) != hash_passcode(PASSCODE)

    def test_hash_does_not_contain_the_passcode(self) -> None:
        assert PASSCODE not in hash_passcode(PASSCODE)

    def test_short_passcodes_are_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 12 characters"):
            hash_passcode("guest")

    def test_malformed_stored_hash_is_a_rejection_not_a_crash(self) -> None:
        assert verify_passcode(PASSCODE, "not-a-hash") is False

    def test_empty_stored_hash_never_grants_access(self) -> None:
        assert verify_passcode("", "") is False

    def test_verification_has_a_floor_delay(self) -> None:
        started = time.monotonic()
        verify_passcode("wrong", hash_passcode(PASSCODE))
        assert time.monotonic() - started >= 0.2


class TestRateLimiter:
    def test_locks_after_the_configured_failures(self) -> None:
        limiter = RateLimiter(max_attempts=3, window_seconds=900)
        for _ in range(2):
            limiter.record("caller", ok=False)
        assert limiter.is_locked("caller") is False
        limiter.record("caller", ok=False)
        assert limiter.is_locked("caller") is True

    def test_lock_is_per_caller(self) -> None:
        limiter = RateLimiter(max_attempts=2)
        limiter.record("a", ok=False)
        limiter.record("a", ok=False)
        assert limiter.is_locked("a") is True
        assert limiter.is_locked("b") is False

    def test_failures_age_out_of_the_window(self) -> None:
        limiter = RateLimiter(max_attempts=2, window_seconds=100)
        now = time.time()
        limiter.record("a", ok=False, now=now - 200)
        limiter.record("a", ok=False, now=now - 150)
        assert limiter.is_locked("a", now=now) is False

    def test_success_clears_the_window(self) -> None:
        limiter = RateLimiter(max_attempts=3)
        limiter.record("a", ok=False)
        limiter.record("a", ok=False)
        limiter.record("a", ok=True)
        assert limiter.failures("a") == 0

    def test_retry_after_is_reported(self) -> None:
        limiter = RateLimiter(max_attempts=2, window_seconds=600)
        now = time.time()
        limiter.record("a", ok=False, now=now)
        limiter.record("a", ok=False, now=now)
        assert 590 <= limiter.retry_after("a", now=now) <= 600


class TestSessionCodec:
    def test_issue_and_verify(self) -> None:
        codec = SessionCodec(SECRET)
        payload = codec.verify(codec.issue("owner"))
        assert payload["sub"] == "owner"

    def test_tampered_token_is_rejected(self) -> None:
        codec = SessionCodec(SECRET)
        token = codec.issue("owner")
        with pytest.raises(SessionError, match="signature is invalid"):
            codec.verify(token[:-4] + "AAAA")

    def test_token_signed_with_another_secret_is_rejected(self) -> None:
        token = SessionCodec("f" * 32).issue("owner")
        with pytest.raises(SessionError):
            SessionCodec(SECRET).verify(token)

    def test_absolute_expiry_is_enforced(self) -> None:
        codec = SessionCodec(SECRET, absolute_hours=1)
        now = time.time()
        token = codec.issue("owner", now=now - 7200)
        with pytest.raises(SessionError, match="expired"):
            codec.verify(token, now=now)

    def test_idle_timeout_is_enforced(self) -> None:
        codec = SessionCodec(SECRET, absolute_hours=12, idle_minutes=30)
        now = time.time()
        token = codec.issue("owner", now=now - 3600)
        with pytest.raises(SessionError, match="idle timeout"):
            codec.verify(token, now=now)

    def test_active_session_within_both_windows_is_accepted(self) -> None:
        codec = SessionCodec(SECRET, absolute_hours=12, idle_minutes=60)
        now = time.time()
        payload = codec.verify(codec.issue("owner", now=now - 600), now=now)
        assert payload["sub"] == "owner"

    def test_bumping_the_epoch_invalidates_outstanding_sessions(self) -> None:
        token = SessionCodec(SECRET, epoch=1).issue("owner")
        with pytest.raises(SessionError, match="previous epoch"):
            SessionCodec(SECRET, epoch=2).verify(token)

    def test_refresh_slides_the_idle_window(self) -> None:
        codec = SessionCodec(SECRET, idle_minutes=30)
        now = time.time()
        payload = codec.verify(codec.issue("owner", now=now - 600), now=now)
        later = now + 1500
        assert codec.verify(codec.refresh(payload, now=now), now=later)["sub"] == "owner"

    def test_weak_secret_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 16 characters"):
            SessionCodec("short")


class TestIdentifierHashing:
    def test_is_one_way_and_stable(self) -> None:
        assert hash_identifier("198.51.100.7", salt="s") == hash_identifier(
            "198.51.100.7", salt="s"
        )
        assert "198.51.100.7" not in hash_identifier("198.51.100.7", salt="s")

    def test_differs_by_salt(self) -> None:
        assert hash_identifier("198.51.100.7", salt="a") != hash_identifier(
            "198.51.100.7", salt="b"
        )
