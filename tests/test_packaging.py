"""The community-scripts layout the install line relies on - a mistake here
only shows up when someone runs the install on Proxmox, so it is checked here."""

import json
import re
import tomllib
from pathlib import Path

import brokersync

ROOT = Path(__file__).parent.parent
SLUG = "wealthfolio-companion"
RAW = "https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main"


def test_catalog_entry_matches_the_scripts():
    meta = json.loads((ROOT / "json" / f"{SLUG}.json").read_text())
    assert meta["slug"] == SLUG and meta["type"] == "ct"
    assert [m["script"] for m in meta["install_methods"]] == [f"ct/{SLUG}.sh"]
    assert (ROOT / "ct" / f"{SLUG}.sh").exists()
    assert (ROOT / "install" / f"{SLUG}-install.sh").exists()
    assert meta["interface_port"] == 8443


def test_ct_script_loads_the_engine_with_this_repository_as_script_source():
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    lines = [ln for ln in ct.splitlines() if ln and not ln.startswith("#")]
    # COMMUNITY_SCRIPTS_URL must be set before the engine is sourced: build.func
    # resolves install/<slug>-install.sh and writes the container's `update`
    # command from it. Without it both come from community-scripts/ProxmoxVE.
    assert lines[0] == f'export COMMUNITY_SCRIPTS_URL="${{COMMUNITY_SCRIPTS_URL:-{RAW}}}"'
    # The engine runs as root on the host: pinned to a commit, set unconditionally (an `update`
    # command written by an older version still exports the URL it used), and loaded from there.
    assert re.fullmatch(r'export COMMUNITY_SCRIPTS_CORE_URL="https://raw\.githubusercontent\.com/'
                        r'community-scripts/core/[0-9a-f]{40}"', lines[1])
    assert lines[2] == 'source <(curl -fsSL "$COMMUNITY_SCRIPTS_CORE_URL/core/build.func")'
    assert "core/main" not in ct
    # build.func derives the install script name from APP: lower case, no spaces.
    app = re.search(r'^APP="([^"]+)"', ct, re.M).group(1)
    assert app.lower().replace(" ", "") == SLUG


def test_install_line_in_the_docs_matches_the_script():
    line = f'bash -c "$(curl -fsSL {RAW}/ct/{SLUG}.sh)"'
    assert line in (ROOT / "README.md").read_text()
    assert line in (ROOT / "ct" / f"{SLUG}.sh").read_text()


def test_units_and_setup_agree_on_paths():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    for unit in (ROOT / "deploy" / "systemd").iterdir():
        text = unit.read_text()
        if unit.name == "wealthfolio-companion-update.service":
            # Root on purpose: the updater runs the console's update (opt-in on the page Updates).
            assert "User=root" in text and "ExecStart=/usr/local/bin/companion-autoupdate" in text
        elif unit.name == "wealthfolio-companion-update-recover.service":
            # Root on purpose: undoes an interrupted update at boot, only while update-pending exists (#149).
            assert "User=root" in text and "ExecStart=/usr/local/bin/companion-update --recover" in text
            assert "ConditionPathExists=/opt/wealthfolio-companion/update-pending" in text
            assert "enable -q wealthfolio-companion-update-recover.service" in setup
        elif unit.suffix == ".service":
            assert "ExecStart=/opt/wealthfolio-companion/venv/bin/wealthfolio-companion" in text
            assert "COMPANION_DATA=/opt/wealthfolio-companion/data" in text
            # Else only the certificate's renewal is root: it writes /etc/wealthfolio-companion/tls (#92).
            assert "User=companion" in text or ("User=root" in text and "renew-cert" in text)
    assert "USER_NAME=companion" in setup and 'BASE=/opt/wealthfolio-companion' in setup
    updater = (ROOT / "deploy" / "companion-update").read_text()
    assert "SERVICES=(wealthfolio-companion wealthfolio-companion-run.timer)" in updater


