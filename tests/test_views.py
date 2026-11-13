"""One date/time format on every page, unknown bookings readable without JSON (#73)."""

import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from brokersync.state import State
from brokersync.web import views

from .fakes import PASSWORD
from .helpers import first_password
from .test_web import csrf, make


def test_dates_and_times_in_german_time_and_format():
    assert views.local_time("2026-03-03T10:00:01+00:00") == "03.03.2026 11:00"  # CET
    assert views.local_time("2026-07-03T10:00:01Z", seconds=True) == "03.07.2026 12:00:01"  # CEST
    assert views.local_time("2026-07-03T10:00:01") == "03.07.2026 12:00"  # no zone: UTC, like Wealthfolio
    assert views.local_day("2026-12-31T23:30:00+00:00") == "01.01.2027"  # the German day
    assert views.local_day("2026-01-02") == "02.01.2026"
    assert views.local_day(datetime(2026, 1, 2, 12, tzinfo=UTC)) == "02.01.2026"
    assert views.utc_iso("2026-03-03T11:00:01+01:00") == "2026-03-03T10:00:01+00:00"
    assert views.local_time("") == "" and views.local_day("kein Datum") == "kein Datum"
    assert views.money("1234.5") == "1.234,50"


def test_unknown_payloads_of_every_adapter_are_described():
    tr = views.describe_unknown({"eventType": "SPINOFF", "title": "Beispiel AG", "subtitle": "Abspaltung",
                                 "status": "EXECUTED", "betrag": "-12.5 EUR"})
    assert tr == {"amount": "-12,50 EUR", "security": "", "shares": "", "text": "Beispiel AG · Abspaltung · EXECUTED"}
    sc = views.describe_unknown({"type": "SECURITY_TRANSFER", "isin": "IE00TEST0001", "quantity": "3",
                                 "betrag": "0 EUR", "hinweis": "ohne Gegenstück"})
    assert (sc["security"], sc["shares"], sc["text"]) == ("IE00TEST0001", "3", "ohne Gegenstück")
    db = views.describe_unknown({"Umsatzart": "Umtausch", "Bezeichnung": "Test Fonds", "ISIN": "DE000TEST001",
                                 "Nominal / Stück": "10", "Ausmachender Betrag": "1.234,56"})
    assert db == {"amount": "1.234,56", "security": "Test Fonds · DE000TEST001", "shares": "10", "text": ""}
    cd = views.describe_unknown({"betrag": "7.10 EUR", "buchungstext": "Ertrag Fonds"})
    assert (cd["amount"], cd["text"]) == ("7,10 EUR", "Ertrag Fonds")
    assert views.describe_unknown(None)["amount"] == ""


