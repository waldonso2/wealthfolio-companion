"""Trade Republic adapter: timeline mapping (via pytr's parser), web login with
app confirmation, session cookies, PIN safety, and the holdings check."""

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
import requests

from brokersync import config as config_mod
from brokersync.adapters.base import AdapterError, AuthRequired
from brokersync.adapters.tr import TradeRepublicAdapter
from brokersync.mapping import Accounts, to_activities
from brokersync.model import CashBalance, Kind, Position, to_dict
from brokersync.reconcile import compare
from brokersync.sync import Syncer
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient, WealthfolioError

from .fakes import PASSWORD, FakeWealthfolio
from .helpers import first_password
from .test_sync import RecordingNotifier

CASE = json.loads((Path(__file__).parent / "contract" / "tr" / "timeline.json").read_text())
RECORDING = CASE["recording"]
CREDS = {"phone": "+49 170 0000000", "pin": "1234"}


def txs():
    return TradeRepublicAdapter.replay(RECORDING).get_transactions(datetime(2026, 8, 1, tzinfo=UTC))


def by_raw(raw_type):
    return next(t for t in txs() if t.raw_type == raw_type)


def test_trades_book_like_the_csv_import():
    buy = by_raw("TRADING_TRADE_EXECUTED")
    assert (buy.kind, buy.net, buy.gross, buy.fee, buy.shares) == (Kind.BUY, D("111"), D("110"), D("1"),
                                                                     D("0.685102"))
    acts = to_activities(buy, "tr", Accounts("c", "p"))
    assert [(a["accountId"], a["activityType"]) for a in acts] == [
        ("c", "TRANSFER_OUT"), ("p", "TRANSFER_IN"), ("p", "BUY")]
    assert acts[0]["amount"] == acts[1]["amount"] == "111"
    # amount = shares × unit price (6 decimals, as the addon) + fee: within a cent of the cash paid.
    trade = acts[2]
    assert (trade["unitPrice"], trade["fee"]) == ("160.560033", "1")
    assert abs(D(trade["amount"]) - D("111")) < D("0.000001")
    sell = next(t for t in txs() if t.kind == Kind.SELL)
    assert (sell.net, sell.gross, sell.fee, sell.tax) == (D("200.93"), D("202.09"), D("1"), D("0.16"))
    acts = to_activities(sell, "tr", Accounts("c", "p"))
    assert [a["activityType"] for a in acts] == ["SELL", "TRANSFER_OUT", "TRANSFER_IN"]
    assert abs(D(next(a for a in acts if a["activityType"] == "SELL")["amount"]) - D("200.93")) < D("0.000001")


def test_dividend_net_with_withholding_tax():
    div = next(t for t in txs() if t.kind == Kind.DIVIDEND)
    (d, *_) = to_activities(div, "tr", Accounts("c", "p"))
    assert (d["activityType"], d["amount"], d["tax"], d["quantity"]) == ("DIVIDEND", "2.24", "0.78", "10.640298")


def test_saveback_is_a_bonus_funded_buy_without_cash_transfer():
    sb = next(t for t in txs() if t.bonus_funded)
    acts = to_activities(sb, "tr", Accounts("c", "p"))
    assert [(a["accountId"], a["activityType"], a.get("subtype")) for a in acts] == [
        ("p", "CREDIT", "BONUS"), ("p", "BUY", None)]
    assert acts[0]["amount"] == "15" and abs(D(acts[1]["amount"]) - D("15")) < D("0.000001")


def test_cash_events_and_taxes():
    kinds = {t.raw_type: (t.kind, t.net, t.label) for t in txs()}
    assert kinds["CARD_TRANSACTION"] == (Kind.WITHDRAWAL, D("4.28"), "Kartenzahlung")
    assert kinds["CARD_REFUND"] == (Kind.DEPOSIT, D("24.99"), "Kartenerstattung")
    assert kinds["OUTGOING_TRANSFER"][0] == Kind.WITHDRAWAL
    assert kinds["INTEREST_PAYOUT"][:2] == (Kind.INTEREST, D("4.87"))
    assert kinds["SSP_TAX_CORRECTION_INVOICE"][:2] == (Kind.TAX_REFUND, D("2.87"))
    vorab = next(t for t in txs() if t.label == "Vorabpauschale")
    assert (vorab.kind, vorab.net) == (Kind.TAX, D("0.19"))


