"""Any bank with a PSD2 account interface, through Enable Banking (waldonso2/wealthfolio-importer-addon#137).

PSD2 obliges banks to let licensed account information services read payment accounts. Enable
Banking is one; its free "restricted" mode reads only accounts the owner of an application links
himself - so every user registers an application of his own and enters its id and private key
(kept in the vault). The service runs nothing for others.

Login: the bank chosen by name (as Enable Banking lists it), ``POST /auth`` gives the bank's page;
the user signs in there and is sent to the application's redirect URL with a ``code``. The page
there need not load: the user pastes its address, ``POST /sessions`` turns the code into a session
that reads for as long as the bank allows (``access.valid_until``, mostly 90 or 180 days). Each
request carries a JWT the application's key signs (RS256, ``kid`` = application id).

Payment accounts only - no depot. Bookings go through ``fints.to_transactions`` (same kinds, patterns
and id scheme as FinTS): pending ones are skipped, giro bookings of depot trades are
``SECURITIES_CASH``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .. import ratelimit
from ..model import CashBalance, Position, Transaction
from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField, rate_limited
from .fints import BERLIN, NO_TAN_DAYS, to_transactions

log = logging.getLogger(__name__)

API = "https://api.enablebanking.com"
SLOTS = 3
# Days before the end of the bank's consent on which a run reminds the user to sign in again.
REMIND_DAYS = (7, 1)
# Asked for when the bank names no maximum (PSD2 allows 180 days, many banks 90).
DEFAULT_CONSENT_DAYS = 90
BALANCE_ORDER = ("CLBD", "ITBD", "XPCD", "CLAV", "ITAV", "OPBD")
# At most this many pages of transactions: a bank whose continuation key never ends stops the fetch.
MAX_PAGES = 1000
Amount = namedtuple("Amount", "amount currency")

FIELDS: list[CredentialField] = [
    CredentialField("bank", "Bank", help="Der Name deiner Bank, wie Enable Banking ihn führt, z. B. N26."),
    CredentialField("country", "Land", help="Leer lassen für Deutschland (DE); sonst der Ländercode, z. B. AT."),
    CredentialField("app_id", "Application-ID",
                    help="Aus dem Kontrollzentrum von Enable Banking: deine eigene App (eingeschränkter Modus, "
                         "nur deine Konten). Als Redirect-URL dort die Adresse dieses Dienstes eintragen."),
    CredentialField("private_key", "Privater Schlüssel der App", secret=True,
                    help="Der Inhalt der .pem-Datei, die Enable Banking beim Anlegen der App erzeugt hat."),
    CredentialField("iban", "IBAN des Girokontos", help="Leer lassen, wenn die Bank nur ein Konto freigibt."),
]


def _pem(text: str) -> bytes:
    """The key as typed into a one-line form field: line breaks are gone, so the base64 is wrapped anew."""
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)
    if not m:
        raise AdapterError("Der private Schlüssel ist kein PEM-Schlüssel (-----BEGIN PRIVATE KEY----- …).")
    body = "".join(m.group(2).split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {m.group(1)}-----\n{lines}\n-----END {m.group(1)}-----\n".encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(app_id: str, key_pem: str, now: float | None = None) -> str:
    """The bearer token of one request: RS256, valid an hour (Enable Banking's own example)."""
    try:
        key = serialization.load_pem_private_key(_pem(key_pem), password=None)
    except ValueError as e:
        raise AdapterError("Der private Schlüssel lässt sich nicht lesen - bitte die .pem-Datei neu einfügen.") from e
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AdapterError("Der private Schlüssel ist kein RSA-Schlüssel.")
    iat = int(now if now is not None else time.time())
    head = _b64(json.dumps({"typ": "JWT", "alg": "RS256", "kid": app_id}).encode())
    body = _b64(json.dumps({"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": iat,
                            "exp": iat + 3600}).encode())
    signature = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(signature)}"


def _day(value) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _record(t: dict) -> dict:
    """An Enable Banking transaction as the MT940/camt record ``fints.to_transactions`` reads."""
    amount = Decimal(str((t.get("transaction_amount") or {}).get("amount") or "0"))
    if t.get("credit_debit_indicator") == "DBIT" and amount > 0:
        amount = -amount
    party, account = ("creditor", "creditor_account") if amount < 0 else ("debtor", "debtor_account")
    info = t.get("remittance_information") or []
    booked = _day(t.get("booking_date")) or _day(t.get("transaction_date")) or _day(t.get("value_date"))
    return {
        "amount": Amount(amount, (t.get("transaction_amount") or {}).get("currency") or "EUR"),
        "entry_date": booked,
        "date": _day(t.get("value_date")) or booked,
        "posting_text": (t.get("bank_transaction_code") or {}).get("description") or "",
        "purpose": " ".join(info) if isinstance(info, list) else str(info),
        "applicant_name": (t.get(party) or {}).get("name") or "",
        "applicant_iban": (t.get(account) or {}).get("iban") or "",
        "end_to_end_reference": t.get("entry_reference") or "",
    }


