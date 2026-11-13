"""The web UI's attack surface (SEC-07, #153), table-driven over every route the app has: a new route is in
these tests the moment it exists - public only once it is on the allow-list, state-changing only behind the
CSRF token."""

import base64
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from brokersync import config as config_mod
from brokersync import logos
from brokersync.vault import Vault
from brokersync.web.routes import HIDDEN, ROUTERS
from brokersync.web.routes.services import NAMES

from .helpers import PASSWORD, first_password
from .test_web import csrf, make

# Everything reachable without a login. The first-password page is not in context.PUBLIC (its POST needs the
# CSRF token and the setup code, #89), but it opens without a login - after the first password it only
# redirects to /login.
PUBLIC_ROUTES = {("GET", "/login"), ("POST", "/login"), ("GET", "/healthz"), ("GET", "/ca.crt"),
                 ("GET", "/setup-password"), ("POST", "/setup-password"), ("POST", "/setup-language")}
# POSTs that take no CSRF token: the login only (it has no session yet; wrong passwords go to the brake).
NO_CSRF = {"/login"}


def routes() -> list[tuple[str, str]]:
    """(method, path) of every route of every router, the hidden pages too; path parameters filled in."""
    out = []
    for router in ROUTERS + HIDDEN:
        for route in router.routes:
            for method in sorted(route.methods):
                if "{key}" in route.path and route.path.startswith("/services/"):
                    out += [(method, route.path.replace("{key}", k)) for k in sorted(NAMES)]
                else:
                    out.append((method, route.path.replace("{key}", "fake")))
    return out


def template(path: str) -> str:
    return re.sub(r"^/services/[a-z]+", "/services/{key}", path).replace("/fake", "/{key}")


def snapshot(data: Path) -> dict[str, str]:
    """What the service keeps in data/ (lock files and temp files aside), as hashes."""
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(data.iterdir())
            if f.is_file() and not f.name.endswith((".lock", ".tmp"))}


def session_of(client) -> dict:
    """The signed session cookie's content (Starlette: base64 JSON, then timestamp and signature)."""
    return json.loads(base64.b64decode(client.cookies["session"].split(".")[0] + "=="))


def test_only_get_and_post_exist():
    # csrf_guard checks POST: a PUT/PATCH/DELETE route would slip past it until the guard covers it too.
    assert {m for m, _ in routes()} == {"GET", "POST"}


