"""The interface every broker adapter implements.

Adapters only read: there is no method that places orders, moves money or
changes anything at the broker, and an adapter must not call such endpoints
(AC 7 of #35).

Login is a two-step dance because brokers ask for a second factor:

1. ``login()`` uses the stored session if it is still valid. If the broker
   needs the user (TAN, SMS code, app confirmation), it raises
   ``AuthRequired`` with a ``Challenge`` that the web UI shows.
2. ``complete_login(code)`` finishes the login with what the user entered
   (an empty string for "I confirmed it in the app").

After a successful login ``session_state()`` returns what is needed to log in
again without the user (cookies, tokens); the sync stores it encrypted and
passes it to the next adapter instance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx

from .. import ratelimit
from ..model import CashBalance, Position, Transaction


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = False
    help: str = ""
    # May stay empty: the login then asks for it each time and it is never stored (#85).
    ask: bool = False


@dataclass(frozen=True)
class Challenge:
    # "code": the user types a code; "confirm": the user confirms in an app
    # or, when ``url`` is set, on that page in the browser; "pin": the user types
    # a PIN that isn't stored (#85).
    kind: str
    message: str
    url: str = ""
    # A picture to show with it (a data: URL), e.g. comdirect's photoTAN graphic.
    image: str = ""


# What stays of a session when the safe mode ends it after a run (#85): the lock-out guards, so a
# rejected PIN or unanswered TAN challenges are never retried blindly.
SAFETY_KEYS = ("pin_rejected", "open_challenges", "wrong_tans")


def safety_state(session: dict | None) -> dict:
    return {k: v for k, v in (session or {}).items() if k in SAFETY_KEYS}


class AuthRequired(Exception):
    """The broker needs the user: show ``challenge`` and call ``complete_login``."""

    def __init__(self, challenge: Challenge):
        super().__init__(challenge.message)
        self.challenge = challenge


class AdapterError(Exception):
    """The broker could not be read (down, changed API, wrong credentials)."""


class RateLimited(AdapterError):
    """The broker limits the requests (HTTP 429/503) even after ``ratelimit.with_backoff``: this broker's run
    stops, nothing of it is marked synced, and the next scheduled run waits until ``until`` (#149)."""

    def __init__(self, message: str, until: datetime):
        super().__init__(message)
        self.until = until


def rate_limited(label: str, response: httpx.Response | None = None, now: datetime | None = None) -> RateLimited:
    """The ``RateLimited`` for an answer that is still 429/503 (or a throttle without one, e.g. a websocket's):
    when to try again from its Retry-After, else after ``ratelimit.NEXT_TRY``."""
    seconds = ratelimit.NEXT_TRY if response is None else ratelimit.retry_after(
        response, ratelimit.NEXT_TRY, ratelimit.MAX_NEXT, now)
    until = (now or datetime.now(UTC)) + timedelta(seconds=seconds)
    shown = until.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    code = "" if response is None else f" (HTTP {response.status_code})"
    return RateLimited(f"{label} begrenzt gerade die Abrufe{code} - nächster Versuch ab {shown}.", until)


class BrokerAdapter(ABC):
    key: ClassVar[str]
    label: ClassVar[str]
    credential_fields: ClassVar[list[CredentialField]] = []
    # True if get_positions() is the complete list of holdings: then the sync
    # compares it with Wealthfolio (a missing position counts as 0 shares).
    reports_positions: ClassVar[bool] = False
    # True if reading the whole history is cheap: then every run fetches all of
    # it (from the start date) and the comparison with Wealthfolio also finds
    # synced activities the broker no longer lists (brokersync.coverage).
    full_history: ClassVar[bool] = False
    # True if the adapter books only a depot by default but can book the whole giro account too: the sync passes
    # ``BrokerConfig.spending`` on as ``full_giro`` (comdirect). Switching it starts the broker over (reset.py).
    giro_with_spending: ClassVar[bool] = False
    # True if a later version of the adapter reads the tax of dividends it booked without one before
    # (comdirect, wealthfolio-importer-addon#52): once, the sync reads from the start date again and puts
    # the tax on its own dividends (``Syncer._backfill_own_tax``).
    own_tax_backfill: ClassVar[bool] = False
    # A file the user exports from the broker and uploads (broker settings), for what the interface
    # doesn't deliver (the Deutsche Bank's depot transactions): what to call it, empty if there is none.
    statement_label: ClassVar[str] = ""
    # A bank account besides the cash account that can be booked into a Wealthfolio account of its own
    # (the bank's Tagesgeld): what to call it, empty if the adapter can't read one.
    savings_label: ClassVar[str] = ""
    # What the user must have read before the first login (#112, an unofficial interface): German, shown with a
    # checkbox in the broker settings; the answer is ``BrokerConfig.consented``. Empty: nothing to agree to.
    consent: ClassVar[str] = ""

    def __init__(self, credentials: dict[str, str], session: dict | None = None):
        self.credentials = credentials
        self.session = dict(session or {})
        # The session as it was last read or written: the vault keeps another process's lock-out counters
        # when the stored one changed meanwhile (``Vault.store_session``).
        self.stored_session = dict(session or {})
        # Set by the web UI while a login waits for the user: its challenge and since when (monotonic).
        self.ui_challenge: Challenge | None = None
        self.ui_since = 0.0
        # Set by the sync: tells the user to act now (e.g. confirm in the bank's
        # app) while the adapter waits. Unset in the web UI, which shows the
        # challenge itself.
        self.on_user_action: Callable[[str], object] | None = None
        # Set by the sync: ids it already has, so an adapter can skip expensive
        # per-transaction queries for them (Scalable's details).
        self.known_ids: set[str] = set()
        # Set by an adapter that couldn't fetch everything this run (Scalable's rate limit):
        # one-off backfills wait for a complete run.
        self.incomplete = False
        # Set by an adapter that reads a bank account's history: the first day it read this run.
        self.history_start: date | None = None
        # Set by the sync: the earliest day of that history read in an earlier run (``meta history-start``).
        self.history_read: date | None = None
        # Set by the sync when the user assigned a Wealthfolio account to the savings account
        # (``savings_label``): read it too, from ``savings_since`` (None: as far back as without a TAN).
        self.with_savings = False
        self.savings_since: datetime | None = None
        # Set by the sync from ``BrokerConfig.spending`` for an adapter with ``giro_with_spending``: book the whole
        # giro account (for Wealthfolio's spending tracker) instead of only the depot.
        self.full_giro = False

    @staticmethod
    def parse_statement(data: bytes) -> list[dict]:
        """The rows of an uploaded statement, without account numbers; ``ValueError`` if it isn't one."""
        raise NotImplementedError

    @staticmethod
    def statement_transactions(rows: list[dict], history_start: date | None) -> list[Transaction]:
        """Transactions from stored statement rows; ``history_start``: first day of the bank account's history."""
        raise NotImplementedError

    def close(self) -> None:  # noqa: B027 - optional hook, most adapters hold no connection
        """End the connection; afterwards ``session_state()`` is final."""

    @abstractmethod
    def login(self) -> None:
        """Log in with the stored session or credentials; raise ``AuthRequired`` if the user is needed."""

    def complete_login(self, code: str) -> None:
        """Finish a login that raised ``AuthRequired``."""
        raise AdapterError(f"{self.label} has no second login step")

    def session_state(self) -> dict:
        """What the next run needs to log in without the user. Stored encrypted."""
        return self.session

    @classmethod
    def replay(cls, recording: dict) -> BrokerAdapter:
        """An instance that answers from a recording instead of the broker (contract tests).

        Adapters talking HTTP build it on a transport that replays the recorded
        responses; see tests/contract/README.md.
        """
        raise NotImplementedError(f"{cls.__name__} has no replay support")

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_transactions(self, since: datetime | None) -> list[Transaction]:
        """All transactions booked at or after ``since`` (everything if ``None``)."""

    @abstractmethod
    def get_cash(self) -> list[CashBalance]: ...
