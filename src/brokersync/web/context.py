"""What every page of the UI shares (#76): the data, the syncer, the templates and the helpers around them.

``app._create_app`` builds one ``AppContext`` per built app and keeps it on ``app.state.ctx``; the routes
(``web/routes/``) get it through the dependency ``Ctx``.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import __version__, appearance, lookthrough, redact
from .. import config as config_mod
from ..adapters import BrokerAdapter
from ..mapping import Accounts
from ..notify import Notifier
from ..state import State
from ..sync import Syncer
from ..vault import Vault, session_tag, verify_password
from ..wealthfolio import WealthfolioClient, WealthfolioError, connect
from .gate import Gate, Throttle
from .i18n import N_, translate

# Pages anyone can open, without a login and without a CSRF token. Not /setup-password: its form carries a
# CSRF token and the one-time setup code (#89) - else any web page could set the first password.
PUBLIC = {"/login", "/healthz", "/ca.crt"}
# Sessions of the UI (#96): signed out after this long without a request, and after this long at all; an action
# that changes credentials, deletes bookings or the fetch mode asks for the password again after REAUTH_SECONDS.
IDLE_SECONDS = 30 * 60
MAX_SECONDS = 12 * 3600
REAUTH_SECONDS = 15 * 60
ACCOUNTS_SECONDS = 60
# Keys of UI texts (#87), translated where they are shown (``_(label)`` in the templates).
STATUS_LABEL = {"ok": N_("OK"), "needs_auth": N_("Anmeldung nötig"), "error": N_("Fehler"), "running": N_("läuft"),
                "aborted": N_("abgebrochen"), "limited": N_("Abruflimit")}

# The pages under *Einstellungen*: (path, title, what it is for, group, icon of ``_icons.html``), grouped like
# Wealthfolio's settings (a sidebar on wide screens, the list of /settings on phones). The header shows the
# daily pages only (#71).
SETTINGS_PAGES = [
    ("/appearance", N_("Darstellung"), N_("Schrift, Theme und Sprache"), N_("Allgemein"), "monitor"),
    ("/notifications", N_("Benachrichtigungen"), N_("ntfy für TAN-Anfragen und Fehler"), N_("Allgemein"), "bell"),
    ("/updates", N_("Updates"), N_("Neue Versionen automatisch installieren"), N_("Allgemein"), "download"),
    ("/setup/wealthfolio", N_("Wealthfolio"), N_("Adresse und Passwort deines Wealthfolio"), N_("Verbindung"),
     "link"),
    ("/brokers", N_("Broker"), N_("Zugangsdaten, Konten, Startdatum, Anmeldung"), N_("Verbindung"), "bank"),
    # Only while patterns are saved (waldonso2/wealthfolio-importer-addon#84): each one stays visible and deletable.
    ("/transfers", N_("Überträge"), N_("Auszahlungen auf eigene Konten erkennen"), N_("Verbindung"), "transfer"),
    ("/security", N_("Sicherheit"), N_("Abrufmodus, Verschlüsselung, Master-Passphrase, Zertifikat"),
     N_("Sicherheit"), "shield"),
    ("/securities", N_("Wertpapiere"), N_("ISIN unter eigenem Symbol führen"), N_("Daten"), "tag"),
    # Hidden for now (waldonso2/wealthfolio-importer-addon#84):
    # ("/duplicates", N_("Duplikate"), N_("Doppelte Buchungen finden und entfernen"), N_("Daten"), "copy"),
    ("/about", N_("Über"), N_("Version, Links, Datenschutz"), N_("Über"), "info"),
]


def settings_groups(pages: list[tuple[str, str, str, str, str]]) -> list[tuple[str, list]]:
    """The pages by group, in the order of ``SETTINGS_PAGES``."""
    groups: dict[str, list] = {}
    for page in pages:
        groups.setdefault(page[3], []).append(page)
    return list(groups.items())


@dataclass
class AppContext:
    data_dir: Path
    vault: Vault
    state: State
    adapters: dict[str, type[BrokerAdapter]]
    make_wf: Callable[[str, Any], WealthfolioClient]
    syncer: Syncer
    templates: Jinja2Templates
    notifier: Notifier | None
    run_in_thread: bool
    secure: bool
    gate: Gate
    throttle: Throttle
    # Adapters waiting for a TAN, between the two requests of a login.
    pending: dict[str, BrokerAdapter] = field(default_factory=dict)
    # Set while a sync started from the UI runs.
    running: threading.Event = field(default_factory=threading.Event)
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    # Brokers whose login step runs now (#134 M5): a second request at the same time never sends a TAN twice
    # or finishes one login twice. Not a lock held for the step - the step may start the next broker's login.
    login_busy: set[str] = field(default_factory=set)
    # PINs typed at a login that aren't stored (#85): kept in memory only until the run that follows.
    pins: dict[str, str] = field(default_factory=dict)
    # The long action going on in the background (#104; a UI text key) and the message it ended with.
    job: str = ""
    job_result: dict | None = None
    # The account check of page *Prüfung* (#104): {broker: Report}, when, and the account names.
    audits: dict = field(default_factory=dict)
    audits_at: float = 0.0
    account_names: dict[str, str] = field(default_factory=dict)
    audit_error: str | None = None
    # Look-through (*Durchschau*): the depot's positions, read in the background.
    depot: lookthrough.Depot = field(default_factory=lambda: lookthrough.Depot())
    depot_at: float = 0.0
    depot_error: str | None = None
    _accounts: tuple[float, str, list] | None = None

    def render(self, request: Request, name: str, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(16)
        if request.session.get("enable_service") and request.session.get("user"):
            from .routes.services import settle  # a service waiting for its settings (#145)

            settle(request, self)
        flash = request.session.pop("flash", None)
        if not flash and self.job_result and request.session.get("user"):
            with self.start_lock:  # shown once, even to two pages at the same moment
                flash, self.job_result = self.job_result, None  # what a background job ended with (#104)
        nav_unknown = self.state.unknown_count() if request.session.get("user") else 0
        language = self.language()
        try:
            c: config_mod.Config | None = self.cfg()
        except Exception:  # locked: no settings pages anyway
            c = None
        pages = [p for p in SETTINGS_PAGES if p[0] != "/transfers" or (c and c.transfer_patterns)]
        try:
            wf_font = appearance.font(self.state)  # the font chosen in Wealthfolio
        except Exception:  # locked: Wealthfolio's default
            wf_font = appearance.DEFAULT
        # The scheme chosen on *Darstellung*; the header's symbol turns a fixed one round here, a "system" one in
        # the CSS (data-flip, #123).
        scheme = c.ui_scheme if c else "system"
        if scheme != "system" and request.session.get("flip"):
            scheme = "light" if scheme == "dark" else "dark"
        return self.templates.TemplateResponse(request, name, {
            "csrf": request.session["csrf"], "flash": flash, "version": __version__, "adapters": self.adapters,
            "nav_unknown": nav_unknown, "settings_pages": pages, "settings_groups": settings_groups(pages),
            "lang": language, "reauth": not recent(request), "font": (c and c.ui_font) or wf_font,
            "wf_font": wf_font, "scheme": scheme, **ctx,
        })

    @contextmanager
    def login_step(self, key: str) -> Iterator[bool]:
        """True while this request has the broker's login to itself, False if another one has it now."""
        with self.start_lock:
            free = key not in self.login_busy
            self.login_busy.add(key)
        try:
            yield free
        finally:
            if free:
                with self.start_lock:
                    self.login_busy.discard(key)

    def event(self, request: Request, event: str, *, ok: bool = True, broker: str = "", detail: str = "") -> None:
        """One entry of the event log on *Sicherheit* (#102); ``event`` a key of ``EVENTS``. Never a secret."""
        self.state.add_event(event, client=client_of(request), ok=ok, broker=broker, detail=detail)

    def reconfirm(self, request: Request, password: str) -> str | None:
        """For a sensitive action (#96): None when the password was entered within ``REAUTH_SECONDS`` or is
        right now, else the error text for the page."""
        if recent(request):
            return None
        client = client_of(request)
        with self.throttle.attempt(client) as a:
            if a.refused:
                text, params = self.throttle.message(a)
                return tr(request, text, **params)
            a.ok = verify_password(password, self.vault.load().get("ui_password_hash"))
        self.event(request, "reauth", ok=a.ok)
        if not a.ok:
            return tr(request, "Das Passwort der Oberfläche stimmt nicht.")
        request.session["auth"] = int(time.time())
        return None

    def language(self) -> str:
        """Language of the pages; German while the config can't be read (locked)."""
        try:
            return self.cfg().ui_language
        except Exception:
            return "de"

    def cfg(self) -> config_mod.Config:
        return config_mod.load(self.data_dir)

    def update(self, change: Callable[[config_mod.Config], None]) -> config_mod.Config:
        """Change the config under its lock (#117); check the form before, ``change`` only applies it."""
        return config_mod.update(self.data_dir, change)

    def wf_client(self, c: config_mod.Config) -> WealthfolioClient:
        return connect(self.make_wf, c.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                       c.wealthfolio_fingerprint)

    def wf_accounts(self, c: config_mod.Config, fresh: bool = False):
        """Wealthfolio's active accounts, kept for ``ACCOUNTS_SECONDS`` (#104) unless ``fresh``."""
        cached = self._accounts
        if not fresh and cached and cached[1] == c.wealthfolio_url and time.monotonic() - cached[0] < ACCOUNTS_SECONDS:
            return cached[2], None
        try:
            with self.wf_client(c) as wf:
                accounts = [a for a in wf.list_accounts() if a.is_active]
        except WealthfolioError as e:
            self._accounts = None
            return [], str(e)
        self._accounts = (time.monotonic(), c.wealthfolio_url, accounts)
        return accounts, None

    def needs_login(self, key: str, c: config_mod.Config) -> bool:
        """A fetch starts with a login in the UI: in the safe mode, when a credential is typed each time (#85),
        or when the last fetch needed a login."""
        if c.safe_mode:
            return True
        last = self.state.last_run(key)
        if last and last.status == "needs_auth":  # the last fetch stopped at the login: start there
            return True
        stored = self.vault.broker(key).get("credentials", {})
        return any(f.ask and not stored.get(f.name) for f in self.adapters[key].credential_fields)

    def finish_login(self, key: str, adapter: BrokerAdapter) -> None:
        adapter.close()
        self.vault.store_session(key, adapter)


