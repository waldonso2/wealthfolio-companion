"""The page *Prüfung*: account check (read only), gaps to book again or ignore, opening positions."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import audit, meta_keys
from ...sync import opening_id, refetch_flag
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import AppContext, Ctx, accounts_of, language, redirect, tr
from ..i18n import N_

router = APIRouter()


AUDIT_SECONDS = 10 * 60


@router.get("/check", response_class=HTMLResponse)
def check_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    adapters, state = ctx.adapters, ctx.state
    if time.time() - ctx.audits_at > AUDIT_SECONDS:
        # The account check reads every activity: in the background (#104), the page shows the last result.
        from .overview import launch

        launch(ctx, N_("Kontenprüfung"), lambda lang: _audit(ctx, c), language(request))
    audits, names, error = ctx.audits, ctx.account_names, ctx.audit_error
    # Everything about a broker in one card (#83): gaps, opening positions and the account check.
    brokers = []
    cash_name, depot_name = tr(request, "Verrechnungskonto"), tr(request, "Depotkonto")  # an account Wealthfolio lacks
    for k, b in c.brokers.items():
        if k not in adapters or not b.cash_account_id or not b.portfolio_account_id:
            continue
        synced = state.synced(k)
        gaps = state.gaps(k)
        result = audits.get(k)
        missing = missing_positions(ctx, k)
        entry: dict = {"key": k, "label": adapters[k].label, "full": adapters[k].full_history, "gaps": gaps,
                 "checked": bool(state.last_run(k)), "missing": missing,
                 "openings": [{**o, "booked": opening_id(o["isin"], o["day"]) in synced} for o in state.openings(k)],
                 "audit": result, "cash_name": names.get(b.cash_account_id, cash_name),
                 "depot_name": names.get(b.portfolio_account_id, depot_name), "depot_id": b.portfolio_account_id}
        open_gaps = sum(1 for g in gaps if g["kind"] in ("missing", "partial", "orphan"))
        audit_issues = (len(result.moments) + len(result.lonely)) if result else 0
        entry["issues"] = open_gaps + len(missing) + audit_issues
        brokers.append(entry)
    return ctx.render(request, "check.html", wf_error=error, source=audit.source, effect=audit.cash_effect,
                      brokers=brokers, classes=json.loads(state.meta(meta_keys.ASSET_CLASSES()) or "null"),
                      audited_at=datetime.fromtimestamp(ctx.audits_at, UTC) if ctx.audits_at else None,
                      auditing=ctx.running.is_set() and ctx.job == N_("Kontenprüfung"))


def _audit(ctx: AppContext, c) -> None:
    """The account check of every broker, kept for the page (``AUDIT_SECONDS``)."""
    audits, names, error = {}, {}, None
    try:
        with ctx.wf_client(c) as wf:
            names = {a.id: a.name for a in wf.list_accounts()}
            for key, b in c.brokers.items():
                if key in ctx.adapters and b.cash_account_id and b.portfolio_account_id:
                    audits[key] = audit.run(wf, accounts_of(b))
    except WealthfolioError as e:
        error = str(e)
    ctx.audits, ctx.account_names, ctx.audit_error, ctx.audits_at = audits, names, error, time.time()


@router.post("/check/audit")
def check_audit(request: Request, ctx: Ctx):
    """*Neu prüfen*: the account check now, in the background."""
    from .overview import start_job

    ctx.audits_at = 0.0
    return start_job(request, ctx, N_("Kontenprüfung"), lambda lang: _audit(ctx, ctx.cfg()), "/check")


def missing_positions(ctx: AppContext, key: str) -> list[dict]:
    """Positions the broker holds more of than Wealthfolio (last check), pre-filled for an opening position."""
    state = ctx.state
    if not ctx.adapters[key].reports_positions:
        return []
    known = {p["isin"]: p for p in json.loads(state.meta(meta_keys.POSITIONS(key)) or "[]")}
    entered = {o["isin"] for o in state.openings(key)}
    out = []
    for d in (state.reconcile(key) or {}).get("deviations", []):
        if d["kind"] != "position" or ":" in d["key"] or d["key"] in entered:
            continue
        try:
            missing = Decimal(d["broker"]) - Decimal(d["wealthfolio"])
        except InvalidOperation:
            continue
        if missing > 0:
            p = known.get(d["key"], {})
            out.append({"isin": d["key"], "name": d["name"], "shares": format(missing.normalize(), "f"),
                        "price": p.get("cost") or "", "currency": p.get("currency") or "EUR"})
    return out


@router.post("/check/{key}/opening")
async def check_opening(request: Request, ctx: Ctx, key: str):
    """An opening position: bought on a day before the start date; the next run books it."""
    state = ctx.state
    if key not in ctx.adapters:
        return redirect(request, "/check", tr(request, "Unbekannter Broker."), "error")
    form = await request.form()
    isin = str(form.get("isin", "")).strip().upper()
    day = str(form.get("day", "")).strip()
    if form.get("action") != "delete":
        try:
            isin = inputs.security_id(isin)
        except inputs.Invalid as e:
            return redirect(request, "/check", e.text(language(request)), "error")
    if form.get("action") == "delete":
        if opening_id(isin, day) not in state.synced(key):
            state.delete_opening(key, isin, day)
        return redirect(request, "/check", tr(request, "Anfangsbestand entfernt."))
    try:
        shares = inputs.decimal(str(form.get("shares", "")), N_("Stückzahl"))
        price = inputs.decimal(str(form.get("price", "")), N_("Kurs"))
        fee = inputs.decimal(str(form.get("fee") or "0"), N_("Gebühr"))
        bought = date.fromisoformat(day)
        name = inputs.text(str(form.get("name") or ""), N_("Name"), max_len=200)
    except ValueError:  # inputs.Invalid too
        return redirect(request, "/check", tr(request, "Bitte Kaufdatum, Stückzahl, Kurs und Gebühr als Zahlen "
                                                       "angeben."), "error")
    if shares <= 0 or price <= 0 or fee < 0 or bought > inputs.today() or not isin:
        return redirect(request, "/check", tr(request, "Stückzahl und Kurs müssen größer 0 sein, die Gebühr "
                                                       "mindestens 0, das Kaufdatum darf nicht in der Zukunft liegen."),
                        "error")
    known = {p["isin"]: p for p in json.loads(state.meta(meta_keys.POSITIONS(key)) or "[]")}
    p = known.get(isin, {})
    name = name or p.get("name") or isin
    state.add_opening(key, isin, bought.isoformat(), name,
                      str(shares), str(price), str(fee), p.get("currency") or "EUR")
    return redirect(request, "/check", tr(request, "Anfangsbestand {isin} gespeichert - der nächste Abruf bucht ihn "
                                                   "als Kauf am {day} samt Einzahlung des Kaufbetrags.",
                                          isin=isin, day=f"{bought:%d.%m.%Y}"))


@router.post("/check/{key}")
async def check_decide(request: Request, ctx: Ctx, key: str):
    """The user's decision on transactions missing in Wealthfolio: book them again, or ignore them."""
    state = ctx.state
    if key not in ctx.adapters:
        return redirect(request, "/check", tr(request, "Unbekannter Broker."), "error")
    form = await request.form()
    action = str(form.get("action", ""))
    open_refs = {g["ref"] for g in state.gaps(key) if g["kind"] != "orphan"}
    tx_ids = [str(t) for t in form.getlist("tx") if str(t) in open_refs]
    if not tx_ids:
        return redirect(request, "/check", tr(request, "Keine Buchung ausgewählt."), "warn")
    if action == "ignore":
        state.ignore(key, tx_ids)
        return redirect(request, "/check", tr(request, "{n} Buchung(en) werden nicht mehr gebucht.", n=len(tx_ids)))
    if action != "rebook":
        return redirect(request, "/check", tr(request, "Unbekannte Aktion."), "error")
    state.forget(key, tx_ids)
    if not ctx.adapters[key].full_history:
        state.set_flag(refetch_flag(key))  # they may lie before the next run's window
    return redirect(request, "/check", tr(request, "{n} Buchung(en) legt der nächste Abruf wieder an – außer sie "
                                                   "sind inzwischen in Wealthfolio (z. B. aus einem Import).",
                                          n=len(tx_ids)))
