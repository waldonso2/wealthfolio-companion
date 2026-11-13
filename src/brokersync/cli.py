"""Command line: ``wealthfolio-companion serve`` (web UI, HTTPS), ``wealthfolio-companion run`` (one sync, for the
timer), the helpers ``deploy/setup.sh`` uses (``renew-cert``, ``migrate``) and ``backup``/``restore`` (#109).
One function per command (``COMMANDS``)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__, redact
from .config import data_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wealthfolio-companion", description="Wealthfolio Companion")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_cmd = sub.add_parser("serve", help="run the web UI")
    serve_cmd.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the UI is for the LAN; gate.Hosts limits it
    serve_cmd.add_argument("--port", type=int, default=8090)
    serve_cmd.add_argument("--certfile", help="TLS certificate: serve HTTPS")
    serve_cmd.add_argument("--keyfile", help="TLS key")
    serve_cmd.add_argument("--redirect-port", type=int, help="also listen with plain HTTP here, redirecting to HTTPS")
    container = sub.add_parser("container", help="run in the container image: migrate, setup code, certificate, "
                                                 "daily fetch and the web UI in one process (#148)")
    container.add_argument("--host", default="0.0.0.0")  # noqa: S104 - published by the container's port mapping
    run = sub.add_parser("run", help="sync all enabled brokers once")
    run.add_argument("--broker", action="append", help="only this broker (repeatable)")
    sub.add_parser("reset-ui-password", help="forget the web UI password; prints the code to set a new one")
    sub.add_parser("setup-code", help="a new one-time code for setting the first web UI password")
    sc = sub.add_parser("install-sc", help="install or update Scalable's official CLI (sc), signature-checked")
    sc.add_argument("--dir", default="/opt/wealthfolio-companion/bin")
    cert = sub.add_parser("make-cert",
                          help="create or renew the TLS certificate of the web UI (signed by the container's own CA)")
    cert.add_argument("--dir", default=TLS_DIR)
    cert.add_argument("--host", action="append", default=[], help="host name for the certificate (repeatable)")
    cert.add_argument("--ip", action="append", default=[], help="IP address for the certificate (repeatable)")
    renew = sub.add_parser("renew-cert", help="the UI certificate for this container's name and current addresses, "
                                              "plus extra-names; restarts the UI when it changed (root, daily)")
    renew.add_argument("--dir", default=TLS_DIR)
    renew.add_argument("--no-restart", action="store_true", help="don't restart the web UI (deploy/setup.sh)")
    sub.add_parser("migrate", help="bring the data of an older version up to date (encryption)")
    sub.add_parser("rotate-key", help="re-encrypt everything for a new host key, read from stdin (deploy/setup.sh)")
    sub.add_parser("recover", help="finish or undo a re-encryption that was interrupted (also on every start)")
    sub.add_parser("security-status", help="what is protected how (the page Sicherheit)")
    sub.add_parser("backup", help="data/ as an encrypted archive to stdout: companion-cli backup > file.tar.gz")
    sub.add_parser("restore", help="data/ from such an archive on stdin, after checking it opens with this "
                                   "container's key; the web UI must be stopped")
    due = sub.add_parser("update-due", help="for the container's updater: whether to ask GitHub now, and the newer "
                                            "release to install (prints 'checked' and 'install <version>')")
    due.add_argument("--since", type=float, required=True, help="seconds since GitHub was last asked")
    done = sub.add_parser("update-done", help="for the container's updater: notify and record how an update went")
    done.add_argument("--from", dest="old", required=True)
    done.add_argument("--to", dest="new", required=True)
    done.add_argument("--failed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()
    if args.cmd in ("serve", "container", "run", "migrate", "recover", "backup", "restore"):
        from .security import recover

        done = recover(data_dir())
        if done:
            logging.getLogger("brokersync").warning("an interrupted re-encryption was %s", done)
        if args.cmd == "recover":
            print(f"Re-encryption {done}." if done else "Nothing to recover.")
            return 0
    return COMMANDS[args.cmd](args)


TLS_DIR = "/etc/wealthfolio-companion/tls"


def _serve(args) -> int:
    from .web.serve import serve

    serve(data_dir(), host=args.host, port=args.port, certfile=args.certfile, keyfile=args.keyfile,
          redirect_port=args.redirect_port)
    return 0


def _container(args) -> int:
    from .container import main as container_main

    return container_main(args)


def _make_cert(args) -> int:
    from pathlib import Path

    from .tls import ensure_certificate

    print(ensure_certificate(Path(args.dir), args.host, args.ip))
    return 0


def _renew_cert(args) -> int:
    """#92: daily (wealthfolio-companion-cert.timer) - after an address change, a fitting certificate within 24 h."""
    import socket
    import subprocess
    from pathlib import Path

    from .tls import ensure_certificate, extra_names

    directory = Path(args.dir)
    try:
        ips = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        ips = []
    names, more_ips = extra_names(directory)
    before = (directory / "cert.pem").read_bytes() if (directory / "cert.pem").exists() else b""
    print(ensure_certificate(directory, [socket.gethostname(), *names], ips + more_ips))
    for f in directory.glob("*.pem"):
        f.chmod(0o600)
    if not args.no_restart and (directory / "cert.pem").read_bytes() != before:
        subprocess.run(["systemctl", "try-restart", "wealthfolio-companion"], timeout=120, check=False)
    return 0


