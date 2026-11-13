"""The page *Durchschau* (waldonso2/wealthfolio-importer-addon#139): positions, holdings file upload, top stocks."""

from fastapi.testclient import TestClient

from brokersync.state import State

from .fake_broker import ISIN as FUND
from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf

STOCK = "US0378331005"
# Fabricated holdings file of the fake broker's fund: the stock at 20 %, 10 % left open.
FILE = (f"ISIN,Name,Country,Sector,Weight (%)\n{STOCK},Example Inc,United States,IT,20\n"
        "US5949181045,Other Corp,United States,IT,70\n")


def test_upload_a_holdings_file_and_see_the_stock_added_up_with_the_direct_position(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()  # 5 shares of the fund (1 EUR each in the fake)
    wf.add_existing(accountId="acc-depot", activityType="BUY", date="2026-01-02T10:00:00.000Z", amount="5",
                    quantity="5", unitPrice="1", currency="EUR", assetId=STOCK, assetSymbol=STOCK, comment="direkt")
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)

    page = client.get("/lookthrough").text  # reads the positions (inline in tests)
    page = client.get("/lookthrough").text
    assert FUND in page and "keine" in page
    token = csrf(client, "/lookthrough")
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                    files={"file": ("holdings.csv", FILE.encode(), "text/csv")})
    assert "2 Positionen gelesen, zusammen 90,00 % des Fonds" in r.text
    assert State(tmp_path).funds()[FUND]["constituents"][0][:3] == [STOCK, "Example Inc", "0.2"]
    # 5 direct + 20 % of 5 in the fund = 6 EUR of 10: the stock first, at 60 %, 1 EUR through the fund, 5 direct.
    first = r.text.split("Aktien über alle Positionen</caption>")[1].split("<tbody>")[1].split("</tr>")[0]
    assert "Example Inc" in first and "60,00 %" in first and "<strong>6,00</strong>" in first
    assert '">1,00</td>' in first and 'data-label="direkt">5,00</td>' in first
    assert "Nicht zugeordnet (Bargeld, Derivate, Rest der Gewichte): 5,00 %" in r.text


def test_upload_is_refused_for_an_unknown_position_and_a_file_without_weights(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    syncer, _ = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    client.get("/lookthrough")
    token = csrf(client, "/lookthrough")
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": "nope"},
                    files={"file": ("h.csv", FILE.encode(), "text/csv")})
    assert "Unbekannte Position" in r.text
    r = client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                    files={"file": ("h.csv", b"Ticker,Price\nA,1\n", "text/csv")})
    assert "Spalten für Name und Gewichtung fehlen" in r.text
    assert State(tmp_path).funds() == {}


def _client(tmp_path):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    syncer, notifier = setup(tmp_path, FakeWealthfolio())
    syncer.run()
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    client.get("/lookthrough")
    return client, csrf(client, "/lookthrough")


def test_limits_are_checked_and_saved_and_a_risk_can_be_accepted(tmp_path):
    from brokersync import config as config_mod

    client, token = _client(tmp_path)
    # One fund, nothing else: a single stock of 100 % and too few stocks.
    page = client.get("/lookthrough").text
    assert "Klumpenrisiken" in page and "So gewollt" in page and "100,00 %" in page
    assert 'href="/services/taxonomies"' in page and 'name="write"' not in page  # the service's page now (#151)
    r = client.post("/lookthrough/settings", data={"csrf": token, "stock": "abc"})
    assert "Zahl" in r.text and not config_mod.load(tmp_path).xray_limits
    r = client.post("/lookthrough/settings", data={"csrf": token, "write": "on", "stock": "12,5", "effective": "1"})
    c = config_mod.load(tmp_path)
    assert not c.lookthrough_write and c.xray_limits == {"stock": "0.125", "effective": "1"}
    assert 'value="12,50"' in r.text
    r = client.post("/lookthrough/accept", data={"csrf": token, "key": f"stock:{FUND}"})
    assert "Keine Klumpenrisiken über deinen Grenzen." in r.text and "So gewollt (1)" in r.text
    r = client.post("/lookthrough/accept", data={"csrf": token, "key": f"stock:{FUND}", "action": "undo"})
    assert "So gewollt (1)" not in r.text