def test_tax_charge_referral_bonus_and_worthless_book_like_the_csv_import():
    charge = by_raw("SSP_TAX_CORRECTION")  # pytr calls it a refund, but it takes money
    (a,) = to_activities(charge, "tr", Accounts("c", "p"))
    assert (a["accountId"], a["activityType"], a["amount"]) == ("c", "TAX", "12.34")
    (a,) = to_activities(by_raw("REFERRAL_FIRST_TRADE_EXECUTED_INVITER"), "tr", Accounts("c", "p"))
    assert (a["accountId"], a["activityType"], a["subtype"], a["amount"]) == ("c", "CREDIT", "BONUS", "15")
    worthless = by_raw("SSP_CORPORATE_ACTION_ACTIVITY")
    (a,) = to_activities(worthless, "tr", Accounts("c", "p"))  # a sale at 0, no money moves (as the addon)
    assert (a["accountId"], a["activityType"], a["quantity"], a["amount"]) == ("p", "SELL", "3", "0")


def test_dividend_reinvestment_is_a_buy_paid_from_cash_with_the_share_count_of_the_details():
    (buy, transfer_out, transfer_in) = sorted(to_activities(by_raw("SSP_CORPORATE_ACTION_INSTRUMENT"), "tr",
                                                           Accounts("c", "p")), key=lambda a: a["activityType"])
    assert (buy["accountId"], buy["activityType"], buy["quantity"], buy["amount"]) == ("p", "BUY", "0.25", "7.5")
    assert {a["accountId"] for a in (transfer_out, transfer_in)} == {"c", "p"}  # funded from the cash account


def _reinvestment(sections, value=-23.2):
    return {"id": "r1", "timestamp": "2025-03-26T08:00:00.000+0000", "title": "Shell", "icon": "logos/GB00BP6MXD84/v2",
            "subtitle": "Reinvestition der Dividende", "status": "EXECUTED",
            "eventType": "SSP_CORPORATE_ACTION_INSTRUMENT", "details": {"id": "r1", "sections": sections},
            "amount": {"currency": "EUR", "value": value, "fractionDigits": 2}}


def _shares_row(text):
    return {"title": "Übersicht", "type": "table",
            "data": [{"title": "Aktien", "detail": {"text": text, "type": "text"}, "style": "plain"}]}


@pytest.mark.parametrize("sections, value", [
    ([], -23.2),                              # no share count in the details: nothing is guessed
    ([_shares_row("0")], -23.2),              # zero shares
    ([_shares_row("viel")], -23.2),           # not a number
    ([_shares_row("0,25")], 17.1),            # money came in, it is no debit
])
def test_dividend_reinvestment_without_shares_or_debit_stays_unknown(sections, value):
    from brokersync.adapters.tr import to_transactions

    (t,) = to_transactions([_reinvestment(sections, value)])
    assert t.kind == Kind.UNKNOWN


def test_dividend_reinvestment_reads_german_share_notation():
    from brokersync.adapters.tr import to_transactions

    (t,) = to_transactions([_reinvestment([_shares_row("1.234,5")])])
    assert (t.kind, t.shares, t.net, t.isin) == (Kind.BUY, D("1234.5"), D("23.2"), "GB00BP6MXD84")


def test_unknown_and_informational_events():
    out = txs()
    unknown = [t for t in out if t.kind == Kind.UNKNOWN]
    assert [t.raw_type for t in unknown] == ["SSP_CORPORATE_ACTION_INVOICE_SHARES (SPLIT)"]
    assert set(unknown[0].raw) == {"eventType", "title", "subtitle", "status"}  # nothing personal
    # With an amount, it is shown so the event can be booked by hand - still nothing personal.
    from brokersync.adapters.tr import _unknown

    bonus = _unknown({"id": "x", "eventType": "REFERRAL_FIRST_TRADE_EXECUTED_INVITER", "title": "Einladung",
                      "subtitle": "Empfehlungsbonus erhalten", "amount": {"value": 15.0, "currency": "EUR"},
                      "details": {"sections": [{"title": "IBAN", "detail": {"text": "DE00 0000"}}]}}, "REFERRAL")
    assert bonus.raw["betrag"] == "15.0 EUR" and "DE00" not in json.dumps(bonus.raw)
    ids = {t.id for t in out}
    assert "00000000-0000-4000-8000-000000000014" not in ids  # address change: informational
    assert "00000000-0000-4000-8000-000000000015" not in ids  # cancelled transfer


