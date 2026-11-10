"""The daily pages: overview (a traffic light per broker), "Jetzt abrufen", settings index, unknown bookings."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ... import __version__, container, crypto, meta_keys, redact, retired, swap, updates
from ... import config as config_mod
from ...history import COMPLETION_KINDS
from ...sync import AlreadyRunning, is_running
from ...wealthfolio import WealthfolioError
from .. import inputs, views
from ..context import AppContext, Ctx, language, redirect, tr
from ..i18n import N_, translate

log = logging.getLogger(__name__)
router = APIRouter()

TIMER = "wealthfolio-companion-run.timer"


@router.get("/broker", response_class=HTMLResponse)
def dashboard(request: Request, ctx: Ctx):
    # A run whose process died stays at 'running' in the database; close it
    # unless a sync (here or the timer's) really is in progress.
    if not ctx.running.is_set():
        ctx.syncer.cleanup()
    busy = ctx.running.is_set() or is_running(ctx.data_dir)
    c = ctx.cfg()
    secrets_ = ctx.vault.load()
    brokers = _brokers(ctx, c, secrets_)
    progress = json.loads(ctx.state.meta(meta_keys.PROGRESS()) or "{}") if busy else {}
    return ctx.render(request, "dashboard.html", brokers=brokers,
                      wf_missing=not (c.wealthfolio_url and _wf_done(secrets_)), running=busy,
                      job=ctx.job if ctx.running.is_set() else N_("Abruf"), progress=progress,
                      refresh=busy and not request.query_params.get("still"),
                      next_run=_next_timer_run(), runs=ctx.state.runs(limit=20),
                      retired_notices=retired.notices(ctx.state), safe_mode=c.safe_mode,
                      update=updates.available(ctx.state, __version__), releases=updates.RELEASES,
                      container=container.active(),
                      checklist=_checklist(ctx, c, secrets_, brokers), sync_off=not c.service("sync"))


def _checklist(ctx: AppContext, c: config_mod.Config, secrets_: dict, brokers: list[dict]) -> list[dict] | None:
    """What a new setup still lacks (#110), until all of it is done and the list hidden; None then."""
    if ctx.state.flag(meta_keys.CHECKLIST_HIDDEN()):
        return None
    return [
        {"text": N_("Wealthfolio verbinden"), "href": "/setup/wealthfolio", "done": _wf_done(secrets_)},
        {"text": N_("Ersten Broker einrichten"), "href": "/brokers",
         "done": any(b["has_credentials"] for b in brokers)},
        {"text": N_("Benachrichtigungen einrichten (für TAN-Anfragen und Fehler)"), "href": "/notifications",
         "done": bool(c.ntfy_topic)},
        {"text": N_("Optional: Master-Passphrase setzen"), "href": "/security", "optional": True,
         "done": crypto.passphrase_enabled(ctx.data_dir)},
        {"text": N_("Optional: dem Zertifikat der Oberfläche vertrauen"), "href": "/security", "optional": True,
         "done": False},
    ]


@router.post("/checklist/hide")
def checklist_hide(request: Request, ctx: Ctx):
    ctx.state.set_flag(meta_keys.CHECKLIST_HIDDEN())
    return redirect(request, "/broker")


def _brokers(ctx: AppContext, c: config_mod.Config, secrets_: dict) -> list[dict]:
    brokers = []
    lang = c.ui_language
    for key, adapter in ctx.adapters.items():
        b = c.brokers.get(key, config_mod.BrokerConfig())
        last = ctx.state.last_run(key)
        reconcile = ctx.state.reconcile(key)
        unknown = len(ctx.state.unknown_events(key))
        gaps = sum(1 for g in ctx.state.gaps(key) if g["kind"] in ("missing", "partial", "orphan"))
        balances = ctx.state.balances(key)
        # The overview shows no amounts, only whether the broker's balance matches Wealthfolio (#83).
        cash_off = [d for d in (reconcile or {}).get("deviations", []) if d.get("kind") == "cash"]
        saldo = None if not balances or reconcile is None else ("off" if cash_off else "ok")
        light, reasons = views.broker_light(asdict(last) if last else None, reconcile, unknown, gaps, b.enabled,
                                            lang=lang, limited_until=ctx.state.meta(meta_keys.RATE_LIMIT(key)))
        brokers.append({
            "key": key,
            "label": adapter.label,
            "savings_label": adapter.savings_label,
            "config": b,
            "has_credentials": bool(secrets_.get("brokers", {}).get(key, {}).get("credentials")),
            "last": last,
            "saldo": saldo,
            "reconcile": reconcile,
            "unknown": unknown,
            "gaps": gaps,
            "light": light,
            "reasons": reasons,
        })
    return brokers


@router.post("/retired/dismiss")
def retired_dismiss(request: Request, ctx: Ctx, broker: str = Form(...)):
    retired.dismiss(ctx.state, broker)
    return redirect(request, "/broker")


@router.post("/run")
def run_now(request: Request, ctx: Ctx, broker: str = Form("")):
    if ctx.running.is_set():
        return redirect(request, "/broker", tr(request, "Ein Abruf läuft bereits."), "warn")
    c = ctx.cfg()
    if not c.service("sync"):
        return redirect(request, "/broker",
                        tr(request, "Broker-Sync ist ausgeschaltet - auf der Seite Dienste einschalten."), "warn")
    if broker:
        if broker not in ctx.adapters:
            return redirect(request, "/broker", tr(request, "Unbekannter Broker."), "error")
        if ctx.needs_login(broker, c):
            from .broker_login import start_login  # this POST starts the login itself (#99)

            return start_login(request, ctx, broker, True)
        return start_run(request, ctx, [broker])
    # "Alle abrufen": the brokers that need a login first are logged in one after the other (#85), then all
    # are fetched in one run.
    keys = [k for k, b in c.brokers.items()
            if k in ctx.adapters and b.enabled and ctx.vault.broker(k).get("credentials")]
    login = [k for k in keys if ctx.needs_login(k, c)]
    if login:
        request.session["run_queue"] = {"todo": login, "run": keys, "login": list(login)}
        from .broker_login import start_login

        return start_login(request, ctx, login[0], True)
    return start_run(request, ctx, None)


def start_run(request: Request, ctx: AppContext, only: list[str] | None) -> RedirectResponse:
    """Start a run from the UI (``manual``: the safe mode fetches only so), with the PINs typed at the logins."""
    pins = {k: ctx.pins.pop(k) for k in list(ctx.pins) if only is None or k in only}

    def work(lang: str) -> None:
        try:
            ctx.syncer.run(only, manual=True, pins=pins)
        except AlreadyRunning:
            log.info("sync skipped: already running (timer)")
        finally:
            pins.clear()

    return start_job(request, ctx, N_("Abruf"), work, "/broker",
                     started=N_("Abruf gestartet. Die Seite aktualisiert sich, bis er fertig ist."),
                     done=N_("Abruf beendet."))


def start_job(request: Request, ctx: AppContext, name: str, work: Callable[[str], tuple[str, str] | None], url: str,
              *, started: str = N_("{job} läuft im Hintergrund. Das Ergebnis erscheint hier, sobald es fertig ist."),
              done: str = "") -> RedirectResponse:
    """A long action in the background (#104): a fetch, *Neu aufsetzen*, logos, the account check - one at a time.
    ``work(lang)`` returns its message (text, level), which the next page shows."""
    busy = launch(ctx, name, work, language(request))
    if busy:
        text = (tr(request, "Ein Abruf läuft bereits.") if busy == N_("Abruf") else
                tr(request, "Gerade läuft noch: {job}. Bitte danach noch einmal.", job=tr(request, busy)))
        return redirect(request, url, text, "warn")
    if ctx.run_in_thread:
        return redirect(request, url, tr(request, started, job=tr(request, name)))
    if ctx.job_result is None and done:
        return redirect(request, url, tr(request, done))
    return redirect(request, url)


def launch(ctx: AppContext, name: str, work: Callable[[str], tuple[str, str] | None], lang: str) -> str:
    """Start ``work`` (in a thread, unless the app runs jobs inline - tests); the job going on instead, if any."""
    with ctx.start_lock:  # check and set as one: two quick clicks start one job
        if ctx.running.is_set():
            return ctx.job
        ctx.running.set()  # before the thread starts, so the next page view already shows it
        ctx.job = name

    def body() -> None:
        try:
            try:
                result = work(lang)
            except Exception:
                log.exception("%s failed", name)
                result = (translate("{job} ist fehlgeschlagen - siehe Protokoll.", lang, job=translate(name, lang)),
                          "error")
            if result:
                ctx.job_result = {"text": redact.redact(result[0]), "level": result[1]}
        finally:  # whatever goes wrong above: the next job must not wait for this one for ever
            ctx.running.clear()

    if ctx.run_in_thread:
        threading.Thread(target=body, daemon=True).start()
    else:
        body()
    return ""


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ctx: Ctx):
    return ctx.render(request, "settings.html")


