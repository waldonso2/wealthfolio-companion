"""One page per service (waldonso2/wealthfolio-importer-addon#151): head with the switch, status, settings,
actions - the actions in the background, a removal only of what the service wrote itself."""

from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync import meta_keys, xray_addon
from brokersync.state import State
from brokersync.vault import Vault

from .fake_broker import ISIN as FUND
from .fakes import FakeWealthfolio
from .helpers import first_password
from .test_sync import setup
from .test_web import csrf

LINES = [["US0378331005", "Example Inc", "0.7", "US", "Information Technology", "USD"],
         ["DE0007164600", "Beispiel AG", "0.2", "DE", "Industrials", "EUR"]]


def _client(tmp_path, wf=None):
    from brokersync.web.app import create_app

    from .fake_broker import FakeBroker

    wf = wf or FakeWealthfolio()
    syncer, _ = setup(tmp_path, wf)
    syncer.run()
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_checked", True))
    app = create_app(tmp_path, wealthfolio=syncer._wealthfolio, adapters={"fake": FakeBroker}, run_in_thread=False)
    client = TestClient(app, base_url="http://sync.local:8090")
    first_password(client)
    return client, csrf(client, "/services/sync"), wf


def test_every_service_page_shows_head_status_settings_and_actions_in_that_order(tmp_path):
    client, _, _ = _client(tmp_path)
    for key in ("sync", "taxonomies", "isin", "logos", "spending", "xray"):
        page = client.get(f"/services/{key}").text
        assert f'action="/services/{key}"' in page and 'role="switch"' in page, key
        head, status, actions = (page.index('name="back"'), page.index(">Status</h2>"), page.index(">Aktionen</h2>"))
        assert head < status < actions, key
        nav = page.split('<nav class="main"')[1]
        assert 'aria-current="page"' in nav.split('href="/"')[1][:30]  # under Dienste in the header
    page = client.get("/services/sync").text
    assert "Testbroker" in page and 'class="light' in page and 'href="/brokers"' not in page  # #180
    assert 'href="/security"' in page and 'action="/run"' in page
    assert "Automatisch" in page


def test_jetzt_ausfuehren_runs_the_step_in_the_background_only_when_on(tmp_path):
    wf = FakeWealthfolio()
    client, token, wf = _client(tmp_path, wf)
    State(tmp_path).set_fund(FUND, "World", LINES)
    wf.assets = [{"id": FUND, "name": "World"}]
    r = client.post("/services/taxonomies/run", data={"csrf": token})
    assert "Erst einschalten" in r.text and wf.assignments == []
    client.post("/services/taxonomies", data={"csrf": token, "back": "page"})
    r = client.post("/services/taxonomies/run", data={"csrf": token})
    assert "2 Zuordnungen in Wealthfolio eingetragen" in r.text
    assert {a["categoryId"] for a in wf.assignments} == {"country_US", "country_DE", "45", "20"}
    page = client.get("/services/taxonomies").text
    assert "Zuletzt in Wealthfolio eingetragen" in page and "World" in page and "Liste des Anbieters" in page

    r = client.post("/services/isin/run", data={"csrf": token})  # on for an installation set up before
    assert "ISINs in Wealthfolio ergänzt" in r.text
    r = client.post("/services/spending/run", data={"csrf": token})
    assert "Erst einschalten" in r.text  # no cash account chosen yet
    config_mod.update(tmp_path, lambda c: setattr(c.brokers["fake"], "spending", True))
    r = client.post("/services/spending/run", data={"csrf": token})
    assert "Ausgaben-Tracker aktualisiert" in r.text and wf.spending["accountIds"] == ["acc-cash"]
    assert "Vom Dienst in Wealthfolios Ausgaben-Tracker aufgenommen" in client.get("/services/spending").text
    assert client.post("/services/sync/run", data={"csrf": token}).status_code == 200  # no such action: a message


def test_remove_needs_a_confirmation_and_takes_only_our_assignments(tmp_path):
    client, token, wf = _client(tmp_path)
    State(tmp_path).set_fund(FUND, "World", LINES)
    wf.assets = [{"id": FUND, "name": "World"}]
    config_mod.update(tmp_path, lambda c: setattr(c, "lookthrough_write", True))
    client.post("/services/taxonomies/run", data={"csrf": token})
    wf.assignments.append({"id": "mine", "assetId": FUND, "taxonomyId": "regions", "categoryId": "country_DE",
                           "weight": 10000, "source": "MANUAL"})
    r = client.post("/services/taxonomies/remove", data={"csrf": token})
    assert "bestätigen" in r.text and len(wf.assignments) == 5
    r = client.post("/services/taxonomies/remove", data={"csrf": token, "confirm": "1"})
    assert "4 Zuordnungen aus Wealthfolio entfernt" in r.text
    assert [a["id"] for a in wf.assignments] == ["mine"] and not config_mod.load(tmp_path).lookthrough_write
    assert State(tmp_path).meta(meta_keys.TAXONOMIES_LAST()) is None


