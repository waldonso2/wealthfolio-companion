"""Checks for what the forms of the web UI accept, on the server (the HTML ``pattern``s are only a help).

Each returns the cleaned value or raises ``Invalid`` with a German text for the page (``str(e)``), or in the
page's language with ``e.text(lang)`` (#87; a field's ``label`` is a UI text key, ``N_``). Free text is capped
at ``MAX_TEXT`` characters: everything ends up in the encrypted config, the vault or a log line.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from ..assets import valid_isin
from ..history import BERLIN
from .i18n import N_, translate

MAX_TEXT = 500
ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
MIC = re.compile(r"^[A-Z0-9]{4}$")
# A Wealthfolio symbol: ticker, ISIN or a provider form like "SAP.DE", "BRK-B", "^GDAXI".
SYMBOL = re.compile(r"^[A-Za-z0-9.^:_-]{1,32}$")


class Invalid(ValueError):
    """A UI text key (``web/i18n.py``) and its params; ``str(e)`` is the German text."""

    def __init__(self, key: str, **params):
        self.key, self.params = key, params
        super().__init__(translate(key, "de", **params))

    def text(self, lang: str) -> str:
        """The message in ``lang``, the field's label (a key itself) translated too."""
        params = {k: translate(v, lang) if k == "label" else v for k, v in self.params.items()}
        return translate(self.key, lang, **params)


def text(value: str, label: str, *, max_len: int = MAX_TEXT) -> str:
    value = (value or "").strip()
    if len(value) > max_len:
        raise Invalid("{label}: höchstens {max_len} Zeichen.", label=label, max_len=max_len)
    if any(ord(c) < 32 for c in value):
        raise Invalid("{label}: enthält Steuerzeichen.", label=label)
    return value


def choice(value: str, allowed: tuple[str, ...], label: str) -> str:
    """One of ``allowed``, exactly."""
    if value not in allowed:
        raise Invalid("{label}: unbekannte Auswahl.", label=label)
    return value


def http_url(value: str, label: str, *, required: bool = True) -> str:
    """An http(s) address with a host, without user info, query or fragment; trailing slash removed."""
    value = text(value, label).rstrip("/")
    if not value and not required:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Invalid("{label}: bitte eine Adresse mit http:// oder https:// angeben.", label=label)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Invalid("{label}: ohne Zugangsdaten, ? oder # in der Adresse.", label=label)
    try:
        parsed.port  # noqa: B018 - raises for an invalid port
    except ValueError as e:
        raise Invalid("{label}: ungültiger Port.", label=label) from e
    return value


def https_url(value: str, label: str) -> str:
    """An https address with a host, without user info or fragment, kept as typed (a FinTS server's path matters)."""
    value = text(value, label)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise Invalid("{label}: bitte eine Adresse mit https:// angeben.", label=label)
    if parsed.username or parsed.password or parsed.fragment:
        raise Invalid("{label}: ohne Zugangsdaten oder # in der Adresse.", label=label)
    try:
        parsed.port  # noqa: B018 - raises for an invalid port
    except ValueError as e:
        raise Invalid("{label}: ungültiger Port.", label=label) from e
    return value


def blz(value: str, label: str) -> str:
    """A German bank code: 8 digits, spaces allowed."""
    value = text(value, label).replace(" ", "")
    if not re.fullmatch(r"\d{8}", value):
        raise Invalid("{label}: bitte 8 Ziffern angeben.", label=label)
    return value


def today() -> date:
    """Today in German time: the container's clock is UTC, the start date is midnight German time (sync.BERLIN)."""
    return datetime.now(BERLIN).date()


def iso_date(value: str, label: str) -> str:
    value = text(value, label)
    if not value:
        return ""
    try:
        day = date.fromisoformat(value)
    except ValueError as e:
        raise Invalid("{label}: bitte als Datum angeben (JJJJ-MM-TT).", label=label) from e
    if day > today():
        raise Invalid("{label}: darf nicht in der Zukunft liegen.", label=label)
    return day.isoformat()


def decimal(value: str, label: str, *, minimum: Decimal | None = None, maximum: Decimal = Decimal("1e12")) -> Decimal:
    """A number as typed (comma or point), finite and within bounds - ``Decimal`` alone takes ``Infinity``,
    ``NaN`` and ``1e999999``."""
    try:
        number = Decimal(text(value, label, max_len=40).replace(",", "."))
    except InvalidOperation as e:
        raise Invalid("{label}: bitte als Zahl angeben.", label=label) from e
    if not number.is_finite() or abs(number) > maximum or (minimum is not None and number < minimum):
        raise Invalid("{label}: bitte als Zahl angeben.", label=label)
    return number


def isin(value: str) -> str:
    value = text(value, N_("ISIN")).replace(" ", "").upper()
    if not ISIN.match(value) or not valid_isin(value):
        raise Invalid(N_("Keine gültige ISIN (12 Zeichen, Prüfziffer)."))
    return value


SECURITY_ID = re.compile(r"^[A-Z0-9][A-Z0-9|/._-]{1,39}$")


def security_id(value: str) -> str:
    """The id a broker reports a position under: an ISIN as a rule, but not always one (crypto, older parsers)."""
    value = text(value, N_("Wertpapier")).upper()
    if not SECURITY_ID.match(value):
        raise Invalid(N_("Unbekanntes Wertpapier-Kennzeichen."))
    return value


def iban(value: str) -> str:
    """Empty, or an IBAN with a valid check sum (ISO 13616, mod 97)."""
    value = text(value, N_("IBAN")).replace(" ", "").upper()
    if not value:
        return ""
    if not IBAN.match(value) or int("".join(str(int(c, 36)) for c in value[4:] + value[:4])) % 97 != 1:
        raise Invalid(N_("Keine gültige IBAN (Prüfsumme stimmt nicht)."))
    return value


def mic(value: str) -> str | None:
    value = text(value, N_("Börse")).upper()
    if not value:
        return None
    if not MIC.match(value):
        raise Invalid(N_("Börse: ein MIC aus 4 Zeichen, z. B. XETR."))
    return value


def symbol(value: str) -> str:
    value = text(value, N_("Symbol"))
    if not SYMBOL.match(value):
        raise Invalid(N_("Symbol: 1-32 Zeichen, nur Buchstaben, Ziffern und . ^ : _ -"))
    return value
