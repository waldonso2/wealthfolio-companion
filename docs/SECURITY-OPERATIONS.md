# Security operations: repository, releases and supply chain

For maintainers. What the repository files enforce, what only the GitHub settings can enforce (with the owner's
checklist), the dependency policy, and the risks that remain. Issues: #151 (SEC-05), #155 (SEC-09),
waldonso2/wealthfolio-importer-addon#150. User-facing release verification: [OPERATIONS.md](OPERATIONS.md#security).
Never put a secret, token or recovery code into this file or an issue - evidence here is settings and links only.

## 1. Workflows (inventory, 0.1.0a6)

Every workflow sets a read-only default at the top; a job that writes names each write scope with a comment.
No workflow uses `pull_request_target`, `workflow_run` or a self-hosted runner, and no workflow reads a repository
secret except the automatic `GITHUB_TOKEN`. Every `actions/checkout` sets `persist-credentials: false`. Every
action, reusable workflow and the quality-kit pin is a full commit SHA with its tag as a comment (Dependabot,
`github-actions`, monthly, 7-day cooldown). zizmor (CI job `workflows`, quality-kit) checks all of it on every push
and pull request; locally: `uvx zizmor --offline .github/workflows` (0 findings at the default persona).

| Workflow | Triggers | Job: permissions | Writes / hand-offs |
|---|---|---|---|
| `ci.yml` | push to `main`, `pull_request`, `workflow_call` (release), `workflow_dispatch`, weekly | all jobs `contents: read` (top level); reusable quality-kit jobs inherit it | none. Pull requests from forks run here with a read-only token and no secrets |
| `release.yml` | push to `main` only; `concurrency: release` (queued, never cancelled) | `ci`: read; `sbom`: read; `publish`: `contents`, `id-token`, `attestations`, `artifact-metadata` write, environment `release`; `image`: `packages`, `id-token`, `attestations`, `artifact-metadata` write, environment `release` | `sbom` → artifact `sbom` (1 day) → `publish` (data only, attested). `publish` creates tag + release, attestation of archive, `SHA256SUMS`, `sbom.cdx.json`; `image` pushes `ghcr.io/waldonso2/wealthfolio-companion`, cosign signature, provenance, BuildKit SBOM |
| `core-watch.yml` | weekly, `workflow_dispatch` | `watch`: `contents: read`, `issues: write` | opens one issue |
| `ishares-products.yml` | weekly, `workflow_dispatch` | `products`: `contents`, `pull-requests`, `issues`, `actions` write | pushes branch `ishares-products` (data file only), starts `ci.yml` there, opens a pull request; the token is only in the last step's environment |
| `scorecard.yml` | push to `main`, weekly, `workflow_dispatch` | top `read-all` (scorecard-action's own requirement), job: `security-events`, `id-token` write | SARIF to code scanning, results to OpenSSF |

Rules the release workflow keeps (#88, #151):

- A job with `id-token: write` can make a Sigstore certificate for `release.yml@refs/heads/main` - exactly what the
  installer trusts. So `publish` installs nothing: it uses the runner's git, `python3` (standard library) and `gh`
  plus pinned actions. Tools (cyclonedx-py) run in the read-only `sbom` job; their output reaches `publish` as data.
- The release is created only after the whole `ci.yml` passed on the same commit, only from `main`, and only when the
  version's tag does not exist. Releases are immutable (GitHub *immutable releases* is on: tag and assets can't be
  changed after publishing).
- `image` runs only for a newly created release (`needs.publish.outputs.created`).

`.github/CODEOWNERS` names the owner for the workflows, the installer (`ct/`, `install/`), the updater and units
(`deploy/`), the release verifier (`src/brokersync/provenance.py`, `scripts/pull-verified-image.sh`), the image
(`Dockerfile`), `sc_install.py` and the dependencies (`pyproject.toml`, `requirements*.txt`, `requirements*.in`,
`scripts/lock.sh`). It takes effect only with
the ruleset below.

## 2. GitHub settings

### State read on 30.10.2026 (public API, anonymous)

- `GET /repos/waldonso2/wealthfolio-companion/rules/branches/main`: ruleset "Rules main" (id 25310483, active,
  `~DEFAULT_BRANCH`): `deletion`, `non_fast_forward`, `required_status_checks` **with an empty list** - so no check
  is actually required, and there is no pull-request rule. A second ruleset "Rules" (id 25309883) is disabled.
  Bypass list as shown to an anonymous reader: empty.
- No tag ruleset. Releases are immutable (`"immutable": true` on v0.1.0a4 and v0.1.0a5).
- `security_and_analysis`: secret scanning **on**, push protection **on**, Dependabot security updates **on**;
  non-provider patterns and validity checks off.
- Environments: not readable anonymously; `release` is created by the first run of `release.yml` that names it -
  unprotected until the owner configures it (below).
- Addon repositories: `wealthfolio-importer-addon` has `deletion` and `non_fast_forward` only;
  `wealthfolio-companion-addon-xray` has no rule on `main`.

### Owner checklist

Repository `waldonso2/wealthfolio-companion` unless noted. Tick in #151; the result is visible afterwards in
`GET /repos/waldonso2/wealthfolio-companion/rules/branches/main` and the audit log.

1. **Ruleset `main`** - *Settings → Rules → Rulesets → Rules main* (edit the active one; delete the disabled
   "Rules"):
   - *Require a pull request before merging*: on. *Required approvals*: 0 while you are the only maintainer (you can't
     approve your own pull request); *Require review from Code Owners*: on; *Dismiss stale approvals*: on;
     *Require conversation resolution*: on.
   - *Require status checks to pass*: add `test (3.11)`, `test (3.13)`, `audit`, `scripts`, `docker`, `contract`,
     `workflows / zizmor`, `secrets / gitleaks`, `dependency-review / review` (*Add checks* lists them after one
     run; leave `performance` out - it only reports). *Require branches to be up to date*: optional.
   - *Block force pushes*, *Restrict deletions*: on (already).
   - *Bypass list*: only *Repository admin* with mode **For pull requests only** (lets you merge your own pull request
     that touches code-owned files; direct pushes stay blocked for everyone, and every bypass is in the audit log).
     No app, no integration, no deploy key in the list.
2. **Ruleset tags** - *New ruleset → New tag ruleset*, name `release tags`, target `v*`, enforcement active:
   *Restrict updates*, *Restrict deletions*, *Block force pushes*; bypass list empty. Creating a tag stays allowed,
   so the release workflow still works (check at the next release). Immutable releases already protect published
   ones; this also covers a tag without a release.
3. **Environment `release`** - *Settings → Environments → release* (exists after the next release run; or *New
   environment* `release` now):
   - *Deployment branches and tags*: *Selected branches and tags* → `main` only.
   - *Required reviewers*: `waldonso2`, *Prevent self-review* off (you approve your own release); the release then
     waits for your click in the run - publishing needs you, not only write access. Without reviewers, at least the
     branch rule.
   - No environment secrets are needed.
4. **Actions** - *Settings → Actions → General*:
   - *Actions permissions*: *Allow waldonso2, and select non-waldonso2, actions and reusable workflows* with
     *Allow actions created by GitHub* on and the list `docker/*, sigstore/cosign-installer@*, ossf/scorecard-action@*, zizmorcore/zizmor-action@*` (the last one runs
     in quality-kit's `workflows` job: without it CI ends in `startup_failure` and the required checks never start);
     *Require actions to be pinned to a full-length commit SHA*: on.
   - *Workflow permissions*: *Read repository contents and packages permissions* (the default token read-only; the
     workflows ask for more per job). *Allow GitHub Actions to create and approve pull requests*: on only if you want
     `ishares-products.yml` to open its pull request itself (otherwise it opens an issue); it can't approve anything
     a ruleset with code-owner review protects.
   - *Fork pull request workflows from outside collaborators*: *Require approval for all external contributors*.
   - *Artifact and log retention*: default.
5. **Code security** - *Settings → Advanced Security*: Dependabot alerts on, security updates on (already), version
   updates come from `.github/dependabot.yml`; CodeQL *Default setup* on (Python, GitHub Actions); secret scanning
   and push protection on (already), *Non-provider patterns* on; *Private vulnerability reporting* on (SECURITY.md).
6. **Packages** - *Profile → Packages → wealthfolio-companion → Package settings*: *Manage Actions access*: only this
   repository with role *Write*; visibility public; no other collaborators.
7. **Access** - *Settings → Collaborators and teams*: only you (admin). *Settings → Deploy keys*: none.
   *Settings → Webhooks*: only what you know. *Profile → Settings → Applications*: GitHub Apps (the Claude app) on
   *Only select repositories*; remove OAuth apps you don't use.
8. **Account** (waldonso2/wealthfolio-importer-addon#150, part 1) - *Profile → Settings → Password and
   authentication*: a passkey or security key, SMS removed, recovery codes offline; *Developer settings → Personal
   access tokens*: no classic tokens, fine-grained ones with expiry and single repositories; *Sessions* and *SSH and
   GPG keys*: nothing unknown; the e-mail account with a second factor.
9. The same rulesets 1-2 in `wealthfolio-importer-addon` and `wealthfolio-companion-addon-xray` (their CI job names).
   Deliberate difference: there *Require review from Code Owners* stays off - neither repository has a `CODEOWNERS`
   file, the addons are installed by hand from a release archive and nothing of them runs as root. Read on
   8 Nov 2026: *Dismiss stale approvals* and *Require conversation resolution* on in both, required checks `ci`,
   `contract`, `dependency-review / review`, `secrets / gitleaks`, `workflows / zizmor`, tag ruleset `v*` active.

## 3. Supply-chain and dependency policy (#155)

### Inputs

| Input | Pinned by | Install path | Updated by |
|---|---|---|---|
| Runtime Python packages (and setuptools) | `requirements.txt`: exact versions, SHA-256 hashes, every supported Python/platform (`uv pip compile --universal`, `scripts/lock.sh`) | `deploy/setup.sh`, `Dockerfile`, CI, `ishares-products.yml`: `--require-hashes --only-binary :all:`, then the package with `--no-deps --no-build-isolation` | Dependabot `pip` weekly (pytr by hand: diff read, contract case re-run) |
| Release verifier (`sigstore`) | `requirements-verify.txt`, constrained to `requirements.txt` | `ct/`, `install/`, `deploy/companion-update`: throwaway venv, same flags | Dependabot `pip` |
| CI tools (pytest, ruff, mypy, pip-audit, playwright, cyclonedx-bom) | `requirements-dev.txt`, constrained to both locks | CI, release `sbom` job: same flags | Dependabot `pip` |
| Container base image | `python:3.13-slim-trixie@sha256:…` in both `FROM` lines | `Dockerfile` | Dependabot `docker` weekly (digest/patch only; a new Python minor or Debian release by hand) |
| GitHub Actions, quality-kit reusable workflows | full commit SHA + tag comment | workflows | Dependabot `github-actions` monthly; quality-kit by moving the `v1` pin |
| Install engine `community-scripts/core` | commit in `ct/wealthfolio-companion.sh` | Proxmox host | `core-watch.yml` opens an issue; the pin moves after review |
| Scalable `sc` | latest release, minisign signature with Scalable's pinned key + SHA-256 (`sc_install.py`) | `setup.sh build`, `Dockerfile` | each install/update |
| cosign (user side, Docker) | 3.0.6 installer by SHA-256 (README) | the user's machine | by hand |
| Fonts | files in `src/brokersync/web/static/fonts/` with their licences | part of the release tree (`SHA256SUMS`) | by hand |

`tests/test_packaging.py` fails when a pip install in `Dockerfile`, `ct/`, `install/`, `deploy/` or a workflow
neither takes a lock with `--require-hashes --only-binary :all:` nor is this package with
`--no-deps --no-build-isolation`, when a runtime dependency of `pyproject.toml` is missing from the lock or has no
hash, and when the two `FROM` lines differ or lose the digest. pip's environment and configuration (`PIP_INDEX_URL`,
`pip.conf`) can change where a file comes from, never which file: with `--require-hashes` pip refuses anything whose
hash isn't in the lock, and `--only-binary :all:` means no source distribution's build code runs.

### Choosing, updating, auditing

- **New dependency:** only when the standard library or an existing dependency can't do it; maintained, with wheels
  for every platform in the lock (x86_64 and aarch64 Linux, Python 3.11-3.13), a licence compatible with MIT. Added
  in `pyproject.toml` (runtime) or the `.in` file, `scripts/lock.sh` in the same change.
- **Updates:** Dependabot pull requests (a release must be 7 days old: `cooldown`), CI must pass; a manual
  `scripts/lock.sh --upgrade` the same way. pytr and the install engine only after reading their diff.
- **Audit:** `pip-audit --require-hashes --disable-pip` of `requirements.txt`, `requirements-verify.txt` and
  `requirements-dev.txt` in CI job `audit` on every push, pull request and weekly (Monday 05:17 UTC). A finding in
  any of them **fails the build** - also a dev-only one, because the CI tools run in the release workflow. Triage:
  runtime (`requirements.txt`) or verifier → fix release; dev-only → update in the next change, no release needed.
  Dependency review on pull requests (quality-kit) flags new vulnerable dependencies. The image's OS packages are
  refreshed through the weekly Dependabot `docker` digest; there is no OS-package scanner in CI yet.
- **SBOM per release:** `sbom.cdx.json` - CycloneDX 1.6 JSON of `requirements.txt` (every runtime package of every
  platform with version, `pkg:pypi` purl and the locked hashes; root component `wealthfolio-companion` with the
  release version), made by `cyclonedx-py requirements` (cyclonedx-bom, locked in `requirements-dev.txt`) in the
  read-only `sbom` job, attached to the GitHub release and named in the release's build-provenance attestation.
  Check: `gh attestation verify sbom.cdx.json --repo waldonso2/wealthfolio-companion`. The container image carries
  BuildKit's SPDX SBOM as a registry attestation (`docker buildx imagetools inspect <image> --format
  '{{ json .SBOM }}'`).

### Exceptions

An accepted vulnerability or a deliberately unpinned input is listed here with the reason, the owner and a review
date, and the matching `pip-audit --ignore-vuln <id>` carries a comment pointing here. An entry past its date fails
review.

| Id / input | Reason | Owner | Review by |
|---|---|---|---|
| none | | | |

## 4. What the release verification does not cover (gap analysis of wealthfolio-importer-addon#150)

0.1.0a5 replaced #150's plan (minisign key, part 5) with Sigstore build provenance: install and update accept a
release only with an attestation of `release.yml@refs/heads/main` on a GitHub-hosted runner. What that changes:

- **Covered:** a changed or swapped release asset, a release built anywhere else (fork, other workflow, branch,
  self-hosted runner, laptop), a downgrade, a tampered download on the way, a stolen token without write access to
  `main`. The updater runs nothing downloaded before the check, and the check runs with the installed release's
  verifier, not with code from `main` (#150's part 5.2 - done).
- **Not covered: whoever controls `main` controls the identity.** The trust anchor is GitHub's OIDC identity of the
  workflow, not a key the owner holds. Someone who takes over the `waldonso2` account, or any credential that can
  merge to `main` and start the release (an admin token, an app with write access while no rule requires review),
  produces a release that verifies exactly like a real one, and every container with automatic updates installs it.
  #150 part 3's minisign key stored as a GitHub environment secret would not stop that either: the account owner can
  change the environment and the workflow. Only a signature made **outside GitHub**, by a key GitHub never sees,
  would - at the cost of a manual signing step per release.
- **Independent fingerprint (part 4):** with Sigstore there is no key fingerprint to publish; the identity string is
  public and cannot be faked, but it is also the thing an account takeover inherits. Publishing it elsewhere adds
  nothing. What a takeover can't hide is the **public Rekor log**: every attestation of `release.yml@main` is logged
  there with time and digest. Watching it from outside GitHub (e.g. sigstore's `rekor-monitor`, or a mail alert on
  new releases from a second account) detects a release you did not make.
- **The anchor of a new install** stays `ct/` from `main` over HTTPS (OPERATIONS.md, *Trust chain*); checking the
  files of a release tag first is documented there.

### Release watch by mail (second account, no code)

A release you did not make is the one thing a takeover cannot avoid: the workflow run, the GitHub release and the
Rekor entry all exist. The cheapest alarm lives **outside the account an attacker would take**:

1. Create (or use) a second GitHub account with its own e-mail address and a second factor of its own, not linked to
   `waldonso2` and not a collaborator with write access.
2. On `waldonso2/wealthfolio-companion`: **Watch → Custom → Releases**. GitHub then mails that account for every new
   release. Check *Settings → Notifications → Email* of that account (the *Watching* row must include e-mail).
3. Rule for yourself: every mail must match a release you started. One you did not start means: switch automatic
   updates off on your installations, check the attestation of the release against the Rekor log
   (`search.sigstore.dev`, search by the release's SHA-256), and secure the account (parts 1-2 of #150).

Not covered: someone who also takes the second account, or who deletes the release before the mail is read (the mail
stays). A Rekor-side monitor (sigstore's `rekor-monitor`, run on a host that is not GitHub) would add the log as
source; it is not built - it needs a machine of yours that runs it.

What reduces the remaining risk, in order of effort: parts 1-2 of #150 (account, rulesets - the checklist above), the
environment `release` with you as required reviewer, a Rekor/new-release watch outside GitHub, and - not built - a
delay before the automatic update installs a new release (a window to notice and pull it) or an owner-held offline
key signing `SHA256SUMS` next to the provenance.
