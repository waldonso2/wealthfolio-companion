#!/usr/bin/env bash
# Install: run in the Proxmox host shell
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main/ct/wealthfolio-companion.sh)"
# Update: run `update` in the container (it runs the installed companion-update; see update_script).
#
# The engine comes from community-scripts/core; COMMUNITY_SCRIPTS_URL points it
# at this repository, so it loads install/wealthfolio-companion-install.sh
# from here. This script from main is the trust anchor of a new install (docs/OPERATIONS.md); once installed,
# `update` runs the installed, verified updater and nothing comes from a branch again (#146).
# The engine runs as root on the Proxmox host: it is pinned to a reviewed commit, not
# core's moving main branch, and set every run (the `update` command keeps the URL of
# the run that wrote it). .github/workflows/core-watch.yml reports newer commits.
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main}"
export COMMUNITY_SCRIPTS_CORE_URL="https://raw.githubusercontent.com/community-scripts/core/5514fdfd6c3fe5fb74d91a42863c807a99c617a5"
source <(curl -fsSL "$COMMUNITY_SCRIPTS_CORE_URL/core/build.func")
# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-companion/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-companion

APP="Wealthfolio-Companion"
var_tags="${var_tags:-finance;wealthfolio}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-2}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"
# Trust boundary (docs/OPERATIONS.md, #154): unprivileged, no bind mount, device, FUSE or TUN. Nesting stays on (the
# engine's default): Proxmox advises it for systemd 257 (Debian 13), and the units' namespace sandboxing needs it.
var_nesting="${var_nesting:-1}"
var_fuse="${var_fuse:-no}"
var_tun="${var_tun:-no}"
var_gpu="${var_gpu:-no}"

# The engine's banner says "Community Scripts" for a script it has no header of its own for (core.func
# get_header); the fallback is a plain variable read by header_info, so it is set here, after build.func.
_CS_FALLBACK_HEADER='
  ╭───────────────────────────╮
  │   Wealthfolio Companion   │
  ╰───────────────────────────╯'

header_info "$APP"
variables
color
catch_errors

# >>> release functions: the same in ct/wealthfolio-companion.sh, install/wealthfolio-companion-install.sh and
# deploy/companion-update (test_packaging compares them). Nothing downloaded is unpacked, sourced or run before
# fetch_release has verified it.
# valid_version <v>: a PEP 440 release or pre-release (0.1.0, 0.1.0a5, 1.2.3rc1) - nothing else reaches a URL or path.
valid_version() {
  [[ "$1" =~ ^[0-9]+(\.[0-9]+){1,3}((a|b|rc)[0-9]+)?$ ]]
}
# latest_release: the version GitHub's releases/latest redirects to (no API, no token).
latest_release() {
  local url
  url="$(curl -fsSI --retry 6 --proto '=https' -o /dev/null -w '%{redirect_url}' \
    https://github.com/waldonso2/wealthfolio-companion/releases/latest)" || return 1
  url="${url#https://github.com/waldonso2/wealthfolio-companion/releases/tag/v}"
  valid_version "$url" || { echo "GitHub named no valid latest release." >&2; return 1; }
  printf '%s\n' "$url"
}
# fetch_release <version> <dir> <python> <provenance.py>: into the new, private <dir>: SHA256SUMS and the release
# archive, both proven by the release workflow's attestation (#165) - only then unpacked to <dir>/app, whose files
# must be exactly those of SHA256SUMS (#88).
fetch_release() {
  local version="$1" dir="$2" python="$3" verifier="$4" name base ok=0
  if ! valid_version "$version"; then
    echo "Refused: '${version//[^A-Za-z0-9._-]/?}' is not a version." >&2
    return 1
  fi
  name="wealthfolio-companion-$version.tar.gz"
  base="https://github.com/waldonso2/wealthfolio-companion/releases/download/v$version"
  (umask 077 && mkdir "$dir") || return 1
  if ! curl -fsSL --retry 6 --proto '=https' "$base/SHA256SUMS" -o "$dir/SHA256SUMS" ||
    ! curl -fsSL --retry 6 --proto '=https' "$base/$name" -o "$dir/$name"; then
    echo "Downloading release $version failed." >&2
    return 1
  fi
  XDG_CACHE_HOME="$dir" XDG_DATA_HOME="$dir" "$python" -I "$verifier" "$dir/SHA256SUMS" "$dir/$name" || return 1
  mkdir "$dir/app" && tar -xzf "$dir/$name" -C "$dir/app" --strip-components=1 --no-same-owner || return 1
  (cd "$dir/app" && sha256sum --quiet --strict -c ../SHA256SUMS) || ok=1
  diff <(cd "$dir/app" && find . -type f | LC_ALL=C sort) <(cut -c67- "$dir/SHA256SUMS" | LC_ALL=C sort) >/dev/null ||
    { echo "The release archive holds files SHA256SUMS doesn't list." >&2; ok=1; }
  return "$ok"
}
# <<< release functions

