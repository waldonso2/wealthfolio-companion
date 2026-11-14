"""Sessions of the UI (#96), forms that keep their input (#103), background jobs and progress (#104), tables
(#105), transfer patterns while saved (#84), the config under its lock (#117)."""

import re
import threading
import time
import types

from brokersync import config as config_mod
from brokersync import crypto, meta_keys
from brokersync.state import State
from brokersync.vault import Vault, weak_password

from .helpers import PASSWORD, first_password
from .test_web import csrf, make


class Clock:
    """``time`` for the web modules, moved on by hand."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now


def clock(monkeypatch) -> Clock:
    from brokersync.web import app, context

    c = Clock()
    fake = types.SimpleNamespace(time=c.time, monotonic=time.monotonic)
    monkeypatch.setattr(app, "time", fake)
    monkeypatch.setattr(context, "time", fake)
    return c


def test_logout_ends_a_copied_cookie(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    copied = dict(client.cookies)
    r = client.post("/logout", data={"csrf": csrf(client, "/")}, follow_redirects=False)
    assert r.headers["clear-site-data"] == '"cookies"'
    client.cookies.clear()
    client.cookies.update(copied)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_idle_and_absolute_limits(tmp_path, monkeypatch):
    c = clock(monkeypatch)
    client, _, _ = make(tmp_path)
    first_password(client)
    for _ in range(20):  # active: every 29 minutes a page, up to just below 12 h
        c.now += 29 * 60
        if c.now - time.time() > 11.5 * 3600:
            break
        assert client.get("/", follow_redirects=False).status_code == 200
    c.now += 31 * 60  # idle for more than 30 minutes
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"

    client.post("/login", data={"password": PASSWORD})
    c.now += 12 * 3600 + 1  # never longer than 12 h, active or not
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_change_ends_the_other_sessions(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    from fastapi.testclient import TestClient

    other = TestClient(client.app, base_url="http://sync.local:8090")
    other.post("/login", data={"password": PASSWORD})
    assert other.get("/", follow_redirects=False).status_code == 200
    token = csrf(client, "/security")
    r = client.post("/security/password", data={"csrf": token, "password": "falsch", "new": "x", "new2": "x"})
    assert "bisherige Passwort stimmt nicht" in r.text
    r = client.post("/security/password", data={"csrf": token, "password": PASSWORD, "new": "passwort1234",
                                                "new2": "passwort1234"})
    assert "zu verbreitet" in r.text
    new = "Moewe-Kartoffel-Jazz-71"
    r = client.post("/security/password", data={"csrf": token, "password": PASSWORD, "new": new, "new2": new})
    assert "Passwort geändert" in r.text
    assert client.get("/", follow_redirects=False).status_code == 200  # this one stays signed in
    assert other.get("/", follow_redirects=False).headers["location"] == "/login"
    assert other.post("/login", data={"password": new}, follow_redirects=False).headers["location"] == "/"

    # "Alle anderen Sitzungen beenden": the others out, this one in.
    client.post("/security/sessions", data={"csrf": csrf(client, "/security")})
    assert client.get("/", follow_redirects=False).status_code == 200
    assert other.get("/", follow_redirects=False).headers["location"] == "/login"


def test_common_passwords_are_refused():
    for weak in ("passwort1234", "Passwort!2024", "123456789012", "qwertzuiop12", "aaaaaaaaaaaa", "geheim-123456"):
        assert weak_password(weak), weak
    assert not weak_password("Moewe-Kartoffel-Jazz-71")


def test_the_first_password_must_not_be_common(tmp_path):
    client, _, _ = make(tmp_path)
    r = first_password(client, "password1234")
    assert "zu verbreitet" in r.text and not Vault(tmp_path).load().get("ui_password_hash")


def test_sensitive_actions_ask_for_the_password_after_15_minutes(tmp_path, monkeypatch):
    c = clock(monkeypatch)
    client, _, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/security")
    assert 'name="ui_password"' not in client.get("/security").text  # just signed in
    c.now += 16 * 60
    assert 'name="ui_password"' in client.get("/security").text
    r = client.post("/security/mode", data={"csrf": token, "mode": "safe"})
    assert "stimmt nicht" in r.text and config_mod.load(tmp_path).sync_mode == "auto"
    r = client.post("/security/mode", data={"csrf": token, "mode": "safe", "ui_password": PASSWORD})
    assert "Sicherer Modus an" in r.text and config_mod.load(tmp_path).sync_mode == "safe"
    assert 'name="ui_password"' not in client.get("/security").text  # confirmed just now

    # New credentials too, after a while.
    c.now += 16 * 60
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "cash_account_id": "",
                                           "portfolio_account_id": ""})
    assert r.status_code == 422 and 'id="err-ui_password"' in r.text and not Vault(tmp_path).broker("fake")


def test_broker_form_keeps_the_input_and_saves_nothing_on_an_error(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/brokers/fake")
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                           "cash_account_id": "acc-cash", "portfolio_account_id": "acc-cash",
                                           "start_date": "2026-01-02"})
    assert r.status_code == 422
    assert 'value="max"' in r.text and 'value="2026-01-02"' in r.text  # what was typed stays
    assert 'aria-describedby="err-portfolio_account_id"' in r.text and "verschiedene Konten" in r.text
    assert 'role="alert"' not in r.text or "flash error" in r.text
    assert "fake" not in config_mod.load(tmp_path).brokers and not Vault(tmp_path).broker("fake")  # nothing saved
    r = client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "start_date": "2999-01-01"})
    assert r.status_code == 422 and 'id="err-start_date"' in r.text and "Zukunft" in r.text
    from brokersync.web import inputs

    assert f'max="{inputs.today().isoformat()}"' in client.get("/brokers/fake").text  # the German day, not UTC's


def test_flash_messages_have_a_role(tmp_path):
    client, _, _ = make(tmp_path)
    r = first_password(client)
    assert 'class="flash ok" role="status"' in r.text
    r = client.post("/login", data={"password": "falsch"})
    assert 'class="flash error" role="alert"' in r.text


def test_one_vault_decryption_per_request(tmp_path, monkeypatch):
    client, _, _ = make(tmp_path)
    first_password(client)
    client.get("/")
    calls = []
    unseal = crypto.unseal
    monkeypatch.setattr(crypto, "unseal", lambda key, raw, purpose: calls.append(purpose) or unseal(key, raw, purpose))
    client.get("/")
    client.get("/brokers/fake")
    assert calls.count("secrets") == 0  # unchanged since the last request: not decrypted again
    Vault(tmp_path).update(lambda d: d.__setitem__("x", 1))
    calls.clear()
    client.get("/brokers/fake")
    assert calls.count("secrets") <= 1


def test_overview_shows_the_running_broker_and_refreshes_slowly(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    app = client.app.app()
    app.state.ctx.running.set()
    app.state.ctx.job = "Abruf"
    State(tmp_path).set_meta(meta_keys.PROGRESS(), '{"broker": "fake", "label": "Test", "n": 1, "m": 2, '
                                                   '"phase": "approval", "since": "2026-10-15T08:00:00+00:00"}')
    page = client.get("/broker").text
    assert '<meta http-equiv="refresh" content="2">' in page and 'aria-live="polite"' in page
    assert "Test (1 von 2)" in page and "Warte auf deine Freigabe in der App (seit 15.10.2026 10:00)" in page
    page = client.get("/broker?still=1").text
    assert "http-equiv" not in page and "Aktualisieren" in page
    app.state.ctx.running.clear()


def test_a_run_reports_its_progress_and_clears_it(tmp_path):
    from .fakes import FakeWealthfolio
    from .test_sync import setup

    seen = []
    syncer, _ = setup(tmp_path, FakeWealthfolio())
    original = syncer._phase
    syncer._phase = lambda phase: (original(phase), seen.append(syncer.state.meta(meta_keys.PROGRESS())))
    syncer.run()
    assert [__import__("json").loads(s)["phase"] for s in seen] == ["login", "fetch", "book", "check"]
    assert '"n": 1, "m": 1' in seen[0]
    assert syncer.state.meta(meta_keys.PROGRESS()) is None


def test_tables_are_semantic():
    from pathlib import Path

    templates = Path(__file__).parents[1] / "src/brokersync/web/templates"
    for f in templates.glob("*.html"):
        text = f.read_text()
        for table in re.findall(r"<table>.*?</table>", text, re.S):
            assert "<thead>" in table and "<tbody>" in table and "<caption" in table, f.name
            assert "<th>" not in table and "<th></th>" not in table, f.name
    assert "_('auswählen')" not in (templates / "check.html").read_text()


def test_transfer_patterns_are_listed_while_saved(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    assert 'href="/transfers"' not in client.get("/settings").text
    config_mod.update(tmp_path, lambda c: c.transfer_patterns.append({"label": "Tagesgeld", "iban": "",
                                                                       "keyword": "Spar", "destinationAccountId": ""}))
    page = client.get("/settings").text
    assert 'href="/transfers"' in page
    page = client.get("/transfers").text
    assert "Tagesgeld" in page
    client.post("/transfers", data={"csrf": csrf(client, "/transfers"), "delete": "0"})
    assert config_mod.load(tmp_path).transfer_patterns == []


def test_two_config_changes_at_once_both_stay(tmp_path):
    config_mod.save(tmp_path, config_mod.Config())

    def change(field, value):
        def apply(c):
            setattr(c, field, value)
            time.sleep(0.05)  # both inside read-change-write at once without the lock
        config_mod.update(tmp_path, apply)

    threads = [threading.Thread(target=change, args=("sync_mode", "safe")),
               threading.Thread(target=change, args=("ui_language", "en"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c = config_mod.load(tmp_path)
    assert (c.sync_mode, c.ui_language) == ("safe", "en")


def test_fetch_all_says_why_nothing_was_fetched(tmp_path, monkeypatch):
    from brokersync.sync import AlreadyRunning

    client, _wf, _n = make(tmp_path)
    first_password(client)
    config_mod.update(tmp_path, lambda c: c.brokers.__setitem__("fake", config_mod.BrokerConfig(
        enabled=False, cash_account_id="acc-cash", portfolio_account_id="acc-depot")))
    token = csrf(client, "/broker")
    r = client.post("/run", data={"csrf": token})
    assert "Kein Broker zum Abrufen" in r.text  # a broker with automatic fetching off is skipped on purpose

    config_mod.update(tmp_path, lambda c: setattr(c.brokers["fake"], "enabled", True))
    Vault(tmp_path).set_broker_credentials("fake", {"username": "max"})

    def busy(*args, **kwargs):
        raise AlreadyRunning

    monkeypatch.setattr(client.app.app().state.ctx.syncer, "run", busy)
    r = client.post("/run", data={"csrf": csrf(client, "/broker")})
    assert "Nichts wurde abgerufen" in r.text and "Abruf beendet" not in r.text
