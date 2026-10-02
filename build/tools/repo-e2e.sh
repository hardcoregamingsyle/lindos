#!/bin/bash
# ============================================================================
#  build/tools/repo-e2e.sh - the update path, end to end, on real apt (Linux only)
#
#  Usage: build/tools/repo-e2e.sh            (as root, in a DISPOSABLE ubuntu:24.04 container)
#
#  Env:   LINDOS_E2E_DIR=out/e2e   LINDOS_E2E_PORT=8099
#         LINDOS_E2E_FULL_UPGRADE=1   also run `lindos-update apply --all` (upgrades the whole container)
#
#  It proves what no test on a developer machine can: real apt, dpkg, gpg and a real HTTP server.
#    1. makes a THROW-AWAY key (never leaves this run) and a copy of the keyring package that
#       carries it, with the source switched on and pointing at http://127.0.0.1:PORT/stable/
#       (this also runs the build's keyring guard on its positive path, with real gpg);
#    2. builds lindos-core + lindos-archive-keyring twice (1.0.0, 1.0.1 - the lock-step stamping);
#    3. publishes 1.0.0 with build/publish-apt-repo.sh --release, the signing key imported from an
#       environment variable exactly as CI does it, and serves it on localhost;
#    4. installs from local files, lets apt use the repository, checks apt trusts it;
#    5. publishes 1.0.1; `lindos-update check --refresh` must see it, update-state.json must show
#       it (category "lindos", origin "Lindos"), the plan digest must be the same for the state
#       file and a fresh plan, and the helper must REFUSE a wrong digest;
#    6. `lindos-update apply` installs it; check says up to date; history has the upgrade;
#    7. (optional) `lindos-update apply --all` on a 1.0.2 - the full-upgrade path for real.
#  Exit: 0 all good - 1 a check failed (the log says which)
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="repo-e2e"
ROOT="$(repo_root)"

[ "$(id -u)" -eq 0 ] || die "repo-e2e installs packages: run it as root in a disposable container (.github/workflows/ci.yml, job repo-e2e)"
require_cmd gpg gpgv apt-get dpkg-deb apt-ftparchive python3 curl

E2E="${LINDOS_E2E_DIR:-${ROOT}/out/e2e}"
PORT="${LINDOS_E2E_PORT:-8099}"
FULL="${LINDOS_E2E_FULL_UPGRADE:-0}"
URL="http://127.0.0.1:${PORT}/stable/"
LOGS="${E2E}/logs"

rm -rf "${E2E}"
mkdir -p "${LOGS}" "${E2E}/site"
# the throw-away key lives OUTSIDE the output directory, which may be uploaded
KEYHOME="$(mktemp -d)"
chmod 0700 "${KEYHOME}"
HTTP_PID=""
cleanup() {
    if [ -n "${HTTP_PID}" ]; then kill "${HTTP_PID}" 2>/dev/null || true; fi
    gpgconf --homedir "${KEYHOME}" --kill gpg-agent >/dev/null 2>&1 || true
    rm -rf "${KEYHOME}"
}
trap cleanup EXIT

step() { log "=== $*"; }
fail() { die "FAILED: $*"; }
json_check() {
    # json_check 'python expression over d' JSON MESSAGE
    python3 -c 'import json, sys
d = json.loads(sys.argv[2])
sys.exit(0 if eval(sys.argv[1]) else 1)' "$1" "$2" || fail "$3 (got: ${2:0:400})"
}

# ---------------------------------------------------------------------------
step "1. throw-away key + a keyring package that trusts it"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
GNUPGHOME="${KEYHOME}"
export GNUPGHOME
gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "Lindos e2e (throw-away) <e2e@lindos.invalid>" ed25519 cert never
FPR="$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ && !done { print $10; done = 1 }')"
gpg --batch --quiet --pinentry-mode loopback --passphrase '' --quick-add-key "${FPR}" ed25519 sign 1d
SUBFPR="$(gpg --list-keys --with-colons | awk -F: '/^fpr:/ { n++; if (n == 2) print $10 }')"
[ -n "${FPR}" ] && [ -n "${SUBFPR}" ] || fail "could not create the throw-away key"
LINDOS_APT_SIGNING_KEY="$(gpg --batch --pinentry-mode loopback --passphrase '' --armor --export-secret-subkeys "${SUBFPR}!")"

