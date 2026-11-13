"""Client for the REST API of a self-hosted Wealthfolio (``/api/v1``).

Authentication: ``POST /api/v1/auth/login {"password": ...}`` answers with a
``wf_session`` cookie holding a JWT. The server also accepts that JWT as
``Authorization: Bearer``, which works over plain HTTP too (the cookie may be
marked ``Secure``), so the client sends it as a header. A server started with
``WF_AUTH_REQUIRED=false`` needs no password.

Activities are created one at a time (``POST /api/v1/activities``), like the
addon does, so ``tax``, ``subtype`` and ``sourceGroupId`` arrive unchanged.
Wealthfolio rejects an exact duplicate (same idempotency fingerprint) with
"Duplicate activity detected"; ``create_activity`` reports that as
``DuplicateActivity`` instead of an error.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import ssl
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlsplit

import httpx

API = "/api/v1"


class WealthfolioError(Exception):
    pass


class AuthFailed(WealthfolioError):
    pass


class LogosUnsupported(WealthfolioError):
    """Wealthfolio before 3.8.0: no custom asset logos."""


class SpendingUnsupported(WealthfolioError):
    """Wealthfolio without the spending tracker."""


class StorageUnsupported(WealthfolioError):
    """Wealthfolio without the addon storage (before 3.11)."""


@dataclass(frozen=True)
class DuplicateActivity:
    message: str


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    currency: str
    account_type: str
    is_active: bool


class _Pinned(ssl.SSLContext):
    """TLS to a Wealthfolio with a certificate of its own (#95): trusted by its SHA-256 fingerprint, not by a CA.
    Checked in the handshake, before the first byte of a request - the password - is sent."""

    pin = ""

    def wrap_socket(self, sock, *args, **kwargs):  # type: ignore[override]
        tls = super().wrap_socket(sock, *args, **kwargs)
        der = tls.getpeercert(binary_form=True) or b""
        if not hmac.compare_digest(hashlib.sha256(der).hexdigest(), self.pin):
            tls.close()
            raise ssl.SSLCertVerificationError("Wealthfolio's certificate is not the one trusted (SHA-256 differs)")
        return tls


def normalize_fingerprint(value: str) -> str:
    """``AB:CD:…`` or ``abcd…`` → 64 lower-case hex digits; empty if it isn't a SHA-256."""
    hex_ = re.sub(r"[\s:]", "", value or "").lower()
    return hex_ if re.fullmatch(r"[0-9a-f]{64}", hex_) else ""


def pinned_context(fingerprint: str) -> ssl.SSLContext:
    ctx = _Pinned(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the fingerprint is the check
    ctx.pin = normalize_fingerprint(fingerprint)
    return ctx


def server_fingerprint(url: str) -> str:
    """The SHA-256 of the certificate the server at ``url`` shows now - to show the user, who confirms it."""
    parts = urlsplit(url)
    pem = ssl.get_server_certificate((parts.hostname or "", parts.port or 443), timeout=10)
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()


def is_certificate_error(error: Exception) -> bool:
    return "CERTIFICATE_VERIFY_FAILED" in str(error) or "certificate" in str(error).lower()


def connect(make, url: str, password: str | None, fingerprint: str = ""):
    """``make(url, password)`` - the factory of the app or the tests - with the pinned certificate, if any."""
    return make(url, password, fingerprint=fingerprint) if fingerprint else make(url, password)


class WealthfolioClient:
    def __init__(self, base_url: str, password: str | None, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None, fingerprint: str = ""):
        self.base_url = base_url.rstrip("/")
        self.password = password or None
        self._token: str | None = None
        verify: ssl.SSLContext | bool = pinned_context(fingerprint) if fingerprint else True
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  follow_redirects=False, verify=verify)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WealthfolioClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── auth ────────────────────────────────────────────────────────────────
    def login(self) -> None:
        if not self.password:
            return
        try:
            r = self._http.post(f"{API}/auth/login", json={"password": self.password})
        except httpx.HTTPError as e:
            raise WealthfolioError(f"Wealthfolio is not reachable at {self.base_url}: {e}") from e
        if r.status_code in (401, 403):
            raise AuthFailed("Wealthfolio rejected the password.")
        if r.status_code >= 400:
            raise WealthfolioError(f"Login failed: HTTP {r.status_code} {_detail(r)}")
        token = _session_token(r)
        if not token:
            raise WealthfolioError("Login succeeded but Wealthfolio sent no session token.")
        self._token = token

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        if self.password and not self._token:
            self.login()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            try:
                r = self._http.request(method, f"{API}{path}", headers=headers, **kw)
            except httpx.HTTPError as e:
                raise WealthfolioError(f"Wealthfolio is not reachable at {self.base_url}: {e}") from e
            if r.status_code == 401 and self.password and attempt == 0:
                self.login()  # session expired
                continue
            if r.status_code == 401:
                raise AuthFailed("Wealthfolio asks for a login - enter the Wealthfolio password in the setup.")
            return r
        raise AssertionError("unreachable")

    # ── API ─────────────────────────────────────────────────────────────────
    def list_accounts(self) -> list[Account]:
        r = self._request("GET", "/accounts")
        _raise(r)
        return [
            Account(a["id"], a.get("name", ""), a.get("currency", ""), a.get("accountType", ""),
                    a.get("isActive", True))
            for a in r.json()
        ]

    def create_activity(self, payload: dict) -> dict | DuplicateActivity:
        r = self._request("POST", "/activities", json=payload)
        if r.status_code >= 400:
            detail = _detail(r)
            # Wealthfolio's fingerprint check (409, "Duplicate activity detected"): any wording of it, not
            # only today's (SEC-09) - a real error never says "duplicate".
            if "duplicate" in detail.lower():
                return DuplicateActivity(detail)
            raise WealthfolioError(f"Wealthfolio rejected the activity ({payload.get('activityType')}): {detail}")
        return r.json()

    def update_activity(self, payload: dict) -> dict:
        """``PUT /activities``: ``id`` plus the fields; ``asset: {}`` removes the asset.

        Wealthfolio keeps the other leg of a linked transfer pair in step and
        recalculates the portfolio afterwards.
        """
        r = self._request("PUT", "/activities", json=payload)
        if r.status_code >= 400:
            raise WealthfolioError(f"Wealthfolio rejected the update ({payload.get('activityType')}): {_detail(r)}")
        return r.json()

    def delete_activity(self, activity_id: str) -> None:
        r = self._request("DELETE", f"/activities/{quote(activity_id, safe='')}")
        _raise(r)

    # ── taxonomies (look-through, waldonso2/wealthfolio-importer-addon#139) ──
    def taxonomy_categories(self, taxonomy_id: str) -> list[dict]:
        """The categories of a taxonomy (``id``, ``name``, ``parentId``), e.g. ``regions``, ``industries_gics``."""
        r = self._request("GET", f"/taxonomies/{quote(taxonomy_id, safe='')}")
        _raise(r)
        return (r.json() or {}).get("categories") or []

    def asset_assignments(self, asset_id: str) -> list[dict]:
        """``{id, taxonomyId, categoryId, weight, source}`` per category an asset is assigned to."""
        r = self._request("GET", f"/taxonomies/assignments/asset/{quote(asset_id, safe='')}")
        _raise(r)
        return r.json()

    def replace_assignments(self, asset_id: str, taxonomy_id: str, assignments: list[dict]) -> None:
        """Every assignment of the asset in this taxonomy replaced by ``assignments`` (weights in basis points)."""
        r = self._request("PUT", f"/taxonomies/assignments/asset/{quote(asset_id, safe='')}/taxonomy/"
                                 f"{quote(taxonomy_id, safe='')}", json=assignments)
        _raise(r)

    def delete_assignment(self, assignment_id: str) -> None:
        r = self._request("DELETE", f"/taxonomies/assignments/{quote(assignment_id, safe='')}")
        _raise(r)

    def holdings(self, account_id: str) -> list[dict]:
        """Current holdings of an account, cash included (``holdingType`` "cash"/"security")."""
        r = self._request("GET", "/holdings", params={"accountId": account_id})
        _raise(r)
        return r.json()

    def list_assets(self) -> list[dict]:
        """Every asset (``id``, ``displayCode``, ``instrumentSymbol``, ``metadata.identifiers.isin``, ...)."""
        r = self._request("GET", "/assets")
        _raise(r)
        return r.json()

    def update_asset_profile(self, asset: dict, metadata: dict) -> None:
        """``PUT /assets/profile/{id}`` with new ``metadata`` and every other field as read: Wealthfolio sets
        name, display code, notes and provider config from the payload, so an omitted one would be cleared."""
        fields = ("name", "displayCode", "kind", "quoteMode", "quoteCcy", "instrumentType", "instrumentSymbol",
                  "instrumentExchangeMic", "providerConfig")
        payload = {k: asset.get(k) for k in fields} | {"notes": asset.get("notes") or "", "metadata": metadata}
        r = self._request("PUT", f"/assets/profile/{quote(asset['id'], safe='')}", json=payload)
        if r.status_code >= 400:
            raise WealthfolioError(f"Wealthfolio rejected the asset profile: {_detail(r)}")

    def asset_logos(self) -> set[str]:
        """Ids of the assets that have a custom logo (Wealthfolio 3.8.0 and later)."""
        r = self._request("GET", "/assets/logos")
        if r.status_code in (404, 405):
            raise LogosUnsupported("This Wealthfolio has no custom asset logos (version 3.8.0 or later needed).")
        _raise(r)
        try:
            return {s["assetId"] for s in r.json()}
        except (ValueError, TypeError, KeyError) as e:  # an older server's page instead of the API
            raise LogosUnsupported("This Wealthfolio answers no logo list (version 3.8.0 or later needed).") from e

    def set_asset_logo(self, asset_id: str, png: bytes) -> None:
        """``PUT /assets/logo/{id}``: a PNG of at most 256x256 pixels and 150 KB."""
        r = self._request("PUT", f"/assets/logo/{quote(asset_id, safe='')}",
                          json={"dataBase64": base64.b64encode(png).decode()})
        if r.status_code >= 400:
            raise WealthfolioError(f"Wealthfolio rejected the logo: {_detail(r)}")

    def spending_settings(self) -> dict:
        """``GET /spending/settings``: ``{enabled, accountIds, excludedCategoryIds}``; 404: no tracker."""
        r = self._request("GET", "/spending/settings")
        if r.status_code in (404, 405):
            raise SpendingUnsupported("This Wealthfolio has no spending tracker.")
        _raise(r)
        return r.json()

    def settings(self) -> dict | None:
        """``GET /settings`` (read only): the user's app settings (``font``, ``themeId``, ...); None if not offered."""
        r = self._request("GET", "/settings")
        if r.status_code in (404, 405):
            return None
        _raise(r)
        body = r.json()
        return body if isinstance(body, dict) else None

    def set_spending_accounts(self, account_ids: list[str], enable: bool = True) -> None:
        """Tracks exactly these accounts; ``enable`` also turns the tracker on (else its switch is left as the user
        has it). Wealthfolio categorizes new ones itself."""
        body: dict = {"accountIds": account_ids}
        if enable:
            body["enabled"] = True
        r = self._request("PUT", "/spending/settings", json=body)
        _raise(r)

    def _storage(self, method: str, addon_id: str, key: str, **kw) -> httpx.Response:
        r = self._request(method, f"/addons/storage/{quote(addon_id, safe='')}/{quote(key, safe='')}", **kw)
        if r.status_code in (404, 405):
            raise StorageUnsupported("This Wealthfolio has no addon storage (from 3.11.0).")
        _raise(r)
        return r

    def storage_get(self, addon_id: str, key: str) -> str | None:
        """A value of an addon's storage (#146), None if not set."""
        r = self._storage("GET", addon_id, key)
        value = r.json() if r.content.strip() else None
        return value if isinstance(value, str) else None

    def storage_set(self, addon_id: str, key: str, value: str) -> None:
        self._storage("PUT", addon_id, key, json={"value": value})

    def storage_delete(self, addon_id: str, key: str) -> None:
        self._storage("DELETE", addon_id, key)

    def search_activities(self, account_ids: list[str], date_from: date, date_to: date,
                          page_size: int = 500) -> list[dict]:
        """All activities of the accounts between the two dates (inclusive)."""
        out: list[dict] = []
        page = 0
        while True:
            r = self._request("POST", "/activities/search", json={
                "page": page,
                "pageSize": page_size,
                "accountIdFilter": account_ids,
                "dateFrom": date_from.isoformat(),
                "dateTo": date_to.isoformat(),
                "sort": {"id": "date", "desc": False},
            })
            _raise(r)
            body = r.json()
            data = body.get("data", [])
            out.extend(data)
            total = body.get("meta", {}).get("totalRowCount")
            # Without a total: on while pages come full, so no older activity is missed silently.
            if not data or (len(out) >= total if total is not None else len(data) < page_size):
                return out
            page += 1


def _session_token(r: httpx.Response) -> str | None:
    for header in r.headers.get_list("set-cookie"):
        m = re.match(r"\s*wf_session=([^;]+)", header)
        if m:
            return m.group(1)
    return None


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if isinstance(body.get(key), str):
                return body[key]
    return str(body)[:500]


def _raise(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise WealthfolioError(f"HTTP {r.status_code}: {_detail(r)}")
