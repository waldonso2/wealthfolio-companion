"""Web UI: setup assistant, status, "Jetzt abrufen", broker login with TAN.

Everything a user configures happens here; nobody edits files. The UI is
protected by its own password, chosen on the first visit. Texts are German,
the audience are users of German brokers; English is the second language
(``web/i18n.py``, #87).

This module builds the app: templates, session, login check and the routers
of ``web/routes/`` (one per area, #76), which share an ``AppContext``
(``web/context.py``).
"""

from __future__ import annotations

import html
import logging
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .. import crypto, redact
from .. import state as state_mod
from ..adapters import ADAPTERS, BrokerAdapter
from ..notify import Notifier
from ..state import State
from ..sync import UNMATCHED_SECURITIES, Syncer
from ..vault import Vault, session_tag
from ..wealthfolio import WealthfolioClient
from . import i18n, views
from .context import (
    IDLE_SECONDS,
    MAX_SECONDS,
    PUBLIC,
    SETTINGS_PAGES,
    STATUS_LABEL,
    AppContext,
    accounts_of,
    csrf_guard,
)
from .gate import Gate, Hosts, Throttle
from .i18n import N_
from .routes import ROUTERS

log = logging.getLogger(__name__)

__all__ = ["PUBLIC", "SETTINGS_PAGES", "STATUS_LABEL", "accounts_of", "create_app"]

HERE = Path(__file__).parent


def create_app(data_dir: Path, *, wealthfolio=None, adapters: dict[str, type[BrokerAdapter]] | None = None,
               notifier: Notifier | None = None, run_in_thread: bool = True, secure: bool = False,
               logo_source=None, hosts: Hosts | None = None) -> Gate:
    """The web UI behind its gate: security headers, and the unlock page while a master passphrase is set
    and not entered (the app itself is built once the data can be read)."""
    data_dir = Path(data_dir)
    throttle = Throttle()
    gate = Gate(data_dir, secure=secure, throttle=throttle, templates_dir=HERE / "templates",
                static_dir=HERE / "static", hosts=hosts)
    gate.build = lambda: _create_app(data_dir, wealthfolio=wealthfolio, adapters=adapters, notifier=notifier,
                                     run_in_thread=run_in_thread, secure=secure, gate=gate, throttle=throttle,
                                     logo_source=logo_source)
    if not crypto.is_locked(data_dir):
        gate.app()  # build it now: start-up work (retired brokers) and errors show at start, not on a first visit
    return gate


def _create_app(data_dir: Path, *, wealthfolio, adapters, notifier, run_in_thread: bool, secure: bool, gate: Gate,
                throttle: Throttle, logo_source=None) -> FastAPI:
    vault = Vault(data_dir)
    adapters = adapters or ADAPTERS
    make_wf = wealthfolio or WealthfolioClient
    ctx = AppContext(data_dir=data_dir, vault=vault, state=State(data_dir), adapters=adapters, make_wf=make_wf,
                     syncer=Syncer(data_dir, wealthfolio=make_wf, notifier=notifier, adapters=adapters,
                                   logo_source=logo_source),
                     templates=_templates(), notifier=notifier, run_in_thread=run_in_thread, secure=secure,
                     gate=gate, throttle=throttle)

    session_key = vault.load().get("ui_session_key")
    if not session_key:
        session_key = secrets.token_urlsafe(32)
        vault.update(lambda d: d.__setitem__("ui_session_key", session_key))

    app = FastAPI(title="Wealthfolio Companion", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_guard)])
    app.state.ctx = ctx

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        # The first-password page opens without a login, but its POST carries a CSRF token and the one-time
        # code (#89), so it isn't in PUBLIC.
        if path in PUBLIC or path in ("/setup-password", "/setup-language") or path.startswith("/static/"):
            return await call_next(request)
        if path == "/metrics" and _metrics_token(request):  # a scraper without a UI login (OPS-04)
            return await call_next(request)
        secrets_ = vault.load()
        if not secrets_.get("ui_password_hash"):
            return RedirectResponse("/setup-password", 303)
        session, now = request.session, time.time()
        if (not session.get("user") or session.get("pw") != session_tag(secrets_)
                or now - session.get("seen", 0) > IDLE_SECONDS or now - session.get("since", 0) > MAX_SECONDS):
            # Also a cookie of an earlier password or session version (#96): after a logout, a password change
            # or companion-reset-password it signs in nobody; and none after 30 min idle or 12 h at all.
            session.clear()
            return RedirectResponse("/login", 303)
        if now - session["seen"] > 60:  # not every request: a page that reloads itself keeps the session anyway
            session["seen"] = int(now)
        return await call_next(request)

    # Added after the middleware above so it runs first and fills request.session.
    app.add_middleware(SessionMiddleware, secret_key=session_key, same_site="strict", https_only=secure,
                       max_age=MAX_SECONDS)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    for router in ROUTERS:
        app.include_router(router)
    _error_pages(app, ctx)
    return app


