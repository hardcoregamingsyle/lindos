#!/bin/bash
# install-xone.sh — OPTIONAL: build and install the xone DKMS driver for Xbox One /
# Series controllers connected through the **Xbox Wireless Adapter** (USB dongle)
# or by USB cable.  Bluetooth is handled by xpadneo, not xone.
#
# Usage: install-xone.sh install [--accept-firmware-license] [--skip-firmware]
#        install-xone.sh remove | status
#
# LICENCE WARNING (dongle support): the Xbox Wireless Adapter needs proprietary
# firmware that xone downloads from Microsoft's own servers (the Windows driver
# package, extracted with cabextract).  Lindos does not ship or redistribute
# that firmware.  By passing --accept-firmware-license you confirm you accept
# Microsoft's licence terms for that download.  Without the flag the driver
# is built but the dongle firmware is NOT fetched (USB-cable use still works).
#
# Notes: xone replaces the in-kernel 'xpad' driver for these devices (it is
# blacklisted by xone's installer).  Secure Boot: DKMS modules need a MOK-
# enrolled key.  Needs internet access: exits 3 when offline.
#   XONE_REPO   git URL (default: dlundqvist/xone — maintained fork of medusalix/xone)
#   XONE_REF    branch/tag to build (default: master)
# Exit codes: 0 ok · 1 error · 2 usage / not root / licence not accepted · 3 offline
set -Eeuo pipefail

PROG="install-xone"
XONE_REPO="${XONE_REPO:-https://github.com/dlundqvist/xone.git}"
XONE_REPO_FALLBACK="https://github.com/medusalix/xone.git"
XONE_REF="${XONE_REF:-master}"
LOG_FILE="/var/log/lindos/install-xone.log"
SRC_DIR="/usr/src/lindos-xone"

ACCEPT_LICENSE=0
SKIP_FIRMWARE=0

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
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
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

dkms_status() {
    if have dkms; then
        dkms status 2>/dev/null | grep -i '^xone' || true
    fi
}

do_status() {
    local st
    st="$(dkms_status)"
    if [ -n "${st}" ]; then
        printf 'xone: installed (dkms)\n%s\n' "$(printf '%s\n' "${st}" | sed 's/^/  /')"
    else
        printf 'xone: not installed\n'
    fi
    if [ -e /lib/firmware/xow_dongle.bin ] || [ -e /usr/lib/firmware/xow_dongle.bin ]; then
        printf '  wireless-adapter firmware: present\n'
    else
        printf '  wireless-adapter firmware: absent (install with --accept-firmware-license)\n'
    fi
    if have mokutil; then
        printf '  Secure Boot: %s\n' "$(mokutil --sb-state 2>/dev/null | head -n1 || echo unknown)"
    fi
}

clone_source() {
    local work="$1"
    if git clone --quiet --depth 1 --branch "${XONE_REF}" "${XONE_REPO}" "${work}/xone" 2>/dev/null; then
        return 0
    fi
    log "clone of ${XONE_REPO} (${XONE_REF}) failed; trying ${XONE_REPO_FALLBACK}"
    git clone --quiet --depth 1 "${XONE_REPO_FALLBACK}" "${work}/xone"
}

do_install() {
    require_root install
    if [ "${ACCEPT_LICENSE}" -eq 0 ] && [ "${SKIP_FIRMWARE}" -eq 0 ]; then
        usage >&2
        die "the Xbox Wireless Adapter firmware is proprietary Microsoft software downloaded from Microsoft; re-run with --accept-firmware-license to fetch it, or --skip-firmware to build the driver only (USB cable use)" 2
    fi
    online || die "offline — xone must be fetched from GitHub and built (dkms, kernel headers)" 3
    local kver work
    kver="$(uname -r)"
    apt_install dkms git build-essential curl cabextract "linux-headers-${kver}" \
        || apt_install dkms git build-essential curl cabextract linux-headers-generic \
        || die "could not install build dependencies (dkms, git, cabextract, kernel headers)"
    work="$(mktemp -d)"
    trap 'rm -rf "${work}"' EXIT
    clone_source "${work}" || die "could not clone xone"
    if [ -n "$(dkms_status)" ] && [ -x "${SRC_DIR}/uninstall.sh" ]; then
        log "removing the previously installed xone module first"
        (cd "${SRC_DIR}" && ./uninstall.sh >/dev/null 2>&1) || true
    fi
    rm -rf "${SRC_DIR}"
    cp -a "${work}/xone" "${SRC_DIR}"
    log "building xone (${XONE_REF}) for kernel ${kver}"
    (cd "${SRC_DIR}" && ./install.sh --release) || die "xone install.sh failed (see above)"
    if [ "${SKIP_FIRMWARE}" -eq 0 ]; then
        log "downloading the Xbox Wireless Adapter firmware from Microsoft (licence accepted by user)"
        if [ -x "${SRC_DIR}/install_firmware.sh" ]; then
            (cd "${SRC_DIR}" && ./install_firmware.sh --skip-disclaimer) \
                || log "warning: firmware download failed — the dongle will not work until it succeeds (re-run install)"
        else
            log "warning: install_firmware.sh not found in this xone checkout; dongle firmware not installed"
        fi
    else
        log "firmware download skipped (--skip-firmware): the Xbox Wireless Adapter will NOT work, cable does"
    fi
    log "xone installed. Unplug/replug the controller or adapter."
    do_status
}

do_remove() {
    require_root remove
    if [ -x "${SRC_DIR}/uninstall.sh" ]; then
        (cd "${SRC_DIR}" && ./uninstall.sh) || log "uninstall.sh reported errors (continuing)"
    elif have dkms; then
        local line ver
        line="$(dkms status 2>/dev/null | grep -i '^xone' | head -n1 || true)"
        ver="$(printf '%s' "${line}" | sed -E 's#^[^/]+/([^,: ]+).*#\1#')"
        [ -n "${ver}" ] && dkms remove "xone/${ver}" --all || true
    fi
    rm -rf "${SRC_DIR}"
    rm -f /etc/modprobe.d/xone-blacklist.conf
    log "xone removed (the in-kernel xpad driver is used again after a reboot)"
    do_status
}

main() {
    local action="" arg
    for arg in "$@"; do
        case "${arg}" in
            --accept-firmware-license) ACCEPT_LICENSE=1 ;;
            --skip-firmware)           SKIP_FIRMWARE=1 ;;
            install|remove|status)     action="${arg}" ;;
            -h|--help)                 usage; exit 0 ;;
            *) usage >&2; die "unknown argument: ${arg}" 2 ;;
        esac
    done
    case "${action}" in
        install) do_install ;;
        remove)  do_remove ;;
        status)  do_status ;;
        *) usage >&2; die "action required: install|remove|status" 2 ;;
    esac
}

main "$@"
