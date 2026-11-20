"""Secure Remote Password (SRP-6a) zoals AWS Cognito het gebruikt voor ``USER_SRP_AUTH``.

Zelfde berekening als ``Amazon.Extensions.CognitoAuthentication`` (de .NET-client) en ``pycognito``, maar
zonder AWS SDK: alleen ``hashlib``/``hmac``. Het wachtwoord gaat nooit over de lijn.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac
import secrets

# RFC 3526, 3072-bit MODP-groep (de groep die Cognito gebruikt)
N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
G_HEX = "2"
INFO_BITS = b"Caldera Derived Key"

N = int(N_HEX, 16)
G = int(G_HEX, 16)

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _hash_sha256(data: bytes) -> str:
    """SHA-256 als hex, aangevuld tot 64 tekens."""
    return hashlib.sha256(data).hexdigest().rjust(64, "0")


def _hex_hash(hex_string: str) -> str:
    return _hash_sha256(bytes.fromhex(hex_string))


def _long_to_hex(value: int) -> str:
    return f"{value:x}"


def pad_hex(value: int | str) -> str:
    """Hex-representatie zoals Cognito die verwacht: even lengte en geen 'negatief' hoogste bit."""
    text = value if isinstance(value, str) else _long_to_hex(value)
    if len(text) % 2 == 1:
        return "0" + text
    if text[0] in "89ABCDEFabcdef":
        return "00" + text
    return text


K = int(_hex_hash("00" + N_HEX + "0" + G_HEX), 16)


def _compute_hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, INFO_BITS + b"\x01", hashlib.sha256).digest()[:16]


def cognito_timestamp(now: datetime | None = None) -> str:
    """Tijdstempel in het formaat ``Tue Nov 3 14:05:09 UTC 2026`` (dag zonder voorloopnul, Engelse namen)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return f"{_DAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day} {now:%H:%M:%S} UTC {now.year}"


class CognitoSrp:
    """Eén SRP-login: maakt ``SRP_A`` en beantwoordt daarna de ``PASSWORD_VERIFIER``-challenge."""

    def __init__(self, user_pool_id: str, small_a: int | None = None) -> None:
        """Maak een nieuw willekeurig geheim ``a`` (of gebruik ``small_a`` in tests)."""
        self.pool_name = user_pool_id.split("_", 1)[1]
        self.small_a = small_a if small_a is not None else int.from_bytes(secrets.token_bytes(128), "big") % N
        self.large_a = pow(G, self.small_a, N)
        if self.large_a % N == 0:
            raise ValueError("Ongeldige SRP-waarde A")

    @property
    def srp_a(self) -> str:
        """De publieke waarde ``A`` als hex, voor ``InitiateAuth``."""
        return _long_to_hex(self.large_a)

    def password_authentication_key(self, username: str, password: str, server_b: int, salt_hex: str) -> bytes:
        """Gedeelde sleutel volgens SRP-6a met de afleiding van Cognito."""
        u = int(_hex_hash(pad_hex(self.large_a) + pad_hex(server_b)), 16)
        if u == 0:
            raise ValueError("Ongeldige SRP-waarde u")
        user_password_hash = _hash_sha256(f"{self.pool_name}{username}:{password}".encode())
        x = int(_hex_hash(pad_hex(salt_hex) + user_password_hash), 16)
        s = pow(server_b - K * pow(G, x, N), self.small_a + u * x, N)
        return _compute_hkdf(bytes.fromhex(pad_hex(s)), bytes.fromhex(pad_hex(_long_to_hex(u))))

    def password_verifier_response(
        self, challenge: dict[str, str], password: str, timestamp: str | None = None
    ) -> dict[str, str]:
        """Antwoord op de ``PASSWORD_VERIFIER``-challenge (``ChallengeResponses`` van RespondToAuthChallenge)."""
        user_id = challenge["USER_ID_FOR_SRP"]
        key = self.password_authentication_key(user_id, password, int(challenge["SRP_B"], 16), challenge["SALT"])
        timestamp = timestamp or cognito_timestamp()
        secret_block = challenge["SECRET_BLOCK"]
        message = self.pool_name.encode() + user_id.encode() + base64.standard_b64decode(secret_block) + timestamp.encode()
        signature = base64.standard_b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()
        return {
            "TIMESTAMP": timestamp,
            "USERNAME": user_id,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
            "PASSWORD_CLAIM_SIGNATURE": signature,
        }
