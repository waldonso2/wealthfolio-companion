"""Finds transactions that are already in Wealthfolio.

Checked against the activities of the broker's accounts:

- **Synced before** (e.g. the sync state was lost or a run was interrupted):
  activities carrying this transaction's ``[SYNC <broker>:<id>]`` reference
  are left to Wealthfolio's own duplicate check - see ``find``.
- **Imported by the Broker Importer addon** from the CSV export or a PDF
  statement: those have other comments and times, so they are matched by
  substance, like the addon's ``matchExisting``: same account and type, at
  most 36 hours apart, same share count (not for dividends) and the same
  amount within 0.02. The security should match too, but the addon books it
  under the ticker the user mapped while the sync uses the ISIN: so a trade
  with a different symbol still matches, a dividend only if it is the only
  candidate. Cash-only activities match on account, type and amount.

Each existing activity matches at most one transaction.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right, insort
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from .mapping import is_cash_symbol
from .model import when

MATCH_WINDOW_SECONDS = 36 * 60 * 60
AMOUNT_TOLERANCE = Decimal("0.02")
MAIN_TYPES = {"BUY", "SELL", "DIVIDEND", "DEPOSIT", "WITHDRAWAL", "INTEREST", "FEE", "TAX", "CREDIT"}
SYNC_TAG = re.compile(r"\[SYNC [^\]]*\]")  # the reference at the end of every comment the sync writes


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return abs(Decimal(str(v)))
    except InvalidOperation:
        return None


def _when(s: str) -> datetime:
    return when(s)


def main_activity(payloads: list[dict]) -> dict:
    """The activity that stands for the transaction (not a transfer leg or refund credit)."""
    for p in payloads:
        if p["activityType"] in ("BUY", "SELL", "DIVIDEND"):
            return p
    return payloads[0]


class ExistingIndex:
    def __init__(self, existing: list[dict]):
        self.existing = existing
        self.used: set[str] = set()
        self._parsed: dict[int, tuple] = {}
        # By account and type, sorted by time (then list position), so a find looks only at its time window.
        self._buckets: dict[tuple, list[tuple[datetime, int, dict]]] = {}
        self._indexed = 0
        # The ids of the activities carrying each "[SYNC broker:id]" reference, filled like the buckets.
        self._tags: dict[str, set[str]] = {}
        self._tagged = 0

    def _tagged_ids(self, tag: str) -> set[str]:
        for e in self.existing[self._tagged:]:
            for t in SYNC_TAG.findall(e.get("comment") or ""):
                self._tags.setdefault(t, set()).add(e["id"])
        self._tagged = len(self.existing)
        return self._tags.get(tag, set())

    def _facts(self, e: dict) -> tuple[datetime, Decimal | None, Decimal | None, Decimal | None]:
        """Date, amount, quantity and unit price of an existing activity, parsed once: every find compares
        against all of them (the sync appends to ``existing`` during a run, so it is filled as they come)."""
        hit = self._parsed.get(id(e))
        if hit is None or hit[0] is not e:
            hit = (e, _when(e["date"]), _dec(e.get("amount")), _dec(e.get("quantity")), _dec(e.get("unitPrice")))
            self._parsed[id(e)] = hit
        return hit[1:]

    def _near(self, account: str | None, activity_type: str, moment: datetime, seconds: float) -> list[tuple]:
        """(time, list position, activity) of the account's activities of the type at most ``seconds`` away,
        in list order. The sync appends to ``existing`` during a run: those are added on the next call."""
        for pos in range(self._indexed, len(self.existing)):
            e = self.existing[pos]
            bucket = self._buckets.setdefault((e.get("accountId"), e.get("activityType")), [])
            insort(bucket, (self._facts(e)[0], pos, e))
        self._indexed = len(self.existing)
        bucket = self._buckets.get((account, activity_type), [])
        window = timedelta(seconds=seconds)
        near = bucket[bisect_left(bucket, (moment - window,)):bisect_right(bucket, (moment + window, float("inf")))]
        return sorted(near, key=lambda t: t[1])

    def find(self, ref: str, payloads: list[dict]) -> str | None:
        """Id of the existing activity (from another source) this transaction already is, or None.

        Activities this service created itself (they carry a ``[SYNC …]`` reference) are not a
        match: its own ones are re-sent whole and Wealthfolio rejects the parts that exist as
        duplicates, so an interrupted run is completed; another transaction's (two equal card
        payments within the window) are that one, never this one.
        """
        self.used |= self._tagged_ids(ref.strip())
        a = main_activity(payloads)
        if a["activityType"] not in MAIN_TYPES:
            return None
        when = _when(a["activityDate"])
        amount = _dec(a.get("amount")) or Decimal(0)
        quantity = _dec(a.get("quantity"))
        symbol = a.get("asset", {}).get("symbol", "")
        # Cash activities carry no asset (older sync versions: a $CASH one).
        is_cash = not symbol or is_cash_symbol(symbol)

        def same_substance(e: dict) -> bool:
            if e["id"] in self.used or e.get("accountId") != a["accountId"] or "[SYNC " in (e.get("comment") or ""):
                return False
            if e.get("activityType") != a["activityType"] or (e.get("subtype") or None) != a.get("subtype"):
                return False
            if e.get("currency") and a.get("currency") and e["currency"] != a["currency"]:
                return False  # another currency is another booking (#100)
            e_when, existing_amount, e_quantity, e_price = self._facts(e)
            if abs((e_when - when).total_seconds()) > MATCH_WINDOW_SECONDS:
                return False
            if existing_amount is None:
                existing_amount = e_quantity * e_price if e_quantity is not None and e_price is not None else None
            if existing_amount is None or abs(existing_amount - amount) > AMOUNT_TOLERANCE:
                return False
            return a["activityType"] not in ("BUY", "SELL") or e_quantity == quantity

        def same_security(e: dict) -> bool:
            return is_cash or symbol in (e.get("assetSymbol"), e.get("assetId"))

        def amount_of(e: dict) -> Decimal:
            existing_amount = self._facts(e)[1]
            return existing_amount if existing_amount is not None else Decimal(0)

        # The nearest in time first, then the closest amount (#100): two equal savings-plan deposits on two days
        # must each find their own, not the first in Wealthfolio's list.
        near = self._near(a["accountId"], a["activityType"], when, MATCH_WINDOW_SECONDS)
        candidates = sorted((e for _, _, e in near if same_substance(e)),
                            key=lambda e: (abs((self._facts(e)[0] - when).total_seconds()),
                                           abs(amount_of(e) - amount)))
        # Same security first. The addon books a security under the ticker the
        # user mapped it to, the sync under the ISIN, so a different symbol is
        # still a match when nothing else is - for trades the share count and
        # amount within a day and a half pin it down.
        match = next((e for e in candidates if same_security(e)), None)
        if match is None and not is_cash and (a["activityType"] in ("BUY", "SELL") or len(candidates) == 1):
            match = candidates[0] if candidates else None
        if match is None:
            return None
        self.used.add(match["id"])
        return match["id"]

    def find_settlement(self, cash_account: str, signed: Decimal, when: datetime, days: int) -> str | None:
        """The transfer leg the securities side booked on the cash account for a bank settlement.

        A debit (``signed`` < 0) pays a buy: TRANSFER_OUT to the depot. A credit
        is the proceeds of a sale or a payout: TRANSFER_IN from the depot.
        """
        want = "TRANSFER_OUT" if signed < 0 else "TRANSFER_IN"
        amount = abs(signed)
        best = None
        for _, _, e in self._near(cash_account, want, when, days * 86400):
            if e["id"] in self.used:
                continue
            e_when, existing_amount = self._facts(e)[:2]
            if existing_amount is None or abs(existing_amount - amount) > AMOUNT_TOLERANCE:
                continue
            gap = abs((e_when - when).total_seconds())
            if gap > days * 86400:
                continue
            if best is None or gap < best[0]:
                best = (gap, e["id"])
        if best is None:
            return None
        self.used.add(best[1])
        return best[1]