PK="${E2E}/packages"
mkdir -p "${PK}"
cp -a "${ROOT}/packages/lindos-core" "${ROOT}/packages/lindos-archive-keyring" "${PK}/"
KR="${PK}/lindos-archive-keyring/root"
KEYRING="${KR}/usr/share/keyrings/lindos-archive-keyring.gpg"
gpg --export "${FPR}" > "${KEYRING}"
printf '%s\n' "${FPR}" > "${KR}/usr/share/lindos/archive-key.fingerprint"
cat > "${KR}/etc/apt/sources.list.d/lindos.sources" <<EOF
Enabled: yes
Types: deb
URIs: ${URL}
Suites: ./
Signed-By: /usr/share/keyrings/lindos-archive-keyring.gpg
EOF

# ---------------------------------------------------------------------------
step "2. build 1.0.0, 1.0.1 (and 1.0.2 for the full upgrade) - lock-step stamping"
build() {
    LINDOS_PACKAGES_DIR="${PK}" WORK_DIR="${E2E}/work" \
        bash "${ROOT}/build/mkdeb.sh" --version "$1" --out "${E2E}/debs-$1" lindos-core lindos-archive-keyring \
        > "${LOGS}/mkdeb-$1.log" 2>&1 || { cat "${LOGS}/mkdeb-$1.log" >&2; fail "mkdeb $1"; }
}
build 1.0.0
build 1.0.1
[ "${FULL}" != "1" ] || build 1.0.2
[ "$(dpkg-deb -f "${E2E}/debs-1.0.1/lindos-core_1.0.1_all.deb" Version)" = "1.0.1" ] || fail "the stamped version is not 1.0.1"

# publish CURRENT [PREVIOUS...]: the first directory is the release, the others are kept older releases
publish() {
    local args=(--in "$1") d
    shift
    for d in "$@"; do args+=(--previous "${d}"); done
    env -u GNUPGHOME LINDOS_APT_SIGNING_KEY="${LINDOS_APT_SIGNING_KEY}" \
        bash "${ROOT}/build/publish-apt-repo.sh" --release "${args[@]}" --out "${E2E}/site/stable" --keep 3 \
        --expect-fingerprint "${FPR}" --keyring "${KEYRING}" --sources-file "${KR}/etc/apt/sources.list.d/lindos.sources" \
        >> "${LOGS}/publish.log" 2>&1 || { tail -n 30 "${LOGS}/publish.log" >&2; fail "publishing"; }
}

# ---------------------------------------------------------------------------
step "3. publish 1.0.0 and serve it on ${URL}"
publish "${E2E}/debs-1.0.0"
python3 -m http.server "${PORT}" --bind 127.0.0.1 --directory "${E2E}/site" > "${LOGS}/http.log" 2>&1 &
HTTP_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS "${URL}InRelease" > /dev/null 2>&1 && break
    sleep 1
done
curl -fsS "${URL}InRelease" > /dev/null || fail "the repository is not being served"

# ---------------------------------------------------------------------------
step "4. install from local files, then let apt use the repository"
apt-get update -q > "${LOGS}/apt-update-base.log" 2>&1 || true
apt-get install -y --no-install-recommends \
    "${E2E}/debs-1.0.0/lindos-core_1.0.0_all.deb" "${E2E}/debs-1.0.0/lindos-archive-keyring_1.0.0_all.deb" \
    > "${LOGS}/apt-install.log" 2>&1 || { tail -n 40 "${LOGS}/apt-install.log" >&2; fail "installing the two packages"; }
[ -s /usr/share/keyrings/lindos-archive-keyring.gpg ] || fail "the keyring package did not install a keyring"
apt-get update > "${LOGS}/apt-update-1.log" 2>&1 || { tail -n 30 "${LOGS}/apt-update-1.log" >&2; fail "apt-get update with the Lindos source"; }
grep -q "127.0.0.1:${PORT}" "${LOGS}/apt-update-1.log" || fail "apt did not read the Lindos repository"
if grep -Eq 'NO_PUBKEY|is not signed|EXPKEYSIG|BADSIG|^Err:.*127\.0\.0\.1' "${LOGS}/apt-update-1.log"; then
    fail "apt does not trust the repository (see ${LOGS}/apt-update-1.log)"
fi
apt-cache policy lindos-core | grep -q "127.0.0.1:${PORT}" || fail "lindos-core has no candidate from the Lindos repository"
repo_json="$(lindos-update repo status --json)" || fail "lindos-update repo status"
json_check 'd["configured"] is True and d["reachable"] is True' "${repo_json}" "the Lindos source is not reported as configured and reachable"

