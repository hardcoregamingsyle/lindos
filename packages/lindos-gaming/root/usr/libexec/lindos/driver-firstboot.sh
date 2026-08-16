#!/bin/bash
# driver-firstboot.sh — detect recommended GPU/Wi-Fi/audio drivers once at first boot.
#
# Called by lindos-driver-firstboot.service.  It runs `lindos-drivers autodetect --json`, records
# the recommendations, and — when the machine is online and NOT an OEM image — writes an install
# "offer" file for the OOBE / Lindos Settings to surface.  It NEVER installs proprietary drivers
# by itself: installation always needs the user's first-boot/OOBE consent (SPEC-VM.md section 24).
# Guarded, idempotent (marker /var/lib/lindos/driver-firstboot.done), never blocks boot, always
# exits 0 (best effort) unless --strict.
set -Eeuo pipefail

STATE_DIR="/var/lib/lindos"
MARKER="${STATE_DIR}/driver-firstboot.done"
RECOMMEND="${STATE_DIR}/driver-recommendations.json"
OFFER="${STATE_DIR}/driver-install-offer.json"
SYSTEM_JSON="/etc/lindos/system.json"
LOG_DIR="/var/log/lindos"
LOG_FILE="${LOG_DIR}/driver-firstboot.log"
FORCE=0
STRICT=0

log() {
    printf 'lindos-drivers firstboot: %s\n' "$*" >&2
    if [ -d "${LOG_DIR}" ]; then
        printf '%s %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

die() {
    log "error: $*"
    if [ "${STRICT}" = 1 ]; then
        exit 1
    fi
    exit 0
}

usage() {
    cat <<'EOF'
usage: driver-firstboot.sh [--force] [--strict]
  --force   run even if the marker file exists
  --strict  exit non-zero on failure (default: always exit 0, best effort)
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

in_chroot() {
    if have systemd-detect-virt && systemd-detect-virt --quiet --chroot; then
        return 0
    fi
    if have ischroot && ischroot; then
        return 0
    fi
    if have systemd-detect-virt && systemd-detect-virt --quiet --container; then
        return 0
    fi
    [ ! -d /run/systemd/system ]
}

online() {
    [ -n "${LINDOS_OFFLINE:-}" ] && return 1
    if have nm-online && nm-online -x -q -t 5 >/dev/null 2>&1; then
        return 0
    fi
    local u
    for u in "http://archive.ubuntu.com/" "http://packages.linuxmint.com/" "https://deb.debian.org/"; do
        if have curl && curl -s --max-time 6 -o /dev/null "${u}"; then
            return 0
        elif have wget && wget -q --spider --timeout=6 "${u}"; then
            return 0
        fi
    done
    return 1
}

is_oem() {
    # OEM images set {"oem": true} in /etc/lindos/system.json (lindos-core config).
    [ -f "${SYSTEM_JSON}" ] || return 1
    if have python3; then
        python3 - "${SYSTEM_JSON}" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.exit(0 if json.load(fh).get("oem") is True else 1)
except Exception:
    sys.exit(1)
PY
        return $?
    fi
    grep -Eq '"oem"[[:space:]]*:[[:space:]]*true' "${SYSTEM_JSON}"
}

has_recommendations() {
    # true if the recommendations JSON lists any gpu/wifi/audio package
    [ -s "${RECOMMEND}" ] || return 1
    if have python3; then
        python3 - "${RECOMMEND}" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        r = json.load(fh).get("recommended", {})
    sys.exit(0 if (r.get("gpu") or r.get("wifi") or r.get("audio")) else 1)
except Exception:
    sys.exit(1)
PY
        return $?
    fi
    return 0
}

write_offer() {
    # write_offer <offer-bool> <online-bool> <oem-bool>
    local offer="$1" onl="$2" oem="$3"
    if [ "${offer}" = "true" ]; then
        log "writing install offer (${OFFER}); OOBE/Settings will ask for consent"
    else
        log "not offering install (online=${onl} oem=${oem}); recommendations recorded only"
    fi
    printf '{"offer":%s,"online":%s,"oem":%s,"recommendations":"%s","command":"lindos-drivers install --auto"}\n' \
        "${offer}" "${onl}" "${oem}" "${RECOMMEND}" >"${OFFER}" 2>/dev/null || \
        log "warning: could not write ${OFFER}"
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) FORCE=1 ;;
            --strict) STRICT=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage; exit 2 ;;
        esac
        shift
    done
    if [ "$(id -u)" != 0 ]; then
        die "must run as root"
    fi
    if in_chroot; then
        log "inside a chroot/container: deferring driver autodetect to first boot"
        exit 0
    fi
    mkdir -p "${STATE_DIR}" "${LOG_DIR}" 2>/dev/null || true
    chmod 0755 "${STATE_DIR}" 2>/dev/null || true
    if [ "${FORCE}" != 1 ] && [ -e "${MARKER}" ]; then
        log "already done (${MARKER}); use --force to re-run"
        exit 0
    fi
    if ! have lindos-drivers; then
        die "lindos-drivers not found"
    fi
    log "running lindos-drivers autodetect"
    if lindos-drivers autodetect --json >"${RECOMMEND}.tmp" 2>>"${LOG_FILE}"; then
        mv -f "${RECOMMEND}.tmp" "${RECOMMEND}" 2>/dev/null || true
    else
        rm -f "${RECOMMEND}.tmp" 2>/dev/null || true
        : >"${MARKER}"
        die "autodetect failed (see ${LOG_FILE})"
    fi

    local onl="false" oem="false" offer="false"
    if online; then
        onl="true"
    fi
    if is_oem; then
        oem="true"
    fi
    if [ "${onl}" = "true" ] && [ "${oem}" = "false" ] && has_recommendations; then
        offer="true"
    fi
    write_offer "${offer}" "${onl}" "${oem}"

    : >"${MARKER}"
    log "done — recommendations in ${RECOMMEND}; run 'lindos-drivers install --auto' to install"
    exit 0
}

main "$@"