@router.get("/unknown", response_class=HTMLResponse)
def unknown_page(request: Request, ctx: Ctx):
    return ctx.render(request, "unknown.html", events=ctx.state.unknown_events(),
                      completions={f"{b}:{t}": c for (b, t), c in ctx.state.completions().items()},
                      kinds=COMPLETION_KINDS)


# What each kind needs besides the ISIN (#132).
_NEEDS = {"buy": ("shares", "amount"), "sell": ("shares", "amount"), "in": ("shares", "amount"),
          "out": ("shares",), "split": ("ratio",)}


@router.post("/unknown/complete")
async def unknown_complete(request: Request, ctx: Ctx):
    """An unknown booking the user completed (#132): the next run books it under the booking's own id."""
    form = await request.form()
    broker, tx_id = str(form.get("broker", "")), str(form.get("tx_id", ""))
    event = next((e for e in ctx.state.unknown_events(broker) if e["tx_id"] == tx_id), None) if broker else None
    if event is None:
        return redirect(request, "/unknown", tr(request, "Diese Buchung ist nicht mehr offen."), "error")
    if form.get("action") == "delete":
        ctx.state.delete_completion(broker, tx_id)
        return redirect(request, "/unknown", tr(request, "Zurückgenommen."))
    kind = str(form.get("kind", ""))
    try:
        if kind not in _NEEDS:
            raise inputs.Invalid(N_("Bitte wählen, was die Buchung ist."))
        # shortcut: always EUR - unknown payloads rarely name a currency; a field for it once a broker needs one.
        data: dict = {"kind": kind, "isin": inputs.isin(str(form.get("isin", ""))), "day": event["occurred_at"],
                      "currency": "EUR", "name": inputs.text(str(form.get("name") or ""), N_("Name"), max_len=200)}
        for field, label in (("shares", N_("Stückzahl")), ("amount", N_("Betrag")), ("fee", N_("Gebühr")),
                             ("tax", N_("Steuer")), ("ratio", N_("Verhältnis"))):
            raw = str(form.get(field) or "").strip()
            if raw:
                data[field] = str(inputs.decimal(raw, label, minimum=Decimal(0)))
            elif field in _NEEDS[kind]:
                raise inputs.Invalid("{label}: bitte angeben.", label=label)
        if any(Decimal(data[f]) <= 0 for f in _NEEDS[kind]):
            raise inputs.Invalid(N_("Stückzahl, Betrag und Verhältnis müssen größer 0 sein."))
    except inputs.Invalid as e:
        return redirect(request, "/unknown", e.text(language(request)), "error")
    ctx.state.add_completion(broker, tx_id, data)
    return redirect(request, "/unknown", tr(request, "Gespeichert - der nächste Abruf bucht es."))


