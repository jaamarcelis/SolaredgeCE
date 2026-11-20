"""Python-client voor de interne API van monitoring.solaredge.com.

Python-versie van de .NET-client ``SolarEdge.Monitoring.Client``. Onafhankelijk van Home Assistant:
alleen ``aiohttp`` is nodig.
"""

from .auth import AuthTokens, CognitoAuthenticator, TokenManager
from .client import SolarEdgeMonitoringClient, default_site_filter, measurement_datasource
from .exceptions import (
    ApiError,
    AuthenticationError,
    CloudflareError,
    InvalidCredentialsError,
    MfaRequiredError,
    NotApplicableError,
    SolarEdgeError,
)

__all__ = [
    "ApiError",
    "AuthTokens",
    "AuthenticationError",
    "CloudflareError",
    "CognitoAuthenticator",
    "InvalidCredentialsError",
    "MfaRequiredError",
    "NotApplicableError",
    "SolarEdgeError",
    "SolarEdgeMonitoringClient",
    "TokenManager",
    "default_site_filter",
    "measurement_datasource",
]