def _migrate(args) -> int:
    from .security import migrate

    for line in migrate(data_dir()) or ["Nothing to migrate."]:
        print(line)
    return 0


def _rotate_key(args) -> int:
    from . import crypto
    from .security import rotate_host_key

    try:
        rotate_host_key(data_dir(), sys.stdin.buffer.read().strip())
    except (crypto.Locked, ValueError) as e:
        print(f"Key not rotated: {e}", file=sys.stderr)
        return 1
    print("Everything re-encrypted for the new key.")
    return 0


def _security_status(args) -> int:

    from .container import tls_enabled
    from .security import status

    for c in status(data_dir(), secure=tls_enabled()):
        print(f"{'HINWEIS' if c.warn else 'OK' if c.ok else 'PROBLEM':8} {c.label}: {c.text}")
    return 0


def _run(args) -> int:
    from . import crypto
    from .sync import AlreadyRunning, Syncer

    if crypto.is_locked(data_dir()):
        _notify_locked()
        return 1
    try:
        results = Syncer(data_dir()).run(args.broker)
    except AlreadyRunning as e:
        print(e)
        return 0
    for r in results:
        print(f"{r.broker}: {r.status} - {r.created} neu, {r.existing} schon vorhanden, {r.failed} fehlgeschlagen,"
              f" {r.unknown} unbekannt")
    return 1 if any(r.status == "error" for r in results) else 0


def _setup_code(args) -> int:
    from .vault import SETUP_CODE_HOURS, Vault, new_setup_code

    vault = Vault(data_dir())
    if args.cmd == "reset-ui-password":
        def forget(d: dict) -> None:
            d.pop("ui_password_hash", None)
            d.pop("ui_session_key", None)  # signs everyone out

        vault.update(forget)
        print("Web UI password removed.")
    elif vault.load().get("ui_password_hash"):
        print("The web UI password is set already; to choose a new one: companion-reset-password",
              file=sys.stderr)
        return 1
    # Printed here, on the console only - never logged.
    print(f"Setup code (valid {SETUP_CODE_HOURS} h, enter it with the new password): {new_setup_code(vault)}")
    return 0


def _install_sc(args) -> int:
    from pathlib import Path

    from .sc_install import InstallError, install

    try:
        version, new = install(Path(args.dir))
    except (InstallError, OSError) as e:
        print(f"Scalable CLI not installed: {e}", file=sys.stderr)
        return 1
    print(f"Scalable CLI {version} {'installed' if new else 'is up to date'} in {args.dir}.")
    return 0


def _backup(args) -> int:
    from .backup import write

    if sys.stdout.isatty():
        print("Redirect the archive into a file: companion-cli backup > backup.tar.gz", file=sys.stderr)
        return 2
    names = write(data_dir(), sys.stdout.buffer)
    sys.stdout.buffer.flush()
    print(f"Backed up {len(names)} files - they open only with this container's key "
          "(/etc/wealthfolio-companion) and the master passphrase, if set.", file=sys.stderr)
    return 0


