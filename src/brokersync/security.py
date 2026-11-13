"""How the service protects what it stores: the checks on the page Sicherheit,
the master passphrase (set, change, remove) and the migration of data from
versions before 0.9.0 (``wealthfolio-companion migrate``, run by ``deploy/setup.sh``).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import crypto, sandbox
from . import state as state_mod
from .sync import run_lock
from .vault import Vault
from .web.i18n import N_

PLAINTEXT_LEFTOVERS = ("config.json", "secret.key")


@dataclass
class Check:
    """One line on the page *Sicherheit*. ``label`` and ``detail`` are UI text keys (German, ``web/i18n.py``,
    translated on the page); ``detail``'s placeholders are filled from ``params``, ``text`` is the German line."""
    label: str
    ok: bool
    detail: str
    warn: bool = False  # not a problem, but worth knowing
    params: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.detail.format(**self.params) if self.params else self.detail


KEY_DETAILS = {
    "systemd-creds-tpm2": N_("Mit systemd-creds und dem TPM verschlüsselt außerhalb von data/; eine Kopie des "
                             "Containers allein öffnet ihn nicht."),
    "systemd-creds": N_("Mit systemd-creds verschlüsselt außerhalb von data/, mit dem Host-Secret des Containers "
                        "(kein TPM); der Dienst bekommt ihn nur beim Start."),
    "systemd": N_("Außerhalb von data/, nur für root lesbar; der Dienst bekommt ihn beim Start (systemd-creds "
                  "konnte hier nicht verschlüsseln)."),
    "file": N_("Aus COMPANION_KEY_FILE."),
    "container": N_("Als Secret des Containers außerhalb von data/ (kein TPM): Wer das Volume und das Secret kopiert, "
                    "kann die Daten öffnen - dagegen hilft die Master-Passphrase."),
    "data": N_("Liegt in data/ neben den verschlüsselten Daten - wer data/ kopiert, kann alles entschlüsseln. "
               "Ein Update richtet das ein."),
}


def status(data_dir: Path, *, secure: bool) -> list[Check]:
    d = Path(data_dir)
    out: list[Check] = []
    source = crypto.host_key_source(d)
    protected = ("systemd-creds-tpm2", "systemd-creds", "systemd", "file", "container")
    out.append(Check(N_("Schlüssel des Dienstes"), source in protected,
                     KEY_DETAILS.get(source, KEY_DETAILS["systemd"])))
    if crypto.passphrase_enabled(d):
        out.append(Check(N_("Master-Passphrase"), True, N_("Gesetzt: Ohne sie sind Zugangsdaten, Einstellungen und "
                                                           "Sync-Daten nicht lesbar, auch nicht aus einer Sicherung.")))
    elif source != "systemd-creds-tpm2":
        # #94: without a TPM the key's protection lives in the container too - a backup of it holds both.
        out.append(Check(N_("Master-Passphrase"), True, N_("Nicht gesetzt, und ohne TPM: Ein Backup des Containers "
                                                           "enthält Schlüssel und Daten. Master-Passphrase setzen "
                                                           "oder die Backups verschlüsseln."), warn=True))
    else:
        out.append(Check(N_("Master-Passphrase"), True, N_("Nicht gesetzt: Abrufe laufen nach einem Neustart ohne "
                                                           "Zutun weiter. Wer den ganzen Container samt Schlüssel "
                                                           "kopiert, kann die Daten entschlüsseln."), warn=True))
    secrets_file = d / "secrets.enc"
    sealed = secrets_file.exists() and crypto.is_sealed(secrets_file.read_bytes())
    out.append(Check(N_("Zugangsdaten und Sitzungen"), sealed or not secrets_file.exists(),
                     N_("AES-256-GCM (secrets.enc).") if sealed
                     else N_("Noch nichts gespeichert.") if not secrets_file.exists()
                     else N_("Altes Format - wird beim nächsten Lesen neu verschlüsselt.")))
    out.append(Check(N_("Einstellungen"), not (d / "config.json").exists(),
                     N_("AES-256-GCM (config.enc, Benachrichtigungen in notify.enc).")
                     if not (d / "config.json").exists() else N_("config.json liegt noch unverschlüsselt vor.")))
    db = d / "state.db"
    if not state_mod.ENCRYPTED:
        out.append(Check(N_("Sync-Datenbank"), False, N_("Unverschlüsselt: SQLCipher gibt es für diese Plattform "
                                                         "nicht (nur x86-64 und ARM64).")))
    else:
        plain = state_mod.is_plain(db)
        out.append(Check(N_("Sync-Datenbank"), not plain, N_("Mit SQLCipher verschlüsselt (AES-256).") if not plain
                         else N_("Noch unverschlüsselt - wird beim nächsten Öffnen verschlüsselt.")))
    out.append(_connection(secure))
    out.append(Check(N_("Scalable CLI"), True, N_("Läuft in einer Sandbox (Landlock): kein Zugriff auf data/ und "
                                                  "den Schlüssel.") if sandbox.supported() else
                     N_("Ohne Sandbox: Der Kernel kann kein Landlock. sc läuft als Dienst-Benutzer und "
                        "könnte dessen Schlüssel und Daten lesen."), warn=not sandbox.supported()))
    loose = _loose_permissions(d)
    out.append(Check(N_("Dateirechte"), not loose, N_("Nur der Dienst-Benutzer kann data/ lesen.") if not loose
                     else N_("Für andere lesbar: {files}"), params={"files": ", ".join(loose)} if loose else {}))
    leftovers = [f for f in PLAINTEXT_LEFTOVERS if (d / f).exists()]
    if source == "data":
        leftovers = [f for f in leftovers if f != "secret.key"]
    out.append(Check(N_("Klartext-Reste"), not leftovers, N_("Keine.") if not leftovers else "{files}",
                     params={"files": ", ".join(leftovers)} if leftovers else {}))
    return out


