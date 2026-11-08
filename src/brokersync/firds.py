"""A rough asset class from ESMA's FIRDS register (#185, the rest of waldonso2/wealthfolio-importer-addon#141).

FIRDS lists every instrument traded in the EU with its CFI code (ISO 10962). Its first letters say what it is:
``E`` shares, ``D`` debt, ``C`` a fund (``CE`` an ETF) whose fifth letter names the principal investment type. That
is the whole inference - a fund of mixed or other assets, derivatives, structured products and anything the register
does not know stay without a class. Only the ISIN goes out, only when the user switched it on
(``Config.asset_class_lookup``), only to ``registers.esma.europa.eu`` over https, no redirects.
"""

from __future__ import annotations

import re

import httpx

from .assets import valid_isin

URL = "https://registers.esma.europa.eu/solr/esma_registers_firds/select"
MAX_BYTES = 256 * 1024
RETRY_DAYS = 30  # an ISIN the register does not know: asked again after this
PAUSE = 0.5  # seconds between two requests of a pass (replaced in tests)
ROWS = 20  # an instrument has one entry per trading venue; the CFI is the same on all but a few

# CFI position 5 of a fund (principal investment type) -> Wealthfolio's asset class.
FUND_TYPES = {"E": "EQUITY", "B": "FIXED_INCOME", "C": "COMMODITIES", "R": "REAL_ESTATE"}
# Shares (S), preferred (P), convertible (C), preference convertible (F), depository receipts (D), limited
# partnerships (L). Not structured instruments (Y: Xetra-Gold & Co) and not others (M).
EQUITY_GROUPS = "SPCFDL"
# Debt: all but money market instruments (Y), which are cash-like, not a bond.
DEBT_GROUPS_NOT = "Y"


class Unavailable(Exception):
    """The register did not answer properly (5xx, timeout, odd body): nothing is recorded, try later."""


def cfi_class(cfi: str) -> str | None:
    """Wealthfolio's asset class a CFI code names, or None (mixed, other, derivative, structured, unknown)."""
    cfi = cfi.strip().upper()
    if len(cfi) < 6 or not cfi.isalpha():
        return None
    category, group = cfi[0], cfi[1]
    if category == "E":
        return "EQUITY" if group in EQUITY_GROUPS else None
    if category == "D":
        return None if group in DEBT_GROUPS_NOT else "FIXED_INCOME"
    if category == "C":
        return FUND_TYPES.get(cfi[4])
    return None


def cfi_codes(isin: str, *, transport: httpx.BaseTransport | None = None) -> list[str]:
    """The distinct CFI codes FIRDS lists for ``isin`` (empty: not in the register). Raises ``Unavailable``."""
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) or not valid_isin(isin):
        return []
    params = {"q": f"isin:{isin}", "rows": str(ROWS), "fl": "gnr_cfi_code", "wt": "json"}
    try:
        with httpx.Client(timeout=20.0, transport=transport, follow_redirects=False) as http, \
                http.stream("GET", URL, params=params) as r:
            if r.status_code != 200:
                raise Unavailable(f"HTTP {r.status_code}")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise Unavailable("answer too large")
        docs = httpx.Response(200, content=body).json()["response"]["docs"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        raise Unavailable(type(e).__name__) from e
    return sorted({d["gnr_cfi_code"].upper() for d in docs
                   if isinstance(d, dict) and re.fullmatch(r"[A-Za-z]{6}", str(d.get("gnr_cfi_code") or ""))})


def asset_class(codes: list[str]) -> str | None:
    """One class when every code names the same one; any disagreement or unnamed code means None."""
    classes = {cfi_class(c) for c in codes}
    return classes.pop() if len(classes) == 1 else None
