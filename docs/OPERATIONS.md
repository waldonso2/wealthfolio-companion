# Operations: Updates, Security, Backups, and Technical Details

Back to the [README](../README.md).

## Updates

**Manual update:** Open the container console in Proxmox and run `update`. Settings and credentials are preserved. `update` runs the installed updater `companion-update`, which works in this order and stops at the first failure:

1. The version (the latest release, or `VERSION=<version> update`) must be a plain PEP 440 version (`0.1.0`, `0.1.0a5`); anything else is refused before any download.
2. `SHA256SUMS` and the release archive are downloaded into a private directory (mode 700) and verified (see *Release verification*). Nothing is unpacked before that; afterwards every unpacked file must match `SHA256SUMS`, with no extra file.
3. The release is built next to the running one in `/opt/wealthfolio-companion/releases/<version>-<id>` (its own venv and verifier). The running version is untouched until here: a failure leaves it as it was.
4. The service stops, and the encrypted data is backed up to `/opt/wealthfolio-companion/backup-*.tar.gz` (mode 600, the three most recent are kept).
5. The symlink `/opt/wealthfolio-companion/current` is switched to the new release in one rename (`app` and `venv` point through it), then units, commands and the data migration are set up.
6. Health check: the new program reports the new version, the service is active and `https://127.0.0.1:8443/healthz` answers `ok` within two minutes.
7. Commit (older releases are removed) - or roll back: the previous release and the data from the backup are put back, configured and started again; `update` exits with an error and the automatic update reports it via ntfy.

Only one update runs at a time (`flock` on `/run/wealthfolio-companion-update.lock`; a second one exits with status 75), and none while a sync runs. If the container stops in the middle (power cut, reboot): unfinished downloads and builds are removed at the next update, and an update that already switched but was never found healthy is undone - at the next boot by `wealthfolio-companion-update-recover.service`, or by the next `update` - so the result is always one complete release, never a mixture. `companion-update --recover` does the same by hand. To go back to an older version on purpose, run `VERSION=<version> update`; without `VERSION`, updates never downgrade.

**New-release notifications:** After a sync, the service checks GitHub once per day for the latest release (it only follows the `releases/latest` redirect; no data is sent). If a newer release exists, the *Broker* page shows it and ntfy sends one notification. Disable this with `Environment=COMPANION_UPDATE_CHECK=0` via `systemctl edit wealthfolio-companion-run`.

**Automatic updates (optional, *Updates* page):** When enabled, the container checks for a new release at the configured interval (minimum one minute, maximum once per day) and installs it with the same `companion-update` as `update` (verification, staging, backup, health check and rollback as described above). ntfy reports the installed version, and the *Updates* page shows the latest result. If an update fails, the previous version continues running (or is rolled back to) and the same version is not retried for an hour. Updates do not run during a sync or while an update from the console runs. The container timer `wealthfolio-companion-update.timer` runs every minute as root because it performs the same operation as `update`; when disabled, it installs nothing. Enabling or disabling it requires the UI password. Inspect its logs with `journalctl -u wealthfolio-companion-update` in the console.

## Security

Stored data is encrypted. The **Security** page always shows what is protected and how; from the console, run `companion-cli security-status`.

| Data | Protection |
|---|---|
| Credentials, sessions, tokens, Wealthfolio password, logo.dev key | AES-256-GCM (`data/secrets.enc`) |
| Settings (accounts, securities, transfers with IBANs) | AES-256-GCM (`data/config.enc`) |
| Sync database (bookings, holdings, runs) | SQLCipher, AES-256 (`data/state.db`) |
| ntfy settings | AES-256-GCM (`data/notify.enc`), using only the service key so notifications can be sent while the service is locked |
| Service key | **Not** in `data/`: encrypted with `systemd-creds` at `/etc/wealthfolio-companion/key.cred` (root-only); systemd provides it at startup |
| UI password | argon2id; 15-minute pause after five failed attempts, and the same pause after 30 failed attempts across all clients; at most two checks run simultaneously to limit memory exhaustion. The initial password requires the one-time setup code from the console |
| UI | HTTPS on port 8443 using a certificate issued by the container's own CA. Import the CA once from the *Security* page to trust it in your browser. Its name constraints permit only local-network names; its key is root-only in `/etc/wealthfolio-companion/tls/`. An existing certificate from another issuer (`cert.pem`/`key.pem`) is preserved. Secure cookies and strict security headers are also used |
| Logs, notifications, and error messages | Credentials, tokens, IBANs, and account numbers are redacted in logs, ntfy messages, and UI errors. Request URLs are not logged. ntfy messages omit amounts unless *Details* is enabled |
| Service | Dedicated user and systemd hardening (write access only to `data/`, no privilege escalation, restricted system calls; `systemd-analyze security`: 1.5; see *Container, services and network*); files readable only by the service; daily sync ends within 45 minutes |

