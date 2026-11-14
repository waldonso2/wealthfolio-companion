"""comdirect via its official REST API for private customers (read-only).

The user enables the API in the comdirect online banking (persönlicher Bereich
→ Zugänge → REST API) and gets a client ID and secret there. Specification:
"comdirect REST API – Schnittstellenspezifikation" (April 2020).

- **Login** (five calls, chapter 2): OAuth2 password grant with Zugangsnummer
  and PIN, the session object, a TAN challenge for it, its activation, and the
  ``cd_secondary`` grant that issues the token for banking and brokerage. With
  photoTAN-Push the user approves in the photoTAN app: in the web UI they click
  "confirmed", a scheduled run sends a notification and polls the challenge's
  status link. A photoTAN graphic or mobileTAN is typed in the web UI.
- **Session:** access token (10 minutes) and refresh token are kept in the
  session and refreshed while comdirect allows; after that a new login with TAN
  is needed.
- **Lock-out guard:** comdirect locks the online banking after five TAN
  challenges without a correct TAN in between, and after three wrong TANs. The
  adapter counts unanswered challenges and wrong TANs in the session and stops
  asking long before that (``MAX_OPEN_CHALLENGES``, ``MAX_WRONG_TANS``); a
  rejected PIN stops it too, until the credentials are saved anew.
- **Only the depot is booked, not the giro account.** The cash account in
  Wealthfolio receives the dividends (swept from the depot) and nothing else:
  a buy is preceded by a deposit of its cost, a sale followed by a withdrawal
  of its proceeds (``Transaction.external_cash``). No balance is compared.
- **Unless the spending tracker is on** (``full_giro``, set from ``BrokerConfig.spending``): then the whole giro
  account is booked, so Wealthfolio's tracker sees card payments, direct debits and transfers. Settlements of
  trades become real transfers giro → depot (no ``external_cash``), every other booking a deposit, withdrawal,
  fee or interest on the cash account, and one opening balance (``giro-opening``: the account balance minus all
  booked movements, once, in the first run after a reset) keeps the cash account at the real balance.
  Switching the mode starts the broker over (``reset.py``), because the two models book the same trade
  differently.
- **Trades:** the depot transactions carry share count, price and market
  value, but neither fees nor taxes. Each buy/sell is paired with the giro
  booking that settled it (``Wertpapier``, ±``SETTLEMENT_DAYS``): its amount
  is the trade's cash, the difference to the market value its costs. The API
  doesn't split costs of a sale into fee and tax, so both go into the fee.
  Depot transfers are ``UNKNOWN``; positions bought before the start date
  are added as opening positions on the page *Prüfung*.
- **Dividends** come as giro bookings (``Zinsen / Dividenden``) and are booked
  on the depot when the posting text names the security (ISIN, or a WKN of a
  position); otherwise they are reported. The booking's amount is the net
  payout; its tax (withholding plus German taxes) is taken from the posting
  text only when gross and every tax are named there and add up to that net
  amount (``dividend_tax``) - else the dividend stays net, without tax.

Only GET endpoints of ``banking`` and ``brokerage`` are called besides the login;
the order, quote and transfer endpoints are never used (AC 7 of #35).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Literal
from zoneinfo import ZoneInfo

import httpx

from .. import ratelimit
from ..model import CashBalance, Kind, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField, rate_limited

log = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

API = "https://api.comdirect.de"
TOKEN_URL = f"{API}/oauth/token"
# comdirect locks after 5 challenges without a correct TAN and after 3 wrong TANs.
MAX_OPEN_CHALLENGES = 3
MAX_WRONG_TANS = 1
# A scheduled run waits this long for the approval in the photoTAN app.
PUSH_WAIT = 180
PUSH_POLL = 3
# "Ich habe bestätigt" in the web UI waits at most this long for the status.
CONFIRM_WAIT = 30
# A trade's giro booking lies this many days around its business date.
SETTLEMENT_DAYS = 6
# A trade without its giro booking after this many days is reported.
# Below the sync's overlap (sync.OVERLAP, 7 days): a trade still without its giro booking is reported while
# it is still inside the fetch window - with 10 days it fell out of the window first and was never listed.
UNSETTLED_DAYS = SETTLEMENT_DAYS
PAGE_SIZE = 500
# Without a date filter comdirect returns only about half a year; older bookings
# come only when asked for explicitly. Without a start date: this far back.
HISTORY_YEARS = 10

ISIN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")
WKN = re.compile(r"\b([A-Z0-9]{6})\b")
INTEREST = re.compile(r"zins", re.IGNORECASE)
DIVIDEND = re.compile(r"dividend|ertrag|aussch[uü]ttung|ausschuettung", re.IGNORECASE)

# A dividend's posting text, e.g. "Brutto 20,00 USD Quellensteuer 15% 3,00 USD Devisenkurs EUR/USD 1,0850
# Kapitalertragsteuer 0,78 EUR": a label, at most 20 other characters (a rate like "15 %"), then the amount
# with two decimals and an optional currency. Each label may appear once - else nothing is taken.
_AMOUNT = (r"[^\d]{0,20}?(?:\d+(?:,\d+)?\s?%\s*)?"
           r"(?<![\d,.])(\d{1,3}(?:\.\d{3})*,\d{2})(?![\d%]|\s%)(?:\s?((?-i:[A-Z]{3}))\b)?")
_GROSS = re.compile(r"\bBrutto(?:betrag|dividende|ertrag)?\b" + _AMOUNT, re.IGNORECASE)
_TAXES = (
    re.compile(r"\b(?:Quellensteuer|QuSt)\b" + _AMOUNT, re.IGNORECASE),
    re.compile(r"\b(?:Kapitalertrags?steuer|KapESt|KapSt|KESt)\b" + _AMOUNT, re.IGNORECASE),
    re.compile(r"\b(?:Solidarit(?:ae|ä)tszuschlag|Solidarit(?:ae|ä)tszuschl\.|SolZ|Soli)" + _AMOUNT, re.IGNORECASE),
    re.compile(r"\b(?:Kirchensteuer|KiSt)\b" + _AMOUNT, re.IGNORECASE),
)
_FX = re.compile(r"\b(?:Devisenkurs|Umrechnungskurs)\s+(?:EUR\s?/\s?((?-i:[A-Z]{3}))\s+)?(\d+,\d+)\b", re.IGNORECASE)


def _dec(v) -> Decimal | None:
    if isinstance(v, dict):
        v = v.get("value")
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _unit(v) -> str:
    return (v or {}).get("unit") or "EUR" if isinstance(v, dict) else "EUR"


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _noon(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 12, tzinfo=BERLIN).astimezone(UTC)


def _type(t: dict) -> str:
    """The transactionType of a booking (an EnumText) as lowercase text."""
    tt = t.get("transactionType") or {}
    if isinstance(tt, str):
        return tt.lower()
    return f"{tt.get('key') or ''} {tt.get('text') or ''}".lower()


def _text(t: dict) -> str:
    """remittanceInfo without the line numbers comdirect puts in front of each 35-character line."""
    raw = t.get("remittanceInfo") or ""
    lines = [raw[i:i + 37] for i in range(0, len(raw), 37)] if re.match(r"^\d{2}", raw) else [raw]
    return " ".join(" ".join(re.sub(r"^\d{2}", "", line).split()) for line in lines).strip()


def _is_securities(t: dict) -> bool:
    return "wertpapier" in _type(t) or "securities" in _type(t)


FEE_TEXT = re.compile(r"entgelt|geb(?:ü|ue|u)hr|kontof(?:ü|ue|u)hrung", re.IGNORECASE)
OPENING_ID = "giro-opening"


def _party(b: dict, outgoing: bool) -> tuple[str, str]:
    """(name, IBAN) of the other side of a giro booking: the creditor of a payment, the remitter of a receipt."""
    for key in (("creditor", "deptor") if outgoing else ("remitter", "deptor")):
        p = b.get(key)
        if isinstance(p, dict) and (p.get("holderName") or p.get("iban")):
            return str(p.get("holderName") or ""), str(p.get("iban") or "").replace(" ", "").upper()
    return "", ""


def _is_income(t: dict) -> bool:
    kind = _type(t)
    return "zinsen" in kind or "dividend" in kind or "interest" in kind


def _account_id(t: dict) -> str:
    return f"acc-{t.get('reference') or ''}"


def _unknown(tx_id: str, when: datetime, raw_type: str, payload: dict, currency: str = "EUR") -> Transaction:
    return Transaction(id=tx_id, kind=Kind.UNKNOWN, datetime=when, currency=currency, net=Decimal(0),
                       label=raw_type, raw_type=raw_type, raw=payload)


def to_transactions(bookings: list[dict], depot_transactions: list[dict], wkn_to_isin: dict[str, str],
                    today: date | None = None, names: dict[str, str] | None = None, *,
                    full_giro: bool = False, opening_balance: Decimal | None = None) -> list[Transaction]:
    """Giro bookings and depot transactions (booked only) → transactions, oldest first.

    ``wkn_to_isin`` and ``names`` (ISIN → name) come from the depot positions and
    transactions: dividends name their security by ISIN or WKN in the posting text.
    ``full_giro``: the whole giro account is booked (see the module docstring); ``opening_balance``: the account
    balance, from which the opening deposit is derived (only then, only once).
    """
    names = names or {}
    today = today or datetime.now(BERLIN).date()
    bookings = [b for b in bookings if b.get("bookingStatus", "BOOKED") == "BOOKED" and b.get("reference")]
    used: set[str] = set()
    out: list[Transaction] = []

    # Trades: the depot transaction plus the giro booking that settled it.
    for d in depot_transactions:
        if d.get("bookingStatus", "BOOKED") != "BOOKED":
            continue
        kind_key = ((d.get("transactionType") or {}).get("key") if isinstance(d.get("transactionType"), dict)
                    else d.get("transactionType")) or ""
        instrument = d.get("instrument") or {}
        isin = instrument.get("isin") or ""
        wkn = instrument.get("wkn") or ""
        name = instrument.get("name") or instrument.get("shortName") or isin
        business = _day(d.get("businessDate") or d.get("bookingDate"))
        if business is None:
            continue
        tx_id = f"dep-{d.get('transactionId')}"
        shares = _dec(d.get("quantity"))
        gross = _dec(d.get("transactionValue"))
        if kind_key not in ("BUY", "SELL"):
            out.append(_unknown(tx_id, _noon(business), f"DEPOT_{kind_key or 'OTHER'}",
                                {"transactionType": kind_key, "isin": isin, "stück": str(shares or "")}))
            continue
        buy = kind_key == "BUY"
        cash = _settlement(bookings, used, buy, business, gross or Decimal(0), isin, wkn)
        if cash is None:
            if (today - business).days > UNSETTLED_DAYS:
                out.append(_unknown(tx_id, _noon(business), f"{kind_key}_OHNE_KONTOBUCHUNG",
                                    {"isin": isin, "stück": str(shares or ""), "kurswert": str(gross or "")}))
            else:
                log.info("comdirect: %s %s from %s has no giro booking yet", kind_key, isin, business)
            continue
        used.add(cash["reference"])
        net = abs(_dec(cash.get("amount")) or Decimal(0))
        costs = abs(net - gross) if gross is not None else Decimal(0)
        out.append(Transaction(
            id=tx_id, kind=Kind.BUY if buy else Kind.SELL, datetime=_noon(business), currency=_unit(cash.get("amount")),
            net=net, isin=isin or None, name=name, shares=abs(shares) if shares is not None else None,
            gross=gross, fee=costs, label="Kauf" if buy else "Verkauf", external_cash=not full_giro))

    # Giro bookings: only dividends are booked (the giro account itself isn't); a payout whose
    # security can't be told is reported.
    for b in bookings:
        if b["reference"] in used or not _is_income(b):
            continue
        value = _dec(b.get("amount")) or Decimal(0)
        text = _text(b)
        if value == 0 or (INTEREST.search(text) and not DIVIDEND.search(text)):
            continue
        if value < 0:  # a dividend reversed: the original stays booked, so the user must see it
            out.append(_unknown(f"acc-{b['reference']}", _noon(_day(b.get("bookingDate")) or today),
                                "DIVIDENDE_STORNO", {"betrag": str(value), "text": text}))
            continue
        currency = _unit(b.get("amount"))
        when = _noon(_day(b.get("bookingDate")) or _day(b.get("valutaDate")) or today)
        security = _security(text, wkn_to_isin)
        if security:
            tax = dividend_tax(text, value, currency)
            out.append(Transaction(id=_account_id(b), kind=Kind.DIVIDEND, datetime=when, currency=currency, net=value,
                                   isin=security, name=names.get(security, security),
                                   gross=value + tax if tax else None, tax=tax or Decimal(0), label="Dividende",
                                   text=text))
        else:
            out.append(_unknown(_account_id(b), when, "DIVIDENDE_OHNE_WERTPAPIER",
                                {"betrag": f"{value} {currency}", "buchungstext": text[:140]}, currency))
    if full_giro:
        out += _giro_movements(bookings, used, today, opening_balance)
    return sorted(out, key=lambda t: (t.datetime, t.id))


def _giro_movements(bookings: list[dict], used: set[str], today: date, opening_balance: Decimal | None
                    ) -> list[Transaction]:
    """The giro bookings the depot side did not take (``full_giro``): deposits, withdrawals, fees, interest, and
    the opening deposit that brings the cash account to the real balance."""
    out: list[Transaction] = []
    counted = Decimal(0)  # everything that moved the real balance in the window, booked here or not
    first: date | None = None
    for b in bookings:
        value = _dec(b.get("amount")) or Decimal(0)
        day = _day(b.get("bookingDate")) or _day(b.get("valutaDate")) or today
        counted += value
        first = day if first is None or day < first else first
        if b["reference"] in used or value == 0:
            continue
        text = _text(b)
        if _is_income(b) and DIVIDEND.search(text):
            continue  # a dividend: booked above (or reported)
        if _is_securities(b) and (today - day).days <= UNSETTLED_DAYS:
            continue  # its depot transaction may still come: booked as a transfer then, not twice
        currency = _unit(b.get("amount"))
        name, iban = _party(b, value < 0)
        kind_text = b.get("transactionType")
        label = str(kind_text.get("text") or "") if isinstance(kind_text, dict) else ""
        if value > 0:
            kind = Kind.INTEREST if _is_income(b) and INTEREST.search(text) else Kind.DEPOSIT
        else:
            kind = Kind.FEE if FEE_TEXT.search(f"{label} {text}") and not name else Kind.WITHDRAWAL
        out.append(Transaction(id=_account_id(b), kind=kind, datetime=_noon(day), currency=currency, net=abs(value),
                               signed=value, label=label, counterparty=name, counterparty_iban=iban, text=text))
    if opening_balance is not None and first is not None and opening_balance != counted:
        gap = opening_balance - counted
        out.append(Transaction(id=OPENING_ID, kind=Kind.DEPOSIT if gap > 0 else Kind.WITHDRAWAL,
                               datetime=_noon(first) - timedelta(hours=1), currency="EUR", net=abs(gap), signed=gap,
                               label="Anfangsbestand", text="Kontostand vor dem ersten gebuchten Umsatz"))
    return out


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(".", "").replace(",", "."))


def _once(pattern: re.Pattern[str], text: str) -> re.Match[str] | Literal[False] | None:
    """The only match, None without one, False when there are several (ambiguous)."""
    found = list(pattern.finditer(text))
    return found[0] if len(found) == 1 else (None if not found else False)


def dividend_tax(text: str, net: Decimal, currency: str) -> Decimal | None:
    """The tax of a dividend from its posting text: gross minus ``net``, when the text names the gross amount
    and every tax it lists (withholding, Kapitalertragsteuer, Soli, Kirchensteuer) adds up with ``net`` to it.

    Amounts in another currency need the text's exchange rate (EUR/XXX, the booking currency EUR); each
    converted amount may be off by a cent of rounding. Anything missing, repeated or not adding up: None -
    the tax is never guessed.
    """
    gross = _once(_GROSS, text)
    fx = _once(_FX, text)
    if not gross or fx is False:
        return None
    rate = _amount(fx.group(2)) if fx else None
    pair = fx.group(1).upper() if fx and fx.group(1) else None
    converted = 0

    def in_booking(m: re.Match[str]) -> Decimal | None:
        nonlocal converted
        value, unit = _amount(m.group(1)), (m.group(2) or currency).upper()
        if unit == currency:
            return value
        if currency != "EUR" or not rate or (pair and pair != unit):
            return None
        converted += 1
        return (value / rate).quantize(Decimal("0.01"))

    total = in_booking(gross)
    taxes = Decimal(0)
    for pattern in _TAXES:
        m = _once(pattern, text)
        if m is False:
            return None
        value = in_booking(m) if m else Decimal(0)
        if value is None:
            return None
        taxes += value
    if total is None or not taxes or abs(total - taxes - net) > Decimal("0.01") * converted:
        return None
    return total - net


def _settlement(bookings: list[dict], used: set[str], buy: bool, business: date, gross: Decimal, isin: str,
                wkn: str) -> dict | None:
    """The giro booking that settled a trade: securities type, right sign, near the business date,
    an amount that is the market value plus (buy) or minus (sale) costs - one naming the security first."""
    best = None
    for b in bookings:
        if b["reference"] in used or not _is_securities(b):
            continue
        value = _dec(b.get("amount"))
        day = _day(b.get("bookingDate")) or _day(b.get("valutaDate"))
        if value is None or day is None or (value < 0) != buy:
            continue
        if not -1 <= (day - business).days <= SETTLEMENT_DAYS:
            continue
        amount = abs(value)
        costs = amount - gross if buy else gross - amount
        if costs < Decimal("-0.01") or (gross and costs > gross / 2):
            continue
        text = b.get("remittanceInfo") or ""
        named = bool((isin and isin in text) or (wkn and wkn in text))
        score = (not named, abs((day - business).days), costs)
        if best is None or score < best[0]:
            best = (score, b)
    return best[1] if best else None


def _security(text: str, wkn_to_isin: dict[str, str]) -> str | None:
    m = ISIN.search(text)
    if m:
        return m.group(1)
    for m in WKN.finditer(text):
        if m.group(1) in wkn_to_isin:
            return wkn_to_isin[m.group(1)]
    return None


class ComdirectAdapter(BrokerAdapter):
    giro_with_spending = True
    key = "comdirect"
    label = "comdirect"
    credential_fields: ClassVar[list[CredentialField]] = [
        CredentialField("username", "Zugangsnummer", help="Die 8-stellige Zugangsnummer des comdirect-Bankings."),
        CredentialField("pin", "PIN", secret=True),
        CredentialField("client_id", "Client-ID",
                        help="Aus dem comdirect-Banking: Persönlicher Bereich → Zugänge verwalten → REST API."),
        CredentialField("client_secret", "Client-Secret", secret=True),
        CredentialField("iban", "IBAN des Girokontos", help="Leer lassen, wenn du nur ein Girokonto hast."),
    ]
    reports_positions = True
    own_tax_backfill = True

    # Replaced in tests with an httpx.MockTransport.
    transport: ClassVar[httpx.BaseTransport | None] = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._http: httpx.Client | None = None
        self._pending: dict | None = None
        self._accounts: list[dict] | None = None
        self._depots: list[dict] | None = None
        self._positions: list[dict] | None = None

    # ── HTTP ────────────────────────────────────────────────────────────────
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(transport=type(self).transport, timeout=60)
        return self._http

    def _request_info(self) -> str:
        sid = self.session.setdefault("session_id", secrets.token_hex(16))
        return json.dumps({"clientRequestId": {"sessionId": sid, "requestId": datetime.now().strftime("%H%M%S%f")[:9]}})

    def _headers(self, token: str, **extra: str) -> dict[str, str]:
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": f"Bearer {token}", "x-http-request-info": self._request_info(), **extra}

    def _token(self, data: dict) -> httpx.Response:
        creds = {"client_id": self.credentials.get("client_id", ""),
                 "client_secret": self.credentials.get("client_secret", "")}
        try:
            return self.http.post(TOKEN_URL, data={**creds, **data},
                                  headers={"Accept": "application/json",
                                           "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e

    def _keep(self, body: dict) -> None:
        now = time.time()
        self.session["access_token"] = body["access_token"]
        self.session["refresh_token"] = body.get("refresh_token", "")
        self.session["expires_at"] = now + int(body.get("expires_in") or 599)

    def get(self, path: str, **params) -> dict:
        self._ensure_token()
        headers = self._headers(self.session["access_token"])
        try:
            # Reads only (bookings, depot): token, session, validation and activation never come through here.
            r = ratelimit.with_backoff(lambda: self.http.get(f"{API}/api{path}", params=params, headers=headers))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        if ratelimit.limited(r):
            raise rate_limited(self.label, r)
        if r.status_code == 401:
            self._clear_tokens()
            raise AuthRequired(Challenge("confirm", "Die comdirect-Sitzung ist abgelaufen. Bitte neu anmelden."))
        if r.status_code >= 400:
            raise RequestRejected(r.status_code, f"comdirect: {_message(r)}")
        return r.json()

    def _dated_pages(self, path: str, first_day: date, **params) -> list[dict]:
        """Pages from ``first_day`` on (``min-bookingDate``); if comdirect rejects the date, without it."""
        try:
            return self._pages(path, **params, **{"min-bookingDate": first_day.isoformat()})
        except RequestRejected as e:
            if e.status not in (400, 422):
                raise
            log.warning("comdirect: %s rejects min-bookingDate %s (%s), asking without", path, first_day, e)
            return self._pages(path, **params)

    def _pages(self, path: str, **params) -> list[dict]:
        out: list[dict] = []
        while True:
            page = self.get(path, **params, **{"paging-first": len(out), "paging-count": PAGE_SIZE})
            values = page.get("values") or []
            out.extend(values)
            matches = (page.get("paging") or {}).get("matches")
            if not values or matches is None or len(out) >= int(matches):
                return out

    # ── login ───────────────────────────────────────────────────────────────
    def _clear_tokens(self) -> None:
        for k in ("access_token", "refresh_token", "expires_at"):
            self.session.pop(k, None)

    def _ensure_token(self) -> None:
        if not self.session.get("access_token"):
            raise AuthRequired(Challenge("confirm", "Bitte bei comdirect anmelden."))
        if self.session.get("expires_at", 0) - 30 > time.time():
            return
        if not self._refresh():
            raise AuthRequired(Challenge("confirm", "Die comdirect-Sitzung ist abgelaufen. Bitte neu anmelden."))

    def _refresh(self) -> bool:
        if not self.session.get("refresh_token"):
            return False
        r = self._token({"grant_type": "refresh_token", "refresh_token": self.session["refresh_token"]})
        if r.status_code != 200:
            self._clear_tokens()
            return False
        self._keep(r.json())
        return True

    def _guard(self) -> None:
        if self.session.get("pin_rejected"):
            raise AdapterError(
                "comdirect hat Zugangsnummer, PIN oder TAN abgelehnt. Bitte die Zugangsdaten in den "
                "Broker-Einstellungen neu speichern - bis dahin versucht der Dienst es nicht noch einmal, damit "
                "das comdirect-Banking nicht gesperrt wird.")
        if self.session.get("open_challenges", 0) >= MAX_OPEN_CHALLENGES:
            raise AdapterError(
                f"{self.session['open_challenges']} TAN-Anfragen an comdirect wurden nicht bestätigt. Nach fünf "
                "sperrt comdirect das Online-Banking, darum fragt der Dienst nicht weiter. Melde dich einmal mit "
                "TAN auf comdirect.de an und speichere danach die Zugangsdaten in den Broker-Einstellungen neu.")
        missing = [f.label for f in self.credential_fields if f.name != "iban" and not self.credentials.get(f.name)]
        if missing:
            raise AdapterError(f"Für comdirect fehlt: {', '.join(missing)} - bitte in den Broker-Einstellungen "
                               "eintragen.")

    def login(self) -> None:
        if self.session.get("pin_rejected"):
            self._guard()
        if self.session.get("access_token") and (self.session.get("expires_at", 0) - 30 > time.time()
                                                 or self._refresh()):
            return
        self._guard()
        # Only a push whose status can be polled may start from a scheduled run; an unknown type (credentials
        # saved anew) waits for the web UI too - never a challenge nobody answers (each counts towards five).
        if self.on_user_action and (self.session.get("tan_type") != "P_TAN_PUSH"
                                    or self.session.get("push_without_link")):
            # A TAN to type, or a push whose approval can't be checked: only the web UI can do
            # that - don't open a challenge nobody answers.
            raise AuthRequired(Challenge("code", "comdirect braucht eine TAN. Bitte in der Weboberfläche anmelden."))
        self._start()
        pending = self._require_pending()
        typ = pending["typ"]
        if typ == "P_TAN_PUSH" and self.on_user_action:
            if not pending.get("link"):
                self.session["push_without_link"] = True
                raise AuthRequired(Challenge("confirm", "Bitte die comdirect-Anmeldung in der Weboberfläche "
                                                        "bestätigen."))
            self.on_user_action("Bitte bestätige die Anmeldung von Wealthfolio Companion in der "
                                "comdirect photoTAN-App.")
            if not self._wait_push(PUSH_WAIT):
                raise AuthRequired(Challenge("confirm", "Die Anmeldung wurde nicht rechtzeitig in der photoTAN-App "
                                                        "bestätigt."))
            self._activate("")
            return
        raise AuthRequired(self._challenge())

    def _challenge(self, again: bool = False) -> Challenge:
        p = self._pending or {}
        prefix = "Noch nicht bestätigt. " if again else ""
        if p.get("typ") == "P_TAN_PUSH":
            return Challenge("confirm", prefix + "Bestätige die Anmeldung in der comdirect photoTAN-App.")
        if p.get("typ") == "P_TAN":
            return Challenge("code", "Scanne die photoTAN-Grafik mit der comdirect photoTAN-App und gib die TAN ein.",
                             image=f"data:image/png;base64,{p.get('challenge') or ''}")
        if p.get("typ") == "M_TAN":
            return Challenge("code", f"Gib die mobileTAN ein, die an {p.get('challenge') or 'dein Handy'} ging.")
        return Challenge("code", "Gib die TAN ein.")

    def _start(self) -> None:
        r = self._token({"grant_type": "password", "username": self.credentials["username"],
                         "password": self.credentials["pin"]})
        if r.status_code in (400, 401):
            self.session["pin_rejected"] = True
            raise AdapterError(f"comdirect hat die Anmeldung abgelehnt (Zugangsnummer, PIN oder Client-Daten): "
                               f"{_message(r)}")
        if r.status_code != 200:
            raise AdapterError(f"comdirect: {_message(r)}")
        token = r.json()["access_token"]
        try:
            r = self.http.get(f"{API}/api/session/clients/user/v1/sessions", headers=self._headers(token))
            sessions = r.json() if r.status_code == 200 else []
            if not sessions:
                raise AdapterError(f"comdirect: keine Sitzung ({_message(r)})")
            ident = sessions[0]["identifier"]
            body = {"identifier": ident, "sessionTanActive": True, "activated2FA": True}
            # Every challenge counts towards comdirect's limit - before the call, so a crash can't hide one.
            self.session["open_challenges"] = self.session.get("open_challenges", 0) + 1
            r = self.http.post(f"{API}/api/session/clients/user/v1/sessions/{ident}/validate", json=body,
                               headers=self._headers(token))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        info = r.headers.get("x-once-authentication-info")
        if r.status_code not in (200, 201) or not info:
            raise AdapterError(f"comdirect: TAN-Anforderung fehlgeschlagen ({_message(r)})")
        challenge = json.loads(info)
        self.session["tan_type"] = challenge.get("typ")
        link = (challenge.get("link") or {}).get("href") or ""
        self._pending = {"token": token, "session": body, "id": challenge.get("id"), "typ": challenge.get("typ"),
                         "challenge": challenge.get("challenge"), "link": link}

    def _wait_push(self, seconds: float) -> bool:
        """Poll the challenge's status link until the app approved it."""
        p = self._pending or {}
        if not p.get("link"):
            return True  # nothing to poll: the user says they approved
        # The bearer token goes along: only comdirect's own API, never a host the answer names (#107).
        if p["link"].startswith("/"):
            url = f"{API}{p['link']}"
        elif p["link"].startswith(f"{API}/"):
            url = p["link"]
        else:
            raise AdapterError("comdirect: unerwarteter Status-Link der TAN-Anforderung - abgebrochen.")
        deadline = time.monotonic() + seconds
        while True:
            try:
                r = self.http.get(url, headers=self._headers(p["token"]))
                if r.status_code == 200 and (r.json() or {}).get("status") == "AUTHENTICATED":
                    return True
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(PUSH_POLL)

    def _require_pending(self) -> dict:
        """The challenge opened by ``_start``."""
        if self._pending is None:
            raise AdapterError("comdirect: keine offene TAN-Abfrage - bitte die Anmeldung neu starten.")
        return self._pending

    def _activate(self, code: str) -> None:
        p = self._require_pending()
        extra = {"x-once-authentication-info": json.dumps({"id": p["id"]})}
        if code:
            extra["x-once-authentication"] = code.strip()
        try:
            r = self.http.patch(f"{API}/api/session/clients/user/v1/sessions/{p['session']['identifier']}",
                                json=p["session"], headers=self._headers(p["token"], **extra))
        except httpx.HTTPError as e:
            raise AdapterError(f"comdirect nicht erreichbar: {e}") from e
        if r.status_code != 200:
            if code:
                self.session["wrong_tans"] = self.session.get("wrong_tans", 0) + 1
                if self.session["wrong_tans"] > MAX_WRONG_TANS:
                    self.session["pin_rejected"] = True
                    raise AdapterError("comdirect hat die TAN zweimal abgelehnt. Nach drei falschen TANs sperrt "
                                       "comdirect das Online-Banking - bitte zuerst einmal auf comdirect.de mit "
                                       "richtiger TAN anmelden und dann die Zugangsdaten neu speichern.")
                raise AuthRequired(Challenge("code", f"comdirect hat die TAN abgelehnt ({_message(r)}). Bitte "
                                                     "prüfen und erneut eingeben."))
            raise AuthRequired(self._challenge(again=True))
        r = self._token({"grant_type": "cd_secondary", "token": p["token"]})
        if r.status_code != 200:
            raise AdapterError(f"comdirect: Anmeldung nicht abgeschlossen ({_message(r)})")
        self._keep(r.json())
        self.session["open_challenges"] = 0
        self.session["wrong_tans"] = 0
        self._pending = None

    def complete_login(self, code: str) -> None:
        if self._pending is None:
            return
        if self._pending["typ"] == "P_TAN_PUSH":
            if not self._wait_push(CONFIRM_WAIT):
                raise AuthRequired(self._challenge(again=True))
            self._activate("")
        else:
            if not code.strip():
                raise AuthRequired(self._challenge())
            self._activate(code)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @classmethod
    def replay(cls, recording: dict) -> ComdirectAdapter:
        adapter = cls({"username": "replay", "pin": "replay", "client_id": "replay", "client_secret": "replay",
                       "iban": recording.get("iban", "")},
                      {"access_token": "replay", "expires_at": 4102444800})
        adapter._http = httpx.Client(transport=replay_transport(recording))
        adapter.today = date.fromisoformat(recording["today"]) if recording.get("today") else None
        return adapter

    # ── data ────────────────────────────────────────────────────────────────
    today: date | None = None

    def _giro(self) -> dict:
        if self._accounts is None:
            self._accounts = self._pages("/banking/clients/user/v2/accounts/balances")
        wanted = (self.credentials.get("iban") or "").replace(" ", "").upper()
        for b in self._accounts:
            account = b.get("account") or {}
            if wanted and (account.get("iban") or "").replace(" ", "").upper() == wanted:
                return b
        if wanted:
            raise AdapterError(f"Die IBAN {wanted} gehört nicht zu diesem comdirect-Zugang.")
        giro = [b for b in self._accounts if ((b.get("account") or {}).get("accountType") or {}).get("key") == "CA"]
        if not (giro or self._accounts):
            raise AdapterError("comdirect meldet kein Konto für diesen Zugang.")
        return (giro or self._accounts)[0]

    def _all_positions(self) -> list[dict]:
        if self._positions is None:
            if self._depots is None:
                self._depots = self._pages("/brokerage/clients/user/v3/depots")
            self._positions = []
            for d in self._depots:
                self._positions += self.get(f"/brokerage/v3/depots/{d['depotId']}/positions",
                                            **{"with-attr": "instrument"}).get("values") or []
        return self._positions

    def get_cash(self) -> list[CashBalance]:
        # The giro account isn't booked (only dividends land on the cash account): no balance to compare.
        return []

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._all_positions():
            instrument = p.get("instrument") or {}
            shares = _dec(p.get("quantity"))
            isin = instrument.get("isin") or p.get("wkn")
            if not isin or not shares:
                continue
            out.append(Position(isin, instrument.get("name") or "", shares, _unit(p.get("currentValue")),
                                cost=_dec(p.get("purchasePrice"))))
        return out

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = self.today or datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else None
        # A day more: a trade's settlement can lie before its business date. Without a start date
        # comdirect is asked explicitly for HISTORY_YEARS - unasked it returns only half a year.
        first_day = start - timedelta(days=1) if start else \
            today.replace(year=today.year - HISTORY_YEARS, day=min(today.day, 28))
        bookings = self._dated_pages(f"/banking/v1/accounts/{self._giro()['accountId']}/transactions", first_day,
                                     transactionState="BOOKED")
        if self._depots is None:
            self._depots = self._pages("/brokerage/clients/user/v3/depots")
        depot_txs: list[dict] = []
        for d in self._depots:
            depot_txs += self._dated_pages(f"/brokerage/v3/depots/{d['depotId']}/transactions", first_day,
                                           bookingStatus="BOOKED")
        wkn_to_isin: dict[str, str] = {}
        names: dict[str, str] = {}
        for p in self._all_positions():
            instrument = p.get("instrument") or {}
            wkn, isin = instrument.get("wkn") or p.get("wkn"), instrument.get("isin")
            if wkn and isin:
                wkn_to_isin[wkn] = isin
            if isin:
                names[isin] = instrument.get("name") or ""
        for d in depot_txs:
            i = d.get("instrument") or {}
            if i.get("wkn") and i.get("isin"):
                wkn_to_isin[i["wkn"]] = i["isin"]
            if i.get("isin") and i.get("name"):
                names[i["isin"]] = i["name"]
        # How far back comdirect delivers: only counts and dates, no amounts.
        log.info("comdirect: %d giro bookings (oldest %s), %d depot transactions (oldest %s), asked from %s",
                 len(bookings), min((str(b.get("bookingDate")) for b in bookings if b.get("bookingDate")), default="-"),
                 len(depot_txs), min((str(d.get("businessDate") or d.get("bookingDate")) for d in depot_txs
                                      if d.get("businessDate") or d.get("bookingDate")), default="-"),
                 start or first_day)
        opening = None
        if self.full_giro and not self.known_ids:  # the first run after a reset: derive the opening deposit
            balance = _dec((self._giro() or {}).get("balance"))
            opening = balance
            # Only what the run books counts: from the day before the start date, like the filter below.
            cut = start - timedelta(days=1) if start else None
            bookings = [b for b in bookings if cut is None or (_day(b.get("bookingDate")) or cut) >= cut]
        txs = to_transactions(bookings, depot_txs, wkn_to_isin, today, names, full_giro=self.full_giro,
                              opening_balance=opening)
        if start is None:
            return txs
        first = _noon(start) - timedelta(hours=12)
        return [t for t in txs if t.datetime >= first or t.id == OPENING_ID]


class RequestRejected(AdapterError):
    """comdirect answered a read call with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    if isinstance(body, dict):
        msgs = body.get("messages") or []
        text = "; ".join(m.get("message", "") for m in msgs if isinstance(m, dict)) or body.get("error_description") \
            or body.get("error") or body.get("code")
        if text:
            return f"{text} (HTTP {r.status_code})"
    return f"HTTP {r.status_code}"


def replay_transport(recording: dict) -> httpx.MockTransport:
    """Answers the read calls from a recording: balances, giro bookings, depots, positions, depot transactions."""
    def page(values: list) -> httpx.Response:
        return httpx.Response(200, json={"paging": {"index": 0, "matches": len(values)}, "values": values})

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/accounts/balances"):
            return page(recording.get("balances", []))
        if "/banking/" in path and path.endswith("/transactions"):
            return page(recording.get("bookings", []))
        if path.endswith("/v3/depots"):
            return page(recording.get("depots", []))
        if path.endswith("/positions"):
            return page(recording.get("positions", []))
        if "/brokerage/" in path and path.endswith("/transactions"):
            return page(recording.get("depot_transactions", []))
        return httpx.Response(404, json={"code": "not.found"})

    return httpx.MockTransport(handle)
