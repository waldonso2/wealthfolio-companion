#!/usr/bin/env bash
# SEC-12 (#158) for the Docker image, scenarios 12-14 and 16-17 of the issue, on a DISPOSABLE Docker host: install
# a verified release's image, refuse everything else, update, back up and restore, check the hardening. Never run it
# where real credentials exist: it creates and deletes its own containers and volumes (project sec12docker).
#
# A Docker host on Proxmox (an unprivileged LXC with nesting and keyctl; a small VM works the same). On the Proxmox
# host, one line at a time, 110 = a free container ID, vmbr0 = your bridge:
#   pct create 110 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst --hostname companion-docker-test \
#     --cores 2 --memory 2048 --rootfs local-lvm:12 --net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged 1 \
#     --features nesting=1,keyctl=1 --start 1
#   pct exec 110 -- bash -c 'apt-get update && apt-get install -y docker.io docker-compose curl openssl ca-certificates'
#   pct enter 110
# Then in the container:
#   curl -fsSL -o sec12-docker.sh https://raw.githubusercontent.com/waldonso2/wealthfolio-companion/main/scripts/sec12-docker.sh
#   bash sec12-docker.sh run          about 10 minutes; leaves the new release running for a look at the UI
#   bash sec12-docker.sh clean        removes containers, volumes and the work folder
# The result goes to /root/sec12-docker-report.txt (no secrets in it). PREV overrides the older release to start from.
set -uo pipefail
# `pct enter` and `pct exec` start a shell without /usr/local/bin, where cosign is installed.
export PATH="/usr/local/bin:/usr/local/sbin:$PATH"

REPO=waldonso2/wealthfolio-companion
RAW="https://raw.githubusercontent.com/$REPO/main"
WORK=/root/sec12-docker
REPORT=/root/sec12-docker-report.txt
export COMPOSE_PROJECT_NAME=sec12docker
FAILS=0

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
header() { say ""; say "== $* ($(date -u +%FT%TZ))"; }

compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
pull() { (cd "$WORK" && compose pull companion >/dev/null 2>&1); }
up() { (cd "$WORK" && compose up -d --no-deps companion >/dev/null 2>&1); }
healthy() { curl -fsk --max-time 5 -o /dev/null https://127.0.0.1:8443/healthz; }
wait_healthy() { local i; for i in $(seq 1 90); do healthy && return 0; sleep 2; done; return 1; }
image_ref() { sed -n 's/^COMPANION_IMAGE=//p' "$WORK/.env"; }
running_image() { docker inspect -f '{{.Config.Image}}' companion 2>/dev/null; }
container_id() { docker inspect -f '{{.Id}} {{.State.StartedAt}}' companion 2>/dev/null; }
latest() {
  local url
  url="$(curl -fsSI --retry 6 --proto '=https' -o /dev/null -w '%{redirect_url}' "https://github.com/$REPO/releases/latest")" || return 1
  printf '%s\n' "${url##*/tag/v}"
}
previous() {
  [[ -n "${PREV:-}" ]] && { printf '%s\n' "$PREV"; return 0; }
  curl -fsSL --retry 6 --proto '=https' "https://api.github.com/repos/$REPO/releases?per_page=5" |
    grep -o '"tag_name": *"v[^"]*"' | sed -n '2s/.*"v\([^"]*\)"/\1/p'
}
datafiles() {  # the sealed files of the data volume; SQLite's -wal/-shm and the lock files come and go
  docker run --rm -v sec12docker_companion-data:/data:ro --entrypoint sh "$(running_image)" -c \
    "cd /data && find . -type f ! -name 'before-restore-*' ! -name '*-wal' ! -name '*-shm' ! -name '*.lock' | LC_ALL=C sort | tr '\n' ' '"
}

install_cosign() {  # the README's pinned cosign 3.0.6 and the checksums of sigstore's own installer
  command -v cosign >/dev/null 2>&1 && return 0
  local a s
  case "$(uname -m)" in
    x86_64) a=amd64 s=c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74 ;;
    aarch64) a=arm64 s=bedac92e8c3729864e13d4a17048007cfafa79d5deca993a43a90ffe018ef2b8 ;;
    *) return 1 ;;
  esac
  local rc=0
  { curl -fsSLo /tmp/cosign --retry 6 "https://github.com/sigstore/cosign/releases/download/v3.0.6/cosign-linux-$a" &&
      echo "$s  /tmp/cosign" | sha256sum -c - >/dev/null &&
      install -m 755 /tmp/cosign /usr/local/bin/cosign; } || rc=$?
  rm -f /tmp/cosign
  return "$rc"  # not rm's status: a failed download must fail the preflight
}