# >>> bootstrap verifier: the same in ct/ and install/ (test_packaging compares them).
# bootstrap_verifier <dir>: a verifier for a container that has none yet (a new install, or one installed before
# the release layout): provenance.py and its hash-locked requirements from this repository's main branch - the
# same source and moment as this script itself (the trust anchor, docs/OPERATIONS.md) - never from the release
# being checked. Afterwards every update uses the verifier of the installed, verified release.
bootstrap_verifier() {
  local dir="$1" main="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main}"
  (umask 077 && mkdir "$dir") &&
    curl -fsSL --retry 6 --proto '=https' "$main/src/brokersync/provenance.py" -o "$dir/provenance.py" &&
    curl -fsSL --retry 6 --proto '=https' "$main/requirements-verify.txt" -o "$dir/requirements-verify.txt" &&
    python3 -m venv "$dir/venv" &&
    "$dir/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --require-hashes \
      --only-binary :all: -r "$dir/requirements-verify.txt"
}
# <<< bootstrap verifier

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -e /opt/wealthfolio-companion/app ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi
  # Installed with the release layout: the installed, verified updater does everything (setup.sh points `update`
  # straight at it; this only runs when the engine's entry point is called).
  if [[ -x /usr/local/bin/companion-update && -L /opt/wealthfolio-companion/current ]]; then
    exec /usr/local/bin/companion-update
  fi

  # Once, for an installation from before the release layout (0.1.0a4 and older): verify the release with the
  # bootstrap verifier, then let the verified release's own updater put it in place (staged, health-checked,
  # rolled back on failure). VERSION=0.19.0 update: that version on purpose, also an older one.
  local version installed stage rc=0
  version="${VERSION:-}"
  if [[ -z "$version" ]] && ! version="$(latest_release)"; then
    msg_error "The latest release could not be found."
    exit 1
  fi
  installed="$(/opt/wealthfolio-companion/venv/bin/wealthfolio-companion --version 2>/dev/null || true)"
  if [[ -z "${VERSION:-}" && "$version" == "$installed" ]]; then
    msg_ok "No update required. ${APP} is already at v${installed}"
    exit
  fi
  [[ -n "${VERSION:-}" ]] && export COMPANION_ALLOW_DOWNGRADE=1
  stage="$(mktemp -d -p /var/tmp)"  # on disk: /tmp may be a small tmpfs
  msg_info "Verifying release ${version}"
  if ! bootstrap_verifier "$stage/verify" ||
    ! fetch_release "$version" "$stage/release" "$stage/verify/venv/bin/python" "$stage/verify/provenance.py"; then
    rm -rf "$stage"
    msg_error "Update stopped: the release's provenance or its checksums could not be verified - details above. The previous version keeps running."
    exit 1
  fi
  msg_ok "Verified release ${version}"
  if [[ ! -f "$stage/release/app/deploy/companion-update" ]]; then
    rm -rf "$stage"
    msg_error "Release ${version} is older than the release layout (0.1.0a5) - it can't be installed this way."
    exit 1
  fi
  bash "$stage/release/app/deploy/companion-update" --deploy "$stage/release/app" "$version" || rc=$?
  rm -rf "$stage"
  if ((rc != 0)); then
    msg_error "Update failed - the previous version keeps running. Details above."
    exit 1
  fi
  msg_ok "Updated successfully!"
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
# The one-time code for the first password (only this console shows it): without it nobody else in the
# network can choose the password before you do.
# stdin from /dev/null: the engine may run this with stdin closed, and systemd-run --pipe then fails with 208
# (EXIT_STDIN); a missing code must never fail the finished install - the line below says how to get it.
SETUP_CODE="$( (pct exec "$CTID" -- /usr/local/bin/companion-cli setup-code </dev/null 2>/dev/null || true) | sed -n 's/^Setup code.*: //p')"
echo -e "${INFO}${YW}Open the web UI and choose its password (the certificate comes from the container's own CA: confirm the browser warning once, or import /ca.crt):${CL}"
HOST="$IP"
[[ "$HOST" == *:* ]] && HOST="[$HOST]"  # an IPv6 address goes in brackets in a URL
echo -e "${GATEWAY}${BGN}https://${HOST}:8443/setup-password?code=${SETUP_CODE}${CL}"
echo -e "${INFO}${YW}Setup code: ${SETUP_CODE:-run companion-cli setup-code in the container} (valid 24 h)${CL}"
