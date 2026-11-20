"""SRP-berekening vergeleken met de referentie-implementatie pycognito (alleen in de dev-omgeving)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac

import pytest

from custom_components.solaredge_monitoring.api.srp import CognitoSrp, N, cognito_timestamp, pad_hex

POOL = "eu-central-1_TestPool"
SMALL_A = int("1f" * 64, 16)
SERVER_B = pow(2, int("2a" * 64, 16), N)  # willekeurige geldige B
SALT = "8f3c1a2b4d5e6f708192a3b4c5d6e7f8"


def test_group_is_3072_bit():
    assert N.bit_length() == 3072
    # Snelle Fermat-test: N is (waarschijnlijk) priem
    assert pow(2, N - 1, N) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0xABC, "0abc"), (0x7F, "7f"), (0x80, "0080"), ("ff", "00ff"), ("1234", "1234")],
)
def test_pad_hex(value, expected):
    assert pad_hex(value) == expected


def test_timestamp_format_has_no_leading_zero_day():
    assert cognito_timestamp(datetime(2026, 11, 3, 4, 5, 9, tzinfo=UTC)) == "Tue Nov 3 04:05:09 UTC 2026"
    assert cognito_timestamp(datetime(2026, 11, 20, 14, 0, 0, tzinfo=UTC)) == "Fri Nov 20 14:00:00 UTC 2026"


def test_matches_pycognito_reference():
    aws_srp = pytest.importorskip("pycognito.aws_srp")
    # Dummy-client: alleen de wiskunde wordt gebruikt, geen AWS-aanroepen
    reference = aws_srp.AWSSRP("user@example.com", "geheim", POOL, "client", client=object())
    reference.small_a_value = SMALL_A
    reference.large_a_value = reference.calculate_a()

    ours = CognitoSrp(POOL, small_a=SMALL_A)
    assert ours.srp_a == aws_srp.long_to_hex(reference.large_a_value)

    expected = reference.get_password_authentication_key("user-id-123", "geheim", SERVER_B, SALT)
    assert ours.password_authentication_key("user-id-123", "geheim", SERVER_B, SALT) == expected


def test_password_verifier_signature():
    srp = CognitoSrp(POOL, small_a=SMALL_A)
    secret_block = base64.standard_b64encode(b"secret-block").decode()
    challenge = {"USER_ID_FOR_SRP": "user-id-123", "SRP_B": f"{SERVER_B:x}", "SALT": SALT, "SECRET_BLOCK": secret_block}

    response = srp.password_verifier_response(challenge, "geheim", timestamp="Fri Nov 20 14:00:00 UTC 2026")

    key = srp.password_authentication_key("user-id-123", "geheim", SERVER_B, SALT)
    message = b"TestPool" + b"user-id-123" + b"secret-block" + b"Fri Nov 20 14:00:00 UTC 2026"
    expected = base64.standard_b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode()
    assert response == {
        "TIMESTAMP": "Fri Nov 20 14:00:00 UTC 2026",
        "USERNAME": "user-id-123",
        "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
        "PASSWORD_CLAIM_SIGNATURE": expected,
    }


def test_random_a_differs():
    assert CognitoSrp(POOL).small_a != CognitoSrp(POOL).small_a