# The event log's entries (#102): key → what the page *Sicherheit* says.
EVENTS = {"login": N_("Anmeldung"), "logout": N_("Abmeldung"), "setup_password": N_("Erstes Passwort festgelegt"),
          "password": N_("Passwort geändert"), "sessions": N_("Alle anderen Sitzungen beendet"),
          "credentials": N_("Zugangsdaten geändert"), "wealthfolio": N_("Wealthfolio-Adresse gespeichert"),
          "passphrase": N_("Master-Passphrase geändert"), "mode": N_("Abrufmodus geändert"),
          "reset": N_("Neu aufgesetzt"),
          "unknown_ignore": N_("Unbekannte Buchung ignoriert"), "lock": N_("Gesperrt"), "unlock": N_("Entsperrt"),
          "reauth": N_("Passwort für eine Aktion bestätigt"),
          "auto_update": N_("Automatisches Update")}


def client_of(request: Request) -> str:
    return request.client.host if request.client else "?"


def sign_in(request: Request, secrets_: dict) -> None:
    """A fresh session after the password was checked, nothing carried over from before."""
    now = int(time.time())
    request.session.clear()
    request.session.update(user="admin", pw=session_tag(secrets_), since=now, seen=now, auth=now)


def recent(request: Request) -> bool:
    return time.time() - request.session.get("auth", 0) <= REAUTH_SECONDS


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def language(request: Request) -> str:
    """The language of the pages (#87): ``Config.ui_language``, German while the config can't be read."""
    ctx = getattr(request.app.state, "ctx", None)
    return ctx.language() if ctx is not None else "de"


