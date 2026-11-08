"""ESMA's FIRDS as the last source of an asset class (#185): the CFI mapping, the request, and the pass."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from brokersync import firds, lookthrough, meta_keys
from brokersync.lookthrough import Depot
from brokersync.state import State
from tests.test_lookthrough import _wf

STOCK, ETF_EQ, ETF_MIXED, GONE = "DE000DTR0CK8", "IE00B4L5Y983", "LU0908500753", "XS0000000009"


@pytest.mark.parametrize("cfi, expected", [
    ("ESVUFR", "EQUITY"),       # Daimler Truck shares
    ("EDSXFR", "EQUITY"),       # depository receipt
    ("CEOGES", "EQUITY"),       # iShares Core MSCI World (ETF, equities)
    ("CEOIBS", "FIXED_INCOME"),  # Vanguard USD Treasury Bond ETF
    ("DBFTFB", "FIXED_INCOME"),  # a Bund
    ("CEOICS", "COMMODITIES"),
    ("CIOIRS", "REAL_ESTATE"),
    ("CECGMS", None),           # a fund of "others" (a swap ETF): not guessed
    ("CIOILS", None),           # mixed
    ("EYAYMT", None),           # structured (Xetra-Gold): the name rule handles those
    ("DYZZZZ", None),           # money market
    ("OCEFPS", None),           # an option
    ("ES", None), ("", None), ("12VUFR", None),
])
def test_cfi_class(cfi, expected):
    assert firds.cfi_class(cfi) == expected


def test_one_class_only_when_every_code_agrees():
    assert firds.asset_class(["CEOGES", "ESVUFR"]) == "EQUITY"
    assert firds.asset_class(["CEOGES", "CEOIBS"]) is None
    assert firds.asset_class(["CEOGES", "CECGMS"]) is None
    assert firds.asset_class([]) is None


def _transport(status=200, docs=None, body=None, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if status != 200:
            return httpx.Response(status, headers={"location": "https://evil.example/"} if status == 302 else {})
        return httpx.Response(200, content=body if body is not None else json.dumps(
            {"response": {"numFound": len(docs or []), "docs": docs or []}}).encode())
    return httpx.MockTransport(handle)


def test_the_request_names_only_the_isin_and_the_register():
    seen: list[httpx.Request] = []
    docs = [{"gnr_cfi_code": "ESVUFR"}, {"gnr_cfi_code": "esvufr"}, {"gnr_cfi_code": "ESVUFR"}, {"x": 1}]
    assert firds.cfi_codes(STOCK, transport=_transport(docs=docs, seen=seen)) == ["ESVUFR"]
    (r,) = seen
    assert r.url.host == "registers.esma.europa.eu" and r.url.scheme == "https" and not r.content
    assert r.url.params["q"] == f"isin:{STOCK}" and set(r.url.params) == {"q", "rows", "fl", "wt"}
    assert firds.cfi_codes(STOCK, transport=_transport(docs=[])) == []  # not in the register
    assert firds.cfi_codes("not an isin", transport=_transport(seen=seen)) == [] and len(seen) == 1  # no request


@pytest.mark.parametrize("kwargs", [{"status": 504}, {"status": 429}, {"status": 302}, {"body": b"<html>504</html>"},
                                    {"body": b'{"response": {}}'}, {"body": b"x" * (firds.MAX_BYTES + 1)}])
def test_an_odd_answer_is_unavailable_never_an_empty_result(kwargs):
    with pytest.raises(firds.Unavailable):
        firds.cfi_codes(STOCK, transport=_transport(**kwargs))


def _depot(*pairs):
    return Depot(positions=[{"id": i, "isin": isin, "name": n, "currency": "EUR", "value": D(1)}
                            for i, isin, n in pairs])


def test_a_position_without_class_asks_the_register_only_when_switched_on(tmp_path, monkeypatch):
    asked: list[str] = []
    answers = {STOCK: ["ESVUFR"], ETF_EQ: ["CEOGES"], ETF_MIXED: ["CECGMS"], GONE: []}
    monkeypatch.setattr(firds, "cfi_codes", lambda isin: asked.append(isin) or answers[isin])
    state = State(tmp_path)
    depot = _depot(("a-stock", STOCK, "Daimler Truck Holding AG"), ("a-etf", ETF_EQ, "Core World"),
                   ("a-swap", ETF_MIXED, "Core Stoxx 600"), ("a-gone", GONE, "Old Note"),
                   ("a-user", STOCK, "Mine"), ("a-bond", "", "Euro Govt Bond ETF"))
    fake, wf = _wf()
    fake.assignments.append({"id": "u1", "assetId": "a-user", "taxonomyId": "asset_classes",
                             "categoryId": "FIXED_INCOME", "weight": 10000, "source": "MANUAL"})
    lookthrough.asset_classes(state, wf, depot, write=True)  # switched off: no request
    lookthrough.asset_classes(state, wf, depot, write=False, lookup=True)  # nothing written: no request either
    assert asked == []
    found = lookthrough.asset_classes(state, wf, depot, write=True, lookup=True)
    assert sorted(asked) == sorted([STOCK, ETF_EQ, ETF_MIXED, GONE])  # the user's own asset was skipped before asking
    mine = {a["assetId"]: (a["categoryId"], a["weight"], a["source"]) for a in fake.assignments}
    assert mine == {"a-user": ("FIXED_INCOME", 10000, "MANUAL"), "a-bond": ("FIXED_INCOME", 10000, "AUTO"),
                    "a-stock": ("EQUITY", 10000, "AUTO"), "a-etf": ("EQUITY", 10000, "AUTO")}
    assert found["named"]["a-stock"]["from"] == "firds" and found["named"]["a-bond"]["from"] == "name"
    assert sorted(p["id"] for p in found["missing"]) == ["a-gone", "a-swap"]
    # Known ISINs are not asked again; the one the register doesn't list waits RETRY_DAYS.
    asked.clear()
    lookthrough.asset_classes(state, wf, depot, write=True, lookup=True)
    assert asked == []
    old = (datetime.now(UTC) - timedelta(days=firds.RETRY_DAYS + 1)).isoformat()
    state.set_meta(meta_keys.FIRDS(GONE), json.dumps({"codes": [], "at": old}))
    lookthrough.asset_classes(state, wf, depot, write=True, lookup=True)
    assert asked == [GONE]


def test_an_unavailable_register_ends_the_pass_and_records_nothing(tmp_path, monkeypatch):
    asked: list[str] = []

    def down(isin):
        asked.append(isin)
        raise firds.Unavailable("HTTP 504")

    monkeypatch.setattr(firds, "cfi_codes", down)
    state = State(tmp_path)
    fake, wf = _wf()
    depot = _depot(("a1", STOCK, "A"), ("a2", ETF_EQ, "B"))
    found = lookthrough.asset_classes(state, wf, depot, write=True, lookup=True)
    assert asked == [STOCK]  # the second isn't tried
    assert fake.assignments == [] and len(found["missing"]) == 2
    assert state.meta(meta_keys.FIRDS(STOCK)) is None
