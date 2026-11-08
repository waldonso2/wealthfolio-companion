#!/usr/bin/env bash
# SEC-12 (#158): end-to-end checks of install, update, rollback and hardening on a DISPOSABLE test container.
# Never run it in a container with real broker credentials: it installs older releases, blocks GitHub for a
# moment, stops the service and restores a backup. Run as root inside the test LXC:
#
#   sec12-e2e.sh run            scenarios 5, 7-9, 11, 14-17 (and the verifier part of 2-4), about 15 minutes
#   sec12-e2e.sh prepare-reboot install the previous release, then start the update you interrupt (scenario 10)
#   sec12-e2e.sh after-reboot   check the state after that interrupted update
#   sec12-e2e.sh prepare-auto   install the previous release for the automatic update (scenario 6)
#   sec12-e2e.sh check-auto     check that the automatic update installed the latest release
#
# Every result goes to /root/sec12-report.txt (no secrets in it). Environment: PREV (the older release to go
# back to, default: the release before the latest), SYNTH (the synthetic test password typed in the UI).
set -uo pipefail

BASE=/opt/wealthfolio-companion
REPORT=/root/sec12-report.txt
VERSION_FILE=/root/.wealthfolio-companion
REPO=https://github.com/waldonso2/wealthfolio-companion
SYNTH="${SYNTH:-SEC12-Test-Passwort-4711}"
FAILS=0

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }

installed() { cat "$VERSION_FILE" 2>/dev/null || echo "?"; }
healthy() {
  systemctl is-active --quiet wealthfolio-companion &&
    curl -fsSk --max-time 5 https://127.0.0.1:8443/healthz 2>/dev/null | grep -q '"ok":true'
}
wait_healthy() {
  local i
  for i in $(seq 1 60); do healthy && return 0; sleep 2; done
  return 1
}
latest() {
  local url
  url="$(curl -fsSI --proto '=https' -o /dev/null -w '%{redirect_url}' "$REPO/releases/latest")" || return 1
  printf '%s\n' "${url##*/tag/v}"
}
# The release before the latest: the second newest tag of the releases page (PREV overrides it).
previous() {
  [[ -n "${PREV:-}" ]] && { printf '%s\n' "$PREV"; return 0; }
  curl -fsSL --proto '=https' "https://api.github.com/repos/waldonso2/wealthfolio-companion/releases?per_page=5" |
    grep -o '"tag_name": *"v[^"]*"' | sed -n '2s/.*"v\([^"]*\)"/\1/p'
}
run_update() {  # run_update [VERSION] [extra env...]: the console's update, its exit code
  local v="${1:-}"; shift || true
  if [[ -n "$v" ]]; then env VERSION="$v" "$@" /usr/bin/update; else env "$@" /usr/bin/update; fi
}
header() {
  say ""
  say "== $* ($(date -u +%FT%TZ))"
}

preflight() {
  [[ $EUID == 0 ]] || { echo "Run as root." >&2; exit 2; }
  [[ -x /usr/local/bin/companion-update && -e "$BASE/current" ]] ||
    { echo "Wealthfolio Companion (0.1.0a5 or later) is not installed here." >&2; exit 2; }
  if [[ -d "$BASE/data" ]] && find "$BASE/data" -name 'secrets.enc' -size +2k | grep -q .; then
    echo "This container may hold real credentials (a large vault). Use a disposable test container." >&2
    [[ "${SEC12_FORCE:-}" == 1 ]] || exit 2
  fi
}