preflight() {
  [[ $EUID == 0 ]] || { echo "Run as root." >&2; exit 2; }
  local c
  for c in docker curl openssl; do command -v "$c" >/dev/null || { echo "$c is missing." >&2; exit 2; }; done
  docker info >/dev/null 2>&1 || { echo "The Docker daemon does not answer." >&2; exit 2; }
  { docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1; } ||
    { echo "Docker Compose is missing." >&2; exit 2; }
  install_cosign || { echo "cosign 3 could not be downloaded or its checksum did not match." >&2; exit 2; }
  cosign version >/dev/null 2>&1 || { echo "cosign is installed but does not run (PATH: $PATH)." >&2; exit 2; }
  if docker ps -a --format '{{.Names}}' | grep -qx companion; then
    echo "A container named 'companion' exists. Use a disposable host, or remove it first." >&2; exit 2
  fi
}

prepare() {
  mkdir -p "$WORK" && cd "$WORK" || exit 2
  if ! curl -fsSL --retry 6 -o pull-verified-image.sh "$RAW/scripts/pull-verified-image.sh" ||
    ! curl -fsSL --retry 6 -o docker-compose.yml "$RAW/deploy/docker-compose.example.yml"; then
    echo "download failed" >&2; exit 2
  fi
  chmod +x pull-verified-image.sh
  [[ -f companion.key ]] || { openssl rand -base64 32 >companion.key; chown 10001:10001 companion.key; chmod 600 companion.key; }
  # Wealthfolio's own service is not part of this test (--no-deps), but Compose reads its variables.
  printf 'WF_SECRET_KEY=%s\nWF_AUTH_PASSWORD_HASH=unused\n' "$(openssl rand -base64 32)" >.env
  chmod 600 .env
}

scenario_12() {  # install a verified release
  local old="$1"
  header "Scenario 12: the verified image of $old starts"
  local ref
  ref="$(cd "$WORK" && ./pull-verified-image.sh --env .env "$old" 2>"$WORK/verify.log")"
  check "pull-verified-image.sh accepts v$old" test -n "$ref"
  if [[ -z "$ref" ]]; then
    say "      $(tail -n 3 "$WORK/verify.log" | tr '\n' ' ')"
    return 1  # nothing below can run without a verified image
  fi
  check "the reference is a digest, not a tag" bash -c "[[ '$ref' =~ @sha256:[0-9a-f]{64}$ ]]"
  up; check "the container starts and answers" wait_healthy
  check "it runs exactly the verified reference" bash -c "[[ -n '$(image_ref)' && '$(running_image)' == '$(image_ref)' ]]"
  healthy || say "      $(cd "$WORK" && docker logs companion 2>&1 | tail -n 5 | tr '\n' ' ')"
  check "the redirect on 8090 answers 308" test "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8090/)" == 308
  check "the setup code is in the log" bash -c "docker logs companion 2>&1 | grep -q 'Setup code'"
}

scenario_13() {  # everything else is refused
  header "Scenario 13: wrong release, tag, signer or digest is refused"
  local out rc policy
  out="$(cd "$WORK" && ./pull-verified-image.sh latest 2>/dev/null)"; rc=$?
  check "'latest' is refused, nothing printed (exit $rc)" bash -c "[[ $rc != 0 && -z '$out' ]]"
  out="$(cd "$WORK" && ./pull-verified-image.sh 9.9.9 2>/dev/null)"; rc=$?
  check "a release that does not exist is refused (exit $rc)" bash -c "[[ $rc != 0 && -z '$out' ]]"
  out="$(cd "$WORK" && ./pull-verified-image.sh 'x;id' 2>/dev/null)"; rc=$?
  check "a version with shell characters is refused (exit $rc)" bash -c "[[ $rc != 0 && -z '$out' ]]"
  # The script's own policy, against images that are not ours or not signed.
  grep -q "release.yml@refs/heads/main" "$WORK/pull-verified-image.sh" || fail "the script names its signer"
  policy=(--certificate-identity "https://github.com/$REPO/.github/workflows/release.yml@refs/heads/main"
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
    --certificate-github-workflow-repository "$REPO" --certificate-github-workflow-ref refs/heads/main
    --certificate-github-workflow-trigger push)
  check "control: cosign's own release image verifies with ITS identity (so the next check proves the policy)" \
    cosign verify --certificate-identity keyless@projectsigstore.iam.gserviceaccount.com \
      --certificate-oidc-issuer https://accounts.google.com ghcr.io/sigstore/cosign/cosign:v3.0.6
  check "an image signed by someone else is refused (the same image, our policy)" \
    bash -c "! cosign verify $(printf '%q ' "${policy[@]}") ghcr.io/sigstore/cosign/cosign:v3.0.6 >/dev/null 2>&1"
  check "an unsigned image is refused (alpine)" \
    bash -c "! cosign verify $(printf '%q ' "${policy[@]}") docker.io/library/alpine:3.20 >/dev/null 2>&1"
  check "a digest that does not exist is refused" \
    bash -c "! cosign verify $(printf '%q ' "${policy[@]}") ghcr.io/$REPO@sha256:$(printf '0%.0s' $(seq 64)) >/dev/null 2>&1"
}