# ---------------------------------------------------------------------------
step "5. publish 1.0.1: lindos-update must see it"
publish "${E2E}/debs-1.0.1" "${E2E}/debs-1.0.0"
rc=0
check_json="$(lindos-update check --refresh --json 2> "${LOGS}/check.err")" || rc=$?
[ "${rc}" -eq 0 ] || fail "lindos-update check exited ${rc} (0 = updates found)"
json_check 'any(u["name"] == "lindos-core" and u["candidate"] == "1.0.1" for u in d["lindos_updates"])' "${check_json}" "lindos-core 1.0.1 was not found"
status_json="$(lindos-update status --json)" || fail "lindos-update status"
json_check 'd["available"] and any(g["id"] == "lindos" and any(i["name"] == "lindos-core" and i["origin_label"] == "Lindos" for i in g["items"]) for g in d["groups"])' \
    "${status_json}" "update-state.json does not list lindos-core under Lindos"
json_check 'd["repo"]["configured"] is True and d["digest"].startswith("sha256:")' "${status_json}" "the state has no repo/digest"
plan_json="$(lindos-update plan --json)" || fail "lindos-update plan"
state_digest="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["digest"])' "${status_json}")"
plan_digest="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["digest"])' "${plan_json}")"
[ "${state_digest}" = "${plan_digest}" ] || fail "the plan digest differs between the state file (${state_digest}) and a fresh plan (${plan_digest})"

step "5b. the helper refuses a plan that is not the one shown"
zero="sha256:0000000000000000000000000000000000000000000000000000000000000000"
rc=0
/usr/libexec/lindos/lindos-helper apt-full-upgrade "{\"plan_digest\":\"${zero}\"}" > "${LOGS}/refuse.log" 2>&1 || rc=$?
[ "${rc}" -ne 0 ] || fail "the helper ran an upgrade with a wrong digest"
grep -q "not the one you were shown" "${LOGS}/refuse.log" || fail "the refusal did not say why (see ${LOGS}/refuse.log)"
[ "$(dpkg-query -W -f='${Version}' lindos-core)" = "1.0.0" ] || fail "a refused upgrade changed lindos-core"

# ---------------------------------------------------------------------------
step "6. lindos-update apply installs it"
lindos-update apply --yes --no-refresh > "${LOGS}/apply.log" 2>&1 || { tail -n 30 "${LOGS}/apply.log" >&2; fail "lindos-update apply"; }
[ "$(dpkg-query -W -f='${Version}' lindos-core)" = "1.0.1" ] || fail "lindos-core is not 1.0.1 after apply"
[ "$(dpkg-query -W -f='${Version}' lindos-archive-keyring)" = "1.0.1" ] || fail "lindos-archive-keyring is not 1.0.1 after apply"
rc=0
lindos-update check --json > "${LOGS}/check-after.json" 2>&1 || rc=$?
[ "${rc}" -eq 3 ] || fail "lindos-update check should say nothing to do (exit 3), got ${rc}"
history_json="$(lindos-update history --json)" || fail "lindos-update history"
json_check 'any(any(p["name"] == "lindos-core" for p in e["actions"].get("Upgrade", [])) for e in d["entries"])' "${history_json}" "history has no lindos-core upgrade"
python3 -m lindos.updatestate reboot-hook || fail "the reboot-required hook failed"
lindos-update cleanup --yes > "${LOGS}/cleanup.log" 2>&1 || { tail -n 30 "${LOGS}/cleanup.log" >&2; fail "lindos-update cleanup"; }
[ ! -e /var/lib/lindos/update-in-progress ] || fail "an in-progress marker was left behind"

# ---------------------------------------------------------------------------
if [ "${FULL}" = "1" ]; then
    step "7. full upgrade (1.0.2 plus whatever the container is behind on)"
    publish "${E2E}/debs-1.0.2" "${E2E}/debs-1.0.0" "${E2E}/debs-1.0.1"
    lindos-update apply --all --yes --include-kernel --allow-removals > "${LOGS}/apply-all.log" 2>&1 \
        || { tail -n 60 "${LOGS}/apply-all.log" >&2; fail "lindos-update apply --all"; }
    [ "$(dpkg-query -W -f='${Version}' lindos-core)" = "1.0.2" ] || fail "lindos-core is not 1.0.2 after the full upgrade"
    [ ! -e /var/lib/lindos/update-in-progress ] || fail "an in-progress marker was left behind by the full upgrade"
    audit="$(dpkg --audit)"
    [ -z "${audit}" ] || fail "dpkg reports broken packages after the full upgrade: ${audit}"
    rc=0
    lindos-update plan > "${LOGS}/plan-after.log" 2>&1 || rc=$?
    [ "${rc}" -eq 3 ] || fail "the plan after a full upgrade should be empty (exit 3), got ${rc}"
fi

ok "the update path works end to end (logs in ${LOGS})"