# ── login ───────────────────────────────────────────────────────────────────
class FakeJar(requests.cookies.RequestsCookieJar):
    def __init__(self, filename):
        super().__init__()
        self.filename = filename

    def save(self, ignore_discard=True):
        line = "\t".join([".traderepublic.com", "TRUE", "/", "TRUE", "0", "tr_session", "abc"])
        Path(self.filename).write_text(f"# Netscape HTTP Cookie File\n{line}\n")


class FakeApi:
    instances: list = []

    def __init__(self, phone, pin, cookies, *, resume=False, authenticator=False, confirm=True, reject=False):
        self.phone, self.pin, self.cookies_file = phone, pin, cookies
        self.resume, self.authenticator, self.confirm, self.reject = resume, authenticator, confirm, reject
        self._websession = type("S", (), {"cookies": FakeJar(cookies)})()
        self.calls = []
        self.seen_cookies = Path(cookies).read_text()
        FakeApi.instances.append(self)

    def resume_websession(self):
        self.calls.append("resume")
        return self.resume

    def initiate_weblogin(self):
        self.calls.append("initiate")
        if self.reject:
            r = requests.Response()
            r.status_code, r.url = 401, "https://api.traderepublic.com/api/v2/auth/web/login"
            raise requests.HTTPError(response=r)
        return 120

    @property
    def weblogin_needs_authenticator(self):
        return self.authenticator

    def complete_weblogin(self, code=None):
        self.calls.append(("complete", code))
        if not self.confirm:
            raise TimeoutError

    async def close(self):
        pass


@pytest.fixture
def api(monkeypatch):
    FakeApi.instances = []
    opts = {}
    monkeypatch.setattr(TradeRepublicAdapter, "api_factory",
                        staticmethod(lambda phone, pin, cookies: FakeApi(phone, pin, cookies, **opts)))
    return opts


def test_web_login_confirmed_in_the_app_keeps_the_cookies(api):
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm"
    assert FakeApi.instances[-1].phone == "+491700000000"
    a.complete_login("")
    a.close()
    assert "tr_session" in a.session_state()["cookies"]
    # The next run resumes the session with those cookies - no confirmation needed.
    api["resume"] = True
    b = TradeRepublicAdapter(CREDS, a.session_state())
    b.login()
    assert "tr_session" in FakeApi.instances[-1].seen_cookies
    assert FakeApi.instances[-1].calls == ["resume"]
    b.close()


def test_scheduled_run_asks_on_the_phone_and_waits(api):
    told = []
    a = TradeRepublicAdapter(CREDS, {"cookies": "expired"})
    a.on_user_action = told.append
    a.login()
    assert told and "Trade-Republic-App" in told[0]
    assert FakeApi.instances[-1].calls == ["resume", "initiate", ("complete", None)]


def test_not_confirmed_in_time(api):
    api["confirm"] = False
    a = TradeRepublicAdapter(CREDS)
    a.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="nicht rechtzeitig"):
        a.login()


def test_authenticator_code(api):
    api["authenticator"] = True
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "code"
    a.complete_login("123456")
    assert ("complete", "123456") in FakeApi.instances[-1].calls


def test_rejected_pin_is_never_tried_again(api):
    api["reject"] = True
    a = TradeRepublicAdapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    count = len(FakeApi.instances)
    with pytest.raises(AdapterError, match="nicht noch einmal"):
        TradeRepublicAdapter(CREDS, a.session_state()).login()
    assert len(FakeApi.instances) == count


# ── sync and holdings check ─────────────────────────────────────────────────
class ReplayTR(TradeRepublicAdapter):
    def __init__(self, credentials, session=None):
        super().__init__(credentials, session)
        self._replay = RECORDING

    def login(self):
        pass


