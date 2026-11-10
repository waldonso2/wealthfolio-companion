"""Transactions → Wealthfolio activity payloads.

Same rules as the Broker Importer addon (``src/pdf/activities.ts``,
``src/common.ts`` in waldonso2/wealthfolio-importer-addon), so the sync and the
CSV/PDF imports book the same way:

- Two accounts per broker: trades and dividends on the securities account,
  everything else on the cash account. Every cash movement on the securities
  account is funded from / swept to the cash account as a
  TRANSFER_OUT/TRANSFER_IN pair sharing one ``sourceGroupId`` - without it
  Wealthfolio counts the pair as spending. The securities account ends up
  holding no cash.
- BUY/SELL carry ``amount = tradeFinalCash`` (exact ``quantity × unitPrice ±
  (fee + tax)``): Wealthfolio's duplicate fingerprint hashes the exact amount.
- Fee and tax go into the ``fee``/``tax`` fields; a dividend or interest
  payment is one activity with its net amount. A tax refund can't go into the
  tax field, so it is its own CREDIT/TAX_REFUND.
- Comments end in ``[SYNC <broker>:<id>]`` and are part of the fingerprint:
  never reword them, or every synced activity reappears as new.
- Cash activities (deposits, transfers, fees, ...) carry no asset, like the
  addon's imports (Wealthfolio's import drops its ``$CASH-<ccy>`` placeholder).
  Created with an asset, a TRANSFER_IN/TRANSFER_OUT is a *securities* transfer
  of that asset and moves no money; ``brokersync.repair`` fixes the ones
  versions before 0.3.6 created that way.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .model import SAVINGS, Kind, Transaction

# "$CASH-EUR", "CASH:USD", ... as Wealthfolio's is_cash_symbol, plus the bare
# "$CASH" its holdings show for such an asset ("CASH" alone is a ticker).
CASH_SYMBOL = re.compile(r"^(\$CASH|\$?CASH[-_:][A-Z]{3})$")
# Booked net must match gross ± fee ± tax within this.
TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class Accounts:
    cash: str
    portfolio: str
    savings: str = ""  # optional: the bank's savings account (Tagesgeld), if the user assigned one

    @property
    def ids(self) -> list[str]:
        """The broker's Wealthfolio accounts: cash, portfolio and, if assigned, savings."""
        return [a for a in (self.cash, self.portfolio, self.savings) if a]


@dataclass(frozen=True)
class SecurityMapping:
    symbol: str
    exchange_mic: str | None = None
    name: str | None = None
    asset_id: str | None = None  # an existing Wealthfolio asset (``brokersync.assets``)


@dataclass(frozen=True)
class TransferPattern:
    label: str
    iban: str = ""
    keyword: str = ""
    destination_account_id: str = ""

    @classmethod
    def from_config(cls, d: dict) -> TransferPattern:
        return cls(d.get("label", ""), (d.get("iban") or "").replace(" ", "").upper(), d.get("keyword") or "",
                   d.get("destinationAccountId") or "")


def match_pattern(tx: Transaction, patterns: list[TransferPattern]) -> TransferPattern | None:
    """Same order as the addon's matchPattern: IBAN exact, IBAN in the text, keyword in the text."""
    text = f"{tx.counterparty} {tx.text}".upper()
    iban = tx.counterparty_iban.replace(" ", "").upper()
    for test in (lambda p: p.iban and p.iban == iban,
                 lambda p: p.iban and p.iban in text.replace(" ", ""),
                 lambda p: p.keyword and p.keyword.upper() in text):
        for p in patterns:
            if test(p):
                return p
    return None


def is_cash_symbol(symbol: str | None) -> bool:
    """A cash placeholder instead of a security: ``$CASH-EUR``, ``CASH:USD``, ``$CASH``."""
    return bool(symbol) and bool(CASH_SYMBOL.match((symbol or "").strip().upper()))


class MappingError(Exception):
    """The transaction can't be booked as reported (amounts don't add up, data missing)."""


