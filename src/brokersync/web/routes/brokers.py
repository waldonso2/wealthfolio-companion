"""Broker settings: list, credentials and accounts, fetch again, statement upload, *Neu aufsetzen*."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from functools import partial

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from ... import config as config_mod
from ... import reset as reset_mod
from ...adapters import SLOT_KINDS, apply_labels, institutes
from ...model import Kind
from ...sync import AlreadyRunning, refetch_flag, run_lock
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import AppContext, Ctx, accounts_of, language, redirect, tr
from ..i18n import N_, translate

log = logging.getLogger(__name__)
router = APIRouter()

# Credentials with a fixed form, by field name (the same in every adapter); the rest only by length.
CREDENTIAL_CHECKS = {
    "blz": inputs.blz,
    "server": inputs.https_url,
    "iban": lambda v, _label: inputs.iban(v),
    "savings_iban": lambda v, _label: inputs.iban(v),
}

MAX_STATEMENT = 5 * 1024 * 1024
STATEMENT_KINDS = [(Kind.DIVIDEND, N_("Dividenden")), (Kind.BUY, N_("Käufe/Einbuchungen")), (Kind.SELL, N_("Verkäufe")),
                   (Kind.UNKNOWN, N_("nicht automatisch buchbar"))]


def slot_in_use(ctx: AppContext, c: config_mod.Config, key: str) -> bool:
    """A bank in a fixed slot counts as set up once it has a config entry or anything in the vault."""
    return key in c.brokers or bool(ctx.vault.broker(key))


def free_slot(ctx: AppContext, c: config_mod.Config, kind: str) -> str | None:
    """The first slot of this kind (``fints`` / ``psd2``) nobody uses yet; the keys never change (CLAUDE.md)."""
    return next((a.key for a in SLOT_KINDS[kind] if a.key in ctx.adapters and not slot_in_use(ctx, c, a.key)), None)


@router.get("/brokers", response_class=HTMLResponse)
def brokers_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    # The fixed brokers always; a slot bank only once it is set up (no row of empty places).
    shown = {k: a for k, a in ctx.adapters.items() if not getattr(a, "slot_kind", None) or slot_in_use(ctx, c, k)}
    return ctx.render(request, "brokers.html", cfg=c, shown=shown, kinds=SLOT_KINDS,
                      full={kind: free_slot(ctx, c, kind) is None for kind in SLOT_KINDS})


@router.get("/brokers/add", response_class=HTMLResponse)
def broker_add_page(request: Request, ctx: Ctx, q: str = ""):
    """The dialog to add a bank over FinTS: a search in the banks' list."""
    if free_slot(ctx, ctx.cfg(), "fints") is None:
        return redirect(request, "/brokers", tr(request, "Alle Plätze dieser Art sind belegt."), "warn")
    return ctx.render(request, "broker_add.html", q=q.strip()[:60], found=institutes.search(q))


@router.post("/brokers/add")
def broker_add(request: Request, ctx: Ctx, bank: str = Form("")):
    """Takes the next free FinTS slot, names it and keeps the bank code for the setup page."""
    slot = free_slot(ctx, ctx.cfg(), "fints")
    if slot is None:
        return redirect(request, "/brokers", tr(request, "Alle Plätze dieser Art sind belegt."), "warn")
    try:
        blz = inputs.blz(bank, N_("Bankleitzahl"))
        known = institutes.lookup(blz)
        if known is None:
            raise inputs.Invalid(N_("Diese Bankleitzahl ist nicht in der Liste."))
    except inputs.Invalid as e:
        return redirect(request, "/brokers/add", e.text(language(request)), "error")
    name = known[0]
    ctx.update(lambda cfg: cfg.brokers.__setitem__(slot, config_mod.BrokerConfig(label=name[:80])))
    ctx.vault.set_broker_credentials(slot, {"blz": blz})
    apply_labels(ctx.adapters, ctx.cfg().brokers)
    ctx.event(request, "credentials", broker=slot, detail="added")
    return redirect(request, f"/brokers/{slot}", tr(request, "Platz belegt - jetzt Zugangsdaten und Konten eintragen."))


@router.get("/brokers/{key}", response_class=HTMLResponse)
def broker_page(request: Request, ctx: Ctx, key: str):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    stored = ctx.vault.broker(key).get("credentials", {})
    plain = {f.name: stored.get(f.name, "") for f in ctx.adapters[key].credential_fields if not f.secret}
    return _broker_form(request, ctx, key, ctx.cfg().broker(key), plain)


