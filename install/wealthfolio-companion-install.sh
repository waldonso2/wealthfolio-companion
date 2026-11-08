#!/usr/bin/env bash

# Copyright (c) 2026 waldonso2
# License: MIT | https://github.com/waldonso2/wealthfolio-companion/raw/main/LICENSE
# Source: https://github.com/waldonso2/wealthfolio-companion

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing Dependencies"
$STD apt install -y python3 python3-venv
msg_ok "Installed Dependencies"

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
  url="$(curl -fsSI --retry 3 --retry-delay 2 --proto '=https' -o /dev/null -w '%{redirect_url}' \
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
  if ! curl -fsSL --retry 3 --retry-delay 2 --proto '=https' "$base/SHA256SUMS" -o "$dir/SHA256SUMS" ||
    ! curl -fsSL --retry 3 --retry-delay 2 --proto '=https' "$base/$name" -o "$dir/$name"; then
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
    curl -fsSL --retry 3 --retry-delay 2 --proto '=https' "$main/src/brokersync/provenance.py" -o "$dir/provenance.py" &&
    curl -fsSL --retry 3 --retry-delay 2 --proto '=https' "$main/requirements-verify.txt" -o "$dir/requirements-verify.txt" &&
    python3 -m venv "$dir/venv" &&
    "$dir/venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir --require-hashes \
      --only-binary :all: -r "$dir/requirements-verify.txt"
}
# <<< bootstrap verifier

STAGE="$(mktemp -d -p /var/tmp)" # on disk: /tmp may be a small tmpfs
msg_info "Verifying the latest release"
if ! VERSION="$(latest_release)" || ! bootstrap_verifier "$STAGE/verify" ||
  ! fetch_release "$VERSION" "$STAGE/release" "$STAGE/verify/venv/bin/python" "$STAGE/verify/provenance.py"; then
  rm -rf "$STAGE"
  msg_error "The release's provenance or its checksums could not be verified - details above. Installation stopped."
  exit 1
fi
msg_ok "Verified release ${VERSION}"
if [[ ! -f "$STAGE/release/app/deploy/companion-update" ]]; then
  rm -rf "$STAGE"
  msg_error "Release ${VERSION} is older than the release layout (0.1.0a5) - it can't be installed this way."
  exit 1
fi

msg_info "Installing Wealthfolio Companion ${VERSION}"
if ! $STD bash "$STAGE/release/app/deploy/companion-update" --deploy "$STAGE/release/app" "$VERSION"; then
  rm -rf "$STAGE"
  msg_error "Installing failed - details above."
  exit 1
fi
rm -rf "$STAGE"
msg_ok "Installed and started Wealthfolio Companion ${VERSION}"

motd_ssh
customize

cleanup_lxc
