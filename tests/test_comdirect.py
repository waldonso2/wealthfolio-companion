"""comdirect: login with session TAN and the lock-out guard, against a fake API."""

import json
from datetime import date
from decimal import Decimal as D

import httpx
import pytest

from brokersync.adapters import AdapterError, AuthRequired
from brokersync.adapters import comdirect as cd
from brokersync.adapters.comdirect import ComdirectAdapter
from brokersync.model import Kind

CREDS = {"username": "12345678", "pin": "TEST-PIN", "client_id": "test-client", "client_secret": "test-secret"}


class FakeComdirect:
    """The login calls of comdirect's API, with photoTAN-Push (status link) or mobileTAN."""

    def __init__(self, typ="P_TAN_PUSH", link=True, pin="TEST-PIN", tan="123456"):
        self.typ, self.link, self.pin, self.tan = typ, link, pin, tan
        self.approved = False
        self.challenges = 0
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/oauth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            grant = form["grant_type"]
            if grant == "password":
                if form["password"] != self.pin:
                    return httpx.Response(401, json={"error": "invalid_grant", "error_description": "Bad credentials"})
                return httpx.Response(200, json={"access_token": "first", "refresh_token": "r1", "expires_in": 599})
            if grant == "cd_secondary":
                return httpx.Response(200, json={"access_token": "full", "refresh_token": "r2", "expires_in": 599})
            if grant == "refresh_token":
                if form["refresh_token"] == "expired":
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "refreshed", "refresh_token": "r3", "expires_in": 599})
        assert json.loads(request.headers["x-http-request-info"])["clientRequestId"]["sessionId"]
        if path == "/api/session/clients/user/v1/sessions":
            return httpx.Response(200, json=[{"identifier": "SESSION-1", "sessionTanActive": False,
                                              "activated2FA": False}])
        if path.endswith("/validate"):
            self.challenges += 1
            info = {"id": f"CH-{self.challenges}", "typ": self.typ, "availableTypes": [self.typ]}
            if self.typ == "M_TAN":
                info["challenge"] = "+49-160-99XXXX"
            if self.typ == "P_TAN_PUSH" and self.link:
                info["link"] = {"href": f"/api/session/v1/authentications/CH-{self.challenges}", "rel": "self",
                                "method": "GET"}
            return httpx.Response(201, json={"identifier": "SESSION-1"},
                                  headers={"x-once-authentication-info": json.dumps(info)})
        if path.startswith("/api/session/v1/authentications/"):
            return httpx.Response(200, json={"status": "AUTHENTICATED" if self.approved else "PENDING"})
        if request.method == "PATCH":
            if self.typ == "M_TAN" and request.headers.get("x-once-authentication") != self.tan:
                return httpx.Response(422, json={"code": "TAN_UNGUELTIG",
                                                 "messages": [{"severity": "ERROR", "message": "TAN ungültig"}]})
            if self.typ == "P_TAN_PUSH" and not self.approved:
                return httpx.Response(422, json={"code": "expired"})
            return httpx.Response(200, json={"identifier": "SESSION-1", "sessionTanActive": True,
                                             "activated2FA": True})
        return httpx.Response(404, json={})


@pytest.fixture
def api(monkeypatch):
    fake = FakeComdirect()
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    monkeypatch.setattr(cd, "PUSH_POLL", 0)
    monkeypatch.setattr(cd, "CONFIRM_WAIT", 0)
    monkeypatch.setattr(cd, "PUSH_WAIT", 0)
    return fake


def test_web_login_with_photo_tan_push(api):
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "confirm" and "photoTAN-App" in e.value.challenge.message
    assert a.session_state()["open_challenges"] == 1
    # Not approved yet: asked again, no activation tried (it could count as a wrong TAN).
    with pytest.raises(AuthRequired, match="Noch nicht bestätigt"):
        a.complete_login("")
    assert not any(c.startswith("PATCH") for c in api.calls)
    api.approved = True
    a.complete_login("")
    s = a.session_state()
    assert (s["access_token"], s["refresh_token"], s["open_challenges"]) == ("full", "r2", 0)
    assert api.challenges == 1


