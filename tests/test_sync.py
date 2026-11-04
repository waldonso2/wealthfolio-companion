from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest

from brokersync import config as config_mod
from brokersync.adapters import AdapterError, BrokerAdapter
from brokersync.model import Kind, Transaction
from brokersync.notify import Notifier
from brokersync.sync import BERLIN, AlreadyRunning, Syncer, run_lock
from brokersync.vault import Vault
from brokersync.wealthfolio import WealthfolioClient

from .fake_broker import FakeBroker
from .fakes import PASSWORD, FakeWealthfolio


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__("https://ntfy.example", "topic")
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, title, message, *, link=None, priority="default", tags=""):
        self.sent.append((title, message, link))
        return True


class BrokenAdapter(BrokerAdapter):
    key = "broken"
    label = "Kaputt"

    def login(self):
        raise AdapterError("broker is down")

    def get_positions(self):
        return []

    def get_transactions(self, since):
        return []

    def get_cash(self):
        return []


# Login to the fake broker at 16:00 German time: deposit 13:00, buy 14:00, dividend 15:00.
ANCHOR = "2026-01-05T15:00:00+00:00"


def setup(tmp_path, wf: FakeWealthfolio, brokers=("fake",), confirmed=True):
    cfg = config_mod.Config(wealthfolio_url="http://wf.local:8080", public_url="http://sync.local:8090")
    for key in brokers:
        cfg.brokers[key] = config_mod.BrokerConfig(enabled=True, cash_account_id="acc-cash",
                                                   portfolio_account_id="acc-depot")
    config_mod.save(tmp_path, cfg)
    vault = Vault(tmp_path)
    vault.update(lambda d: d.__setitem__("wealthfolio_password", PASSWORD))
    for key in brokers:
        vault.set_broker_credentials(key, {"username": "test"})
    if confirmed:
        vault.set_broker_session("fake", {"confirmed": True, "anchor": ANCHOR})
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, wealthfolio=lambda url, pw: WealthfolioClient(url, pw, transport=wf.transport()),
                    notifier=notifier, adapters={"fake": FakeBroker, "broken": BrokenAdapter})
    return syncer, notifier


def test_first_run_books_everything_second_run_nothing(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.status, r.created, r.existing, r.failed, r.unknown) == ("ok", 3, 0, 0, 1)
    types = [a["activityType"] for a in wf.activities]
    assert types == ["DEPOSIT", "TRANSFER_OUT", "TRANSFER_IN", "BUY", "DIVIDEND", "TRANSFER_OUT", "TRANSFER_IN"]
    # The unknown event is reported once, with a link to the list.
    assert [(t, link) for t, _, link in notifier.sent] == [
        ("Testbroker: 1 unbekannte Buchungen", "http://sync.local:8090/unknown")]
    assert syncer.state.unknown_events()[0]["raw_type"] == "FAKE_SPECIAL_EVENT"
    # Holdings check: 502.68 EUR cash and 5 shares, as the fake broker reports.
    assert syncer.state.reconcile("fake")["deviations"] == []

    posted = len(wf.created)
    [r2] = syncer.run()
    # Fetches from the last success minus the overlap, so the old events aren't even read again.
    assert (r2.status, r2.created, r2.existing) == ("ok", 0, 0)
    assert len(wf.created) == posted  # nothing even sent
    assert len(notifier.sent) == 1  # the unknown event isn't reported again


def test_lost_state_is_recovered_from_the_comments(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    count = len(wf.activities)
    (tmp_path / "state.db").unlink()
    syncer2, _ = setup(tmp_path, wf)
    [r] = syncer2.run()
    assert (r.created, r.existing) == (0, 3)
    assert len(wf.activities) == count
    # Every leg came back as a duplicate: its ids are still stored (#134 M1), not an empty list.
    present = {a["id"] for a in wf.activities}
    synced = syncer2.state.synced("fake")
    assert synced and all(ids and set(ids) <= present for status, ids in synced.values() if status == "imported")


def test_activities_from_the_addons_csv_import_are_recognised(tmp_path):
    wf = FakeWealthfolio()
    # What the addon's CSV import booked for the same buy: other comment and time.
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-01-05T13:00:31.123Z", quantity="5",
                    unitPrice="100", amount="501", fee="1", currency="EUR", comment="Buy Testfonds [13:00:31.123]",
                    assetSymbol="IE00B4L5Y983", assetId="IE00B4L5Y983")
    wf.add_existing(accountId="acc-cash", activityType="DEPOSIT", date="2026-01-05T12:00:05.000Z", quantity="1",
                    unitPrice="1", amount="1000", currency="EUR", comment="Einzahlung", assetSymbol="", assetId="")
    syncer, _ = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.created, r.existing) == (1, 2)
    assert [a["activityType"] for a in wf.created] == ["DIVIDEND", "TRANSFER_OUT", "TRANSFER_IN"]