def test_an_ishares_page_is_saved_only_when_it_is_one_and_fetching_needs_no_switch(tmp_path):
    from brokersync import lookthrough

    client, token = _client(tmp_path)
    r = client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": "https://example.com/x"})
    assert "keine Produktseite von iShares" in r.text
    page = "https://www.ishares.com/de/privatanleger/de/produkte/251882/ishares-msci-world-ucits-etf-acc-fund"
    client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": page})
    assert lookthrough.status(State(tmp_path), FUND)["url"] == page
    r = client.post("/lookthrough/fetch", data={"csrf": token})  # no switch: fetching is always on
    assert "Positionslisten geholt" in r.text


def test_costs_need_a_ter_and_a_fee_model_and_show_in_the_look_through(tmp_path):
    from brokersync import config as config_mod

    client, token = _client(tmp_path)
    page = client.get("/lookthrough").text
    assert "Gesamtkostenquote unbekannt" in page and "nicht geschätzt" in page  # nothing guessed
    r = client.post("/lookthrough/ter", data={"csrf": token, "asset_id": FUND, "ter": "9"})
    assert "bitte als Zahl angeben" in r.text
    r = client.post("/lookthrough/costs", data={"csrf": token, "fake:fixed": "abc"})
    assert "bitte als Zahl angeben" in r.text and config_mod.load(tmp_path).cost_models == {}
    client.post("/lookthrough/costs", data={"csrf": token, "fake:fixed": "1", "fake:spread": "0,05"})
    assert config_mod.load(tmp_path).cost_models == {"fake": {"fixed": "1", "spread": "0.0005"}}
    page = "https://www.ishares.com/de/privatanleger/de/produkte/123456/example-etf"
    client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": page})  # shown: iShares'
    r = client.post("/lookthrough/ter", data={"csrf": token, "asset_id": FUND, "ter": "2,5"})
    assert 'value="2,5"' in r.text and 'value="0,05"' in r.text and "von Hand" in r.text
    assert "Gesamtkostenquote unbekannt" not in r.text and "Verkauf heute geschätzt 1,00 EUR" in r.text
    risks = r.text.split("<caption class=\"sr-only\">Klumpenrisiken</caption>")[1].split("</table>")[0]
    assert "Gesamtkostenquote" in risks  # 1 EUR fee on 5 EUR is far above 0.5 % a year


def test_a_funds_top_10_show_under_it_and_the_rest_only_on_request(tmp_path):
    client, token = _client(tmp_path)
    many = "ISIN,Name,Weight (%)\n" + "".join(f",Example {n:02d} Inc,{(n + 1) / 100}\n" for n in range(60))
    client.post("/lookthrough/upload", data={"csrf": token, "asset_id": FUND},
                files={"file": ("holdings.csv", many.encode(), "text/csv")})
    page = client.get("/lookthrough").text
    fund = page.split(f"<caption class=\"sr-only\">Positionen von {FUND}")[1]
    top = fund.split(f"Alle 60 Positionen von {FUND} zeigen")[0]
    assert top.index("Example 59 Inc") < top.index("Example 58 Inc") and "Example 49 Inc" not in top  # top 10
    assert "Example 34 Inc" not in page  # the rest isn't sent: a page with large funds stays small
    full = client.get(f"/lookthrough?fund={FUND}").text
    assert "Example 00 Inc" in full and "Nur die größten 10 zeigen" in full
    assert "<h1>X-Ray</h1>" in page and "Gesamt: Aktien über alle Positionen" in page
    assert "/lookthrough/upload" in page  # an issuer without automatic fetching: its list is uploaded
    matrix = client.get("/lookthrough?top=50").text
    assert '<strong aria-current="page">Top 50</strong>' in matrix and "Example 10 Inc" in matrix  # 50th of 60
    assert "Example 09 Inc</td>" not in matrix.split("Aktien über alle Positionen</caption>")[1].split("</table>")[0]
    assert "Example 00 Inc" in client.get("/lookthrough?top=all").text


