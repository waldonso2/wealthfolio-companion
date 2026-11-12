"""An exchange of one ISIN for another (a reverse split into a new instrument), proposed on *Unbekannt*.

Trade Republic reports it as two events (the old security out, the new one in; ``tr._propose_exchanges`` pairs
them). The user confirms; the cost basis of the removed shares comes from Wealthfolio's holding of the old
position, so the exchange books like the CSV import does - the basis moves to the new security, no gain or loss.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .logos import isin_of
from .wealthfolio import WealthfolioClient


class SwapError(Exception):
    """The proposal cannot be booked from what Wealthfolio holds; ``args[0]`` says why (shown to the user),
    ``detail`` is what was looked at (names and symbols of the account's positions, never amounts)."""

    def __init__(self, why: str, detail: str = "") -> None:
        super().__init__(why)
        self.detail = detail


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _same_name(a: str, b: str) -> bool:
    """Names equal ignoring case and spaces, or one contained in the other (Wealthfolio's name of an asset is the
    user's or Yahoo's, the broker's is Trade Republic's). Never for an empty or very short name."""
    a, b = "".join(a.lower().split()), "".join(b.lower().split())
    return len(a) >= 8 and len(b) >= 8 and (a in b or b in a)


def asset_ids(wf: WealthfolioClient, known: dict, isin: str) -> set[str]:
    """Wealthfolio assets that carry the ISIN: the one the sync learned, and any whose own ISIN (metadata or
    symbol) matches - an import books a security under the user's mapped ticker, so the symbol is no ISIN."""
    ids = {known["asset_id"]} if known.get("asset_id") else set()
    ids |= {a["id"] for a in wf.list_assets() if a.get("id") and isin_of(a) == isin}
    return ids


def cost_basis(wf: WealthfolioClient, account_id: str, ids: set[str], isin: str, name: str,
               shares: Decimal, symbol: str = "") -> tuple[Decimal, str]:
    """The cost basis of ``shares`` of the old position, in the position's own currency: the holding's basis
    scaled to the shares removed. Found by asset id or symbol; failing both, by the broker's name when exactly one
    holding has it. Raises ``SwapError`` when the position is not found or holds fewer shares."""
    rows = [h for h in wf.holdings(account_id) if (h.get("holdingType") or "").lower() != "cash"]
    live = [h for h in rows if not h.get("isClosed")]
    found = [h for h in live if (h.get("instrument") or {}).get("id") in ids
             or (h.get("instrument") or {}).get("symbol") in (isin, symbol or isin)]
    if not found and symbol:  # the user named the symbol: case does not matter
        found = [h for h in live if ((h.get("instrument") or {}).get("symbol") or "").lower() == symbol.lower()]
    if not found:
        found = [h for h in live if _same_name(name, (h.get("instrument") or {}).get("name") or "")]
    if len(found) != 1:
        def label(h: dict) -> str:
            inst = h.get("instrument") or {}
            return f"{inst.get('name') or '?'} ({inst.get('symbol')}{', closed' if h.get('isClosed') else ''})"

        seen = "; ".join(label(h) for h in rows[:80])
        raise SwapError("position", f"{len(found)} matches for {isin} / {name}. Account holds: {seen or 'nothing'}")
    h = found[0]
    held, basis = _dec(h.get("quantity")), _dec((h.get("costBasis") or {}).get("local"))
    if held < shares or held <= 0:
        raise SwapError("held")
    if basis <= 0:
        raise SwapError("basis")
    currency = h.get("localCurrency") or (h.get("instrument") or {}).get("currency") or "EUR"
    return (basis * shares / held).quantize(Decimal("0.01")), currency