def test_scheduled_run_asks_on_the_phone_and_waits(api):
    s = ComdirectAdapter(CREDS, {"tan_type": "P_TAN_PUSH"})  # known from the web login
    notes: list[str] = []
    s.on_user_action = notes.append
    with pytest.raises(AuthRequired, match="nicht rechtzeitig"):
        s.login()
    assert notes and "photoTAN-App" in notes[0]
    # The open challenge is kept; once approved the next run gets through.
    api.approved = True
    again = ComdirectAdapter(CREDS, s.session_state())
    again.on_user_action = notes.append
    again.login()
    assert again.session_state()["access_token"] == "full"


def test_a_valid_or_refreshable_session_needs_no_tan(api):
    a = ComdirectAdapter(CREDS, {"access_token": "full", "refresh_token": "r2", "expires_at": 0})
    a.login()
    assert a.session_state()["access_token"] == "refreshed" and api.challenges == 0
    b = ComdirectAdapter(CREDS, {"access_token": "full", "refresh_token": "expired", "expires_at": 0})
    with pytest.raises(AuthRequired):
        b.login()
    assert api.challenges == 1


def test_too_many_open_challenges_stop_before_comdirect_locks(api):
    session: dict = {}
    for _ in range(cd.MAX_OPEN_CHALLENGES):
        a = ComdirectAdapter(CREDS, session)
        with pytest.raises(AuthRequired):
            a.login()
        session = a.session_state()
    with pytest.raises(AdapterError, match="nicht bestätigt"):
        ComdirectAdapter(CREDS, session).login()
    assert api.challenges == cd.MAX_OPEN_CHALLENGES < 5


def test_a_rejected_pin_is_never_retried(api):
    api.pin = "OTHER"
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AdapterError, match="abgelehnt"):
        a.login()
    calls = len(api.calls)
    with pytest.raises(AdapterError, match="neu speichern"):
        ComdirectAdapter(CREDS, a.session_state()).login()
    assert len(api.calls) == calls


def test_mobile_tan_typed_in_and_a_second_wrong_tan_blocks(monkeypatch):
    fake = FakeComdirect(typ="M_TAN")
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired) as e:
        a.login()
    assert e.value.challenge.kind == "code" and "+49-160-99XXXX" in e.value.challenge.message
    with pytest.raises(AuthRequired, match="abgelehnt"):
        a.complete_login("000000")
    with pytest.raises(AdapterError, match="zweimal abgelehnt"):
        a.complete_login("111111")
    assert a.session_state()["pin_rejected"]

    ok = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired):
        ok.login()
    ok.complete_login("123456")
    assert ok.session_state()["access_token"] == "full"
    # A scheduled run doesn't open a challenge it can't answer (TAN to type).
    sched = ComdirectAdapter(CREDS, {"tan_type": "M_TAN"})
    sched.on_user_action = lambda m: None
    before = fake.challenges
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        sched.login()
    assert fake.challenges == before


def test_photo_tan_graphic_is_shown():
    a = ComdirectAdapter(CREDS)
    a._pending = {"typ": "P_TAN", "challenge": "iVBORw0KGgo="}
    c = a._challenge()
    assert c.kind == "code" and c.image == "data:image/png;base64,iVBORw0KGgo="