def test_unknown_page_shows_fields_and_raw_data_only_folded(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    State(tmp_path).add_unknown("fake", "x1", "SPINOFF", "2026-03-03T23:30:00+00:00",
                                {"title": "Beispiel AG", "isin": "DE000TEST001", "betrag": "-12.5 EUR"})
    page = client.get("/unknown").text
    assert '<td data-label="Datum">04.03.2026</td>' in page  # German day
    assert '<td data-label="Betrag">-12,50 EUR</td>' in page
    assert re.search(r'<td data-label="Wertpapier">DE000TEST001', page)
    assert re.search(r"<details><summary[^>]*>Rohdaten</summary><pre>", page)
    # A pre-filled GitHub issue, opened only on click (#86): type, day and the masked payload.
    link = re.search(r'href="(https://github.com/[^"]+/issues/new\?[^"]+)"', page).group(1)
    query = parse_qs(urlparse(html.unescape(link)).query)
    assert query["title"] == ["Unbekannte Buchung: Testbroker SPINOFF"]
    assert "04.03.2026" in query["body"][0] and "DE000TEST001" in query["body"][0]


def test_traffic_light_per_broker():
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    ok = {"status": "ok", "started_at": "2026-10-12T06:00:00+00:00", "failed": 0}
    assert views.broker_light(None, None, 0, 0, True, now) == ("grey", [("Noch nie abgerufen", "")])
    assert views.broker_light(ok, {"deviations": []}, 0, 0, True, now) == ("green", [])
    needs_auth = {**ok, "status": "needs_auth"}
    assert views.broker_light(needs_auth, None, 0, 0, True, now) == ("red", [("Anmeldung nötig", "login")])
    assert views.broker_light({**ok, "status": "error", "failed": 2}, None, 0, 0, True, now)[0] == "red"
    light, reasons = views.broker_light(ok, {"deviations": [{}, {}]}, 1, 3, True, now)
    assert light == "yellow" and reasons == [("2 Abweichungen zu Wealthfolio", "check"),
                                             ("1 unbekannte Buchung", "unknown"), ("3 fehlt in Wealthfolio", "check")]
    old = {**ok, "started_at": "2026-10-09T06:00:00+00:00"}
    assert views.broker_light(old, None, 0, 0, True, now) == ("yellow", [("Seit 3 Tagen kein Abruf", "")])
    assert views.broker_light(old, None, 0, 0, False, now) == ("green", [])  # manual only: no reminder


def test_overview_shows_three_runs_and_folds_the_rest(tmp_path):
    client, _, _ = make(tmp_path)
    first_password(client)
    state = State(tmp_path)
    for _ in range(5):
        state.finish_run(state.start_run("fake"), "ok")
    page = client.get("/broker").text
    head, _, folded = page.partition("Ältere Läufe (2)")
    row = '<li><span class="status ok">'
    assert folded and head.count(row) == 3 and folded.count(row) == 2
    assert "Schritt für Schritt" not in page  # no setup steps any more (#83), a checklist instead (#110)


def test_fonts_are_served_from_the_container(tmp_path):
    client, _, _ = make(tmp_path)
    css = client.get("/static/style.css").text
    names = [f"jetbrains-mono-latin-{w}.woff2" for w in (400, 500, 600, 700)]  # what Wealthfolio bundles (fontsource)
    names += ["inter-latin-wght.woff2", "merriweather-latin-400.woff2", "merriweather-latin-700.woff2"]
    for name in names:
        r = client.get(f"/static/fonts/{name}")
        assert r.status_code == 200 and r.content[:4] == b"wOF2"
        assert f"/static/fonts/{name}" in css
    assert "http" not in css.split("*/", 1)[1]  # nothing from outside


def _font(page: str) -> str:
    return re.search(r'<html[^>]* data-font="([a-z]+)"', page).group(1)


@pytest.mark.parametrize(("chosen", "shown"), [("font-sans", "sans"), ("font-serif", "serif"), ("font-mono", "mono"),
                                               ("font-comic", "mono"), (None, "mono")])
def test_the_ui_uses_the_font_chosen_in_wealthfolio(tmp_path, chosen, shown):
    client, wf, _ = make(tmp_path)
    first_password(client)
    assert _font(client.get("/").text) == "mono"  # nothing read yet: Wealthfolio's default
    if chosen is None:
        wf.settings = None  # a Wealthfolio without GET /settings (404)
    else:
        wf.settings["font"] = chosen
    token = csrf(client, "/setup/wealthfolio")
    client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": PASSWORD})
    assert _font(client.get("/").text) == shown


def test_every_run_reads_the_font_again(tmp_path):
    client, wf, _ = make(tmp_path)
    first_password(client)
    token = csrf(client, "/setup/wealthfolio")
    client.post("/setup/wealthfolio", data={"csrf": token, "url": "http://wf.local:8080/", "password": PASSWORD})
    wf.settings["font"] = "font-serif"
    client.app.inner.state.ctx.syncer.run()
    assert _font(client.get("/").text) == "serif"
    wf.password = "changed"  # Wealthfolio unreachable for the run: the font read before stays, nothing fails
    client.app.inner.state.ctx.syncer.run()
    assert _font(client.get("/").text) == "serif"


def test_overview_card_has_two_buttons_and_the_reasons_link_to_their_page(tmp_path):
    from .test_safe_mode import make as make_safe

    client, _, _ = make_safe(tmp_path, safe=False)
    state = State(tmp_path)
    state.finish_run(state.start_run("fake"), "ok")
    state.add_unknown("fake", "x1", "ODD", "2026-01-02", {"title": "Beispiel"})
    state.set_reconcile("fake", [{"kind": "cash", "key": "EUR", "name": "Cash EUR", "broker": "1", "wealthfolio": "2"}])
    page = client.get("/broker").text
    card = page[page.index('id="broker-fake"'):]
    assert '<a href="/unknown">1 unbekannte Buchung</a>' in card
    assert '<a href="/check#broker-fake">1 Abweichung zu Wealthfolio</a>' in card
    actions = card[card.index('<div class="actions">'):card.index("</div>", card.index('<div class="actions">'))]
    assert actions.count("<button") == 1 and "Jetzt abrufen" in actions
    assert 'aria-label="Einstellungen Testbroker"' in actions and "<svg" in actions
    assert ">Unbekannte<" not in card and ">Prüfen<" not in card


def test_the_language_is_chosen_on_the_page_appearance_light_or_dark_in_the_header(tmp_path):
    from brokersync import config as config_mod

    from .test_safe_mode import make as make_safe

    client, _, token = make_safe(tmp_path, safe=False)
    page = client.get("/broker").text
    # #123: a session starts in the device's colours; the header's symbol turns them for every page until
    # logout - no script.
    assert "data-flip" not in page and "<script" not in page
    assert 'action="/theme"' in page and 'aria-label="Hell/Dunkel umschalten"' in page
    assert 'aria-pressed="false"' in page
    r = client.post("/theme", data={"csrf": token, "next": "/check"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/check")
    for path in ("/", "/check", "/settings"):  # every page, not only the one it was clicked on
        assert '<html lang="de" data-font="mono" data-theme="system" data-flip>' in client.get(path).text
    assert 'aria-pressed="true"' in client.get("/broker").text
    client.post("/theme", data={"csrf": token, "next": "/"})
    assert "data-flip" not in client.get("/broker").text  # and back
    for bad in ("//evil.example/x", "https://evil.example/", "/\\evil.example"):  # only a path of this site
        assert client.post("/theme", data={"csrf": token, "next": bad}, follow_redirects=False).headers[
            "location"] == "/"
    assert client.post("/theme", data={"next": "/"}).status_code == 400  # CSRF
    assert 'name="theme"' not in client.get("/appearance").text
    r = client.post("/appearance", data={"csrf": token, "language": "en"})
    assert "Saved." in r.text and '<html lang="en"' in r.text and 'aria-label="Switch light/dark"' in r.text
    assert config_mod.load(tmp_path).ui_language == "en"
    r = client.post("/appearance", data={"csrf": token, "language": "fr"})
    assert r.status_code == 422 and "Language: unknown choice." in r.text


def _html(page: str) -> str:
    return re.search(r"<html[^>]*>", page).group(0)


def test_font_and_theme_are_chosen_on_the_page_appearance(tmp_path):
    from brokersync import config as config_mod

    from .test_safe_mode import make as make_safe

    client, _, token = make_safe(tmp_path, safe=False)
    page = client.get("/appearance").text
    # Wealthfolio's tiles as radios: "Wie Wealthfolio" first and chosen, its "Aa" in Wealthfolio's font.
    assert re.findall(r'name="font" value="([a-z]*)"', page) == ["auto", "sans", "serif", "mono"]
    assert 'name="font" value="auto" checked' in page and "Wie Wealthfolio (Mono)" in page
    assert '<span class="tile-face font-mono" aria-hidden="true">Aa</span>' in page
    assert re.findall(r'name="scheme" value="([a-z]+)"', page) == ["light", "dark", "system"]
    assert 'name="scheme" value="system" checked' in page and "<script" not in page
    assert _html(page) == '<html lang="de" data-font="mono" data-theme="system">'

    r = client.post("/appearance", data={"csrf": token, "language": "de", "font": "serif", "scheme": "dark"})
    assert "Gespeichert." in r.text and _html(r.text) == '<html lang="de" data-font="serif" data-theme="dark">'
    c = config_mod.load(tmp_path)
    assert (c.ui_font, c.ui_scheme) == ("serif", "dark")
    assert 'name="font" value="serif" checked' in r.text and 'name="scheme" value="dark" checked' in r.text
    # The header's symbol turns a fixed scheme round on the server; "system" in the CSS (data-flip).
    client.post("/theme", data={"csrf": token, "next": "/"})
    assert _html(client.get("/").text) == '<html lang="de" data-font="serif" data-theme="light" data-flip>'
    client.post("/theme", data={"csrf": token, "next": "/"})

    for bad in ({"font": "comic"}, {"scheme": "sepia"}, {"font": "font-sans"}):
        r = client.post("/appearance", data={"csrf": token, "language": "de", **bad})
        assert r.status_code == 422 and "unbekannte Auswahl" in r.text and 'role="alert"' in r.text
    c = config_mod.load(tmp_path)
    assert (c.ui_font, c.ui_scheme, c.ui_language) == ("serif", "dark", "de")  # nothing of a bad form saved
    assert client.post("/appearance", data={"csrf": token, "language": "en", "font": "x"}).status_code == 422
    assert config_mod.load(tmp_path).ui_language == "de"
    # A form without the fields (the language only) keeps them; back to Wealthfolio's font.
    client.post("/appearance", data={"csrf": token, "language": "de"})
    assert _html(client.get("/").text) == '<html lang="de" data-font="serif" data-theme="dark">'
    client.post("/appearance", data={"csrf": token, "language": "de", "font": "auto", "scheme": "system"})
    assert _html(client.get("/").text) == '<html lang="de" data-font="mono" data-theme="system">'
    assert config_mod.load(tmp_path).ui_font == ""


def test_the_page_about(tmp_path):
    from brokersync import __version__
    from brokersync.web.routes.settings import ABOUT_LINKS

    from .test_safe_mode import make as make_safe

    client, _, _ = make_safe(tmp_path, safe=False)
    page = client.get("/about").text
    assert "<h2>Wealthfolio Companion</h2>" in page and f"Version {__version__}" in page
    assert '<img class="app-icon" src="/static/icon.svg"' in page
    for key in ("repo", "docs", "issues", "security", "license", "pytr", "fints", "sc"):
        assert f'href="{ABOUT_LINKS[key]}" rel="noopener noreferrer"' in page, key
    assert "Fehler melden" in page and "Sicherheitslücke melden" in page and "Datenschutz:" in page
    assert ABOUT_LINKS["repo"].startswith("https://github.com/waldonso2/")
    assert 'href="/about" aria-current="page"' in page  # in the settings sidebar
    assert '<a href="/about">Über</a>' in client.get("/broker").text  # and in the footer


def test_a_saved_fixed_colour_scheme_of_before_is_ignored(tmp_path):
    import json
    from dataclasses import asdict

    from brokersync import config as config_mod
    from brokersync import crypto

    config_mod.save(tmp_path, config_mod.Config(ui_language="en"))
    data = {**asdict(config_mod.Config(ui_language="en")), "ui_theme": "dark"}  # as stored before #123
    for k in config_mod.NOTIFY_FIELDS:
        data.pop(k)
    crypto.write_atomic(tmp_path / "config.enc",
                        crypto.seal(crypto.data_key(tmp_path), json.dumps(data).encode(), "config"))
    c = config_mod.load(tmp_path)
    assert c.ui_language == "en" and not hasattr(c, "ui_theme")


def test_a_saved_fire_section_of_before_is_ignored(tmp_path):
    import json
    from dataclasses import asdict

    from brokersync import config as config_mod
    from brokersync import crypto

    config_mod.save(tmp_path, config_mod.Config(ui_language="en"))
    data = {**asdict(config_mod.Config(ui_language="en")), "fire": {"expenses": "30000"}}  # the removed calculator
    for k in config_mod.NOTIFY_FIELDS:
        data.pop(k)
    crypto.write_atomic(tmp_path / "config.enc",
                        crypto.seal(crypto.data_key(tmp_path), json.dumps(data).encode(), "config"))
    c = config_mod.load(tmp_path)
    assert c.ui_language == "en" and not hasattr(c, "fire")
    config_mod.save(tmp_path, c)  # and it is gone once saved again
    blob = (tmp_path / "config.enc").read_bytes()
    assert "fire" not in json.loads(crypto.unseal(crypto.data_key(tmp_path), blob, "config"))


def test_the_colours_meet_wcag_aa_in_both_schemes():
    """#105: status text ≥ 4.5:1 on card and page, the traffic light's grey ≥ 3:1, and the counter badge."""
    import re
    from pathlib import Path

    css = (Path(__file__).parent.parent / "src" / "brokersync" / "web" / "static" / "style.css").read_text()
    light = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6})", css.split("@media")[0]))
    switched = css.split("@media (prefers-color-scheme: light) {")[1]
    dark = dict(re.findall(r"--([a-z-]+): (#[0-9a-f]{6})", switched.split("}")[0]))

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for scheme in (light, dark):
        for bg in (scheme["bg"], scheme["card"]):
            for text in ("ok", "warn", "error", "muted", "text"):
                assert ratio(scheme[text], bg) >= 4.5, (text, scheme[text], bg)
            assert ratio(scheme["grey"], bg) >= 3, (scheme["grey"], bg)
        assert ratio(scheme["on-primary"], scheme["warn"]) >= 4.5  # the counter of *Unbekannt*


def test_english_pages_use_english_formats():
    """#115: with the language English, days are ISO and amounts 1,234.56; German stays as it was."""
    from brokersync.web import views

    assert views.local_day("2026-03-03T10:00:00Z", "en") == "2026-03-03"
    assert views.local_time("2026-03-03T10:00:00Z", lang="en") == "2026-03-03 11:00"
    assert views.money("1234.5", "en") == "1,234.50" and views.money("1234.5") == "1.234,50"
    assert views.local_day("2026-03-03T10:00:00Z") == "03.03.2026"
