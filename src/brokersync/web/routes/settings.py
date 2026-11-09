"""Settings pages without a broker of their own: Wealthfolio, notifications, securities, updates."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
from decimal import Decimal
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import __version__, appearance, meta_keys, updates
from ...notify import Notifier
from ...wealthfolio import WealthfolioError, connect, is_certificate_error, normalize_fingerprint, server_fingerprint
from .. import inputs
from ..context import AppContext, Ctx, language, redirect, tr
from ..i18n import N_

log = logging.getLogger(__name__)
router = APIRouter()


# ── Wealthfolio ─────────────────────────────────────────────────────────────
@router.get("/setup/wealthfolio", response_class=HTMLResponse)
def wealthfolio_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    return _wealthfolio_form(request, ctx, c.wealthfolio_url, c.wealthfolio_fingerprint)


def _wealthfolio_form(request: Request, ctx: AppContext, url: str, fingerprint: str, *, error: str = "",
                      offer: str = "", status: int = 200) -> HTMLResponse:
    """The form, after a test or an error with what was typed (never the password). ``offer``: the fingerprint
    of the certificate the server shows, to trust after the user compared it (#95)."""
    response = ctx.render(request, "wealthfolio.html", url=url, fingerprint=fingerprint, error=error, offer=offer,
                          has_password=bool(ctx.vault.load().get("wealthfolio_password")),
                          plain=url.startswith("http://") and not _local(url))
    response.status_code = status
    return response


def _read_font(ctx: AppContext, wf) -> None:
    """The font chosen in Wealthfolio, for this UI (appearance.py); a failure only leaves the one read before."""
    try:
        appearance.refresh(ctx.state, wf)
    except (WealthfolioError, ValueError):
        log.warning("Wealthfolio's font not read")


def _local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def _checked(url: str, password: str, fingerprint: str) -> tuple[str, str]:
    """The form's address and fingerprint, checked; raises ``inputs.Invalid``."""
    url = inputs.http_url(url, N_("Wealthfolio-Adresse"))
    if len(password) > inputs.MAX_TEXT:  # not stripped: a password may start or end with a space
        raise inputs.Invalid("{label}: höchstens {max_len} Zeichen.", label=N_("Passwort"), max_len=inputs.MAX_TEXT)
    pin = normalize_fingerprint(fingerprint)
    if fingerprint.strip() and not pin:
        raise inputs.Invalid("{label}: 64 Hex-Zeichen (SHA-256), mit oder ohne Doppelpunkte.", label=N_("Fingerprint"))
    return url, pin


@router.post("/setup/wealthfolio")
def wealthfolio_save(request: Request, ctx: Ctx, url: str = Form(...), password: str = Form(""),
                     no_password: str = Form(""), fingerprint: str = Form(""), test: str = Form("")):
    try:
        url, pin = _checked(url, password, fingerprint)
    except inputs.Invalid as e:
        return _wealthfolio_form(request, ctx, url, fingerprint, error=e.text(language(request)), status=422)
    stored = ctx.vault.load().get("wealthfolio_password")
    if stored and not password and not no_password and url != ctx.cfg().wealthfolio_url:
        # The stored password only ever goes to the address it was saved for (#153): never to a new one.
        return _wealthfolio_form(request, ctx, url, fingerprint, status=422, error=tr(
            request, "Für eine neue Adresse bitte das Wealthfolio-Passwort noch einmal eingeben."))
    secret = None if no_password else (password or stored)
    try:  # "Nur testen" (#95): with what was typed, nothing saved
        with connect(ctx.make_wf, url, secret, pin) as wf:
            accounts = [a for a in wf.list_accounts() if a.is_active]
            if not test:
                _read_font(ctx, wf)
    except WealthfolioError as e:
        offer = ""
        if url.startswith("https://") and is_certificate_error(e):
            with contextlib.suppress(OSError, ValueError):
                offer = server_fingerprint(url)
        if test or offer:
            return _wealthfolio_form(request, ctx, url, fingerprint, offer=offer, status=422,
                                     error=tr(request, "Verbindung fehlgeschlagen: {error}", error=e))
        accounts, error = [], str(e)
    else:
        error = ""
    if test:
        request.session["flash"] = {"text": tr(request, "Test erfolgreich - {n} Konten gefunden. Noch nichts "
                                                        "gespeichert.", n=len(accounts)), "level": "ok"}
        return _wealthfolio_form(request, ctx, url, fingerprint)

    def change(c) -> None:
        c.wealthfolio_url, c.wealthfolio_fingerprint = url, pin

    c = ctx.update(change)

    def store(d: dict) -> None:
        if no_password:
            d.pop("wealthfolio_password", None)
        elif password:
            d["wealthfolio_password"] = password
        d["wealthfolio_checked"] = not error

    ctx.vault.update(store)
    ctx.event(request, "wealthfolio", detail=urlsplit(url).hostname or "")
    if error:
        return redirect(request, "/setup/wealthfolio", tr(request, "Verbindung fehlgeschlagen: {error}", error=error),
                        "error")
    ctx.wf_accounts(c, fresh=True)
    return redirect(request, "/brokers", tr(request, "Verbunden - {n} Konten in Wealthfolio gefunden.",
                                            n=len(accounts)))


# ── notifications ───────────────────────────────────────────────────────────
@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    if not c.ntfy_topic:
        # 128 bits, saved at once (#98): who subscribes and reloads before saving keeps the same topic.
        topic = f"wealthfolio-sync-{secrets.token_urlsafe(16)}"
        c = ctx.update(lambda c: setattr(c, "ntfy_topic", c.ntfy_topic or topic))
    return ctx.render(request, "notifications.html", cfg=c, has_token=bool(c.ntfy_token))


@router.post("/notifications")
def notifications_save(request: Request, ctx: Ctx, server: str = Form(...), topic: str = Form(""),
                       token: str = Form(""), public_url: str = Form(""), details: str = Form(""),
                       public_ok: str = Form(""), forget_token: str = Form(""), test: str = Form("")):
    try:
        server = inputs.http_url(server, N_("ntfy-Server"))
        topic = inputs.text(topic, N_("Thema"), max_len=64)
        public_url = inputs.http_url(public_url, N_("Adresse dieser Oberfläche"), required=False)
        token = inputs.text(token, N_("Zugangstoken"))
    except inputs.Invalid as e:
        return redirect(request, "/notifications", e.text(language(request)), "error")
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic):
        return redirect(request, "/notifications", tr(request, "Thema: nur Buchstaben, Ziffern, - und _."), "error")
    if details and urlsplit(server).hostname == "ntfy.sh" and not public_ok:
        # On the public server anyone who knows the topic reads along: amounts only on purpose.
        return redirect(request, "/notifications", tr(request, "Details auf ntfy.sh: Dort liest jeder mit, der das "
                                                               "Thema kennt - bitte ausdrücklich bestätigen."),
                        "error")

    def change(c) -> None:
        old_server = c.ntfy_server
        c.ntfy_server, c.ntfy_topic, c.public_url, c.ntfy_details = server, topic, public_url, bool(details)
        if forget_token or (not token and server != old_server):
            c.ntfy_token = ""  # a token belongs to its server: never sent to a new one (#153)
        elif token:
            c.ntfy_token = token

    c = ctx.update(change)
    if test:
        n = ctx.notifier or Notifier(c.ntfy_server, c.ntfy_topic, c.ntfy_token)
        ok = n.send("Wealthfolio Companion", "Testnachricht - Benachrichtigungen funktionieren.",
                    link=c.public_url or None, tags="white_check_mark")
        if not ok:
            return redirect(request, "/notifications", tr(request, "Testnachricht konnte nicht gesendet werden."),
                            "error")
        return redirect(request, "/notifications", tr(request, "Testnachricht gesendet."))
    return redirect(request, "/broker", tr(request, "Benachrichtigungen gespeichert."))


