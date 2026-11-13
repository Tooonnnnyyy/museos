"""Password hashing and session tokens, from the standard library only.

No external crypto dependency: passwords are hashed with ``hashlib.scrypt``
(n=2^14, r=8, p=1, 32-byte key — the current OWASP minimum) over a per-user
random 16-byte salt, and compared in constant time. Session tokens are
``secrets.token_hex(32)`` (256 bits), stored hashed-free in the sessions table
but never derivable: an opaque, random bearer token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_N, _R, _P, _DKLEN = 16384, 8, 1, 32
_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"{_PREFIX}${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split("$")
        if algo != _PREFIX:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    return hmac.compare_digest(dk, expected)


def new_token() -> str:
    return secrets.token_hex(32)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    e = normalize_email(email)
    return bool(e) and "@" in e and "." in e.split("@")[-1] and len(e) <= 254 and " " not in e