def test_every_route_but_the_allow_list_needs_a_login(tmp_path, hidden_pages):
    client, _, _ = make(tmp_path)
    first_password(client)
    anonymous = TestClient(client.app, base_url="http://sync.local:8090")
    for method, path in routes():
        if (method, template(path)) in PUBLIC_ROUTES:
            continue
        r = anonymous.request(method, path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/login"), (method, path)
    # A cookie that isn't signed with the session key signs in nobody either.
    anonymous.cookies.set("session", "eyJ1c2VyIjogImFkbWluIn0=.forged.sig")
    assert anonymous.get("/", follow_redirects=False).headers["location"] == "/login"


def test_only_the_allow_list_opens_without_a_login(tmp_path, hidden_pages):
    # Before the first password the login check sends everything to /setup-password - except what it lets
    # through: that set must be exactly the allow-list, so a new public route fails here until added on purpose.
    client, _, _ = make(tmp_path)
    reachable = set()
    for method, path in routes():
        r = client.request(method, path, follow_redirects=False)
        if not (r.status_code == 303 and r.headers.get("location") == "/setup-password"):
            reachable.add((method, template(path)))
    assert reachable == PUBLIC_ROUTES


@pytest.mark.parametrize("token", [None, "forged"])
def test_every_post_needs_the_csrf_token_and_changes_nothing_without_it(tmp_path, hidden_pages, token):
    client, _, _ = make(tmp_path)
    first_password(client)
    csrf(client, "/")  # a token in this session: the forged one is compared, not just missing
    before = snapshot(tmp_path)
    exempt = set()
    for method, path in routes():
        if method != "POST":
            continue
        r = client.post(path, data={} if token is None else {"csrf": token}, follow_redirects=False)
        if r.status_code != 400:
            exempt.add(template(path))
            continue
        assert "Formular abgelaufen" in r.text, path
    assert exempt == NO_CSRF
    assert snapshot(tmp_path) == before
    assert client.get("/", follow_redirects=False).status_code == 200  # still signed in: nothing ended the session


def test_a_form_from_another_site_is_refused_before_any_password_check(tmp_path):
    # A web page elsewhere could else spend the brake's attempts through the user's browser and lock them out.
    client, _, _ = make(tmp_path)
    first_password(client)
    client.post("/logout", data={"csrf": csrf(client, "/")})
    for _ in range(10):
        r = client.post("/login", data={"password": "falsch"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and "anderen Website" in r.text
    assert client.post("/login", data={"password": PASSWORD}, headers={"Sec-Fetch-Site": "same-origin"},
                       follow_redirects=False).headers["location"] == "/"


def test_the_unlock_page_refuses_another_site_too(tmp_path):
    from brokersync import crypto, security
    from brokersync.web.app import create_app

    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    crypto.lock(tmp_path)
    client = TestClient(create_app(tmp_path, run_in_thread=False), base_url="http://sync.local")
    for _ in range(10):
        r = client.post("/unlock", data={"passphrase": "falsch"}, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
    r = client.post("/unlock", data={"passphrase": "eine sehr lange passphrase"}, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_a_forwarded_header_does_not_get_past_the_brake(tmp_path, monkeypatch):
    # Without COMPANION_TRUST_PROXY the app never reads X-Forwarded-For: a new address per try changes nothing.
    monkeypatch.delenv("COMPANION_TRUST_PROXY", raising=False)
    client, _, _ = make(tmp_path)
    first_password(client)
    client.post("/logout", data={"csrf": csrf(client, "/")})
    for i in range(5):
        client.post("/login", data={"password": "falsch"}, headers={"X-Forwarded-For": f"10.9.0.{i}"})
    r = client.post("/login", data={"password": PASSWORD}, headers={"X-Forwarded-For": "10.9.1.1"})
    assert "Zu viele Fehlversuche" in r.text


def test_every_password_check_of_the_ui_goes_through_the_brake():
    # UI password, master passphrase, setup code (#90): each check sits inside a Throttle.attempt.
    web = Path(__file__).parent.parent / "src" / "brokersync" / "web"
    found = 0
    for f in web.rglob("*.py"):
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if re.search(r"\b(verify_password|crypto\.unlock|check_setup_code)\(", line) and "import" not in line:
                found += 1
                assert any(".attempt(" in prev for prev in lines[max(0, i - 6):i]), f"{f.name}:{i + 1}"
    assert found >= 6


def test_a_login_starts_a_new_session(tmp_path):
    # Session fixation: nothing of the session before the login is carried over, not even its CSRF token.
    client, _, _ = make(tmp_path)
    first_password(client)
    client.post("/logout", data={"csrf": csrf(client, "/")})
    client.get("/login")
    before = session_of(client)
    assert before.get("csrf") and not before.get("user")  # the login page gave this session a token
    client.post("/login", data={"password": PASSWORD})
    after = csrf(client, "/")
    assert after != before["csrf"] and set(session_of(client)) >= {"user", "pw", "since"}
    assert client.post("/run", data={"csrf": before["csrf"]}).status_code == 400


def test_session_cookie_flags(tmp_path):
    from .test_security import client_for

    r = first_password(client_for(tmp_path, secure=True), PASSWORD, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    for flag in ("httponly", "secure", "samesite=strict", "path=/", "max-age=43200"):
        assert flag in cookie, flag


def test_headers_on_every_answer_also_errors_and_redirects(tmp_path):
    client, _, _ = make(tmp_path)
    for r in (client.get("/login"), client.get("/", follow_redirects=False), client.get("/static/style.css"),
              client.post("/login", data={})):
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"], r.status_code
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"


def test_templates_escape_everything():
    from brokersync.web.app import _templates
    from brokersync.web.i18n import markup

    env = _templates().env
    assert env.autoescape is True or env.autoescape("page.html")
    templates = Path(__file__).parent.parent / "src" / "brokersync" / "web" / "templates"
    for f in templates.glob("*.html"):
        text = f.read_text()
        assert "|safe" not in text.replace(" ", "") and "autoescape false" not in text, f.name
    # Our own texts may hold HTML; what is put into them never.
    assert str(markup("{broker}: {account}", "de", broker="<script>", account="&")) == "&lt;script&gt;: &amp;"


def test_the_stored_wealthfolio_password_never_goes_to_a_new_address(tmp_path):
    client, wf, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/setup/wealthfolio")
    client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080", "password": wf.password})
    asked = []
    client.app.app().state.ctx.make_wf = lambda url, pw: asked.append((url, pw)) or (_ for _ in ()).throw(
        AssertionError("no request"))
    for test in ("", "1"):
        r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "https://elsewhere.example", "test": test})
        assert r.status_code == 422 and "noch einmal eingeben" in r.text
    assert asked == [] and config_mod.load(tmp_path).wealthfolio_url == "http://wf.local:8080"


def test_the_ntfy_token_stays_with_its_server(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/notifications")
    topic = config_mod.load(tmp_path).ntfy_topic
    client.post("/notifications", data={"csrf": token, "server": "https://ntfy.example", "topic": topic,
                                        "token": "tk_secret"})
    client.post("/notifications", data={"csrf": token, "server": "https://ntfy.example", "topic": topic})
    assert config_mod.load(tmp_path).ntfy_token == "tk_secret"  # same server: kept
    client.post("/notifications", data={"csrf": token, "server": "https://other.example", "topic": topic})
    assert config_mod.load(tmp_path).ntfy_token == ""


def test_logo_dev_redirects_only_over_https():
    hops = []

    def answer(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"location": f"http://img.logo.dev/x?token={request.url.params['token']}"})

    source = logos.LogoDev("pk_secret", transport=httpx.MockTransport(answer))
    with pytest.raises(logos.StopPass):
        source.fetch("DE0005557508")
    assert len(hops) == 1 and hops[0].startswith("https://")


def test_a_corrupt_unlock_cache_locks_instead_of_failing(tmp_path, monkeypatch):
    from brokersync import crypto, security

    security.change_passphrase(tmp_path, "eine sehr lange passphrase")
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "unlock.key").write_text("not hex")
    monkeypatch.setenv("COMPANION_RUNTIME", str(runtime))
    assert crypto.is_locked(tmp_path)
    assert crypto.unlock(tmp_path, "eine sehr lange passphrase") and not crypto.is_locked(tmp_path)


def test_credentials_can_be_deleted_with_the_password(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    vault = Vault(tmp_path)
    vault.set_broker_credentials("fake", {"username": "max"})
    vault.set_broker_session("fake", {"token": "t" * 40})
    config_mod.update(tmp_path, lambda c: c.brokers.__setitem__("fake", config_mod.BrokerConfig(enabled=True)))
    page = client.get("/brokers/fake").text
    assert 'action="/brokers/fake/forget"' in page
    token = csrf(client, "/brokers/fake")
    ctx = client.app.app().state.ctx
    ctx.pins["fake"] = "1234"
    r = client.post("/brokers/fake/forget", data={"csrf": token})
    assert "gelöscht" in r.text
    assert "fake" not in Vault(tmp_path).load().get("brokers", {}) and "fake" not in ctx.pins
    assert not config_mod.load(tmp_path).brokers["fake"].enabled
    assert 'action="/brokers/fake/forget"' not in client.get("/brokers/fake").text  # nothing left to delete
    assert any(e["event"] == "credentials" and e["detail"] == "deleted" for e in ctx.state.events(5))


def test_deleting_credentials_asks_for_the_password_after_a_while(tmp_path, monkeypatch):
    from .test_ui_sessions import clock

    c = clock(monkeypatch)
    client, _, _ = make(tmp_path)
    first_password(client)
    Vault(tmp_path).set_broker_credentials("fake", {"username": "max"})
    c.now += 16 * 60
    token = csrf(client, "/brokers/fake")
    assert 'id="forget_pw"' in client.get("/brokers/fake").text
    r = client.post("/brokers/fake/forget", data={"csrf": token, "ui_password": "falsch-falsch"})
    assert "stimmt nicht" in r.text and Vault(tmp_path).broker("fake")
    client.post("/brokers/fake/forget", data={"csrf": token, "ui_password": PASSWORD})
    assert not Vault(tmp_path).broker("fake")
