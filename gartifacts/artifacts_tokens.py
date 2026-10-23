"""
Content tokens for the artifact content host.

A sandboxed frame cannot send cookies, so each content request carries a token in its
URL path. The token is an encrypted, expiring claim naming the signed-in user and the
bundle; encrypting it keeps the user's email out of logs and out of the artifact's own
script. A leaked token lets anyone read that one bundle, as that user, until it
expires, so lifetimes are short.
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

TOKEN_KEY_ENV = "WORKSPACE_MCP_ARTIFACTS_TOKEN_KEY"
TOKEN_TTL_ENV = "WORKSPACE_MCP_ARTIFACTS_TOKEN_TTL_SECONDS"
DEFAULT_TOKEN_TTL_SECONDS = 3600
MIN_TOKEN_TTL_SECONDS = 60
MAX_TOKEN_TTL_SECONDS = 86400

_PURPOSE = "artifact-content"
_VERSION = 1
_HKDF_INFO = b"workspace-mcp artifacts content token v1"


class TokenConfigError(RuntimeError):
    """Raised when the token key is missing or unusable."""


@dataclass(frozen=True)
class ContentClaims:
    """What a verified content token says."""

    artifact_hash: str
    email: str
    expires_at: float


def token_ttl_seconds() -> int:
    """Configured token lifetime, clamped to a sane range."""
    raw = os.getenv(TOKEN_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_TOKEN_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", TOKEN_TTL_ENV, raw)
        return DEFAULT_TOKEN_TTL_SECONDS
    return max(MIN_TOKEN_TTL_SECONDS, min(MAX_TOKEN_TTL_SECONDS, value))


def _fernet(secret: Optional[str] = None) -> Fernet:
    material = secret if secret is not None else os.getenv(TOKEN_KEY_ENV, "")
    if len(material.strip()) < 16:
        raise TokenConfigError(
            f"{TOKEN_KEY_ENV} must be set to a secret of at least 16 characters"
        )
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO
    ).derive(material.strip().encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def mint_content_token(
    artifact_hash: str,
    email: str,
    *,
    ttl_seconds: Optional[int] = None,
    now: Optional[float] = None,
    secret: Optional[str] = None,
) -> str:
    """Return a URL-safe token for ``email`` to read bundle ``artifact_hash``."""
    issued = time.time() if now is None else now
    ttl = token_ttl_seconds() if ttl_seconds is None else ttl_seconds
    payload = {
        "v": _VERSION,
        "p": _PURPOSE,
        "h": artifact_hash,
        "e": email.strip().lower(),
        "x": issued + ttl,
    }
    token = _fernet(secret).encrypt(json.dumps(payload).encode("utf-8"))
    return token.decode("ascii").rstrip("=")


def verify_content_token(
    token: str, *, now: Optional[float] = None, secret: Optional[str] = None
) -> Optional[ContentClaims]:
    """Return the claims of a valid, unexpired token; None for anything else."""
    if not token or len(token) > 2048:
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = _fernet(secret).decrypt(padded.encode("ascii"))
        payload = json.loads(raw)
    except (InvalidToken, ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != _VERSION or payload.get("p") != _PURPOSE:
        return None
    artifact_hash, email, expires = payload.get("h"), payload.get("e"), payload.get("x")
    if not isinstance(artifact_hash, str) or not isinstance(email, str):
        return None
    if not isinstance(expires, (int, float)):
        return None
    current = time.time() if now is None else now
    if current >= expires:
        return None
    return ContentClaims(artifact_hash=artifact_hash, email=email, expires_at=expires)
