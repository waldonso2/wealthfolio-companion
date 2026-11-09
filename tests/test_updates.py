"""The hint at a newer release: asked once a day, shown on the overview, notified once."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from brokersync import meta_keys, updates
from brokersync.state import State
from brokersync.sync import Syncer

from .helpers import first_password
from .test_sync import RecordingNotifier
from .test_web import csrf, make


def github(tag: str | None, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if tag is None:
            raise httpx.ConnectError("offline")
        return httpx.Response(302, headers={"location": f"{updates.RELEASES}/tag/{tag}"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_versions_compare_as_numbers():
    assert updates.newer("0.27.0", "0.26.1")
    assert updates.newer("0.10.0", "0.9.9")
    assert not updates.newer("0.26.1", "0.26.1")
    assert not updates.newer("0.26.0", "0.26.1")
    assert not updates.newer("garbage", "0.26.1")


def test_asked_once_a_day_and_a_new_version_reported_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "1")
    state, calls = State(tmp_path), []
    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) == "0.27.0"
    assert updates.available(state, "0.26.1") == "0.27.0"
    assert updates.available(state, "0.27.0") is None

    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) is None  # within the day: not asked
    assert len(calls) == 1

    stored = json.loads(state.meta(meta_keys.UPDATE()))
    stored["checked"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    state.set_meta(meta_keys.UPDATE(), json.dumps(stored))
    assert updates.check(state, "0.26.1", http=github("v0.27.0", calls)) is None  # same version: no second notice
    assert len(calls) == 2


def test_github_not_reachable_keeps_what_was_known(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "1")
    state = State(tmp_path)
    state.set_meta(meta_keys.UPDATE(), json.dumps(
        {"version": "0.27.0", "product": updates.PRODUCT, "checked": "2020-01-01T00:00:00+00:00"}))
    assert updates.check(state, "0.26.1", http=github(None, [])) is None
    assert updates.available(state, "0.26.1") == "0.27.0"


def test_turned_off_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "0")
    calls: list = []
    assert updates.check(State(tmp_path), "0.26.1", http=github("v9.0.0", calls)) is None
    assert calls == []


def test_a_run_notifies_a_new_release_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "1")
    monkeypatch.setattr(updates, "latest", lambda http: "99.0.0")
    notifier = RecordingNotifier()
    syncer = Syncer(tmp_path, notifier=notifier)
    syncer.run()
    assert [(t, link) for t, _, link in notifier.sent] == [("Update verfügbar", updates.RELEASES)]
    syncer.run()
    assert len(notifier.sent) == 1


def test_the_overview_shows_a_newer_release(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    assert "Update verfügbar" not in client.get("/broker").text
    State(tmp_path).set_meta(meta_keys.UPDATE(), json.dumps(
        {"version": "99.0.0", "product": updates.PRODUCT, "checked": datetime.now(UTC).isoformat()}))
    page = client.get("/broker").text
    assert "Update verfügbar: Version 99.0.0" in page
    assert f"{updates.RELEASES}/tag/v99.0.0" in page


def test_the_updater_asks_only_when_switched_on_and_its_time_is_up():
    calls = []
    assert updates.due(False, 60, "0.26.1", 1e9, http=github("v0.27.0", calls)) == (False, None) and not calls
    assert updates.due(True, 300, "0.26.1", 299, http=github("v0.27.0", calls)) == (False, None) and not calls
    assert updates.due(True, 5, "0.26.1", 30, http=github("v0.27.0", calls)) == (False, None)  # at least a minute
    assert updates.due(True, 60, "0.26.1", 60, http=github("v0.27.0", calls)) == (True, "0.27.0")
    assert updates.due(True, 60, "0.27.0", 60, http=github("v0.27.0", calls)) == (True, None)
    assert updates.due(True, 60, "0.26.1", 60, http=github(None, calls)) == (True, None)  # offline: nothing


def test_the_updater_reads_its_switch_from_the_host_tier_and_reports_the_result(tmp_path, monkeypatch, capsys):
    from brokersync import cli, config

    monkeypatch.setenv("COMPANION_DATA", str(tmp_path))
    config.update(tmp_path, lambda c: (setattr(c, "auto_update", True), setattr(c, "update_interval", 120)))
    assert config.load_notify(tmp_path).auto_update and config.load_notify(tmp_path).update_interval == 120
    monkeypatch.setattr(updates, "latest", lambda http: "99.0.0")
    assert cli.main(["update-due", "--since", "60"]) == 0 and capsys.readouterr().out == ""
    assert cli.main(["update-due", "--since", "120"]) == 0
    assert capsys.readouterr().out.split("\n")[:2] == ["checked", "install 99.0.0"]
    assert cli.main(["update-done", "--from", "0.33.2", "--to", "99.0.0"]) == 0
    last = json.loads(State(tmp_path).meta(meta_keys.AUTO_UPDATE()))
    assert (last["from"], last["to"], last["ok"]) == ("0.33.2", "99.0.0", True)
    assert cli.main(["update-done", "--from", "0.33.2", "--to", "99.0.1", "--failed"]) == 1
    assert "fehlgeschlagen" in capsys.readouterr().out


def test_the_page_updates_switches_it_on_with_a_bounded_interval(tmp_path):
    from brokersync import config

    client, _, _ = make(tmp_path)
    first_password(client)
    page = client.get("/updates").text
    assert "Neue Versionen automatisch installieren" in page and 'value="60"' in page
    token = csrf(client, "/updates")
    for bad in ("30", "abc", "90,5"):
        r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": bad})
        assert 'role="alert"' in r.text and not config.load(tmp_path).auto_update
    r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": "300"})
    assert "Gespeichert." in r.text
    c = config.load(tmp_path)
    assert c.auto_update and c.update_interval == 300
    State(tmp_path).set_meta(meta_keys.AUTO_UPDATE(), json.dumps(
        {"from": "0.33.2", "to": "0.34.0", "ok": False, "at": datetime.now(UTC).isoformat()}))
    assert "Update auf 0.34.0 fehlgeschlagen, Version 0.33.2 läuft weiter" in client.get("/updates").text


def test_a_renamed_repository_is_followed_only_within_github():
    renamed = "https://github.com/waldonso2/renamed-again/releases/latest"

    def answer(location):
        def handler(request):
            if request.url.path.endswith("wealthfolio-companion/releases/latest"):
                return httpx.Response(301, headers={"location": location})
            return httpx.Response(302, headers={"location": "/waldonso2/wealthfolio-companion/releases/tag/v1.0.0"})
        return httpx.Client(transport=httpx.MockTransport(handler))

    assert updates.latest(answer(renamed)) == "1.0.0"
    assert updates.latest(answer("https://example.org/releases/latest")) is None  # never another host
    assert updates.latest(answer("http://github.com/x/releases/latest")) is None  # https only


def test_pre_releases_sort_before_their_release():
    """PEP 440: 0.1.0a1 < 0.1.0a2 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 < 0.1.1a1."""
    order = ["0.1.0a1", "0.1.0a2", "0.1.0a10", "0.1.0b1", "0.1.0rc1", "0.1.0", "0.1.1a1", "0.2.0"]
    for i, older in enumerate(order):
        for later in order[i + 1:]:
            assert updates.newer(later, older) and not updates.newer(older, later), (older, later)
    assert not updates.newer("0.1.0a1", "0.1.0a1")
    assert not updates.newer("0.1.0dev1", "0.1.0a1") and not updates.newer("0.1.0a", "0.0.1")  # not ours: no hint


def test_the_latest_release_may_be_a_pre_release():
    def client(tag):
        location = f"/waldonso2/wealthfolio-companion/releases/tag/{tag}"
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": location})))

    assert updates.latest(client("v0.1.0a2")) == "0.1.0a2"
    assert updates.latest(client("v0.1.0rc1")) == "0.1.0rc1"
    assert updates.latest(client("v0.1.0-beta")) is None


def test_an_answer_stored_by_broker_sync_counts_for_nothing(tmp_path, monkeypatch):
    """A backup of Wealthfolio Broker Sync brings its last answer (0.4x) along: it is no update of Companion, and the
    next run asks GitHub again instead of waiting a day."""
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "1")
    state = State(tmp_path)
    state.set_meta(meta_keys.UPDATE(), json.dumps({"version": "0.44.0", "checked": datetime.now(UTC).isoformat()}))
    assert updates.available(state, "0.1.0a2") is None
    calls: list = []
    assert updates.check(state, "0.1.0a2", http=github("v0.1.0a3", calls)) == "0.1.0a3"
    assert calls and updates.available(state, "0.1.0a2") == "0.1.0a3"


def github_with_dates(tag: str, published: datetime, calls: list):
    """releases/latest redirects to ``tag``; the API names when it was published."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            stamp = published.isoformat().replace("+00:00", "Z")
            return httpx.Response(200, json={"tag_name": tag, "published_at": stamp})
        return httpx.Response(302, headers={"location": f"{updates.RELEASES}/tag/{tag}"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_delay_holds_a_release_back_until_it_is_old_enough():
    calls: list = []
    young = github_with_dates("v0.27.0", datetime.now(UTC) - timedelta(days=2), calls)
    old = github_with_dates("v0.27.0", datetime.now(UTC) - timedelta(days=8), calls)
    assert updates.due(True, 900, "0.26.1", 900, delay=0, http=young) == (True, "0.27.0")  # no delay: at once
    assert updates.due(True, 900, "0.26.1", 900, delay=7, http=young) == (True, None)
    assert updates.due(True, 900, "0.26.1", 900, delay=7, http=old) == (True, "0.27.0")
    assert updates.due(True, 900, "0.27.0", 900, delay=7, http=old) == (True, None)  # nothing newer
    # with a delay the API is asked, so not more often than every 15 minutes
    assert updates.due(True, 60, "0.26.1", 899, delay=7, http=old) == (False, None)
    assert updates.due(True, 60, "0.26.1", 899, delay=0, http=old) == (True, "0.27.0")


def test_a_release_without_a_date_is_not_installed_with_a_delay():
    def refuse(code, **kw):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.github.com":
                return httpx.Response(code, **kw)
            return httpx.Response(302, headers={"location": f"{updates.RELEASES}/tag/v0.27.0"})
        return httpx.Client(transport=httpx.MockTransport(handler))

    for client in (refuse(403), refuse(404), refuse(200, json={"tag_name": "v0.27.0"}), refuse(200, content=b"<html>"),
                   refuse(302, headers={"location": "https://evil.example/x"})):
        assert updates.due(True, 900, "0.26.1", 900, delay=3, http=client) == (True, None)


def test_the_delay_is_saved_with_the_updates_and_read_from_the_host_tier(tmp_path, monkeypatch, capsys):
    from brokersync import cli, config

    client, _, _ = make(tmp_path)
    first_password(client)
    assert 'name="delay"' in client.get("/updates").text
    token = csrf(client, "/updates")
    for bad in ("31", "-1", "2,5", "x"):
        r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": "900", "delay": bad})
        assert 'role="alert"' in r.text and not config.load(tmp_path).auto_update, bad
    r = client.post("/updates", data={"csrf": token, "auto_update": "1", "interval": "900", "delay": "7"})
    assert "Gespeichert." in r.text and config.load_notify(tmp_path).update_delay == 7
    monkeypatch.setenv("COMPANION_DATA", str(tmp_path))
    seen = {}
    monkeypatch.setattr(updates, "due", lambda *a, **kw: (seen.update(kw), (True, None))[1])
    assert cli.main(["update-due", "--since", "1000"]) == 0 and seen == {"delay": 7}
    assert capsys.readouterr().out == "checked\n"
