"""Money from before what the sync reads: imports older than the bank account's history, opening positions.

Both become transactions the run books like any other (``sync.Syncer._before_history``, ``Syncer._openings``).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import audit as audit_mod
from . import coverage
from .mapping import Accounts
from .model import Kind, Transaction, when

# Days and start dates are German days (#108).
BERLIN = ZoneInfo("Europe/Berlin")


def opening_id(isin: str, day: str) -> str:
    return f"start-{isin}-{day}"


def money_before(activities: list[dict], accounts: Accounts, history: date,
                 synced: dict[str, tuple[str, list[str]]]) -> list[Transaction]:
    """Trades a CSV/PDF import booked before ``history`` (the earliest bank day read): their money never showed
    up on the cash account. A purchase gets a deposit of its cost just before its transfer to the depot, a sale a
    withdrawal of its proceeds just after (id ``pre-<activity id>`` - never change it); dividends keep their
    money. Not for transfers the sync matched to a bank booking (those were read), nor the sync's own."""
    matched = {i for _, ids in synced.values() for i in ids}
    trades = [a for a in activities if a.get("accountId") == accounts.portfolio
              and a.get("activityType") in ("BUY", "SELL")]
    first = datetime(history.year, history.month, history.day, tzinfo=BERLIN)
    out = []
    for a in activities:
        if a.get("accountId") != accounts.cash or not audit_mod.is_cash_transfer(a) or a.get("id") in matched \
                or coverage.SYNC_REF.search(a.get("comment") or ""):
            continue
        at = audit_mod._when(a)
        buy = a.get("activityType") == "TRANSFER_OUT"
        if at < first and _beside_trade(at, "BUY" if buy else "SELL", trades):
            out.append(_money_leg(a, at, buy))
    return out


def _beside_trade(at: datetime, kind: str, trades: list[dict]) -> bool:
    return any(t.get("activityType") == kind
               and abs((audit_mod._when(t) - at).total_seconds()) <= audit_mod.MOMENT_SECONDS for t in trades)


def _money_leg(a: dict, at: datetime, buy: bool) -> Transaction:
    return Transaction(
        id=f"pre-{a['id']}", kind=Kind.DEPOSIT if buy else Kind.WITHDRAWAL,
        datetime=at + timedelta(seconds=-3 if buy else 3), currency=a.get("currency") or "EUR",
        net=abs(audit_mod._dec(a.get("amount"))),
        label=("Deposit for a buy before the bank history read" if buy
               else "Withdrawal after a sale before the bank history read"))


# What the user can make of an unknown booking on *Unbekannt* (#132): a trade with the booking's money, a
# security delivered in or out without money, or a split.
COMPLETION_KINDS = ("buy", "sell", "in", "out", "split")
# Not in the form: a proposal for an exchange of ISINs (``tr.py`` pairs the two events, ``swap.py`` takes the cost
# basis from Wealthfolio) the user confirms with one click.
SWAP_KINDS = ("swap_out", "swap_in")


def completion(tx_id: str, c: dict) -> Transaction:
    """An unknown booking the user completed (``State.completions``), under its own id: a later adapter that
    learns the type produces the same id, so it is never booked twice. Amounts as typed: ``amount`` is the
    money that moved (a buy's cost, a sale's proceeds) or, for a delivery in, its cost basis."""
    day = when(c["day"]).astimezone(BERLIN).date()  # the booking's German day (#108)
    at = datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC)
    shares, amount = Decimal(c.get("shares") or 0), Decimal(c.get("amount") or 0)
    fee, tax = Decimal(c.get("fee") or 0), Decimal(c.get("tax") or 0)
    common = {"id": tx_id, "datetime": at, "currency": c.get("currency") or "EUR", "isin": c["isin"],
              "name": c.get("name") or c["isin"], "label": c.get("label") or ""}
    kind = c["kind"]
    if kind == "split":
        return Transaction(kind=Kind.SPLIT, net=Decimal(0), ratio=Decimal(c["ratio"]), **common)
    if kind in SWAP_KINDS:  # the cost basis travels with the shares: amount = the old position's cost basis
        leg = Kind.SECURITY_OUT if kind == "swap_out" else Kind.SECURITY_IN
        return Transaction(kind=leg, net=amount, shares=shares, gross=amount, **common)
    if kind == "buy":
        return Transaction(kind=Kind.BUY, net=amount, shares=shares, gross=amount - fee - tax, fee=fee, tax=tax,
                           **common)
    if kind == "sell":
        return Transaction(kind=Kind.SELL, net=amount, shares=shares, gross=amount + fee + tax, fee=fee, tax=tax,
                           **common)
    if kind == "in":  # delivered in without money from this account: like an opening position
        return Transaction(kind=Kind.BUY, net=amount, shares=shares, gross=amount, external_cash=True, **common)
    # "out": delivered out without money, booked as a sale at 0 (as a write-off)
    return Transaction(kind=Kind.SELL, net=Decimal(0), shares=shares, gross=Decimal(0), **common)


def openings(rows: list[dict]) -> list[Transaction]:
    """Opening positions the user entered (page Prüfung): a buy at noon of that day, its cost deposited first
    (``external_cash``), id ``start-<isin>-<day>``."""
    out = []
    for o in rows:
        shares, price, fee = Decimal(o["shares"]), Decimal(o["price"]), Decimal(o["fee"])
        day = date.fromisoformat(o["day"])
        # Holdings read before 0.9.9 (python-fints' parser) could carry "<ISIN>|/DE/<WKN>" as ISIN: the id
        # keeps it (it is booked under it), the ISIN is the real one, so the holdings check finds it.
        isin = o["isin"].split("|")[0].strip()
        out.append(Transaction(
            id=opening_id(o["isin"], o["day"]), kind=Kind.BUY,
            datetime=datetime(day.year, day.month, day.day, 12, tzinfo=BERLIN).astimezone(UTC),
            currency=o["currency"], net=shares * price + fee, isin=isin, name=o["name"], shares=shares,
            gross=shares * price, fee=fee, label="Anfangsbestand", external_cash=True))
    return out