def setup_sync(tmp_path):
    wf = FakeWealthfolio()
    cfg = config_mod.Config(wealthfolio_url="http://wf", public_url="http://sync:8090")
    cfg.brokers["tr"] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                portfolio_account_id="acc-depot", start_date="2026-08-01")
    config_mod.save(tmp_path, cfg)
    v = Vault(tmp_path)
    v.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    v.set_broker_credentials("tr", CREDS)
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                    notifier=notifier, adapters={"tr": ReplayTR})
    return syncer, wf, notifier


def test_sync_books_the_timeline_and_reports_lasting_deviations(tmp_path):
    syncer, wf, notifier = setup_sync(tmp_path)
    [r] = syncer.run()
    assert (r.status, r.created, r.unknown) == ("ok", 16, 1)
    assert syncer.state.balances("tr") == [{"currency": "EUR", "account": "", "amount": "1234.56",
                                             "at": syncer.state.balances("tr")[0]["at"]}]
    devs = syncer.state.reconcile("tr")["deviations"]
    # The fabricated recording doesn't add up (sold shares never bought etc.): deviations,
    # but not reported after one run.
    assert {d["key"] for d in devs} >= {"EUR", "US88032Q1094"}
    assert not any("Bestand weicht ab" in t for t, _, _ in notifier.sent)
    syncer.run()
    assert sum("Bestand weicht ab" in t for t, _, _ in notifier.sent) == 1
    syncer.run()  # same deviations: not reported again
    assert sum("Bestand weicht ab" in t for t, _, _ in notifier.sent) == 1
    # NVIDIA: bought 0.685102 and Trade Republic says 0.685102 - no deviation.
    assert "US67066G1040" not in {d["key"] for d in syncer.state.reconcile("tr")["deviations"]}


def test_compare_matches_mapped_symbols_and_missing_positions():
    wf_cash = [{"holdingType": "cash", "localCurrency": "EUR", "quantity": "100.004"}]
    wf_depot = [{"holdingType": "security", "quantity": "2", "instrument": {"symbol": "VWCE", "name": "All-World"}},
                {"holdingType": "security", "quantity": "1", "instrument": {"symbol": "OLD", "name": "Sold"}}]
    devs = compare([CashBalance("EUR", D("100"))], [Position("IE00BK5BQT80", "All-World", D("2"), "EUR")],
                   wf_cash, wf_depot, {"IE00BK5BQT80": "VWCE"})
    assert [(d.kind, d.key, d.broker, d.wealthfolio) for d in devs] == [("position", "OLD", "0", "1")]
    assert compare([CashBalance("EUR", D("100"))], None, wf_cash, wf_depot, {}) == []


def test_dashboard_shows_the_holdings_check(tmp_path):
    import re

    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    syncer, wf, _ = setup_sync(tmp_path)
    syncer.run()
    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"tr": ReplayTR}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    first_password(client)
    page = client.get("/broker").text
    assert "Abweichungen zu Wealthfolio (" in page and 'class="light yellow"' in page
    assert re.search(r"<td[^>]*>Cash EUR</td><td[^>]*>1234.56</td>", page)
    assert 'Saldo <strong class="warn-text">weicht ab</strong>' in page


def test_positions_in_the_current_and_the_older_format():
    from brokersync.adapters.tr import portfolio_positions

    assert [p.isin for p in TradeRepublicAdapter.replay(RECORDING).get_positions()] == ["US67066G1040"]
    new = {"categories": [{"positions": [{"isin": "A"}]}, {"positions": [{"isin": "B"}]}]}
    assert [p["isin"] for p in portfolio_positions(new)] == ["A", "B"]
    assert portfolio_positions({"positions": [{"instrumentId": "C"}]}) == [{"instrumentId": "C"}]
    assert portfolio_positions(None) == []


# ── CSV-imported securities under a mapped ticker ───────────────────────────
def add_csv_buy(wf, *, date="2026-09-02T09:00:00.000Z", symbol="NVDA", asset_id=None):
    """What the addon's CSV import booked for the NVIDIA buy, with the ticker the user mapped."""
    leg = dict(quantity="1", unitPrice="1", amount="111", currency="EUR", assetSymbol="", assetId="")
    wf.add_existing(accountId="acc-cash", activityType="TRANSFER_OUT", date=date, comment="Funds for buy", **leg)
    wf.add_existing(accountId="acc-depot", activityType="TRANSFER_IN", date=date, comment="Funds from Cash", **leg)
    return wf.add_existing(accountId="acc-depot", activityType="BUY", date=date, quantity="0.685102",
                           unitPrice="160.56", amount="111", fee="1", currency="EUR", comment="NVIDIA - Buy",
                           assetSymbol=symbol, assetId=asset_id or symbol, assetName="NVIDIA Corp.")


