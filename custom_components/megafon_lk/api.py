"""Client for the (unofficial) MegaFon personal-account API.

AUTH: phone number + SMS code through the same JSON endpoints the web cabinet
uses (sessionCheck bootstrap -> otp/request -> otp/submit). The resulting
session cookies drive the same client as the old cookie-paste flow.

The three x-cabinet-* headers are OPTIONAL here: the API answers whatever it
returns during the OTP flow and any missing header is simply not sent.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.megafon.ru"
LK_URL = "https://lk.megafon.ru"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

ACCESS_TOKEN_COOKIE = "X-Cabinet-Access-Token"
ID_TOKEN_COOKIE = "X-Cabinet-Id-Token"
SESSION_CHECK_PATH = "/mlk/api/auth/sessionCheck"
OTP_REQUEST_PATH = "/mlk/api/auth/otp/request"
OTP_SUBMIT_PATH = "/mlk/api/auth/otp/submit"
# Refresh a bit before the upstream Max-Age=1200 actually expires.
TOKEN_REFRESH_MARGIN = 60
# Response headers that may carry the x-cabinet-* device params the data
# endpoints want.
_CABINET_HEADER_KEYS = (
    "x-cabinet-check-info",
    "x-cabinet-id-param",
    "x-cabinet-validation-param",
)


class MegafonError(Exception):
    """Generic API error."""


class MegafonAuthError(MegafonError):
    """Session/OTP is invalid or expired."""


class MegafonCaptchaError(MegafonError):
    """Server demands a captcha — use the cookie fallback."""


def _raise_for_error_code(body: dict[str, Any], path: str) -> None:
    """MegaFon often answers HTTP 200 with {"code": ..., "message": ...}."""
    code = body.get("code")
    if body.get("captchaNeeded") or code == "captcha":
        raise MegafonCaptchaError(f"{path}: server requires a captcha")
    if code and code not in ("0", 0):
        message = body.get("message") or f"code {code}"
        if str(code).startswith("a1"):
            raise MegafonAuthError(f"{path}: {message}")
        raise MegafonError(f"{path}: {message}")


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode the payload of a JWT; {} on failure."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _jwt_exp(token: str) -> float:
    """Expiry of a JWT in epoch seconds; 0 on decode failure."""
    try:
        return float(_jwt_claims(token).get("exp", 0))
    except (TypeError, ValueError):
        return 0.0


def _iter_jwts(obj: Any) -> Iterator[str]:
    """Yield every JWT-looking string nested anywhere in a JSON body."""
    if isinstance(obj, str):
        if obj.startswith("eyJ"):
            yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_jwts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_jwts(value)


def _iter_jwts_deep(obj: Any, seen: set[str] | None = None) -> Iterator[str]:
    """Same as _iter_jwts, but also descends into the claims of found JWTs —
    the access token hides inside the id token's `lk_claims.pssoToken`."""
    if seen is None:
        seen = set()
    for token in _iter_jwts(obj):
        if token in seen:
            continue
        seen.add(token)
        yield token
        yield from _iter_jwts_deep(_jwt_claims(token), seen)


@callback
def async_create_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Session on HA's shared connector that never stores/sends cookies itself."""
    return aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
        cookie_jar=aiohttp.DummyCookieJar(),
    )


def _jar_to_cookie_header(jar: aiohttp.CookieJar) -> str:
    parts = []
    for cookie in jar:
        parts.append(f"{cookie.key}={cookie.value}")
    return "; ".join(parts)


