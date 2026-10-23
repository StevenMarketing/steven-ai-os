"""Per-tenant Google Business Profile OAuth connect (Issue #110, increment 1).

This package owns the *live* connection groundwork and nothing else:

* :mod:`gbp.oauth.config` -- where credentials come from (environment only);
* :mod:`gbp.oauth.tenancy` -- the tenant-scoping guarantees every store obeys;
* :mod:`gbp.oauth.state` -- single-use, TTL-bounded connect flows per tenant;
* :mod:`gbp.oauth.vault` -- encrypted-at-rest, per-tenant token storage;
* :mod:`gbp.oauth.flow` -- the authorization-code connect/callback orchestration.

Nothing here opens a socket by itself: every network hop goes through an
INJECTED transport, so the whole package is exercised offline in tests.

The existing single-connection pieces (``gbp.auth.oauth`` /
``gbp.auth.token_manager``) and ``scripts/google_flow.py`` are left untouched:
``src/gbp/auth/**`` is a protected path and this increment does not need to
change it.
"""

from gbp.oauth.config import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    ENCRYPTION_KEY_ENV,
    LIVE_ENV,
    REDIRECT_ENV,
    VAULT_DIR_ENV,
    LiveNotConfigured,
    OAuthConfig,
    encryption_key,
    live_enabled,
    load_config,
    require_config,
    vault_dir,
)
from gbp.oauth.tenancy import (
    TenantIsolationError,
    assert_owner,
    normalize_tenant,
    same_tenant,
    scope_rows,
    tenant_key,
)
from gbp.oauth.state import StateStore, FlowStateError
from gbp.oauth.vault import (
    FernetCipher,
    StdlibCipher,
    TokenExpiredError,
    TokenVault,
    VaultError,
    default_cipher,
)
from gbp.oauth.flow import (
    OAuthExchangeError,
    connection_rows,
    disconnect,
    handle_callback,
    start_connect,
)
from gbp.oauth.wiring import GoogleConnectWiring

__all__ = [
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "ENCRYPTION_KEY_ENV",
    "LIVE_ENV",
    "REDIRECT_ENV",
    "VAULT_DIR_ENV",
    "LiveNotConfigured",
    "OAuthConfig",
    "encryption_key",
    "live_enabled",
    "load_config",
    "require_config",
    "vault_dir",
    "TenantIsolationError",
    "assert_owner",
    "normalize_tenant",
    "same_tenant",
    "scope_rows",
    "tenant_key",
    "StateStore",
    "FlowStateError",
    "FernetCipher",
    "StdlibCipher",
    "TokenExpiredError",
    "TokenVault",
    "VaultError",
    "default_cipher",
    "OAuthExchangeError",
    "connection_rows",
    "disconnect",
    "handle_callback",
    "start_connect",
    "GoogleConnectWiring",
]
