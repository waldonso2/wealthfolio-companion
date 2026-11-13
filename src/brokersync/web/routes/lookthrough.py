"""The page *X-Ray* (waldonso2/wealthfolio-importer-addon#139): every stock across the depot's funds,
how the funds overlap, and the cluster risks over the user's limits.

The positions of all Wealthfolio accounts are read in the background (#104) and kept for ``READ_SECONDS``.
A fund counts as one once its holdings list is there - fetched from its issuer (``lookthrough.fetch``, opt-in)
or uploaded; until then it is one line of its own, never guessed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from ... import lookthrough, meta_keys, xray
from ...sync import AlreadyRunning
from ...wealthfolio import WealthfolioError
from .. import inputs
from ..context import AppContext, Ctx, language, redirect, tr
from ..i18n import N_, translate
from ..views import money

log = logging.getLogger(__name__)
router = APIRouter()

READ_SECONDS = 10 * 60
MAX_FILE = 5 * 1024 * 1024
# The limits' form fields: (key of xray.LIMITS, label, a share in percent?)
LIMIT_FIELDS = [("stock", N_("Einzelaktie über (%)"), True), ("country", N_("Land über (%)"), True),
                ("sector", N_("Sektor über (%)"), True), ("currency", N_("Fremdwährung über (%)"), True),
                ("overlap", N_("Überschneidung zweier ETFs über (%)"), True),
                ("effective", N_("Effektive Titelzahl unter"), False),
                ("cash", N_("Bargeld je Konto über (EUR)"), False),
                ("cost", N_("Gesamtkostenquote über (% im Jahr)"), True)]
KIND = {"stock": N_("Einzelaktie"), "country": N_("Land"), "sector": N_("Sektor"), "currency": N_("Fremdwährung"),
        "overlap": N_("Überschneidung"), "effective": N_("Wenige Titel"),
        "cash": N_("Bargeld über der Einlagensicherung"), "cost": N_("Gesamtkostenquote")}
# A broker's fee model (costs.Model): (field, label, a share in percent?, maximum)
COST_FIELDS = [("fixed", N_("Ordergebühr je Verkauf (EUR)"), False, Decimal(1000)),
               ("percent", N_("Ordergebühr je Verkauf (% des Werts)"), True, Decimal(10)),
               ("spread", N_("Spread je Kauf und Verkauf (%)"), True, Decimal(10)),
               ("custody", N_("Depotgebühr im Jahr (EUR)"), False, Decimal(100000))]


def _read(ctx: AppContext, c) -> None:
    error = None
    try:
        with ctx.wf_client(c) as wf:
            depot = lookthrough.read_depot(wf, ctx.syncer.learned_isins(c))
    except WealthfolioError as e:
        depot, error = lookthrough.Depot(), str(e)
    ctx.depot, ctx.depot_error, ctx.depot_at = depot, error, time.time()


def _typed(raw: str | None, percent: bool, lang: str) -> str:
    """A saved number as the user typed it, without rounding (a TER of 0,075 stays 0,075)."""
    if not raw:
        return ""
    text = format((Decimal(raw) * (100 if percent else 1)).normalize(), "f")
    return text if lang == "en" else text.replace(".", ",")


def _position(ctx: AppContext, asset_id: str) -> dict | None:
    return next((p for p in ctx.depot.positions if p["id"] == asset_id), None)


TOP = 10  # rows shown at first under each fund; the rest folded
TOPS = ("10", "25", "50", "100", "all")  # the overall matrix: the user's choice; the page renders only these rows
COLUMNS = 4  # the largest funds get a column of the matrix of their own, the others share "weitere ETFs"


def _matrix(rows: list[xray.Exposure], funds: list[xray.Holding], total: Decimal) -> list[tuple]:
    """Per stock: its row, EUR through each of the ``COLUMNS`` largest funds, through the other funds, and held
    directly (single stocks and funds without a list)."""
    columns = [h.id for h in funds[:COLUMNS]]
    others = {h.id for h in funds[COLUMNS:]}
    out = []
    for r in rows:
        via = {k: share * total for k, share in r.via.items()}
        out.append((r, [via.get(k) for k in columns], sum((v for k, v in via.items() if k in others), Decimal(0)),
                    sum((v for k, v in via.items() if k not in others and k not in columns), Decimal(0))))
    return out


def read_soon(request: Request, ctx: AppContext) -> None:
    """The depot's positions read again in the background when older than ``READ_SECONDS``."""
    if time.time() - ctx.depot_at > READ_SECONDS:
        from .overview import launch

        launch(ctx, N_("X-Ray"), lambda lang: _read(ctx, ctx.cfg()), language(request))


