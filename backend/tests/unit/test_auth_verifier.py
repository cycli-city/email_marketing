"""Token verification: real signatures, real attacks, a fake network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest

from app.auth.jwks import JwksCache, JwksUnavailableError, parse_jwks
from app.auth.verifier import MAX_TOKEN_BYTES, InvalidTokenError, TokenVerifier, VerifierConfig
from app.core.clock import FakeClock
from app.core.http import build_http_client
from tests.auth_helpers import (
    DROP,
    ISSUER,
    JWKS_URL,
    FakeIdp,
    hs256_with_public_key_as_secret,
    rsa_key,
    unsigned_token,
)

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


def build(
    idp: FakeIdp,
    clock: FakeClock,
    *,
    ttl: float = 600.0,
    min_refetch: float = 30.0,
    max_stale: float = 86_400.0,
    leeway: float = 10.0,
) -> TokenVerifier:
    client = build_http_client(transport=idp.transport())
    cache = JwksCache(
        JWKS_URL,
        client,
        clock,
        ttl_seconds=ttl,
        min_refetch_seconds=min_refetch,
        max_stale_seconds=max_stale,
    )
    return TokenVerifier(cache, VerifierConfig(issuer=ISSUER, leeway_seconds=leeway), clock)


async def rejection(verifier: TokenVerifier, token: str) -> str:
    with pytest.raises(InvalidTokenError) as raised:
        await verifier.verify(token)
    return raised.value.reason


# ───────────────────────────── the happy path ─────────────────────────────


class TestValidTokens:
    async def test_a_genuine_token_yields_a_principal(self, idp: FakeIdp, clock: FakeClock) -> None:
        verifier = build(idp, clock)
        principal = await verifier.verify(
            idp.token(NOW, email="Owner@Example.COM", aal="aal2", exp=int(NOW.timestamp()) + 900)
        )
        assert principal.email == "owner@example.com"
        assert principal.aal == "aal2"
        assert principal.session_id == "11111111-1111-1111-1111-111111111111"
        assert principal.expires_at == NOW + timedelta(seconds=900)

    async def test_unverified_flag_absent_is_fine_but_explicitly_false_is_not(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock)
        assert (await verifier.verify(idp.token(NOW, user_metadata=DROP))).email
        assert await rejection(
            verifier, idp.token(NOW, user_metadata={"email_verified": False})
        ) == ("email_not_verified")

    async def test_rs256_keys_are_supported_too(self, idp: FakeIdp, clock: FakeClock) -> None:
        key = rsa_key()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        idp.extra_jwks.append({**jwk, "kid": "rsa-1", "alg": "RS256", "use": "sig"})
        token = jwt.encode(idp.claims(NOW), key, algorithm="RS256", headers={"kid": "rsa-1"})
        assert (await build(idp, clock).verify(token)).email == "owner@example.com"


# ───────────────────────────── claims & time ─────────────────────────────


class TestClaims:
    async def test_expired_tokens_are_rejected_beyond_the_leeway(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, leeway=10)
        ts = int(NOW.timestamp())
        assert (await verifier.verify(idp.token(NOW, exp=ts - 5))).email  # within leeway
        assert await rejection(verifier, idp.token(NOW, exp=ts - 11)) == "expired"

    async def test_tokens_expire_as_the_clock_moves(self, idp: FakeIdp, clock: FakeClock) -> None:
        verifier = build(idp, clock)
        token = idp.token(NOW)
        await verifier.verify(token)
        clock.advance(hours=1, seconds=11)
        assert await rejection(verifier, token) == "expired"

    async def test_tokens_from_the_future_are_rejected(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, leeway=10)
        ts = int(NOW.timestamp())
        assert await rejection(verifier, idp.token(NOW, iat=ts + 11)) == "issued_in_the_future"
        assert await rejection(verifier, idp.token(NOW, nbf=ts + 11)) == "not_yet_valid"

    @pytest.mark.parametrize("claim", ["exp", "iat", "sub", "aud", "iss"])
    async def test_required_claims_must_be_present(
        self, idp: FakeIdp, clock: FakeClock, claim: str
    ) -> None:
        assert (
            await rejection(build(idp, clock), idp.token(NOW, **{claim: DROP}))
            == f"missing_claim_{claim}"
        )

    async def test_a_non_numeric_expiry_is_rejected_not_trusted(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        assert (
            await rejection(build(idp, clock), idp.token(NOW, exp="never"))
            == "claim_exp_not_numeric"
        )

    async def test_audience_and_issuer_must_match(self, idp: FakeIdp, clock: FakeClock) -> None:
        verifier = build(idp, clock)
        assert await rejection(verifier, idp.token(NOW, aud="someone-else")) == "bad_audience"
        assert (
            await rejection(verifier, idp.token(NOW, iss="https://evil.example/auth/v1"))
            == "bad_issuer"
        )

    @pytest.mark.parametrize("role", ["anon", "service_role", "supabase_admin"])
    async def test_only_logged_in_users_pass_not_the_anon_or_service_keys(
        self, idp: FakeIdp, clock: FakeClock, role: str
    ) -> None:
        """The public anon key and the service-role key are JWTs signed by the same project — they must never log in."""
        assert (
            await rejection(build(idp, clock), idp.token(NOW, role=role))
            == "role_not_authenticated"
        )

    async def test_anonymous_sessions_are_rejected(self, idp: FakeIdp, clock: FakeClock) -> None:
        assert (
            await rejection(build(idp, clock), idp.token(NOW, is_anonymous=True))
            == "anonymous_session"
        )

    async def test_identity_claims_must_be_well_formed(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock)
        assert await rejection(verifier, idp.token(NOW, sub="not-a-uuid")) == "sub_not_uuid"
        assert await rejection(verifier, idp.token(NOW, email=DROP)) == "missing_email"
        assert await rejection(verifier, idp.token(NOW, email="no-at-sign")) == "missing_email"


# ───────────────────────────── attacks ─────────────────────────────


class TestAttacks:
    async def test_a_forged_signature_is_rejected(self, idp: FakeIdp, clock: FakeClock) -> None:
        rogue = FakeIdp()  # different key pair, but claims to be `key-1`
        forged = rogue.token(NOW, kid=idp.kid)
        assert await rejection(build(idp, clock), forged) == "bad_signature"

    async def test_a_tampered_payload_is_rejected(self, idp: FakeIdp, clock: FakeClock) -> None:
        token = idp.token(NOW, email="someone@else.example")
        header, _, signature = token.split(".")
        evil_payload = jwt.utils.base64url_encode(
            json.dumps(idp.claims(NOW, email="owner@example.com")).encode()
        ).decode()
        assert (
            await rejection(build(idp, clock), f"{header}.{evil_payload}.{signature}")
            == "bad_signature"
        )

    async def test_alg_none_is_rejected_before_any_crypto(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        assert await rejection(build(idp, clock), unsigned_token(idp.claims(NOW), idp.kid)) == (
            "algorithm_not_allowed"
        )

    async def test_the_hs256_confusion_attack_fails(self, idp: FakeIdp, clock: FakeClock) -> None:
        """Signing with HMAC using the PUBLIC key as the secret must not work: symmetric algorithms are not accepted."""
        token = hs256_with_public_key_as_secret(idp.claims(NOW), idp.kid, idp.public_pem())
        assert await rejection(build(idp, clock), token) == "algorithm_not_allowed"

    async def test_the_algorithm_comes_from_the_key_set_not_the_token(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        """A token claiming RS256 for an EC key (an allowed algorithm, wrong for this key) is refused."""
        token = jwt.encode(idp.claims(NOW), rsa_key(), algorithm="RS256", headers={"kid": idp.kid})
        assert await rejection(build(idp, clock), token) == "algorithm_mismatch"

    @pytest.mark.parametrize(
        "header",
        [
            {"jku": "https://evil.example/jwks.json"},
            {"jwk": {"kty": "EC"}},
            {"x5u": "https://evil.example/c"},
        ],
    )
    async def test_attacker_supplied_key_material_is_refused(
        self, idp: FakeIdp, clock: FakeClock, header: dict[str, Any]
    ) -> None:
        assert (
            await rejection(build(idp, clock), idp.token(NOW, headers=header)) == "forbidden_header"
        )

    async def test_a_missing_or_unknown_kid_is_rejected(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock)
        no_kid = jwt.encode(idp.claims(NOW), idp.private_key(), algorithm="ES256")
        assert await rejection(verifier, no_kid) == "missing_kid"
        assert await rejection(verifier, idp.token(NOW, kid="ghost")) == "unknown_kid"

    @pytest.mark.parametrize("garbage", ["", "x", "a.b.c", "....", "Bearer abc"])
    async def test_garbage_is_rejected(self, idp: FakeIdp, clock: FakeClock, garbage: str) -> None:
        await rejection(build(idp, clock), garbage)

    async def test_oversized_tokens_are_refused_before_parsing(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        assert await rejection(build(idp, clock), "a" * (MAX_TOKEN_BYTES + 1)) == "token_size"
        assert idp.fetches == 0, "no network activity for an obviously bad token"


# ───────────────────────────── signing-key handling ─────────────────────────────


class TestKeyCache:
    async def test_keys_are_fetched_once_and_cached_until_the_ttl(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, ttl=600)
        token = idp.token(NOW, exp=int(NOW.timestamp()) + 10_000)
        for _ in range(5):
            await verifier.verify(token)
        assert idp.fetches == 1
        clock.advance(seconds=599)
        await verifier.verify(token)
        assert idp.fetches == 1
        clock.advance(seconds=2)
        await verifier.verify(token)
        assert idp.fetches == 2

    async def test_concurrent_first_requests_share_a_single_fetch(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock)
        token = idp.token(NOW)
        await asyncio.gather(*(verifier.verify(token) for _ in range(25)))
        assert idp.fetches == 1

    async def test_forged_kids_cannot_turn_into_a_request_flood(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, min_refetch=30)
        await verifier.verify(idp.token(NOW))  # fetch #1
        for i in range(50):
            assert await rejection(
                verifier, idp.token(NOW, kid=f"ghost-{i}", key=FakeIdp().private_key())
            ) == ("unknown_kid")
        assert idp.fetches == 1, "unknown kids within the refetch window never hit the network"
        clock.advance(seconds=31)
        await rejection(verifier, idp.token(NOW, kid="ghost-x", key=FakeIdp().private_key()))
        assert idp.fetches == 2, "one refetch is allowed once the window has passed"

    async def test_a_rotated_in_key_is_picked_up_after_the_refetch_window(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, min_refetch=30)
        await verifier.verify(idp.token(NOW))
        idp.add_key("key-2")
        fresh = idp.token(NOW, kid="key-2")
        assert await rejection(verifier, fresh) == "unknown_kid"  # inside the throttle window
        clock.advance(seconds=31)
        assert (await verifier.verify(fresh)).email == "owner@example.com"

    async def test_with_no_keys_and_the_provider_down_it_fails_closed(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        idp.outage = True
        verifier = build(idp, clock, min_refetch=30)
        with pytest.raises(JwksUnavailableError):
            await verifier.verify(idp.token(NOW))
        with pytest.raises(JwksUnavailableError):
            await verifier.verify(idp.token(NOW))
        assert idp.fetches == 1, "a down provider is not hammered by every request"
        idp.outage = False
        clock.advance(seconds=31)
        assert (await verifier.verify(idp.token(NOW))).email

    async def test_cached_keys_keep_working_through_a_short_outage_but_not_forever(
        self, idp: FakeIdp, clock: FakeClock
    ) -> None:
        verifier = build(idp, clock, ttl=600, min_refetch=30, max_stale=1000)
        long_lived = idp.token(NOW, exp=int(NOW.timestamp()) + 100_000)
        await verifier.verify(long_lived)
        idp.outage = True
        clock.advance(seconds=700)  # past the TTL, refresh fails, stale keys still trusted
        assert (await verifier.verify(long_lived)).email
        clock.advance(seconds=400)  # now older than max_stale
        with pytest.raises(JwksUnavailableError):
            await verifier.verify(long_lived)

    def test_the_key_url_must_be_https(self, idp: FakeIdp, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            JwksCache(
                "http://x.supabase.co/auth/v1/.well-known/jwks.json", build_http_client(), clock
            )


class TestParseJwks:
    def test_only_well_formed_asymmetric_verification_keys_are_kept(self, idp: FakeIdp) -> None:
        good = idp.jwks()["keys"][0]
        payload = {
            "keys": [
                good,
                {**good, "kid": "wrong-use", "use": "enc"},
                {**good, "kid": "sign-only", "key_ops": ["sign"]},
                {**good, "kid": "kty-mismatch", "kty": "RSA"},
                {**good, "kid": "weak-alg", "alg": "HS256"},
                {"kty": "oct", "kid": "symmetric", "alg": "HS256", "k": "c2VjcmV0"},
                {k: v for k, v in good.items() if k != "kid"},
                "not-a-dict",
            ]
        }
        assert set(parse_jwks(payload)) == {idp.kid}

    @pytest.mark.parametrize("payload", [None, [], {}, {"keys": "x"}, {"keys": [None]}])
    def test_junk_payloads_yield_no_keys(self, payload: Any) -> None:
        assert parse_jwks(payload) == {}

    async def test_a_key_set_with_no_usable_keys_counts_as_a_failed_fetch(
        self, clock: FakeClock
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"keys": [{"kty": "oct", "kid": "x", "alg": "HS256", "k": "AA"}]}
            )

        cache = JwksCache(
            JWKS_URL, build_http_client(transport=httpx.MockTransport(handler)), clock
        )
        with pytest.raises(JwksUnavailableError):
            await cache.get("x")