def _broker_form(request: Request, ctx: AppContext, key: str, b: config_mod.BrokerConfig, plain: dict[str, str],
                 errors: dict[str, str] | None = None) -> HTMLResponse:
    """The broker's form; after an error (#103) with what was typed - never a secret - and the error at its field."""
    adapter = ctx.adapters[key]
    accounts, error = ctx.wf_accounts(ctx.cfg())
    stored = ctx.vault.broker(key).get("credentials", {})
    response = ctx.render(request, "broker.html", key=key, adapter=adapter, b=b, accounts=accounts, wf_error=error,
                          stored={k: bool(v) for k, v in stored.items()}, plain=plain, errors=errors or {},
                          has_access=bool(ctx.vault.broker(key)),
                          today=inputs.today().isoformat(),
                          statement_rows=len(ctx.state.statement_rows(key)) if adapter.statement_label else 0)
    if errors:
        response.status_code = 422
    return response


@router.post("/brokers/{key}")
async def broker_save(request: Request, ctx: Ctx, key: str):
    adapters = ctx.adapters
    if key not in adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    form = await request.form()
    lang = language(request)
    errors: dict[str, str] = {}
    b = replace(ctx.cfg().broker(key), enabled=form.get("enabled") == "on")

    def field(name: str, check) -> str:
        raw = str(form.get(name, ""))
        try:
            return check(raw)
        except inputs.Invalid as e:
            errors[name] = e.text(lang)
            return raw

    account = partial(inputs.text, label=N_("Konto"), max_len=100)
    b.cash_account_id = field("cash_account_id", account)
    b.portfolio_account_id = field("portfolio_account_id", account)
    b.savings_account_id = field("savings_account_id", account) if adapters[key].savings_label else ""
    b.start_date = field("start_date", partial(inputs.iso_date, label=N_("Startdatum")))
    if b.enabled and (not b.cash_account_id or not b.portfolio_account_id):
        errors.setdefault("cash_account_id" if not b.cash_account_id else "portfolio_account_id",
                          tr(request, "Bitte ein Verrechnungs- und ein Depotkonto wählen."))
    elif b.enabled and b.cash_account_id == b.portfolio_account_id:
        errors.setdefault("portfolio_account_id",
                          tr(request, "Verrechnungs- und Depotkonto müssen verschiedene Konten sein."))
    if b.savings_account_id and b.savings_account_id in (b.cash_account_id, b.portfolio_account_id):
        errors.setdefault("savings_account_id", tr(request, "Das {label}-Konto muss ein eigenes Wealthfolio-Konto "
                                                            "sein, nicht das Verrechnungs- oder Depotkonto.",
                                                   label=adapters[key].savings_label))
    old = ctx.vault.broker(key).get("credentials", {})
    creds = _credentials(request, adapters[key], form, old, errors)
    adapter = adapters[key]
    b.consented = b.consented or form.get("consent") == "1"
    if adapter.consent and not b.consented and any(creds.values()):
        errors["consent"] = tr(request, "Bitte bestätige, dass du den Hinweis zur Schnittstelle gelesen hast.")
    if creds != old and (e := ctx.reconfirm(request, str(form.get("ui_password", "")))):
        errors["ui_password"] = e  # new credentials: the UI password again after a while (#96)
    if errors:  # checked completely before anything is saved: no half-saved form
        plain = {f.name: creds.get(f.name, "")[:inputs.MAX_TEXT] for f in adapters[key].credential_fields
                 if not f.secret}
        return _broker_form(request, ctx, key, b, plain, errors)
    ctx.update(lambda c: c.brokers.__setitem__(key, b))
    if creds != old:
        ctx.vault.set_broker_credentials(key, creds)
        ctx.event(request, "credentials", broker=key)
        from .broker_login import start_login  # this POST starts the login with the new credentials (#99)

        request.session["flash"] = {"text": tr(request, "Gespeichert. Jetzt beim Broker anmelden."), "level": "ok"}
        # The login talks to the bank for up to minutes: in the thread pool, never on the event loop.
        return await run_in_threadpool(start_login, request, ctx, key, False)
    return redirect(request, "/broker", tr(request, "Gespeichert."))


