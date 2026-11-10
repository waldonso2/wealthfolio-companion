"""Which Wealthfolio asset a broker's ISIN is booked under.

The addon books a security under the ticker the user mapped (``NVDA``), the
sync under the ISIN, and Wealthfolio's holdings carry no ISIN. So the asset is
learned from the activities of the broker's trades and dividends: the sync
state knows which Wealthfolio activities belong to a transaction - the sync's
own or the CSV/PDF activity it matched - and that activity names the asset.

Used for the holdings check (positions compared by asset, not by symbol) and
for booking: a new trade of a known ISIN goes onto the same asset, instead of
opening a second position under the ISIN.

An asset the addon created (another symbol than the ISIN) wins over one the
sync created under the ISIN.

Trades a CSV/PDF import booked on its own (not matched by the sync, e.g. DKB
statements imported before the sync read the giro account) are found too
(``learn_from_imports``): the import names the ISIN in the comment of a
trade's cash transfer ("Funds for <ISIN> (…) buy", "<ISIN> (…) sale -> Cash"),
booked a few seconds from the trade itself.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import meta_keys
from .mapping import Accounts, SecurityMapping, is_cash_symbol
from .model import Kind, Transaction, when
from .state import State, now
from .wealthfolio import WealthfolioClient, WealthfolioError

log = logging.getLogger(__name__)

SECURITY_KINDS = (Kind.BUY, Kind.SELL, Kind.DIVIDEND, Kind.SECURITY_OUT, Kind.SECURITY_IN)
SECURITY_TYPES = ("BUY", "SELL", "DIVIDEND")


def _preferred(isin: str, known: dict | None) -> bool:
    """True when ``known`` is already the asset to keep (one the addon created)."""
    return known is not None and bool(known) and known["symbol"] != isin


def learn(state: State, broker: str, transactions: list[Transaction], wf: WealthfolioClient,
          accounts: Accounts) -> int:
    """Learn the assets of the transactions' ISINs; returns how many were (re)learned."""
    known = state.assets(broker)
    wanted: dict[str, list[str]] = {}  # activity id -> ISINs
    dates = []
    for tx in transactions:
        if tx.kind not in SECURITY_KINDS or not tx.isin or _preferred(tx.isin, known.get(tx.isin)):
            continue
        ids = state.activity_ids(broker, tx.id)
        if ids:
            for i in ids:
                wanted.setdefault(i, []).append(tx.isin)
            dates.append(tx.datetime)
    if not wanted:
        return 0
    # Activities are stored at the transaction's time; a day of margin for time zones.
    activities = wf.search_activities([accounts.cash, accounts.portfolio], (min(dates) - timedelta(days=1)).date(),
                                      (max(dates) + timedelta(days=1)).date())
    learned = 0
    for a in activities:
        isins = wanted.get(a.get("id", ""))
        asset_id = a.get("assetId") or ""
        if not isins or a.get("activityType") not in SECURITY_TYPES or not asset_id or is_cash_symbol(asset_id):
            continue
        symbol = a.get("assetSymbol") or asset_id
        for isin in isins:
            current = known.get(isin)
            if _preferred(isin, current) or (current and current["asset_id"] == asset_id):
                continue
            entry: dict[str, Any] = {"asset_id": asset_id, "symbol": symbol,
                                     "exchange_mic": a.get("exchangeMic") or None, "name": a.get("assetName") or None}
            state.set_asset(broker, isin, **entry)
            known[isin] = entry
            learned += 1
    return learned


ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
# A trade and its transfer legs are booked this close together (the addon: 1-2 seconds apart).
LEG_SECONDS = 10
AMOUNT_TOLERANCE = Decimal("0.01")
# The reference a PDF import ends every activity of one statement with.
PDF_REF = re.compile(r"\[PDF [^\]]+\]")


def clean_isin(isin: str) -> str:
    """The ISIN of a key holdings read before 0.9.9 stored as "<ISIN>|/DE/<WKN>"."""
    return isin.split("|")[0].strip()


def valid_isin(code: str) -> bool:
    """ISO 6166 check digit (Luhn over the letters as numbers) - so a random 12-character word isn't one."""
    digits = "".join(str(int(c, 36)) for c in code[:-1])
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(code[-1])


def _when(a: dict) -> datetime:
    return when(a.get("date") or "")


def _entry(a: dict) -> dict:
    asset_id = a.get("assetId") or ""
    return {"asset_id": asset_id, "symbol": a.get("assetSymbol") or asset_id,
            "exchange_mic": a.get("exchangeMic") or None, "name": a.get("assetName") or None}