def test_a_csv_buy_under_a_ticker_is_recognised(tmp_path):
    syncer, wf, _ = setup_sync(tmp_path)
    add_csv_buy(wf)
    syncer.run()
    buys = [a for a in wf.activities if a["activityType"] == "BUY" and a["quantity"] == "0.685102"]
    assert [b["assetSymbol"] for b in buys] == ["NVDA"]  # not booked a second time under the ISIN


def test_dividend_with_another_symbol_matches_only_when_unambiguous():
    from brokersync.dedup import ExistingIndex

    payload = {"accountId": "p", "activityType": "DIVIDEND", "activityDate": "2026-09-05T10:00:00.000Z",
               "amount": "2.24", "asset": {"symbol": "US20030N1019"}}
    one = {"id": "x", "accountId": "p", "activityType": "DIVIDEND", "date": "2026-09-05T08:00:00Z", "amount": "2.24",
           "assetSymbol": "CMCSA"}
    assert ExistingIndex([one]).find("[SYNC tr:1]", [payload]) == "x"
    two = [one, {**one, "id": "y", "assetSymbol": "OTHER"}]
    assert ExistingIndex(two).find("[SYNC tr:1]", [payload]) is None


def test_duplicates_from_before_the_fix_are_found_and_only_the_sync_copy_removed(tmp_path, hidden_pages):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    syncer, wf, _ = setup_sync(tmp_path)
    csv_buy = add_csv_buy(wf)
    # What 0.3.1 booked on top: the same buy under the ISIN, with its transfer legs.
    ref = " [SYNC tr:00000000-0000-4000-8000-000000000002]"
    for acc, t in (("acc-cash", "TRANSFER_OUT"), ("acc-depot", "TRANSFER_IN")):
        wf.add_existing(accountId=acc, activityType=t, date="2026-09-02T09:00:10.000Z", quantity="1", unitPrice="1",
                        amount="111", currency="EUR", assetSymbol="$CASH-EUR", comment=f"Funds{ref}",
                        sourceGroupId="sync-tr-00000000-0000-4000-8000-000000000002")
    sync_buy = wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-09-02T09:00:12.000Z",
                               quantity="0.685102", unitPrice="160.560033", amount="111", fee="1", currency="EUR",
                               comment=f"Kauforder NVIDIA{ref}", assetSymbol="US67066G1040",
                               assetId="US67066G1040")
    syncer.state.mark("tr", "00000000-0000-4000-8000-000000000002", "imported", [sync_buy["id"]])

    app = create_app(tmp_path, wealthfolio=lambda u, p: WealthfolioClient(u, p, transport=wf.transport()),
                     adapters={"tr": ReplayTR}, notifier=RecordingNotifier(), run_in_thread=False)
    client = TestClient(app)
    first_password(client)
    syncer.state.set_reconcile("tr", [{"name": "Cash EUR", "broker": "1", "wealthfolio": "2"}])
    page = client.get("/duplicates").text
    assert "Trade Republic: 1 doppelt" in page and "NVDA" in page and "US67066G1040" in page
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post("/duplicates", data={"csrf": csrf, "broker": "tr"})
    assert "bestätige" in r.text and wf.deleted == []
    r = client.post("/duplicates", data={"csrf": csrf, "broker": "tr", "confirm": "1"})
    assert "1 doppelte Vorgänge entfernt (3 Buchungen)" in r.text and "neu berechnet" in r.text
    # The check was computed with the duplicates in it: gone until the next run.
    assert syncer.state.reconcile("tr") is None
    assert len(wf.deleted) == 3 and sync_buy["id"] == wf.deleted[-1]
    assert any(a["id"] == csv_buy["id"] for a in wf.activities)
    assert "Trade Republic: 0 doppelt" in client.get("/duplicates").text
    # The removed transaction is "existing" now: the next sync doesn't create it again.
    syncer.run()
    assert [a["assetSymbol"] for a in wf.activities if a["activityType"] == "BUY" and a["quantity"] == "0.685102"] \
        == ["NVDA"]


