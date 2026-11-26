"""Encryption for tenant secrets (roadmap step 8): shop API credentials and cached access tokens.

AES-256-GCM with the key from SECRETS_KEY. Every sealed value is bound to a context string
("tenant id + purpose"), passed as GCM's associated data: a ciphertext copied into another
tenant's row, or used for another purpose, fails to decrypt instead of being accepted.

v4 keeps this key in Secret Manager; a later step can swap in Cloud KMS behind seal()/open_sealed()
without touching callers. HMAC keyed with the same secret (separate label) hashes one-time codes
(code_digest), so a Redis dump doesn't reveal them: a 6-digit code's plain SHA-256 can be
reversed by trying all million values, its HMAC can't without the key.
"""
import hashlib
import hmac
import json
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import settings

NONCE_BYTES = 12                     # GCM's standard nonce size; random per seal
VERSION = b"\x01"                    # format byte, so the scheme can change later


class SecretsUnavailable(RuntimeError):
    """SECRETS_KEY isn't set (local dev only): tenant secrets can't be stored or read."""


class SealError(ValueError):
    """A sealed value is corrupt, was made with another key, or belongs to another context."""


def _key() -> bytes:
    """The configured key, or SecretsUnavailable."""
    if settings.secrets_key is None:
        raise SecretsUnavailable("Set SECRETS_KEY in api/.env to connect order tracking")
    return settings.secrets_key


def seal(data: dict, context: str) -> bytes:
    """Encrypt a small JSON-able dict for one context (e.g. 'orders:<tenant id>')."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    plain = json.dumps(data, separators=(",", ":")).encode()
    return VERSION + nonce + AESGCM(_key()).encrypt(nonce, plain, context.encode())


def open_sealed(blob: bytes, context: str) -> dict:
    """Decrypt what seal() made for the same context; SealError for anything else."""
    if len(blob) <= 1 + NONCE_BYTES or blob[:1] != VERSION:
        raise SealError("not a sealed value")
    nonce, cipher = blob[1:1 + NONCE_BYTES], blob[1 + NONCE_BYTES:]
    try:
        return json.loads(AESGCM(_key()).decrypt(nonce, cipher, context.encode()))
    except InvalidTag as exc:
        raise SealError("sealed value doesn't match its key or context") from exc


def code_digest(code: str, context: str) -> str:
    """Keyed hash of a one-time code for one context (tenant, conversation, order)."""
    key = hmac.new(_key(), b"one-time-codes", hashlib.sha256).digest()    # separate subkey
    return hmac.new(key, f"{context}|{code}".encode(), hashlib.sha256).hexdigest()


def same_digest(a: str, b: str) -> bool:
    """Constant-time comparison, so response timing doesn't reveal how much of a code matched."""
    return hmac.compare_digest(a, b)
