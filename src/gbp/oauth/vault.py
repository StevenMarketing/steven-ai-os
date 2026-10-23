"""Encrypted-at-rest, per-tenant OAuth token vault (Issue #110).

One record per tenant, one file per record, encrypted with a key derived from
``GBP_TOKEN_ENCRYPTION_KEY``. A record holds the access token, the refresh
token, the absolute expiry and a little non-secret bookkeeping (last sync time
and status) so the dashboard can show a connection state without ever reading a
token value.

Encryption
----------
``Fernet`` is used when the ``cryptography`` package is importable (opt in with
``GBP_VAULT_CIPHER=fernet``); otherwise an equivalent, dependency-free
construction is used -- HMAC-SHA256 keystream (CTR-style) with an
encrypt-then-MAC tag, keyed from the same secret. Both refuse to return
tampered ciphertext, and the file mode is ``0o600``.

The repository pins no dependencies, so the stdlib cipher is the default and is
what the tests exercise; the Fernet path is covered when the package exists.

Tenant isolation
----------------
Every method takes a tenant id and is served by :func:`gbp.oauth.tenancy.tenant_key`,
so a record can only ever be read back by the tenant that wrote it -- asking for
another tenant's id returns ``None``, never that tenant's data.

Standard library only, no network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gbp.auth.token_manager import Token
from gbp.oauth.tenancy import assert_owner, normalize_tenant, tenant_key

__all__ = [
    "VaultError",
    "TokenExpiredError",
    "StdlibCipher",
    "FernetCipher",
    "default_cipher",
    "TokenVault",
    "TOKEN_URL",
    "CIPHER_MAGIC",
]

#: Google's OAuth token endpoint (only ever reached through an injected transport).
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Domain separator so a vault blob is never confused with another HMAC use.
CIPHER_MAGIC = b"GBPV1"

_MAC_BYTES = 32
_NONCE_BYTES = 16
_SKEW_SECONDS = 60.0


class VaultError(RuntimeError):
    """A vault failure that is safe to show (it never contains a token)."""


class TokenExpiredError(VaultError):
    """The stored token cannot be used: no refresh token, or refresh failed.

    Callers map this onto the dashboard's ``token_expired`` connection state.
    """


# --------------------------------------------------------------------------- #
# ciphers
# --------------------------------------------------------------------------- #
def _xor(data: bytes, keystream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, keystream))


class StdlibCipher:
    """Encrypt-then-MAC with an HMAC-SHA256 keystream (stdlib only).

    ``encrypt`` output layout: ``MAGIC | nonce | tag | ciphertext``.
    """

    def __init__(self, secret: str,
                 nonce_source: Optional[Callable[[int], bytes]] = None) -> None:
        if not secret:
            raise VaultError("a non-empty vault secret is required")
        self._key = hashlib.sha256(str(secret).encode("utf-8")).digest()
        self._nonce_source: Callable[[int], bytes] = nonce_source or os.urandom

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            counter += 1
            out.extend(hmac.new(
                self._key, nonce + counter.to_bytes(4, "big"), hashlib.sha256
            ).digest())
        return bytes(out[:length])

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = self._nonce_source(_NONCE_BYTES)
        body = _xor(plaintext, self._keystream(nonce, len(plaintext)))
        tag = hmac.new(self._key, nonce + body, hashlib.sha256).digest()
        return CIPHER_MAGIC + nonce + tag + body

    def decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(CIPHER_MAGIC):
            raise VaultError("vault record is not a recognised ciphertext")
        rest = blob[len(CIPHER_MAGIC):]
        if len(rest) < _NONCE_BYTES + _MAC_BYTES:
            raise VaultError("vault record is truncated")
        nonce = rest[:_NONCE_BYTES]
        tag = rest[_NONCE_BYTES:_NONCE_BYTES + _MAC_BYTES]
        body = rest[_NONCE_BYTES + _MAC_BYTES:]
        expected = hmac.new(self._key, nonce + body, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise VaultError("vault integrity check failed (tampered or wrong key)")
        return _xor(body, self._keystream(nonce, len(body)))


class FernetCipher:
    """``cryptography`` Fernet, keyed from the same vault secret.

    Imported lazily so the module stays usable (and testable) without the
    package installed.
    """

    def __init__(self, secret: str) -> None:
        if not secret:
            raise VaultError("a non-empty vault secret is required")
        try:
            from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on the env
            raise VaultError(
                "GBP_VAULT_CIPHER=fernet requires the 'cryptography' package") from exc
        key = base64.urlsafe_b64encode(hashlib.sha256(str(secret).encode()).digest())
        self._fernet = Fernet(key)
        self._invalid = InvalidToken

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, blob: bytes) -> bytes:
        try:
            return self._fernet.decrypt(blob)
        except Exception as exc:  # noqa: BLE001 - normalised, never leaks the value
            raise VaultError("vault integrity check failed (tampered or wrong key)") from exc


def default_cipher(secret: str, *, prefer_fernet: bool = False,
                   environ: Optional[Dict[str, Any]] = None) -> Any:
    """Pick the cipher: Fernet on request (and availability), else stdlib."""
    if not prefer_fernet:
        env = environ if environ is not None else os.environ
        prefer_fernet = str(env.get("GBP_VAULT_CIPHER") or "").strip().lower() == "fernet"
    if prefer_fernet:
        return FernetCipher(secret)
    return StdlibCipher(secret)


# --------------------------------------------------------------------------- #
# vault
# --------------------------------------------------------------------------- #
#: Keys the vault never hands out (they are secret material).
_SECRET_KEYS = ("access_token", "refresh_token")

#: Connection states the dashboard understands.
STATE_NOT_CONNECTED = "not_connected"
STATE_CONNECTED = "connected"
STATE_TOKEN_EXPIRED = "token_expired"

#: ``last_error`` values that mean the token is no longer usable.
_DEAD_ERRORS = {"token_expired", "revoked", "invalid_grant", "no_refresh_token"}


class TokenVault:
    """Per-tenant, encrypted-at-rest token storage with auto-refresh.

    Parameters
    ----------
    directory:
        Where the ``<tenant_key>.token`` files live (mode ``0o600``).
    key:
        The vault secret (``GBP_TOKEN_ENCRYPTION_KEY``).
    cipher:
        Injectable cipher (tests use a fixed nonce source); by default the
        stdlib cipher is built from ``key``.
    token_url / client_id / client_secret:
        Used by :meth:`get_valid` when it has to refresh.
    clock:
        Injectable ``() -> float`` epoch clock.
    """

    def __init__(self, directory: str, key: str, *, cipher: Any = None,
                 token_url: str = TOKEN_URL,
                 client_id: Optional[str] = None,
                 client_secret: Optional[str] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.directory = str(directory)
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._clock: Callable[[], float] = clock or time.time
        self._cipher = cipher if cipher is not None else default_cipher(key)

    # -- paths / raw IO ---------------------------------------------------- #
    def path_for(self, tenant: Any) -> str:
        """Absolute path of a tenant's record (derived, never user-supplied)."""
        return os.path.join(self.directory, "%s.token" % tenant_key(tenant))

    def _read_record(self, tenant: Any) -> Optional[Dict[str, Any]]:
        path = Path(self.path_for(tenant))
        if not path.exists():
            return None
        raw = path.read_bytes()
        record = json.loads(self._cipher.decrypt(raw).decode("utf-8"))
        if not isinstance(record, dict):
            raise VaultError("vault record is not an object")
        # Defence in depth: a record whose contents name another tenant is
        # refused rather than returned.
        assert_owner(record.get("tenant_id"), tenant)
        return record

    def _write_record(self, tenant: Any, record: Dict[str, Any]) -> None:
        target = Path(self.path_for(tenant))
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = self._cipher.encrypt(
            json.dumps(record, sort_keys=True).encode("utf-8"))
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        try:  # an existing file keeps its old mode otherwise
            os.chmod(str(target), 0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    # -- write ------------------------------------------------------------- #
    def save(self, tenant: Any, token: Token, *, last_sync_at: Optional[str] = None,
             sync_status: Optional[str] = None,
             last_error: Optional[str] = None) -> Dict[str, Any]:
        """Persist ``token`` for ``tenant`` and return its non-secret meta."""
        tid = normalize_tenant(tenant)
        previous = None
        try:
            previous = self._read_record(tid)
        except VaultError:
            previous = None  # unreadable/corrupt: overwrite cleanly
        now = self._clock()
        record: Dict[str, Any] = {
            "tenant_id": tid,
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": float(token.expires_at or 0.0),
            "scopes": list(token.scopes),
            "token_type": token.token_type,
            "created_at": (previous or {}).get("created_at", now),
            "updated_at": now,
            "last_sync_at": last_sync_at if last_sync_at is not None
                            else (previous or {}).get("last_sync_at"),
            "sync_status": sync_status if sync_status is not None
                           else (previous or {}).get("sync_status"),
            "last_error": last_error,
        }
        self._write_record(tid, record)
        return self._public(record)

    def note_outcome(self, tenant: Any, status: str, *,
                     at: Optional[str] = None, error: Optional[str] = None,
                     clear_error: bool = False) -> Optional[Dict[str, Any]]:
        """Record a whole sync outcome (status + time + optional safe reason).

        Issue #117: every sync attempt must leave a trace. ``error`` is a safe
        reason (exception class / API error code only -- the caller is
        responsible for reducing it); ``clear_error=True`` forgets a previous
        failure after a success. Nothing here touches the token material.
        """
        try:
            record = self._read_record(tenant)
        except VaultError:
            return None
        if record is None:
            return None
        record["sync_status"] = str(status)
        record["last_sync_at"] = at or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
        if error is not None:
            record["last_error"] = str(error)
        elif clear_error:
            record["last_error"] = None
        self._write_record(tenant, record)
        return self._public(record)

    def note_sync(self, tenant: Any, status: str,
                  at: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Record the last sync result (and time) without touching the token."""
        try:
            record = self._read_record(tenant)
        except VaultError:
            return None
        if record is None:
            return None
        record["sync_status"] = str(status)
        record["last_sync_at"] = at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))
        self._write_record(tenant, record)
        return self._public(record)

    def mark_error(self, tenant: Any, reason: str) -> None:
        """Flag the record as unusable (``token_expired``/``revoked``)."""
        try:
            record = self._read_record(tenant)
        except VaultError:
            return None
        if record is None:
            return None
        record["last_error"] = str(reason)
        self._write_record(tenant, record)
        return None

    def delete(self, tenant: Any) -> bool:
        """Forget a tenant's record; returns whether one was removed."""
        path = Path(self.path_for(tenant))
        if not path.exists():
            return False
        path.unlink()
        return True

    # -- read -------------------------------------------------------------- #
    def record(self, tenant: Any) -> Optional[Dict[str, Any]]:
        """The decrypted record (tests only -- contains tokens, never log it)."""
        return self._read_record(tenant)

    @staticmethod
    def _public(record: Dict[str, Any]) -> Dict[str, Any]:
        """The non-secret projection of a record."""
        return {
            "tenant_id": record.get("tenant_id"),
            "expires_at": record.get("expires_at"),
            "scopes": list(record.get("scopes") or []),
            "token_type": record.get("token_type"),
            "has_refresh_token": bool(record.get("refresh_token")),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "last_sync_at": record.get("last_sync_at"),
            "sync_status": record.get("sync_status"),
            "last_error": record.get("last_error"),
        }

    def meta(self, tenant: Any) -> Optional[Dict[str, Any]]:
        """Non-secret metadata for ``tenant`` (``None`` when nothing is stored)."""
        record = self._read_record(tenant)
        return self._public(record) if record is not None else None

    def load(self, tenant: Any) -> Optional[Token]:
        """The stored token for ``tenant`` (``None`` when nothing is stored)."""
        record = self._read_record(tenant)
        if record is None:
            return None
        return Token.from_dict(record)

    def connection_state(self, tenant: Any, *, now: Optional[float] = None) -> str:
        """``not_connected`` / ``connected`` / ``token_expired`` for ``tenant``."""
        record = self._read_record(tenant)
        if record is None:
            return STATE_NOT_CONNECTED
        if str(record.get("last_error") or "") in _DEAD_ERRORS:
            return STATE_TOKEN_EXPIRED
        token = Token.from_dict(record)
        if not token.access_token and not token.refresh_token:
            return STATE_TOKEN_EXPIRED
        if not token.refresh_token and token.is_expired(
                now if now is not None else self._clock(), _SKEW_SECONDS):
            # Nothing to refresh with -> the connection needs a human.
            return STATE_TOKEN_EXPIRED
        return STATE_CONNECTED

    def is_expired(self, tenant: Any, *, now: Optional[float] = None) -> bool:
        """True when the stored access token is missing or expired."""
        record = self._read_record(tenant)
        if record is None:
            return True
        token = Token.from_dict(record)
        return token.is_expired(now if now is not None else self._clock(), _SKEW_SECONDS)

    # -- refresh ----------------------------------------------------------- #
    def get_valid(self, tenant: Any, transport: Any = None, *,
                  now: Optional[float] = None) -> Token:
        """Return a usable token, refreshing it first when it has expired.

        Raises :class:`TokenExpiredError` -- and flags the record -- when there
        is nothing to refresh with or the refresh is rejected, which is what
        the dashboard surfaces as ``token_expired``.
        """
        stamp = now if now is not None else self._clock()
        record = self._read_record(tenant)
        if record is None:
            raise TokenExpiredError("not_connected")
        token = Token.from_dict(record)
        if not token.is_expired(stamp, _SKEW_SECONDS):
            return token
        if not token.refresh_token:
            self.mark_error(tenant, "token_expired")
            raise TokenExpiredError("no_refresh_token")
        if transport is None:
            self.mark_error(tenant, "token_expired")
            raise TokenExpiredError("refresh_transport_missing")
        data: Dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        if self.client_id:
            data["client_id"] = self.client_id
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            response = transport.post(self.token_url, data=data)
        except Exception:  # noqa: BLE001 - provider failure -> honest state
            self.mark_error(tenant, "token_expired")
            raise TokenExpiredError("refresh_transport_failed") from None
        payload = response.json() if hasattr(response, "json") else (response or {})
        status = getattr(response, "status", 200)
        if not isinstance(payload, dict) or int(status) >= 400 or not payload.get("access_token"):
            self.mark_error(tenant, "token_expired")
            raise TokenExpiredError("refresh_rejected")
        try:
            expires_in = float(payload.get("expires_in") or 0.0)
        except (TypeError, ValueError):
            expires_in = 0.0
        refreshed = Token(
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token") or token.refresh_token,
            expires_at=stamp + expires_in,
            scopes=payload.get("scopes") or token.scopes,
            token_type=payload.get("token_type") or token.token_type,
        )
        self.save(tenant, refreshed, last_error=None)
        return refreshed