def test_version_is_the_same_everywhere():
    """One place, pyproject.toml (#80): the package reads it from its installed metadata."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == brokersync.__version__, "reinstall: pip install --no-deps --no-build-isolation -e ."
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {project['version']}" in changelog


def test_setup_survives_its_own_pitfalls():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    # tar | grep -q under pipefail: grep stops early, tar gets SIGPIPE, the pipeline "fails" - the check never hit.
    assert not re.search(r"tar [^|]*\| *grep -q", setup)
    # Steps that talk to systemd never hang an update forever; companion-update starts the service afterwards.
    assert "timeout 900 /usr/local/bin/companion-cli migrate" in setup and "rotate-key" in setup
    # Only through companion-update: build before the switch, configure after it.
    assert "build) build ;;" in setup and "configure) configure ;;" in setup


def test_the_updater_is_installed_and_runs_the_consoles_update_unattended():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    script = (ROOT / "deploy" / "companion-autoupdate").read_text()
    timer = (ROOT / "deploy" / "systemd" / "wealthfolio-companion-update.timer").read_text()
    assert "/usr/local/bin/companion-autoupdate" in setup
    assert "enable -q --now wealthfolio-companion-update.timer" in setup
    # The same, checked update as by hand; never while a fetch runs or another update holds the lock; nothing
    # before the service says so.
    assert "/usr/local/bin/companion-update ||" in script and "/usr/bin/update" not in script
    assert "is-active --quiet wealthfolio-companion-run.service" in script and "flock -n 9" in script
    assert script.index("update-due") < script.index("/usr/local/bin/companion-update ||")
    assert '/usr/local/bin/companion-update' in setup and "exec /usr/local/bin/companion-update" in setup
    assert "OnUnitInactiveSec=60s" in timer
    service = (ROOT / "deploy" / "systemd" / "wealthfolio-companion-update.service").read_text()
    assert "Environment=TERM=" in service  # the engine's `clear` fails without one (0.34.0)
    # 0077 left the new program root-only and the service unable to start (203/EXEC, 0.34.0).
    assert "UMask=0022" in service and "\numask 022\n" in setup


def test_the_cli_wrapper_runs_commands_like_the_web_service():
    cli = (ROOT / "deploy" / "companion-cli").read_text()
    unit = (ROOT / "deploy" / "systemd" / "wealthfolio-companion.service").read_text()
    for env in ("COMPANION_DATA", "COMPANION_RUNTIME", "COMPANION_TLS"):
        assert f"Environment={env}=" in unit and f"Environment={env}=" in cli


def test_core_pin_is_the_one_the_watch_workflow_compares():
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    pin = re.search(r"community-scripts/core/([0-9a-f]{40})", ct).group(1)
    watch = (ROOT / ".github" / "workflows" / "core-watch.yml").read_text()
    assert "ct/wealthfolio-companion.sh" in watch and "community-scripts/core" in watch
    assert len(pin) == 40


def test_backups_are_never_readable_by_others():
    for path in (ROOT / "ct" / f"{SLUG}.sh", ROOT / "deploy" / "setup.sh"):
        for line in path.read_text().splitlines():
            if "tar -czf" in line:
                assert "umask 077" in line, f"{path.name}: {line.strip()}"



UNITS = ROOT / "deploy" / "systemd"
SANDBOX = ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true", "PrivateDevices=true",
           "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectKernelLogs=true",
           "ProtectControlGroups=true", "ProtectClock=true", "ProtectHostname=true", "ProtectProc=invisible",
           "RestrictSUIDSGID=true", "RestrictRealtime=true", "RestrictNamespaces=true", "LockPersonality=true",
           "SystemCallArchitectures=native", "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
           "RemoveIPC=true", "CapabilityBoundingSet=\n", "UMask=0077")


def _directives(name: str) -> list[str]:
    return [line for line in (UNITS / name).read_text().splitlines() if line and not line.startswith(("#", "["))]


def test_every_unit_is_sandboxed_as_far_as_its_job_allows():
    """SEC-08 (#154): the hardening a later edit must not lose. The service user's units write only data/ (the
    unlock cache is their RuntimeDirectory, temp files their PrivateTmp, the TLS files come as credentials) and
    reach only IP networks; the certificate's renewal is root but sends no IP traffic; the updaters stay root
    with a writable system, but without the kernel, raw sockets or the capabilities they never use."""
    for name in ("wealthfolio-companion.service", "wealthfolio-companion-run.service"):
        text = (UNITS / name).read_text() + "\n"
        for option in (*SANDBOX, "DevicePolicy=closed", "SystemCallFilter=@sandbox",
                       "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n", "RuntimeDirectoryMode=0700"):
            assert option in text, (name, option)
        assert [d for d in _directives(name) if d.startswith(("ReadWritePaths=", "BindPaths="))] == [
            "ReadWritePaths=/opt/wealthfolio-companion/data"], name
        assert "User=companion" in text and "AmbientCapabilities" not in text
    cert = (UNITS / "wealthfolio-companion-cert.service").read_text() + "\n"
    for option in (*SANDBOX, "IPAddressDeny=any"):
        assert option in cert, option
    assert [d for d in _directives("wealthfolio-companion-cert.service") if d.startswith("ReadWritePaths=")] == [
        "ReadWritePaths=/etc/wealthfolio-companion/tls"]
    for name in ("wealthfolio-companion-update.service", "wealthfolio-companion-update-recover.service"):
        text = (UNITS / name).read_text()
        for option in ("PrivateTmp=true", "ProtectKernelTunables=true", "ProtectKernelModules=true",
                       "ProtectKernelLogs=true", "ProtectControlGroups=true", "RestrictSUIDSGID=true",
                       "LockPersonality=true", "SystemCallArchitectures=native",
                       "RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6"):
            assert option in text, (name, option)
        dropped = re.search(r"^CapabilityBoundingSet=~(.*)$", text, re.MULTILINE).group(1).split()
        assert {"CAP_SYS_ADMIN", "CAP_SYS_MODULE", "CAP_SYS_PTRACE", "CAP_SYS_RAWIO", "CAP_NET_ADMIN",
                "CAP_NET_RAW", "CAP_BPF", "CAP_MKNOD"} <= set(dropped), name
        # It writes /opt, /etc, /usr/bin and /root and may need the TPM (systemd-creds): never these.
        for option in ("ProtectSystem=", "ProtectHome=", "PrivateDevices=", "DevicePolicy=", "ProtectClock="):
            assert option not in text, (name, option)
    # Both updaters carry the same sandbox.
    def sandbox(name):
        return [d for d in _directives(name) if d.split("=")[0].startswith(("Protect", "Private", "Restrict",
                                                                           "Lock", "SystemCall", "Capability"))]
    assert sandbox("wealthfolio-companion-update.service") == sandbox("wealthfolio-companion-update-recover.service")


def test_systemd_rates_the_units_exposure():
    """#107: systemd-analyze's exposure score, offline from the files, where systemd is installed (CI's Ubuntu)."""
    import shutil
    import subprocess

    import pytest

    if not shutil.which("systemd-analyze"):
        pytest.skip("no systemd-analyze")
    limits = {"wealthfolio-companion.service": 2.0, "wealthfolio-companion-run.service": 2.0,
              "wealthfolio-companion-cert.service": 2.0,
              # Root with a writable system by its job (above); measured 6.1.
              "wealthfolio-companion-update.service": 6.5, "wealthfolio-companion-update-recover.service": 6.5}
    for name, limit in limits.items():
        done = subprocess.run(["systemd-analyze", "security", "--offline=true", "--no-pager", str(UNITS / name)],
                              capture_output=True, text=True, timeout=60, check=False)
        found = re.search(r"Overall exposure level for \S+: ([0-9.]+)", done.stdout)
        if not found:
            pytest.skip(f"systemd-analyze can't rate unit files offline here: {done.stderr.strip()[:200]}")
        assert float(found.group(1)) <= limit, (name, found.group(1))


