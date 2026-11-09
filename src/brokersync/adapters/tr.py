"""Trade Republic via its unofficial app API, using pytr (read-only).

- **Login:** the web login (pytr's v2 login, as app.traderepublic.com): phone
  number and PIN, then a confirmation in the Trade Republic app - or a code
  from an authenticator app, if the account uses one. Unlike a device reset it
  doesn't log out the phone. The session cookies are kept (encrypted) in the
  session and resumed by later runs; once Trade Republic ends the session, a
  scheduled run asks for the confirmation via ntfy and waits for it.
- **Data:** the timeline (transactions and activity log) with each event's
  details, parsed by pytr's ``Event`` - the same parser pytr's own exports use.
  Events are mapped to the rules of the addon's CSV import (``transform.ts``):
  trades with fee and tax, dividends net with withholding tax, Saveback as a
  bonus-funded buy, interest, Vorabpauschale, tax corrections (refunds and
  charges), referral bonuses, deposits, card payments and transfers; of the
  corporate actions worthless write-offs (a sale at 0) and dividend
  reinvestments (a buy paid from the cash account, the share count read from
  the event's details; without it the event stays unknown). Other corporate
  actions, securities transfers and private markets are reported as unknown:
  the CSV import handles them.
- **Purely informational events** (orders created/cancelled, documents, ...),
  listed by pytr, are skipped silently; anything else pytr can't classify is
  reported.
- **Status:** cancelled/failed events are skipped.

The API is unofficial and may change; pytr follows those changes, so it is
pinned to a known version and updated deliberately.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from .. import ratelimit
from ..model import CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField, rate_limited

SKIPPED_STATUS = {"CANCELED", "CANCELLED", "FAILED", "REJECTED", "PENDING"}
# Seconds to wait for a websocket answer.
TIMEOUT = 30
# Detail requests in flight at once.
BATCH = 20
# Seconds between two groups of details (plus up to as much jitter), so TR isn't asked in one burst (#149).
TR_PAUSE = 0.25


def _dec(x) -> Decimal | None:
    # pytr hands out floats; round to 8 places and drop trailing zeros (no exponent).
    if x is None:
        return None
    d = Decimal(x) if isinstance(x, str) else Decimal(str(round(float(x), 8)))  # text as it is, no float
    return d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()


def to_transactions(events: list[dict]) -> list[Transaction]:
    """Timeline events (with ``details``) → transactions, via pytr's parser."""
    from pytr.event import (
        ConditionalEventType,
        Event,
        PPEventType,
        events_known_ignored,
        events_known_ignored_subtitle,
        events_known_ignored_title,
    )

    out: list[Transaction] = []
    for raw in events:
        status = (raw.get("status") or "").upper()
        if status in SKIPPED_STATUS:
            continue
        event_type = (raw.get("eventType") or "").upper()
        try:
            ev = Event.from_dict(raw)
        except Exception as e:  # pytr can't read it: report, don't drop
            out.append(_unknown(raw, f"unreadable: {type(e).__name__}"))
            continue
        kind = ev.event_type
        value = _dec(ev.value) or Decimal(0)
        fees = abs(_dec(ev.fees) or Decimal(0))
        taxes = abs(_dec(ev.taxes) or Decimal(0))
        parsed_shares = _dec(ev.shares) if ev.shares is not None else None
        shares = abs(parsed_shares) if parsed_shares is not None else None
        base = dict(id=raw["id"], datetime=ev.date.astimezone(UTC), currency=_currency(raw), label=_label(raw),
                    raw_type=event_type)
        security = dict(isin=ev.isin, name=ev.title or "", shares=shares)
        if kind is None:
            if (event_type in events_known_ignored or raw.get("title") in events_known_ignored_title
                    or raw.get("subtitle") in events_known_ignored_subtitle) or raw.get("amount") is None:
                continue  # informational, no money or shares moved
            out.append(_unclassified(value, base, raw, ev)
                       or _unknown(raw, event_type or raw.get("subtitle") or raw.get("title") or "?"))
            continue

        if kind == ConditionalEventType.TRADE_INVOICE and not ev.shares2:
            out.append(_trade(raw, value, fees, taxes, base, security))
        elif kind == ConditionalEventType.SAVEBACK and value < 0:
            out.append(Transaction(kind=Kind.BUY, net=-value, gross=-value - fees, fee=fees, bonus_funded=True,
                                   **base, **security))
        elif kind == PPEventType.DIVIDEND and value > 0 and ev.isin and shares:
            out.append(Transaction(kind=Kind.DIVIDEND, net=value, gross=value + taxes, tax=taxes, **base, **security))
        elif kind == PPEventType.INTEREST and value > 0:
            out.append(Transaction(kind=Kind.INTEREST, net=value, gross=value + taxes, tax=taxes, **base,
                                   name=ev.title or ""))
        elif kind in (PPEventType.TAXES, PPEventType.TAX_REFUND) and value < 0:  # a tax correction can charge
            out.append(Transaction(kind=Kind.TAX, net=-value, **base, name=_named(ev)))
        elif kind == PPEventType.TAX_REFUND and value > 0:
            out.append(Transaction(kind=Kind.TAX_REFUND, net=value, **base, name=_named(ev)))
        elif kind in (PPEventType.DEPOSIT, PPEventType.REMOVAL) and value != 0:
            # Money in is always a deposit (addon rule); money out is spending
            # unless a transfer pattern says it went to an own account.
            inbound = value > 0
            out.append(Transaction(kind=Kind.DEPOSIT if inbound else Kind.WITHDRAWAL, net=abs(value),
                                   counterparty=ev.title or "", counterparty_iban=_detail(raw, "IBAN"),
                                   **{**base, "label": _cash_label(raw, inbound)}))
        else:
            # Corporate actions, securities transfers, private markets, swaps ...
            name = kind.name if hasattr(kind, "name") else str(kind)
            out.append(_unknown(raw, f"{event_type or name} ({name})"))
    return out