def test_partial_failure_is_retried_without_duplicating_created_legs(tmp_path):
    wf = FakeWealthfolio()
    wf.fail_types = {"BUY"}
    syncer, notifier = setup(tmp_path, wf)
    [r] = syncer.run()
    assert (r.status, r.created, r.failed) == ("error", 2, 1)
    assert "simulated failure" in r.messages[0]
    assert any("nicht übernommen" in t for t, _, _ in notifier.sent)
    wf.fail_types = set()
    [r2] = syncer.run()
    assert (r2.status, r2.created) == ("ok", 1)
    # Each transfer leg of the buy exists once.
    legs = [a for a in wf.activities if a.get("sourceGroupId") == "sync-fake-TEST-20260105-2"]
    assert sorted(a["activityType"] for a in legs) == ["TRANSFER_IN", "TRANSFER_OUT"]


def test_a_failing_broker_is_reported_and_the_others_still_run(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, brokers=("broken", "fake"))
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "error"
    assert results["fake"].status == "ok" and results["fake"].created == 3
    assert ("Kaputt: Abruf fehlgeschlagen", "broker is down", "http://sync.local:8090/broker") in notifier.sent


def test_needs_auth_notifies_with_the_login_link(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, confirmed=False)
    [r] = syncer.run()
    assert r.status == "needs_auth"
    assert notifier.sent[0][0] == "Testbroker: Anmeldung nötig"
    assert notifier.sent[0][2] == "http://sync.local:8090/brokers/fake/login"
    assert wf.created == []


def test_since_uses_last_success_with_overlap(tmp_path):
    seen = []

    class Recording(FakeBroker):
        def get_transactions(self, since):
            seen.append(since)
            return []

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"fake": Recording}
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"].start_date = "2026-01-01"
    config_mod.save(tmp_path, cfg)
    syncer.run()
    syncer.run()
    assert seen[0] == datetime(2026, 1, 1, tzinfo=BERLIN)
    assert seen[1] is not None and seen[1] < datetime.now(UTC)


def test_refetch_from_the_start_date_once(tmp_path):
    from brokersync.sync import refetch_flag

    seen = []

    class Recording(FakeBroker):
        def get_transactions(self, since):
            seen.append(since)
            return []

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"fake": Recording}
    syncer.run()
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"].start_date = "2025-06-01"
    config_mod.save(tmp_path, cfg)
    syncer.run()
    assert seen[1] != datetime(2025, 6, 1, tzinfo=BERLIN)  # a later start date alone changes nothing
    syncer.state.set_flag(refetch_flag("fake"))
    syncer.run()
    syncer.run()
    assert seen[2] == datetime(2025, 6, 1, tzinfo=BERLIN)
    assert seen[3] != seen[2]  # only once
    assert not syncer.state.flag(refetch_flag("fake"))


def test_a_failed_refetch_is_tried_again(tmp_path):
    from brokersync.sync import refetch_flag

    wf = FakeWealthfolio()
    wf.fail_types = {"BUY"}
    syncer, _ = setup(tmp_path, wf)
    syncer.state.set_flag(refetch_flag("fake"))
    [r] = syncer.run()
    assert r.failed and syncer.state.flag(refetch_flag("fake"))


def test_inconsistent_transactions_fail_visibly(tmp_path):
    class Bad(FakeBroker):
        def get_transactions(self, since):
            return [Transaction("b1", Kind.BUY, datetime(2026, 2, 1, tzinfo=UTC), "EUR", D("999"), isin="IE00B4L5Y983",
                                name="X", shares=D(1), gross=D(100), label="Kauf")]

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"fake": Bad}
    [r] = syncer.run()
    assert (r.status, r.failed) == ("error", 1)
    assert "don't add up" in r.messages[0]
    assert wf.created == []


def test_only_one_run_at_a_time(tmp_path):
    with run_lock(tmp_path), pytest.raises(AlreadyRunning), run_lock(tmp_path):
        pass