def test_removal_continues_when_wealthfolio_deleted_the_partner_leg_or_one_fails(tmp_path):
    from brokersync import duplicates as dup
    from brokersync.mapping import Accounts

    syncer, wf, _ = setup_sync(tmp_path)
    for n, day in ((2, "02"), (3, "03")):
        add_csv_buy(wf, date=f"2026-09-{day}T09:00:00.000Z")
        ref = f" [SYNC tr:tx-{n}]"
        for acc, t in (("acc-cash", "TRANSFER_OUT"), ("acc-depot", "TRANSFER_IN")):
            wf.add_existing(accountId=acc, activityType=t, date=f"2026-09-{day}T09:00:10.000Z", quantity="1",
                            unitPrice="1", amount="111", currency="EUR", assetSymbol="$CASH-EUR",
                            comment=f"Funds{ref}", sourceGroupId=f"sync-tr-tx-{n}")
        wf.add_existing(accountId="acc-depot", activityType="BUY", date=f"2026-09-{day}T09:00:12.000Z",
                        quantity="0.685102", unitPrice="160.560033", amount="111", fee="1", currency="EUR",
                        comment=f"Kauf{ref}", assetSymbol="US67066G1040", id=f"buy-{n}")
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    found = dup.find(client, "tr", Accounts("acc-cash", "acc-depot"))
    assert [d.tx_id for d in found] == ["tx-2", "tx-3"]
    real_delete = client.delete_activity
    client.delete_activity = lambda i: (_ for _ in ()).throw(WealthfolioError("locked")) if i == "buy-2" \
        else real_delete(i)
    deleted, done, errors = dup.remove(client, found)
    assert done == ["tx-3"] and deleted == 3 and len(errors) == 1 and "locked" in errors[0]
    # tx-2 lost its transfer pair but kept the trade: the next check finds the trade alone.
    client.delete_activity = real_delete
    again = dup.find(client, "tr", Accounts("acc-cash", "acc-depot"))
    assert [(d.tx_id, len(d.activities)) for d in again] == [("tx-2", 1)]
    assert dup.remove(client, again)[1] == ["tx-2"]
    assert not [a for a in wf.activities if "[SYNC" in (a.get("comment") or "")]


def test_login_with_a_valid_session_says_no_confirmation_was_needed(tmp_path, api):
    from fastapi.testclient import TestClient

    from brokersync.web.app import create_app

    api["resume"] = True
    Vault(tmp_path).set_broker_credentials("tr", CREDS)
    Vault(tmp_path).set_broker_session("tr", {"cookies": "still valid"})
    app = create_app(tmp_path, adapters={"tr": TradeRepublicAdapter}, notifier=RecordingNotifier(),
                     run_in_thread=False)
    client = TestClient(app)
    first_password(client)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/brokers/tr/login").text).group(1)
    assert FakeApi.instances == [] or FakeApi.instances[-1].calls == []  # showing the page asks nothing
    r = client.post("/brokers/tr/login/start", data={"csrf": token})
    assert "Sitzung ist noch gültig" in r.text
    assert FakeApi.instances[-1].calls == ["resume"]


def test_positions_are_compared_by_the_asset_the_csv_import_booked(tmp_path):
    # Wealthfolio's holdings carry no ISIN, and the addon books under the user's ticker:
    # the asset is learned from the CSV buy the sync recognised.
    syncer, wf, _ = setup_sync(tmp_path)
    add_csv_buy(wf, symbol="NVDA.US", asset_id="asset-nvda")
    syncer.run()
    assert syncer.state.assets("tr")["US67066G1040"] == {"asset_id": "asset-nvda", "symbol": "NVDA.US",
                                                         "exchange_mic": None, "name": "NVIDIA Corp."}
    devs = syncer.state.reconcile("tr")["deviations"]
    assert not any(d["key"] in ("US67066G1040", "asset-nvda") for d in devs)
    # The next NVIDIA trade is booked onto that asset, not under the ISIN.
    from brokersync import assets

    tx = next(t for t in txs() if t.isin == "US67066G1040" and t.kind == Kind.BUY)
    acts = to_activities(tx, "tr", Accounts("c", "p"), assets.mappings(syncer.state, "tr"))
    assert acts[-1]["asset"]["id"] == "asset-nvda" and acts[-1]["asset"]["symbol"] == "NVDA.US"