def tr(request: Request, text: str, **params) -> str:
    """A UI text (a German key, ``web/i18n.py``) in the page's language, its placeholders filled with
    ``params`` - for the flash messages of ``redirect``, which are stored translated."""
    return translate(text, language(request), **params)


def redirect(request: Request, url: str, flash: str | None = None, level: str = "ok") -> RedirectResponse:
    """To ``url``, with a message already in the page's language (``tr``)."""
    if flash:
        # Messages can carry a broker's error text: never a secret or an account number on screen.
        request.session["flash"] = {"text": redact.redact(flash), "level": level}
    return RedirectResponse(url, 303)


def cross_site(request: Request) -> bool:
    """A form another website sent through the user's browser (``Sec-Fetch-Site``). Refused before any password
    check: a page elsewhere could else spend the brake's attempts and lock the owner out (#153)."""
    return request.method == "POST" and request.headers.get("sec-fetch-site") == "cross-site"


async def csrf_guard(request: Request) -> None:
    if cross_site(request):
        raise HTTPException(403, tr(request, "Anfrage von einer anderen Website abgelehnt."))
    # Starlette caches the parsed form, so the endpoints can still read it.
    if request.method == "POST" and request.url.path not in PUBLIC:
        form = await request.form()
        expected, given = request.session.get("csrf") or "", str(form.get("csrf") or "")
        if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(400, tr(request, "Formular abgelaufen - bitte die Seite neu laden."))


def accounts_of(b: config_mod.BrokerConfig) -> Accounts:
    return Accounts(b.cash_account_id, b.portfolio_account_id, b.savings_account_id)