def test_the_container_and_its_files_stay_least_privileged():
    """SEC-08 (#154): an unprivileged LXC without devices or mounts; the program root's and readable, only data/
    the service user's (0700), the key and TLS directory root-only (0700, files 0600)."""
    ct = (ROOT / "ct" / f"{SLUG}.sh").read_text()
    for var in ('var_unprivileged="${var_unprivileged:-1}"', 'var_fuse="${var_fuse:-no}"', 'var_tun="${var_tun:-no}"',
                'var_gpu="${var_gpu:-no}"', 'var_nesting="${var_nesting:-1}"'):
        assert var in ct, var
    assert not re.search(r"\bmp[0-9]+=|var_mount_fs=|pct set", ct)
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    assert 'useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin "$USER_NAME"' in setup
    assert 'install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$BASE/data"' in setup
    assert 'install -d -m 700 "$ETC" "$ETC/tls"' in setup
    assert 'chmod 600 "$2"' in setup and 'install -m 600 "$1" "$ETC/secret.key$2"' in setup
    # The last word on data/'s modes, after every step that wrote into it.
    assert setup.rindex('chmod -R u=rwX,go= "$BASE/data"') > setup.rindex("companion-cli")
    # The service user owns nothing but its data: the program it runs stays root's.
    assert [line.strip() for line in setup.splitlines() if "chown" in line] == [
        'chown -R "$USER_NAME:$USER_NAME" "$BASE/data"'] * 2
    assert "\numask 022\n" in setup
    updater = (ROOT / "deploy" / "companion-update").read_text()
    assert 'chmod 755 "$dir"' in updater and "(umask 077 && tar -czf" in updater

