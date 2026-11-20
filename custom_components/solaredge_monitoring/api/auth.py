"""Login via AWS Cognito (SRP) en tokenbeheer, zonder AWS SDK."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import json
import logging
import time
from typing import Any

import aiohttp

from .const import COGNITO_CLIENT_ID, COGNITO_REGION, COGNITO_USER_POOL_ID
from .exceptions import AuthenticationError, InvalidCredentialsError, MfaRequiredError
from .srp import CognitoSrp

_LOGGER = logging.getLogger(__name__)

EXPIRY_MARGIN = 300  # seconden: token vernieuwen als het binnen 5 minuten verloopt
MFA_CHALLENGES = ("SMS_MFA", "SOFTWARE_TOKEN_MFA")


@dataclass(slots=True)
class AuthTokens:
    """Tokens van Cognito. ``expires_at`` is een Unix-tijdstip (seconden)."""

    username: str
    access_token: str
    refresh_token: str | None
    expires_at: float
    id_token: str | None = None

    def is_valid(self, margin: float = EXPIRY_MARGIN) -> bool:
        """Of het access token nog minstens ``margin`` seconden geldig is."""
        return time.time() + margin < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        """Voor opslag in de config entry."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuthTokens | None:
        """Uit opgeslagen data; ``None`` als die ontbreekt of onvolledig is."""
        if not data or not data.get("access_token"):
            return None
        return cls(
            username=data.get("username", ""),
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0)),
            id_token=data.get("id_token"),
        )


def jwt_expiry(token: str) -> float:
    """Lees de ``exp``-claim uit een JWT (zonder de handtekening te controleren)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError) as err:
        raise AuthenticationError("Ongeldig token van Cognito") from err


class CognitoAuthenticator:
    """Login (USER_SRP_AUTH), MFA en tokenvernieuwing tegen de user pool van monitoring.solaredge.com."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        user_pool_id: str = COGNITO_USER_POOL_ID,
        client_id: str = COGNITO_CLIENT_ID,
        region: str = COGNITO_REGION,
    ) -> None:
        """Gebruik ``session`` voor de aanroepen naar de Cognito JSON-API."""
        self._session = session
        self._user_pool_id = user_pool_id
        self._client_id = client_id
        self._url = f"https://cognito-idp.{region}.amazonaws.com/"

    async def _call(self, target: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        }
        async with self._session.post(self._url, data=json.dumps(body), headers=headers) as resp:
            text = await resp.text()
            data: dict[str, Any] = json.loads(text) if text else {}
            if resp.status >= 400:
                error_type = str(data.get("__type", "")).split("#")[-1]
                message = data.get("message") or data.get("Message") or text
                if error_type in ("NotAuthorizedException", "UserNotFoundException", "CodeMismatchException"):
                    raise InvalidCredentialsError(message)
                raise AuthenticationError(f"Cognito {target} mislukt ({error_type or resp.status}): {message}")
            return data

    def _tokens(self, username: str, result: dict[str, Any], previous: AuthTokens | None = None) -> AuthTokens:
        access = result["AccessToken"]
        return AuthTokens(
            username=username,
            access_token=access,
            # Bij een refresh geeft Cognito meestal geen nieuw refresh token terug
            refresh_token=result.get("RefreshToken") or (previous.refresh_token if previous else None),
            expires_at=jwt_expiry(access),
            id_token=result.get("IdToken") or (previous.id_token if previous else None),
        )

    async def async_login(self, username: str, password: str) -> AuthTokens:
        """Log in met gebruikersnaam en wachtwoord.

        :raises InvalidCredentialsError: verkeerd wachtwoord of onbekende gebruiker.
        :raises MfaRequiredError: er is een MFA-code nodig; rond af met :meth:`async_respond_to_mfa`.
        """
        srp = CognitoSrp(self._user_pool_id)
        init = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "USER_SRP_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"USERNAME": username, "SRP_A": srp.srp_a},
            },
        )
        if init.get("ChallengeName") != "PASSWORD_VERIFIER":
            raise AuthenticationError(f"Onverwachte Cognito-stap: {init.get('ChallengeName')}")

        challenge = init["ChallengeParameters"]
        body: dict[str, Any] = {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ClientId": self._client_id,
            "ChallengeResponses": srp.password_verifier_response(challenge, password),
        }
        if init.get("Session"):
            body["Session"] = init["Session"]
        response = await self._call("RespondToAuthChallenge", body)
        return self._handle_auth_response(response, challenge["USER_ID_FOR_SRP"], username)

    async def async_respond_to_mfa(self, mfa: MfaRequiredError, code: str, username: str) -> AuthTokens:
        """Rond een login af met de MFA-code."""
        code_key = "SMS_MFA_CODE" if mfa.mfa_type == "SMS_MFA" else "SOFTWARE_TOKEN_MFA_CODE"
        response = await self._call(
            "RespondToAuthChallenge",
            {
                "ChallengeName": mfa.mfa_type,
                "ClientId": self._client_id,
                "Session": mfa.session,
                "ChallengeResponses": {"USERNAME": mfa.username, code_key: code.strip()},
            },
        )
        return self._handle_auth_response(response, mfa.username, username)

    def _handle_auth_response(self, response: dict[str, Any], user_id: str, username: str) -> AuthTokens:
        if "AuthenticationResult" in response:
            return self._tokens(username, response["AuthenticationResult"])
        challenge = response.get("ChallengeName")
        if challenge in MFA_CHALLENGES:
            raise MfaRequiredError(challenge, response["Session"], user_id)
        if challenge == "NEW_PASSWORD_REQUIRED":
            raise AuthenticationError("SolarEdge vereist een nieuw wachtwoord; log eenmalig in via de website.")
        raise AuthenticationError(f"Onbekende Cognito-stap: {challenge}")

    async def async_refresh(self, tokens: AuthTokens) -> AuthTokens:
        """Haal een nieuw access token op met het refresh token.

        :raises InvalidCredentialsError: het refresh token is verlopen of ingetrokken.
        """
        if not tokens.refresh_token:
            raise InvalidCredentialsError("Geen refresh token")
        response = await self._call(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self._client_id,
                "AuthParameters": {"REFRESH_TOKEN": tokens.refresh_token},
            },
        )
        if "AuthenticationResult" not in response:
            raise AuthenticationError(f"Vernieuwen gaf geen tokens (stap {response.get('ChallengeName')})")
        return self._tokens(tokens.username, response["AuthenticationResult"], tokens)


