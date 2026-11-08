"""The page *Dienste* (#145), the start page: what the service writes into Wealthfolio, each switched on and off;
and one page per service (#151, ``/services/<key>``): head with the switch, status, settings, actions.

A service that still lacks something (Wealthfolio, a broker, a key) is not switched on: the user is sent to its
page, which says what is missing, and it turns on by itself once that is saved (``settle``, called on every page
view).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ... import assets as assets_mod
from ... import config as config_mod
from ... import logos as logos_mod
from ... import lookthrough, meta_keys, xray_addon
from ...sync import AlreadyRunning
from ...wealthfolio import WealthfolioError
from ..context import AppContext, Ctx, redirect, tr
from ..i18n import N_, translate

router = APIRouter()

# (key, name, what it does in Wealthfolio)
SERVICES = [
    ("sync", N_("Broker-Sync"), N_("bucht die Umsätze deiner Broker und Banken in Wealthfolio")),
    ("taxonomies", N_("Länder & Sektoren"),
     N_("trägt Länder und Sektoren der ETFs in Wealthfolio ein; deine eigenen Zuordnungen bleiben")),
    ("isin", N_("ISIN ergänzen"), N_("trägt bei Wertpapieren ohne ISIN die ISIN in Wealthfolio ein")),
    ("logos", N_("Logos"), N_("setzt die Logos der Wertpapiere von logo.dev in Wealthfolio")),
    ("spending", N_("Ausgaben-Tracker"),
     N_("nimmt die Verrechnungskonten deiner Banken in Wealthfolios Ausgaben-Auswertung auf")),
    ("xray", N_("X-Ray in Wealthfolio"),
     N_("legt die Fondslisten für das X-Ray-Addon in Wealthfolio ab; Beträge bleiben in Wealthfolio")),
]
NAMES = {key: name for key, name, _ in SERVICES}
TEXTS = {key: text for key, _, text in SERVICES}
BUSY = N_("Ein Abruf läuft gerade - bitte gleich noch einmal.")


def missing(ctx: AppContext, key: str, c: config_mod.Config) -> tuple[str, str] | None:
    """What service ``key`` still needs before it can run: (message, page), else None."""
    secrets_ = ctx.vault.load()
    if not secrets_.get("wealthfolio_checked"):
        return N_("Erst Wealthfolio verbinden."), "/setup/wealthfolio"
    brokers = secrets_.get("brokers", {})
    if key == "sync" and not any(b.get("credentials") for b in brokers.values()):
        return N_("Erst einen Broker einrichten."), "/brokers"
    if key == "spending" and not any(b.spending and b.cash_account_id for b in c.brokers.values()):
        return N_("Erst unten ein Verrechnungskonto wählen."), "/services/spending#accounts"
    if key == "logos" and not secrets_.get("logo_dev_token"):
        return N_("Erst unten einen logo.dev-Schlüssel eintragen."), "/services/logos#token"
    return None


def settle(request: Request, ctx: AppContext) -> None:
    """A service the user switched on while it lacked something: on now, if the settings were saved since."""
    key = request.session.get("enable_service")
    if key not in NAMES:
        return
    c = ctx.cfg()
    if missing(ctx, key, c):
        return
    request.session.pop("enable_service")
    ctx.update(lambda c: c.set_service(key, True))
    request.session["flash"] = {"text": tr(request, "{name} ist eingeschaltet.", name=tr(request, NAMES[key])),
                                "level": "ok"}


@router.get("/", response_class=HTMLResponse)
def services_page(request: Request, ctx: Ctx):
    c = ctx.cfg()
    services = [{"key": key, "name": name, "text": text, "on": c.service(key)} for key, name, text in SERVICES]
    xray = json.loads(ctx.state.meta(meta_keys.XRAY_ADDON_STATUS()) or "{}")  # the last write for the addon
    return ctx.render(request, "services.html", services=services,
                      pending=request.session.get("enable_service"), xray=xray)


@router.post("/services/{key}")
def services_switch(request: Request, ctx: Ctx, key: str, back: str = Form("")):
    if key not in NAMES:
        return redirect(request, "/", tr(request, "Unbekannter Dienst."), "error")
    page = f"/services/{key}"
    to = page if back == "page" else "/"
    c = ctx.cfg()
    name = tr(request, NAMES[key])
    if c.service(key):
        ctx.update(lambda c: c.set_service(key, False))
        if request.session.get("enable_service") == key:
            request.session.pop("enable_service")
        return redirect(request, to, tr(request, "{name} ist ausgeschaltet.", name=name))
    lacking = missing(ctx, key, c)
    if lacking:
        request.session["enable_service"] = key
        return redirect(request, page, tr(request, "{what} Danach schaltet sich {name} ein.",
                                          what=tr(request, lacking[0]), name=name), "warn")
    ctx.update(lambda c: c.set_service(key, True))
    request.session.pop("enable_service", None)
    return redirect(request, to, tr(request, "{name} ist eingeschaltet.", name=name))


# ── the pages ────────────────────────────────────────────────────────────────
def _sync(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    from .overview import _brokers

    brokers = [b for b in _brokers(ctx, c, ctx.vault.load()) if b["config"].enabled or b["has_credentials"]]
    return {"brokers": brokers, "safe_mode": c.safe_mode}


def _taxonomies(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    funds = []
    for asset_id, f in sorted(ctx.state.funds().items(), key=lambda kv: kv[1]["name"].lower()):
        st = lookthrough.status(ctx.state, asset_id)
        ff = st.get("from_finanzfluss") or []
        countries = "finanzfluss" if "regions" in ff else ("breakdown" if st.get("regions") else "lines")
        funds.append({"name": f["name"], "issuer": lookthrough.issuer({"name": f["name"]}, st), "countries": countries,
                      "sectors": "finanzfluss" if any(t != "regions" for t in ff) else "lines"})
    return {"written": json.loads(ctx.state.meta(meta_keys.TAXONOMIES_LAST()) or "null"), "funds": funds,
            "classes": json.loads(ctx.state.meta(meta_keys.ASSET_CLASSES()) or "null")}


def _isin(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    from .lookthrough import read_soon

    read_soon(request, ctx)
    names = {a["asset_id"]: a["name"] for key in c.brokers for a in ctx.state.assets(key).values() if a["name"]}
    names |= {p["id"]: p["name"] for p in ctx.depot.positions}
    done = [{"name": names.get(asset_id, asset_id), **entry}
            for asset_id, entry in assets_mod.completed(ctx.state).items()]
    return {"done": sorted(done, key=lambda d: d["at"], reverse=True),
            "without": [p for p in ctx.depot.positions if not p["isin"]], "depot_error": ctx.depot_error,
            "read": bool(ctx.depot_at)}


def _logos(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    return {"has_token": bool(ctx.vault.load().get("logo_dev_token")), "last": logos_mod.last(ctx.state)}


def _spending(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    accounts, error = ctx.wf_accounts(c)
    names = {a.id: a.name for a in accounts}
    rows = [{"key": key, "label": ctx.adapters[key].label, "account": names.get(b.cash_account_id, b.cash_account_id),
             "on": b.spending} for key, b in c.brokers.items() if key in ctx.adapters and b.cash_account_id]
    ours = json.loads(ctx.state.meta(meta_keys.SPENDING()) or "[]")
    return {"rows": rows, "tracked": [names.get(a, a) for a in ours], "wf_error": error}


def _xray(request: Request, ctx: AppContext, c: config_mod.Config) -> dict:
    from .lookthrough import read_soon

    read_soon(request, ctx)
    built = xray_addon.build(ctx.state, ctx.depot, ctx.syncer.learned_isins(c), 0)
    return {"status": json.loads(ctx.state.meta(meta_keys.XRAY_ADDON_STATUS()) or "null"),
            "stale": len(json.loads(ctx.state.meta(meta_keys.XRAY_ADDON_STALE()) or "[]")),
            "funds": sorted(f["name"] for f in built.index["funds"].values()), "missing": built.index["missing"],
            "read": bool(ctx.depot_at), "depot_error": ctx.depot_error}


PAGES: dict[str, Callable[[Request, AppContext, config_mod.Config], dict]] = {
    "sync": _sync, "taxonomies": _taxonomies, "isin": _isin, "logos": _logos, "spending": _spending, "xray": _xray}


def render(request: Request, ctx: AppContext, key: str, *, errors: dict | None = None):
    c = ctx.cfg()
    on = c.service(key)
    response = ctx.render(request, f"service_{key}.html", key=key, service=NAMES[key], text=TEXTS[key], on=on,
                          lacking=None if on else missing(ctx, key, c), cfg=c, errors=errors or {},
                          pending=request.session.get("enable_service") == key,
                          busy=ctx.running.is_set(), **PAGES[key](request, ctx, c))
    if errors:
        response.status_code = 422
    return response


@router.get("/services/{key}", response_class=HTMLResponse)
def service_page(request: Request, ctx: Ctx, key: str):
    if key not in NAMES:
        return redirect(request, "/", tr(request, "Unbekannter Dienst."), "error")
    return render(request, ctx, key)


# ── settings ─────────────────────────────────────────────────────────────────
@router.post("/services/logos/settings")
def logos_settings(request: Request, ctx: Ctx, token: str = Form(""), remove_token: str = Form("")):
    token = token.strip()
    if remove_token:
        ctx.vault.update(lambda d: d.pop("logo_dev_token", None))
    elif token:
        if len(token) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            return render(request, ctx, "logos",
                          errors={"token": tr(request, "Der Schlüssel sieht nicht wie ein logo.dev-Schlüssel aus.")})
        ctx.vault.update(lambda d: d.__setitem__("logo_dev_token", token))
    return redirect(request, "/services/logos", tr(request, "Gespeichert."))


@router.post("/services/taxonomies/settings")
def taxonomies_settings(request: Request, ctx: Ctx, lookup: str = Form("")):
    on = lookup == "1"
    ctx.update(lambda c: setattr(c, "asset_class_lookup", on))
    return redirect(request, "/services/taxonomies", tr(request, "Gespeichert."))


@router.post("/services/spending/settings")
async def spending_settings(request: Request, ctx: Ctx):
    """The cash accounts for the tracker, all brokers in one form (``BrokerConfig.spending``)."""
    form = await request.form()
    c = ctx.cfg()
    chosen = {str(v) for v in form.getlist("broker")}
    listed = {k for k, b in c.brokers.items() if k in ctx.adapters and b.cash_account_id}
    if chosen - listed:
        return render(request, ctx, "spending",
                      errors={"broker": tr(request, "Unbekanntes Konto - bitte die Seite neu laden.")})

    def change(c: config_mod.Config) -> None:
        for k in listed:
            c.brokers[k].spending = k in chosen

    ctx.update(change)
    return redirect(request, "/services/spending", tr(request, "Gespeichert."))


# ── actions ──────────────────────────────────────────────────────────────────
def _run_taxonomies(ctx: AppContext, lang: str) -> tuple[str, str]:
    w = ctx.syncer.write_taxonomies()
    return translate("{written} Zuordnungen in Wealthfolio eingetragen, {manual} mit deinen eigenen gelassen.", lang,
                     written=w.written, manual=w.manual), "ok"


def _run_isin(ctx: AppContext, lang: str) -> tuple[str, str]:
    n = ctx.syncer.complete_isins()
    if n is None:
        return translate("ISINs konnten nicht ergänzt werden - siehe Protokoll.", lang), "error"
    return translate("{n} ISINs in Wealthfolio ergänzt.", lang, n=n), "ok"


def _run_logos(ctx: AppContext, lang: str) -> tuple[str, str]:
    result = ctx.syncer.update_logos()
    if result is None:
        return translate("Logos konnten nicht geholt werden - siehe Protokoll.", lang), "error"
    text = translate("{set} Logos gesetzt, {none} ohne Logo bei logo.dev, {failed} fehlgeschlagen.", lang,
                     set=result.set, none=result.none, failed=result.failed)
    if result.message:
        text += f" {result.message}"
    return text, "warn" if result.message or result.failed else "ok"


def _run_spending(ctx: AppContext, lang: str) -> tuple[str, str]:
    if not ctx.syncer.update_spending():
        return translate("Der Ausgaben-Tracker wurde nicht aktualisiert - siehe Protokoll.", lang), "error"
    return translate("Ausgaben-Tracker aktualisiert.", lang), "ok"


def _run_xray(ctx: AppContext, lang: str) -> tuple[str, str]:
    status = ctx.syncer.write_xray_addon() or {}
    if not status.get("ok"):
        return translate("Die Fondslisten wurden nicht abgelegt: {error}", lang, error=status.get("error", "")), "error"
    return translate("Fondslisten abgelegt: {written} geschrieben, {deleted} gelöscht.", lang,
                     written=status.get("written", 0), deleted=status.get("deleted", 0)), "ok"


RUNS = {"taxonomies": _run_taxonomies, "isin": _run_isin, "logos": _run_logos, "spending": _run_spending,
        "xray": _run_xray}


def _job(ctx: AppContext, work: Callable[[AppContext, str], tuple[str, str]]) -> Callable[[str], tuple[str, str]]:
    def job(lang: str) -> tuple[str, str]:
        try:
            return work(ctx, lang)
        except AlreadyRunning:
            return translate(BUSY, lang), "warn"
        except WealthfolioError as e:
            return translate("Wealthfolio: {error}", lang, error=str(e)), "error"
    return job


@router.post("/services/{key}/run")
def service_run(request: Request, ctx: Ctx, key: str):
    """*Jetzt ausführen*: the service's step now, the same code a run calls, in the background."""
    if key not in RUNS:
        return redirect(request, "/", tr(request, "Unbekannter Dienst."), "error")
    page = f"/services/{key}"
    c = ctx.cfg()
    if not c.service(key) or missing(ctx, key, c):
        return redirect(request, page, tr(request, "Erst einschalten."), "warn")
    from .overview import start_job

    return start_job(request, ctx, NAMES[key], _job(ctx, RUNS[key]), page)