def test_the_isin_page_lists_what_was_completed_and_what_still_has_none(tmp_path):
    client, token, wf = _client(tmp_path)
    wf.assets = [{"id": FUND, "name": "World", "metadata": {}}]
    assert "Noch keine ISIN ergänzt." in client.get("/services/isin").text
    r = client.post("/services/isin/run", data={"csrf": token})
    assert "1 ISINs in Wealthfolio ergänzt" in r.text
    page = client.get("/services/isin").text
    row = page.split("Ergänzte ISINs</caption>")[1].split("</tbody>")[0]
    assert f"<code>{FUND}</code>" in row and "Ergänzt\">–" not in row  # with its moment
    assert 'href="/securities"' in page and "ISIN fehlt? Selbst zuordnen" in page
    assert "Jedes Wertpapier im Depot hat eine ISIN." in page  # the fund's ISIN is known


def test_xray_addon_is_stored_now_and_removed_again(tmp_path):
    client, token, wf = _client(tmp_path)
    State(tmp_path).set_fund(FUND, "World", LINES)
    page = client.get("/services/xray").text
    assert "releases/latest" in page and "Add-ons" in page and "Noch nichts abgelegt" in page
    client.post("/services/xray", data={"csrf": token, "back": "page"})
    r = client.post("/services/xray/run", data={"csrf": token})
    assert "Fondslisten abgelegt" in r.text and (xray_addon.ADDON_ID, xray_addon.INDEX) in wf.storage
    page = client.get("/services/xray").text
    assert "Zuletzt abgelegt" in page and "Fonds mit Liste: 1" in page
    wf.storage[("other-addon", "x")] = "kept"
    r = client.post("/services/xray/remove", data={"csrf": token, "confirm": "1"})
    assert "Fondslisten aus Wealthfolio entfernt" in r.text
    assert list(wf.storage) == [("other-addon", "x")] and not config_mod.load(tmp_path).service("xray")
    assert State(tmp_path).meta(meta_keys.XRAY_ADDON_STATUS()) is None


def test_an_old_wealthfolio_is_named_on_the_xray_page(tmp_path):
    wf = FakeWealthfolio()
    wf.storage = None
    client, token, _ = _client(tmp_path, wf)
    client.post("/services/xray", data={"csrf": token, "back": "page"})
    client.post("/services/xray/run", data={"csrf": token})
    assert "Wealthfolio zu alt" in client.get("/services/xray").text


def test_positions_without_asset_class_are_shown_on_pruefung_and_the_services_page(tmp_path):
    """wealthfolio-importer-addon#141 stage 1: what Wealthfolio counts as "Unknown", with what to do."""
    import json

    client, _, _ = _client(tmp_path)
    State(tmp_path).set_meta(meta_keys.ASSET_CLASSES(), json.dumps({
        "missing": [{"id": "a", "name": "Example World ETF", "isin": "IE0000000001"}],
        "named": {"b": {"name": "Example Govt Bond ETF", "class": "FIXED_INCOME"}}, "at": "2026-10-30T06:41:00+00:00"}))
    page = client.get("/check").text
    assert "Ohne Anlageklasse in Wealthfolio: 1" in page and "Example World ETF" in page and "IE0000000001" in page
    page = client.get("/services/taxonomies").text
    assert "Anlageklasse aus dem Fondsnamen" in page and "Example Govt Bond ETF" in page
    assert 'href="/check#asset-classes"' in page


def test_the_asset_class_lookup_is_off_until_switched_on_on_the_taxonomies_page(tmp_path):
    client, token, _ = _client(tmp_path)
    page = client.get("/services/taxonomies").text
    assert 'name="lookup"' in page and "checked" not in page.split('name="lookup"')[1].split(">")[0]
    assert not config_mod.load(tmp_path).asset_class_lookup
    r = client.post("/services/taxonomies/settings", data={"csrf": token, "lookup": "1"}, follow_redirects=False)
    assert r.status_code == 303 and config_mod.load(tmp_path).asset_class_lookup
    assert "checked" in client.get("/services/taxonomies").text.split('name="lookup"')[1].split(">")[0]
    client.post("/services/taxonomies/settings", data={"csrf": token}, follow_redirects=False)
    assert not config_mod.load(tmp_path).asset_class_lookup