class MegafonOtpSession:
    """Drives the phone -> SMS -> code login and harvests the session.

    Keeps its own (real) cookie jar: JSESSIONID, csrf-token and friends are
    required by otp/request.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cabinet_headers: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self.cookies: dict[str, str] = {}
        # The x-cabinet-* device params are validated by the server — random
        # ones get {} responses. Real values come from the user's browser.
        self.cabinet_headers: dict[str, str] = dict(cabinet_headers or {})

    def _csrf(self) -> str | None:
        return self.cookies.get("csrf-token")

    def _headers(self, operation: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://lk.megafon.ru",
            "Referer": "https://lk.megafon.ru/",
            "User-Agent": USER_AGENT,
            "X-App-Type": "react_lk",
            "Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            **self.cabinet_headers,
        }
        if csrf := self._csrf():
            headers["X-CSRF-Token"] = csrf
        if operation:
            headers["X-Cabinet-Key-Operation-Id"] = operation
        return headers

    def _harvest(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            if morsel["max-age"] == "0" or not morsel.value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = morsel.value
        for key in _CABINET_HEADER_KEYS:
            if value := resp.headers.get(key):
                self.cabinet_headers[key] = value

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    async def async_bootstrap(self) -> None:
        """Anonymous session + CSRF cookie.

        Loads the cabinet page first: the anti-bot layer (ServiceWall's
        *sscw/cfidsw cookies) issues its tokens on lk.megafon.ru, and data
        endpoints answer {} without them. Cookies from redirects count too.
        """
        try:
            async with self._session.get(
                f"{LK_URL}/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                for r in (*resp.history, resp):
                    self._harvest(r)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Preloading %s failed: %s", LK_URL, err)
        _LOGGER.debug("Bootstrap cookies: %s", sorted(self.cookies))
        async with self._session.get(
            f"{BASE_URL}{SESSION_CHECK_PATH}",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://lk.megafon.ru",
                "Referer": "https://lk.megafon.ru/",
                "User-Agent": USER_AGENT,
                "X-App-Type": "react_lk",
                **self.cabinet_headers,
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            self._harvest(resp)
            resp.raise_for_status()

    async def _post_form(self, path: str, fields: dict[str, str], operation: str) -> dict[str, Any]:
        async with self._session.post(
            f"{BASE_URL}{path}",
            data=urlencode(fields),
            headers={
                **self._headers(operation),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=False,
        ) as resp:
            self._harvest(resp)
            body = await resp.json(content_type=None)
        if not isinstance(body, dict):
            raise MegafonError(f"Unexpected response for {path}")
        _raise_for_error_code(body, path)
        return body

    async def async_request_otp(self, phone: str) -> None:
        """Ask the server to send the SMS code."""
        await self._post_form(
            OTP_REQUEST_PATH,
            {"login": phone, "captcha": "", "captchaReady": "true"},
            "manual-auth",
        )

    async def async_submit_otp(self, phone: str, otp: str) -> None:
        """Submit the SMS code; on success the session cookies are complete."""
        await self._post_form(
            OTP_SUBMIT_PATH,
            {
                "login": phone,
                "otp": otp,
                "captcha": "",
                "incognitoFlag": "false",
            },
            "manual-auth",
        )


class MegafonClient:
    """Thin async wrapper around the endpoints used by the web cabinet.

    Identical to the old client, but the three x-cabinet-* headers are
    optional — whatever the OTP flow harvested is sent, the rest is omitted.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str,
        check_info: str = "",
        id_param: str = "",
        validation_param: str = "",
    ) -> None:
        self._session = session
        self._cookies = _parse_cookie_string(cookie)
        # The SPA generates these device params as random UUIDs and keeps them
        # stable; if the caller has none, make up our own.
        self._check_info = check_info or str(uuid.uuid4())
        self._id_param = id_param or str(uuid.uuid4())
        self._validation_param = validation_param or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"https://lk.megafon.ru/{self._id_param}")
        )
        self.msisdn: str | None = None
        self._access_token_exp = 0.0

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{n}={v}" for n, v in self._cookies.items())

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://lk.megafon.ru",
            "Referer": "https://lk.megafon.ru/",
            "User-Agent": USER_AGENT,
            "Cookie": self.cookie_header,
            "X-App-Type": "react_lk",
            "X-Cabinet-Check-Info": self._check_info,
            "X-Cabinet-Id-Param": self._id_param,
            "X-Cabinet-Validation-Param": self._validation_param,
        }
        if extra:
            headers.update(extra)
        return headers

    def _store_response_cookies(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            if morsel["max-age"] == "0" or not morsel.value:
                self._cookies.pop(name, None)
                continue
            self._cookies[name] = morsel.value
            if name == ACCESS_TOKEN_COOKIE:
                try:
                    max_age = int(morsel["max-age"])
                except (TypeError, ValueError):
                    max_age = 1200
                self._access_token_exp = time.monotonic() + max_age

    def _store_access_token(self, token: str, max_age: float | None = None) -> None:
        """Store the access token as the X-Cabinet-Access-Token cookie.

        The server sends the token in the X-AUTH-TOKEN response header or
        inside the sessionCheck body; the SPA writes it into the cookie itself.
        """
        self._cookies[ACCESS_TOKEN_COOKIE] = token
        if max_age is None:
            exp = _jwt_exp(token)
            max_age = exp - time.time() if exp else 1200
        self._access_token_exp = time.monotonic() + max_age

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        _refreshed: bool = False,
    ) -> Any:
        try:
            async with self._session.get(
                f"{BASE_URL}{path}",
                headers=self._headers(headers),
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
            ) as resp:
                self._store_response_cookies(resp)
                # The SPA turns this response header into the
                # X-Cabinet-Access-Token cookie client-side.
                if token := resp.headers.get("X-AUTH-TOKEN"):
                    self._store_access_token(token)
                if resp.status in (301, 302, 303, 307, 308, 401, 403):
                    if not _refreshed and path != SESSION_CHECK_PATH:
                        _LOGGER.debug(
                            "HTTP %s for %s, retrying after refresh",
                            resp.status,
                            path,
                        )
                        self._access_token_exp = 0.0
                        await self.async_ensure_session()
                        return await self._get(path, params, headers, _refreshed=True)
                    raise MegafonAuthError(f"HTTP {resp.status} for {path}")
                resp.raise_for_status()
                _LOGGER.debug(
                    "%s request_cookies=%s response_headers=%s",
                    path,
                    sorted(self._cookies),
                    dict(resp.headers),
                )
                if msisdn := resp.headers.get("x-nokia-msisdn") or resp.headers.get(
                    "x-cabinet-msisdn"
                ):
                    self.msisdn = msisdn
                body = await resp.json(content_type=None)
                _LOGGER.debug("%s -> %s", path, str(body)[:2000])
                if isinstance(body, dict):
                    try:
                        _raise_for_error_code(body, path)
                    except MegafonAuthError:
                        if not _refreshed and path != SESSION_CHECK_PATH:
                            _LOGGER.debug(
                                "Auth error in %s response body, retrying after refresh",
                                path,
                            )
                            self._access_token_exp = 0.0
                            await self.async_ensure_session()
                            return await self._get(
                                path, params, headers, _refreshed=True
                            )
                        raise
                return body
        except MegafonError:
            raise
        except aiohttp.ClientResponseError as err:
            raise MegafonError(f"HTTP {err.status} for {path}") from err
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise MegafonError(f"Request to {path} failed: {err}") from err

    async def _preload_antibot_cookies(self) -> None:
        """Fetch lk.megafon.ru once so the anti-bot layer issues its cookies —
        data endpoints answer {} without them."""
        if any(
            name.startswith(("gsscw", "cfidsw", "fgsscw")) for name in self._cookies
        ):
            return
        try:
            async with self._session.get(
                f"{LK_URL}/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": USER_AGENT,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                for r in (*resp.history, resp):
                    self._store_response_cookies(r)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Preloading %s failed: %s", LK_URL, err)

    async def async_ensure_session(self) -> None:
        if time.monotonic() < self._access_token_exp - TOKEN_REFRESH_MARGIN:
            return
        await self._preload_antibot_cookies()
        _LOGGER.debug("Access token expired, refreshing via sessionCheck")
        body = await self._get(
            SESSION_CHECK_PATH,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Cabinet-Key-Operation-Id": "rt-auth",
            },
        )
        if not isinstance(body, dict) or not body.get("authenticated"):
            raise MegafonAuthError("sessionCheck reports an unauthenticated session")
        # Tokens arrive inside the JSON body — the SPA then writes them into
        # the X-Cabinet-* cookies itself, so data requests stay cookie-based.
        found_access = False
        for token in _iter_jwts_deep(body):
            claims = _jwt_claims(token)
            if "auth_level" in claims or "appl_code" in claims:
                self._store_access_token(token)
                found_access = True
            elif claims.get("token_type") == "Bearer" or "id_token" in claims:
                self._cookies[ID_TOKEN_COOKIE] = token
        if not found_access and self._access_token_exp <= time.monotonic():
            _LOGGER.debug(
                "sessionCheck returned no access token — neither via "
                "X-AUTH-TOKEN header, Set-Cookie nor the body"
            )

    async def async_get_balance(self) -> dict[str, Any]:
        now = dt_util.now().strftime("%Y-%m-%dT%H:%M:%S")
        return await self._get("/mlk/balance/api/main", {"operationTime": now})

    async def async_get_remainders(self) -> dict[str, Any]:
        return await self._get("/mlk/api/options/v2/remainders/mini")

    async def async_get_tariff(self) -> dict[str, Any]:
        return await self._get(
            "/mlk/api/tariff/2019-3/current", {"includeOptions": "false"}
        )


def _parse_cookie_string(cookie: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            jar[name] = value
    return jar
