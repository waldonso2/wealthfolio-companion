"""Unknown bookings completed on *Unbekannt* (waldonso2/wealthfolio-importer-addon#132) and splits (#133)."""

from decimal import Decimal as D

import pytest

from brokersync.history import COMPLETION_KINDS, completion
from brokersync.mapping import Accounts, MappingError, to_activities
from brokersync.model import Kind
from brokersync.state import State

from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf, make

ISIN = "IE00B4L5Y983"
ACCOUNTS = Accounts("acc-cash", "acc-depot")
UNKNOWN_ID = "TEST-20260105-4"  # the fake broker's unknown event on the day of test_sync.ANCHOR


def test_a_split_is_one_activity_with_its_ratio_on_the_depot():
    tx = completion("ev-1", {"kind": "split", "isin": ISIN, "ratio": "0.1", "day": "2026-04-20T08:00:00+00:00",
                             "name": "Brent 3x Short"})
    [a] = to_activities(tx, "tr", ACCOUNTS)
    assert (a["accountId"], a["activityType"], a["amount"], a["quantity"]) == ("acc-depot", "SPLIT", "0.1", "0")
    assert a["asset"]["symbol"] == ISIN and a["comment"].endswith("[SYNC tr:ev-1]")
    assert a["activityDate"].startswith("2026-04-20")
    with pytest.raises(MappingError, match="split ratio"):
        to_activities(completion("ev-1", {"kind": "split", "isin": ISIN, "ratio": "0", "day": "2026-04-20"}),
                      "tr", ACCOUNTS)


@pytest.mark.parametrize("kind", COMPLETION_KINDS)
def test_every_kind_books_by_the_booking_model(kind):
    c = {"kind": kind, "isin": ISIN, "day": "2026-01-05", "shares": "2", "amount": "23.20", "fee": "1",
         "ratio": "2"}
    tx = completion("ev-2", c)
    acts = to_activities(tx, "tr", ACCOUNTS)
    types = [a["activityType"] for a in acts]
    expected = {"buy": ["TRANSFER_OUT", "TRANSFER_IN", "BUY"],
                "sell": ["SELL", "TRANSFER_OUT", "TRANSFER_IN"],
                "in": ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY"],
                "out": ["SELL"],
                "split": ["SPLIT"]}
    assert types == expected[kind]
    if kind == "buy":
        assert tx.kind == Kind.BUY and (tx.net, tx.gross, tx.fee) == (D("23.20"), D("22.20"), D("1"))
    if kind == "out":
        assert acts[0]["amount"] in ("0", "0.00")


