"""One sync run: every enabled broker → Wealthfolio.

Per broker: log in (stored session), fetch the transactions - the whole
history for adapters that can read it cheaply (``full_history``), else since
the last successful run (minus an overlap) -, skip the ones already synced,
check the rest against Wealthfolio's existing activities, create what is
missing. Then compare the ones synced before with Wealthfolio
(``brokersync.coverage``): deleted ones and activities the broker no longer
lists are shown on the page *Prüfung* and reported, never re-created or
deleted without the user. A broker that fails is reported and the others still
run (AC 9). Unknown event types are stored and reported once, never dropped
(AC 10).

Only one run at a time: the web UI and the systemd timer share a lock file.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import (
    __version__,
    appearance,
    coverage,
    crypto,
    lookthrough,
    meta_keys,
    retired,
    spending,
    updates,
    xray,
    xray_addon,
)
from . import assets as assets_mod
from . import config as config_mod
from . import history as history_mod
from . import logos as logos_mod
from . import repair as repair_mod
from .adapters import ADAPTERS, AdapterError, AuthRequired, BrokerAdapter, apply_labels
from .adapters.base import RateLimited, safety_state
from .adapters.fints import NO_TAN_DAYS
from .dedup import ExistingIndex
from .history import BERLIN, opening_id
from .mapping import Accounts, MappingError, SecurityMapping, TransferPattern, to_activities, tx_ref
from .model import CashBalance, Kind, Position, Transaction, when
from .notify import Notifier
from .reconcile import compare
from .redact import redact
from .state import State, masked
from .vault import Vault
from .wealthfolio import DuplicateActivity, StorageUnsupported, WealthfolioClient, WealthfolioError, connect

log = logging.getLogger(__name__)
__all__ = ["BERLIN", "opening_id"]  # where the web UI and the tests take them from

# Re-fetch this much before the last successful run: brokers book late.
OVERLAP = timedelta(days=7)
# A bank's securities settlement lies at most this many days from the trade.
SETTLEMENT_DAYS = 6
UNMATCHED_SECURITIES = "WERTPAPIER_OHNE_GEGENSTUECK"


def tax_flag(key: str) -> str:
    return meta_keys.DIVIDEND_TAX(key)


def refetch_flag(key: str) -> str:
    return meta_keys.REFETCH(key)


def savings_flag(key: str) -> str:
    """Meta: the savings account (Wealthfolio id) a run read without failures - another one is read anew."""
    return meta_keys.SAVINGS_ACCOUNT(key)


class AlreadyRunning(Exception):
    pass


@dataclass
class BrokerResult:
    broker: str
    status: str  # ok | needs_auth | error | skipped
    created: int = 0
    existing: int = 0
    failed: int = 0
    unknown: int = 0
    messages: list[str] = field(default_factory=list)


@dataclass
class RunContext:
    """What one broker's run carries from step to step (``Syncer._sync_broker``)."""
    cfg: config_mod.Config
    key: str
    bcfg: config_mod.BrokerConfig
    result: BrokerResult
    secrets: dict
    adapter: BrokerAdapter
    # plan_window
    backfill: bool = False
    own_tax: bool = False
    full: bool = False
    refetch: bool = False
    since: datetime | None = None
    savings: str = ""
    savings_new: bool = False
    synced: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    # fetch
    tax_backfill: bool = False
    transactions: list[Transaction] = field(default_factory=list)
    history: date | None = None
    cash: list[CashBalance] | None = None
    positions: list[Position] | None = None
    # prepare
    todo: list[Transaction] = field(default_factory=list)
    accounts: Accounts = field(default_factory=lambda: Accounts("", ""))
    mappings: dict[str, SecurityMapping] = field(default_factory=dict)
    patterns: list[TransferPattern] = field(default_factory=list)
    paired: dict[str, str] = field(default_factory=dict)
    # with Wealthfolio
    repaired: int = 0
    activities: list[dict] = field(default_factory=list)
    unmatched: list[Transaction] = field(default_factory=list)


@contextmanager
def run_lock(data_dir: Path, wait: float = 0) -> Iterator[None]:
    """The sync lock, shared by the web UI and the timer; waits up to ``wait`` seconds."""
    deadline = time.monotonic() + wait
    with crypto.open_lock(Path(data_dir) / "sync.lock") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as e:
                if time.monotonic() >= deadline:
                    raise AlreadyRunning("A sync is already running.") from e
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def is_running(data_dir: Path) -> bool:
    """Whether a sync holds the lock - in this process or another (the timer)."""
    try:
        with run_lock(data_dir):
            return False
    except AlreadyRunning:
        return True