def _remove_taxonomies(ctx: AppContext, lang: str) -> tuple[str, str]:
    n = ctx.syncer.remove_taxonomies()
    ctx.state.delete_meta(meta_keys.TAXONOMIES_LAST())
    return translate("{n} Zuordnungen aus Wealthfolio entfernt.", lang, n=n), "ok"


def _remove_xray(ctx: AppContext, lang: str) -> tuple[str, str]:
    status = ctx.syncer.write_xray_addon()  # switched off: clears what it wrote
    if status and not status.get("ok"):
        return translate("Die Fondslisten wurden nicht entfernt: {error}", lang, error=status.get("error", "")), "error"
    return translate("Fondslisten aus Wealthfolio entfernt.", lang), "ok"


REMOVES = {"taxonomies": _remove_taxonomies, "xray": _remove_xray}


@router.post("/services/{key}/remove")
def service_remove(request: Request, ctx: Ctx, key: str, confirm: str = Form("")):
    """*Aus Wealthfolio entfernen*: only what the service wrote itself, and the service switched off."""
    if key not in REMOVES:
        return redirect(request, "/", tr(request, "Unbekannter Dienst."), "error")
    page = f"/services/{key}"
    if confirm != "1":
        return redirect(request, page, tr(request, "Bitte zum Entfernen bestätigen."), "error")
    ctx.update(lambda c: c.set_service(key, False))
    from .overview import start_job

    return start_job(request, ctx, N_("Aus Wealthfolio entfernen"), _job(ctx, REMOVES[key]), page)