class TokenManager:
    """Levert een geldig access token: gecacht → vernieuwd met refresh token → opnieuw inloggen met wachtwoord.

    Nieuwe tokens worden via ``on_update`` doorgegeven (bijv. om ze in de config entry op te slaan).
    Lukt stil inloggen niet (MFA of verkeerd wachtwoord), dan volgt een :class:`AuthenticationError`.
    """

    def __init__(
        self,
        authenticator: CognitoAuthenticator,
        username: str,
        password: str | None,
        tokens: AuthTokens | None = None,
        on_update: Callable[[AuthTokens], Awaitable[None] | None] | None = None,
    ) -> None:
        """Start met eventueel opgeslagen tokens."""
        self._auth = authenticator
        self._username = username
        self._password = password
        self._tokens = tokens
        self._on_update = on_update
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> AuthTokens | None:
        """De huidige tokens."""
        return self._tokens

    async def async_get_access_token(self, force_renew: bool = False) -> str:
        """Geef een geldig access token (thread-safe; gelijktijdige aanroepen wachten op één login)."""
        async with self._lock:
            tokens = self._tokens
            if tokens and not force_renew and tokens.is_valid():
                return tokens.access_token

            if tokens and tokens.refresh_token:
                try:
                    return await self._store(await self._auth.async_refresh(tokens))
                except InvalidCredentialsError as err:
                    _LOGGER.info("Refresh token geweigerd (%s); opnieuw inloggen", err)

            if not self._password:
                raise AuthenticationError("Sessie verlopen en geen wachtwoord bekend; opnieuw inloggen nodig")
            try:
                return await self._store(await self._auth.async_login(self._username, self._password))
            except MfaRequiredError as err:
                raise AuthenticationError("Opnieuw inloggen vereist een MFA-code") from err

    async def _store(self, tokens: AuthTokens) -> str:
        self._tokens = tokens
        if self._on_update:
            result = self._on_update(tokens)
            if asyncio.iscoroutine(result):
                await result
        return tokens.access_token