class Syncer:
    def __init__(self, data_dir: Path, *, wealthfolio: Callable[[str, str | None], WealthfolioClient] | None = None,
                 notifier: Notifier | None = None, adapters: dict[str, type[BrokerAdapter]] | None = None,
                 logo_source: Callable[[str], logos_mod.LogoDev] | None = None,
                 fund_sources: Callable[[], lookthrough.Sources] | None = None):
        self.data_dir = Path(data_dir)
        self.vault = Vault(self.data_dir)
        self.state = State(self.data_dir)
        retired.clean_up(self.data_dir, self.state, self.vault)
        self.adapters = adapters or ADAPTERS
        apply_labels(self.adapters, config_mod.load(self.data_dir).brokers)
        self._consent_existing()
        self._wealthfolio = wealthfolio or WealthfolioClient
        # Seconds to give Wealthfolio to recalculate holdings before the check.
        self.recalc_wait = float(os.environ.get("COMPANION_RECALC_WAIT", "5"))
        self._notifier = notifier
        self._pins: dict[str, str] = {}  # PINs typed at the login, for the current run only (#85)
        self._manual = False  # the current run was started in the UI (a rate limit doesn't hold it back, #149)
        self._progress: dict = {}  # the broker being fetched, for the overview (#104)
        self._logo_source = logo_source or logos_mod.LogoDev
        self._fund_sources = fund_sources or lookthrough.Sources

    def _consent_existing(self) -> None:
        """Brokers with a ``consent`` set up before it existed (0.23.0, #112): their credentials were saved
        before - they count as agreed. New credentials can't be saved without agreeing (broker settings)."""
        brokers = self.vault.load().get("brokers", {})
        cfg = config_mod.load(self.data_dir)
        keys = [k for k, a in self.adapters.items() if a.consent and brokers.get(k, {}).get("credentials")
                and not (cfg.brokers.get(k) and cfg.brokers[k].consented)]
        def agree(c: config_mod.Config) -> None:
            for k in keys:
                c.broker(k).consented = True

        if keys:
            config_mod.update(self.data_dir, agree)

    def notifier(self, cfg: config_mod.Config) -> Notifier:
        if self._notifier:
            return self._notifier
        return Notifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token, details=cfg.ntfy_details)

    def link(self, cfg: config_mod.Config, path: str) -> str | None:
        return f"{cfg.public_url.rstrip('/')}{path}" if cfg.public_url else None

    def run(self, only: list[str] | None = None, *, manual: bool = False,
            pins: dict[str, str] | None = None) -> list[BrokerResult]:
        """``manual``: started in the UI after the user's login (the safe mode fetches only then).
        ``pins``: PINs typed at the login, used for this run only and never stored (#85)."""
        # Waits a moment: the status page holds the lock briefly for its checks.
        with run_lock(self.data_dir, wait=5):
            self.state.abort_stale_runs()
            cfg = config_mod.load(self.data_dir)
            self._read_appearance(cfg)
            if cfg.safe_mode and not manual:
                if cfg.service("sync"):
                    self._remind(cfg)
                self._update_funds(cfg)  # the issuers' lists are no broker: they stay daily in safe mode too
                return []
            self._pins = dict(pins or {})
            self._manual = manual
            # "Automatisch abrufen" off: the timer and the general button skip
            # the broker, a request for exactly this broker still runs it.
            keys = [key for key, bcfg in cfg.brokers.items()
                    if key in self.adapters and (not only or key in only) and (bcfg.enabled or (only and key in only))
                    and cfg.service("sync")]
            results = []
            try:
                for n, key in enumerate(keys, 1):
                    self._progress = {"broker": key, "label": self.adapters[key].label, "n": n, "m": len(keys)}
                    results.append(self._run_broker(cfg, key))
                self._progress = {}
                if cfg.service("isin"):
                    self._complete_isins(cfg)  # before the logos: they look the ISIN up there first
                self._update_logos(cfg)
                self._update_funds(cfg)
                self._update_spending(cfg)
                self._check_update(cfg)
            finally:
                self._pins = {}
                self.state.delete_meta(meta_keys.PROGRESS())
                self.state.prune_events()
            return results

    def _phase(self, phase: str) -> None:
        """Where the run is, for the overview (#104): login, fetch, book, check or approval (waiting for the app)."""
        if self._progress:
            self._progress.update(phase=phase, since=datetime.now(UTC).isoformat())
            self.state.set_meta(meta_keys.PROGRESS(), json.dumps(self._progress))

    def _remind(self, cfg: config_mod.Config) -> None:
        """Safe mode: the timer contacts no broker - it reminds the user to fetch in the UI."""
        due = [self.adapters[k].label for k, b in cfg.brokers.items()
               if k in self.adapters and b.enabled and self.vault.broker(k).get("credentials")]
        log.info("safe mode: scheduled run skipped (%d broker(s) to fetch in the UI)", len(due))
        if due:
            self.notifier(cfg).send("Abruf fällig", f"Sicherer Modus: {', '.join(due)} in der Oberfläche abrufen - "
                                    "jeder Abruf braucht deine Anmeldung.", link=self.link(cfg, "/broker"), tags="key")

    def end_sessions(self, keys: list[str] | None = None) -> None:
        """Safe mode: forget the brokers' sessions, keeping only the lock-out guards (``SAFETY_KEYS``)."""
        def change(d: dict) -> None:  # under the vault's lock: a login's newer counters aren't lost
            for key, b in d.get("brokers", {}).items():
                session = b.get("session")
                if (keys is None or key in keys) and session and session != safety_state(session):
                    b["session"] = safety_state(session)

        self.vault.update(change)

    def learned_isins(self, cfg: config_mod.Config) -> dict[str, str]:
        """Asset id -> ISIN of every position the sync learned (assets.py)."""
        return {a["asset_id"]: isin for key in cfg.brokers for isin, a in self.state.assets(key).items()}

    def complete_isins(self) -> int | None:
        """*Jetzt ergänzen* on the service's page (#151): one pass now (under the sync lock)."""
        with run_lock(self.data_dir, wait=5):
            return self._complete_isins(config_mod.load(self.data_dir))

    def _complete_isins(self, cfg: config_mod.Config) -> int | None:
        """Missing ISINs on the assets the sync knows (#140); never touches a broker's result. How many were
        set, None when it failed."""
        learned = self.learned_isins(cfg)
        if not learned:
            return 0
        try:
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                done = assets_mod.complete_isins(self.state, wf, learned)
            if done:
                log.info("ISIN set on %d Wealthfolio assets", done)
            return done
        except Exception:
            log.exception("ISINs not completed")
            return None

    def update_logos(self) -> logos_mod.Result | None:
        """The button on the page *Logos*: one pass now (under the sync lock)."""
        with run_lock(self.data_dir, wait=5):
            return self._update_logos(config_mod.load(self.data_dir))

    def _update_logos(self, cfg: config_mod.Config) -> logos_mod.Result | None:
        """Logos after a run, if turned on; whatever goes wrong here never touches the brokers' results."""
        secrets = self.vault.load()
        token = secrets.get("logo_dev_token")
        if not cfg.logos_enabled or not token:
            return None
        learned = {a["asset_id"]: isin for key in cfg.brokers for isin, a in self.state.assets(key).items()}
        source = self._logo_source(token)
        try:
            with connect(self._wealthfolio, cfg.wealthfolio_url, secrets.get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                return logos_mod.update(self.state, wf, source, learned)
        except Exception:
            log.exception("logos not updated")
            return None
        finally:
            source.close()

    def update_funds(self, only: str = "") -> lookthrough.Result | None:
        """The buttons on *Durchschau*: fetch every fund's holdings now, or ``only`` this asset's (under the sync
        lock)."""
        with run_lock(self.data_dir, wait=5):
            return self._update_funds(config_mod.load(self.data_dir), force=True, only=only)

    def _update_funds(self, cfg: config_mod.Config, *, force: bool = False,
                      only: str = "") -> lookthrough.Result | None:
        """Look-through after a run (#139): the funds' holdings if turned on, then new cluster risks via ntfy.
        Whatever goes wrong here never touches the brokers' results."""
        secrets = self.vault.load()
        sources = self._fund_sources()
        try:
            with connect(self._wealthfolio, cfg.wealthfolio_url, secrets.get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                depot = lookthrough.read_depot(wf, self.learned_isins(cfg))
                result = lookthrough.update(self.state, depot, sources, force=force, only=only)
                if cfg.lookthrough_write:
                    try:
                        lookthrough.write_taxonomies(self.state, wf, depot)
                    except WealthfolioError:
                        log.exception("look-through: countries and sectors not written to Wealthfolio")
                try:  # read only unless the service is on (#141)
                    lookthrough.asset_classes(self.state, wf, depot, cfg.lookthrough_write, cfg.asset_class_lookup)
                except WealthfolioError:
                    log.exception("look-through: asset classes not checked")
                self._write_xray_addon(wf, depot, cfg)
            if not depot.positions and not depot.cash:  # Wealthfolio answered nothing: keep what was notified
                return result
            if not self.state.funds():  # no list yet: every ETF would count as one stock, a false alarm
                return result
            found = xray.findings(lookthrough.holdings(self.state, depot), lookthrough.limits(cfg.xray_limits),
                                  depot.base, depot.cash, lookthrough.cost(self.state, depot, cfg).rate)
            new = lookthrough.new_risks(self.state, found)
            if new:
                names = ", ".join(f.name or f.kind for f in new[:5])
                self.notifier(cfg).send("Klumpenrisiko", f"{len(new)} neue Klumpenrisiken in X-Ray: {names}",
                                        link=self.link(cfg, "/lookthrough"), tags="warning")
            return result
        except Exception as e:
            log.exception("look-through not updated")
            lookthrough.record(self.state, error=redact(f"{type(e).__name__}: {e}"))  # shown on X-Ray
            return None
        finally:
            sources.close()

    def _write_xray_addon(self, wf: WealthfolioClient, depot: lookthrough.Depot, cfg: config_mod.Config) -> None:
        """The funds' lists for the X-Ray addon (#146), or their removal once switched off; never fails the pass."""
        key = meta_keys.XRAY_ADDON_STATUS()
        on = cfg.service("xray")
        if not on and not self.state.meta(key):
            return  # never written: nothing to clear
        if on and not depot.positions:
            return  # Wealthfolio answered nothing: keep what the addon reads
        status: dict = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "ok": True}
        try:
            if on:
                result = xray_addon.write(self.state, wf, depot, self.learned_isins(cfg), status["at"])
            else:
                result = xray_addon.clear(self.state, wf)
            status |= {"written": result.written, "deleted": result.deleted, "skipped": result.skipped}
        except StorageUnsupported:
            status |= {"ok": False, "unsupported": True,
                       "error": "Wealthfolio kennt den Addon-Speicher erst ab Version 3.11.0."}
        except Exception as e:
            log.exception("X-Ray addon: lists not written")
            status |= {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}
        if not on and status["ok"]:
            self.state.delete_meta(key)  # cleared: nothing of ours is left in the addon's storage
        else:
            self.state.set_meta(key, json.dumps(status))

    def write_xray_addon(self) -> dict | None:
        """*Jetzt ablegen* / *Aus Wealthfolio entfernen* on the service's page (#151): the lists written now, or
        cleared once the service is off (under the sync lock); the stored status, None once cleared."""
        with run_lock(self.data_dir, wait=5):
            cfg = config_mod.load(self.data_dir)
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                on = cfg.service("xray")
                depot = lookthrough.read_depot(wf, self.learned_isins(cfg)) if on else lookthrough.Depot()
                self._write_xray_addon(wf, depot, cfg)
            return json.loads(self.state.meta(meta_keys.XRAY_ADDON_STATUS()) or "null")

    def write_taxonomies(self) -> lookthrough.Written:
        """*Jetzt eintragen* on the service's page (#151): countries and sectors now (under the sync lock)."""
        with run_lock(self.data_dir, wait=5):
            cfg = config_mod.load(self.data_dir)
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                depot = lookthrough.read_depot(wf, self.learned_isins(cfg))
                written = lookthrough.write_taxonomies(self.state, wf, depot)
                lookthrough.asset_classes(self.state, wf, depot, write=True, lookup=cfg.asset_class_lookup)
                return written

    def update_spending(self) -> bool:
        """*Jetzt ausführen* on the service's page (#151): the tracked accounts now (under the sync lock)."""
        with run_lock(self.data_dir, wait=5):
            return self._update_spending(config_mod.load(self.data_dir))

    def remove_taxonomies(self) -> int:
        """*Aus Wealthfolio entfernen* on the service's page: our countries and sectors out again (under the sync
        lock)."""
        with run_lock(self.data_dir, wait=5):
            cfg = config_mod.load(self.data_dir)
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                return lookthrough.remove_taxonomies(wf)

    def _read_appearance(self, cfg: config_mod.Config) -> None:
        """The font chosen in Wealthfolio, for the UI (appearance.py); read only, never touches a broker's result."""
        if not cfg.wealthfolio_url:
            return
        try:
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                appearance.refresh(self.state, wf)
        except Exception:
            log.exception("Wealthfolio's font not read")

    def _update_spending(self, cfg: config_mod.Config) -> bool:
        """The banks' cash accounts in Wealthfolio's spending tracker (#134); never touches a broker's result.
        False when it failed."""
        if not cfg.service("spending"):  # switched off on *Dienste*: the accounts the sync added leave it
            cfg = replace(cfg, brokers={k: replace(b, spending=False) for k, b in cfg.brokers.items()})
        try:
            with connect(self._wealthfolio, cfg.wealthfolio_url, self.vault.load().get("wealthfolio_password"),
                         cfg.wealthfolio_fingerprint) as wf:
                spending.update(self.state, wf, cfg)
        except Exception:
            log.exception("spending tracker not updated")
            return False
        return True

    def _check_update(self, cfg: config_mod.Config) -> None:
        """Once a day: a newer release is shown on the overview and notified once (updates.py)."""
        version = updates.check(self.state, __version__)
        if version and not cfg.auto_update:  # switched on, the updater installs it and says so itself
            from .container import active

            how = (f"./pull-verified-image.sh --env .env {version} && docker compose pull companion"
                   " && docker compose up -d" if active() else "In der Konsole des Containers: update")
            self.notifier(cfg).send("Update verfügbar", f"Version {version} ist erschienen (installiert: "
                                    f"{__version__}). {how}", link=updates.RELEASES, tags="arrow_up")

    def cleanup(self) -> None:
        """Close runs a dead process left at 'running', unless a sync is in progress."""
        try:
            with run_lock(self.data_dir):
                self.state.abort_stale_runs()
        except AlreadyRunning:
            pass

    def _waiting(self, key: str) -> datetime | None:
        """Until when the broker asked us to wait (#149), None when that is over."""
        until = self.state.meta(meta_keys.RATE_LIMIT(key))
        moment = when(until) if until else None
        return moment if moment and moment > datetime.now(UTC) else None

    def _run_broker(self, cfg: config_mod.Config, key: str) -> BrokerResult:
        label = self.adapters[key].label
        if not self._manual and (until := self._waiting(key)):
            # A scheduled run doesn't ask a broker that limits the requests before the time it named; the last
            # run (status "limited") stays what the overview shows. A fetch the user starts still tries.
            log.info("%s: rate limited until %s, skipped", key, until.isoformat())
            return BrokerResult(key, "skipped", messages=[f"{label}: Abruflimit bis {until.isoformat()}"])
        run_id = self.state.start_run(key)
        result = BrokerResult(key, "ok")
        notifier = self.notifier(cfg)
        try:
            self._sync_broker(cfg, key, result)
            self.state.delete_meta(meta_keys.RATE_LIMIT(key))
        except RateLimited as e:
            # Not "ok": nothing of the run counts as a success, so the next one reads from before it.
            result.status = "limited"
            result.messages.append(str(e))
            if not self.state.meta(meta_keys.RATE_LIMIT(key)):  # once per episode, until a run gets through
                notifier.send(f"{label}: Abruflimit", str(e), link=self.link(cfg, "/broker"), tags="hourglass")
            self.state.set_meta(meta_keys.RATE_LIMIT(key), e.until.astimezone(UTC).isoformat())
        except AuthRequired as e:
            result.status = "needs_auth"
            result.messages.append(str(e))
            notifier.send(f"{label}: Anmeldung nötig", f"{e} Öffne die Seite, um dich anzumelden.",
                          link=self.link(cfg, f"/brokers/{key}/login"), priority="high", tags="key")
        except (AdapterError, WealthfolioError) as e:
            result.status = "error"
            result.messages.append(str(e))
            notifier.send(f"{label}: Abruf fehlgeschlagen", str(e), link=self.link(cfg, "/broker"), priority="high",
                          tags="warning")
        except Exception as e:  # a broken adapter must not stop the others
            log.exception("sync of %s crashed", key)
            result.status = "error"
            result.messages.append(f"Unexpected error: {e}")
            notifier.send(f"{label}: Abruf fehlgeschlagen", f"Unerwarteter Fehler: {e}", link=self.link(cfg, "/broker"),
                          priority="high", tags="warning")
        if cfg.safe_mode or self._safe_now():
            # Safe mode (#85): the login ends with the run, whatever happened - the next one needs the user.
            # Also when it was switched on while this run went on: its session must not outlive the switch.
            self.end_sessions([key])
        if result.status == "ok" and result.failed:
            result.status = "error"
            notifier.send(f"{label}: {result.failed} Buchungen nicht übernommen", "\n".join(result.messages[:5]),
                          link=self.link(cfg, "/broker"), tags="warning")
        self.state.finish_run(run_id, result.status, created=result.created, existing=result.existing,
                              failed=result.failed, unknown=result.unknown, message="\n".join(result.messages))
        log.info("%s: %s, %d created, %d already there, %d failed, %d unknown", key, result.status,
                 result.created, result.existing, result.failed, result.unknown)
        if result.status != "ok" and result.messages:
            log.warning("%s: %s", key, " | ".join(result.messages[:5]))
        return result

    def _safe_now(self) -> bool:
        try:
            return config_mod.load(self.data_dir).safe_mode
        except Exception:  # unreadable (locked meanwhile): end the session to be safe
            return True

    def _sync_broker(self, cfg: config_mod.Config, key: str, result: BrokerResult) -> None:
        """One broker's run, as named steps in this order (ARCHITECTURE.md, *Ablauf eines Laufs*)."""
        run = self._plan_window(cfg, key, result)
        self._phase("login")
        self._fetch(run)
        self._prepare(run)
        self._phase("book")
        with connect(self._wealthfolio, cfg.wealthfolio_url, run.secrets.get("wealthfolio_password"),
                     cfg.wealthfolio_fingerprint) as wf:
            # Before the check against existing activities: it then sees them repaired.
            run.repaired = self._repair_cash_assets(key, wf, run.accounts)
            self._add_before_history(run, wf)
            run.activities = self._activities(wf, run.accounts, run.transactions, run.since if run.full else None,
                                              run.full)
            self._book(run, wf)
            self._phase("check")
            self._relink(run)
            self._compare(cfg, key, run.transactions, run.synced, run.activities, run.full, run.since)
            if run.tax_backfill:
                self._backfill_tax(run, wf)
            if run.own_tax:
                self._backfill_own_tax(run, wf)
            self._learn_assets(run, wf)
            self._check_holdings(run, wf)
        self._commit_flags(run)

    def _plan_window(self, cfg: config_mod.Config, key: str, result: BrokerResult) -> RunContext:
        """The adapter with its session, and from when it reads (full history, backfill, refetch, savings)."""
        bcfg = cfg.brokers[key]
        if self.adapters[key].consent and not bcfg.consented:
            raise AdapterError("Erst in den Broker-Einstellungen den Hinweis zur Schnittstelle bestätigen.")
        if not bcfg.cash_account_id or not bcfg.portfolio_account_id:
            raise AdapterError("No Wealthfolio accounts assigned - finish the setup for this broker.")
        secrets = self.vault.load()
        stored = secrets.get("brokers", {}).get(key, {})
        adapter_cls = self.adapters[key]
        credentials = dict(stored.get("credentials", {}))
        if self._pins.get(key):
            credentials["pin"] = self._pins[key]  # typed at the login, not stored (#85)
        adapter = adapter_cls(credentials, stored.get("session"))
        label = adapter_cls.label

        def user_action(message: str) -> None:
            self._phase("approval")
            self.notifier(cfg).send(f"{label}: Bestätigung nötig", message, link=self.link(cfg, "/broker"),
                                    priority="high", tags="key")

        adapter.on_user_action = user_action
        run = RunContext(cfg=cfg, key=key, bcfg=bcfg, result=result, secrets=secrets, adapter=adapter)
        # Once, for brokers with positions: the whole history, to learn the
        # Wealthfolio asset of every ISIN (brokersync.assets) for the check.
        run.backfill = adapter_cls.reports_positions and not self.state.flag(meta_keys.ASSETS_LEARNED(key))
        run.full = adapter_cls.full_history
        # "Ab Startdatum neu abrufen" in the broker settings: once from the start date again.
        run.refetch = not run.full and self.state.flag(refetch_flag(key))
        run.own_tax = adapter_cls.own_tax_backfill and not self.state.flag(meta_keys.OWN_DIVIDEND_TAX(key))
        run.since = (self._start(bcfg) if run.full or run.backfill or run.refetch or run.own_tax
                     else self._since(key, bcfg))
        # The savings account (Tagesgeld), if the user assigned one: read from the start date until a run
        # read it without failures, then with the cash account.
        run.savings = bcfg.savings_account_id if adapter_cls.savings_label else ""
        adapter.with_savings = bool(run.savings)
        run.savings_new = bool(run.savings) and self.state.meta(savings_flag(key)) != run.savings
        adapter.savings_since = self._start(bcfg) if run.savings_new else run.since
        # Before this run books anything: what the comparison with Wealthfolio checks.
        run.synced = self.state.synced(key)
        return run

    def _fetch(self, run: RunContext) -> None:
        """Log in and read transactions, statements, opening positions, cash and positions."""
        adapter, key = run.adapter, run.key
        try:
            adapter.login()
            self._phase("fetch")
            # Once per broker: details also for transactions found in an import, for the tax of their
            # dividends (Scalable's CSV export has none) - see _add_dividend_tax.
            run.tax_backfill = not self.state.flag(tax_flag(key))
            adapter.known_ids = {t for t, (status, _) in run.synced.items()
                                 if not (run.tax_backfill and status == "existing")}
            stored = self.state.meta(meta_keys.HISTORY_START(key))
            adapter.history_read = date.fromisoformat(stored) if stored else None
            adapter.full_giro = bool(adapter.giro_with_spending and run.bcfg.spending)
            done = {t: history_mod.completion(t, c) for (_, t), c in self.state.completions(key).items()}
            # A completed unknown booking replaces the adapter's own (same id).
            run.transactions = ([t for t in adapter.get_transactions(run.since) if t.id not in done]
                                + list(done.values()) + self._openings(key))
            run.history = self._history_start(key, adapter, run.bcfg)
            run.transactions += self._statements(key, adapter, run.history)
            run.cash = self._broker_cash(key, adapter)
            run.positions = self._broker_positions(key, adapter) if type(adapter).reports_positions else None
        finally:
            # Keep whatever session the adapter has, even half-way through a login - and if closing fails.
            try:
                adapter.close()
            finally:
                self.vault.store_session(key, adapter)

    def _prepare(self, run: RunContext) -> None:
        """What is new, unknown events reported, accounts, mappings, patterns, savings transfer pairs."""
        cfg, key = run.cfg, run.key
        todo = [t for t in run.transactions if t.id not in run.synced]
        log.info("%s: %d transactions from the broker since %s, %d not synced yet", key, len(run.transactions),
                 run.since.date().isoformat() if run.since else "the beginning", len(todo))
        self._report_unknown(cfg, key, [t for t in todo if t.kind == Kind.UNKNOWN], run.result)
        todo = [t for t in todo if t.kind != Kind.UNKNOWN]

        run.accounts = Accounts(run.bcfg.cash_account_id, run.bcfg.portfolio_account_id, run.savings)
        # The user's mappings win over the assets learned from earlier activities.
        run.mappings = assets_mod.mappings(self.state, key)
        run.mappings.update({isin: SecurityMapping(m.get("symbol") or isin, m.get("exchangeMic"), m.get("name"))
                             for isin, m in cfg.security_mappings.items()})
        run.patterns = [TransferPattern.from_config(p) for p in cfg.transfer_patterns]
        # The savings side of a transfer between the cash and the savings account: booked with its cash side
        # (one transfer pair) - unless that was booked on its own before the savings account was read.
        run.paired = {t.id: t.pair for t in todo if t.pair and t.pair not in run.synced}
        run.todo = [t if t.id in run.paired or not t.pair else replace(t, pair="") for t in todo]

    def _add_before_history(self, run: RunContext, wf: WealthfolioClient) -> None:
        """Trades imported from statements older than the bank account's history: their money."""
        before = self._before_history(wf, run.key, run.accounts, run.history, run.synced)
        run.transactions += before
        run.todo += [t for t in before if t.id not in run.synced]

    def _book(self, run: RunContext, wf: WealthfolioClient) -> None:
        """Create what is missing; savings sides of transfer pairs are marked with their cash side."""
        key = run.key
        if run.todo:
            existing = ExistingIndex(run.activities)
            # Bank settlements last: the securities side booked in this run (a depot statement) is there then.
            for tx in sorted(run.todo, key=lambda t: (t.kind == Kind.SECURITIES_CASH, t.datetime)):
                if tx.id in run.paired:
                    continue  # booked with its cash side, below
                if tx.kind == Kind.SECURITIES_CASH:
                    if not self._settle_securities_cash(key, tx, run.accounts, existing, run.result):
                        run.unmatched.append(tx)
                    continue
                self._sync_tx(wf, key, tx, run.accounts, run.mappings, run.patterns, existing, run.result)
        if run.paired:
            now_synced = self.state.synced(key)
            for tx_id, pair in run.paired.items():
                if pair in now_synced:
                    self.state.mark(key, tx_id, "existing", now_synced[pair][1])

    def _backfill_tax(self, run: RunContext, wf: WealthfolioClient) -> None:
        """Once per broker: the tax of dividends matched with an import before (see _add_dividend_tax)."""
        by_id = {a["id"]: a for a in run.activities}
        added, tax_errors = 0, 0
        for tx in run.transactions:
            status, ids = run.synced.get(tx.id, ("", []))
            if status == "existing" and ids and ids[0] in by_id:
                try:
                    added += self._add_dividend_tax(wf, tx, by_id[ids[0]])
                except WealthfolioError as e:
                    tax_errors += 1
                    log.info("%s: tax not added to %s: %s", run.key, tx.id, e)
        if added:
            log.info("%s: added the tax to %d imported dividends", run.key, added)
        if not tax_errors and not run.result.failed and not run.adapter.incomplete:
            self.state.set_flag(tax_flag(run.key))

    def _backfill_own_tax(self, run: RunContext, wf: WealthfolioClient) -> None:
        """Once per broker (``own_tax_backfill``): the tax on the sync's own dividends booked before the adapter
        could read it - only the ``tax`` field, so amount and comment (the fingerprint) stay."""
        by_id = {a["id"]: a for a in run.activities}
        added, errors = 0, 0
        for tx in run.transactions:
            status, ids = run.synced.get(tx.id, ("", []))
            if status != "imported":
                continue
            for activity in (by_id[i] for i in ids if i in by_id):
                try:
                    added += self._add_dividend_tax(wf, tx, activity, own=tx_ref(run.key, tx))
                except WealthfolioError as e:
                    errors += 1
                    log.info("%s: tax not added to %s: %s", run.key, tx.id, e)
        if added:
            log.info("%s: added the tax to %d dividends booked before", run.key, added)
        if not errors and not run.result.failed:
            self.state.set_flag(meta_keys.OWN_DIVIDEND_TAX(run.key))

    def _learn_assets(self, run: RunContext, wf: WealthfolioClient) -> None:
        """The Wealthfolio asset of every ISIN, from the sync's activities and, for positions, from imports."""
        key, cfg = run.key, run.cfg
        try:
            assets_mod.learn(self.state, key, run.transactions, wf, run.accounts)
            # Positions whose asset is unknown, or didn't fit last time: maybe only an import booked
            # their trades.
            deviating = {d["key"] for d in (self.state.reconcile(key) or {}).get("deviations", [])
                         if d.get("kind") == "position"}
            # Not those the user assigned on the page Wertpapiere: the check finds them by symbol.
            check = {p.isin for p in run.positions or [] if p.isin and p.isin not in cfg.security_mappings
                     and (p.isin not in self.state.assets(key) or p.isin in deviating)}
            if check:
                learned = assets_mod.learn_from_imports(self.state, key, wf, run.accounts, check,
                                                        recheck=check & deviating)
                if learned:
                    log.info("%s: learned the Wealthfolio asset of %d position(s) from imported trades", key,
                             learned)
            if run.backfill:
                self.state.set_flag(meta_keys.ASSETS_LEARNED(key))
        except WealthfolioError as e:
            log.info("%s: assets not learned: %s", key, e)

    def _check_holdings(self, run: RunContext, wf: WealthfolioClient) -> None:
        """Broker cash and positions vs. Wealthfolio's holdings - only after a run without failures."""
        if run.cash is None or run.result.failed:
            return
        if run.result.created or run.repaired:
            time.sleep(self.recalc_wait)  # Wealthfolio recalculates holdings in the background
        self._reconcile(run.cfg, run.key, wf, run.accounts, run.cash, run.positions, run.mappings)

    def _commit_flags(self, run: RunContext) -> None:
        """Report unmatched settlements, resolve vanished unknowns, and clear the one-off flags of a good run."""
        key = run.key
        self._report_unmatched(run.cfg, key, run.unmatched, run.result)
        if run.full:
            self._resolve_vanished_unknowns(key, run.transactions, run.since)
        if run.refetch and not run.result.failed:
            self.state.delete_meta(refetch_flag(key))
        if run.savings_new and not run.result.failed:
            self.state.set_meta(savings_flag(key), run.savings)

    @staticmethod
    def _activities(wf: WealthfolioClient, accounts: Accounts, transactions: list[Transaction],
                    since: datetime | None, full: bool) -> list[dict]:
        """The broker's accounts' activities around the fetched transactions - for a full history all of them."""
        # Wide enough for a settlement a few days after the trade (SECURITIES_CASH).
        margin = timedelta(days=SETTLEMENT_DAYS + 1)
        if full:
            first = (since - margin).date() if since else date(1970, 1, 1)
            last = date(2100, 1, 1)
        elif transactions:
            first = (min(t.datetime for t in transactions) - margin).date()
            last = (max(t.datetime for t in transactions) + margin).date()
        else:
            return []
        return wf.search_activities(accounts.ids, first, last)

    def _relink(self, run: RunContext) -> None:
        """A transaction booked before whose stored activities are gone, while Wealthfolio holds the same booking
        from an import (matched by ``ExistingIndex``, like a new one): it is booked, linked to that activity, not
        missing. Happens when Wealthfolio rejected every leg as a duplicate of the import (nothing to remember)
        or an import was booked anew (new ids)."""
        tagged: dict[str, set[str]] = {}
        for a in run.activities:
            m = coverage.SYNC_REF.search(a.get("comment") or "")
            if m and m.group(1) == run.key:
                tagged.setdefault(m.group(2), set()).add(a["id"])
        present = {a["id"] for a in run.activities}
        index = ExistingIndex(run.activities)
        # Never another transaction's activity: what the state links and what carries a [SYNC] reference.
        index.used |= {i for _, ids in run.synced.values() for i in ids} | {i for ids in tagged.values() for i in ids}
        for tx in run.transactions:
            row = run.synced.get(tx.id)
            if (not row or row[0] == "ignored" or tx.kind == Kind.UNKNOWN or tx.id in tagged
                    or any(i in present for i in row[1] if i)):
                continue
            try:
                payloads = to_activities(tx, run.key, run.accounts, run.mappings, run.patterns)
            except MappingError:
                continue
            match = index.find(tx_ref(run.key, tx), payloads)
            if match:
                self.state.mark(run.key, tx.id, "existing", [match])
                run.synced[tx.id] = ("existing", [match])

    def _compare(self, cfg: config_mod.Config, key: str, transactions: list[Transaction],
                 synced: dict[str, tuple[str, list[str]]], activities: list[dict], full: bool,
                 since: datetime | None) -> None:
        """Transactions synced before that are gone from Wealthfolio, and the reverse (brokersync.coverage)."""
        gaps = coverage.check(key, transactions, synced, activities, full_history=full, since=since)
        before = {(g["kind"], g["ref"]) for g in self.state.gaps(key)}
        self.state.set_gaps(key, [g.as_dict() for g in gaps])
        new = [g for g in gaps if g.kind != coverage.IGNORED and (g.kind, g.ref) not in before]
        if not new:
            return
        log.info("%s: %d transactions missing in Wealthfolio, %d activities no longer at the broker", key,
                 sum(g.kind != coverage.ORPHAN for g in new), sum(g.kind == coverage.ORPHAN for g in new))
        label = self.adapters[key].label
        lines = []
        missing = [g for g in new if g.kind != coverage.ORPHAN]
        orphans = [g for g in new if g.kind == coverage.ORPHAN]
        if missing:
            lines.append(f"{len(missing)} schon übernommene Buchung(en) fehlen in Wealthfolio (gelöscht?).")
        if orphans:
            lines.append(f"{len(orphans)} Buchung(en) in Wealthfolio gibt es bei {label} nicht (mehr), "
                         "z. B. storniert.")
        self.notifier(cfg).send(f"{label}: Abweichung zu Wealthfolio",
                                " ".join(lines) + " Auf der Seite Prüfung entscheidest du, was passiert.",
                                link=self.link(cfg, "/check"), tags="mag")

    def _resolve_vanished_unknowns(self, key: str, transactions: list[Transaction], since: datetime | None) -> None:
        """A full history is the whole truth: an unknown event the broker no longer lists as one is settled
        (e.g. a cancellation a newer version nets out with its original)."""
        still = {t.id for t in transactions if t.kind == Kind.UNKNOWN}
        for e in self.state.unknown_events(key):
            if e["raw_type"] == UNMATCHED_SECURITIES or e["tx_id"] in still:
                continue
            if since and when(e["occurred_at"]) < since:
                continue
            self.state.resolve_unknown(key, e["tx_id"])

    @staticmethod
    def _add_dividend_tax(wf: WealthfolioClient, tx: Transaction, activity: dict, own: str = "") -> int:
        """Put the broker's tax on a dividend an import booked without it (Scalable's CSV has none).

        Only the ``tax`` field changes: amount (net) and comment stay, and tax is not part of
        Wealthfolio's duplicate fingerprint, so a re-import still finds the activity. The
        sync's own activities and dividends that carry a tax already are left alone - unless ``own`` names the
        sync's reference of ``tx``: then only the activity carrying it (``_backfill_own_tax``).
        """
        if tx.kind != Kind.DIVIDEND or tx.tax <= 0 or activity.get("activityType") != "DIVIDEND":
            return 0
        comment = activity.get("comment") or ""
        if (own not in comment) if own else ("[SYNC " in comment):
            return 0
        try:
            if Decimal(str(activity.get("tax") or 0)) != 0:
                return 0
        except ArithmeticError:
            return 0
        payload = repair_mod.payload(activity)
        payload.pop("asset", None)  # keep the asset
        payload["tax"] = format(tx.tax.normalize(), "f")
        wf.update_activity(payload)
        return 1

    def _repair_cash_assets(self, key: str, wf: WealthfolioClient, accounts: Accounts) -> int:
        """Once per broker: the cash activities older versions created with a $CASH asset (brokersync.repair).

        Until they are repaired nothing is booked: the sync would re-send a
        transaction of theirs without the asset, which Wealthfolio doesn't
        recognise as the same activity.
        """
        flag = meta_keys.CASH_ASSETS_REPAIRED(key)
        if self.state.flag(flag):
            return 0
        updated, errors = repair_mod.repair(wf, key, accounts)
        if errors:
            raise WealthfolioError(
                f"{len(errors)} Überträge aus früheren Versionen konnten nicht korrigiert werden (nichts gebucht, "
                f"der nächste Abruf versucht es erneut): " + "; ".join(errors[:3]))
        self.state.set_flag(flag)
        return updated

    def _broker_cash(self, key: str, adapter: BrokerAdapter) -> list[CashBalance] | None:
        # The broker's balance, shown next to the run and compared with
        # Wealthfolio's. Not worth failing the run for.
        try:
            balances = adapter.get_cash()
        except (AdapterError, AuthRequired, NotImplementedError) as e:
            log.info("%s: no balance: %s", key, e)
            return None
        self.state.set_balances(key, [(b.currency, str(b.amount), b.account) for b in balances])
        return balances

    def _broker_positions(self, key: str, adapter: BrokerAdapter) -> list[Position] | None:
        try:
            positions = adapter.get_positions()
        except (AdapterError, AuthRequired, NotImplementedError) as e:
            log.info("%s: no positions: %s", key, e)
            return None
        # For the page Prüfung: what a position missing in Wealthfolio is pre-filled with.
        self.state.set_meta(meta_keys.POSITIONS(key), json.dumps(
            [{"isin": p.isin, "name": p.name, "shares": str(p.shares), "currency": p.currency,
              "cost": str(p.cost) if p.cost is not None else ""} for p in positions]))
        return positions

    def _history_start(self, key: str, adapter: BrokerAdapter, bcfg: config_mod.BrokerConfig) -> date | None:
        """The earliest day of the bank account's history the sync ever read (``meta history-start:<broker>``),
        for adapters that read one. Versions before 0.11.0 kept it only for statements: without it, the first
        successful run tells - it read from the start date, else NO_TAN_DAYS back."""
        if adapter.history_start is None:
            return None
        stored = self.state.meta(meta_keys.HISTORY_START(key))
        earliest = date.fromisoformat(stored) if stored else None
        if earliest is None:
            first = self.state.first_success(key)
            if bcfg.start_date:
                earliest = date.fromisoformat(bcfg.start_date)
            elif first:
                earliest = first.astimezone(BERLIN).date() - timedelta(days=NO_TAN_DAYS)
        earliest = min(d for d in (earliest, adapter.history_start) if d)
        if earliest.isoformat() != stored:
            self.state.set_meta(meta_keys.HISTORY_START(key), earliest.isoformat())
        return earliest

    def _statements(self, key: str, adapter: BrokerAdapter, history: date | None) -> list[Transaction]:
        """Transactions from statements the user uploaded (Deutsche Bank depot CSV). Rows older than the
        earliest day of the bank account's history the sync ever read keep their money off the cash account."""
        if not type(adapter).statement_label:
            return []
        rows = self.state.statement_rows(key)
        return type(adapter).statement_transactions(rows, history) if rows else []

    def _before_history(self, wf: WealthfolioClient, key: str, accounts: Accounts, history: date | None,
                        synced: dict[str, tuple[str, list[str]]]) -> list[Transaction]:
        """Trades a CSV/PDF import booked before the bank account's history the sync read: their money never
        showed up there. Like external_cash: a purchase gets a deposit of its cost just before its transfer to
        the depot, a sale a withdrawal of its proceeds just after; dividends keep their money. Not for transfers
        the sync matched to a bank booking (those were read), nor for the sync's own activities."""
        if history is None:
            return []
        try:
            activities = wf.search_activities([accounts.cash, accounts.portfolio], date(1970, 1, 1),
                                              history - timedelta(days=1))
        except WealthfolioError as e:
            log.info("%s: imports before the bank history not checked: %s", key, e)
            return []
        out = history_mod.money_before(activities, accounts, history, synced)
        new = [t for t in out if t.id not in synced]
        if new:
            log.info("%s: %d imported trade(s) before the bank history (%s) get their money", key, len(new),
                     history.isoformat())
        return out

    def _openings(self, key: str) -> list[Transaction]:
        """Opening positions the user entered (page Prüfung): a buy on that day, its cost deposited first."""
        return history_mod.openings(self.state.openings(key))

    def _reconcile(self, cfg: config_mod.Config, key: str, wf: WealthfolioClient, accounts: Accounts,
                   cash: list[CashBalance], positions: list[Position] | None,
                   mappings: dict[str, SecurityMapping]) -> None:
        try:
            wf_cash = wf.holdings(accounts.cash)
            wf_portfolio = wf.holdings(accounts.portfolio)
            wf_savings = wf.holdings(accounts.savings) if accounts.savings else None
        except WealthfolioError as e:
            log.info("%s: no holdings for the check: %s", key, e)
            return
        # Masked as stored (SEC-04), so "the same as last time" compares like with like.
        deviations = [masked(d.as_dict()) for d in compare(cash, positions, wf_cash, wf_portfolio,
                                                   {i: m.symbol for i, m in mappings.items()},
                                                   {i: a["asset_id"] for i, a in self.state.assets(key).items()},
                                                   wf_savings=wf_savings,
                                                   savings_label=self.adapters[key].savings_label)]
        assets = self.state.assets(key)
        for d in deviations:
            if d["kind"] == "position" and d["key"] in assets:
                log.info("%s: position %s deviates; its Wealthfolio asset: %s (%s)", key, d["key"],
                         assets[d["key"]]["asset_id"], assets[d["key"]]["symbol"])
        previous = self.state.reconcile(key) or {}
        # Report only what shows up twice in a row (not a recalculation in
        # progress), and each set of deviations only once.
        persistent = [d for d in deviations if d in previous.get("deviations", [])]
        if persistent and persistent != previous.get("notified"):
            lines = [f"{d['name']}: {self.adapters[key].label} {d['broker']}, Wealthfolio {d['wealthfolio']}"
                     for d in persistent[:6]]
            self.notifier(cfg).send(f"{self.adapters[key].label}: Bestand weicht ab", "\n".join(lines),
                                    link=self.link(cfg, "/broker"), tags="scales")
        self.state.set_reconcile(key, deviations, notified=persistent)

    def _settle_securities_cash(self, key: str, tx: Transaction, accounts: Accounts, existing: ExistingIndex,
                                result: BrokerResult) -> bool:
        """A bank booking for a securities trade/payout: there if the securities side booked its transfer."""
        signed = tx.signed if tx.signed is not None else -tx.net
        match = existing.find_settlement(accounts.cash, signed, tx.datetime, SETTLEMENT_DAYS)
        if not match:
            return False
        self.state.mark(key, tx.id, "existing", [match])
        self.state.resolve_unknown(key, tx.id)
        result.existing += 1
        return True

    def _report_unmatched(self, cfg: config_mod.Config, key: str, events: list[Transaction],
                          result: BrokerResult) -> None:
        # Not marked as synced: once the PDF statement is imported, the next run finds it.
        new = [t for t in events if self.state.add_unknown(
            key, t.id, UNMATCHED_SECURITIES, t.datetime.isoformat(),
            {"betrag": str(t.signed if t.signed is not None else -t.net), "buchungstext": t.label,
             "verwendungszweck": t.text[:200]})]
        result.unknown += len(events)
        if new:
            total = ", ".join(f"{t.signed if t.signed is not None else -t.net} {t.currency}" for t in new[:5])
            self.notifier(cfg).send(
                f"{self.adapters[key].label}: Wertpapier-Buchung ohne Gegenstück",
                f"{len(new)} Buchung(en) ({total}) gehören zu Wertpapiergeschäften, die noch nicht in Wealthfolio "
                "sind. Importiere die Wertpapierabrechnung (PDF) in Wealthfolio; danach erkennt der nächste Abruf "
                "sie.",
                link=self.link(cfg, "/unknown"), tags="question",
            )

    def _since(self, key: str, bcfg: config_mod.BrokerConfig) -> datetime | None:
        last = self.state.last_success(key)
        if last:
            since = last - OVERLAP
            # Unmatched securities settlements stay in the window until the
            # securities side (PDF import) books them and the check finds them.
            open_since = self.state.oldest_open(key, UNMATCHED_SECURITIES)
            if open_since and open_since - timedelta(days=1) < since:
                since = open_since - timedelta(days=1)
            return since
        return self._start(bcfg)

    @staticmethod
    def _start(bcfg: config_mod.BrokerConfig) -> datetime | None:
        if bcfg.start_date:
            # The day starts at midnight German time, like everything else the user sees (#108).
            return datetime.fromisoformat(bcfg.start_date).replace(tzinfo=BERLIN)
        return None

    def _sync_tx(self, wf: WealthfolioClient, key: str, tx: Transaction, accounts: Accounts,
                 mappings: dict[str, SecurityMapping], patterns: list[TransferPattern], existing: ExistingIndex,
                 result: BrokerResult) -> None:
        try:
            payloads = to_activities(tx, key, accounts, mappings, patterns)
        except MappingError as e:
            result.failed += 1
            result.messages.append(f"{tx.datetime.date()} {tx.label or tx.kind} {tx.name}: {e}")
            return
        match = existing.find(tx_ref(key, tx), payloads)
        if match:
            self.state.mark(key, tx.id, "existing", [match])
            result.existing += 1
            imported = next((e for e in existing.existing if e["id"] == match), None)
            if imported is not None:
                try:
                    self._add_dividend_tax(wf, tx, imported)
                except WealthfolioError as e:
                    log.info("%s: tax not added to %s: %s", key, tx.id, e)
            return
        ids: list[str] = []
        new = 0
        ref = tx_ref(key, tx).strip()
        try:
            for p in payloads:
                created = wf.create_activity(p)
                if isinstance(created, DuplicateActivity):
                    # This leg was created by an earlier, interrupted run: keep its id (#134 M1).
                    ids += [e["id"] for e in existing.existing
                            if ref in (e.get("comment") or "") and e["id"] not in ids]
                    continue
                new += 1
                ids.append(created.get("id", ""))
                # A bank settlement later in this run (SECURITIES_CASH) can match the cash leg of a trade
                # or payout booked now - not the legs of a transfer to an own account.
                if tx.kind in (Kind.BUY, Kind.SELL, Kind.DIVIDEND) and not tx.external_cash:
                    existing.existing.append({"id": created.get("id", ""), "accountId": p["accountId"],
                                              "activityType": p["activityType"], "amount": p.get("amount"),
                                              "date": p["activityDate"], "comment": p.get("comment", "")})
        except WealthfolioError as e:
            # Not marked: the next run retries; legs created now come back as duplicates.
            result.failed += 1
            result.messages.append(f"{tx.datetime.date()} {tx.label or tx.kind} {tx.name}: {e}")
            return
        self.state.mark(key, tx.id, "imported", ids)
        if new:
            result.created += 1
        else:
            result.existing += 1

    def _report_unknown(self, cfg: config_mod.Config, key: str, events: list[Transaction],
                        result: BrokerResult) -> None:
        new = [t for t in events
               if self.state.add_unknown(key, t.id, t.raw_type or t.label, t.datetime.isoformat(), t.raw)]
        result.unknown = len(events)
        if new:
            types = ", ".join(sorted({t.raw_type or t.label for t in new}))
            self.notifier(cfg).send(
                f"{self.adapters[key].label}: {len(new)} unbekannte Buchungen",
                f"Nicht übernommen, bitte prüfen und ggf. von Hand eintragen: {types}",
                link=self.link(cfg, "/unknown"), tags="question",
            )