def test_setup_installs_only_the_locked_hashes():
    setup = (ROOT / "deploy" / "setup.sh").read_text()
    assert '--require-hashes --only-binary :all: -r "$APP/requirements.txt"' in setup
    assert '--no-deps --no-build-isolation "$APP"' in setup
    # Every install of the package goes through the lock.
    assert setup.count('bin/pip" install') == 1 and setup.count('"${PIP[@]}"') == 2


def _locked() -> dict[str, tuple[str, str]]:
    """requirements.txt: name -> (version, marker)."""
    out = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\", line)
        if m:
            out[m.group(1).lower().replace("_", "-")] = (m.group(2), (m.group(3) or "").strip())
    return out


def test_lock_covers_every_dependency_with_hashes():
    from packaging.markers import Marker
    from packaging.requirements import Requirement

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked = _locked()
    wanted = [Requirement(r) for r in meta["project"]["dependencies"]]
    wanted += [Requirement(r) for r in meta["build-system"]["requires"]]
    for req in wanted:
        name = req.name.lower().replace("_", "-")
        assert name in locked, f"{req.name} is missing in requirements.txt - run scripts/lock.sh"
        version, marker = locked[name]
        assert req.specifier.contains(version, prereleases=True), \
            f"{req.name}=={version} doesn't satisfy {req.specifier} - run scripts/lock.sh"
        if req.marker:
            assert str(Marker(marker)) == str(req.marker), f"{req.name}: marker {marker!r} - run scripts/lock.sh"
    # Every locked package carries at least one hash.
    text = (ROOT / "requirements.txt").read_text()
    assert text.count("--hash=sha256:") >= len(locked)
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.strip())
    assert all("--hash=sha256:" in b for b in blocks if "==" in b)


def _block(path: Path, name: str) -> str:
    text = path.read_text()
    start = text.index(f"# >>> {name}")
    return text[start:text.index(f"# <<< {name}", start)]


def test_install_and_update_share_the_release_functions():
    """#88/#146/#165: one implementation of "download, verify, then unpack" - the same text in the installer, the
    engine's update script and the installed updater; the bootstrap verifier the same in ct/ and install/.
    Their behaviour: tests/test_update_security.py."""
    ct, install = ROOT / "ct" / f"{SLUG}.sh", ROOT / "install" / f"{SLUG}-install.sh"
    updater = ROOT / "deploy" / "companion-update"
    shared = _block(ct, "release functions")
    assert shared == _block(install, "release functions") == _block(updater, "release functions")
    boot = _block(ct, "bootstrap verifier")
    assert boot == _block(install, "bootstrap verifier")
    assert f'{RAW}}}"' in boot  # from main, like the script itself - never from the release being checked
    assert "--require-hashes" in boot and "--only-binary :all:" in boot and "requirements-verify.txt" in boot
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > SHA256SUMS" in release
    # Nothing of the engine's unverified download and no checksum-only path is left.
    for path in (ct, install):
        text = path.read_text()
        assert "fetch_and_deploy_gh_release" not in text and "verify_release" not in text
        assert "deploy/companion-update\" --deploy" in text
    # The installed release is updated by its own updater, never by a script from a branch.
    assert "exec /usr/local/bin/companion-update" in ct.read_text()