# ── securities ──────────────────────────────────────────────────────────────
@router.get("/securities", response_class=HTMLResponse)
def securities_page(request: Request, ctx: Ctx):
    return ctx.render(request, "securities.html", mappings=ctx.cfg().security_mappings)


@router.post("/securities")
def securities_save(request: Request, ctx: Ctx, isin: str = Form(...), symbol: str = Form(""),
                    exchange_mic: str = Form(""), delete: str = Form("")):
    isin = isin.strip().upper()
    if delete:
        def drop(c) -> None:
            c.security_mappings.pop(isin, None)

        ctx.update(drop)
    else:
        try:
            isin = inputs.isin(isin)
            mapping = {"symbol": inputs.symbol(symbol), "exchangeMic": inputs.mic(exchange_mic)}
        except inputs.Invalid as e:
            return redirect(request, "/securities", e.text(language(request)), "error")
        ctx.update(lambda c: c.security_mappings.__setitem__(isin, mapping))
    return redirect(request, "/securities", tr(request, "Gespeichert."))


# ── logos ───────────────────────────────────────────────────────────────────
@router.get("/logos")
def logos_page(request: Request):
    return RedirectResponse("/services/logos", 303)  # the service's page now (#151)


# ── appearance ─────────────────────────────────────────────────────────────
# Each language under its own name, in every language.
LANGUAGES = {"de": "Deutsch", "en": "English"}