def test_trade_settlement_and_unsettled_trades():
    def booking(ref, day, amount, text="WP-ABRECHNUNG"):
        return {"reference": ref, "bookingStatus": "BOOKED", "bookingDate": day, "amount": {"value": amount,
                "unit": "EUR"}, "transactionType": {"key": "SECURITIES", "text": "Wertpapier"},
                "remittanceInfo": f"01{text:<35}"}

    def trade(tid, day, kind, value, isin):
        return {"transactionId": tid, "bookingStatus": "BOOKED", "businessDate": day,
                "quantity": {"value": "2", "unit": "XXX"}, "instrument": {"isin": isin, "wkn": "", "name": "TEST"},
                "transactionValue": {"value": value, "unit": "EUR"}, "transactionType": {"key": kind}}

    from datetime import date
    # Two buys of the same amount: each takes the booking that names its ISIN.
    bookings = [booking("B1", "2026-03-05", "-101.00", "KAUF DE000TEST00B"),
                booking("B2", "2026-03-05", "-101.00", "KAUF DE000TEST00A")]
    trades = [trade("T1", "2026-03-04", "BUY", "100", "DE000TEST00A"),
              trade("T2", "2026-03-04", "BUY", "100", "DE000TEST00B")]
    txs = {t.id: t for t in cd.to_transactions(bookings, trades, {}, date(2026, 3, 20))}
    assert set(txs) == {"dep-T1", "dep-T2"}
    assert (txs["dep-T1"].net, txs["dep-T1"].fee) == (D("101.00"), D("1.00"))
    # A booking far off in amount (costs over half the value) or date isn't the settlement.
    far = cd.to_transactions([booking("B3", "2026-03-20", "-101"), booking("B4", "2026-03-05", "-300")],
                             [trade("T3", "2026-03-04", "BUY", "100", "DE000TEST00A")], {}, date(2026, 3, 20))
    # Unused giro bookings aren't booked at all: only dividends land on the cash account.
    assert [(t.id, t.kind.value) for t in far] == [("dep-T3", "UNKNOWN")]
    assert all(t.external_cash for t in txs.values())


def test_a_push_without_status_link_is_left_to_the_web_ui(monkeypatch):
    fake = FakeComdirect(link=False)
    monkeypatch.setattr(ComdirectAdapter, "transport", httpx.MockTransport(fake.handle))
    s = ComdirectAdapter(CREDS, {"tan_type": "P_TAN_PUSH"})
    s.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        s.login()
    again = ComdirectAdapter(CREDS, s.session_state())
    again.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        again.login()
    assert fake.challenges == 1  # the second run didn't ask again
    # In the web UI the user confirms; then the activation is tried once.
    web = ComdirectAdapter(CREDS, again.session_state())
    with pytest.raises(AuthRequired):
        web.login()
    fake.approved = True
    web.complete_login("")
    assert web.session_state()["access_token"] == "full"


def test_without_a_start_date_comdirect_is_asked_for_ten_years_and_without_if_it_refuses():
    seen: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append({"path": request.url.path, **params})
        if request.url.path.endswith("/accounts/balances"):
            return httpx.Response(200, json={"paging": {"matches": 1}, "values": [
                {"accountId": "A", "account": {"accountType": {"key": "CA"}}, "balance": {"value": "1"}}]})
        if request.url.path.endswith("/v3/depots"):
            return httpx.Response(200, json={"paging": {"matches": 1}, "values": [{"depotId": "D"}]})
        if "/brokerage/" in request.url.path and request.url.path.endswith("/transactions") \
                and "min-bookingDate" in params:
            return httpx.Response(422, json={"code": "request.object.invalid"})
        return httpx.Response(200, json={"paging": {"matches": 0}, "values": []})

    from datetime import date
    a = ComdirectAdapter(CREDS, {"access_token": "full", "expires_at": 4102444800})
    a._http = httpx.Client(transport=httpx.MockTransport(handle))
    a.today = date(2026, 10, 10)
    assert a.get_transactions(None) == []
    giro = [c for c in seen if "/banking/v1/" in c["path"]]
    depot = [c for c in seen if "/brokerage/v3/depots/D/transactions" in c["path"]]
    assert giro[0]["min-bookingDate"] == "2016-10-10"
    assert depot[0]["min-bookingDate"] == "2016-10-10" and "min-bookingDate" not in depot[1]