# Wealthfolio's activity ``metadata`` is a JSON string; ``flow.is_external`` keeps an unpaired securities transfer
# from being reported as an incomplete transfer.
EXTERNAL_FLOW = json.dumps({"flow": {"is_external": True}}, separators=(",", ":"))


def fmt(d: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros ("3016", "25.125")."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def round6(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def trade_final_cash(kind: Kind, quantity: str, unit_price: str, fee: str, tax: str = "0") -> str:
    """Exact cash flow of a trade, as the addon's ``tradeFinalCash``."""
    gross = abs(Decimal(quantity)) * abs(Decimal(unit_price))
    charges = abs(Decimal(fee or "0")) + abs(Decimal(tax or "0"))
    return fmt(gross + charges if kind == Kind.BUY else gross - charges)


def iso(dt: datetime, seconds: int = 0) -> str:
    """UTC ISO timestamp with milliseconds, like JavaScript's toISOString()."""
    d = (dt + timedelta(seconds=seconds)).astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def group_id(broker: str, tx: Transaction) -> str:
    return f"sync-{broker}-{tx.id}"


def tx_ref(broker: str, tx: Transaction) -> str:
    return f" [SYNC {broker}:{tx.id}]"


def _check_amounts(tx: Transaction) -> None:
    if tx.net < 0:
        raise MappingError("Booked amount is negative.")
    if tx.kind in (Kind.BUY, Kind.SELL):
        if not tx.isin or not tx.shares or tx.shares <= 0 or tx.gross is None:
            raise MappingError("ISIN, share count or market value missing.")
        expected = tx.gross + tx.fee + tx.tax if tx.kind == Kind.BUY else tx.gross - tx.fee - tx.tax
        if abs(expected - tx.net) > TOLERANCE:
            raise MappingError(
                f"Market value, fee and tax ({fmt(expected)}) don't add up to the booked amount ({fmt(tx.net)})."
            )
    if tx.kind == Kind.SPLIT and (not tx.isin or not tx.ratio or tx.ratio <= 0):
        raise MappingError("ISIN or split ratio missing.")
    if tx.kind in (Kind.SECURITY_OUT, Kind.SECURITY_IN) and (
            not tx.isin or not tx.shares or tx.shares <= 0 or tx.net <= 0):
        raise MappingError("ISIN, share count or cost basis of the exchange missing.")
    if tx.kind == Kind.DIVIDEND and (not tx.isin or (tx.shares is not None and tx.shares <= 0)):
        raise MappingError("ISIN or share count missing.")
    if (tx.kind in (Kind.DIVIDEND, Kind.INTEREST) and tx.gross is not None
            and abs(tx.gross - tx.tax - tx.net) > TOLERANCE):
        raise MappingError(
            f"Gross income minus tax ({fmt(tx.gross - tx.tax)}) doesn't match the booked amount ({fmt(tx.net)})."
        )


# Cash-only kinds booked as one activity: Wealthfolio type, subtype, default label, whether the text is added.
# The comments are part of Wealthfolio's duplicate fingerprint - never reword them.
_CASH_KINDS = {
    Kind.DEPOSIT: ("DEPOSIT", None, "Deposit", True),
    Kind.FEE: ("FEE", None, "Fee", True),
    Kind.TAX: ("TAX", None, "Tax", False),
    Kind.BONUS: ("CREDIT", "BONUS", "Bonus", True),
    Kind.TAX_REFUND: ("CREDIT", "TAX_REFUND", "Tax refund", False),
}


def to_activities(
    tx: Transaction,
    broker: str,
    accounts: Accounts,
    mappings: dict[str, SecurityMapping] | None = None,
    patterns: list[TransferPattern] | None = None,
) -> list[dict]:
    """Wealthfolio ``NewActivity`` payloads for one transaction, in booking order."""
    if tx.kind == Kind.UNKNOWN:
        raise MappingError(f"Unknown event type {tx.raw_type or tx.label!r}.")
    if tx.kind == Kind.SECURITIES_CASH:
        raise MappingError("The cash side of a securities transaction is booked by the securities side.")
    _check_amounts(tx)
    return _Booking(tx, broker, accounts, mappings or {}, patterns or []).activities()


class _Booking:
    """The activities of one transaction (``to_activities``), one method per kind of booking."""

    def __init__(self, tx: Transaction, broker: str, accounts: Accounts, mappings: dict[str, SecurityMapping],
                 patterns: list[TransferPattern]):
        self.tx, self.broker, self.accounts, self.mappings, self.patterns = tx, broker, accounts, mappings, patterns
        self.ref = tx_ref(broker, tx)
        self.gid = group_id(broker, tx)
        self.tax_field = fmt(tx.tax) if tx.tax > 0 else None
        self.refund = -tx.tax if tx.tax < 0 else Decimal(0)
        self.out: list[dict] = []

    def cash(self, account: str, activity_type: str, seconds: int, amount: Decimal, comment: str, *,
             subtype: str | None = None, source_group: str | None = None, tax: Decimal | None = None) -> dict:
        return _clean({
            "accountId": account,
            "activityType": activity_type,
            "subtype": subtype,
            "activityDate": iso(self.tx.datetime, seconds),
            "currency": self.tx.currency,
            "quantity": "1",
            "unitPrice": "1",
            "amount": fmt(amount),
            "tax": fmt(tax) if tax else None,
            "comment": comment,
            "sourceGroupId": source_group,
        })

    def activities(self) -> list[dict]:
        tx, accounts = self.tx, self.accounts
        # Cash-only kinds live on the cash account - or on the savings account they were booked on.
        if tx.account == SAVINGS and not accounts.savings:
            raise MappingError("A booking of the savings account, but no Wealthfolio account is assigned to it.")
        self.own = accounts.savings if tx.account == SAVINGS else accounts.cash
        if tx.own_transfer and tx.kind in (Kind.DEPOSIT, Kind.WITHDRAWAL):
            return self.own_transfer()
        if tx.kind in _CASH_KINDS:
            activity_type, subtype, default, with_text = _CASH_KINDS[tx.kind]
            text = _text(tx) if with_text else ""
            return [self.cash(self.own, activity_type, 0, tx.net, f"{tx.label or default}{_name(tx)}{text}{self.ref}",
                              subtype=subtype)]
        if tx.kind == Kind.WITHDRAWAL:
            return self.withdrawal()
        if tx.kind == Kind.INTEREST:
            return self.interest()
        if tx.kind == Kind.DIVIDEND:
            return self.dividend()
        if tx.kind == Kind.SPLIT:
            return [self.split()]
        if tx.kind in (Kind.SECURITY_OUT, Kind.SECURITY_IN):
            return [self.security_leg()]
        return self.trade()

    def security_leg(self) -> dict:
        """One side of an exchange of ISINs: an unpaired TRANSFER_OUT / TRANSFER_IN of the securities account with the
        cost basis as unit price, marked external so Wealthfolio doesn't report it as an incomplete transfer. The new
        position arrives a second after the old one leaves."""
        tx = self.tx
        leaving = tx.kind == Kind.SECURITY_OUT
        return _clean({
            "accountId": self.accounts.portfolio,
            "activityType": "TRANSFER_OUT" if leaving else "TRANSFER_IN",
            "activityDate": iso(tx.datetime, 0 if leaving else 1),
            "currency": tx.currency,
            "quantity": fmt(tx.shares or Decimal(0)),
            "unitPrice": fmt(round6(tx.net / (tx.shares or Decimal(1)))),
            "comment": f"{self.label} {tx.name}{self.ref}",
            "asset": self.asset(),
            "metadata": EXTERNAL_FLOW,
        })

    def split(self) -> dict:
        tx, ratio = self.tx, self.tx.ratio or Decimal(0)  # checked > 0 in _check_amounts
        return _clean({
            "accountId": self.accounts.portfolio,
            "activityType": "SPLIT",
            "activityDate": iso(tx.datetime),
            "currency": tx.currency,
            "quantity": "0",
            "unitPrice": "0",
            "amount": fmt(ratio),
            "comment": f"Split {fmt(ratio)} {tx.name}{self.ref}",
            "asset": self.asset(),
        })

    def own_transfer(self) -> list[dict]:
        tx, accounts, ref, gid = self.tx, self.accounts, self.ref, self.gid
        if not accounts.savings:
            raise MappingError("A transfer to the savings account, but no Wealthfolio account is assigned to it.")
        what = tx.text or tx.label or "Transfer"
        source, target = ((accounts.cash, accounts.savings) if tx.kind == Kind.WITHDRAWAL
                          else (accounts.savings, accounts.cash))
        names = {accounts.cash: "Cash", accounts.savings: "Savings"}
        return [self.cash(source, "TRANSFER_OUT", 0, tx.net, f"-> {names[target]}: {what}{ref}", source_group=gid),
                self.cash(target, "TRANSFER_IN", 0, tx.net, f"<- {names[source]}: {what}{ref}", source_group=gid)]

    def withdrawal(self) -> list[dict]:
        tx, ref, gid = self.tx, self.ref, self.gid
        # Only outbound money checks the transfer patterns (addon rule): an inbound transfer is always a deposit.
        p = match_pattern(tx, self.patterns)
        if not p:
            return [self.cash(self.own, "WITHDRAWAL", 0, tx.net,
                              f"{tx.label or 'Withdrawal'}{_name(tx)}{_text(tx)}{ref}")]
        what = tx.text or tx.label or "Transfer"
        out = [self.cash(self.own, "TRANSFER_OUT", 0, tx.net, f"-> {p.label}: {what}{_name(tx, ' (')}{ref}",
                         source_group=gid if p.destination_account_id else None)]
        if p.destination_account_id:
            out.append(self.cash(p.destination_account_id, "TRANSFER_IN", 0, tx.net,
                                 f"<- {self.broker.upper()}: {what}{ref}", source_group=gid))
        return out

    def interest(self) -> list[dict]:
        tx, ref, refund = self.tx, self.ref, self.refund
        out = [self.cash(self.own, "INTEREST", 0, tx.net - refund,
                         f"{tx.label or 'Interest'}{_name(tx)}{_text(tx)}{ref}", tax=tx.tax if tx.tax > 0 else None)]
        if refund:
            out.append(self.cash(self.own, "CREDIT", 0, refund, f"Tax refund on interest{ref}", subtype="TAX_REFUND"))
        return out

    def asset(self) -> dict:
        tx = self.tx
        m = self.mappings.get(tx.isin or "")
        return _clean({
            "id": m.asset_id if m else None,
            "symbol": m.symbol if m else tx.isin,
            "exchangeMic": m.exchange_mic if m else None,
            "name": (m.name if m and m.name else None) or tx.name or None,
            "quoteCcy": tx.currency,
        })

    @property
    def label(self) -> str:
        return self.tx.label or self.tx.kind.value.title()

    def refund_credit(self, what: str) -> dict:
        return self.cash(self.accounts.portfolio, "CREDIT", 0, self.refund,
                         f"Tax refund on {what} of {self.tx.name}{self.ref}", subtype="TAX_REFUND")

    def sweep_to_cash(self, text: str) -> None:
        tx, ref, gid = self.tx, self.ref, self.gid
        self.out.append(self.cash(self.accounts.portfolio, "TRANSFER_OUT", 1, tx.net, f"{text} -> Cash{ref}",
                                  source_group=gid))
        self.out.append(self.cash(self.accounts.cash, "TRANSFER_IN", 2, tx.net, f"{text} from Portfolio{ref}",
                                  source_group=gid))

    def dividend(self) -> list[dict]:
        tx = self.tx
        self.out.append(_clean({
            "accountId": self.accounts.portfolio,
            "activityType": "DIVIDEND",
            "activityDate": iso(tx.datetime),
            "currency": tx.currency,
            # Scalable reports no share count for a payout: quantity 1, like the addon.
            "quantity": fmt(tx.shares) if tx.shares else "1",
            "amount": fmt(tx.net - self.refund),
            "tax": self.tax_field,
            "comment": f"{self.label} {tx.name}{self.ref}",
            "asset": self.asset(),
        }))
        if self.refund:
            self.out.append(self.refund_credit("dividend"))
        # A payout always stays on the cash account, whatever external_cash says.
        self.sweep_to_cash(f"{self.label} {tx.name}")
        return self.out

    def trade(self) -> list[dict]:
        tx = self.tx
        if not tx.shares or tx.gross is None:
            raise MappingError(f"{tx.kind.value} {tx.id} has no share count or gross amount")
        quantity = fmt(tx.shares)
        unit_price = fmt(round6(tx.gross / tx.shares))
        fee = fmt(tx.fee)
        trade = _clean({
            "accountId": self.accounts.portfolio,
            "activityType": tx.kind.value,
            "activityDate": iso(tx.datetime),
            "currency": tx.currency,
            "quantity": quantity,
            "unitPrice": unit_price,
            "fee": fee,
            "tax": self.tax_field,
            "amount": trade_final_cash(tx.kind, quantity, unit_price, fee, self.tax_field or "0"),
            "comment": f"{self.label} {tx.name}{self.ref}",
            "asset": self.asset(),
        })
        if tx.kind == Kind.BUY:
            return self.buy(trade)
        return self.sell(trade)

    def buy(self, trade: dict) -> list[dict]:
        tx, accounts, ref, gid, out = self.tx, self.accounts, self.ref, self.gid, self.out
        if tx.bonus_funded:
            # As the addon's STOCKPERK-funded buy: the broker's money arrives on the
            # securities account as a bonus and pays the buy there.
            out.append(self.cash(accounts.portfolio, "CREDIT", -1, tx.net,
                                 f"{self.label} bonus for {tx.isin} ({tx.name}){ref}", subtype="BONUS"))
            out.append(trade)
            return out
        if tx.external_cash:
            out.append(self.cash(accounts.cash, "DEPOSIT", -3, tx.net, f"Deposit for {tx.isin} ({tx.name}) buy{ref}"))
        out.append(self.cash(accounts.cash, "TRANSFER_OUT", -2, tx.net,
                             f"Funds for {tx.isin} ({tx.name}) buy -> Portfolio{ref}", source_group=gid))
        out.append(self.cash(accounts.portfolio, "TRANSFER_IN", -1, tx.net, f"Funds from Cash for {tx.isin} buy{ref}",
                             source_group=gid))
        out.append(trade)
        if self.refund:
            out.append(self.refund_credit("purchase"))
        return out

    def sell(self, trade: dict) -> list[dict]:
        tx, out = self.tx, self.out
        out.append(trade)
        if self.refund:
            out.append(self.refund_credit("sale"))
        if not tx.net:  # written off as worthless: no money moves (as the addon)
            return out
        self.sweep_to_cash(f"{tx.isin} ({tx.name}) sale")
        if tx.external_cash:
            out.append(self.cash(self.accounts.cash, "WITHDRAWAL", 3, tx.net,
                                 f"Proceeds of {tx.isin} ({tx.name}) sale paid out{self.ref}"))
        return out


def _name(tx: Transaction, sep: str = " ") -> str:
    name = tx.name or tx.counterparty
    if not name:
        return ""
    return f"{sep}{name})" if sep == " (" else f"{sep}{name}"


def _text(tx: Transaction) -> str:
    # Purpose of a bank transaction, shortened: Wealthfolio shows the comment in one line.
    text = " ".join(tx.text.split())
    return f": {text[:120]}" if text else ""


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}