def _amount(a: dict) -> Decimal | None:
    try:
        return abs(Decimal(str(a.get("amount")))) if a.get("amount") not in (None, "") else None
    except InvalidOperation:
        return None


def _isins(text: str) -> set[str]:
    return {i for i in ISIN.findall(text) if valid_isin(i)}


def learn_from_imports(state: State, broker: str, wf: WealthfolioClient, accounts: Accounts,
                       isins: set[str], recheck: frozenset[str] | set[str] = frozenset()) -> int:
    """Learn the assets of ``isins`` (positions the broker reports whose asset is unknown or doesn't fit) from
    imported trades whose transfer names the ISIN; returns how many were learned or corrected.

    Exact first: a PDF import ends a trade's and its transfers' comments in the same ``[PDF <statement>]``;
    what that says replaces a stored asset. Then, for ISINs still unknown, by time and amount: the transfer
    within LEG_SECONDS with the trade's amount (a savings plan's executions share a second). An ISIN on
    trades of two assets stays unknown; what is learned narrows the next pass. ``recheck``: ISINs whose stored
    asset didn't fit last time - time and amount may replace it too (the sync's own trades name the ISIN in
    their transfers as well)."""
    known = state.assets(broker)
    activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1), date(2100, 1, 1))
    exact = _by_statement(activities, isins)
    corrected = _store_unique(state, broker, known, exact)
    wanted = {i for i in isins if len(exact.get(i, {})) != 1 and (i in recheck or not _preferred(i, known.get(i)))}
    if not wanted:
        return corrected
    trades = _trades_by_time(activities)
    learned = corrected + _learn_by_time(state, broker, known, wanted, trades)
    if wanted:
        _explain(broker, wanted, trades, known, exact)
    return learned


def _store_unique(state: State, broker: str, known: dict, found: dict[str, dict[str, dict]]) -> int:
    """Store each ISIN ``found`` names exactly one asset for, where it differs; how many."""
    stored = 0
    for isin, assets in found.items():
        if len(assets) == 1:
            entry = next(iter(assets.values()))
            if (known.get(isin) or {}).get("asset_id") != entry["asset_id"]:
                state.set_asset(broker, isin, **entry)
                known[isin] = entry
                stored += 1
    return stored


def _learn_by_time(state: State, broker: str, known: dict, wanted: set[str], trades: list) -> int:
    """Pass after pass: an ISIN that names exactly one asset not taken yet; what is learned leaves ``wanted``
    and narrows the next pass."""
    # An asset stored for the same ISIN under another spelling (python-fints' "<ISIN>|/DE/<WKN>") isn't taken.
    taken = {e["asset_id"] for i, e in known.items() if clean_isin(i) not in wanted}
    learned = 0
    while True:
        found: dict[str, dict[str, dict]] = {}  # ISIN -> asset id -> entry
        for entry, named in trades:
            left = named & wanted
            if len(left) == 1 and entry["asset_id"] not in taken:
                found.setdefault(next(iter(left)), {})[entry["asset_id"]] = entry
        new = {isin: next(iter(assets.values())) for isin, assets in found.items() if len(assets) == 1}
        if not new:
            return learned
        for isin, entry in new.items():
            if (known.get(isin) or {}).get("asset_id") != entry["asset_id"]:
                state.set_asset(broker, isin, **entry)
                learned += 1
            wanted.discard(isin)
            taken.add(entry["asset_id"])


def _by_statement(activities: list[dict], isins: set[str]) -> dict[str, dict[str, dict]]:
    """ISIN → asset id → entry, from a PDF import's trade whose transfers (same ``[PDF …]``) name one ISIN."""
    by_ref: dict[str, set[str]] = {}
    for a in activities:
        m = PDF_REF.search(a.get("comment") or "")
        if m and a.get("activityType") in ("TRANSFER_IN", "TRANSFER_OUT"):
            by_ref.setdefault(m.group(0), set()).update(_isins(a.get("comment") or ""))
    exact: dict[str, dict[str, dict]] = {}
    for a in activities:
        m = PDF_REF.search(a.get("comment") or "")
        named = by_ref.get(m.group(0), set()) & isins if m else set()
        if len(named) == 1 and a.get("activityType") in ("BUY", "SELL") and a.get("assetId") \
                and not is_cash_symbol(a.get("assetId")):
            exact.setdefault(next(iter(named)), {})[a["assetId"]] = _entry(a)
    return exact


