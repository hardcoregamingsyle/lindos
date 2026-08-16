#!/bin/bash
# install-nbfc.sh — install nbfc-linux (NoteBook FanControl) from its pinned GitHub release.
#
# Runs ONLY on explicit request (lindos-tune fan set → hint, Lindos Settings → Hardware → Fans).
# Never called from postinst or the ISO build.  Needs root (callers use pkexec/sudo; no sudo
# inside).  Downloads the Linux Mint 22 / Ubuntu noble .deb of the pinned version, verifies the
# SHA-256 when NBFC_SHA256 is set (empty = warn), installs it with apt, enables nbfc_service and
# prints the model recommendation so the user can apply a config:
#     nbfc config -r          # recommend configs for this laptop
#     nbfc config -a "<name>" # apply one
#     nbfc start && lindos-tune fan set balanced
#
# usage: install-nbfc.sh [--yes] [--version VER] [--sha256 HEX] [--dry-run] [--no-enable]
set -Eeuo pipefail

NBFC_VERSION="${NBFC_VERSION:-0.5.3}"
NBFC_SHA256="${NBFC_SHA256:-}"          # pin for releases; empty → download is only TLS-protected (warned)
NBFC_REPO="https://github.com/nbfc-linux/nbfc-linux"
NBFC_ASSETS=("linux-mint-22-nbfc-linux_%s_amd64.deb" "ubuntu-noble-nbfc-linux_%s_amd64.deb")
LOG_DIR="/var/log/lindos"
LOG_FILE="${LOG_DIR}/install-nbfc.log"
ASSUME_YES=0
DRY_RUN=0
ENABLE_SERVICE=1

log() {
    printf 'install-nbfc: %s\n' "$*" >&2
    if [ -d "${LOG_DIR}" ]; then
        printf '%s %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    log "error: $*"
    exit 1
}

usage() {
    cat <<EOF
usage: install-nbfc.sh [--yes] [--version VER] [--sha256 HEX] [--dry-run] [--no-enable]
  --yes          non-interactive (required when no TTY)
  --version VER  nbfc-linux release tag (default ${NBFC_VERSION})
  --sha256 HEX   expected SHA-256 of the .deb (default: \$NBFC_SHA256, empty = warn only)
  --dry-run      print what would be done
  --no-enable    do not enable/start nbfc_service after installing
EOF
}

run() {
    if [ "${DRY_RUN}" = 1 ]; then
        log "[dry-run] $*"
        return 0
    fi
    "$@"
}

fetch() {
    # fetch URL DEST
    local url="$1" dest="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 20 -o "${dest}" "${url}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${dest}" "${url}"
    else
        die "neither curl nor wget is installed"
    fi
}

online() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsI --connect-timeout 8 "${NBFC_REPO}" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q --spider --timeout=8 "${NBFC_REPO}" >/dev/null 2>&1
    else
        return 1
    fi
}

confirm() {
    if [ "${ASSUME_YES}" = 1 ] || [ "${DRY_RUN}" = 1 ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        die "no terminal — pass --yes to install non-interactively"
    fi
    printf 'Install nbfc-linux %s from %s ? [y/N] ' "${NBFC_VERSION}" "${NBFC_REPO}" >&2
    local answer
    read -r answer
    case "${answer}" in
        y|Y|yes|YES) return 0 ;;
        *) die "cancelled" ;;
    esac
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --yes|-y) ASSUME_YES=1 ;;
            --version) shift; NBFC_VERSION="${1:-}"; [ -n "${NBFC_VERSION}" ] || die "--version needs a value" ;;
            --sha256) shift; NBFC_SHA256="${1:-}" ;;
            --dry-run) DRY_RUN=1 ;;
            --no-enable) ENABLE_SERVICE=0 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 2 ;;
        esac
        shift
    done
    if [ "$(id -u)" != 0 ] && [ "${DRY_RUN}" != 1 ]; then
        die "must run as root (pkexec/sudo install-nbfc.sh)"
    fi
    if command -v nbfc >/dev/null 2>&1 && [ "${DRY_RUN}" != 1 ]; then
        log "nbfc-linux is already installed ($(nbfc --version 2>/dev/null | head -n1 || echo present))"
        log "next: nbfc config -r ; nbfc config -a \"<name>\" ; nbfc start ; lindos-tune fan set balanced"
        exit 0
    fi
    case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
        amd64|x86_64) ;;
        *) die "nbfc-linux .deb releases are amd64 only" ;;
    esac
    mkdir -p "${LOG_DIR}" 2>/dev/null || true
    if ! online; then
        die "offline: cannot download nbfc-linux (${NBFC_REPO}); retry when connected"
    fi
    confirm

    local tmp
    tmp="$(mktemp -d /tmp/lindos-nbfc.XXXXXX)"
    trap 'rm -rf "${tmp}"' EXIT
    local deb="" asset url
    for pattern in "${NBFC_ASSETS[@]}"; do
        # shellcheck disable=SC2059  # pattern is a printf template on purpose
        asset="$(printf "${pattern}" "${NBFC_VERSION}")"
        url="${NBFC_REPO}/releases/download/${NBFC_VERSION}/${asset}"
        log "downloading ${url}"
        if [ "${DRY_RUN}" = 1 ]; then
            deb="${tmp}/${asset}"
            break
        fi
        if fetch "${url}" "${tmp}/${asset}"; then
            deb="${tmp}/${asset}"
            break
        fi
        log "asset ${asset} not available, trying next"
    done
    [ -n "${deb}" ] || die "no release asset found for nbfc-linux ${NBFC_VERSION}"

    if [ "${DRY_RUN}" != 1 ]; then
        if [ -n "${NBFC_SHA256}" ]; then
            printf '%s  %s\n' "${NBFC_SHA256}" "${deb}" | sha256sum -c --quiet - || die "SHA-256 mismatch for ${deb}"
            log "checksum ok"
        else
            log "warning: no SHA-256 pinned for this release — trusting GitHub TLS (set NBFC_SHA256 to pin)"
            log "sha256: $(sha256sum "${deb}" | cut -d' ' -f1)"
        fi
    fi
    export DEBIAN_FRONTEND=noninteractive
    run apt-get install -y -q --no-install-recommends "${deb}"
    if [ "${ENABLE_SERVICE}" = 1 ] && command -v systemctl >/dev/null 2>&1; then
        run systemctl daemon-reload
        run systemctl enable nbfc_service.service
    fi
    log "installed nbfc-linux ${NBFC_VERSION}"
    if [ "${DRY_RUN}" != 1 ] && command -v nbfc >/dev/null 2>&1; then
        log "recommended configs for this machine (nbfc config -r):"
        nbfc config -r 2>&1 | head -n 12 >&2 || true
    fi
    log "next: nbfc config -a \"<name>\" ; nbfc start ; lindos-tune fan set balanced"
    exit 0
}

main "$@"
