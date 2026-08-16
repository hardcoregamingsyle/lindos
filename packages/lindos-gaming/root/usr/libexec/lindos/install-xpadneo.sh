#!/bin/bash
# install-xpadneo.sh — OPTIONAL: build and install the xpadneo DKMS driver
# (Xbox One / Series controllers over Bluetooth: full rumble, battery, correct
# button mapping).  Not needed for USB or for the Xbox Wireless Adapter.
#
# Usage: install-xpadneo.sh install|remove|status
#   XPADNEO_VERSION   git tag to build (default: pinned below; 'latest' picks the newest tag)
#   XPADNEO_REPO      git URL (default: https://github.com/atar-axis/xpadneo.git)
#
# Runs as root (pkexec/sudo by the caller — e.g. `lindos-drivers xpadneo install`).
# Needs internet access (git clone + apt for dkms/headers): exits 3 when offline.
# Secure Boot: DKMS modules are signed with Ubuntu's MOK key if one is enrolled;
# otherwise the module will not load until you enrol a key (mokutil) or disable
# Secure Boot.
# Exit codes: 0 ok · 1 error · 2 usage / not root · 3 offline
set -Eeuo pipefail

PROG="install-xpadneo"
XPADNEO_REPO="${XPADNEO_REPO:-https://github.com/atar-axis/xpadneo.git}"
XPADNEO_VERSION="${XPADNEO_VERSION:-v0.9.6}"
LOG_FILE="/var/log/lindos/install-xpadneo.log"
SRC_DIR="/usr/src/lindos-xpadneo"

log() {
    local msg
    msg="[$(date '+%F %T')] ${PROG}: $*"
    printf '%s\n' "${msg}" >&2
    if mkdir -p "$(dirname "${LOG_FILE}")" 2>/dev/null; then
        printf '%s\n' "${msg}" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    local code="${2:-1}"
    log "ERROR: $1"
    exit "${code}"
}

have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    printf 'Usage: %s install|remove|status\n' "$0"
    printf '  XPADNEO_VERSION=%s (or "latest")  XPADNEO_REPO=%s\n' "${XPADNEO_VERSION}" "${XPADNEO_REPO}"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (use: pkexec $0 $*)" 2
}

online() {
    [ -n "${LINDOS_OFFLINE:-}" ] && return 1
    if have curl; then
        curl -s --max-time 6 -o /dev/null "https://github.com/" && return 0
    elif have wget; then
        wget -q --spider --timeout=6 "https://github.com/" && return 0
    fi
    return 1
}

apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    log "apt-get install $*"
    apt-get install -y -qq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
}

resolve_tag() {
    # prints the tag to build; falls back to the newest tag when the pin is unknown
    local want="${XPADNEO_VERSION}" tags newest
    tags="$(git ls-remote --tags --refs "${XPADNEO_REPO}" 2>/dev/null | awk -F/ '{print $NF}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true)"
    if [ -z "${tags}" ]; then
        # could not list tags: try the pin as-is
        printf '%s\n' "${want}"
        return 0
    fi
    newest="$(printf '%s\n' "${tags}" | sort -V | tail -n1)"
    if [ "${want}" = "latest" ]; then
        printf '%s\n' "${newest}"
    elif printf '%s\n' "${tags}" | grep -qx "${want}"; then
        printf '%s\n' "${want}"
    else
        log "pinned tag ${want} not found upstream; using newest ${newest}"
        printf '%s\n' "${newest}"
    fi
}

dkms_status() {
    if have dkms; then
        dkms status 2>/dev/null | grep -i xpadneo || true
    fi
}

do_status() {
    local st
    st="$(dkms_status)"
    if [ -n "${st}" ]; then
        printf 'xpadneo: installed (dkms)\n  %s\n' "${st}"
    else
        printf 'xpadneo: not installed\n'
    fi
    if [ -d /sys/module/hid_xpadneo ]; then
        printf '  kernel module hid_xpadneo: loaded\n'
    else
        printf '  kernel module hid_xpadneo: not loaded\n'
    fi
    if have mokutil; then
        printf '  Secure Boot: %s\n' "$(mokutil --sb-state 2>/dev/null | head -n1 || echo unknown)"
    fi
}

do_install() {
    require_root install
    online || die "offline — xpadneo needs to be fetched from GitHub and built (dkms, kernel headers)" 3
    local kver tag work
    kver="$(uname -r)"
    apt_install dkms git build-essential "linux-headers-${kver}" \
        || apt_install dkms git build-essential linux-headers-generic \
        || die "could not install build dependencies (dkms, git, kernel headers)"
    tag="$(resolve_tag)"
    log "building xpadneo ${tag} from ${XPADNEO_REPO}"
    work="$(mktemp -d)"
    trap 'rm -rf "${work}"' EXIT
    git clone --quiet --depth 1 --branch "${tag}" "${XPADNEO_REPO}" "${work}/xpadneo" \
        || die "git clone of ${XPADNEO_REPO} (${tag}) failed"
    if [ -n "$(dkms_status)" ]; then
        log "an older xpadneo dkms module is present; removing it first"
        (cd "${work}/xpadneo" && ./uninstall.sh >/dev/null 2>&1) || true
    fi
    rm -rf "${SRC_DIR}"
    cp -a "${work}/xpadneo" "${SRC_DIR}"
    (cd "${SRC_DIR}" && ./install.sh) || die "xpadneo install.sh failed (see above; kernel ${kver})"
    if have modprobe; then
        modprobe hid_xpadneo 2>/dev/null || log "module built but not loaded yet (Secure Boot MOK or reboot needed)"
    fi
    log "xpadneo ${tag} installed. Pair the controller over Bluetooth (hold the pair button) — remove and re-pair if it was paired before."
    do_status
}

do_remove() {
    require_root remove
    if [ -d "${SRC_DIR}" ] && [ -x "${SRC_DIR}/uninstall.sh" ]; then
        (cd "${SRC_DIR}" && ./uninstall.sh) || log "uninstall.sh reported errors (continuing)"
    elif have dkms; then
        local line ver
        line="$(dkms status 2>/dev/null | grep -i xpadneo | head -n1 || true)"
        ver="$(printf '%s' "${line}" | sed -E 's#^[^/]+/([^,: ]+).*#\1#')"
        [ -n "${ver}" ] && dkms remove "hid-xpadneo/${ver}" --all || true
    fi
    rm -rf "${SRC_DIR}"
    log "xpadneo removed"
    do_status
}

main() {
    case "${1:-}" in
        install) do_install ;;
        remove)  do_remove ;;
        status)  do_status ;;
        -h|--help|"") usage; [ -n "${1:-}" ] && exit 0; exit 2 ;;
        *) usage >&2; die "unknown action: $1" 2 ;;
    esac
}

main "$@"
