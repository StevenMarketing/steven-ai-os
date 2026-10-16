"""GBP client package.

Holds the offline mock client plus the retrying HTTP transport, the in-process
QPM limiter and the thin GBP API client.
"""

from gbp.client.mock_client import (
    AuthError,
    GBPError,
    MockGBPClient,
    RateLimitedError,
    with_backoff,
)
from gbp.client.http_client import (
    ApiError,
    HttpClient,
    MockTransport,
    Response,
    Transport,
)
from gbp.client.quota import QuotaLimiter
from gbp.client.gbp_client import GbpClient

__all__ = [
    "GBPError",
    "RateLimitedError",
    "AuthError",
    "ApiError",
    "MockGBPClient",
    "with_backoff",
    "Response",
    "Transport",
    "MockTransport",
    "HttpClient",
    "QuotaLimiter",
    "GbpClient",
]
