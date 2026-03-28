"""API client for oekostrom AG customer portal."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import aiohttp

from .const import API_BASE, API_PROXY, API_ENV, PORTAL_NAME, USER_AGENT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Only alphanumeric endpoint names are valid — prevents injection via
# user-controlled data reaching the proxy's endpoint parameter.
_VALID_ENDPOINT = re.compile(r"^[A-Za-z]{1,50}$")


class OekostromApiError(Exception):
    """Base exception for API errors."""


class OekostromAuthError(OekostromApiError):
    """Authentication error."""


class OekostromApi:
    """Client for the oekostrom AG customer portal API.

    Uses its own aiohttp.ClientSession with a real cookie jar because
    Home Assistant's shared session uses DummyJar (no cookies).
    """

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password_hash = hashlib.md5(password.encode()).hexdigest()
        self._session: aiohttp.ClientSession | None = None
        self._php_session_id: str | None = None
        self._session_guid: str | None = None
        self._user_data: dict[str, Any] | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(),
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def user_data(self) -> dict[str, Any] | None:
        """Return cached user data from last authentication."""
        return self._user_data

    async def _init_php_session(self) -> None:
        """Get a PHP session from the portal landing page."""
        session = self._ensure_session()
        try:
            async with session.get(API_BASE + "/", allow_redirects=True) as resp:
                if resp.status != 200:
                    raise OekostromApiError(
                        f"Portal returned HTTP {resp.status}"
                    )
                phpsessid = None
                for cookie in session.cookie_jar:
                    if cookie.key == "PHPSESSID":
                        phpsessid = cookie.value
                        break
                if not phpsessid:
                    raise OekostromApiError("Failed to obtain PHP session")
                self._php_session_id = phpsessid
        except aiohttp.ClientError as err:
            raise OekostromApiError(f"Connection error: {err}") from err

    async def _call_endpoint(
        self,
        endpoint: str,
        data: dict[str, Any],
        use_login_token: bool = False,
    ) -> Any:
        """Call an API endpoint via the proxy."""
        if not _VALID_ENDPOINT.match(endpoint):
            raise OekostromApiError(f"Invalid endpoint name: {endpoint!r}")

        token = self._php_session_id if use_login_token else self._session_guid
        if not token:
            raise OekostromApiError(f"No token available for {endpoint}")

        params = {
            "api": API_ENV,
            "endpoint": endpoint,
            "token": token,
            "get_from_cache": "false",
            "save_to_cache": "false",
        }

        headers = {
            "Content-Type": "application/json",
            "Origin": API_BASE,
            "Referer": API_BASE + "/",
        }

        session = self._ensure_session()
        try:
            async with session.post(
                API_PROXY,
                params=params,
                json=data,
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    raise OekostromApiError(
                        f"HTTP {resp.status} from proxy for {endpoint}"
                    )

                text = await resp.text()

                if text.startswith("invalid proxy call"):
                    raise OekostromApiError(
                        f"Proxy rejected call to {endpoint}: {text}"
                    )

                try:
                    result = json.loads(text)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise OekostromApiError(
                        f"Invalid JSON from {endpoint}"
                    ) from exc

                if isinstance(result, dict):
                    status = result.get("Status")
                    if status == "SESSIONTIMEOUT":
                        raise OekostromAuthError("Session timed out")

                return result
        except aiohttp.ClientError as err:
            raise OekostromApiError(
                f"Connection error calling {endpoint}: {err}"
            ) from err

    async def authenticate(self) -> dict[str, Any]:
        """Authenticate with the portal and return user data."""
        await self._init_php_session()

        result = await self._call_endpoint(
            "UserLogin",
            {
                "Username": self._username,
                "Password": self._password_hash,
                "PortalName": PORTAL_NAME,
            },
            use_login_token=True,
        )

        if not isinstance(result, dict):
            raise OekostromAuthError("Login returned unexpected response")

        status = result.get("Status")
        if status not in ("OK", "EMAILOK"):
            raise OekostromAuthError(f"Login failed with status: {status}")

        self._session_guid = result["SessionGUID"]
        self._user_data = result

        # Set the oekp_l cookie so subsequent calls work
        session = self._ensure_session()
        session.cookie_jar.update_cookies(
            {"oekp_l": self._session_guid},
            aiohttp.URL(API_BASE),
        )

        _LOGGER.debug(
            "Authenticated as %s with %d account(s)",
            self._username,
            len(result.get("AccountIds", [])),
        )

        return result

    async def get_products(self, acc_id: int) -> list[dict[str, Any]]:
        """Get product/tariff information for an account."""
        result = await self._call_endpoint(
            "GetProducts",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        if isinstance(result, list):
            return result
        return []

    async def get_invoices(self, acc_id: int) -> list[dict[str, Any]]:
        """Get invoices for an account."""
        result = await self._call_endpoint(
            "GetInvoices",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        if isinstance(result, list):
            return result
        return []

    async def get_invoice_summary(self, acc_id: int) -> dict[str, Any]:
        """Get invoice summary for an account."""
        result = await self._call_endpoint(
            "GetInvoiceSummary",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}

    async def get_installments(self, acc_id: int) -> dict[str, Any]:
        """Get installment/payment plan info."""
        result = await self._call_endpoint(
            "GetInstallments",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}

    async def get_load_profile(
        self, acc_id: int, b_date: str, e_date: str
    ) -> Any:
        """Get load profile (energy consumption) data."""
        return await self._call_endpoint(
            "GetLoadProfile",
            {
                "SessionGUID": self._session_guid,
                "AccId": acc_id,
                "BDate": b_date,
                "EDate": e_date,
            },
        )

    async def get_load_profile_widget(self, acc_id: int) -> Any:
        """Get load profile widget data (summary)."""
        return await self._call_endpoint(
            "GetLoadProfileWidget",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )

    async def get_smart_meter(self, acc_id: int) -> dict[str, Any]:
        """Get smart meter status."""
        result = await self._call_endpoint(
            "GetSmartMeter",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}

    async def get_settlement_frequencies(self, acc_id: int) -> list[dict[str, Any]]:
        """Get settlement/billing periods."""
        result = await self._call_endpoint(
            "GetSettlementFrequencies",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        if isinstance(result, list):
            return result
        return []

    async def get_dashboard(self, acc_id: int) -> dict[str, Any]:
        """Get dashboard data."""
        result = await self._call_endpoint(
            "GetDashboard",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}

    async def get_bonus_points(self) -> dict[str, Any]:
        """Get bonus point data."""
        result = await self._call_endpoint(
            "GetBonusPointData",
            {"SessionGUID": self._session_guid},
        )
        return result if isinstance(result, dict) else {}

    async def get_device_data(self, acc_id: int) -> dict[str, Any]:
        """Get device/meter data."""
        result = await self._call_endpoint(
            "GetDeviceData",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}

    async def get_price_infos(self, acc_id: int) -> dict[str, Any]:
        """Get price information."""
        result = await self._call_endpoint(
            "GetPriceInfos",
            {"SessionGUID": self._session_guid, "AccId": acc_id},
        )
        return result if isinstance(result, dict) else {}