### Container, services and network (Proxmox LXC)

**Trust boundary.** The Proxmox host is trusted completely: its root can read the container's disk and memory, and with them the key and the data (only the master passphrase protects against that, or against a stolen container backup). The container is what the service protects itself from the network and from its own programs:

| Container setting (`ct/wealthfolio-companion.sh`, the pinned engine's defaults) | Value |
|---|---|
| Unprivileged (`var_unprivileged=1`) | Yes: root in the container is an unprivileged, mapped user on the host |
| Nesting (`var_nesting=1`) | On: Proxmox advises it for systemd 257 (Debian 13), and the units' sandboxing (mount namespaces) needs it. It lets the container mount its own `/proc` and `/sys`, still inside its user namespace |
| keyctl | On: the engine sets it for every unprivileged container; the service does not use the kernel keyring |
| FUSE, TUN, GPU, `mknod`, extra mount types | Off |
| Bind mounts, host paths, devices, Proxmox API or sockets, Docker socket | None |
| Start at boot | Yes |

**Services inside the container.** The service user `companion` owns nothing but `data/`; its program under `/opt/wealthfolio-companion` (`releases/`, `current`, `app`, `venv`, `bin/sc`) is root's and only readable for it. Secrets reach it as systemd credentials (the service key, the UI's TLS key), so their files stay root-only.

| Unit | Runs as | May write | Network | `systemd-analyze security` |
|---|---|---|---|---|
| `wealthfolio-companion.service` (UI) | `companion` | `data/`, `/run/wealthfolio-companion` (unlock cache, RAM), a private `/tmp` | IPv4/IPv6; listens on 8443 and 8090 | 1.5 |
| `wealthfolio-companion-run.service` (daily sync) | `companion` | the same | IPv4/IPv6, outbound | 1.5 |
| `wealthfolio-companion-cert.service` | root (writes the root-only TLS files) | `/etc/wealthfolio-companion/tls` | no IP traffic (`IPAddressDeny=any`; netlink for its addresses, `systemctl` to restart the UI) | 1.7 |
| `wealthfolio-companion-update.service`, `-update-recover.service` | root | the system (it installs releases, units, commands) | outbound HTTPS | 6.1 |

The `companion` units and the certificate's renewal have no capabilities, `NoNewPrivileges`, `ProtectSystem=strict` (read-only except the paths above), `ProtectHome`, `PrivateTmp`, `PrivateDevices`, protected kernel tunables, modules, logs, cgroups, clock and hostname, `ProtectProc=invisible`, no namespaces, SUID/SGID or realtime, native system calls of `@system-service` only (plus `@sandbox`: the Landlock sandbox Scalable's `sc` runs in) and `UMask=0077`. Not set, on purpose: `PrivateNetwork`/`IPAddressAllow` on the sync and UI (brokers, Wealthfolio and issuers can be anywhere), `MemoryDenyWriteExecute` and `PrivateUsers` (not proven with every library and inside an unprivileged LXC). The updaters stay root with a writable system - that is their job - but get a private `/tmp`, protected kernel tunables, modules, logs, cgroups and hostname, no SUID/SGID files, native system calls, Unix/netlink/IP sockets only and no `CAP_SYS_ADMIN`, `CAP_SYS_MODULE`, `CAP_SYS_PTRACE`, `CAP_SYS_RAWIO`, `CAP_NET_ADMIN`, `CAP_NET_RAW`, `CAP_BPF`, `CAP_MKNOD` and similar; they keep devices (`systemd-creds` may use a TPM) and the file system. They only install releases that passed *Release verification*. `tests/test_packaging.py` asserts these settings and, where systemd is installed, the scores; in the container, `systemd-analyze security <unit>` shows them.

