"""An exchange of one ISIN for another (a reverse split into a new instrument), proposed on *Unbekannt*.

Trade Republic reports it as two events (the old security out, the new one in; ``tr._propose_exchanges`` pairs
them). The user confirms; the cost basis of the removed shares comes from Wealthfolio's holding of the old
position, so the exchange books like the CSV import does - the basis moves to the new security, no gain or loss.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .wealthfolio import WealthfolioClient


class SwapError(Exception):
    """The proposal cannot be booked from what Wealthfolio holds; the message says why (shown to the user)."""


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def cost_basis(wf: WealthfolioClient, account_id: str, asset_id: str | None, isin: str,
               shares: Decimal) -> tuple[Decimal, str]:
    """The cost basis of ``shares`` of the old position, in the position's own currency: the holding's basis
    scaled to the shares removed. Raises ``SwapError`` when the position is not found or holds fewer shares."""
    for h in wf.holdings(account_id):
        inst = h.get("instrument") or {}
        if h.get("holdingType") == "cash" or h.get("isClosed"):
            continue
        if not ((asset_id and inst.get("id") == asset_id) or inst.get("symbol") == isin):
            continue
        held = _dec(h.get("quantity"))
        basis = _dec((h.get("costBasis") or {}).get("local"))
        if held < shares or held <= 0:
            raise SwapError("held")
        if basis <= 0:
            raise SwapError("basis")
        currency = h.get("localCurrency") or inst.get("currency") or "EUR"
        return (basis * shares / held).quantize(Decimal("0.01")), currency
    raise SwapError("position")