@router.get("/lookthrough", response_class=HTMLResponse)
def lookthrough_page(request: Request, ctx: Ctx, top: str = TOPS[0], fund: str = ""):
    read_soon(request, ctx)
    c = ctx.cfg()
    depot, state = ctx.depot, ctx.state
    funds = state.funds()
    holdings = lookthrough.holdings(state, depot)
    rows = xray.exposures(holdings)
    with_list = sorted((h for h in holdings if h.id in funds), key=lambda h: -h.value)
    total = sum((p["value"] for p in depot.positions), Decimal(0))
    limits = lookthrough.limits(c.xray_limits)
    accepted = lookthrough.accepted(state)
    cost = lookthrough.cost(state, depot, c)
    found = xray.findings(holdings, limits, depot.base, depot.cash, cost.rate)
    status = {p["id"]: lookthrough.status(state, p["id"]) for p in depot.positions}
    etfs = [p for p in depot.positions if p["id"] in funds or lookthrough.is_fund(p, status[p["id"]])]
    pending = [p for p in etfs if p["id"] not in funds]
    # The matrix holds stocks only: a fund still without its list is named below it, never a row of its own.
    waiting = {p["isin"] or p["id"] for p in pending}
    stocks = [r for r in rows if r.key != xray.UNASSIGNED and r.key not in waiting]
    top = top if top in TOPS else TOPS[0]
    return ctx.render(
        request, "lookthrough.html", wf_error=ctx.depot_error, positions=depot.positions, funds=funds,
        status=status, etfs=etfs, pending=pending, singles=len(depot.positions) - len(etfs),
        issuers={p["id"]: lookthrough.issuer(p, status[p["id"]]) for p in etfs},
        auto={"iShares", *lookthrough.FETCHERS}, last=lookthrough.last_pass(state),
        total=total, top=TOP, tops=TOPS, top_choice=top, matrix_top=len(stocks) if top == "all" else int(top),
        matrix_open="top" in request.query_params, full=fund if fund in funds else "",
        matrix=_matrix(stocks, with_list, total), columns=with_list[:COLUMNS],
        more_funds=len(with_list) > COLUMNS, count=len(stocks),
        largest={k: sorted(f["constituents"], key=lambda c: -Decimal(c[2])) for k, f in funds.items()},
        countries={k: any(c[3] for c in f["constituents"]) for k, f in funds.items()},  # iShares' file names none
        unassigned=next((r for r in rows if r.key == xray.UNASSIGNED), None),
        names={h.id: h.name for h in holdings}, effective=xray.effective_count(rows), fund_list=with_list,
        overlap={(a.id, b.id): xray.overlap(a, b) for a, b in combinations(with_list, 2)},
        own={h.id: xray.own_share(h, holdings) for h in with_list},
        risks=[f for f in found if f.key not in accepted], accepted=[f for f in found if f.key in accepted],
        kind=KIND, cfg=c, limit_fields=LIMIT_FIELDS, limits=limits, base=depot.base, cost=cost,
        ters={p["id"]: lookthrough.ter(lookthrough.status(state, p["id"])) for p in depot.positions},
        cost_fields=COST_FIELDS, brokers=[k for k in c.brokers if k in ctx.adapters],
        typed=lambda raw, percent: _typed(raw, percent, language(request)),
        reading=ctx.running.is_set() and ctx.job in (N_("X-Ray"), N_("Positionslisten abrufen")),
        progress=json.loads(state.meta(meta_keys.FUNDS_PROGRESS()) or "{}") if ctx.running.is_set() else {},
        read_at=datetime.fromtimestamp(ctx.depot_at, UTC) if ctx.depot_at else None)


@router.post("/lookthrough/refresh")
def lookthrough_refresh(request: Request, ctx: Ctx):
    from .overview import start_job

    ctx.depot_at = 0.0
    return start_job(request, ctx, N_("X-Ray"), lambda lang: _read(ctx, ctx.cfg()), "/lookthrough")


