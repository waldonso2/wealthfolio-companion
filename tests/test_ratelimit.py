"""The shared rule for 429/503 (waldonso2/wealthfolio-importer-addon#149): Retry-After, bounded retries of reads
only - never a login, token or TAN request -, and a broker that stays limited stops cleanly and waits."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from brokersync import meta_keys, ratelimit
from brokersync.adapters import ADAPTERS
from brokersync.adapters import comdirect as cd
from brokersync.adapters import enablebanking as eb
from brokersync.adapters.base import AdapterError, rate_limited

from .fake_broker import FakeBroker
from .fakes import FakeWealthfolio
from .test_sync import setup

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def answer(status: int, retry_after: str = "") -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://x.invalid/a?token=t"))


def test_retry_after_reads_seconds_a_date_and_ignores_garbage():
    assert ratelimit.retry_after(answer(429, "3"), 7) == 3
    assert ratelimit.retry_after(answer(429, "Fri, 30 Oct 2026 12:00:20 GMT"), 7, now=NOW) == 20
    assert ratelimit.retry_after(answer(429, "Fri, 30 Oct 2026 11:00:00 GMT"), 7, now=NOW) == 0  # in the past
    assert ratelimit.retry_after(answer(429, "soon, please"), 7) == 7
    assert ratelimit.retry_after(answer(429, "-5"), 7) == 7
    assert ratelimit.retry_after(answer(429), 7) == 7
    assert ratelimit.retry_after(answer(429, "86400"), 7) == ratelimit.MAX_WAIT  # capped


def sender(*statuses: int, retry_after: str = ""):
    calls: list[int] = []

    def send() -> httpx.Response:
        calls.append(1)
        return answer(statuses[min(len(calls), len(statuses)) - 1], retry_after)

    return send, calls


def test_a_short_limit_is_waited_out(_no_rate_limit_waits):
    send, calls = sender(429, 200)
    assert ratelimit.with_backoff(send).status_code == 200
    assert len(calls) == 2 and len(_no_rate_limit_waits) == 1 and 2 <= _no_rate_limit_waits[0] <= 2.4


def test_retry_after_is_followed_and_503_counts_like_429(_no_rate_limit_waits):
    send, calls = sender(503, 200, retry_after="3")
    assert ratelimit.with_backoff(send).status_code == 200
    assert 3 <= _no_rate_limit_waits[0] <= 3.6


def test_after_the_last_wait_the_answer_comes_back_as_it_is(_no_rate_limit_waits):
    send, calls = sender(429, retry_after="3600")
    assert ratelimit.with_backoff(send).status_code == 429
    assert len(calls) == len(ratelimit.WAITS) + 1
    assert all(w <= ratelimit.MAX_WAIT for w in _no_rate_limit_waits)  # a run never hangs for long


def test_other_errors_are_not_tried_again(_no_rate_limit_waits):
    send, calls = sender(500, 200)
    assert ratelimit.with_backoff(send).status_code == 500
    assert len(calls) == 1 and not _no_rate_limit_waits


def test_the_wait_named_to_the_user_follows_retry_after():
    e = rate_limited("Testbank", answer(429, "7200"), now=NOW)
    assert e.until == NOW + timedelta(hours=2) and "Testbank begrenzt gerade die Abrufe (HTTP 429)" in str(e)
    assert "30.10.2026 15:00" in str(e)  # German time
    assert rate_limited("TR", now=NOW).until == NOW + timedelta(seconds=ratelimit.NEXT_TRY)
    assert isinstance(e, AdapterError)


# ── adapters ────────────────────────────────────────────────────────────────

def test_comdirect_never_retries_the_password_grant(monkeypatch, _no_rate_limit_waits):
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429, json={})

    monkeypatch.setattr(cd.ComdirectAdapter, "transport", httpx.MockTransport(handle))
    a = cd.ComdirectAdapter({"username": "1234567", "pin": "000000", "client_id": "c", "client_secret": "s"})
    with pytest.raises(AdapterError):
        a.login()
    assert len(calls) == 1 and not _no_rate_limit_waits
    assert not a.session.get("open_challenges") and not a.session.get("pin_rejected")


def test_comdirect_reads_are_retried(monkeypatch):
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(429 if len(calls) == 1 else 200, json={"values": []})

    monkeypatch.setattr(cd.ComdirectAdapter, "transport", httpx.MockTransport(handle))
    a = cd.ComdirectAdapter({}, {"access_token": "a", "expires_at": 9e12})
    assert a.get("/banking/clients/user/v2/accounts/balances") == {"values": []} and len(calls) == 2


def test_enable_banking_retries_reads_but_never_the_login_posts(monkeypatch, _no_rate_limit_waits):
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(429, json={"message": "slow down"})
        if request.url.path == "/aspsps" and calls.count("GET /aspsps") == 1:
            return httpx.Response(429)
        if request.url.path == "/aspsps":
            return httpx.Response(200, json={"aspsps": [{"name": "Testbank", "country": "DE"}]})
        return httpx.Response(200, json={"redirect_urls": ["https://example.invalid/back"]})

    monkeypatch.setattr(eb.EnableBankingAdapter, "transport", httpx.MockTransport(handle))
    a = ADAPTERS["psd2-1"]({"bank": "Testbank"})
    a._bearer = "recorded"
    with pytest.raises(AdapterError):
        a.login()
    assert calls == ["GET /aspsps", "GET /aspsps", "GET /application", "POST /auth"]
    assert len(_no_rate_limit_waits) == 1


# ── the run ─────────────────────────────────────────────────────────────────

class LimitedBroker(FakeBroker):
    key = "broken"
    label = "Gedrosselt"
    asked = 0

    def get_transactions(self, since):
        type(self).asked += 1
        raise rate_limited(self.label, answer(429, "7200"))


def test_a_limited_broker_stops_waits_and_leaves_the_others_alone(tmp_path):
    wf = FakeWealthfolio()
    syncer, notifier = setup(tmp_path, wf, brokers=("broken", "fake"))
    syncer.vault.set_broker_session("broken", {"confirmed": True, "anchor": "2026-01-05T15:00:00+00:00"})
    syncer.adapters = {"fake": FakeBroker, "broken": LimitedBroker}
    LimitedBroker.asked = 0
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "limited" and "Gedrosselt begrenzt gerade" in results["broken"].messages[0]
    assert results["fake"].status == "ok" and results["fake"].created == 3
    assert [t for t, _, _ in notifier.sent if "Abruflimit" in t] == ["Gedrosselt: Abruflimit"]
    assert syncer.state.last_success("broken") is None  # nothing of it counts as fetched
    until = syncer.state.meta(meta_keys.RATE_LIMIT("broken"))
    assert until and datetime.fromisoformat(until) > datetime.now(UTC) + timedelta(minutes=110)

    # The next scheduled run doesn't ask the broker before then; the last run stays what the card shows.
    results = {r.broker: r for r in syncer.run()}
    assert results["broken"].status == "skipped" and LimitedBroker.asked == 1
    assert syncer.state.last_run("broken").status == "limited"
    # A fetch the user starts tries; still limited, it isn't notified again.
    syncer.run(["broken"], manual=True)
    assert LimitedBroker.asked == 2 and len([t for t, _, _ in notifier.sent if "Abruflimit" in t]) == 1

    # Once the time is over and a run gets through, the wait is gone.
    syncer.state.set_meta(meta_keys.RATE_LIMIT("broken"), (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    syncer.adapters = {"fake": FakeBroker, "broken": FakeBroker}
    assert {r.broker: r.status for r in syncer.run()}["broken"] == "ok"
    assert syncer.state.meta(meta_keys.RATE_LIMIT("broken")) is None


def test_the_card_shows_the_limit_in_yellow_with_the_next_try():
    from brokersync.web import views

    last = {"status": "limited", "started_at": datetime.now(UTC).isoformat()}
    light, reasons = views.broker_light(last, None, 0, 0, True, limited_until="2026-10-30T14:00:00+00:00")
    assert light == "yellow" and reasons == [("Abruflimit beim Anbieter - nächster Versuch ab 30.10.2026 15:00", "")]
    assert views.broker_light(last, None, 0, 0, True, lang="en")[1] == [("The provider limits the requests", "")]