def test_setup_refuses_a_downgrade_unless_asked(tmp_path):
    import subprocess

    setup = (ROOT / "deploy" / "setup.sh").read_text()
    guard = setup[setup.index('NEW_VERSION="$('):setup.index("# The release's own environment")]
    base = tmp_path / "base"
    (base / "app").mkdir(parents=True)
    (base / "venv" / "bin").mkdir(parents=True)
    (base / "venv" / "bin" / "wealthfolio-companion").write_text("#!/bin/sh\necho \"$(cat \"$0.version\")\"\n")
    (base / "venv" / "bin" / "wealthfolio-companion").chmod(0o755)
    installed = base / "venv" / "bin" / "wealthfolio-companion.version"

    def run(new: str, allow: str = "", old: str = "0.19.0") -> int:
        installed.write_text(old)
        (base / "app" / "pyproject.toml").write_text(f'[project]\nversion = "{new}"\n')
        script = f'set -euo pipefail\nBASE="{base}"\nAPP="$BASE/app"\n{guard}\necho ok'
        env = {"PATH": "/usr/bin:/bin", "COMPANION_ALLOW_DOWNGRADE": allow}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode

    assert run("0.19.0") == 0 and run("0.20.0") == 0 and run("0.19.10") == 0
    assert run("0.18.0") == 1 and run("0.9.9") == 1
    assert run("0.18.0", allow="1") == 0
    # PEP 440 pre-releases: 0.1.0a1 < 0.1.0a2 < 0.1.0b1 < 0.1.0rc1 < 0.1.0 (sort -V alone puts 0.1.0 first).
    order = ["0.1.0a1", "0.1.0a2", "0.1.0a10", "0.1.0b1", "0.1.0rc1", "0.1.0", "0.1.1a1"]
    for i, older in enumerate(order):
        for newer in order[i + 1:]:
            assert run(newer, old=older) == 0, f"{older} -> {newer}"
            assert run(older, old=newer) == 1, f"{newer} -> {older}"
    assert run("0.1.0a1", old="0.1.0a1") == 0
    assert "COMPANION_ALLOW_DOWNGRADE=1" in (ROOT / "ct" / "wealthfolio-companion.sh").read_text()