@router.post("/lookthrough/settings")
async def lookthrough_settings(request: Request, ctx: Ctx):
    """The limits of the cluster risks - the whole form checked before anything is saved. Writing countries and
    sectors into Wealthfolio is the service's page now (/services/taxonomies, #151)."""
    form = await request.form()
    saved: dict[str, str] = {}
    try:
        for key, label, percent in LIMIT_FIELDS:
            value = str(form.get(key) or "").strip()
            if not value:
                continue
            number = inputs.decimal(value, label, minimum=Decimal(0),
                                    maximum=Decimal(100) if percent else Decimal("1e9"))
            saved[key] = str(number / 100 if percent else number)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")

    ctx.update(lambda c: setattr(c, "xray_limits", saved))
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/accept")
def lookthrough_accept(request: Request, ctx: Ctx, key: str = Form(...), action: str = Form("accept")):
    """A cluster risk the user holds on purpose: no longer listed as one nor notified - or listed again."""
    try:
        key = inputs.text(key, N_("Meldung"), max_len=400)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    keys = lookthrough.accepted(ctx.state)
    keys = keys - {key} if action == "undo" else keys | {key}
    ctx.state.set_meta(meta_keys.XRAY_ACCEPTED(), json.dumps(sorted(keys)))
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/costs")
async def lookthrough_costs(request: Request, ctx: Ctx):
    """The brokers' fee models, the whole form checked before anything is saved; an empty field: unknown."""
    form = await request.form()
    c = ctx.cfg()
    models: dict[str, dict[str, str]] = {}
    try:
        for key in (k for k in c.brokers if k in ctx.adapters):
            for name, label, percent, high in COST_FIELDS:
                value = _plain(str(form.get(f"{key}:{name}") or "").strip())
                if value:
                    number = inputs.decimal(value, label, minimum=Decimal(0), maximum=high)
                    models.setdefault(key, {})[name] = str(number / 100 if percent else number)
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    ctx.update(lambda c: setattr(c, "cost_models", models))
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/ter")
def lookthrough_ter(request: Request, ctx: Ctx, asset_id: str = Form(...), ter: str = Form("")):
    """A position's TER typed by the user, used before the issuer's; empty: the issuer's again."""
    if _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    try:
        value = inputs.decimal(ter.strip(), N_("Laufende Kosten (TER, % im Jahr)"), minimum=Decimal(0),
                               maximum=Decimal(5)) if ter.strip() else None
    except inputs.Invalid as e:
        return redirect(request, "/lookthrough", e.text(language(request)), "error")
    lookthrough.set_status(ctx.state, asset_id, ter_manual=str(value / 100) if value is not None else "")
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/source")
def lookthrough_source(request: Request, ctx: Ctx, asset_id: str = Form(...), url: str = Form("")):
    """The iShares product page of a fund, or L&G's address of its holdings CSV, so the fetch finds its holdings;
    empty: forget it."""
    if _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    url = url.strip()
    if url and (len(url) > 300 or not (lookthrough.ishares_page(url) or lookthrough.lgim_doc(url))):
        return redirect(request, "/lookthrough", tr(request, "Das ist keine Produktseite von iShares "
                                                             "(https://www.ishares.com/…/products/<Nummer>/<Name>) und "
                                                             "keine Positionsliste von L&G "
                                                             "(https://fundcentres.lgim.com/srp/documents-id/…)."),
                        "error")
    lookthrough.set_status(ctx.state, asset_id, url=url, status="")  # due at the next fetch
    return redirect(request, "/lookthrough", tr(request, "Gespeichert."))


@router.post("/lookthrough/fetch")
def lookthrough_fetch(request: Request, ctx: Ctx, asset_id: str = Form("")):
    """*Jetzt abrufen*: every fund's holdings list now, or one position's (``asset_id``), in the background."""
    if asset_id and _position(ctx, asset_id) is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    from .overview import start_job

    def work(lang: str) -> tuple[str, str]:
        try:
            result = ctx.syncer.update_funds(only=asset_id)
        except AlreadyRunning:
            return translate("Ein Abruf läuft gerade - bitte gleich noch einmal.", lang), "warn"
        ctx.depot_at = 0.0
        if result is None:
            return translate("Die Positionslisten konnten nicht geholt werden - siehe Protokoll.", lang), "error"
        text = translate("{fetched} Positionslisten geholt, {none} ohne Quelle (hochladen), {failed} fehlgeschlagen.",
                         lang, fetched=result.fetched, none=result.none, failed=len(result.errors))
        return text, "warn" if result.errors else "ok"

    return start_job(request, ctx, N_("Positionslisten abrufen"), work, "/lookthrough")


@router.post("/lookthrough/upload")
async def lookthrough_upload(request: Request, ctx: Ctx):
    """A fund's holdings file from its issuer (CSV or XLSX): its lines replace what was kept before."""
    form = await request.form()
    asset_id = str(form.get("asset_id") or "")
    position = _position(ctx, asset_id)
    if position is None:
        return redirect(request, "/lookthrough", tr(request, "Unbekannte Position - bitte die Seite neu laden."),
                        "error")
    file = form.get("file")
    if not isinstance(file, StarletteUpload):
        return redirect(request, "/lookthrough", tr(request, "Bitte eine Datei auswählen."), "error")
    data = await file.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        return redirect(request, "/lookthrough", tr(request, "Die Datei ist zu groß (höchstens 5 MB)."), "error")
    try:
        lines = await run_in_threadpool(lookthrough.parse_file, data)  # never on the event loop: the UI stays
    except (ValueError, lookthrough.SourceError) as e:
        return redirect(request, "/lookthrough", tr(request, str(e)), "error")
    ctx.state.set_fund(asset_id, position["name"], lookthrough.rows(lines))
    lookthrough.set_status(ctx.state, asset_id, status="upload", source="Upload", error="", lines=len(lines))
    total = sum((c.weight for c in lines), Decimal(0)) * 100
    log.info("look-through: holdings file of %s, %d lines", asset_id, len(lines))
    text = tr(request, "{name}: {n} Positionen gelesen, zusammen {sum} % des Fonds.", name=position["name"],
              n=len(lines), sum=money(total, language(request)))
    return redirect(request, "/lookthrough", text)


def _plain(value: str) -> str:
    """Thousands separators out ("30.000", "30,000.50"): ``inputs.decimal`` takes "," and "." as decimal mark."""
    value = value.replace(" ", "").replace("\xa0", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", value):
        return value.replace(".", "")
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", value):
        return value.replace(",", "")
    return value