def _trade(raw: dict, value: Decimal, fees: Decimal, taxes: Decimal, base: dict, security: dict) -> Transaction:
    shares = security["shares"]
    if value < 0:  # buy: paid = market value + fee + tax
        return Transaction(kind=Kind.BUY, net=-value, gross=-value - fees - taxes, fee=fees, tax=taxes,
                           **base, **security)
    if shares and value > 0:  # sell: received = market value - fee - tax
        return Transaction(kind=Kind.SELL, net=value, gross=value + fees + taxes, fee=fees, tax=taxes,
                           **base, **security)
    if shares and value == 0 and raw.get("subtitle") == "Wertlos":  # written off: a sale at 0, as the CSV import
        return Transaction(kind=Kind.SELL, net=Decimal(0), gross=Decimal(0), **base, **security)
    return _unknown(raw, f"{base['raw_type']} {raw.get('subtitle') or ''}".strip())


def _unclassified(value: Decimal, base: dict, raw: dict, ev) -> Transaction | None:
    """Events pytr has no type for that the CSV import books (``transform.ts``); None: still unknown."""
    event_type = base["raw_type"]
    if event_type.startswith("REFERRAL_") and value > 0:  # the inviter's referral bonus
        return Transaction(kind=Kind.BONUS, net=value, **base)
    if event_type == "SSP_CORPORATE_ACTION_INSTRUMENT" and "Reinvestition" in (raw.get("subtitle") or ""):
        # A dividend reinvestment: a buy of the new shares paid from the cash account (as the CSV import). pytr reads
        # no share count, the details' "Aktien" row has it; without it, or without a debit, nothing is guessed.
        shares = _german_number(_detail(raw, "Aktien"))
        if value < 0 and ev.isin and shares:
            return Transaction(kind=Kind.BUY, net=-value, gross=-value, **base,
                               isin=ev.isin, name=ev.title or "", shares=shares)
    return None