def test_the_token_never_goes_to_a_status_link_of_another_host(api, monkeypatch):
    """#107: the challenge's status link gets the bearer token - only comdirect's API, relative or absolute."""
    a = ComdirectAdapter(CREDS)
    with pytest.raises(AuthRequired):
        a.login()
    a._pending["link"] = "https://evil.example/collect"
    with pytest.raises(AdapterError, match="Status-Link"):
        a._wait_push(0)
    assert not any("evil.example" in c for c in api.calls)
    a._pending["link"] = "https://api.comdirect.de/api/session/v1/authentications/CH-1"
    api.approved = True
    assert a._wait_push(0)


def test_a_scheduled_run_with_an_unknown_tan_type_opens_no_challenge(api):
    """Credentials saved anew (session empty): only the web UI may ask comdirect for a TAN - it could be one
    to type in, and every unanswered challenge counts towards comdirect's five."""
    s = ComdirectAdapter(CREDS)
    s.on_user_action = lambda m: None
    with pytest.raises(AuthRequired, match="Weboberfläche"):
        s.login()
    assert api.challenges == 0


def test_a_trade_without_giro_booking_is_reported_while_still_fetched():
    """It must turn UNKNOWN before it drops out of the sync's window (overlap of 7 days)."""
    from brokersync.sync import OVERLAP

    assert OVERLAP.days > cd.UNSETTLED_DAYS


def test_a_reversed_dividend_is_reported():
    booking = {"reference": "R-STORNO", "bookingStatus": "BOOKED", "bookingDate": "2026-03-02",
               "transactionType": {"key": "ZINSEN_DIVIDENDEN"}, "amount": {"value": "-12.34", "unit": "EUR"},
               "remittanceInfo": "STORNO DIVIDENDE TEST AG ISIN DE0000000001"}
    [tx] = cd.to_transactions([booking], [], {}, today=date(2026, 3, 5))
    assert tx.kind == Kind.UNKNOWN and tx.raw_type == "DIVIDENDE_STORNO" and tx.id == "acc-R-STORNO"


def remittance(*lines: str) -> str:
    """comdirect's posting text: each line of 35 characters behind its two-digit number."""
    return "".join(f"{i:02d}{line:<35}" for i, line in enumerate(lines, 1))


def dividend(*lines: str, amount="73.07", unit="EUR", isin="DE000TEST001") -> dict:
    return {"reference": "R-DIV", "bookingStatus": "BOOKED", "bookingDate": "2026-04-02",
            "transactionType": {"key": "INTEREST_DIVIDENDS", "text": "Zinsen / Dividenden"},
            "amount": {"value": amount, "unit": unit},
            "remittanceInfo": remittance("Dividendengutschrift", f"{isin} TEST AG", *lines)}


DOMESTIC = ("Brutto 100,00 EUR", "Kapitalertragsteuer 23,74 EUR", "Solidaritaetszuschl. 1,30 EUR",
            "Kirchensteuer 1,89 EUR")
FOREIGN = ("Bruttodividende 20,00 USD", "Quellensteuer 15 % 3,00 USD", "Devisenkurs EUR/USD 1,0850",
           "KapESt 0,78 EUR", "SolZ 0,04 EUR")


@pytest.mark.parametrize(("lines", "net", "tax"), [
    (DOMESTIC, "73.07", "26.93"),
    # Abbreviated labels, amounts without currency (= the booking's), thousands separator.
    (("BRUTTO 1.250,00", "KAPST 312,50", "SOLZ 17,18"), "920.32", "329.68"),
    # Foreign: gross and withholding in USD at the rate given (18,43 - 2,76 - 0,78 - 0,04 EUR).
    (FOREIGN, "14.85", "3.58"),
    # Only withholding tax (the saver's allowance covers the German taxes).
    (("Brutto 10,00 USD", "Quellensteuer 1,50 USD", "Devisenkurs 1,2500"), "6.80", "1.20"),
])
def test_the_tax_of_a_dividend_is_read_from_its_posting_text(lines, net, tax):
    [tx] = cd.to_transactions([dividend(*lines, amount=net)], [], {}, today=date(2026, 4, 5))
    assert tx.kind == Kind.DIVIDEND and tx.id == "acc-R-DIV" and tx.net == D(net)
    assert tx.tax == D(tax) and tx.gross == D(net) + D(tax)


