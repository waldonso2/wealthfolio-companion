import pytest


@pytest.fixture(autouse=True)
def _no_recalc_wait(monkeypatch):
    # The fake Wealthfolio has its holdings at once; don't wait for a recalculation.
    monkeypatch.setenv("COMPANION_RECALC_WAIT", "0")
    monkeypatch.setenv("COMPANION_UPDATE_CHECK", "0")  # no test asks GitHub


@pytest.fixture(autouse=True)
def _no_issuer_requests(monkeypatch):
    # X-Ray fetches iShares' lists after every run; no test reaches iShares (they answer "not found").
    import httpx

    from brokersync import firds, lookthrough

    real = lookthrough.Sources
    monkeypatch.setattr(lookthrough, "PAUSE", 0)
    monkeypatch.setattr(firds, "PAUSE", 0)
    monkeypatch.setattr(lookthrough, "KNOWN", {})
    monkeypatch.setattr(lookthrough, "PRODUCTS", {})  # the shipped list: tests ask their own fund finders
    monkeypatch.setattr(lookthrough, "Sources",
                        lambda: real(transport=httpx.MockTransport(lambda request: httpx.Response(404))))


@pytest.fixture(autouse=True)
def _no_scalable_pause(monkeypatch):
    # The pause between Scalable detail queries is for the real rate limit only.
    from brokersync.adapters import scalable

    monkeypatch.setattr(scalable, "DETAIL_PAUSE", 0)


@pytest.fixture(autouse=True)
def _no_rate_limit_waits(monkeypatch):
    # The shared backoff (#149) waits through ``ratelimit.sleep``: record the waits instead of waiting.
    from brokersync import ratelimit

    waits: list[float] = []
    monkeypatch.setattr(ratelimit, "sleep", waits.append)
    return waits


@pytest.fixture(autouse=True)
def _fast_key_derivation(monkeypatch, tmp_path_factory):
    # argon2id with its real cost (64 MiB) for every login would make the suite slow; the
    # parameters themselves are checked in test_security. No /run in tests: the unlock cache is per process.
    from brokersync import crypto, vault

    monkeypatch.setattr(vault, "PASSWORD_HASH", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    monkeypatch.setattr(crypto, "ARGON2", {"time_cost": 1, "memory_cost": 1024, "parallelism": 1})
    monkeypatch.setenv("COMPANION_RUNTIME", str(tmp_path_factory.mktemp("no-runtime") / "missing"))
    for var in ("CREDENTIALS_DIRECTORY", "COMPANION_KEY_FILE", "COMPANION_KEY_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    crypto._memory.clear()


@pytest.fixture
def hidden_pages(monkeypatch):
    """Include the pages hidden for now (waldonso2/wealthfolio-importer-addon#84) so their code stays tested."""
    from brokersync.web import app as app_mod
    from brokersync.web.routes import HIDDEN, ROUTERS

    monkeypatch.setattr(app_mod, "ROUTERS", ROUTERS + HIDDEN)