def test_an_existing_install_fetches_the_whole_history_once_to_learn_the_assets(tmp_path):
    seen = []

    class Recording(ReplayTR):
        def get_transactions(self, since):
            seen.append(since)
            return super().get_transactions(since)

    syncer, wf, _ = setup_sync(tmp_path)
    syncer.adapters = {"tr": Recording}
    syncer.run()
    syncer.run()
    syncer.state.db.execute("DELETE FROM meta")  # as after the update to this version
    syncer.state.db.commit()
    syncer.run()
    syncer.run()
    start = seen[0]
    assert start.date().isoformat() == "2026-08-01"
    assert seen[1] != start and seen[2] == start and seen[3] != start


# ── websocket: timeline paging, details, cash, positions ────────────────────
class FakeWsApi(FakeApi):
    """pytr's websocket side: subscriptions answer through ``recv()``, detail
    answers arrive out of order and between unrelated messages."""

    def __init__(self, phone, pin, cookies, *, pages, silent=False):
        super().__init__(phone, pin, cookies, resume=True)
        self.pages = pages  # {("transactions" | "activity", cursor): page}
        self.silent = silent
        self.queue: list[tuple[str, dict, object]] = []
        self.details: list[str] = []
        self.requested: list[tuple[str, str | None]] = []
        self.unsubscribed: list[str] = []
        self.detail_subs: set[str] = set()
        self.max_in_flight = 0
        self.closed = False
        self._next = 0

    def _subscribe(self, response) -> str:
        self._next += 1
        sub_id = str(self._next)
        self.queue.append((sub_id, {}, response))
        return sub_id

    async def timeline_transactions(self, after=None):
        self.requested.append(("transactions", after))
        return self._subscribe(self.pages[("transactions", after)])

    async def timeline_activity_log(self, after=None):
        self.requested.append(("activity", after))
        return self._subscribe(self.pages[("activity", after)])

    async def timeline_detail_v2(self, event_id):
        self._next += 1
        self.details.append(event_id)
        self.detail_subs.add(str(self._next))
        self.max_in_flight = max(self.max_in_flight, len(self.detail_subs))
        self.queue.insert(0, (str(self._next), {}, DETAILS[event_id]))  # newest request answers first
        if len(self.detail_subs) == 1:
            self.queue.insert(1, ("stray", {}, {}))  # another subscription's message
        return str(self._next)

    async def cash(self):
        return self._subscribe([{"currencyId": "EUR", "amount": 1234.56}])

    async def compact_portfolio(self):
        return self._subscribe(RECORDING["portfolio"])

    async def recv(self):
        if self.silent or not self.queue:
            await asyncio.sleep(3600)
        return self.queue.pop(0)

    async def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)
        self.detail_subs.discard(sub_id)

    async def close(self):
        self.closed = True


DETAILS = {e["id"]: e.get("details") for e in RECORDING["events"]}


def _item(event: dict) -> dict:
    """A timeline list item: the event without its details."""
    return {k: v for k, v in event.items() if k != "details"}


@pytest.fixture
def ws(monkeypatch):
    import brokersync.adapters.tr as tr_mod

    newest_first = [_item(e) for e in reversed(RECORDING["events"])]
    pages = {
        ("transactions", None): {"items": newest_first[:6], "cursors": {"after": "p2"}},
        ("transactions", "p2"): {"items": newest_first[6:], "cursors": {"after": "p3"}},
        # The activity log lists some of the same events again.
        ("activity", None): {"items": newest_first[:2], "cursors": {}},
    }
    made = []
    opts = {}

    def factory(phone, pin, cookies):
        made.append(FakeWsApi(phone, pin, cookies, pages=pages, **opts))
        return made[-1]

    monkeypatch.setattr(TradeRepublicAdapter, "api_factory", staticmethod(factory))
    monkeypatch.setattr(tr_mod, "BATCH", 3)
    return made, opts


