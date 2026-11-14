"""Settings, encrypted like everything else (``brokersync.crypto``).

- ``<data>/config.enc`` (data tier): Wealthfolio address, brokers, accounts,
  security mappings, transfer patterns (own IBANs), logos on/off.
- ``<data>/notify.enc`` (host tier): ntfy server, topic and token and the UI's
  address for links - so a locked service can still say it is locked.

Users never edit these files: the web UI writes them. ``config.json`` of
versions before 0.9.0 (and the ntfy token from the vault) are moved in on the
first read. Passwords and broker credentials live in the vault.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import crypto, redact

DEFAULT_DATA_DIR = "/opt/wealthfolio-companion/data"


def data_dir() -> Path:
    return Path(os.environ.get("COMPANION_DATA", DEFAULT_DATA_DIR))


@dataclass
class BrokerConfig:
    enabled: bool = False
    cash_account_id: str = ""
    portfolio_account_id: str = ""
    # Optional: the bank's savings account (Tagesgeld), for adapters with ``savings_label``.
    savings_account_id: str = ""
    # First run fetches from here (ISO date); later runs from the last success.
    start_date: str = ""
    # The adapter's ``consent`` was agreed to (#112); without it no login starts.
    consented: bool = False
    # The cash account goes into Wealthfolio's spending tracker (#134).
    spending: bool = False
    # What the user calls a bank set up in one of the fixed slots (fints1…3, psd2-1…3); empty: the adapter's label.
    label: str = ""


@dataclass
class Config:
    wealthfolio_url: str = "http://localhost:8080"
    # SHA-256 of Wealthfolio's own certificate, trusted instead of a CA (#95); empty: the usual CAs.
    wealthfolio_fingerprint: str = ""
    # Address of this service's UI as the user reaches it; used for links in
    # notifications.
    public_url: str = ""
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    # Details (amounts, positions, error texts) in notifications; off: only what happened and a link.
    ntfy_details: bool = False
    brokers: dict[str, BrokerConfig] = field(default_factory=dict)
    # ISIN -> {"symbol", "exchangeMic", "name"}: the user's own ticker for a security.
    security_mappings: dict[str, dict] = field(default_factory=dict)
    # Outbound bank transfers to own accounts:
    # [{"label", "iban", "keyword", "destinationAccountId"}]. A matched outbound
    # transfer is booked as TRANSFER_OUT (plus TRANSFER_IN on the destination
    # account, if one is set) instead of a withdrawal.
    transfer_patterns: list[dict] = field(default_factory=list)
    # Logos from logo.dev by ISIN after every run (brokersync.logos); the key lives in the vault.
    logos_enabled: bool = False
    # Unused since X-Ray fetches iShares' lists always (#144); kept so configs saved before still load.
    lookthrough_enabled: bool = False
    # Write the funds' countries and sectors into Wealthfolio's taxonomies (replaces only Yahoo's AUTO values).
    lookthrough_write: bool = False
    # With the taxonomies service on: ask ESMA's FIRDS (the ISIN only) for the asset class of a position that has none
    # (brokersync.firds, #185).
    asset_class_lookup: bool = False
    xray_limits: dict[str, str] = field(default_factory=dict)
    # Per broker key its fees as text (#142, costs.Model's fields: fixed, percent, spread, custody); empty: unknown.
    cost_models: dict[str, dict[str, str]] = field(default_factory=dict)
    # "auto": sessions are kept, the timer fetches. "safe": every fetch needs a fresh login in the UI, the
    # session is ended after it, the timer only reminds (#85). For all brokers.
    sync_mode: str = "auto"
    # Page Updates: the container installs a newer release by itself (deploy/companion-autoupdate, root), asking
    # GitHub every ``update_interval`` seconds. In the host tier, so it works while the data is locked.
    auto_update: bool = False
    update_interval: int = 60
    # Days a release must have been published before the automatic update installs it (0 = at once).
    update_delay: int = 0
    # Page Darstellung: "de" / "en"; the font ("" = the one chosen in Wealthfolio, "sans", "serif", "mono") and the
    # colours ("system" = the device's, "light", "dark"); the header's symbol turns them round until logout (#123).
    # Not "ui_theme": a fixed scheme stored under that name before #123 stays ignored.
    ui_language: str = "de"
    ui_font: str = ""
    ui_scheme: str = "system"
    # Page Dienste (#145): what the sync writes into Wealthfolio besides the flags above (``SERVICES``). None: set
    # up before the page existed - all of it on, as it ran; a new install starts with {} (``security.migrate``).
    services: dict[str, bool] | None = None

    def service(self, key: str) -> bool:
        """Whether the service ``key`` of the page *Dienste* is on."""
        if key in _FLAGS:
            return bool(getattr(self, _FLAGS[key]))
        if self.services is None:
            return key not in NEW_SERVICES  # set up before the page: what ran stays on, what is new starts off
        return self.services.get(key, False)

    def set_service(self, key: str, on: bool) -> None:
        if key in _FLAGS:
            setattr(self, _FLAGS[key], on)
            return
        services = ({k: k not in NEW_SERVICES for k in SERVICES} if self.services is None
                    else dict(self.services))
        services[key] = on
        self.services = services

    @property
    def safe_mode(self) -> bool:
        return self.sync_mode == "safe"

    def broker(self, key: str) -> BrokerConfig:
        return self.brokers.setdefault(key, BrokerConfig())


# The services of the page *Dienste* without a flag of their own (#145), and those that have one.
SERVICES = ("sync", "isin", "spending", "xray")
# Services that came after the page: off for an installation set up before it too.
NEW_SERVICES = ("xray",)
_FLAGS = {"taxonomies": "lookthrough_write", "logos": "logos_enabled"}

# The host tier (notify.enc): readable while the data is locked - ntfy and the automatic update.
NOTIFY_FIELDS = ("public_url", "ntfy_server", "ntfy_topic", "ntfy_token", "ntfy_details", "auto_update",
                 "update_interval", "update_delay")


def _read(path: Path, key: bytes, purpose: str) -> dict:
    raw = json.loads(crypto.unseal(key, path.read_bytes(), purpose)) if path.exists() else {}
    redact.register(raw.get("ntfy_token", ""))  # the one secret outside the vault: masked in the log too
    return raw


def load_notify(directory: Path) -> Config:
    """Only the ntfy settings - readable while the service is locked."""
    d = Path(directory)
    if not (d / "notify.enc").exists() and (d / "config.json").exists():
        raw = json.loads((d / "config.json").read_text())
        return Config(**{k: raw[k] for k in NOTIFY_FIELDS if k in raw})
    raw = _read(d / "notify.enc", crypto.host_key(d), "notify")
    return Config(**{k: raw[k] for k in NOTIFY_FIELDS if k in raw})


def load(directory: Path) -> Config:
    d = Path(directory)
    if (d / "config.json").exists():
        _migrate(d)
    raw = _read(d / "config.enc", crypto.data_key(d), "config")
    raw.update(_read(d / "notify.enc", crypto.host_key(d), "notify"))
    brokers = {k: BrokerConfig(**v) for k, v in raw.pop("brokers", {}).items()}
    known = Config.__dataclass_fields__
    return Config(brokers=brokers, **{k: v for k, v in raw.items() if k in known})


def sealed(cfg: Config, data_key: bytes, host_key: bytes) -> tuple[bytes, bytes]:
    """config.enc and notify.enc for ``cfg``."""
    data = asdict(cfg)
    notify = {k: data.pop(k) for k in NOTIFY_FIELDS}
    return (crypto.seal(data_key, json.dumps(data).encode(), "config"),
            crypto.seal(host_key, json.dumps(notify).encode(), "notify"))


def save(directory: Path, cfg: Config, key: bytes | None = None) -> None:
    d = Path(directory)
    # Under the lock a passphrase change holds: the key is taken after it, so a save can't seal the config
    # with a key that was just replaced.
    with crypto.file_lock(d, "secrets.lock"):
        _write(d, cfg, key)


def update(directory: Path, change: Callable[[Config], None]) -> Config:
    """Read, change and write back under the lock (#117): two changes at once (two tabs, the timer's clean-up)
    both stay. Check the user's input before, ``change`` only applies it."""
    d = Path(directory)
    if (d / "config.json").exists():
        _migrate(d)  # takes the lock itself (save)
    with crypto.file_lock(d, "secrets.lock"):
        cfg = load(d)
        change(cfg)
        _write(d, cfg)
        return cfg


def _write(d: Path, cfg: Config, key: bytes | None = None) -> None:
    config_blob, notify_blob = sealed(cfg, key or crypto.data_key(d), crypto.host_key(d))
    crypto.write_atomic(d / "config.enc", config_blob)
    crypto.write_atomic(d / "notify.enc", notify_blob)


def _migrate(d: Path) -> None:
    """config.json (before 0.9.0) → config.enc + notify.enc; the ntfy token comes out of the vault."""
    from .vault import Vault

    with crypto.file_lock(d, "config.lock"):
        if not (d / "config.json").exists():
            return
        raw = json.loads((d / "config.json").read_text())
        brokers = {k: BrokerConfig(**v) for k, v in raw.pop("brokers", {}).items()}
        known = Config.__dataclass_fields__
        cfg = Config(brokers=brokers, **{k: v for k, v in raw.items() if k in known})
        vault = Vault(d)
        cfg.ntfy_token = cfg.ntfy_token or vault.load().get("ntfy_token") or ""
        save(d, cfg)
        if "ntfy_token" in vault.load():
            vault.update(lambda v: v.pop("ntfy_token", None))
        (d / "config.json").unlink()
