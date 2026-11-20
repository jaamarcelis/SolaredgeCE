"""Cognito-login, MFA, refresh en de TokenManager, met een nagebootste Cognito JSON-API."""

from __future__ import annotations

import pytest

from custom_components.solaredge_monitoring.api import (
    AuthenticationError,
    AuthTokens,
    CognitoAuthenticator,
    InvalidCredentialsError,
    MfaRequiredError,
    SolarEdgeMonitoringClient,
    TokenManager,
)
from tests.fake_session import Call, FakeResponse, FakeSession
from tests.helpers import make_jwt

COGNITO = r"^https://cognito-idp\.eu-central-1\.amazonaws\.com/$"
CHALLENGE = {
    "ChallengeName": "PASSWORD_VERIFIER",
    "ChallengeParameters": {
        "USER_ID_FOR_SRP": "uid-1",
        "SRP_B": "abcdef1234567890" * 8,
        "SALT": "0123456789abcdef",
        "SECRET_BLOCK": "c2VjcmV0",
    },
}


def tokens_response(refresh: str | None = "refresh-1") -> dict:
    result = {"AccessToken": make_jwt(), "IdToken": "id", "ExpiresIn": 3600}
    if refresh:
        result["RefreshToken"] = refresh
    return {"AuthenticationResult": result}


def cognito(*responses: tuple[int, dict]) -> FakeSession:
    """Nep-Cognito die de antwoorden in volgorde geeft."""
    import json  # noqa: PLC0415

    queue = list(responses)

    def respond(_call: Call) -> FakeResponse:
        status, body = queue.pop(0)
        return FakeResponse(status, json.dumps(body))

    return FakeSession().add_callback("POST", COGNITO, respond)


def targets(session: FakeSession) -> list[str]:
    return [c.headers["X-Amz-Target"].split(".")[-1] for c in session.calls if "cognito" in c.url]


async def test_login_srp_flow():
    session = cognito((200, CHALLENGE), (200, tokens_response()))
    tokens = await CognitoAuthenticator(session).async_login("a@b.nl", "pw")  # type: ignore[arg-type]

    init, respond = session.calls
    assert targets(session) == ["InitiateAuth", "RespondToAuthChallenge"]
    assert init.json["AuthFlow"] == "USER_SRP_AUTH"
    assert init.json["AuthParameters"]["USERNAME"] == "a@b.nl"
    assert len(init.json["AuthParameters"]["SRP_A"]) > 500
    assert respond.json["ChallengeResponses"]["USERNAME"] == "uid-1"
    assert set(respond.json["ChallengeResponses"]) == {
        "TIMESTAMP",
        "USERNAME",
        "PASSWORD_CLAIM_SECRET_BLOCK",
        "PASSWORD_CLAIM_SIGNATURE",
    }
    assert tokens.username == "a@b.nl"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.is_valid()


async def test_wrong_password():
    session = cognito((200, CHALLENGE), (400, {"__type": "NotAuthorizedException", "message": "Incorrect username or password."}))
    with pytest.raises(InvalidCredentialsError, match="Incorrect"):
        await CognitoAuthenticator(session).async_login("a@b.nl", "fout")  # type: ignore[arg-type]


async def test_mfa_flow():
    session = cognito(
        (200, CHALLENGE),
        (200, {"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "sess-1"}),
        (200, tokens_response()),
    )
    auth = CognitoAuthenticator(session)  # type: ignore[arg-type]
    with pytest.raises(MfaRequiredError) as info:
        await auth.async_login("a@b.nl", "pw")
    tokens = await auth.async_respond_to_mfa(info.value, " 123456 ", "a@b.nl")

    assert info.value.mfa_type == "SOFTWARE_TOKEN_MFA"
    assert session.calls[2].json["Session"] == "sess-1"
    assert session.calls[2].json["ChallengeResponses"] == {"USERNAME": "uid-1", "SOFTWARE_TOKEN_MFA_CODE": "123456"}
    assert tokens.access_token


async def test_refresh_keeps_refresh_token():
    session = cognito((200, tokens_response(refresh=None)))
    new = await CognitoAuthenticator(session).async_refresh(AuthTokens("a@b.nl", "old", "refresh-1", 0))  # type: ignore[arg-type]
    assert session.calls[0].json["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert new.refresh_token == "refresh-1"


async def test_token_manager_uses_cache_then_refresh_then_login():
    updates: list[AuthTokens] = []
    session = cognito(
        (400, {"__type": "NotAuthorizedException", "message": "Refresh Token has expired"}),
        (200, CHALLENGE),
        (200, tokens_response()),
    )
    valid = AuthTokens("a@b.nl", make_jwt(), "r", 9e12)
    manager = TokenManager(CognitoAuthenticator(session), "a@b.nl", "pw", valid, updates.append)  # type: ignore[arg-type]

    assert await manager.async_get_access_token() == valid.access_token
    assert session.calls == []

    token = await manager.async_get_access_token(force_renew=True)
    assert targets(session) == ["InitiateAuth", "InitiateAuth", "RespondToAuthChallenge"]
    assert token == updates[-1].access_token


async def test_mfa_during_background_login_is_auth_error():
    session = cognito((200, CHALLENGE), (200, {"ChallengeName": "SMS_MFA", "Session": "s"}))
    manager = TokenManager(CognitoAuthenticator(session), "a@b.nl", "pw")  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError, match="MFA"):
        await manager.async_get_access_token()


async def test_without_password_expired_session_is_auth_error():
    session = cognito((400, {"__type": "NotAuthorizedException", "message": "expired"}))
    manager = TokenManager(CognitoAuthenticator(session), "a@b.nl", None, AuthTokens("a@b.nl", "x", "r", 0))  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError, match="wachtwoord"):
        await manager.async_get_access_token()


async def test_transport_retries_once_after_401():
    session = cognito((200, tokens_response()))
    responses = iter([FakeResponse(401, ""), FakeResponse(200, '{"userId": 5}')])
    session.add_callback("GET", "user-management/user", lambda _c: next(responses))
    manager = TokenManager(CognitoAuthenticator(session), "a@b.nl", "pw", AuthTokens("a@b.nl", "old", "r", 9e12))  # type: ignore[arg-type]

    user = await SolarEdgeMonitoringClient(session, manager).user.get_user()  # type: ignore[arg-type]

    gets = [c.headers["authorization"] for c in session.calls if c.method == "GET"]
    assert user.user_id == 5
    assert gets[0] == "Bearer old"
    assert gets[1] != "Bearer old"
