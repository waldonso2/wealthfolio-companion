"""Running as a container (wealthfolio-importer-addon#148): key only from the secret, the daily fetch in-process."""

import threading
from datetime import datetime

import pytest

from brokersync import container, crypto, security
from brokersync.sync import BERLIN


def test_next_run_is_the_set_time_german_time_plus_the_jitter():
    now = datetime(2026, 10, 29, 5, 0, tzinfo=BERLIN)
    assert container.next_run(now, "06:00", 30) == datetime(2026, 10, 29, 6, 30, tzinfo=BERLIN)
    assert container.next_run(datetime(2026, 10, 29, 7, 0, tzinfo=BERLIN), "06:00", 10) == \
        datetime(2026, 10, 30, 6, 10, tzinfo=BERLIN)
    # Across the change to winter time the local time stays 06:00.
    assert container.next_run(datetime(2026, 10, 24, 12, 0, tzinfo=BERLIN), "06:00").astimezone(BERLIN).hour == 6


def test_the_scheduler_runs_renews_and_stops(monkeypatch):
    monkeypatch.setattr(container, "next_run", lambda now, at, jitter: now)
    stop = threading.Event()
    runs = []

    def run():
        runs.append(1)
        if len(runs) == 2:
            stop.set()

    container.scheduler(stop, run=run, renew=lambda: False)
    assert len(runs) == 2
    killed = []
    monkeypatch.setattr(container.os, "kill", lambda pid, sig: killed.append(sig))
    container.scheduler(threading.Event(), run=lambda: runs.append(1), renew=lambda: True)
    assert killed and len(runs) == 2  # a renewed certificate restarts the process before the fetch


def test_in_the_container_the_key_comes_only_from_the_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANION_CONTAINER", "1")
    monkeypatch.setenv("COMPANION_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(crypto.KeyMissing, match="Secret companion-key"):
        crypto.host_key(tmp_path / "data")
    assert not (tmp_path / "data" / "secret.key").exists()  # never a key in data/
    short = tmp_path / "short"
    short.write_text("abc")
    monkeypatch.setenv("COMPANION_KEY_FILE", str(short))
    with pytest.raises(crypto.KeyMissing, match="zu kurz"):
        crypto.host_key(tmp_path / "data")
    good = tmp_path / "key"
    good.write_text("k" * 44)
    monkeypatch.setenv("COMPANION_KEY_FILE", str(good))
    assert crypto.host_key_source(tmp_path) == "container"
    checks = {c.label: c for c in security.status(tmp_path, secure=True)}
    assert checks["Schlüssel des Dienstes"].ok and "Secret des Containers" in checks["Schlüssel des Dienstes"].text


def test_tls_is_on_in_the_container_unless_switched_off(monkeypatch, tmp_path):
    monkeypatch.delenv("COMPANION_TLS", raising=False)
    monkeypatch.delenv("COMPANION_CONTAINER", raising=False)
    assert not container.tls_enabled()
    monkeypatch.setenv("COMPANION_CONTAINER", "1")
    assert container.tls_enabled()
    monkeypatch.setenv("COMPANION_TLS_DIR", str(tmp_path / "tls"))
    assert (container.certificate() / "cert.pem").exists()
    assert not container._cert_changed()  # still fresh: nothing renewed
    monkeypatch.setenv("COMPANION_TLS", "off")
    assert not container.tls_enabled() and container.certificate() is None


def test_without_its_key_the_container_does_not_start(tmp_path, monkeypatch, capsys):
    from brokersync.cli import main

    monkeypatch.setenv("COMPANION_DATA", str(tmp_path))
    monkeypatch.setenv("COMPANION_CONTAINER", "1")
    monkeypatch.setenv("COMPANION_KEY_FILE", str(tmp_path / "missing"))
    assert main(["container"]) == 2


def test_the_container_migrates_prints_the_setup_code_and_serves(tmp_path, monkeypatch, capsys):
    from brokersync.config import load

    key = tmp_path / "key"
    key.write_text("k" * 44)
    data = tmp_path / "data"
    monkeypatch.setenv("COMPANION_DATA", str(data))
    monkeypatch.setenv("COMPANION_CONTAINER", "1")
    monkeypatch.setenv("COMPANION_KEY_FILE", str(key))
    monkeypatch.setenv("COMPANION_TLS", "off")
    served = []
    monkeypatch.setattr("brokersync.web.serve.serve", lambda d, **kw: served.append(kw))
    monkeypatch.setattr(container, "scheduler", lambda stop, **kw: None)

    class Args:
        host = "0.0.0.0"

    assert container.main(Args()) == 0
    assert "Setup code" in capsys.readouterr().out
    assert served == [{"host": "0.0.0.0", "port": 8090, "certfile": None, "keyfile": None,
                       "redirect_port": None}]
    assert load(data).services == {}  # a new install: every service starts off
    assert load(data).ui_language == "en"  # and starts in English


def test_the_updates_page_says_how_a_container_is_updated(tmp_path, monkeypatch):
    from .helpers import first_password
    from .test_web import make

    monkeypatch.setenv("COMPANION_CONTAINER", "1")
    client, _, _ = make(tmp_path)
    first_password(client)
    page = client.get("/updates").text
    assert "pull-verified-image.sh --env .env" in page and "docker compose pull" in page
    assert 'name="auto_update"' not in page