| Path | Owner, mode |
|---|---|
| `/opt/wealthfolio-companion` and the program in it | root, 0755 (readable, not writable by the service) |
| `/opt/wealthfolio-companion/data` | `companion`, 0700; files 0600 |
| `/opt/wealthfolio-companion/backup-*.tar.gz` (before each update) | root, 0600 |
| `/etc/wealthfolio-companion` (key, `tls/`) | root, 0700; `key.cred`/`secret.key` and the TLS key 0600 |
| `/run/wealthfolio-companion` | `companion`, 0700, in RAM |
| `/var/lib/wealthfolio-companion-update` (the automatic update's state) | root, 0700 |
| Logs | journald (`journalctl -u wealthfolio-companion`), secrets redacted |

**Network.** Inbound: TCP 8443 (the UI, HTTPS) and 8090 (only a redirect to 8443); the service listens on nothing else. The Debian template's own SSH server may answer on 22 (root logins by password only if you enabled SSH root access when creating the container); `ss -tlnp` in the container lists what listens. The UI answers only to the container's addresses and known names. Outbound, all HTTPS unless you configure otherwise:

- the brokers and banks you set up (their APIs and FinTS servers, Enable Banking for PSD2 banks, Trade Republic through pytr, Scalable through `sc`), your Wealthfolio (its address and port), your ntfy server if set up;
- the ETF issuers' sites and Finanzfluss (the ISINs of your funds), logo.dev only when logos are on, ESMA's FIRDS register (`registers.esma.europa.eu`, ISINs) only when *Ask ESMA's register for the asset class* is on;
- GitHub: the daily update check (`github.com`), the `sc` download, and for an update the release files (`github.com` and its download hosts), the attestation (`api.github.com`) and the locked Python packages (`pypi.org`, `files.pythonhosted.org`);
- DNS.

*Settings → About* lists the same destinations. The container gets an address on the bridge chosen at creation (the LAN by default) and nothing is forwarded to it from the Internet: keep it that way. For access from outside, use a VPN or a reverse proxy (README, *Behind a reverse proxy*), never a port forward to 8443. With the Proxmox firewall on for the container, allow inbound 8443/8090 from your LAN only, outbound DNS and 443 (plus the ports of Wealthfolio and ntfy if they differ).

**Optional master passphrase (*Security* page):** Without it, someone who copies the entire container and its key (for example, a Proxmox backup) can decrypt the data. With it, they cannot: the effective key is derived from both the service key and the passphrase using argon2id. You must unlock the service in the UI after each container restart; until then it does not sync and sends an ntfy notification. **If you lose the passphrase, the data is lost** and the service must be set up again. Setting, changing, or removing the passphrase (and key rotation during updates) re-encrypts everything: first alongside the old files, then switches over atomically. If power is lost, the next start recovers either the old or new state, never a mixture.

**Release verification (Proxmox install and update):** A release is accepted only when both hold:

1. `SHA256SUMS` and the release archive `wealthfolio-companion-<version>.tar.gz` carry the SLSA build provenance that `.github/workflows/release.yml` on `main` created for them, on a GitHub-hosted runner (Sigstore certificate identity `https://github.com/waldonso2/wealthfolio-companion/.github/workflows/release.yml@refs/heads/main`, issuer `https://token.actions.githubusercontent.com`; the attestation must name both files with exactly their SHA-256).
2. Every unpacked file matches `SHA256SUMS`, and there is no extra file.

No GitHub login is needed. The attestation comes from GitHub's public API (`api.github.com/repos/waldonso2/wealthfolio-companion/attestations/sha256:<digest>`, 60 unauthenticated requests per hour per address; when it refuses, the update says so and stops, and you retry later). The signature is checked **offline** against the Sigstore trusted root shipped in the pinned `sigstore` package: nothing is fetched from Sigstore's TUF server. The verifier is `src/brokersync/provenance.py` in a virtual environment installed from `requirements-verify.txt` (exact versions, SHA-256 hashes, wheels only). The check fails closed: no attestation, another signer (workflow, branch, repository, issuer, self-hosted runner), another digest, an invalid version, a download, `pip` or API failure all stop the install or update before anything downloaded runs; there is no checksum-only fallback.

*Trust chain.* Where each piece comes from:

| Step | Comes from | Trusted because |
|---|---|---|
| `ct/wealthfolio-companion.sh` on the Proxmox host | `raw.githubusercontent.com/.../main` (the one-line install) | **The trust anchor**: HTTPS from this repository's protected `main` branch - the same as any `curl \| bash` installer. You can check it first (below). |
| The install engine (`community-scripts/core`) | A pinned, reviewed commit | Immutable commit hash, set by the script above. |
| `install/wealthfolio-companion-install.sh`, the bootstrap verifier (`provenance.py`, `requirements-verify.txt`) | The same `main`, at the same moment, as the script above | Same anchor; never taken from the release being checked, so a tampered release cannot bring its own verifier. |
| The release | GitHub release assets | Verified by that verifier before it is unpacked. |
| Every later update | The installed `companion-update` and the verifier of the installed release | Both were verified as part of that release. `update` in the console calls `companion-update` directly; nothing comes from a branch again. |

A container installed before 0.1.0a5 does its first update once more through the engine's `update` (the current `ct/` script from `main` and the bootstrap verifier); that update installs `companion-update` and points `update` at it. A release only proves that it was built by the release workflow on `main`; it does not tell which version it is, so the updater still refuses a downgrade. If Sigstore ever rotates its keys, verification fails closed; then run `COMPANION_VERIFY_ONLINE=1 update` once: the verifier refreshes its trusted root through Sigstore's TUF repository (`tuf-repo-cdn.sigstore.dev`, signed from the root shipped in the package - nothing from `main`), and the release it installs brings a current root along. The Docker image is verified separately (cosign signature and provenance in the registry).

*Optional: check the installer before you run it.* The one-line install trusts `main`. To trust only an attested release instead, download its files from the release tag and compare them with the release's attested `SHA256SUMS` before running anything (on the Proxmox host):

```bash
V=0.1.0a5
B=https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/v$V
mkdir wfc && cd wfc
curl -fsSLO "https://github.com/waldonso2/wealthfolio-companion/releases/download/v$V/SHA256SUMS"
for f in ct/wealthfolio-companion.sh install/wealthfolio-companion-install.sh src/brokersync/provenance.py requirements-verify.txt; do
  mkdir -p "$(dirname "$f")" && curl -fsSL "$B/$f" -o "$f"
done
grep -E ' \./(ct/wealthfolio-companion\.sh|install/wealthfolio-companion-install\.sh|src/brokersync/provenance\.py|requirements-verify\.txt)$' SHA256SUMS | sha256sum -c
sha256sum SHA256SUMS
```

Then confirm that `SHA256SUMS` is the one the release workflow attested: on any machine with the GitHub CLI (needs a GitHub login), `gh attestation verify SHA256SUMS --repo waldonso2/wealthfolio-companion --signer-workflow waldonso2/wealthfolio-companion/.github/workflows/release.yml`, or compare the digest with the attestation listed on the repository's *Attestations* page. Only then run `COMMUNITY_SCRIPTS_URL=$B bash ct/wealthfolio-companion.sh`: the engine and the installer then fetch the install script and the verifier from that tag instead of `main`. Residual risk: the engine downloads those files again itself (you checked your copies, not its download), and a tag can be moved by someone with write access to the repository; the release the installer then installs is verified as described above either way.

*For maintainers:* workflow permissions, the GitHub settings checklist, the dependency policy (SBOM per release) and what this verification does not protect against (a takeover of the GitHub account) are in [SECURITY-OPERATIONS.md](SECURITY-OPERATIONS.md).

**Events:** The *Security* page lists logins (including failures), logouts, and changes to credentials, password, passphrase, and sync mode for the last 180 days, including the source address but no passwords or credentials. A login from an address never seen before triggers an ntfy notification.

**Container image trust chain (Docker, Podman, Unraid):** `.github/workflows/release.yml` builds the image only for a new release, after the whole CI, and pushes it to `ghcr.io/waldonso2/wealthfolio-companion` as `v<version>` (and `latest`, which the documentation never uses). The same job signs the image's digest with cosign (keyless: a short-lived Sigstore certificate for the workflow's GitHub OIDC token, logged in Rekor) and attaches GitHub's SLSA build provenance (`https://slsa.dev/provenance/v1`) to it in the registry. `scripts/pull-verified-image.sh <version>` checks that chain before anything runs:

1. The version must be a release version (`X.Y.Z`, `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`); `latest` is refused.
2. The tag `v<version>` is resolved to its digest once (anonymously, ghcr.io); everything after that uses `image@sha256:…` only, so a tag moved later changes nothing.
3. `cosign verify` and `cosign verify-attestation --type slsaprovenance1` must both succeed for that digest with exactly this policy: certificate identity `https://github.com/waldonso2/wealthfolio-companion/.github/workflows/release.yml@refs/heads/main`, OIDC issuer `https://token.actions.githubusercontent.com`, repository `waldonso2/wealthfolio-companion`, ref `refs/heads/main`, trigger `push`. A signature from another repository, workflow, branch or issuer, a missing signature or provenance, or an unreachable registry ends the script with an error and no output.
4. Only then it prints the reference (or writes it into `.env` as `COMPANION_IMAGE`). It never pulls, stops or replaces a container itself; the documented update commands pull the new image before the old container is removed or recreated, so a failed check or a registry outage leaves the running container as it is. The Compose file refuses to start without `COMPANION_IMAGE`; there is no default.

cosign 3 is required (the documented install pins 3.0.6 with the SHA-256 of sigstore's own installer, the version the release workflow signs with); cosign fetches Sigstore's trust root itself (TUF). The script is downloaded from this repository's `main`; read it before running it if that matters to you. The Wealthfolio image in the example Compose file is Wealthfolio's own and is not covered by this check – pin and verify it as its project describes.

## Backup and restore

Before updating, `update` backs up `data/` to `/opt/wealthfolio-companion/backup-*.tar.gz`—encrypted data only, without the key. You can also create a backup manually from the container console:

```bash
(umask 077; companion-cli backup > /root/companion-$(date +%F).tar.gz)
systemctl stop wealthfolio-companion
companion-cli restore < /root/companion-2026-10-15.tar.gz
systemctl start wealthfolio-companion
```

`restore` first verifies that the archive can be opened with this container's key (and the master passphrase, if configured). Otherwise, it changes nothing. The previous state is kept as `data/before-restore-*.tar.gz`. Update backups can also be restored this way.

A backup is **complete only when paired with the key** from `/etc/wealthfolio-companion`; without the key, `data/` is useless. The easiest full backup is a Proxmox backup of the entire container. It includes both key and data, so store it on an encrypted target or configure the master passphrase.

**Sync mode (*Security* page):**
- **Automatic** (default): broker and bank sessions remain saved and the sync runs every morning.
- **Secure:** each sync starts with a fresh login—app approval, TAN, or device code. The service logs out immediately afterwards and deletes the session, so no reusable login remains.
  - The scheduled run does not sync; it reminds you via ntfy.
  - **Fetch all** walks you through the logins one by one (you can skip individual brokers) and then syncs all of them.
  - The mode applies to all brokers.

**Do not save PIN (DKB, Deutsche Bank):** Leave the PIN field empty in broker settings to be prompted for the PIN on every login. It remains in memory only until the sync ends and is never stored. Each sync then requires a login, even in automatic mode; the scheduled run cannot fetch this bank. To delete a previously saved PIN, use the checkbox below the PIN field.

**Wealthfolio:** Prefer connecting to Wealthfolio over HTTPS; otherwise the Wealthfolio password travels over the network unencrypted. The *Security* page warns you about this.

## Technical details

For contributors: [ARCHITECTURE.md](../ARCHITECTURE.md) explains the structure and flow; [CLAUDE.md](../CLAUDE.md) describes the rules and how to add a broker.

- Python service `companion` (FastAPI UI over HTTPS on port 8443; port 8090 redirects; `wealthfolio-companion run` for the systemd timer) at `/opt/wealthfolio-companion`. Encrypted data lives in `/opt/wealthfolio-companion/data`; key and certificate in `/etc/wealthfolio-companion`. Runs as the dedicated `companion` user. Commands requiring the service key use `companion-cli <command>`.
- Wealthfolio is accessed through its REST API (`/api/v1`) using the Wealthfolio password.
- Scalable's CLI `sc` is installed at `/opt/wealthfolio-companion/bin/sc`; `wealthfolio-companion install-sc` installs or updates it (the install and update processes call it).
- Dependencies are installed only at the versions in `requirements.txt`, each checked against a hash; the community-scripts install engine is pinned to a reviewed revision. Report security issues privately; see [SECURITY.md](../SECURITY.md).
- Releases: a new version becomes installable after the version in `pyproject.toml` is bumped and merged into `main`. The release workflow runs the full CI first, then creates the GitHub release used by installation and updates. Each release contains the source archive, `SHA256SUMS` (all files), a CycloneDX SBOM of the runtime dependencies (`sbom.cdx.json`), and GitHub SLSA build provenance for all three, which installation and updates verify themselves (see *Release verification*). To check by hand: `gh attestation verify wealthfolio-companion-<version>.tar.gz --repo waldonso2/wealthfolio-companion` (needs a GitHub login). Published releases cannot be changed.