@router.post("/unknown/swap")
async def unknown_swap(request: Request, ctx: Ctx):
    """Confirm the proposed exchange of two ISINs (Trade Republic's reverse split into a new instrument): the
    cost basis comes from Wealthfolio's position, both legs are stored as completions and booked by the next run."""
    form = await request.form()
    broker, tx_id = str(form.get("broker", "")), str(form.get("tx_id", ""))
    events = {e["tx_id"]: e for e in ctx.state.unknown_events(broker)} if broker else {}
    proposal = (events.get(tx_id) or {}).get("payload", {}).get("proposal")
    legs = [events.get(proposal[s]["tx_id"]) for s in ("out", "in")] if proposal else [None]
    if not proposal or None in legs:
        return redirect(request, "/unknown", tr(request, "Diese Buchung ist nicht mehr offen."), "error")
    c = ctx.cfg()
    b = c.brokers.get(broker)
    try:
        if b is None or not b.portfolio_account_id:
            raise swap.SwapError("account")
        out, new = proposal["out"], proposal["in"]
        shares = Decimal(out["shares"])
        with ctx.wf_client(c) as wf:
            known = ctx.state.assets(broker).get(out["isin"]) or {}
            amount, currency = swap.cost_basis(wf, b.portfolio_account_id, known.get("asset_id"), out["isin"],
                                               shares)
    except (swap.SwapError, WealthfolioError, InvalidOperation) as e:
        why = {"held": N_("Wealthfolio hält von der alten Position weniger Stücke als entfernt wurden."),
               "basis": N_("Wealthfolio kennt für die alte Position keinen Einstandswert."),
               "position": N_("Die alte Position ist in Wealthfolio nicht zu finden."),
               "account": N_("Das Depotkonto des Brokers ist nicht eingestellt.")}.get(str(e))
        return redirect(request, "/unknown", tr(request, why) if why else str(e), "error")
    for kind, n in (("swap_out", out), ("swap_in", new)):
        ctx.state.add_completion(broker, n["tx_id"], {
            "kind": kind, "isin": n["isin"], "day": events[n["tx_id"]]["occurred_at"], "currency": currency,
            "name": n["name"][:200], "shares": n["shares"], "amount": str(amount)})
    return redirect(request, "/unknown", tr(request, "Gespeichert - der nächste Abruf bucht den Tausch."))


def _wf_done(secrets_: dict) -> bool:
    return bool(secrets_.get("wealthfolio_checked"))


_TIMER_CACHE: list = [0.0, None]


def _next_timer_run() -> str | None:
    """Next run of the systemd timer, if it is installed; asked at most once a minute (#104)."""
    if time.monotonic() - _TIMER_CACHE[0] > 60:
        _TIMER_CACHE[:] = [time.monotonic(), _ask_timer()]
    return _TIMER_CACHE[1]


def _ask_timer() -> str | None:
    try:
        out = subprocess.run(["systemctl", "show", TIMER, "-p", "NextElapseUSecRealtime", "--value"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out == "n/a":
        return None
    # "Fri 2026-10-09 06:12:00 CEST" - already local time of the host.
    parts = out.split()
    try:
        return datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except (IndexError, ValueError):
        return out