def _credentials(request: Request, adapter, form, old: dict, errors: dict[str, str]) -> dict:
    """The form's credentials, checked; an empty secret keeps the stored one."""
    creds = {}
    for f in adapter.credential_fields:
        v = str(form.get(f"cred_{f.name}", ""))
        if len(v) > inputs.MAX_TEXT:
            errors[f"cred_{f.name}"] = tr(request, "{label}: höchstens {max_len} Zeichen.", label=f.label,
                                          max_len=inputs.MAX_TEXT)
        elif v and f.name in CREDENTIAL_CHECKS:
            try:
                v = CREDENTIAL_CHECKS[f.name](v, f.label)
            except inputs.Invalid as e:
                errors[f"cred_{f.name}"] = e.text(language(request))
        creds[f.name] = v if v or not f.secret else old.get(f.name, "")
        if f.ask and form.get(f"forget_{f.name}"):
            creds[f.name] = ""  # typed at every login from now on, never stored (#85)
    return creds


@router.post("/brokers/{key}/forget")
def broker_forget(request: Request, ctx: Ctx, key: str, ui_password: str = Form("")):
    """Delete the broker's credentials and session from the vault (#152); its config and bookings stay, the
    automatic fetch is switched off."""
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    if error := ctx.reconfirm(request, ui_password):
        return redirect(request, f"/brokers/{key}", error, "error")
    old = ctx.pending.pop(key, None)
    if old is not None:
        old.close()
    ctx.pins.pop(key, None)
    ctx.vault.update(lambda d: d.get("brokers", {}).pop(key, None))

    def off(c: config_mod.Config) -> None:
        if key in c.brokers:
            c.brokers[key].enabled = False

    ctx.update(off)
    ctx.event(request, "credentials", broker=key, detail="deleted")
    log.info("%s: credentials and session deleted", key)
    return redirect(request, f"/brokers/{key}", tr(request, "Zugangsdaten und Anmeldung gelöscht. Der automatische "
                                                            "Abruf ist aus."))


@router.post("/brokers/{key}/refetch")
def broker_refetch(request: Request, ctx: Ctx, key: str):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    ctx.state.set_flag(refetch_flag(key))
    start = ctx.cfg().broker(key).start_date
    if start:
        text = tr(request, "Der nächste Abruf holt noch einmal alles ab {start}. Schon übernommene Buchungen werden "
                           "nicht doppelt angelegt.", start=start)
    else:
        text = tr(request, "Der nächste Abruf holt noch einmal alles, was der Broker liefert. Schon übernommene "
                           "Buchungen werden nicht doppelt angelegt.")
    return redirect(request, f"/brokers/{key}", text)


@router.post("/brokers/{key}/statement")
async def broker_statement(request: Request, ctx: Ctx, key: str):
    """A statement the user exported from the broker (Deutsche Bank depot CSV): keep its rows for the runs."""
    adapters = ctx.adapters
    if key not in adapters or not adapters[key].statement_label:
        return redirect(request, "/brokers", tr(request, "Für diesen Broker gibt es keinen Datei-Import."), "error")
    file = (await request.form()).get("file")
    if not isinstance(file, StarletteUpload):
        return redirect(request, f"/brokers/{key}", tr(request, "Bitte eine Datei auswählen."), "error")
    data = await file.read(MAX_STATEMENT + 1)
    if len(data) > MAX_STATEMENT:
        return redirect(request, f"/brokers/{key}", tr(request, "Die Datei ist zu groß (höchstens 5 MB)."), "error")
    try:
        rows = adapters[key].parse_statement(data)
        # Read it as the runs will, now: a row they can't read must not reach the state, or every run of
        # this broker fails from then on.
        adapters[key].statement_transactions(rows, None)
    except ValueError as e:
        return redirect(request, f"/brokers/{key}", str(e), "error")
    new = ctx.state.add_statement_rows(key, rows)
    kinds = Counter(t.kind for t in adapters[key].statement_transactions(rows, None))
    summary = ", ".join(f"{n} {tr(request, label)}" for kind, label in STATEMENT_KINDS if (n := kinds.get(kind)))
    log.info("%s: statement uploaded, %d rows (%d new)", key, len(rows), new)
    return redirect(request, f"/brokers/{key}", tr(request, "{rows} Zeilen gelesen ({summary}), {new} davon neu. Der "
                                                           "nächste Abruf bucht sie; nicht Buchbares steht dann unter "
                                                           "„Unbekannte Buchungen“.",
                                                   rows=len(rows), summary=summary, new=new))


@router.get("/brokers/{key}/reset", response_class=HTMLResponse)
def broker_reset_page(request: Request, ctx: Ctx, key: str):
    """*Neu aufsetzen*, first step (#110): how many bookings it deletes; deleting needs the broker's name."""
    return _reset_page(request, ctx, key)


