#!/bin/bash
# ============================================================================
#  build/publish-apt-repo.sh — build a flat-format, signed apt repository for
#  Lindos's own lindos-*.deb packages (SPEC-UPDATE.md §36.5).
#
#  Usage: build/publish-apt-repo.sh [options]
#     --in DIR         directory of lindos-*.deb files (default: out/debs)
#     --kernel-in DIR   directory of the tuned kernel's linux-image/-headers/
#                       -modules-*-lindos .deb files to include too (default:
#                       out/kernel; silently skipped when it does not exist —
#                       a kernel build is optional, everything else is not)
#     --out DIR         output repository directory (default: out/apt-repo)
#     --key-id ID       gpg key id/fingerprint to sign with (default: the
#                       first usable secret key already available)
#     --gen-key         generate a fresh ephemeral signing key when none is
#                       available (writes README-CI-KEY.txt next to the repo
#                       — an ephemeral CI key is NOT a stable trust root)
#     -h, --help
#
#  Produces a **flat-format** repository (no dists/ tree — works from any
#  static HTTPS host, including one that cannot serve arbitrary directory
#  redirects): Packages, Packages.gz, Release (apt-ftparchive), a clearsigned
#  InRelease and a detached Release.gpg (gpg --local-user), the exported
#  public keyring (lindos-archive-keyring.gpg), every .deb copied alongside,
#  and an example sources.list line (lindos.list.example):
#
#      deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] <URL> ./
#
#  Never invents trust: a repo with no available signing key and no --gen-key
#  fails outright rather than shipping something unsigned (SPEC-UPDATE.md §35
#  — "a repo without a valid signature is refused, not downgraded to
#  'trusted' automatically").
#
#  Exit codes: 0 ok · 1 build/usage error
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="publish-apt-repo"

ROOT="$(repo_root)"
lindos_load_config

IN_DIR="${DEBS_DIR:-out/debs}"
KERNEL_IN_DIR="out/kernel"
REPO_DIR="${OUT_DIR:-out}/apt-repo"
KEY_ID=""
GEN_KEY=0

usage() {
    sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --in) [ $# -ge 2 ] || die "--in needs an argument" 1; IN_DIR="$2"; shift 2 ;;
        --in=*) IN_DIR="${1#*=}"; shift ;;
        --kernel-in) [ $# -ge 2 ] || die "--kernel-in needs an argument" 1; KERNEL_IN_DIR="$2"; shift 2 ;;
        --kernel-in=*) KERNEL_IN_DIR="${1#*=}"; shift ;;
        --out) [ $# -ge 2 ] || die "--out needs an argument" 1; REPO_DIR="$2"; shift 2 ;;
        --out=*) REPO_DIR="${1#*=}"; shift ;;
        --key-id) [ $# -ge 2 ] || die "--key-id needs an argument" 1; KEY_ID="$2"; shift 2 ;;
        --key-id=*) KEY_ID="${1#*=}"; shift ;;
        --gen-key) GEN_KEY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" 1 ;;
        *) die "unexpected argument: $1 (see --help)" 1 ;;
    esac
done

require_cmd apt-ftparchive gpg gzip find