verifier_cases() {  # scenarios 2, 3, 4, 7 (the verifier itself) and 8 (network)
  header "Verifier on real release files (scenarios 2-4, 7, 8)"
  local v py prov dir old
  v="$(latest)" || { fail "latest release readable"; return; }
  py="$BASE/current/verify/bin/python"; prov="$BASE/app/src/brokersync/provenance.py"
  dir="$(mktemp -d)"
  if ! curl -fsSL "$REPO/releases/download/v$v/SHA256SUMS" -o "$dir/SHA256SUMS" ||
    ! curl -fsSL "$REPO/releases/download/v$v/wealthfolio-companion-$v.tar.gz" -o "$dir/a.tar.gz"; then
    fail "download release $v"; return
  fi
  ( cd "$dir" && cp a.tar.gz "wealthfolio-companion-$v.tar.gz" )
  check "valid release $v is accepted" "$py" -I "$prov" "$dir/SHA256SUMS" "$dir/wealthfolio-companion-$v.tar.gz"
  cp "$dir/SHA256SUMS" "$dir/S2"; echo "0000  extra-file" >>"$dir/S2"
  check "changed SHA256SUMS is refused" bash -c "! '$py' -I '$prov' '$dir/S2' '$dir/wealthfolio-companion-$v.tar.gz'"
  cp "$dir/a.tar.gz" "$dir/wealthfolio-companion-$v.tar.gz"; printf 'x' >>"$dir/wealthfolio-companion-$v.tar.gz"
  check "changed archive is refused" bash -c "! '$py' -I '$prov' '$dir/SHA256SUMS' '$dir/wealthfolio-companion-$v.tar.gz'"
  old="$(previous)"
  if [[ -n "$old" ]] && curl -fsSL "$REPO/releases/download/v$old/wealthfolio-companion-$old.tar.gz" -o "$dir/o.tar.gz"; then
    cp "$dir/o.tar.gz" "$dir/wealthfolio-companion-$v.tar.gz"
    check "another release's archive under this name is refused" \
      bash -c "! '$py' -I '$prov' '$dir/SHA256SUMS' '$dir/wealthfolio-companion-$v.tar.gz'"
  fi
  cp "$dir/a.tar.gz" "$dir/wealthfolio-companion-$v.tar.gz"
  block_github
  check "no attestation reachable (GitHub blocked) is refused" \
    bash -c "! '$py' -I '$prov' '$dir/SHA256SUMS' '$dir/wealthfolio-companion-$v.tar.gz'"
  unblock_github
  rm -rf "$dir"
}

HOSTS_BACKUP=/root/sec12-hosts.bak
block_github() {
  cp /etc/hosts "$HOSTS_BACKUP"
  printf '0.0.0.0 github.com api.github.com objects.githubusercontent.com release-assets.githubusercontent.com\n' >>/etc/hosts
}
unblock_github() { [[ -f "$HOSTS_BACKUP" ]] && cp "$HOSTS_BACKUP" /etc/hosts && rm -f "$HOSTS_BACKUP"; }

update_cases() {
  local new old before rc
  new="$(latest)"; old="$(previous)"
  [[ -n "$old" && -n "$new" ]] || { fail "previous and latest release known"; return; }
  say "latest release $new, previous $old, installed $(installed)"

  header "Scenario 5: manual update (back to $old, then to $new)"
  run_update "$old"; check "VERSION=$old update installs $old" test "$(installed)" == "$old"
  check "service healthy on $old" wait_healthy
  run_update ""; check "update installs $new" test "$(installed)" == "$new"
  check "service healthy on $new" wait_healthy

  header "Scenario 8 + 14: update while GitHub is unreachable"
  run_update "$old" >/dev/null; before="$(installed)"
  block_github; run_update ""; rc=$?; unblock_github
  check "update fails (exit $rc)" test "$rc" != 0
  check "installed release unchanged ($before)" test "$(installed)" == "$before"
  check "service still healthy" wait_healthy

  header "Scenario 9 + 14: failed health check rolls back"
  run_update "" COMPANION_HEALTH_TIMEOUT=1; rc=$?
  check "update reports failure (exit $rc)" test "$rc" != 0
  check "rolled back to $before" test "$(installed)" == "$before"
  check "current points to a $before release" bash -c "readlink '$BASE/current' | grep -q 'releases/$before-'"
  check "no update-pending left" test ! -e "$BASE/update-pending"
  check "service healthy after rollback" wait_healthy

  header "Scenario 11: two updates at once"
  run_update "" >/tmp/sec12-u1.log 2>&1 &
  local first=$!
  sleep 3
  run_update ""; rc=$?
  check "second update is refused as busy (exit 75, was $rc)" test "$rc" == 75
  wait "$first"; check "first update finished ($(installed))" test "$(installed)" == "$new"
  check "service healthy" wait_healthy
}

secrets_case() {
  header "Scenario 15: no synthetic credential in logs or files"
  local hits
  hits="$( { journalctl --no-pager -q 2>/dev/null; cat /root/sec12-u*.log 2>/dev/null; } | grep -c -F -- "$SYNTH")"
  check "journal holds no '$SYNTH' ($hits hits)" test "$hits" == 0
  hits="$(grep -r -a -l -F -- "$SYNTH" "$BASE" /etc/wealthfolio-companion /run/wealthfolio-companion /tmp /var/log 2>/dev/null | wc -l)"
  check "no file holds '$SYNTH' in plain text ($hits files)" test "$hits" == 0
}