def _connection(secure: bool) -> Check:
    """HTTPS with the certificate systemd handed over: its validity and the CA's fingerprint to compare on import."""
    label = N_("Verbindung zur Oberfläche")
    if not secure:
        return Check(label, False, N_("Unverschlüsseltes HTTP - Passwörter gehen im Klartext durchs Netz."))
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred) / "tls.crt" if cred else None
    if not path or not path.is_file():
        return Check(label, True, N_("HTTPS."))
    from cryptography import x509

    until = f"{x509.load_pem_x509_certificate(path.read_bytes()).not_valid_after_utc:%d.%m.%Y}"
    ca = ca_fingerprint()
    if not ca:
        return Check(label, True, N_("HTTPS. Zertifikat bis {until}"), params={"until": until})
    return Check(label, True, N_("HTTPS. Zertifikat bis {until}, CA SHA-256 {ca}"), params={"until": until, "ca": ca})


def ca_file() -> Path | None:
    """The CA certificate systemd handed over (for download and its fingerprint), if any."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    path = Path(cred) / "tls.ca" if cred else None
    return path if path and path.is_file() else None


def ca_fingerprint() -> str:
    path = ca_file()
    if not path:
        return ""
    from cryptography import x509

    from .tls import fingerprint

    return fingerprint(x509.load_pem_x509_certificate(path.read_bytes()))


def _loose_permissions(d: Path) -> list[str]:
    loose = []
    if d.exists() and stat.S_IMODE(d.stat().st_mode) & 0o077:
        loose.append("data/")
    loose.extend(f.name for f in (sorted(d.glob("*")) if d.exists() else [])
                 if f.is_file() and stat.S_IMODE(f.stat().st_mode) & 0o077)
    return loose


def change_passphrase(data_dir: Path, new: str | None) -> None:
    """Set, change (``new``) or remove (``None``) the master passphrase: everything in the data tier is
    re-encrypted. The service must be unlocked; no sync may run meanwhile."""
    d = Path(data_dir)
    if new is not None and len(new) < crypto.MIN_PASSPHRASE:
        raise ValueError(f"Die Passphrase braucht mindestens {crypto.MIN_PASSPHRASE} Zeichen.")
    # The UI's own writes to state.db wait meanwhile (#120); a write that waited past the swap is refused
    # (``StateReplaced``), never written into the old file.
    with run_lock(d, wait=30), crypto.file_lock(d, "secrets.lock"), state_mod.WRITES:
        recover(d, locked=True)
        new_key, ring = crypto.new_passphrase_key(d, new)
        pkey = ring.pop("_pkey", None)
        _stage_data_tier(d, new_key, crypto.host_key(d), ring)
        # The journal is the switch: from here on the new files win, even after a crash.
        _write_journal(d, "passphrase", b"")
        _swap_staged(d)
        crypto.cache_pkey(d, bytes.fromhex(pkey) if pkey else None)


# ── re-encryption: staged next to the old files, one switch, recovery (#91) ──
JOURNAL = "rekey.json"
STAGED = ".rekey"
DATA_TIER = ("secrets.enc", "config.enc", "notify.enc", "keyring.json")


def _stage_data_tier(d: Path, new_data: bytes, new_host: bytes, ring: dict) -> None:
    """Write every file of the data tier, re-encrypted, as ``<name>.rekey`` - the old ones stay untouched;
    ``state.db`` is re-encrypted as a copy, never in place."""
    old_data = crypto.data_key(d)
    if (d / "secrets.enc").exists():
        crypto.write_atomic(d / f"secrets.enc{STAGED}",
                            crypto.seal(new_data, json.dumps(Vault(d).load()).encode(), "secrets"))
    config_blob, notify_blob = config_mod.sealed(config_mod.load(d), new_data, new_host)
    crypto.write_atomic(d / f"config.enc{STAGED}", config_blob)
    crypto.write_atomic(d / f"notify.enc{STAGED}", notify_blob)
    db = d / "state.db"
    if db.exists() and state_mod.ENCRYPTED:
        tmp = d / f"state.db{STAGED}"
        shutil.copy2(db, tmp)
        os.chmod(tmp, 0o600)
        state_mod.rekey(d, old_data, new_data, path=tmp)
        crypto.fsync_file(tmp)
    crypto.write_atomic(d / f"keyring.json{STAGED}", json.dumps(ring, indent=2).encode())


def _write_journal(d: Path, kind: str, check: bytes) -> None:
    crypto.fsync_dir(d)  # the staged files' renames are on disk before the journal can say they exist
    crypto.write_atomic(d / JOURNAL, json.dumps({"kind": kind, "check": check.hex()}).encode())


def _swap_staged(d: Path) -> None:
    for tmp in sorted(d.glob(f"*{STAGED}")):
        target = d / tmp.name[: -len(STAGED)]
        os.replace(tmp, target)
        if target.name == "state.db":
            for suffix in ("-journal", "-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
    crypto.fsync_dir(d)
    (d / JOURNAL).unlink(missing_ok=True)


def recover(data_dir: Path, *, locked: bool = False) -> str:
    """Finish or undo a re-encryption a crash interrupted; run before anything reads the data tier.

    Without a journal, staged files are leftovers of an attempt that never reached its switch: removed.
    A passphrase change switched when it wrote its journal: finished. A new host key switches when
    ``deploy/setup.sh`` stores the key - the journal's check value tells whether the key in use is the new
    one (finished) or still the old one (undone). Returns what was done ("" for nothing)."""
    d = Path(data_dir)
    staged = list(d.glob(f"*{STAGED}"))
    journal = d / JOURNAL
    if not staged and not journal.exists():
        return ""
    with contextlib.nullcontext() if locked else crypto.file_lock(d, "secrets.lock"):
        if not journal.exists():
            for f in d.glob(f"*{STAGED}"):
                f.unlink()
            return "undone"
        entry = json.loads(journal.read_text())
        if entry["kind"] == "host" and not _opens(crypto.host_key(d), bytes.fromhex(entry["check"])):
            for f in d.glob(f"*{STAGED}"):
                f.unlink()
            journal.unlink()
            return "undone"
        _swap_staged(d)
        return "finished"


def _opens(key: bytes, check: bytes) -> bool:
    try:
        crypto.unseal(key, check, "rekey check")
        return True
    except Exception:
        return False


def _new_install(cfg: config_mod.Config) -> None:
    cfg.services = {}
    cfg.ui_language = "en"  # a new install starts in English; the first page offers the switch


def migrate(data_dir: Path) -> list[str]:
    """Bring the data of an older version up to date (run by deploy/setup.sh); returns what was done."""
    d = Path(data_dir)
    done = []
    if (d / "secrets.enc").exists() and not crypto.is_sealed((d / "secrets.enc").read_bytes()):
        Vault(d).load()
        done.append("Zugangsdaten mit AES-256-GCM neu verschlüsselt")
    if (d / "config.json").exists():
        config_mod.load(d)
        done.append("Einstellungen verschlüsselt")
    if state_mod.ENCRYPTED and state_mod.is_plain(d / "state.db"):
        state_mod.State(d).close()
        done.append("Sync-Datenbank mit SQLCipher verschlüsselt")
    if crypto.is_locked(d):
        return done
    if not (d / "config.enc").exists():  # a new install: every service of the page Dienste starts off (#145)
        config_mod.update(d, _new_install)
    m = re.fullmatch(r"http://([^/:]+):8090", config_mod.load(d).public_url or "")
    if m:
        config_mod.update(d, lambda cfg: setattr(cfg, "public_url", f"https://{m.group(1)}:8443"))
        done.append("Adresse für Links auf HTTPS umgestellt")
    for f in [d] + [p for p in d.glob("*") if p.is_file()]:
        mode = stat.S_IMODE(f.stat().st_mode)
        want = 0o700 if f.is_dir() else 0o600
        if mode & 0o077:
            os.chmod(f, want)
    return done


def rotate_host_key(data_dir: Path, material: bytes) -> None:
    """Re-encrypt everything for a new host key (``deploy/setup.sh`` hands it over).

    The new files are only staged next to the old ones: the switch is the key itself. ``setup.sh``
    stores it after this returned, then ``wealthfolio-companion recover`` swaps the files in - and so does every
    start of the service if anything stopped in between (``recover``). Until the key is stored the old
    one keeps opening everything."""
    d = Path(data_dir)
    if len(material) < 32:
        raise ValueError("Der neue Schlüssel ist zu kurz.")
    with run_lock(d, wait=30), crypto.file_lock(d, "secrets.lock"):
        recover(d, locked=True)
        new_host, new_data, ring = crypto.keys_for_new_host_key(d, material)
        _stage_data_tier(d, new_data, new_host, ring)
        _write_journal(d, "host", crypto.seal(new_host, b"rekey", "rekey check"))