@pytest.mark.parametrize(("lines", "net"), [
    ((), "12.34"),  # nothing but the net amount (the usual case)
    (DOMESTIC[1:], "73.07"),  # no gross: nothing to check against
    (DOMESTIC, "73.08"),  # doesn't add up to the booked amount
    (("Brutto 100,00 EUR",), "100.00"),  # no tax at all
    (FOREIGN[:2] + FOREIGN[3:], "14.85"),  # USD without an exchange rate
    (("Brutto 20,00 USD", "Quellensteuer 3,00 CHF", "Devisenkurs EUR/USD 1,0850"), "15.67"),  # a third currency
    ((*DOMESTIC, "Kirchensteuer 0,00 EUR"), "73.07"),  # a label twice: ambiguous
    (("Brutto 100,00 EUR", "Kapitalertragsteuer 25,00 %"), "75.00"),  # a rate is no amount
])
def test_a_dividend_whose_tax_cant_be_read_reliably_stays_net(lines, net):
    [tx] = cd.to_transactions([dividend(*lines, amount=net)], [], {}, today=date(2026, 4, 5))
    assert tx.kind == Kind.DIVIDEND and tx.net == D(net) and tx.tax == 0 and tx.gross is None


# ── the whole giro account, for the spending tracker (full_giro) ──────────────────────────────────────────────
def _giro_booking(ref, day, amount, kind="Kartenzahlung", text="", **extra):
    return {"reference": ref, "bookingStatus": "BOOKED", "bookingDate": day, "amount": {"value": amount, "unit": "EUR"},
            "transactionType": {"key": kind.upper(), "text": kind}, "remittanceInfo": f"01{text:<35}", **extra}


GIRO = [
    _giro_booking("K1", "2026-03-02", "-12.50", text="REWE MARKT", creditor={"holderName": "REWE", "iban": "DE00"}),
    _giro_booking("G1", "2026-03-03", "2000.00", "Überweisung", "GEHALT", remitter={"holderName": "Muster GmbH"}),
    _giro_booking("F1", "2026-03-04", "-3.90", "Entgelt", "KONTOFUEHRUNG"),
    _giro_booking("Z1", "2026-03-31", "0.52", "Zinsen / Dividenden", "HABENZINSEN"),
    _giro_booking("D1", "2026-03-10", "8.00", "Zinsen / Dividenden", "DIVIDENDE DE000TEST001"),
    _giro_booking("S1", "2026-03-05", "-101.00", "Wertpapier", "KAUF DE000TEST00A"),
    _giro_booking("S2", "2026-02-10", "-50.00", "Wertpapier", "SPARPLAN OHNE DEPOTUMSATZ"),
    _giro_booking("S3", "2026-04-08", "-25.00", "Wertpapier", "KAUF NOCH OFFEN"),
]
DEPOT = [{"transactionId": "T1", "bookingStatus": "BOOKED", "businessDate": "2026-03-04", "quantity": {"value": "2"},
          "instrument": {"isin": "DE000TEST00A", "wkn": "", "name": "TEST"}, "transactionValue": {"value": "100"},
          "transactionType": {"key": "BUY"}}]