backup_case() {
  header "Scenario 16: backup and restore"
  local b=/root/sec12-backup.tar.gz before after
  before="$(cd "$BASE/data" && find . -type f ! -name 'before-restore-*' | LC_ALL=C sort | tr '\n' ' ')"
  (umask 077; /usr/local/bin/companion-cli backup >"$b") ; check "backup written" test -s "$b"
  systemctl stop wealthfolio-companion wealthfolio-companion-run.timer
  /usr/local/bin/companion-cli restore <"$b"; check "restore accepted" test $? == 0
  systemctl start wealthfolio-companion wealthfolio-companion-run.timer
  after="$(cd "$BASE/data" && find . -type f ! -name 'before-restore-*' | LC_ALL=C sort | tr '\n' ' ')"
  check "same data files after restore" test "$before" == "$after"
  check "service healthy after restore" wait_healthy
  check "data opens (security-status)" bash -c "/usr/local/bin/companion-cli security-status >/dev/null"
  rm -f "$b"
}

exposure_case() {
  header "Scenario 17: network exposure and units"
  local ports
  ports="$(ss -Hltn | awk '{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' ')"
  say "listening TCP ports: $ports"
  check "UI on 8443 and redirect on 8090 listen" bash -c "echo ' $ports' | grep -q ' 8443 ' && echo ' $ports' | grep -q ' 8090 '"
  check "nothing else but 22 (SSH of the template) and local services" \
    bash -c "for p in $ports; do case \$p in 22|25|8090|8443) ;; *) ss -Hltn \"sport = :\$p\" | grep -qv '127.0.0.1\|\[::1\]' && exit 1;; esac; done; exit 0"
  local u score
  for u in wealthfolio-companion wealthfolio-companion-run wealthfolio-companion-cert; do
    score="$(systemd-analyze security "$u.service" 2>/dev/null | grep -o 'exposure level for [^:]*: [0-9.]*' | grep -o '[0-9.]*$')"
    check "$u exposure ${score:-?} <= 2.0" awk -v s="${score:-99}" 'BEGIN { exit !(s <= 2.0) }'
  done
  for u in wealthfolio-companion-update wealthfolio-companion-update-recover; do
    score="$(systemd-analyze security "$u.service" 2>/dev/null | grep -o 'exposure level for [^:]*: [0-9.]*' | grep -o '[0-9.]*$')"
    check "$u exposure ${score:-?} <= 6.5" awk -v s="${score:-99}" 'BEGIN { exit !(s <= 6.5) }'
  done
  check "data/ is companion 0700" bash -c "[[ \$(stat -c '%U %a' '$BASE/data') == 'companion 700' ]]"
  check "/etc/wealthfolio-companion is root 0700" bash -c "[[ \$(stat -c '%U %a' /etc/wealthfolio-companion) == 'root 700' ]]"
  check "key file is root 0600" bash -c "f=\$(ls /etc/wealthfolio-companion/key.cred /etc/wealthfolio-companion/secret.key 2>/dev/null | head -1); [[ \$(stat -c '%U %a' \"\$f\") == 'root 600' ]]"
}

case "${1:-}" in
  run)
    preflight
    say "SEC-12 run on $(hostname), $(date -u +%FT%TZ), installed $(installed)"
    verifier_cases
    update_cases
    secrets_case
    backup_case
    exposure_case
    say ""
    say "Result: $FAILS failed. Report: $REPORT"
    exit $((FAILS > 0))
    ;;
  prepare-reboot)
    preflight
    old="$(previous)"
    run_update "$old" >/dev/null
    say ""; say "== Scenario 10 prepared: installed $(installed). Now run 'update' and reboot the container"
    say "   from the Proxmox host (pct reboot <id>) while it shows 'Building', then: sec12-e2e.sh after-reboot"
    ;;
  after-reboot)
    header "Scenario 10: update interrupted by a reboot"
    check "no update-pending left" test ! -e "$BASE/update-pending"
    check "no unfinished build left" bash -c "! ls -d '$BASE'/releases/*.incomplete '$BASE'/releases/.dl.* 2>/dev/null | grep -q ."
    check "current matches the installed version ($(installed))" bash -c "readlink '$BASE/current' | grep -q 'releases/$(installed)-'"
    check "service healthy" wait_healthy
    say "Result so far: $FAILS failed in this step."
    exit $((FAILS > 0))
    ;;
  prepare-auto)
    preflight
    old="$(previous)"
    run_update "$old" >/dev/null
    say ""; say "== Scenario 6 prepared: installed $(installed). Switch on the automatic update (page Updates,"
    say "   interval 60), wait 5 minutes, then: sec12-e2e.sh check-auto"
    ;;
  check-auto)
    header "Scenario 6: automatic update"
    check "automatic update installed the latest release ($(installed))" test "$(installed)" == "$(latest)"
    check "service healthy" wait_healthy
    exit $((FAILS > 0))
    ;;
  *)
    sed -n '2,13p' "$0"
    exit 2
    ;;
esac
