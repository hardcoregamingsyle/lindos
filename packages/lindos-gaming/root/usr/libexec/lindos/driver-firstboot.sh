#!/bin/bash
# driver-firstboot.sh — the SILENT retry for the drivers step on the installed system.
#
# The INSTALLER installs drivers while installing (free drivers + firmware always; a proprietary
# GPU driver only with the user's consent and never when Secure Boot would need a key enrolment)
# and records the result in /var/lib/lindos/install-state.json, step 'drivers'.  This script, run by
# lindos-driver-firstboot.service, only covers what the installer could not do:
#   * install-state says 'done' or 'skipped' -> write the marker and exit at once;
#   * 'pending', 'failed' or no record       -> while ONLINE (after a bounded wait for
#     NetworkManager: wait-for-network), retry silently (no window, no wizard, no notification)
#     and record the outcome with 'lindos.installstate mark'.
#
# What a retry installs never goes beyond what the installer was allowed to:
#   * default                                   -> 'ubuntu-drivers install --free-only' (nothing
#                                                  proprietary);
#   * /var/lib/lindos/driver-proprietary-consent exists (the installer writes it when the user
#     agreed to proprietary drivers) -> 'lindos-drivers install --auto', except that an NVIDIA GPU
#     with Secure Boot enabled stays on the free path (a DKMS module would need a MOK enrolment,
#     an interactive blue screen at the next boot) and the step is recorded as 'skipped'.
# It never asks anything: the old "install offer" file is gone - nothing in Lindos read it.
#
# apt discipline: the Chrome retry (lindos-browser-firstboot.service) wakes up at the same moment and
# the unit is ordered after it, but apt does not queue by itself, so the retry command also runs through
# lindos-core's apt-serialise helper: it takes its turn behind every other Lindos apt job (flock) and
# exports DPkg::Lock::Timeout (APT_CONFIG, asked for with --apt-config), so the apt-get inside lindos-drivers /
# ubuntu-drivers waits for a dpkg lock
# somebody else holds instead of failing at once - a lost attempt would be counted against the three tries.
#
# Never runs in the live session, in a chroot/container, or while Ubiquity's oem-config first-boot
# wizard is still pending (the new user's account does not exist yet).  Guarded, idempotent
# (marker /var/lib/lindos/driver-firstboot.done, attempt counter capped so a broken driver is not
# rebuilt on every boot), never blocks boot, always exits 0 (best effort) unless --strict.
# LINDOS_ROOT prefixes every path (tests); LINDOS_TEST_IN_CHROOT=0|1 overrides the chroot check.
set -Eeuo pipefail

ROOT="${LINDOS_ROOT:-}"
LIBEXEC="${ROOT}/usr/libexec/lindos"
IS_LIVE_SESSION="${LIBEXEC}/is-live-session"
OEM_CONFIG_PENDING="${LIBEXEC}/oem-config-pending"
WAIT_FOR_NETWORK="${LIBEXEC}/wait-for-network"
APT_SERIALISE="${LIBEXEC}/apt-serialise"
PY="${LINDOS_PYTHON:-python3}"
STATE_DIR="${ROOT}/var/lib/lindos"
MARKER="${STATE_DIR}/driver-firstboot.done"
RECOMMEND="${STATE_DIR}/driver-recommendations.json"
CONSENT="${STATE_DIR}/driver-proprietary-consent"
ATTEMPTS="${STATE_DIR}/driver-firstboot.attempts"
MAX_ATTEMPTS=3
RETRY_TIMEOUT="${LINDOS_DRIVER_RETRY_TIMEOUT:-900}"
# how long the retry may queue behind another Lindos apt job before it runs anyway (the time limit above
# starts only once it is its turn)
LOCK_WAIT="${LINDOS_DRIVER_LOCK_WAIT:-180}"
LOG_DIR="${ROOT}/var/log/lindos"
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
    if [ -n "${LINDOS_TEST_IN_CHROOT:-}" ]; then
        [ "${LINDOS_TEST_IN_CHROOT}" = 1 ]
        return
    fi
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

