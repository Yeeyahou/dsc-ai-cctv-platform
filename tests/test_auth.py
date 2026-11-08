import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from dsc.backend.auth import CognitoVerifier
from dsc.backend.errors import Problem


@pytest.fixture
def identity():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = CognitoVerifier("https://cognito-idp.test/pool", ["allowed"])
    verifier.keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key())
    )
    claims = {
        "iss": "https://cognito-idp.test/pool",
        "sub": "subject",
        "client_id": "allowed",
        "exp": int(time.time()) + 60,
        "token_use": "access",
        "scope": "dsc/aas.read",
    }
    return private, verifier, claims


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": 1},
        {"token_use": "id"},
        {"client_id": "other"},
        {"iss": "https://attacker.test/pool"},
    ],
)
def test_invalid_token_claims(identity, changes):
    private, verifier, claims = identity
    token = jwt.encode({**claims, **changes}, private, algorithm="RS256")
    with pytest.raises(Problem) as exc:
        verifier(token)
    assert exc.value.status == 401


def test_valid_access_token_and_signature(identity):
    private, verifier, claims = identity
    assert verifier(jwt.encode(claims, private, algorithm="RS256"))["client_id"] == "allowed"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Problem):
        verifier(jwt.encode(claims, other, algorithm="RS256"))
