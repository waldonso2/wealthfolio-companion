"""What a broker reports, independent of the broker.

Adapters (``brokersync.adapters``) turn a broker's API answers into these
types; ``brokersync.mapping`` turns them into Wealthfolio activities. Amounts
are ``Decimal`` throughout - never floats - so the activity amounts are exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


def when(value: str | date | datetime) -> datetime:
    """A moment from Wealthfolio or the state (#119), always in UTC: ``…Z``, an offset, no zone at all (taken as
    UTC) or only a day (its midnight UTC) - so comparing two of them never fails on naive vs. aware."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


class Kind(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    FEE = "FEE"
    TAX = "TAX"
    TAX_REFUND = "TAX_REFUND"
    # Money the broker gives (Trade Republic referral bonus): cash CREDIT/BONUS.
    BONUS = "BONUS"
    # Cash side of a securities trade or payout on a bank account (e.g. the DKB
    # giro debit for a depot buy). Booked by the securities side (PDF import,
    # depot adapter) as a transfer pair, so the sync only checks that it is
    # there and reports it when it isn't - booking it again would count it twice.
    SECURITIES_CASH = "SECURITIES_CASH"
    # A stock split or reverse split: ``ratio`` new shares per old share (1:10 reverse = 0.1); no money moves.
    SPLIT = "SPLIT"
    # One ISIN exchanged for another (a reverse split into a new instrument): the old position leaves, the new one
    # arrives, no money moves. Each leg is an unpaired TRANSFER_OUT / TRANSFER_IN of the securities account that
    # carries the cost basis as ``net`` (unit price = ``net`` / ``shares``) and is marked external, as the CSV
    # import books it (waldonso2/wealthfolio-importer-addon#125).
    SECURITY_OUT = "SECURITY_OUT"
    SECURITY_IN = "SECURITY_IN"
    # An event type the adapter doesn't know. It is never dropped: the sync
    # stores and reports it (AC 10), and nothing is booked for it.
    UNKNOWN = "UNKNOWN"


SECURITY_KINDS = {Kind.BUY, Kind.SELL, Kind.DIVIDEND}


@dataclass(frozen=True)
class Position:
    isin: str
    name: str
    shares: Decimal
    currency: str
    # Average purchase price per unit, if the broker reports one.
    cost: Decimal | None = None


# ``Transaction.account`` / ``CashBalance.account`` of the broker's savings account (a bank's Tagesgeld);
# empty = the cash account.
SAVINGS = "savings"


@dataclass(frozen=True)
class CashBalance:
    currency: str
    amount: Decimal
    account: str = ""  # SAVINGS for the savings account's balance


@dataclass(frozen=True)
class Transaction:
    # The broker's own, stable reference for this transaction. Part of the
    # activity comments and of the sync state, so the same transaction is
    # never booked twice.
    id: str
    kind: Kind
    # Timezone-aware; trades at execution time, income at the payment date.
    datetime: datetime
    # Booking currency of the cash account.
    currency: str
    # The amount booked on the cash account, always positive.
    net: Decimal
    isin: str | None = None
    name: str = ""
    shares: Decimal | None = None
    # Market value of a trade or gross income of a dividend/interest payment.
    gross: Decimal | None = None
    fee: Decimal = Decimal(0)
    # Income taxes: positive = charged, negative = refunded.
    tax: Decimal = Decimal(0)
    # The broker's own type text, e.g. "Kauf", "Sparplan" - for comments.
    label: str = ""
    # Bank transactions: counterparty and purpose text - for comments and
    # transfer patterns.
    counterparty: str = ""
    counterparty_iban: str = ""
    text: str = ""
    # A buy the broker paid for (Trade Republic Saveback): booked as a bonus
    # credit on the securities account that funds the buy, no cash transfer.
    bonus_funded: bool = False
    # The money of a trade comes from / goes to outside the broker's two
    # accounts (comdirect, whose giro account isn't booked; an opening
    # position; a Deutsche Bank depot row older than the giro history read): a
    # buy is preceded by a deposit of its cost on the cash account, a sale
    # followed by a withdrawal of its proceeds. A dividend ignores it: its
    # money stays on the cash account.
    external_cash: bool = False
    # Which of the broker's bank accounts it was booked on: empty = the cash
    # account, SAVINGS = the savings account (a bank's Tagesgeld).
    account: str = ""
    # A transfer between the broker's cash and savings account, seen on the
    # cash account: booked as one transfer pair between the two. Its other side,
    # seen on the savings account, names this one's id in ``pair`` and is not
    # booked on its own.
    own_transfer: bool = False
    pair: str = ""
    # Signed amount for SECURITIES_CASH: negative = paid out of the account.
    signed: Decimal | None = None
    # The broker's raw event type, shown for UNKNOWN events.
    raw_type: str = ""
    # Original payload of an UNKNOWN event, stored for the report (never secrets).
    raw: dict = field(default_factory=dict, compare=False)
    # A split's factor on the share count (Wealthfolio keeps it in the SPLIT activity's amount).
    ratio: Decimal | None = None


def to_dict(tx: Transaction) -> dict:
    """JSON-friendly form of a transaction (for contract tests and reports)."""
    out = {}
    for name in Transaction.__dataclass_fields__:
        v = getattr(tx, name)
        if name == "raw" or v is None or v == "" or v is False:
            continue
        if isinstance(v, Decimal):
            if name in ("fee", "tax") and v == 0:
                continue
            v = format(v.normalize(), "f")
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, Kind):
            v = v.value
        out[name] = v
    return out
