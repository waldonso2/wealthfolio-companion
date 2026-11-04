"""The page Dienste (#145): each service switched on and off, on only once what it needs is there."""

from brokersync import config as config_mod
from brokersync import security
from brokersync.vault import Vault

from .helpers import first_password
from .test_web import csrf, make


def test_a_new_install_starts_with_every_service_off_an_old_one_keeps_them(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    assert config_mod.load(old).service("sync") and config_mod.load(old).service("isin")  # set up before: on
    new = tmp_path / "new"
    new.mkdir()
    security.migrate(new)
    c = config_mod.load(new)
    assert not any(c.service(k) for k in ("sync", "taxonomies", "isin", "logos", "spending", "xray"))
    c.set_service("isin", True)
    assert c.services == {"isin": True}
    old_cfg = config_mod.load(old)
    old_cfg.set_service("sync", False)
    assert old_cfg.services == {"sync": False, "isin": True, "spending": True, "xray": False}


def test_switching_on_sends_to_the_settings_and_turns_on_once_they_are_saved(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    config_mod.update(tmp_path, lambda c: setattr(c, "services", {}))
    page = client.get("/").text
    assert 'role="switch" aria-checked="false"' in page
    for key in ("taxonomies", "isin", "logos", "spending", "xray"):  # every gear: the service's page (#151)
        assert f'href="/services/{key}"' in page
    assert 'href="/brokers"' in page  # Broker sync's gear goes to the broker list (#180)
    token = csrf(client, "/")
    r = client.post("/services/logos", data={"csrf": token}, follow_redirects=False)
    assert r.headers["location"] == "/services/logos"  # its page names what is missing
    assert 'href="/setup/wealthfolio"' in client.get("/services/logos").text  # Wealthfolio first
    Vault(tmp_path).update(lambda d: d.__setitem__("wealthfolio_checked", True))
    r = client.post("/services/logos", data={"csrf": token}, follow_redirects=False)
    assert r.headers["location"] == "/services/logos" and not config_mod.load(tmp_path).logos_enabled
    page = client.get("/services/logos").text
    assert "schaltet sich ein" in page and 'href="/services/logos#token"' in page
    assert "schaltet sich ein" in client.get("/").text
    client.post("/services/logos/settings", data={"csrf": token, "token": "abc_123"})  # saved: on now
    assert config_mod.load(tmp_path).logos_enabled
    r = client.post("/services/logos", data={"csrf": token})
    assert "Logos ist ausgeschaltet." in r.text and not config_mod.load(tmp_path).logos_enabled
    r = client.post("/services/logos", data={"csrf": token, "back": "page"}, follow_redirects=False)
    assert r.headers["location"] == "/services/logos" and config_mod.load(tmp_path).logos_enabled
    r = client.post("/services/isin", data={"csrf": token})
    assert "ISIN ergänzen ist eingeschaltet." in r.text and config_mod.load(tmp_path).service("isin")
    assert client.post("/services/nope", data={"csrf": token}).status_code == 200  # an error message, nothing set
    assert "Unbekannter Dienst" in client.get("/services/nope").text


def test_broker_sync_off_fetches_no_broker(tmp_path):
    from brokersync.state import State

    from .test_safe_mode import make as make_with_broker

    client, _, token = make_with_broker(tmp_path, safe=False)
    config_mod.update(tmp_path, lambda c: c.set_service("sync", False))
    assert "Broker-Sync ist ausgeschaltet" in client.post("/run", data={"csrf": token}).text
    assert "kein Broker wird abgerufen" in client.get("/broker").text
    assert State(tmp_path).last_run("fake") is None
    config_mod.update(tmp_path, lambda c: c.set_service("sync", True))
    client.post("/run", data={"csrf": token})
    assert State(tmp_path).last_run("fake") is not None