def test_wealthfolio_unreachable_is_an_error_not_a_crash(tmp_path):
    def down(request):
        raise httpx.ConnectError("connection refused")

    syncer, notifier = setup(tmp_path, FakeWealthfolio())
    syncer._wealthfolio = lambda url, pw: WealthfolioClient(url, pw, transport=httpx.MockTransport(down))
    [r] = syncer.run()
    assert r.status == "error" and "not reachable" in r.messages[0]
    assert notifier.sent[-1][0] == "Testbroker: Abruf fehlgeschlagen"


def test_a_failed_sweep_after_a_created_dividend_is_completed(tmp_path):
    wf = FakeWealthfolio()
    wf.fail_types = {"TRANSFER_OUT"}
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    assert [a["activityType"] for a in wf.activities] == ["DEPOSIT", "DIVIDEND"]
    wf.fail_types = set()
    syncer.run()
    div_group = [a["activityType"] for a in wf.activities if "TEST-20260105-3" in a["comment"]]
    assert sorted(div_group) == ["DIVIDEND", "TRANSFER_IN", "TRANSFER_OUT"]
    assert len([a for a in wf.activities if a["activityType"] == "BUY"]) == 1


def test_a_run_waits_briefly_for_the_lock_and_closes_stale_runs(tmp_path):
    import threading
    import time

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.state.start_run("fake")  # left over by a crashed process
    with run_lock(tmp_path):
        t = threading.Thread(target=syncer.run)
        t.start()
        time.sleep(0.3)  # the status page holds the lock briefly
    t.join(10)
    runs = syncer.state.runs()
    assert [r.status for r in runs] == ["ok", "aborted"]