def test_every_fund_is_shown_with_the_reason_a_list_is_missing_and_stays_out_of_the_matrix(tmp_path):
    from brokersync import lookthrough

    client, token = _client(tmp_path)
    page = client.get("/lookthrough").text
    assert "0 ETFs durchleuchtet · 0 ohne Positionsliste · 1 Einzelwerte" in page  # its name says no fund
    assert f"<h3>{FUND}" not in page and "Kein ETF in Wealthfolio gefunden." in page
    state = State(tmp_path)
    lookthrough.set_status(state, FUND, url="https://www.ishares.com/uk/a/en/products/123456/x-etf", status="error",
                           error="iShares: HTTP 403")
    lookthrough.record(state, fetched=0, none=0, failed=1, finder="iShares-Fondsfinder: HTTP 403")
    page = client.get("/lookthrough").text
    assert "0 ETFs durchleuchtet · 1 ohne Positionsliste · 0 Einzelwerte" in page
    assert f"<h3>{FUND}" in page and f"weil ihre Positionsliste noch fehlt: {FUND}." in page
    assert "Gesamt: Aktien über alle Positionen" not in page  # a fund without its list is never a row of its own
    assert "Abruf fehlgeschlagen" in page and "iShares: HTTP 403" in page and "Fondsfinder: HTTP 403" in page
    assert "produktliste#/?productView=all&amp;search=" in page and "<details open>" in page  # find it, paste it
    assert "0 Positionslisten geholt, 1 fehlgeschlagen" in page
    lookthrough.record(state, error="RuntimeError: kaputt")
    assert "Der Abruf ist abgebrochen: RuntimeError: kaputt" in client.get("/lookthrough").text


def test_the_overlap_of_two_funds_is_a_heatmap_and_no_sector_is_shown(tmp_path):
    client, token = _client(tmp_path)
    depot = client.app.inner.state.ctx.depot
    depot.positions.append(dict(depot.positions[0], id="fund-2", name="iShares Second ETF", isin="IE00BJ5JNY98"))
    for asset in (FUND, "fund-2"):
        client.post("/lookthrough/upload", data={"csrf": token, "asset_id": asset},
                    files={"file": ("holdings.csv", FILE.encode(), "text/csv")})
    page = client.get("/lookthrough").text
    heat = page.split('<div class="table heat"')[1].split("</table>")[0]
    assert heat.count('class="num heat-5">90</td>') == 2  # the same list twice: 90 % held twice, darkest
    assert heat.count('class="num self">') == 2 and '<th scope="row" title=' in heat
    assert '<th scope="col">Sektor</th>' not in page and "United States · IT" not in page


def test_an_lgim_address_is_saved_and_anything_else_refused(tmp_path):
    from brokersync import lookthrough

    client, token = _client(tmp_path)
    url = "https://fundcentres.lgim.com/srp/documents-id/00000000-0000-4000-8000-000000000000/Fundholdings.csv"
    client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND, "url": url})
    assert lookthrough.status(State(tmp_path), FUND)["url"] == url
    page = client.post("/lookthrough/source", data={"csrf": token, "asset_id": FUND,
                                                     "url": "https://example.com/x.csv"}).text
    assert "keine Positionsliste von L&amp;G" in page and lookthrough.status(State(tmp_path), FUND)["url"] == url


def test_a_fetch_going_on_shows_its_progress(tmp_path):
    import json

    from brokersync import meta_keys

    client, _ = _client(tmp_path)
    ctx = client.app.inner.state.ctx
    State(tmp_path).set_meta(meta_keys.FUNDS_PROGRESS(), json.dumps({"n": 3, "m": 12, "name": "Example ETF"}))
    ctx.running.set()
    try:
        page = client.get("/lookthrough").text
    finally:
        ctx.running.clear()
    assert '<progress max="12" value="3"' in page and "3 von 12: Example ETF" in page
    assert "<progress" not in client.get("/lookthrough").text  # no fetch going on: no bar
