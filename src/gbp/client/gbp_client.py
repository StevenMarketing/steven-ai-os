"""Thin GBP API client over an injected transport (offline, stdlib only).

Every method funnels through :meth:`GbpClient._request`, which (a) passes the
call through the optional :class:`~gbp.client.quota.QuotaLimiter` and (b) issues
the request via ``self.http`` (a
:class:`~gbp.client.http_client.HttpClient` in production, backed by a
:class:`~gbp.client.http_client.MockTransport` in tests).

Endpoints (Google Business Profile):

* ``GET  /v1/accounts``                                    -- list accounts
* ``GET  /v1/{account}/locations``                          -- list locations
* ``GET  /v4/{location}/reviews``                           -- list reviews
* ``GET  /v4/{review_name}``                                -- get one review
* ``PUT  /v4/{review_name}/reply``                          -- DRAFT reply only
* ``POST /v4/{account}/locations:batchGetReviews``          -- batch reviews

The ``update_reply`` method is provided so a *draft* can be validated against
the contract. It is never invoked by the sync layer and must not be called
live without the owner's explicit approval.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

__all__ = ["GbpClient", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://mybusiness.googleapis.com"

#: Page sizes mirror the documented maxima.
LOCATIONS_PAGE_SIZE = 100
REVIEWS_PAGE_SIZE = 50

#: ``locations.list`` REQUIRES a ``readMask`` (Issue #121). Without it the live
#: endpoint answers 400 INVALID_ARGUMENT ("field read_mask ... Field is
#: required"); offline fake transports never enforce this, so the defect only
#: surfaces live. The mask covers exactly the fields the mapping layer reads
#: (:func:`gbp.sync.live_sync.map_location`): ``name``, ``title`` and
#: ``storefrontAddress`` (postalCode / locality / administrativeArea).
LOCATIONS_READ_MASK = "name,title,storefrontAddress"


class GbpClient:
    """GBP API surface built on a retrying HTTP client.

    Parameters
    ----------
    http:
        Anything with ``request(method, url, params=..., headers=..., data=...)``
        returning the parsed JSON body (i.e. an ``HttpClient``).
    quota:
        Optional :class:`QuotaLimiter`; when set, every call acquires a slot
        *before* the request is issued.
    base:
        API base URL (override in tests / for a regional host).
    """

    def __init__(self, http: Any, quota: Any = None, base: str = DEFAULT_BASE_URL) -> None:
        self.http = http
        self.quota = quota
        self.base = (base or DEFAULT_BASE_URL).rstrip("/")

    # -- helpers ----------------------------------------------------------- #
    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
    ) -> Any:
        if self.quota is not None:
            self.quota.acquire()
        return self.http.request(method, url, params=params, data=data)

    @staticmethod
    def _account_name(account: Any) -> str:
        """Accept ``"accounts/1001"``, a bare ``"1001"`` or an account dict."""
        if isinstance(account, dict):
            account = account.get("name")
        text = str(account)
        if not text:
            raise ValueError("account name is required")
        return text if text.startswith("accounts/") else f"accounts/{text}"

    # -- accounts / locations ---------------------------------------------- #
    def list_accounts(self) -> Dict[str, Any]:
        """``GET {base}/v1/accounts`` -- return the raw payload."""
        return self._request("GET", f"{self.base}/v1/accounts")

    def list_locations(self, account: Any) -> List[Dict[str, Any]]:
        """Walk every page of ``GET {base}/v1/{account}/locations``.

        ``readMask`` is sent on EVERY page request (first and every pageToken
        follow-up): the live ``locations.list`` endpoint rejects a request
        without one (Issue #121).
        """
        account_name = self._account_name(account)
        url = f"{self.base}/v1/{account_name}/locations"
        results: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "pageSize": LOCATIONS_PAGE_SIZE,
                "readMask": LOCATIONS_READ_MASK,
            }
            if page_token:
                params["pageToken"] = page_token
            page = self._request("GET", url, params=params) or {}
            results.extend(page.get("locations") or [])
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return results

    # -- reviews ------------------------------------------------------------ #
    def list_reviews(self, location: str, page_token: Optional[str] = None) -> Dict[str, Any]:
        """``GET {base}/v4/{location}/reviews?pageSize=50[&pageToken=]``.

        Returns the raw page (``{"reviews": [...], "nextPageToken": ...}``); the
        caller drives pagination.
        """
        url = f"{self.base}/v4/{location}/reviews"
        params: Dict[str, Any] = {"pageSize": REVIEWS_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", url, params=params)

    def get_review(self, review_name: str) -> Dict[str, Any]:
        """``GET {base}/v4/{review_name}``."""
        return self._request("GET", f"{self.base}/v4/{review_name}")

    def update_reply(self, review_name: str, comment: str) -> Dict[str, Any]:
        """``PUT {base}/v4/{review_name}/reply`` -- DRAFT ONLY, never live."""
        return self._request(
            "PUT",
            f"{self.base}/v4/{review_name}/reply",
            data={"comment": comment},
        )

    def batch_get_reviews(self, account: Any, location_names: Iterable[str]) -> Dict[str, Any]:
        """``POST {base}/v4/{account}/locations:batchGetReviews``."""
        account_name = self._account_name(account)
        return self._request(
            "POST",
            f"{self.base}/v4/{account_name}/locations:batchGetReviews",
            data={"locationNames": list(location_names)},
        )