def _german_number(text: str) -> Decimal | None:
    """``1.234,56`` → Decimal; None unless it is a positive number (German notation, as the app shows it)."""
    try:
        number = Decimal(text.replace(".", "").replace(",", "."))
    except ArithmeticError:
        return None
    return number if number.is_finite() and number > 0 else None


def portfolio_positions(portfolio: dict | None) -> list[dict]:
    """Positions of a compactPortfolioByType answer: grouped in categories (current
    format, field "isin"); a flat "positions" list in the older compactPortfolio."""
    portfolio = portfolio or {}
    if "categories" in portfolio:
        return [p for cat in portfolio.get("categories") or [] for p in cat.get("positions") or []]
    return list(portfolio.get("positions") or [])


def _cash_label(raw: dict, inbound: bool) -> str:
    if raw.get("subtitle"):
        return raw["subtitle"]
    event_type = (raw.get("eventType") or "").upper()
    if "CARD" in event_type:
        return "Kartenerstattung" if inbound else "Kartenzahlung"
    return "Einzahlung" if inbound else "Auszahlung"


def _detail(raw: dict, title: str) -> str:
    """Text of a detail row (e.g. the counterparty's IBAN), searched in all sections."""
    stack = list((raw.get("details") or {}).get("sections") or [])
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if item.get("title") == title and isinstance(item.get("detail"), dict):
                text = item["detail"].get("text")
                if isinstance(text, str):
                    return text.replace(" ", "").upper()
            stack.extend(v for v in item.values() if isinstance(v, (list, dict)))
        elif isinstance(item, list):
            stack.extend(item)
    return ""


def _named(ev) -> str:
    return f"{ev.title} {ev.isin}" if ev.isin else (ev.title or "")


def _label(raw: dict) -> str:
    return (raw.get("subtitle") or raw.get("title") or "").strip()


def _currency(raw: dict) -> str:
    return (raw.get("amount") or {}).get("currency") or "EUR"


def _detail_rows(raw: dict) -> list[dict[str, str]]:
    """The title/text rows of an event's details view in order (at most 30, short). Kept for corporate actions
    the service cannot book yet (a reverse split, a swap into another ISIN): the share counts, the ratio and the new
    ISIN a booking needs are in these rows, and the app's own text is all there is of them."""
    rows: list[dict[str, str]] = []

    def walk(node, depth=0) -> None:
        if depth > 6 or len(rows) >= 30:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            detail = node.get("detail")
            title, text = node.get("title"), detail.get("text") if isinstance(detail, dict) else None
            if isinstance(title, str) and isinstance(text, str):
                rows.append({"title": title[:60], "text": text[:80]})
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value, depth + 1)

    walk((raw.get("details") or {}).get("sections") or [])
    return rows


def _unknown(raw: dict, raw_type: str) -> Transaction:
    ts = raw.get("timestamp") or "1970-01-01T00:00:00.000+0000"
    ts = ts[:-2] + ":" + ts[-2:] if ts[-5] in "+-" else ts
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        when = datetime.now(UTC)
    # What identifies the event type, and its amount so it can be booked by hand -
    # no counterparties, account numbers or ids beyond the event's own.
    payload = {"eventType": raw.get("eventType"), "title": raw.get("title"), "subtitle": raw.get("subtitle"),
               "status": raw.get("status")}
    amount = raw.get("amount") or {}
    if amount.get("value") is not None:
        payload["betrag"] = f"{amount['value']} {amount.get('currency') or 'EUR'}"
    if (raw.get("eventType") or "").upper().startswith("SSP_CORPORATE_ACTION") and (rows := _detail_rows(raw)):
        payload["details"] = rows
    return Transaction(id=raw.get("id", ""), kind=Kind.UNKNOWN, datetime=when, currency=_currency(raw),
                       net=Decimal(0), label=_label(raw), raw_type=raw_type, raw=payload)