def test_automatic_fetch_off_skips_the_broker_unless_it_is_requested_by_name(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"].enabled = False
    config_mod.save(tmp_path, cfg)
    assert syncer.run() == [] and wf.activities == []  # timer and "Alle abrufen"
    [r] = syncer.run(["fake"])  # the broker's own button
    assert (r.status, r.created) == ("ok", 3)


def test_a_known_unknown_event_gets_the_newer_payload_but_is_not_reported_again(tmp_path):
    from brokersync.state import State

    st = State(tmp_path)
    assert st.add_unknown("tr", "e1", "X", "2026-01-01", {"eventType": "X"}) is True
    assert st.add_unknown("tr", "e1", "X", "2026-01-01", {"eventType": "X", "betrag": "1 EUR"}) is False
    assert st.unknown_events("tr")[0]["payload"]["betrag"] == "1 EUR"


def test_an_unknown_event_booked_later_leaves_the_list(tmp_path):
    """A type the adapter learned in an update: *Ab Startdatum neu abrufen* books it, *Unbekannt* drops it."""
    from brokersync.state import State

    st = State(tmp_path)
    st.add_unknown("tr", "e1", "X", "2026-01-01", {"eventType": "X"})
    st.add_unknown("tr", "e2", "Y", "2026-01-01", {"eventType": "Y"})
    st.mark("tr", "e1", "created", ["a1"])
    assert [e["tx_id"] for e in st.unknown_events("tr")] == ["e2"]


class CrashingAdapter(BrokenAdapter):
    """A bug in an adapter: an exception the sync doesn't expect, after a login."""

    def login(self):
        self.session["token"] = "kept"

    def get_transactions(self, since):
        raise KeyError("eventType")


def test_a_crashing_adapter_is_reported_keeps_its_session_and_the_others_still_run(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, brokers=("broken", "fake"))
    syncer.adapters = {"fake": FakeBroker, "broken": CrashingAdapter}
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "error"
    assert results["broken"].messages == ["Unexpected error: 'eventType'"]
    assert ("Kaputt: Abruf fehlgeschlagen", "Unerwarteter Fehler: 'eventType'", "http://sync.local:8090/broker") \
        in notifier.sent
    assert results["fake"].status == "ok" and results["fake"].created == 3
    # The session is stored even though the run crashed after the login.
    assert syncer.vault.load()["brokers"]["broken"]["session"] == {"token": "kept"}
    assert [r.status for r in syncer.state.runs("broken")] == ["error"]


class CloseFailsAdapter(BrokenAdapter):
    """Closing fails (a temp directory that can't be removed) after a good login."""

    def login(self):
        self.session["token"] = "kept"

    def close(self):
        raise OSError("cannot remove the temp directory")


def test_a_session_is_stored_even_when_closing_the_adapter_fails(tmp_path):
    syncer, _ = setup(tmp_path, FakeWealthfolio(), brokers=("broken",))
    syncer.adapters = {"broken": CloseFailsAdapter}
    assert [r.status for r in syncer.run()] == ["error"]
    assert syncer.vault.load()["brokers"]["broken"]["session"] == {"token": "kept"}


def test_a_broker_without_wealthfolio_accounts_is_an_error_and_unknown_brokers_are_ignored(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf)
    cfg = config_mod.load(tmp_path)
    cfg.brokers["fake"].portfolio_account_id = ""
    cfg.brokers["gone"] = config_mod.BrokerConfig(enabled=True)  # an adapter this version doesn't have
    config_mod.save(tmp_path, cfg)
    [r] = syncer.run()
    assert (r.broker, r.status) == ("fake", "error")
    assert "No Wealthfolio accounts assigned" in r.messages[0]
    assert wf.activities == []


class NoBalanceBroker(FakeBroker):
    def get_cash(self):
        raise AdapterError("balance not offered")

    def get_positions(self):
        raise NotImplementedError


def test_a_broker_without_balance_still_books_but_skips_the_check(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"fake": NoBalanceBroker}
    [r] = syncer.run()
    assert (r.status, r.created) == ("ok", 3)
    assert syncer.state.balances("fake") == []
    assert syncer.state.reconcile("fake") is None


def test_holdings_wealthfolio_cannot_answer_do_not_fail_the_run(tmp_path):
    wf = FakeWealthfolio()

    def handle(request):
        if request.url.path == "/api/v1/holdings":
            return httpx.Response(500, json={"code": 500, "message": "recalculating"})
        return wf.handle(request)

    syncer, notifier = setup(tmp_path, wf)
    syncer._wealthfolio = lambda url, pw: WealthfolioClient(url, pw, transport=httpx.MockTransport(handle))
    [r] = syncer.run()
    assert (r.status, r.created) == ("ok", 3)
    assert syncer.state.balances("fake")  # the broker's balance is still shown
    assert syncer.state.reconcile("fake") is None
    assert not any("Bestand" in t for t, _, _ in notifier.sent)


def holdings(wf, account):
    cash = {h["localCurrency"]: D(h["quantity"]) for h in wf.holdings(account) if h["holdingType"] == "cash"}
    securities = {h["instrument"]["symbol"]: D(h["quantity"]) for h in wf.holdings(account)
                  if h["holdingType"] == "security"}
    return cash, securities


def test_buy_sell_and_dividend_leave_no_cash_on_the_depot_and_no_cash_position(tmp_path):
    from brokersync.mapping import Accounts, to_activities

    wf = FakeWealthfolio()
    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    when = datetime(2026, 1, 5, 10, tzinfo=UTC)
    common = dict(datetime=when, currency="EUR", isin="IE00TEST0001", name="Testfonds")
    for tx in (Transaction("d", Kind.DEPOSIT, when, "EUR", D("1000")),
               Transaction("b", Kind.BUY, net=D("501"), shares=D("5"), gross=D("500"), fee=D("1"), **common),
               Transaction("s", Kind.SELL, net=D("199"), shares=D("2"), gross=D("200"), fee=D("1"), **common),
               Transaction("v", Kind.DIVIDEND, net=D("3.68"), shares=D("3"), gross=D("5"), tax=D("1.32"), **common)):
        for p in to_activities(tx, "tr", Accounts("acc-cash", "acc-depot")):
            client.create_activity(p)
    assert holdings(wf, "acc-depot") == ({"EUR": D(0)}, {"IE00TEST0001": D(3)})
    assert holdings(wf, "acc-cash") == ({"EUR": D("701.68")}, {})


def add_old_sync_run(wf):
    """What 0.3.5 booked for the fake broker: every cash activity with a "$CASH-EUR" asset."""
    from brokersync.mapping import Accounts, to_activities

    from .fake_broker import transactions

    client = WealthfolioClient("http://wf", PASSWORD, transport=wf.transport())
    for tx in transactions(datetime.fromisoformat(ANCHOR)):
        if tx.kind == Kind.UNKNOWN:
            continue
        for p in to_activities(tx, "fake", Accounts("acc-cash", "acc-depot")):
            if "asset" not in p:
                p["asset"] = {"symbol": "$CASH-EUR"}
            client.create_activity(p)


def test_the_holdings_check_reports_cash_on_the_depot_and_a_cash_position():
    from brokersync.model import CashBalance
    from brokersync.reconcile import compare

    wf = FakeWealthfolio()
    add_old_sync_run(wf)
    # The transfers moved no money: the depot paid the buy itself and kept the dividend.
    assert holdings(wf, "acc-depot") == ({"EUR": D("-497.32")}, {"IE00B4L5Y983": D(5)})
    assert holdings(wf, "acc-cash") == ({"EUR": D("1000")}, {})
    found = compare([CashBalance("EUR", D("502.68"))], None, wf.holdings("acc-cash"), wf.holdings("acc-depot"), {})
    assert [(d.key, d.name, d.broker, d.wealthfolio) for d in found] == [
        ("EUR", "Cash EUR", "502.68", "1000"), ("depot:EUR", "Cash EUR im Depotkonto", "0", "-497.32")]
    # Five transfers in, one out, as seen in a real installation: a "$CASH" position.
    cash_position = {"holdingType": "security", "quantity": "5", "instrument": {"id": "$CASH", "symbol": "$CASH"}}
    found = compare([], [], [cash_position], [], {})
    assert [(d.key, d.name, d.broker, d.wealthfolio) for d in found] == [
        ("Cash-Konto:$CASH", "Position $CASH im Cash-Konto", "0", "5")]


def test_transfers_with_a_cash_asset_are_repaired_once(tmp_path):
    wf = FakeWealthfolio()
    add_old_sync_run(wf)
    before = {a["id"]: dict(a) for a in wf.activities}
    syncer, notifier = setup(tmp_path, wf)
    [r] = syncer.run()
    # Updated in place, not deleted and created again; nothing new booked on top.
    assert r.status == "ok" and wf.deleted == [] and (r.created, r.existing) == (0, 3)
    assert {a["id"] for a in wf.activities} == set(before)
    assert len(wf.updated) == 5 and all(u["asset"] == {} for u in wf.updated)
    assert {before[u["id"]]["activityType"] for u in wf.updated} == {"DEPOSIT", "TRANSFER_IN", "TRANSFER_OUT"}
    for a in wf.activities:
        old = before[a["id"]]
        assert {k: v for k, v in a.items() if not k.startswith("asset") and k != "_fp"} == \
            {k: v for k, v in old.items() if not k.startswith("asset") and k != "_fp"}
    assert holdings(wf, "acc-depot") == ({"EUR": D(0)}, {"IE00B4L5Y983": D(5)})
    assert holdings(wf, "acc-cash") == ({"EUR": D("502.68")}, {})
    assert syncer.state.reconcile("fake")["deviations"] == []
    assert not [t for t, _, _ in notifier.sent if "fehlgeschlagen" in t]
    # Once: the next run doesn't even look.
    updates = len(wf.updated)
    syncer.run()
    assert len(wf.updated) == updates


def test_a_failed_repair_is_reported_and_tried_again(tmp_path):
    wf = FakeWealthfolio()
    add_old_sync_run(wf)
    syncer, notifier = setup(tmp_path, wf)
    real = wf.handle
    wf.handle = lambda req: httpx.Response(400, json={"code": 400, "message": "locked"}) \
        if req.method == "PUT" else real(req)
    count = len(wf.activities)
    [r] = syncer.run()
    # Nothing booked: re-sent without the asset, the transfers would be booked a second time.
    assert r.status == "error" and "5 Überträge" in r.messages[0] and "locked" in r.messages[0]
    assert len(wf.activities) == count
    assert [t for t, _, _ in notifier.sent if "fehlgeschlagen" in t] == ["Testbroker: Abruf fehlgeschlagen"]
    wf.handle = real
    [r] = syncer.run()
    assert r.status == "ok" and len(wf.activities) == count
    assert holdings(wf, "acc-depot")[0] == {"EUR": D(0)}


def test_the_reason_of_a_failed_run_is_logged(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, "brokersync.sync")
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf, brokers=("broken",))
    syncer.run()
    assert "broken: broker is down" in caplog.text