def _reset_page(request: Request, ctx: AppContext, key: str, errors: dict[str, str] | None = None):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    c = ctx.cfg()
    b = c.broker(key)
    if not b.cash_account_id or not b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}", tr(request, "Erst die beiden Wealthfolio-Konten wählen."), "error")
    count, error = 0, None
    try:
        with ctx.wf_client(c) as wf:
            count = len(reset_mod.own_activities(wf, key, accounts_of(b)))
    except WealthfolioError as e:
        error = str(e)
    response = ctx.render(request, "broker_reset.html", key=key, adapter=ctx.adapters[key], count=count,
                          wf_error=error, errors=errors or {})
    if errors:
        response.status_code = 422
    return response


@router.post("/brokers/{key}/reset")
def broker_reset(request: Request, ctx: Ctx, key: str, name: str = Form(""), ui_password: str = Form("")):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    label = ctx.adapters[key].label
    errors = {}
    if name.strip().casefold() != label.casefold():
        errors["name"] = tr(request, "Bitte genau „{label}“ eingeben.", label=label)
    if error := ctx.reconfirm(request, ui_password):
        errors["ui_password"] = error
    if errors:
        return _reset_page(request, ctx, key, errors)
    b = ctx.cfg().broker(key)
    if not b.cash_account_id or not b.portfolio_account_id:
        return redirect(request, f"/brokers/{key}", tr(request, "Erst die beiden Wealthfolio-Konten wählen."), "error")
    from .overview import start_job

    ctx.event(request, "reset", broker=key)

    def work(lang: str) -> tuple[str, str]:
        try:
            with run_lock(ctx.data_dir, wait=5), ctx.wf_client(ctx.cfg()) as wf:
                deleted, failures = reset_mod.reset(wf, ctx.state, key, accounts_of(b))
        except AlreadyRunning:
            return translate("Gerade läuft ein Abruf - bitte gleich noch einmal.", lang), "warn"
        except WealthfolioError as e:
            return translate("Wealthfolio nicht erreichbar: {error}", lang, error=e), "error"
        if failures:
            return translate("{deleted} Buchungen gelöscht, {failed} nicht: {errors} - bitte noch einmal versuchen.",
                             lang, deleted=deleted, failed=len(failures), errors="; ".join(failures[:3])), "error"
        log.info("%s: reset, %d activities deleted", key, deleted)
        return translate("{deleted} Buchungen des Dienstes gelöscht. Der nächste Abruf holt alles ab dem Startdatum "
                         "neu.", lang, deleted=deleted), "ok"

    return start_job(request, ctx, N_("Neu aufsetzen"), work, f"/brokers/{key}")


@router.get("/brokers/{key}/remove", response_class=HTMLResponse)
def broker_remove_page(request: Request, ctx: Ctx, key: str):
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    return ctx.render(request, "broker_remove.html", key=key, adapter=ctx.adapters[key], errors={})


@router.post("/brokers/{key}/remove")
def broker_remove(request: Request, ctx: Ctx, key: str, name: str = Form(""), ui_password: str = Form("")):
    """Takes a broker out: settings, saved credentials and session, sync state. Its activities stay in Wealthfolio."""
    if key not in ctx.adapters:
        return redirect(request, "/brokers", tr(request, "Unbekannter Broker."), "error")
    label = ctx.adapters[key].label
    errors = {}
    if name.strip().casefold() != label.casefold():
        errors["name"] = tr(request, "Bitte genau „{label}“ eingeben.", label=label)
    if error := ctx.reconfirm(request, ui_password):
        errors["ui_password"] = error
    if errors:
        response = ctx.render(request, "broker_remove.html", key=key, adapter=ctx.adapters[key], errors=errors)
        response.status_code = 422
        return response
    try:
        with run_lock(ctx.data_dir, wait=5):
            ctx.update(lambda cfg: cfg.brokers.pop(key, None) and None)
            ctx.vault.update(lambda d: d.get("brokers", {}).pop(key, None))
            ctx.state.forget_broker(key)
    except AlreadyRunning:
        return redirect(request, f"/brokers/{key}", tr(request, "Gerade läuft ein Abruf - bitte gleich noch einmal."),
                        "warn")
    ctx.pins.pop(key, None)
    apply_labels(ctx.adapters, ctx.cfg().brokers)
    ctx.event(request, "credentials", broker=key, detail="removed")
    return redirect(request, "/brokers", tr(request, "{label} entfernt. Die Buchungen in Wealthfolio bleiben.",
                                            label=label))
