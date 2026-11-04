"""What the forms of the web UI accept (#33): checked on the server, not only by the browser."""

import asyncio
from datetime import UTC, date, datetime

import pytest

from brokersync import config as config_mod
from brokersync.web import gate, inputs

from .test_security import logged_in
from .test_web import csrf


@pytest.mark.parametrize("url", ["http://wf.local:8080", "https://192.168.1.5/", "http://localhost:8088"])
def test_http_urls(url):
    assert inputs.http_url(url, "x") == url.rstrip("/")


@pytest.mark.parametrize("url", ["wf.local:8080", "file:///etc/passwd", "javascript:alert(1)", "ftp://x",
                                 "http://user:pw@wf.local", "http://wf.local/?a=1", "http://wf.local:99999",
                                 "http://", "http://a\nb", "http://" + "a" * 600])
def test_bad_urls(url):
    with pytest.raises(inputs.Invalid):
        inputs.http_url(url, "x")


def test_isin_iban_mic_symbol_date():
    assert inputs.isin(" ie00b4l5y983 ") == "IE00B4L5Y983"
    for bad in ("IE00B4L5Y984", "XX", "IE00B4L5Y98<"):
        with pytest.raises(inputs.Invalid):
            inputs.isin(bad)
    assert inputs.iban("DE89 3704 0044 0532 0130 00") == "DE89370400440532013000"
    assert inputs.iban("") == ""
    with pytest.raises(inputs.Invalid):
        inputs.iban("DE89370400440532013001")
    assert inputs.mic("xetr") == "XETR" and inputs.mic("") is None
    with pytest.raises(inputs.Invalid):
        inputs.mic("XETRA")
    assert inputs.symbol("SAP.DE") == "SAP.DE"
    with pytest.raises(inputs.Invalid):
        inputs.symbol("<script>")
    assert inputs.iso_date("2024-01-31", "x") == "2024-01-31" and inputs.iso_date("", "x") == ""
    for bad in ("31.01.2024", "2999-01-01"):
        with pytest.raises(inputs.Invalid):
            inputs.iso_date(bad, "x")
    assert inputs.security_id("de0007164600") == "DE0007164600"
    with pytest.raises(inputs.Invalid):
        inputs.security_id("<b>")


def test_today_is_german_time_not_the_servers(monkeypatch):
    class Clock(datetime):  # 23:30 UTC on 4 Nov is already 5 Nov in Berlin
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 4, 23, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(inputs, "datetime", Clock)
    assert inputs.today() == date(2026, 11, 5)
    assert inputs.iso_date("2026-11-05", "x") == "2026-11-05"  # not "in the future" any more
    with pytest.raises(inputs.Invalid):
        inputs.iso_date("2026-11-06", "x")


def test_forms_reject_bad_input_and_keep_the_config(tmp_path, hidden_pages):
    client = logged_in(tmp_path)
    token = csrf(client, "/setup/wealthfolio")
    r = client.post("/setup/wealthfolio", data={"csrf": token, "url": "file:///etc/passwd"})
    assert "http:// oder https://" in r.text
    assert config_mod.load(tmp_path).wealthfolio_url == "http://localhost:8080"

    r = client.post("/notifications", data={"csrf": token, "server": "ntfy.sh", "topic": "t"})
    assert "ntfy-Server" in r.text
    r = client.post("/notifications", data={"csrf": token, "server": "https://ntfy.sh", "topic": "a b/c"})
    assert "Thema" in r.text

    r = client.post("/transfers", data={"csrf": token, "delete": "abc"})
    assert r.status_code == 200
    r = client.post("/transfers", data={"csrf": token, "label": "Tagesgeld", "iban": "DE89370400440532013001"})
    assert "Prüfsumme" in r.text and config_mod.load(tmp_path).transfer_patterns == []

    r = client.post("/securities", data={"csrf": token, "isin": "IE00B4L5Y984", "symbol": "EUNL"})
    assert "gültige ISIN" in r.text
    r = client.post("/securities", data={"csrf": token, "isin": "IE00B4L5Y983", "symbol": "EU NL"})
    assert "Symbol" in r.text and config_mod.load(tmp_path).security_mappings == {}

    r = client.post("/brokers/fake", data={"csrf": token, "start_date": "01.01.2024"})
    assert "Startdatum" in r.text and config_mod.load(tmp_path).brokers.get("fake") is None

    r = client.post("/services/logos/settings", data={"csrf": token, "token": "pk_ok but spaces"})
    assert r.status_code == 422 and 'id="err-token"' in r.text and "logo.dev-Schlüssel" in r.text


def test_oversized_requests_are_refused_before_reading(tmp_path):
    client = logged_in(tmp_path)
    big = "x" * (gate.MAX_BODY + 1)
    # Also without a session: the login answers anyone.
    r = client.post("/login", content=f"password={big}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert r.headers["x-frame-options"] == "DENY"  # the security headers are on that answer too


def test_a_chunked_body_is_cut_off():
    chunks = [{"type": "http.request", "body": b"x" * 600, "more_body": True}] * 3

    async def receive():
        return chunks.pop(0)

    capped = gate._capped(receive, 1000)

    async def read():
        return [await capped() for _ in range(2)]

    first, second = asyncio.run(read())
    assert first["type"] == "http.request" and second["type"] == "http.disconnect"