def test_the_whole_giro_account_is_booked_when_the_spending_tracker_is_on():
    today = date(2026, 4, 10)
    off = {t.id for t in cd.to_transactions(GIRO, DEPOT, {}, today)}
    assert off == {"dep-T1", "acc-D1"}  # the usual: the depot and its dividends only

    on = {t.id: t for t in cd.to_transactions(GIRO, DEPOT, {}, today, full_giro=True)}
    assert set(on) == {"dep-T1", "acc-D1", "acc-K1", "acc-G1", "acc-F1", "acc-Z1", "acc-S2"}  # S3 may still match
    assert not on["dep-T1"].external_cash and on["dep-T1"].net == D("101.00")  # a real transfer giro -> depot
    assert (on["acc-K1"].kind, on["acc-K1"].net, on["acc-K1"].counterparty) == (Kind.WITHDRAWAL, D("12.50"), "REWE")
    assert (on["acc-G1"].kind, on["acc-G1"].counterparty) == (Kind.DEPOSIT, "Muster GmbH")
    assert on["acc-F1"].kind == Kind.FEE and on["acc-Z1"].kind == Kind.INTEREST
    assert on["acc-S2"].kind == Kind.WITHDRAWAL  # a securities booking that never got a depot transaction
    assert on["acc-D1"].kind == Kind.DIVIDEND  # not booked twice as a deposit


def test_the_opening_deposit_brings_the_cash_account_to_the_real_balance():
    today = date(2026, 4, 10)
    moved = sum(D(b["amount"]["value"]) for b in GIRO)  # S3 counts: the bank moved that money
    txs = {t.id: t for t in cd.to_transactions(GIRO, DEPOT, {}, today, full_giro=True, opening_balance=D("5000"))}
    opening = txs["giro-opening"]
    assert (opening.kind, opening.net) == (Kind.DEPOSIT, D("5000") - moved)
    assert opening.datetime < min(t.datetime for t in txs.values() if t.id != "giro-opening")
    # A balance below what the bookings moved is an opening withdrawal; an exact match books nothing.
    low = {t.id: t for t in cd.to_transactions(GIRO, DEPOT, {}, today, full_giro=True, opening_balance=D("-5000"))}
    assert low["giro-opening"].kind == Kind.WITHDRAWAL
    assert "giro-opening" not in {t.id for t in cd.to_transactions(GIRO, DEPOT, {}, today, full_giro=True,
                                                                   opening_balance=moved)}


def test_the_adapter_derives_the_opening_only_in_the_first_run_after_a_reset(monkeypatch):
    a = ComdirectAdapter({}, {})
    a.full_giro = True
    a.today = date(2026, 4, 10)
    a._depots = []
    a._positions = []
    monkeypatch.setattr(a, "_giro", lambda: {"accountId": "A", "balance": {"value": "5000", "unit": "EUR"}})
    monkeypatch.setattr(a, "_dated_pages", lambda path, first, **kw: GIRO if "banking" in path else [])
    assert "giro-opening" in {t.id for t in a.get_transactions(None)}
    a.known_ids = {"acc-K1"}  # something is synced: no second opening
    assert "giro-opening" not in {t.id for t in a.get_transactions(None)}
    a.full_giro = False
    a.known_ids = set()
    assert {t.id for t in a.get_transactions(None)} == {"acc-D1"}  # the usual mode: only the dividend


def test_a_trade_is_a_real_transfer_from_the_giro_account_with_the_tracker_and_an_external_deposit_without():
    from brokersync.mapping import Accounts, to_activities

    for full, first in ((True, "TRANSFER_OUT"), (False, "DEPOSIT")):
        trade = next(t for t in cd.to_transactions(GIRO, DEPOT, {}, date(2026, 4, 10), full_giro=full)
                     if t.id == "dep-T1")
        types = [a["activityType"] for a in to_activities(trade, "comdirect", Accounts("cash", "depot"))]
        assert types == [first, *(["TRANSFER_OUT", "TRANSFER_IN", "BUY"] if not full else
                                  ["TRANSFER_IN", "BUY"])], full