def test_the_tax_of_a_dividend_is_added_to_one_an_import_booked_without_it(tmp_path):
    wf = FakeWealthfolio()
    # The addon's CSV import of Scalable: the dividend net, no tax (the export has none).
    imported = wf.add_existing(accountId="acc-depot", activityType="DIVIDEND", date="2026-01-05T14:00:10.000Z",
                               quantity="1", unitPrice="3.68", amount="3.68", currency="EUR",
                               comment="Dividend TEST Fonds", assetSymbol="IE00B4L5Y983", assetId="IE00B4L5Y983")
    syncer, _ = setup(tmp_path, wf)
    [r] = syncer.run()
    assert r.status == "ok"
    assert imported["tax"] == "1.32" and imported["amount"] == "3.68" and imported["comment"] == "Dividend TEST Fonds"
    assert not any(a["activityType"] == "DIVIDEND" and "[SYNC" in (a.get("comment") or "") for a in wf.activities)
    assert syncer.state.flag("dividend-tax:fake")


class FullFake(FakeBroker):
    full_history = True


def test_dividends_matched_before_get_their_tax_once(tmp_path):
    wf = FakeWealthfolio()
    imported = wf.add_existing(accountId="acc-depot", activityType="DIVIDEND", date="2026-01-05T14:00:10.000Z",
                               quantity="1", unitPrice="3.68", amount="3.68", currency="EUR",
                               comment="Dividend TEST Fonds", assetSymbol="IE00B4L5Y983", assetId="IE00B4L5Y983")
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters = {"fake": FullFake}
    # An older version matched the dividend to the import without touching its tax.
    syncer.state.mark("fake", "TEST-20260105-3", "existing", [imported["id"]])
    seen: list[set] = []
    original = FullFake.get_transactions

    def spy(self, since):
        seen.append(set(self.known_ids))
        return original(self, since)

    FullFake.get_transactions = spy
    try:
        syncer.run()
        syncer.run()
    finally:
        FullFake.get_transactions = original
    assert imported["tax"] == "1.32"
    # The first run asks for the details of the matched transaction again, later runs don't.
    assert "TEST-20260105-3" not in seen[0] and "TEST-20260105-3" in seen[1]
    assert len([u for u in wf.updated if u["id"] == imported["id"]]) == 1