def test_timeline_pages_until_since_and_fetches_the_details_in_batches(ws, _no_rate_limit_waits):
    made, _ = ws
    since = datetime(2026, 9, 5, tzinfo=UTC)
    a = TradeRepublicAdapter(CREDS, {"cookies": "valid"})
    a.login()
    got = a.get_transactions(since)
    api = made[-1]
    # The same transactions as the recording replay gives - details matched to their events
    # although they arrived in another order and between other messages.
    expected = TradeRepublicAdapter.replay(RECORDING).get_transactions(since)
    assert [to_dict(t) for t in got] == [to_dict(t) for t in expected]
    # Paging stops at the first event older than ``since``: p3 is never asked for.
    assert api.requested == [("transactions", None), ("transactions", "p2"), ("activity", None)]
    # Each event's details once, at most BATCH requests in flight, every subscription ended.
    assert sorted(api.details) == sorted({e["id"] for e in RECORDING["events"]
                                          if e["timestamp"] >= "2026-09-05" and e.get("action")})
    assert api.max_in_flight <= 3
    # A short pause between two groups (#149), none before the first.
    groups = -(-len(api.details) // 3)
    assert len(_no_rate_limit_waits) == groups - 1 and all(0.25 <= w <= 0.5 for w in _no_rate_limit_waits)
    assert "00000000-0000-4000-8000-000000000014" not in api.details  # address change: no detail view
    assert len(api.unsubscribed) == len(api.details) + len(api.requested)
    a.close()


def test_cash_and_positions_over_the_websocket(ws):
    a = TradeRepublicAdapter(CREDS, {"cookies": "valid"})
    a.login()
    assert a.get_cash() == [CashBalance("EUR", D("1234.56"))]
    assert [p.isin for p in a.get_positions()] == ["US67066G1040"]
    a.close()


def test_close_ends_the_connection_and_removes_the_cookie_file(ws):
    made, _ = ws
    a = TradeRepublicAdapter(CREDS, {"cookies": "valid"})
    a.login()
    a.get_cash()
    cookies = Path(made[-1].cookies_file)
    assert cookies.exists()
    a.close()
    assert made[-1].closed and not cookies.exists()
    a.close()  # twice is harmless


def test_a_silent_websocket_is_an_error_not_a_hang(ws, monkeypatch):
    import brokersync.adapters.tr as tr_mod

    made, opts = ws
    opts["silent"] = True
    monkeypatch.setattr(tr_mod, "TIMEOUT", 0.05)
    a = TradeRepublicAdapter(CREDS, {"cookies": "valid"})
    a.login()
    with pytest.raises(AdapterError, match="Trade Republic"):
        a.get_transactions(None)
    a.close()
    assert not a.session.get("pin_rejected")


def test_too_many_attempts_and_other_errors_do_not_block_the_pin(api, monkeypatch):
    for error, message in ((Exception("TOO_MANY_REQUESTS"), "Trade Republic begrenzt gerade die Abrufe"),
                           (ConnectionError("network down"), "Trade Republic: network down")):
        monkeypatch.setattr(FakeApi, "initiate_weblogin", lambda self, e=error: (_ for _ in ()).throw(e))
        a = TradeRepublicAdapter(CREDS)
        with pytest.raises(AdapterError, match=message):
            a.login()
        # Not a rejected PIN: the next run may try again.
        assert not a.session_state().get("pin_rejected")


def test_amounts_given_as_text_keep_their_digits():
    from brokersync.adapters.tr import _dec

    assert _dec("0.1") == D("0.1") and _dec("1234567.89") == D("1234567.89") and _dec(0.1) == D("0.1")
    assert _dec("12.50") == D("12.5") and _dec(None) is None


def test_pytr_still_has_the_internals_the_cookie_store_uses():
    """#113: the adapter reads the cookies from pytr's private ``_websession``. An update of pytr that renames
    it would silently cost the stored login - this test fails first."""
    from pytr.api import TradeRepublicApi

    api = TradeRepublicApi.__new__(TradeRepublicApi)
    TradeRepublicApi.__init__(api, phone_no="+4900000000", pin="0000", save_cookies=False)
    assert hasattr(api, "_websession") and hasattr(api._websession, "cookies")
