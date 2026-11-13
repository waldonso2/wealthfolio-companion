"""Every page in a real browser: axe-core (accessibility, contrast in light and dark), no CSP violation,
no script error, and nothing wider than a phone (#72, #83).

Chromium comes from `python -m playwright install chromium` (CI); `CHROMIUM_PATH` points to another build.
Without one the test is skipped locally and fails in CI.
"""

import os
import socket
import threading
import time

import pytest
import uvicorn

from .fake_broker import CODE
from .helpers import PASSWORD as UI_PASSWORD
from .helpers import first_password
from .test_web import csrf, make

sync_api = pytest.importorskip("playwright.sync_api")
axe_api = pytest.importorskip("axe_playwright_python.sync_playwright")

# Every page a signed-in user reaches (the hidden ones aside, routes.HIDDEN).
PAGES = ["/", "/broker", "/check", "/lookthrough", "/unknown", "/settings", "/brokers",
         "/brokers/fake", "/brokers/fake/login",
         "/brokers/fake/reset", "/setup/wealthfolio", "/notifications", "/securities", "/security",
         "/appearance", "/about", "/updates", "/services/sync", "/services/taxonomies", "/services/isin",
         "/services/logos", "/services/spending", "/services/xray"]
PHONE = {"width": 375, "height": 740}
DESKTOP = {"width": 1280, "height": 900}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The app after the whole setup and one sync (so the pages have content), served over HTTP."""
    mp = pytest.MonkeyPatch()
    from brokersync import crypto, vault

    mp.setattr(vault, "PASSWORD_HASH", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    mp.setattr(crypto, "ARGON2", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    mp.setenv("COMPANION_RECALC_WAIT", "0")
    import httpx

    from brokersync import lookthrough

    real = lookthrough.Sources  # the conftest's guard is per test; this module's setup runs before it
    mp.setattr(lookthrough, "PAUSE", 0)
    mp.setattr(lookthrough, "KNOWN", {})
    mp.setattr(lookthrough, "PRODUCTS", {})
    mp.setattr(lookthrough, "Sources", lambda: real(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    tmp = tmp_path_factory.mktemp("ui")
    mp.setenv("COMPANION_RUNTIME", str(tmp / "no-runtime"))
    client, _, _ = make(tmp / "data")
    first_password(client)
    token = csrf(client, "/setup/wealthfolio")
    from .fakes import PASSWORD as WF_PASSWORD

    client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": WF_PASSWORD})
    client.post("/brokers/fake", data={"csrf": token, "cred_username": "max", "enabled": "on",
                                       "cash_account_id": "acc-cash", "portfolio_account_id": "acc-depot"})
    client.post("/brokers/fake/login", data={"csrf": token, "code": CODE})
    assert "Abruf beendet" in client.post("/run", data={"csrf": token}).text

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        except Exception as e:  # no browser installed
            if os.environ.get("CI"):
                raise
            pytest.skip(f"no Chromium for Playwright: {e}")
        yield b
        b.close()


def _problems(page, url, phone):
    """What is wrong on one page: axe violations, console errors (CSP), a page wider than the screen."""
    errors: list[str] = []
    # A failed load is reported with its URL below; the browser's own favicon request has no response event.
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.goto(url)
    found = [f"{v['id']}: {n['target']} {n['failureSummary']}"
             for v in axe_api.Axe().run(page).response["violations"] for n in v["nodes"][:3]]
    found += [f"console: {e}" for e in errors]
    if phone and page.evaluate("document.documentElement.scrollWidth > window.innerWidth"):
        found.append("wider than the phone screen")
    return found


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_every_page_passes_axe_csp_and_fits(site, browser, scheme, viewport):
    context = browser.new_context(color_scheme=scheme, viewport=viewport)
    page = context.new_page()
    report = {"/login": _problems(page, site + "/login", viewport is PHONE)}
    page.fill("#pw", UI_PASSWORD)
    page.click("main form button")
    page.wait_for_url(site + "/")
    for path in PAGES:
        report[path] = _problems(page, site + path, viewport is PHONE)
    context.close()
    assert not {p: f for p, f in report.items() if f}