def _restore(args) -> int:
    from .backup import RestoreError, restore

    try:
        print(restore(data_dir(), sys.stdin.buffer))
    except RestoreError as e:
        print(f"Not restored, data/ unchanged: {e}", file=sys.stderr)
        return 1
    return 0


def _update_due(args) -> int:
    """Asked every minute by deploy/companion-autoupdate (root, through companion-cli): only the host tier is
    read, so it works while the data is locked."""
    from . import updates
    from .config import load_notify

    cfg = load_notify(data_dir())
    asked, version = updates.due(cfg.auto_update, cfg.update_interval, __version__, args.since,
                                 delay=cfg.update_delay)
    if asked:
        print("checked")
    if version:
        print(f"install {version}")
    return 0


def _update_done(args) -> int:
    """After an automatic update: ntfy, and on the page *Updates* (when the data is unlocked)."""
    from datetime import UTC, datetime

    from . import meta_keys
    from .config import load_notify
    from .notify import Notifier
    from .state import State

    cfg = load_notify(data_dir())
    ok = not args.failed
    text = (f"Version {args.new} ist installiert (vorher {args.old})." if ok else
            f"Das automatische Update auf {args.new} ist fehlgeschlagen - Version {args.old} läuft weiter. "
            "Details: journalctl -u wealthfolio-companion-update")
    Notifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token, details=cfg.ntfy_details).send(
        "Update installiert" if ok else "Update fehlgeschlagen", text,
        link=f"{cfg.public_url.rstrip('/')}/updates" if cfg.public_url else None,
        tags="arrow_up" if ok else "warning")
    try:
        state = State(data_dir())
        state.set_meta(meta_keys.AUTO_UPDATE(), json.dumps({"from": args.old, "to": args.new, "ok": ok,
                                                            "at": datetime.now(UTC).isoformat()}))
        state.add_event("auto_update", ok=ok, detail=f"{args.old} -> {args.new}")
    except Exception as e:  # locked by the master passphrase: ntfy said it
        logging.getLogger("brokersync").info("update not recorded on the page (%s)", type(e).__name__)
    print(text)
    return 0 if ok else 1


COMMANDS = {"serve": _serve, "run": _run, "reset-ui-password": _setup_code, "setup-code": _setup_code,
            "install-sc": _install_sc, "make-cert": _make_cert, "renew-cert": _renew_cert, "migrate": _migrate,
            "rotate-key": _rotate_key, "security-status": _security_status, "backup": _backup, "restore": _restore,
            "update-due": _update_due, "update-done": _update_done, "container": _container}


class JsonFormatter(logging.Formatter):
    """One JSON object per line (``COMPANION_LOG_FORMAT=json``, OPS-04 of #85) - already redacted by the
    handler's filter, like the plain lines."""

    def format(self, record: logging.LogRecord) -> str:
        out = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "message": record.getMessage()}
        if record.exc_text:
            out["exception"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("COMPANION_LOG_FORMAT", "").lower() == "json":
        for handler in logging.getLogger().handlers:
            handler.setFormatter(JsonFormatter())
    redact.install()
    # httpx logs every request with its full URL (query parameters included) on INFO; the service
    # logs what it does itself, so only their warnings.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _notify_locked() -> None:
    """The timer found the service locked (master passphrase, after a restart): say so, with a link."""
    from . import config as config_mod
    from .notify import Notifier

    logging.getLogger("brokersync").warning("service is locked: enter the master passphrase in the web UI")
    cfg = config_mod.load_notify(data_dir())
    link = f"{cfg.public_url.rstrip('/')}/unlock" if cfg.public_url else None
    Notifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token).send(
        "Companion gesperrt", "Nach einem Neustart: Bitte in der Weboberfläche die Master-Passphrase eingeben, "
        "sonst ruft der Dienst nichts ab.", link=link, priority="high", tags="lock")


if __name__ == "__main__":
    sys.exit(main())
