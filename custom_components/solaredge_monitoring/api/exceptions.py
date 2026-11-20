"""Exceptions van de SolarEdge Monitoring API-client."""

from __future__ import annotations

from typing import Any


class SolarEdgeError(Exception):
    """Basisklasse voor alle fouten van de client."""


class AuthenticationError(SolarEdgeError):
    """Inloggen of vernieuwen is mislukt, of de sessie is niet meer geldig."""


class InvalidCredentialsError(AuthenticationError):
    """Gebruikersnaam of wachtwoord wordt geweigerd (Cognito NotAuthorizedException)."""


class MfaRequiredError(AuthenticationError):
    """Cognito vraagt om een MFA-code. Rond af met ``CognitoAuthenticator.async_respond_to_mfa``."""

    def __init__(self, mfa_type: str, session: str, username: str) -> None:
        """Bewaar de gegevens die nodig zijn om de MFA-stap af te ronden."""
        super().__init__(f"MFA vereist ({mfa_type})")
        self.mfa_type = mfa_type  # "SMS_MFA" of "SOFTWARE_TOKEN_MFA"
        self.session = session
        self.username = username


class ApiError(SolarEdgeError):
    """De API gaf een foutstatus of onleesbare inhoud."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        raw: str | None = None,
    ) -> None:
        """Bewaar status, foutcode en ruwe body."""
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.raw = raw


class NotApplicableError(ApiError):
    """Het endpoint is niet van toepassing op deze site of dit account.

    Bijvoorbeeld ``BAD_ARGUMENTS`` (geen batterij), ``EMS_FEATURE_NOT_ENABLED`` of een 403 op de
    alert-tellingen voor een eigenaarsaccount.
    """


class CloudflareError(ApiError):
    """De API gaf HTML in plaats van JSON (Cloudflare-controle of loginpagina)."""


NOT_APPLICABLE_CODES = frozenset({"BAD_ARGUMENTS", "EMS_FEATURE_NOT_ENABLED"})


def parse_error(raw: str | None) -> tuple[str | None, str | None]:
    """Herken de drie foutformaten van de API en geef (code, omschrijving).

    1. ``{"errorCode": "...", "message": "..."}`` (analysis)
    2. ``{"timestamp": ..., "status": 400, "error": "Bad Request", "path": ...}`` (Spring)
    3. ``{"error": {"code": "BAD_ARGUMENTS", "description": "..."}}`` (dashboard)
    Daarnaast ``{"message": "..."}`` (alerts).
    """
    import json  # noqa: PLC0415

    if not raw:
        return None, None
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("description")
    if "errorCode" in data or "message" in data:
        return data.get("errorCode"), data.get("message")
    if isinstance(error, str):
        return error, None
    return None, None