def test_the_old_script_name_only_explains_the_move():
    """Containers of Wealthfolio Broker Sync fetch ct/wealthfolio-broker-sync.sh from their `update` command (through
    the renamed repository's redirect): it must install nothing, say how to move (English and German) and fail."""
    import subprocess

    stub = ROOT / "ct" / "wealthfolio-broker-sync.sh"
    text = stub.read_text()
    assert "curl" not in text and "source" not in text and "build.func" not in text
    done = subprocess.run(["bash", str(stub)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert done.returncode == 1 and not done.stdout
    assert "Wealthfolio Companion" in done.stderr
    assert "#moving-from-wealthfolio-broker-sync" in done.stderr
    assert "Nothing was changed here." in done.stderr and "Hier wurde nichts geändert." in done.stderr
    # The guide it links to exists.
    assert "### Moving from Wealthfolio Broker Sync" in (ROOT / "CHANGELOG.md").read_text()


def test_the_updater_takes_pre_releases():
    import subprocess

    script = (ROOT / "deploy" / "companion-autoupdate").read_text()
    pick = re.search(r"sed -n '([^']+)'", script).group(1)
    cases = (("install 0.1.0", "0.1.0"), ("install 0.1.0a1", "0.1.0a1"), ("install 0.2.0rc12", "0.2.0rc12"),
             ("install 0.1.0x", ""), ("install ../evil", ""))
    for answer, version in cases:
        out = subprocess.run(["sed", "-n", pick], input=f"checked\n{answer}\n", capture_output=True, text=True).stdout
        assert out.strip() == version, answer


def test_a_pre_release_is_named_but_never_marked_as_one():
    """releases/latest - what the update check, the install and `update` read - skips GitHub pre-releases."""
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "--prerelease" not in release and "--latest=false" not in release
    assert 'TITLE="$TITLE (alpha)"' in release


def test_setup_takes_a_moved_key_left_as_secret_key(tmp_path):
    """The move from Wealthfolio Broker Sync (CHANGELOG 0.1.0a1): the old key put root-only into $ETC/secret.key, the
    new installation's key.cred removed - setup.sh seals that key with systemd-creds and hands it to the services."""
    import subprocess

    setup = (ROOT / "deploy" / "setup.sh").read_text()
    block = setup[setup.index("ETC=/etc/wealthfolio-companion"):setup.index("\nfor unit in ")]
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "secret.key").write_text("old-key-of-broker-sync-32-characters-long\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemd-creds").write_text(  # encrypt writes "<name>:<content>", decrypt checks the name
        '#!/bin/bash\ncase "$1" in\n has-tpm2) exit 1 ;;\n'
        ' encrypt) name="${3#--name=}"; { printf "%s:" "$name"; cat "$4"; } >"$5" ;;\n'
        ' decrypt) name="${2#--name=}"; c="$(cat "$3")"; [[ "$c" == "$name:"* ]] || exit 1\n'
        '   printf "%s\\n" "${c#*:}" ;;\n'
        'esac\n')
    (fake / "systemd-creds").chmod(0o755)
    script = f'set -euo pipefail\n{block.replace("ETC=/etc/wealthfolio-companion", f"ETC={etc}", 1)}\n' \
             'echo "$KEY_LINE $KEY_SOURCE"'
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={"PATH": f"{fake}:/usr/bin:/bin", "BASE": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert not (etc / "secret.key").exists()
    assert (etc / "key.cred").read_text() == "companion-key:old-key-of-broker-sync-32-characters-long\n"
    assert done.stdout.split()[-2:] == [f"LoadCredentialEncrypted=companion-key:{etc}/key.cred", "systemd-creds"]


def test_the_release_verifier_stands_alone_and_is_locked():
    """#165: install and update run provenance.py on its own, in a venv of requirements-verify.txt only."""
    source = (ROOT / "src" / "brokersync" / "provenance.py").read_text()
    assert not re.search(r"^\s*(from|import) (brokersync|\.)", source, re.M)
    lock = (ROOT / "requirements-verify.txt").read_text()
    assert re.search(r"^sigstore==\S+ \\", lock, re.M)
    blocks = re.split(r"\n(?=[A-Za-z0-9])", lock.strip())
    assert all("--hash=sha256:" in b for b in blocks if "==" in b)
    assert "requirements-verify.in" in (ROOT / "scripts" / "lock.sh").read_text()
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert re.search(r"subject-path: \|\n.*\.tar\.gz\n\s+SHA256SUMS\n", release)


def test_every_pip_install_is_hash_locked():
    """#155: no install path around the lock - every pip install of the scripts, the image and the workflows takes a
    lock file with hashes and wheels only, or is this package itself without dependencies and build isolation."""
    files = [ROOT / "Dockerfile", *ROOT.glob("ct/*.sh"), *ROOT.glob("install/*.sh"), *(ROOT / "deploy").iterdir(),
             *ROOT.glob(".github/workflows/*.yml")]
    seen = 0
    for path in files:
        if not path.is_file():
            continue
        for command in re.sub(r"\\\n", " ", path.read_text()).splitlines():
            if command.lstrip().startswith("#") or re.match(r"\s*PIP=\(", command):
                continue
            if re.search(r'pip"? install|"\$\{PIP\[@\]\}"', command):
                seen += 1
                locked = "--require-hashes" in command and "--only-binary :all:" in command and " -r " in command
                assert locked or "--no-deps --no-build-isolation" in command, f"{path.name}: {command.strip()}"
    assert seen >= 10


def test_the_image_base_is_pinned_by_digest_in_every_stage():
    """#155: Dependabot (docker) reads the FROM lines - both stages name the same base image by its digest."""
    bases = re.findall(r"^FROM (\S+)", (ROOT / "Dockerfile").read_text(), re.M)
    assert len(bases) == 2 and len(set(bases)) == 1 and re.search(r"@sha256:[0-9a-f]{64}$", bases[0])


def test_the_documented_actions_allow_list_covers_every_action_the_workflows_use():
    """A ruleset that allows only listed actions stops the whole run (startup_failure) if one is missing - also an
    action a quality-kit reusable workflow uses (zizmor-action)."""
    import re

    text = (ROOT / "docs" / "SECURITY-OPERATIONS.md").read_text()
    listed = re.search(r"the list `([^`]+)`", text).group(1)
    allowed = [x.strip().removesuffix("@*") for x in listed.split(",")]
    used = set(re.findall(r"uses: *([A-Za-z][\w-]*/[\w.-]+)", "".join(
        p.read_text() for p in (ROOT / ".github" / "workflows").glob("*.yml"))))
    used |= {"zizmorcore/zizmor-action", "actions/dependency-review-action"}  # inside quality-kit v1
    for action in sorted(used):
        owner = action.split("/")[0]
        if owner in ("actions", "github", "waldonso2"):  # created by GitHub / the owner's own
            continue
        assert any(action == a or (a.endswith("/*") and owner == a[:-2]) for a in allowed), action