def test_the_start_date_begins_at_midnight_german_time():
    """#108: a booking at 00:30 on the start date (German time) is inside the window."""
    from brokersync.sync import Syncer

    start = Syncer._start(config_mod.BrokerConfig(start_date="2026-01-01"))
    assert start.astimezone(UTC).isoformat() == "2025-12-31T23:00:00+00:00"
    summer = Syncer._start(config_mod.BrokerConfig(start_date="2026-07-01"))
    assert summer.astimezone(UTC).isoformat() == "2026-06-30T22:00:00+00:00"


def test_an_equal_deposit_after_one_synced_before_is_booked(tmp_path):
    """#134 H1: a deposit equal to one the sync booked a run before (within the 36 h window) is a booking of its
    own, not that one."""
    from dataclasses import replace

    class Again(FakeBroker):
        def get_transactions(self, since):
            txs = super().get_transactions(None)
            deposit = next(t for t in txs if t.kind == Kind.DEPOSIT)
            return [*txs, replace(deposit, id=deposit.id + "-b")]

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    syncer.adapters = {"fake": Again}
    [r] = syncer.run()
    assert r.created == 1
    assert [a["activityType"] for a in wf.activities].count("DEPOSIT") == 2


class NetDividendBroker(FakeBroker):
    """The fake broker as an adapter that books its dividend net, without the tax (comdirect before #52)."""

    def get_transactions(self, since):
        return [t if t.kind != Kind.DIVIDEND else replace(t, tax=D(0), gross=None)
                for t in super().get_transactions(since)]


class LaterTaxBroker(FakeBroker):
    own_tax_backfill = True


def test_own_dividends_get_the_tax_the_adapter_learned_later_once(tmp_path):
    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.adapters["fake"] = NetDividendBroker
    [r] = syncer.run()
    assert r.status == "ok" and r.created == 3
    [div] = [a for a in wf.activities if a["activityType"] == "DIVIDEND"]
    before = dict(div)
    assert not D(div.get("tax") or 0)
    syncer.adapters["fake"] = LaterTaxBroker
    [r] = syncer.run()
    # Only the tax changed: amount and comment (the duplicate fingerprint) stay, nothing new booked.
    assert (r.status, r.created) == ("ok", 0) and len(wf.updated) == 1
    assert div["tax"] == "1.32"
    assert {k: v for k, v in div.items() if k not in ("tax", "_fp")} == \
        {k: v for k, v in before.items() if k not in ("tax", "_fp")}
    assert syncer.state.flag("own-dividend-tax:fake")
    syncer.run()
    assert len(wf.updated) == 1