# The shared helpers of lindos-core (single source of truth, also used by browser-firstboot.sh):
# is-live-session (unknown = "live": never act on a guess) and oem-config-pending.
is_live_session() {
    [ -f "${IS_LIVE_SESSION}" ] || return 0
    bash "${IS_LIVE_SESSION}"
}

oem_config_pending() {
    [ -f "${OEM_CONFIG_PENDING}" ] && bash "${OEM_CONFIG_PENDING}"
}

# Waits (bounded) for NetworkManager first: Lindos masks NetworkManager-wait-online, so the unit's
# After=network-online.target comes before Wi-Fi/DHCP is up and a bare probe would say "offline" on
# every boot.  "Cannot tell" (shared helper missing, no NetworkManager) counts as ready.
network_ready() {
    [ -f "${WAIT_FOR_NETWORK}" ] || return 0
    bash "${WAIT_FOR_NETWORK}"
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

# install-state.json (lindos.installstate; honours LINDOS_ROOT): the recorded status of the
# 'drivers' step, or "" when unknown / the module is unavailable.  Never fails the caller.
state_status() {
    "${PY}" -m lindos.installstate status drivers 2>/dev/null | tr -d '\r' || true
}

# state_mark <status> [detail] — record the outcome; a failure to record is only a warning.
state_mark() {
    "${PY}" -m lindos.installstate mark drivers "$1" "${2:-}" >/dev/null 2>&1 || \
        log "warning: could not record install-state drivers=$1 (non-fatal)"
}

# True when UEFI Secure Boot is on (mokutil, else the SecureBoot efivar's last byte).
secure_boot_enabled() {
    local out f
    if have mokutil; then
        out="$(mokutil --sb-state 2>&1 || true)"
        if grep -qi '^SecureBoot enabled' <<<"${out}"; then
            return 0
        fi
        if grep -qi '^SecureBoot disabled' <<<"${out}"; then
            return 1
        fi
    fi
    for f in "${ROOT}"/sys/firmware/efi/efivars/SecureBoot-*; do
        [ -r "${f}" ] || continue
        [ "$(tail -c 1 "${f}" 2>/dev/null | od -An -tu1 | tr -d '[:space:]')" = "1" ] && return 0
    done
    return 1
}

# True when the recorded autodetect result lists an NVIDIA GPU.
recommends_nvidia() {
    [ -s "${RECOMMEND}" ] || return 1
    "${PY}" - "${RECOMMEND}" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        vendors = json.load(fh).get("gpu_vendors") or []
    sys.exit(0 if "nvidia" in vendors else 1)
except Exception:
    sys.exit(1)
PY
}

# Run the retry command with a time limit; prints nothing to the user, output goes to the log.
# Returns the command's exit status (124 = timed out).  It first takes its turn behind every other Lindos
# apt job and gets a dpkg lock wait for every apt-get it starts (shared apt-serialise helper of
# lindos-core; without it the command simply runs as before).  The time limit is inside the queue: it
# counts from the moment the command really starts.
run_retry() {
    local -a cmd=("$@")
    if have timeout; then
        cmd=(timeout -k 30 "${RETRY_TIMEOUT}" "${cmd[@]}")
    fi
    if [ -f "${APT_SERIALISE}" ]; then
        # --apt-config: the apt-get inside lindos-drivers / ubuntu-drivers cannot be given '-c FILE' from here, so this
        # command (and only this one) gets the lock wait as APT_CONFIG; install-browser.sh and the repair never do (a
        # maintainer script that assigns APT_CONFIG itself, Google Chrome's, would break on the exported value)
        cmd=(bash "${APT_SERIALISE}" --wait "${LOCK_WAIT}" --apt-config -- "${cmd[@]}")
    fi
    log "retrying: ${cmd[*]}"
    local rc=0
    set +e
    "${cmd[@]}" >>"${LOG_FILE}" 2>&1 </dev/null
    rc=$?
    set -e
    return "${rc}"
}

retry_drivers() {
    local proprietary=0 note="" rc=0
    if [ -e "${CONSENT}" ]; then
        proprietary=1
    fi
    # Unknown hardware (autodetect failed) counts as "maybe NVIDIA": Secure Boot decides the same way.
    if [ "${proprietary}" = 1 ] && secure_boot_enabled && { [ ! -s "${RECOMMEND}" ] || recommends_nvidia; }; then
        proprietary=0
        note="proprietary NVIDIA driver left for Settings: Secure Boot needs a key enrolment"
        log "${note}"
    fi

    if [ "${proprietary}" = 1 ]; then
        have lindos-drivers || { state_mark failed "lindos-drivers missing"; return 0; }
        run_retry lindos-drivers install --auto || rc=$?
    else
        if ! have ubuntu-drivers; then
            state_mark skipped "ubuntu-drivers not installed"
            : >"${MARKER}"
            return 0
        fi
        run_retry ubuntu-drivers install --free-only || rc=$?
    fi

    case "${rc}" in
        0)
            if [ -n "${note}" ]; then
                state_mark skipped "${note}"
            elif [ "${proprietary}" = 1 ]; then
                state_mark "done" "lindos-drivers install --auto finished at first boot"
            else
                state_mark "done" "free drivers only (a proprietary GPU driver needs consent: Settings)"
            fi
            : >"${MARKER}"
            log "done"
            ;;
        3)
            state_mark pending "offline at first boot"
            log "offline — will retry on a later boot"
            ;;
        124)
            state_mark failed "timed out after ${RETRY_TIMEOUT}s"
            log "timed out — will retry on a later boot"
            ;;
        *)
            state_mark failed "driver install exit ${rc}"
            log "driver install failed (exit ${rc}, see ${LOG_FILE}) — will retry on a later boot"
            ;;
    esac
    return 0
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
    # Read-only guards first (honest "no" whatever the privilege, so hermetic tests can run them):
    if is_live_session; then
        log "live/ISO session detected (boot=casper) — refusing to run (installed systems only)"
        exit 0
    fi
    if oem_config_pending; then
        log "the oem-config first-boot wizard is still pending — not before the account exists"
        exit 0
    fi
    if [ "$(id -u)" != 0 ]; then
        die "must run as root"
    fi
    if in_chroot; then
        log "inside a chroot/container: deferring the driver retry to a real boot"
        exit 0
    fi
    mkdir -p "${STATE_DIR}" "${LOG_DIR}" 2>/dev/null || true
    chmod 0755 "${STATE_DIR}" 2>/dev/null || true
    if [ "${FORCE}" != 1 ] && [ -e "${MARKER}" ]; then
        log "already done (${MARKER}); use --force to re-run"
        exit 0
    fi

    # The installer already did (or consciously skipped) this step: nothing left to retry.
    local recorded
    recorded="$(state_status)"
    case "${recorded}" in
        done|skipped)
            log "install-state says drivers=${recorded} — the installer handled it, nothing to do"
            : >"${MARKER}"
            exit 0
            ;;
    esac

    if ! network_ready || ! online; then
        log "offline — will retry on a later boot (install-state: ${recorded:-none})"
        if [ -z "${recorded}" ]; then
            state_mark pending "offline at first boot"
        fi
        exit 0
    fi

    local tries=0
    if [ -r "${ATTEMPTS}" ]; then
        tries="$(tr -dc '0-9' <"${ATTEMPTS}" 2>/dev/null || true)"
        tries="${tries:-0}"
    fi
    tries=$((tries + 1))
    if [ "${FORCE}" != 1 ] && [ "${tries}" -gt "${MAX_ATTEMPTS}" ]; then
        log "giving up after ${MAX_ATTEMPTS} attempts (install-state: ${recorded:-none}); install drivers from Settings"
        : >"${MARKER}"
        exit 0
    fi
    printf '%s\n' "${tries}" >"${ATTEMPTS}" 2>/dev/null || true

    if have lindos-drivers; then
        log "recording what lindos-drivers detects (install-state: ${recorded:-none})"
        if lindos-drivers autodetect --json >"${RECOMMEND}.tmp" 2>>"${LOG_FILE}"; then
            mv -f "${RECOMMEND}.tmp" "${RECOMMEND}" 2>/dev/null || true
        else
            rm -f "${RECOMMEND}.tmp" 2>/dev/null || true
            log "autodetect failed (see ${LOG_FILE}); the free-only retry does not need it"
        fi
    fi

    retry_drivers
    exit 0
}

main "$@"
