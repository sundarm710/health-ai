"""Fake-but-structurally-real identity: a signed bearer token (HS256-style JWT, stdlib only).

In production the token comes from an identity provider (Keycloak/Okta/SMART on FHIR) and carries
scopes. Here /login issues one after checking the practitioner's *server-side* PractitionerRole, so
choosing "Doctor" in the UI for a nurse's account does not work. The middleware then trusts only the
signed claims, never the role the form claims.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

SECRET = os.environ.get("SIM_SECRET", "dev-only-not-a-secret").encode()


class AuthError(Exception):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(claims: dict, ttl: int = 3600, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({**claims, "iat": int(now), "exp": int(now + ttl)}).encode())
    sig = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def verify(token: str, *, now: float | None = None) -> dict:
    try:
        head, body, sig = token.split(".")
    except ValueError:
        raise AuthError("malformed token") from None
    good = _b64(hmac.new(SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        raise AuthError("bad signature")
    claims = json.loads(_unb64(body))
    if claims["exp"] < (time.time() if now is None else now):
        raise AuthError("token expired")
    return claims