class TradeRepublicAdapter(BrokerAdapter):
    key = "tr"
    label = "Trade Republic"
    credential_fields: ClassVar[list[CredentialField]] = [
        CredentialField("phone", "Telefonnummer", help="Mit Ländervorwahl, z. B. +4917612345678."),
        CredentialField("pin", "PIN", secret=True, help="Die 4-stellige PIN der Trade-Republic-App."),
    ]

    consent = ("Trade Republic bietet keine offizielle Schnittstelle. Der Dienst nutzt die der Web-App (über die "
               "Bibliothek pytr), wie andere Finanzprogramme auch. Trade Republic kann sie jederzeit ändern oder "
               "solche Zugriffe untersagen; dann klappt der Abruf nicht mehr. Der Dienst liest nur, er handelt nie.")
    # Replaced in tests; real runs use pytr.
    reports_positions = True

    api_factory = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._api = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._replay: dict | None = None

    # ── login ───────────────────────────────────────────────────────────────
    def _make_api(self):
        phone = (self.credentials.get("phone") or "").replace(" ", "")
        pin = self.credentials.get("pin") or ""
        if not phone or not pin:
            raise AdapterError("Telefonnummer und PIN fehlen - bitte in den Broker-Einstellungen eintragen.")
        # pytr keeps cookies in a file; it lives only while this adapter does.
        self._tmp = tempfile.TemporaryDirectory(prefix="companion-tr-")
        cookies = Path(self._tmp.name) / "cookies.txt"
        fd = os.open(cookies, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.session.get("cookies") or "# Netscape HTTP Cookie File\n")
        factory = type(self).api_factory or _pytr_api
        return factory(phone, pin, cookies)

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                "Trade Republic hat Telefonnummer oder PIN abgelehnt. Bitte in den Broker-Einstellungen neu "
                "eintragen - bis dahin versucht der Dienst es nicht noch einmal."
            )
        self._api = self._make_api()
        try:
            if self.session.get("cookies") and self._api.resume_websession():
                self._keep_cookies()
                return
            self._api.initiate_weblogin()
        except Exception as e:
            self._fail(e)
        if self._api.weblogin_needs_authenticator:
            raise AuthRequired(Challenge("code", "Gib den Code aus deiner Authenticator-App ein."))
        if self.on_user_action:
            # Scheduled run: ask on the phone and wait for it (pytr polls until
            # Trade Republic's deadline, about two minutes).
            self.on_user_action("Bitte bestätige die Anmeldung von Wealthfolio Companion in der Trade-Republic-App.")
            self.complete_login("")
            return
        raise AuthRequired(Challenge("confirm", "Bestätige die Anmeldung in der Trade-Republic-App."))

    def complete_login(self, code: str) -> None:
        try:
            self._api.complete_weblogin(code or None)
        except TimeoutError as e:
            raise AuthRequired(Challenge("confirm", "Die Anmeldung wurde nicht rechtzeitig bestätigt. "
                                                    "Bitte neu starten.")) from e
        except Exception as e:
            self._fail(e)
        self._keep_cookies()

    def _keep_cookies(self) -> None:
        jar = self._api._websession.cookies
        if hasattr(jar, "save"):
            jar.save(ignore_discard=True)
            self.session["cookies"] = Path(jar.filename).read_text()

    def _fail(self, e: Exception):
        text = str(e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (400, 401, 403) and "login" in str(getattr(getattr(e, "response", None), "url", "")):
            self.session["pin_rejected"] = True
            raise AdapterError("Trade Republic hat Telefonnummer oder PIN abgelehnt.") from e
        if "TOO_MANY_REQUESTS" in text or "Too many attempts" in text:
            # pytr shows TR's throttle as a TradeRepublicError (an "E" answer on the websocket) naming
            # TOO_MANY_REQUESTS, or a login error: stop, the next scheduled run waits (#149). Details are not
            # tried again - a transaction without its details would be booked wrongly.
            raise rate_limited(self.label) from e
        if isinstance(e, AuthRequired | AdapterError):
            raise e
        raise AdapterError(f"Trade Republic: {text or type(e).__name__}") from e

    def close(self) -> None:
        if self._api is not None and self._loop is not None:
            with suppress(Exception):
                self._loop.run_until_complete(self._api.close())
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ── websocket ───────────────────────────────────────────────────────────
    def _run(self, coro):
        # One event loop for the adapter's life: pytr's websocket belongs to it.
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        try:
            return self._loop.run_until_complete(asyncio.wait_for(coro, TIMEOUT * 10))
        except (AuthRequired, AdapterError):
            raise
        except Exception as e:
            self._fail(e)

    async def _one(self, subscribe):
        """Subscribe and return the first answer for that subscription."""
        sub_id = await subscribe
        while True:
            got_id, _, response = await asyncio.wait_for(self._api.recv(), TIMEOUT)
            if got_id == sub_id:
                await self._api.unsubscribe(sub_id)
                return response

    async def _timeline(self, since: datetime | None) -> list[dict]:
        events: dict[str, dict] = {}
        for fetch in (self._api.timeline_transactions, self._api.timeline_activity_log):
            after = None
            while True:
                page = await self._one(fetch(after))
                older = False
                for item in page.get("items", []):
                    ts = _parse_ts(item.get("timestamp"))
                    if since and ts and ts < since:
                        older = True
                        break
                    events.setdefault(item["id"], item)
                after = (page.get("cursors") or {}).get("after")
                if older or not after:
                    break
        wanted = [e for e in events.values()
                  if (e.get("action") or {}).get("type") == "timelineDetail"
                  and (e.get("action") or {}).get("payload") == e["id"]]
        for i in range(0, len(wanted), BATCH):
            if i:
                ratelimit.sleep(TR_PAUSE * (1 + random.random()))  # noqa: S311 - jitter, no secret
            batch = wanted[i:i + BATCH]
            pending = {await self._api.timeline_detail_v2(e["id"]): e for e in batch}
            while pending:
                got_id, _, response = await asyncio.wait_for(self._api.recv(), TIMEOUT)
                event = pending.pop(got_id, None)
                if event is not None:
                    event["details"] = response
                    await self._api.unsubscribe(got_id)
        return sorted(events.values(), key=lambda e: e.get("timestamp") or "")

    # ── data ────────────────────────────────────────────────────────────────
    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        if self._replay is not None:
            events = [e for e in self._replay.get("events", [])
                      if since is None or (_parse_ts(e.get("timestamp")) or since) >= since]
            return to_transactions(events)
        return to_transactions(self._run(self._timeline(since)))

    def get_cash(self) -> list[CashBalance]:
        cash = self._replay.get("cash", []) if self._replay is not None else self._run(self._one(self._api.cash()))
        return [CashBalance(c.get("currencyId", "EUR"), _dec(c.get("amount")) or Decimal(0)) for c in cash or []]

    def get_positions(self) -> list[Position]:
        if self._replay is not None:
            portfolio = self._replay.get("portfolio", {})
        else:
            portfolio = self._run(self._one(self._api.compact_portfolio()))
        return [Position(p.get("isin") or p.get("instrumentId", ""), p.get("name", ""),
                         _dec(p.get("netSize")) or Decimal(0), "EUR")
                for p in portfolio_positions(portfolio)]

    @classmethod
    def replay(cls, recording: dict) -> TradeRepublicAdapter:
        """Recording: ``{"events": [timeline items with "details"], "cash": [...], "portfolio": {...}}``."""
        adapter = cls({"phone": "+490000000000", "pin": "0000"})
        adapter._replay = recording
        return adapter


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    ts = ts.replace("Z", "+00:00")
    if len(ts) > 5 and ts[-5] in "+-" and ":" not in ts[-5:]:
        ts = ts[:-2] + ":" + ts[-2:]
    try:
        return datetime.fromisoformat(ts).astimezone(UTC)
    except ValueError:
        return None


def _pytr_api(phone: str, pin: str, cookies: Path):
    from pytr.api import TradeRepublicApi

    return TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=True, cookies_file=str(cookies),
                            use_v2_login=True)