def test_a_completed_unknown_booking_is_booked_once_and_leaves_the_list(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    state = State(tmp_path)
    assert [e["tx_id"] for e in state.unknown_events("fake")] == [UNKNOWN_ID]
    state.add_completion("fake", UNKNOWN_ID, {"kind": "buy", "isin": ISIN, "day": "2026-01-05", "shares": "0.2",
                                              "amount": "23.20"})
    [r] = syncer.run()
    assert r.created == 1 and r.unknown == 0
    mine = [a for a in wf.activities if f"[SYNC fake:{UNKNOWN_ID}]" in a["comment"]]
    assert [a["activityType"] for a in mine] == ["TRANSFER_OUT", "TRANSFER_IN", "BUY"]
    assert state.unknown_events("fake") == []
    syncer.run()
    assert len([a for a in wf.activities if f"[SYNC fake:{UNKNOWN_ID}]" in a["comment"]]) == 3


def test_the_form_checks_and_saves_and_withdraws(tmp_path):
    client, wf, _ = make(tmp_path)
    first_password(client)
    State(tmp_path).add_unknown("fake", "ev-9", "SSP_CORPORATE_ACTION_NO_CASH", "2026-04-20T08:00:00+00:00",
                                {"eventType": "SSP_CORPORATE_ACTION_NO_CASH", "subtitle": "Reverse Split"})
    page = client.get("/unknown").text
    assert "Vervollständigen und buchen" in page
    token = csrf(client, "/unknown")
    base = {"csrf": token, "broker": "fake", "tx_id": "ev-9", "kind": "split"}
    r = client.post("/unknown/complete", data={**base, "isin": "IE00B4L5Y984", "ratio": "0,1"})
    assert "Keine gültige ISIN" in r.text and not State(tmp_path).completions()
    r = client.post("/unknown/complete", data={**base, "isin": ISIN})
    assert "Verhältnis: bitte angeben" in r.text
    r = client.post("/unknown/complete", data={**base, "isin": ISIN, "ratio": "0,1"})
    assert "der nächste Abruf bucht es" in r.text
    assert State(tmp_path).completions() == {("fake", "ev-9"): {
        "kind": "split", "isin": ISIN, "day": "2026-04-20T08:00:00+00:00", "currency": "EUR", "name": "",
        "ratio": "0.1"}}
    assert "Vorgemerkt als Split / Reverse Split" in client.get("/unknown").text
    client.post("/unknown/complete", data={**base, "action": "delete"})
    assert not State(tmp_path).completions()
    r = client.post("/unknown/complete", data={**base, "tx_id": "nope", "isin": ISIN, "ratio": "1"})
    assert "nicht mehr offen" in r.text


# ── the proposed exchange of two ISINs (Trade Republic's reverse split into a new instrument) ──────────────────
PROPOSAL = {"out": {"tx_id": "o1", "isin": ISIN, "name": "Brent Alt", "shares": "25000"},
            "in": {"tx_id": "n1", "isin": "IE00B4L5Y984", "name": "Brent Neu", "shares": "25"}}


class _Holdings:
    def __init__(self, rows):
        self.rows = rows

    def holdings(self, _account):
        return self.rows


def _holding(qty, basis, ccy="EUR", **inst):
    return {"holdingType": "security", "quantity": qty, "localCurrency": ccy, "costBasis": {"local": basis},
            "instrument": {"id": "a1", "symbol": ISIN, **inst}}


def test_the_cost_basis_of_the_removed_shares_comes_from_the_holding():
    from brokersync import swap

    assert swap.cost_basis(_Holdings([_holding("25000", "1000")]), "p", None, ISIN, D("10000")) == (D("400.00"), "EUR")
    assert swap.cost_basis(_Holdings([_holding("5", "10", symbol="X")]), "p", "a1", ISIN, D("5")) == (D("10.00"), "EUR")
    for rows, why in (([_holding("100", "1000")], "held"), ([_holding("25000", "0")], "basis"), ([], "position")):
        with pytest.raises(swap.SwapError, match=why):
            swap.cost_basis(_Holdings(rows), "p", None, ISIN, D("25000"))


def test_confirming_the_exchange_stores_both_legs_with_the_cost_basis(tmp_path, monkeypatch):
    from brokersync import config as config_mod
    from brokersync import swap

    client, _, _ = make(tmp_path)
    first_password(client)
    config_mod.update(tmp_path, lambda c: c.brokers.__setitem__("fake", config_mod.BrokerConfig(
        enabled=True, cash_account_id="acc-cash", portfolio_account_id="acc-depot")))
    state = State(tmp_path)
    for tx_id in ("o1", "n1"):
        state.add_unknown("fake", tx_id, "SSP_CORPORATE_ACTION_NO_CASH", "2026-03-02T07:00:00+00:00",
                          {"eventType": "SSP_CORPORATE_ACTION_NO_CASH", "proposal": PROPOSAL})
    assert "Tausch buchen" in client.get("/unknown").text
    token = csrf(client, "/unknown")
    monkeypatch.setattr(swap, "cost_basis", lambda *a: (_ for _ in ()).throw(swap.SwapError("position")))
    r = client.post("/unknown/swap", data={"csrf": token, "broker": "fake", "tx_id": "o1"})
    assert "nicht zu finden" in r.text and not State(tmp_path).completions()
    monkeypatch.setattr(swap, "cost_basis", lambda *a: (D("500.00"), "EUR"))
    r = client.post("/unknown/swap", data={"csrf": token, "broker": "fake", "tx_id": "n1"})
    assert "bucht den Tausch" in r.text
    done = State(tmp_path).completions()
    assert {k: (v["kind"], v["isin"], v["shares"], v["amount"]) for k, v in done.items()} == {
        ("fake", "o1"): ("swap_out", ISIN, "25000", "500.00"),
        ("fake", "n1"): ("swap_in", "IE00B4L5Y984", "25", "500.00")}
    assert "Vorgemerkt als Tausch: alte Position raus" in client.get("/unknown").text
    r = client.post("/unknown/swap", data={"csrf": token, "broker": "fake", "tx_id": "nope"})
    assert "nicht mehr offen" in r.text
