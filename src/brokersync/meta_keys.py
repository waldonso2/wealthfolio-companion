"""Every key of the ``meta`` table in ``state.db`` (#77): what it holds and whether it belongs to a broker.

No module builds a ``meta`` key itself - it calls one of these. ``State.forget_broker`` and
``State.reset_broker`` delete the keys of ``BROKER_KEYS`` for a broker.

Kinds: ``flag`` (set or not, value = when it was set), ``text``, ``json``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetaKey:
    prefix: str
    kind: str  # flag | text | json
    per_broker: bool
    doc: str

    def __call__(self, suffix: str = "") -> str:
        return f"{self.prefix}:{suffix}" if suffix else self.prefix


ASSETS_LEARNED = MetaKey("assets-learned", "flag", True,
                         "positions' assets were learned once from the whole history (assets.py)")
CASH_ASSETS_REPAIRED = MetaKey("cash-assets-repaired", "flag", True,
                               "the $CASH asset was removed from the sync's own cash activities (repair.py)")
REFETCH = MetaKey("refetch", "flag", True, "the next run reads again from the start date")
SAVINGS_ACCOUNT = MetaKey("savings-account", "text", True,
                          "the savings account (Wealthfolio id) a run read without failures")
DIVIDEND_TAX = MetaKey("dividend-tax", "flag", True,
                       "dividends matched before were re-read once to add their tax to imports")
OWN_DIVIDEND_TAX = MetaKey("own-dividend-tax", "flag", True,
                           "the sync's own dividends got the tax the adapter learned to read later (#52)")
HISTORY_START = MetaKey("history-start", "text", True, "the earliest giro day ever read (ISO date)")
RATE_LIMIT = MetaKey("rate-limit", "text", True,
                     "the broker limited the requests: scheduled runs wait until this moment (ISO, UTC; #149)")
POSITIONS = MetaKey("positions", "json", True,
                    "the broker's positions of the last check: [{isin, name, shares, ...}] (page Prüfung)")
RETIRED_NOTICE = MetaKey("retired-notice", "json", True,
                         "a removed broker left activities in Wealthfolio: {label, activities, search}")
LOGO = MetaKey("logo", "json", False, "logo status of a Wealthfolio asset: {isin, status, at} (logos.py)")
LOGOS_LAST = MetaKey("logos:last", "json", False, "the result of the last logo pass (logos.py)")
PROGRESS = MetaKey("progress", "json", False,
                   "the run going on: {broker, label, phase, n, m, since}, gone after it (overview, #104)")
CHECKLIST_HIDDEN = MetaKey("checklist-hidden", "flag", False, "the setup checklist of the overview was hidden (#110)")
FUND = MetaKey("fund", "json", False,
               "look-through: where a Wealthfolio asset's holdings come from: {source, url, status, at, error}")
FUNDS_LAST = MetaKey("funds:last", "json", False,
                     "X-Ray: the last fetch of the funds' lists: {at, fetched, none, failed, finder, error}")
FUNDS_PROGRESS = MetaKey("funds:progress", "json", False,
                         "X-Ray: the fetch of the funds' lists going on: {n, m, name}, gone after it")
XRAY_ACCEPTED = MetaKey("xray:accepted", "json", False, "cluster risks the user accepted on *Durchschau*: [key]")
XRAY_NOTIFIED = MetaKey("xray:notified", "json", False, "cluster risks notified via ntfy: [key]")
TAXONOMIES_LAST = MetaKey("taxonomies:last", "json", False,
                          "look-through: the last write of countries and sectors to Wealthfolio: {written, manual, at}")
ASSET_CLASSES = MetaKey("asset-classes", "json", False,
                        "positions without an asset class in Wealthfolio, and those given one from the fund's name: "
                        "{missing: [{id, name, isin}], named: {asset id: {name, class}}, at} (#141)")
FIRDS = MetaKey("firds", "json", False,
                "what ESMA's FIRDS lists for an ISIN: {codes: [CFI code], at}; codes empty = not in the register, "
                "asked again after firds.RETRY_DAYS (firds.py, #185)")
AUTO_UPDATE = MetaKey("auto-update", "json", False,
                      "the last automatic update (deploy/companion-autoupdate): {from, to, ok, at}")
UPDATE = MetaKey("update", "json", False, "the newest release GitHub named: {version, checked} (updates.py)")
ISIN = MetaKey("isin", "json", False,
               "the ISIN the sync set on a Wealthfolio asset, once: {isin, at} - a bare ISIN before 0.47.0 "
               "(assets.complete_isins, #140)")
SPENDING = MetaKey("spending", "json", False,
                   "cash accounts the sync put into Wealthfolio's spending tracker: [account id] (spending.py, #134)")
XRAY_ADDON_STATUS = MetaKey("xray-addon:status", "json", False,
                            "the last write for the X-Ray addon: {at, ok, written, deleted, skipped, error} (#146)")
XRAY_ADDON_STALE = MetaKey("xray-addon:stale", "json", False,
                           "addon storage keys still to delete for the X-Ray addon: [key] (xray_addon.py, #146)")
WEALTHFOLIO_APPEARANCE = MetaKey("wealthfolio:appearance", "json", False,
                                 "the font the user chose in Wealthfolio: {font: mono|sans|serif, at} (appearance.py)")

ALL = (ASSETS_LEARNED, CASH_ASSETS_REPAIRED, REFETCH, SAVINGS_ACCOUNT, DIVIDEND_TAX, HISTORY_START, POSITIONS,
       RATE_LIMIT, OWN_DIVIDEND_TAX,
       RETIRED_NOTICE, LOGO, LOGOS_LAST, PROGRESS, CHECKLIST_HIDDEN, UPDATE, AUTO_UPDATE, FUND, FUNDS_LAST,
       XRAY_ACCEPTED, XRAY_NOTIFIED, TAXONOMIES_LAST, SPENDING, ISIN, FUNDS_PROGRESS, XRAY_ADDON_STATUS,
       XRAY_ADDON_STALE, WEALTHFOLIO_APPEARANCE, ASSET_CLASSES, FIRDS)
BROKER_KEYS = tuple(k for k in ALL if k.per_broker)