# "auto" (stored as ""): the font chosen in Wealthfolio - an empty form field would read as a missing one.
FONTS = ("auto", "sans", "serif", "mono")
SCHEMES = ("system", "light", "dark")


@router.get("/appearance", response_class=HTMLResponse)
def appearance_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    return _appearance_form(request, ctx, c.ui_font, c.ui_scheme, c.ui_language)


def _appearance_form(request: Request, ctx: AppContext, font: str, scheme: str, lang: str, *, error: str = "",
                     status: int = 200) -> HTMLResponse:
    response = ctx.render(request, "appearance.html", chosen_font=font, chosen_scheme=scheme, chosen_lang=lang,
                          languages=LANGUAGES, error=error)
    response.status_code = status
    return response


@router.post("/appearance")
def appearance_save(request: Request, ctx: Ctx, language: str = Form("de"), font: str | None = Form(None),
                    scheme: str | None = Form(None)):
    c = ctx.cfg()
    font = (c.ui_font or "auto") if font is None else font  # a form without the field keeps what is stored
    scheme = c.ui_scheme if scheme is None else scheme
    try:
        inputs.choice(language, tuple(LANGUAGES), N_("Sprache"))
        inputs.choice(font, FONTS, N_("Schrift"))
        inputs.choice(scheme, SCHEMES, N_("Theme"))
    except inputs.Invalid as e:
        return _appearance_form(request, ctx, c.ui_font, c.ui_scheme, c.ui_language, error=e.text(ctx.language()),
                                status=422)

    def change(c) -> None:
        c.ui_language, c.ui_font, c.ui_scheme = language, "" if font == "auto" else font, scheme

    ctx.update(change)
    return redirect(request, "/appearance", tr(request, "Gespeichert."))  # already in the language just chosen


# ── about ───────────────────────────────────────────────────────────────────
# Every address the page *Über* links to, in one place (the coming rename changes only these).
ABOUT_LINKS = {
    "repo": updates.REPO,
    "docs": f"{updates.REPO}#readme",
    "security": f"{updates.REPO}/blob/main/SECURITY.md",
    "license": f"{updates.REPO}/blob/main/LICENSE",
    "issues": "https://github.com/waldonso2/wealthfolio-importer-addon/issues/new",
    "pytr": "https://github.com/pytr-org/pytr",
    "fints": "https://github.com/raphaelm/python-fints",
    "sc": "https://github.com/ScalableCapital/scalable-cli",
}


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request, ctx: Ctx):
    return ctx.render(request, "about.html", links=ABOUT_LINKS)


# ── updates ─────────────────────────────────────────────────────────────────
@router.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request, ctx: Ctx):
    from ...container import active

    return ctx.render(request, "updates.html", cfg=ctx.cfg(), installed=__version__, container=active(),
                      newest=updates.stored(ctx.state).get("version"),
                      last=json.loads(ctx.state.meta(meta_keys.AUTO_UPDATE()) or "null"),
                      lowest=updates.MIN_INTERVAL, highest=updates.MAX_INTERVAL, max_delay=updates.MAX_DELAY)


@router.post("/updates")
def updates_save(request: Request, ctx: Ctx, auto_update: str = Form(""), interval: str = Form(""),
                 delay: str = Form("0"), ui_password: str = Form("")):
    """Whether the container installs a newer release by itself, how often it asks GitHub (seconds) and how many
    days a release must be out before it does."""
    try:
        seconds = inputs.decimal(interval.strip(), N_("Wie oft nachsehen (Sekunden)"),
                                 minimum=Decimal(updates.MIN_INTERVAL), maximum=Decimal(updates.MAX_INTERVAL))
        days = inputs.decimal(delay.strip() or "0", N_("Wartezeit nach der Veröffentlichung (Tage)"),
                              minimum=Decimal(0), maximum=Decimal(updates.MAX_DELAY))
    except inputs.Invalid as e:
        return redirect(request, "/updates", e.text(language(request)), "error")
    if seconds != seconds.to_integral_value() or days != days.to_integral_value():
        return redirect(request, "/updates", tr(request, "Bitte ganze Zahlen angeben."), "error")
    on = bool(auto_update)
    if on != ctx.cfg().auto_update and (error := ctx.reconfirm(request, ui_password)):
        return redirect(request, "/updates", error, "error")

    def change(c) -> None:
        c.auto_update, c.update_interval, c.update_delay = on, int(seconds), int(days)

    ctx.update(change)
    ctx.event(request, "auto_update", detail=f"{'on' if on else 'off'} {int(seconds)}s, delay {int(days)}d")
    return redirect(request, "/updates", tr(request, "Gespeichert."))