def _trades_by_time(activities: list[dict]) -> list[tuple[dict, set[str]]]:
    """Each trade with the ISINs its transfers name: within LEG_SECONDS, of the same amount if there are any."""
    transfers = [(_when(a), _amount(a), _isins(a.get("comment") or ""))
                 for a in activities if a.get("activityType") in ("TRANSFER_IN", "TRANSFER_OUT")]
    trades = []
    for a in activities:
        asset_id = a.get("assetId") or ""
        if a.get("activityType") not in ("BUY", "SELL") or not asset_id or is_cash_symbol(asset_id):
            continue
        when, amount = _when(a), _amount(a)
        near = [(t_amount, names) for t, t_amount, names in transfers
                if names and abs((t - when).total_seconds()) <= LEG_SECONDS]
        same = [names for t_amount, names in near
                if amount is not None and t_amount is not None and abs(t_amount - amount) <= AMOUNT_TOLERANCE]
        named = set().union(*(same or [names for _, names in near]))
        if named:
            trades.append((_entry(a), named))
    return trades


def _explain(broker: str, wanted: set[str], trades: list, known: dict, exact: dict) -> None:
    """Why an ISIN's asset couldn't be learned - asset ids and ISINs only, to find the cause from the log."""
    owner = {e["asset_id"]: i for i, e in known.items() if clean_isin(i) not in wanted}
    for isin in sorted(wanted):
        if isin in exact:
            reason = f"die Abrechnungen nennen mehrere Wertpapiere ({', '.join(sorted(exact[isin]))})"
        else:
            assets_ = sorted({e["asset_id"] for e, named in trades if isin in named})
            if not assets_:
                reason = "kein Kauf/Verkauf, dessen Übertrag die ISIN nennt (Konten, Zeitabstand, ISIN geändert?)"
            else:
                taken = [f"{a} (gehört zu {owner[a]})" for a in assets_ if a in owner and owner[a] != isin]
                shared = [a for a in assets_ if any(isin in named and len(named) > 1
                                                    for e, named in trades if e["asset_id"] == a)]
                reason = f"Käufe/Verkäufe auf {', '.join(assets_)}"
                if taken:
                    reason += f"; schon vergeben: {', '.join(taken)}"
                if shared:
                    reason += "; Überträge nennen dort mehrere ISINs"
        log.info("%s: Wealthfolio-Wertpapier zu %s nicht zugeordnet: %s", broker, isin, reason)


def mappings(state: State, broker: str) -> dict[str, SecurityMapping]:
    return {isin: SecurityMapping(a["symbol"], a["exchange_mic"], a["name"], a["asset_id"])
            for isin, a in state.assets(broker).items()}


def complete_isins(state: State, wf: WealthfolioClient, learned: dict[str, str]) -> int:
    """The ISIN into ``metadata.identifiers.isin`` of every asset the sync knows (``learned``: asset id ->
    ISIN) that has none (#140): Wealthfolio matches imports, quotes and bonds by it. An ISIN already there is
    never replaced, and one the sync set is set once - removed by the user, it stays removed. Returns how
    many were set; an asset Wealthfolio refuses is skipped."""
    done = 0
    for asset in wf.list_assets():
        isin = learned.get(asset.get("id", ""))
        if not isin or not valid_isin(isin) or state.meta(meta_keys.ISIN(asset["id"])):
            continue
        metadata = asset.get("metadata") or {}
        identifiers = metadata.get("identifiers") or {}
        if identifiers.get("isin"):
            continue
        try:  # Wealthfolio replaces the whole metadata: send all of it, the ISIN added
            wf.update_asset_profile(asset, {**metadata, "identifiers": {**identifiers, "isin": isin}})
        except WealthfolioError:
            log.exception("ISIN of %s not set", asset["id"])
            continue
        state.set_meta(meta_keys.ISIN(asset["id"]), json.dumps({"isin": isin, "at": now()}))
        done += 1
    return done


def completed(state: State) -> dict[str, dict]:
    """Asset id -> {isin, at} of every ISIN ``complete_isins`` set; ``at`` empty for one set before 0.47.0
    (stored as the bare ISIN then)."""
    out = {}
    for asset_id, value in state.metas(meta_keys.ISIN).items():
        out[asset_id] = json.loads(value) if value.startswith("{") else {"isin": value, "at": ""}
    return out
