from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.redact import MAX_ERROR_CHARS, scrub_error
from app.queue.errors import ParkJob
from app.queue.types import payload_hash


class TestScrubError:
    @pytest.mark.parametrize(
        "text",
        [
            "gsk_" + "abcdefghijklmnopqrstuvwx",
            "sbp_" + "0123456789abcdef0123456789abcdef01234567",
            "github_pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz",
            "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFghiJKLmnoPQR",
        ],
    )
    def test_removes_token_shaped_strings(self, text: str) -> None:
        assert text not in scrub_error(f"upstream said no for {text}, giving up")

    @pytest.mark.parametrize(
        "text",
        [
            "request failed: api_key=SUPERSECRETVALUE123",
            "Authorization: Bearer abc.def-ghi_jkl",
            "client_secret: s3cr3t-value&grant_type=refresh_token",
            "password=hunter2 rejected",
        ],
    )
    def test_removes_credential_key_value_pairs(self, text: str) -> None:
        cleaned = scrub_error(text)
        for leaked in ("SUPERSECRETVALUE123", "abc.def-ghi_jkl", "s3cr3t-value", "hunter2"):
            assert leaked not in cleaned

    def test_keeps_ordinary_diagnostics_readable(self) -> None:
        message = "HTTP 429 from provider: token expired, retry in 30 s (request 8f2a)"
        assert scrub_error(message) == message

    def test_bounds_the_length(self) -> None:
        cleaned = scrub_error("x" * 50_000)
        assert len(cleaned) <= MAX_ERROR_CHARS + 20
        assert cleaned.endswith("[truncated]")


class TestPayloadHash:
    def test_ignores_key_order(self) -> None:
        assert payload_hash({"a": 1, "b": {"x": 1, "y": 2}}) == payload_hash(
            {"b": {"y": 2, "x": 1}, "a": 1}
        )

    def test_differs_for_different_payloads(self) -> None:
        assert payload_hash({"a": 1}) != payload_hash({"a": 2})


class TestParkJobValidation:
    def test_requires_exactly_one_resume_point(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ParkJob("why")  # neither
        with pytest.raises(ValueError, match="exactly one"):
            ParkJob("why", delay_seconds=1, until=datetime.now(UTC))  # both
        assert ParkJob("why", delay_seconds=1).delay_seconds == 1
        assert ParkJob("why", until=datetime.now(UTC)).until is not None

    def test_rejects_naive_until(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ParkJob("why", until=datetime(2026, 1, 1))