def _code(pasted: str) -> tuple[str, str]:
    """The ``code`` (and ``state``) from the pasted address, or the bare code."""
    text = pasted.strip()
    query = parse_qs(urlparse(text).query) if "?" in text else {}
    if "error" in query:
        raise AdapterError(f"Die Bank hat die Anmeldung nicht freigegeben ({query['error'][0]}).")
    if query:
        return (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    return text, ""


class EnableBankingAdapter(BrokerAdapter):
    credential_fields = FIELDS

    # Replaced in tests with an httpx.MockTransport.
    transport: ClassVar[httpx.BaseTransport | None] = None

    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._http: httpx.Client | None = None
        self._bearer = ""  # set by replay(): recordings need no key
        self.today: date | None = None

    # ── HTTP ────────────────────────────────────────────────────────────────
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(transport=type(self).transport, base_url=API, timeout=60)
        return self._http

    def _call(self, method: str, path: str, **kwargs) -> dict:
        app_id, key = self.credentials.get("app_id", "").strip(), self.credentials.get("private_key", "")
        if not self._bearer and (not app_id or not key):
            raise AdapterError("Application-ID und privater Schlüssel fehlen - bitte in den Broker-Einstellungen "
                               "eintragen.")
        token = self._bearer or jwt(app_id, key)

        def send() -> httpx.Response:
            return self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

        try:
            # Only reads are tried again: POST /auth and POST /sessions start or finish a login at the bank.
            r = ratelimit.with_backoff(send) if method == "GET" else send()
        except httpx.HTTPError as e:
            raise AdapterError(f"Enable Banking antwortet nicht ({type(e).__name__}).") from e
        if ratelimit.limited(r):
            raise rate_limited(self.label, r)
        if r.status_code in (401, 403) and path.startswith("/accounts/"):
            self.session.pop("session_id", None)  # consent withdrawn or ended: the next run asks for a login
            raise AdapterError("Die Bank hat den Zugriff beendet - bitte neu anmelden.")
        if r.status_code >= 400:
            raise AdapterError(f"Enable Banking: {_message(r)}")
        return r.json()

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # ── login ───────────────────────────────────────────────────────────────
    def _days_left(self) -> int | None:
        until = self.session.get("valid_until")
        if not self.session.get("session_id") or not until:
            return None
        return (datetime.fromisoformat(until.replace("Z", "+00:00")) - datetime.now(UTC)).days

    def login(self) -> None:
        days = self._days_left()
        if days is not None and days >= 0:
            if days in REMIND_DAYS and self.on_user_action:
                self.on_user_action(f"Die Freigabe für {self.label} endet in {days} Tag(en). Melde dich in der "
                                    "Oberfläche neu an, damit der Abruf weiterläuft.")
            return
        if self.on_user_action is not None:  # a run of the sync: only the UI starts a login at the bank
            raise AuthRequired(Challenge("url", "Die Freigabe der Bank fehlt oder ist abgelaufen - bitte in der "
                                                "Oberfläche neu anmelden."))
        raise AuthRequired(self._start())

    def _bank(self) -> dict:
        name = self.credentials.get("bank", "").strip()
        country = (self.credentials.get("country") or "DE").strip().upper()
        banks = self._call("GET", "/aspsps", params={"country": country}).get("aspsps", [])
        found = next((b for b in banks if b.get("name", "").casefold() == name.casefold()), None)
        if found is None:
            close = [b["name"] for b in banks if name and name.casefold() in b.get("name", "").casefold()][:5]
            raise AdapterError(f"Enable Banking kennt keine Bank „{name}“ in {country}."
                               + (f" Meintest du: {', '.join(close)}?" if close else ""))
        return found

    def _start(self) -> Challenge:
        bank = self._bank()
        redirect = (self._call("GET", "/application").get("redirect_urls") or [""])[0]
        if not redirect:
            raise AdapterError("Deine App bei Enable Banking hat keine Redirect-URL - bitte dort eintragen.")
        seconds = bank.get("maximum_consent_validity") or DEFAULT_CONSENT_DAYS * 86400
        until = datetime.now(UTC) + timedelta(seconds=int(seconds)) - timedelta(minutes=5)
        state = secrets.token_urlsafe(24)
        answer = self._call("POST", "/auth", json={
            "access": {"valid_until": until.isoformat()},
            "aspsp": {"name": bank["name"], "country": bank.get("country") or "DE"},
            "state": state, "redirect_url": redirect, "psu_type": "personal",
        })
        self.session = {"state": state}
        return Challenge("url", f"Öffne den Link und melde dich bei {bank['name']} an. Danach landest du auf "
                                f"{redirect} - die Seite muss nicht laden. Kopiere ihre Adresse aus der "
                                "Adresszeile hierher.", url=answer["url"])

    def complete_login(self, code: str) -> None:
        value, state = _code(code)
        if not value:
            raise AuthRequired(Challenge("url", "In der Adresse fehlt der Code - bitte die ganze Adresse einfügen."))
        if state and state != self.session.get("state"):
            raise AdapterError("Die Adresse gehört zu einer anderen Anmeldung - bitte neu starten.")
        answer = self._call("POST", "/sessions", json={"code": value})
        accounts = [{"uid": a.get("uid"), "iban": ((a.get("account_id") or {}).get("iban") or "").upper(),
                     "type": a.get("cash_account_type") or ""} for a in answer.get("accounts", [])]
        if not accounts:
            raise AdapterError("Die Bank hat kein Konto freigegeben - bitte neu anmelden und ein Konto auswählen.")
        self.session = {"session_id": answer["session_id"], "accounts": accounts,
                        "valid_until": (answer.get("access") or {}).get("valid_until") or ""}

    # ── data ────────────────────────────────────────────────────────────────
    def _account(self) -> str:
        accounts = self.session.get("accounts") or []
        iban = (self.credentials.get("iban") or "").replace(" ", "").upper()
        chosen = (next((a for a in accounts if a["iban"] == iban), None) if iban
                  else next((a for a in accounts if a["type"] == "CACC"), accounts[0] if accounts else None))
        if not chosen:
            raise AdapterError("Das gewählte Konto ist nicht freigegeben - bitte neu anmelden und es auswählen.")
        return chosen["uid"]

    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        today = self.today or datetime.now(BERLIN).date()
        start = since.astimezone(BERLIN).date() if since else today - timedelta(days=NO_TAN_DAYS)
        self.history_start = start
        uid, key = self._account(), None
        raw: list[dict] = []
        for _ in range(MAX_PAGES):  # a continuation key that never ends would else loop for ever
            params = {"date_from": start.isoformat(), **({"continuation_key": key} if key else {})}
            page = self._call("GET", f"/accounts/{uid}/transactions", params=params)
            raw += page.get("transactions") or []
            last, key = key, page.get("continuation_key")
            if not key:
                break
            if key == last:
                raise AdapterError(f"{self.label}: die Bank liefert dieselbe Seite noch einmal - Abruf abgebrochen.")
        else:
            raise AdapterError(f"{self.label}: die Bank liefert immer weitere Seiten - Abruf abgebrochen.")
        booked = [t for t in raw if (t.get("status") or "BOOK").upper() not in ("PDNG", "PENDING")]
        log.info("%s (PSD2): %d Umsätze seit %s (%d vorgemerkte übersprungen)", self.label, len(booked),
                 start.isoformat(), len(raw) - len(booked))
        return to_transactions([_record(t) for t in booked])

    def get_cash(self) -> list[CashBalance]:
        balances = self._call("GET", f"/accounts/{self._account()}/balances").get("balances") or []
        rank = {t: i for i, t in enumerate(BALANCE_ORDER)}
        best = min(balances, key=lambda b: rank.get(b.get("balance_type"), len(rank)), default=None)
        if best is None:
            return []
        amount = best.get("balance_amount") or {}
        if amount.get("amount") is None:
            return []
        return [CashBalance(amount.get("currency") or "EUR", Decimal(str(amount["amount"])))]

    def get_positions(self) -> list[Position]:
        return []

    @classmethod
    def replay(cls, recording: dict) -> EnableBankingAdapter:
        adapter = cls({"bank": "Replay"}, {"session_id": "replay", "valid_until": "2099-01-01T00:00:00+00:00",
                                           "accounts": [{"uid": "acc", "iban": "", "type": "CACC"}]})
        adapter._bearer = "replay"
        adapter._http = httpx.Client(transport=replay_transport(recording), base_url=API)
        adapter.today = date.fromisoformat(recording["today"])
        return adapter


def _message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    return f"{body.get('message') or body.get('error') or 'Fehler'} (HTTP {r.status_code})"


def replay_transport(recording: dict) -> httpx.MockTransport:
    """Answers from a recording: ``pages`` of transactions (continuation keys ``1``, ``2`` …) and ``balances``."""
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            pages = recording["pages"]
            n = int(request.url.params.get("continuation_key") or 0)
            body = {"transactions": pages[n], "continuation_key": str(n + 1) if n + 1 < len(pages) else None}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/balances"):
            return httpx.Response(200, json={"balances": recording.get("balances", [])})
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handle)


def _slot(n: int) -> type[BrokerAdapter]:
    label = f"Bank über PSD2 {n}"
    return type(f"EnableBanking{n}Adapter", (EnableBankingAdapter,),
                {"key": f"psd2-{n}", "label": label, "default_label": label, "slot_kind": "psd2",
                 "__module__": __name__})


# Fixed slots like the FinTS banks: a set-up bank's key (config, vault, state, [SYNC …]) never changes.
BANKS = [_slot(n) for n in range(1, SLOTS + 1)]