def _metrics_token(request: Request) -> bool:
    """``Authorization: Bearer <COMPANION_METRICS_TOKEN>`` - only a token of at least 32 characters counts."""
    token = os.environ.get("COMPANION_METRICS_TOKEN", "")
    given = request.headers.get("authorization", "")
    return len(token) >= 32 and secrets.compare_digest(given.encode(), f"Bearer {token}".encode())


ERROR_TITLES = {400: N_("Formular abgelaufen"), 403: N_("Nicht erlaubt"), 404: N_("Seite nicht gefunden"),
                413: N_("Anfrage zu groß"), 429: N_("Zu viele Anfragen")}


def _error_pages(app: FastAPI, ctx: AppContext) -> None:
    """Errors as a page of the UI (#106), not JSON or bare text; an unexpected one shows only an id that
    is also in the log - never its text, which can come from a bank or Wealthfolio (#60)."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        title = ERROR_TITLES.get(exc.status_code, N_("Fehler"))
        message = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else ""
        back = "/" if request.method == "GET" else request.headers.get("referer", "/")
        base = str(request.base_url)
        back = "/" + back[len(base):] if back.startswith(base) else back
        # Only a path of this site: "//host" or "/\host" would lead a browser elsewhere.
        response = ctx.render(request, "error.html", title=title, message=message,
                              back=back if re.fullmatch(r"/(?![/\\])[^\s]*", back) else "/")
        response.status_code = exc.status_code
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        error_id = secrets.token_hex(3)  # 6 characters: 8 digits would be masked as an account number
        log.error("web UI: unexpected error %s on %s", error_id, request.url.path, exc_info=exc)
        lang = ctx.language()
        text = i18n.translate("Unerwarteter Fehler (Fehler-ID {id}). Die Einzelheiten stehen im Log des Dienstes.",
                              lang, id=error_id)
        back = i18n.translate("Zur Übersicht", lang)
        return HTMLResponse(f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" '
                            f'content="width=device-width, initial-scale=1"><title>Wealthfolio Companion</title>'
                            f'<link rel="stylesheet" href="/static/style.css"></head><body><main class="wrap">'
                            f'<div class="card"><p>{html.escape(text)}</p><p><a href="/">{html.escape(back)}</a>'
                            f'</p></div></main></body></html>', status_code=500)


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    # One format for every date and time, German time (#73).
    # In the page's language (#115): German or English formats.
    templates.env.filters["local"] = pass_context(lambda c, v, seconds=False: views.local_time(v, seconds,
                                                                                            c.get("lang", "de")))
    templates.env.filters["day"] = pass_context(lambda c, v: views.local_day(v, c.get("lang", "de")))
    templates.env.filters["utc"] = views.utc_iso
    templates.env.filters["money"] = pass_context(lambda c, v: views.money(v, c.get("lang", "de")))
    # The UI's texts in the page's language (#87): ``{{ _("Speichern") }}``, see ``web/i18n.py``.
    templates.env.globals["_"] = i18n.template_gettext
    templates.env.globals["describe_unknown"] = views.describe_unknown
    # What the footer and *Sicherheit* may claim (#94): the database only where SQLCipher is there.
    templates.env.globals["db_encrypted"] = lambda: state_mod.ENCRYPTED
    templates.env.globals["report_url"] = views.report_url
    templates.env.globals["stage"] = views.stage
    # Foreign error texts (banks, brokers, Wealthfolio) on a page: never a secret or an account number.
    templates.env.filters["redact"] = lambda text: redact.redact(str(text)) if text else text
    # A stored run message line by line: the service's own (German) lines in the page's language, foreign
    # ones as they are (V-04).
    templates.env.filters["known"] = pass_context(lambda c, text: "\n".join(
        i18n.lookup(line, c.get("lang")) for line in str(text).split("\n")) if text else text)
    templates.env.globals["STATUS"] = STATUS_LABEL
    templates.env.globals["UNMATCHED_SECURITIES"] = UNMATCHED_SECURITIES
    return templates