scenario_14() {  # a refusal leaves the running container and the choice alone
  header "Scenario 14: the failed checks changed nothing"
  check "the same container still runs (unrestarted)" test "$(container_id)" == "$1"
  check ".env still names the verified reference" test "$(image_ref)" == "$2"
  check "the service still answers" healthy
}

scenario_update() {  # old -> new, the data volume stays
  local new="$1" before after ref
  header "Update to $new (data volume kept)"
  before="$(datafiles)"
  ref="$(cd "$WORK" && ./pull-verified-image.sh --env .env "$new" 2>>"$WORK/verify.log")"
  check "v$new is verified" test -n "$ref"
  check "the new image is pulled" pull
  up; check "the new container answers" wait_healthy
  check "it runs exactly the verified reference" bash -c "[[ -n '$(image_ref)' && '$(running_image)' == '$(image_ref)' ]]"
  after="$(datafiles)"
  check "the data files are the same ($before)" test "$before" == "$after"
  check "the data opens (security-status)" bash -c "docker exec companion wealthfolio-companion security-status >/dev/null"
}

scenario_16() {  # backup, stop, restore into the volume, start
  header "Scenario 16: backup and restore"
  local before after b="$WORK/backup.tar.gz" img
  img="$(running_image)"
  before="$(datafiles)"
  (umask 077; docker exec companion wealthfolio-companion backup >"$b" 2>/dev/null); check "backup written" test -s "$b"
  (cd "$WORK" && compose stop companion >/dev/null 2>&1)
  check "restore accepted (container stopped)" bash -c "docker run --rm -i -v sec12docker_companion-data:/data \
    -v '$WORK/companion.key:/run/secrets/companion-key:ro' '$img' restore <'$b' >/dev/null 2>&1"
  up; check "service healthy after restore" wait_healthy
  after="$(datafiles)"
  check "same data files after restore" test "$before" == "$after"
  [[ "$before" == "$after" ]] || say "      before: $before / after: $after"
  rm -f "$b"
}

scenario_17() {  # hardening and exposure of the running container
  header "Scenario 17: hardening and exposure"
  local k
  k="$(cat "$WORK/companion.key")"
  check "user 10001" test "$(docker exec companion id -u)" == 10001
  check "read-only root file system" test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' companion)" == true
  check "all capabilities dropped" test "$(docker inspect -f '{{.HostConfig.CapDrop}}' companion)" == "[ALL]"
  check "no-new-privileges" bash -c "docker inspect -f '{{.HostConfig.SecurityOpt}}' companion | grep -q no-new-privileges"
  check "not privileged, no host network or PID namespace" bash -c "! docker inspect -f '{{.HostConfig.Privileged}} {{.HostConfig.NetworkMode}} {{.HostConfig.PidMode}}' companion | grep -qE 'true|host'"
  check "no Docker socket or host path mounted" bash -c "! docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' companion | grep -q '/var/run/docker.sock\|/etc\|/root '"
  check "only 8443 and 8090 are published" test "$(docker port companion | sed 's/ -> .*//' | sort -u | tr '\n' ' ')" == "8090/tcp 8443/tcp "
  check "the key is not in the container log" bash -c "! docker logs companion 2>&1 | grep -qF -- '$k'"
  check "the key is not in the data volume" bash -c "! docker run --rm -v sec12docker_companion-data:/data:ro --entrypoint sh '$(running_image)' -c 'grep -rlF -- \"$k\" /data' | grep -q ."
  check "the key is not in the image config" bash -c "! docker inspect '$(running_image)' | grep -qF -- '$k'"
}

clean() {
  (cd "$WORK" 2>/dev/null && compose down -v >/dev/null 2>&1)
  docker rm -f companion >/dev/null 2>&1
  docker volume rm -f sec12docker_companion-data sec12docker_companion-tls >/dev/null 2>&1
  rm -rf "$WORK"
  echo "Removed containers, volumes and $WORK (cosign stays installed)."
}

case "${1:-}" in
  run)
    preflight
    rm -f "$REPORT"
    new="$(latest)"; old="$(previous)"
    [[ -n "$new" && -n "$old" ]] || { echo "latest and previous release unknown" >&2; exit 2; }
    say "SEC-12 Docker run on $(hostname), $(date -u +%FT%TZ): start with $old, update to $new"
    prepare
    if ! scenario_12 "$old"; then
      say ""; say "Stopped after scenario 12: $FAILS failed. Report: $REPORT"
      exit 1
    fi
    id="$(container_id)"; ref="$(image_ref)"
    scenario_13
    scenario_14 "$id" "$ref"
    scenario_update "$new"
    scenario_16
    scenario_17
    say ""
    say "Result: $FAILS failed. Report: $REPORT. $new keeps running: https://<this host>:8443 (setup code: docker logs companion)."
    exit $((FAILS > 0))
    ;;
  clean) clean ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
