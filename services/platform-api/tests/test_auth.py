"""Проверка access token: подпись, issuer, срок, аудитория, клиент, JWKS."""

from __future__ import annotations

import jwt
import pytest
from conftest import ISSUER, JwksServer, Keys, TokenFactory
from cryptography.hazmat.primitives.asymmetric import rsa

from antec_platform.auth import (
    DeniedToken,
    InvalidToken,
    JwksCache,
    JwksUnavailable,
    TokenVerifier,
    bearer_token,
)

pytestmark = pytest.mark.anyio


async def test_valid_token(verifier: TokenVerifier, make_token: TokenFactory) -> None:
    principal = await verifier.verify(make_token(aud=["platform-api", "account"]))
    assert principal.issuer == ISSUER
    assert principal.subject == "sub-1"
    assert principal.client_id == "open-webui"
    assert principal.entra_oid == "oid-sub-1"
    assert principal.idp == "entra"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"exp_in": -120}, "expired", id="expired-beyond-leeway"),
        pytest.param({"iss": "http://evil/realms/antec"}, "issuer", id="wrong-issuer"),
        pytest.param({"typ": "ID"}, "typ", id="id-token"),
        pytest.param({"kid": "other"}, "kid", id="unknown-kid"),
    ],
)
async def test_invalid_tokens(
    verifier: TokenVerifier, make_token: TokenFactory, overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(InvalidToken):
        await verifier.verify(make_token(**overrides))


async def test_expired_within_leeway_is_accepted(
    verifier: TokenVerifier, make_token: TokenFactory
) -> None:
    await verifier.verify(make_token(exp_in=-10))


async def test_foreign_key_signature_rejected(
    verifier: TokenVerifier, make_token: TokenFactory
) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidToken):
        await verifier.verify(make_token(key=other))


async def test_hs256_with_public_key_rejected(verifier: TokenVerifier, keys: Keys) -> None:
    """Подмена алгоритма: HS256 с открытым ключом в роли секрета не принимается."""
    token = jwt.encode(
        {"iss": ISSUER, "sub": "x", "aud": "platform-api", "azp": "open-webui", "exp": 9999999999},
        "s" * 32,
        algorithm="HS256",
        headers={"kid": keys.kid},
    )
    with pytest.raises(InvalidToken):
        await verifier.verify(token)


async def test_garbage_rejected(verifier: TokenVerifier) -> None:
    with pytest.raises(InvalidToken):
        await verifier.verify("not-a-jwt")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"aud": "account"}, "audience", id="foreign-audience"),
        pytest.param({"aud": []}, "audience", id="empty-audience"),
        pytest.param({"azp": "mcp-m365"}, "client", id="client-not-allowed"),
        pytest.param({"azp": ""}, "client", id="no-azp"),
    ],
)
async def test_denied_tokens_keep_principal(
    verifier: TokenVerifier, make_token: TokenFactory, overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(DeniedToken) as info:
        await verifier.verify(make_token(**overrides))
    assert info.value.reason == reason
    assert info.value.principal.subject == "sub-1"


async def test_jwks_cached(
    verifier: TokenVerifier, make_token: TokenFactory, jwks_server: JwksServer
) -> None:
    for _ in range(3):
        await verifier.verify(make_token())
    assert jwks_server.calls == 1


async def test_unknown_kid_reload_is_rate_limited(
    verifier: TokenVerifier, make_token: TokenFactory, jwks_server: JwksServer
) -> None:
    await verifier.verify(make_token())
    for _ in range(5):
        with pytest.raises(InvalidToken):
            await verifier.verify(make_token(kid="rotated"))
    assert jwks_server.calls == 1


async def test_jwks_unavailable(
    verifier: TokenVerifier, make_token: TokenFactory, jwks_server: JwksServer
) -> None:
    jwks_server.fail = True
    with pytest.raises(JwksUnavailable):
        await verifier.verify(make_token())


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer   abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("", None),
        (None, None),
        ("Bearer " + "a" * 20000, None),
    ],
)
def test_bearer_token(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected


async def test_stale_keys_used_when_jwks_down(
    verifier: TokenVerifier, make_token: TokenFactory, jwks_server: JwksServer, jwks: JwksCache
) -> None:
    """Keycloak недоступен при истечении кэша: токены с известным kid проверяются по кэшу."""
    await verifier.verify(make_token())
    jwks._loaded_at -= 10_000  # кэш истёк
    jwks_server.fail = True
    principal = await verifier.verify(make_token())
    assert principal.subject == "sub-1"
    calls = jwks_server.calls
    await verifier.verify(make_token())
    assert jwks_server.calls == calls  # следующая попытка не раньше чем через 30 с


async def test_platform_roles_parsed_and_sanitized(
    verifier: TokenVerifier, make_token: TokenFactory, caplog: pytest.LogCaptureFixture
) -> None:
    principal = await verifier.verify(
        make_token(
            platform_roles=["platform-user", "platform-admin", "Bad Role", 5, "platform-user"]
        )
    )
    assert principal.roles == ("platform-admin", "platform-user")
    assert "отброшены роли" in caplog.text
    caplog.clear()
    await verifier.verify(make_token(platform_roles=["platform-user", "platform-user"]))
    assert "отброшены роли" not in caplog.text
    assert (await verifier.verify(make_token(platform_roles="platform-admin"))).roles == ()