case "${IN_DIR}" in /*) ;; *) IN_DIR="${ROOT}/${IN_DIR}" ;; esac
case "${KERNEL_IN_DIR}" in /*) ;; *) KERNEL_IN_DIR="${ROOT}/${KERNEL_IN_DIR}" ;; esac
case "${REPO_DIR}" in /*) ;; *) REPO_DIR="${ROOT}/${REPO_DIR}" ;; esac

[ -d "${IN_DIR}" ] || die "--in directory not found: ${IN_DIR}"

timer_start "publish-apt-repo"
rm -rf "${REPO_DIR}"
ensure_dir "${REPO_DIR}"

# ---------------------------------------------------------------------------
# 1. Copy every .deb (lindos-*.deb, plus the kernel's when present) in flat.
# ---------------------------------------------------------------------------
N_DEBS=0
copy_debs() {
    local dir="$1"
    [ -d "${dir}" ] || return 0
    local f
    while IFS= read -r -d '' f; do
        cp -f "${f}" "${REPO_DIR}/"
        N_DEBS=$(( N_DEBS + 1 ))
    done < <(find "${dir}" -maxdepth 1 -type f -name '*.deb' -print0)
}
copy_debs "${IN_DIR}"
if [ -d "${KERNEL_IN_DIR}" ]; then
    copy_debs "${KERNEL_IN_DIR}"
else
    log "--kernel-in directory not found (${KERNEL_IN_DIR}) — publishing without kernel .debs"
fi
[ "${N_DEBS}" -gt 0 ] || die "no .deb files found under ${IN_DIR} (or ${KERNEL_IN_DIR})"
log "copied ${N_DEBS} .deb file(s) into ${REPO_DIR}"

# ---------------------------------------------------------------------------
# 2. Packages / Packages.gz / Release — flat-format: apt-ftparchive is run
#    from inside the repo dir so recorded paths are bare filenames, not a
#    dists/<suite>/<component>/ tree.
# ---------------------------------------------------------------------------
(
    cd "${REPO_DIR}"
    apt-ftparchive packages . > Packages
    gzip -9 -c Packages > Packages.gz
    apt-ftparchive \
        -o APT::FTPArchive::Release::Origin="Lindos" \
        -o APT::FTPArchive::Release::Label="Lindos" \
        -o APT::FTPArchive::Release::Suite="stable" \
        -o APT::FTPArchive::Release::Codename="${LINDOS_CODENAME:-Aurora}" \
        -o APT::FTPArchive::Release::Architectures="amd64 all" \
        -o APT::FTPArchive::Release::Description="Lindos ${LINDOS_VERSION:-1.0.0} package repository" \
        release . > Release
)
log "wrote Packages, Packages.gz, Release (${REPO_DIR})"

# ---------------------------------------------------------------------------
# 3. Signing key: --key-id, else the first secret key already available,
#    else --gen-key (a fresh, ephemeral key — never checked into git).
# ---------------------------------------------------------------------------
GEN_GNUPGHOME="${REPO_DIR}/.gnupg-publish"
KEY_IS_EPHEMERAL=0

resolve_key_id() {
    [ -n "${KEY_ID}" ] && return 0
    KEY_ID="$(gpg --list-secret-keys --with-colons 2>/dev/null | awk -F: '/^sec:/{print $5; exit}')"
    [ -n "${KEY_ID}" ]
}

if ! resolve_key_id; then
    [ "${GEN_KEY}" -eq 1 ] || die "no gpg signing key available (pass --key-id, or --gen-key for an ephemeral CI-only key)"
    log "no signing key available — generating an ephemeral one (--gen-key)"
    ensure_dir "${GEN_GNUPGHOME}"
    chmod 0700 "${GEN_GNUPGHOME}"
    export GNUPGHOME="${GEN_GNUPGHOME}"
    gpg --batch --quiet --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "Lindos CI (ephemeral, do not trust) <ci@lindos.dev>" ed25519 sign 0
    KEY_ID="$(gpg --list-secret-keys --with-colons | awk -F: '/^sec:/{print $5; exit}')"
    [ -n "${KEY_ID}" ] || die "gpg --quick-generate-key did not produce a usable key"
    KEY_IS_EPHEMERAL=1
else
    log "signing with an existing key: ${KEY_ID}"
fi

gpg --batch --yes --output "${REPO_DIR}/lindos-archive-keyring.gpg" --export "${KEY_ID}"

if [ "${KEY_IS_EPHEMERAL}" -eq 1 ]; then
    cat > "${REPO_DIR}/README-CI-KEY.txt" <<EOF
This repository was signed with an EPHEMERAL key generated on the fly by
build/publish-apt-repo.sh --gen-key, because no real signing key was
available. It is NOT a stable trust root: a fresh key is generated every time
this script runs without a real one, so any machine that trusted a previous
lindos-archive-keyring.gpg will reject the next publish's signature. Configure
a real, persistent signing key (--key-id, with that key already present in
this environment's gpg keyring) before relying on this repository for
anything beyond a one-off CI smoke test.
EOF
    warn "signed with an EPHEMERAL CI-only key — see ${REPO_DIR}/README-CI-KEY.txt"
fi

# ---------------------------------------------------------------------------
# 4. InRelease (clearsigned) + Release.gpg (detached, armored) — both, so any
#    apt client (old: Release.gpg; modern: InRelease) can verify the repo.
# ---------------------------------------------------------------------------
GPG_SIGN_ARGS=(--batch --yes --digest-algo SHA256 --local-user "${KEY_ID}")
[ "${KEY_IS_EPHEMERAL}" -eq 1 ] && GPG_SIGN_ARGS+=(--pinentry-mode loopback --passphrase '')
(
    cd "${REPO_DIR}"
    gpg "${GPG_SIGN_ARGS[@]}" --clearsign --output InRelease Release
    gpg "${GPG_SIGN_ARGS[@]}" --detach-sign --armor --output Release.gpg Release
)
log "signed: InRelease, Release.gpg (key ${KEY_ID})"

# Never leave an ephemeral key's private material sitting in a directory that
# a CI job might upload as an artifact — only the exported *public* keyring
# (lindos-archive-keyring.gpg) is meant to leave this script.
if [ "${KEY_IS_EPHEMERAL}" -eq 1 ]; then
    rm -rf "${GEN_GNUPGHOME}"
fi

# ---------------------------------------------------------------------------
# 5. Example sources.list line (SPEC-UPDATE.md §36.5) + summary.
# ---------------------------------------------------------------------------
REPO_URL="${LINDOS_APT_REPO_URL:-https://packages.lindos.dev}"
SOURCES_LINE="deb [signed-by=/etc/apt/keyrings/lindos-archive-keyring.gpg] ${REPO_URL} ./"
printf '%s\n' "${SOURCES_LINE}" > "${REPO_DIR}/lindos.list.example"

ok "repository ready: ${REPO_DIR} (${N_DEBS} package(s))"
log "sources.list line: ${SOURCES_LINE}"
timer_end "publish-apt-repo"
