#!/bin/bash
# shellcheck shell=bash
# shellcheck disable=SC2034  # settings that only the scripts sourcing this file read
# ============================================================================
#  lib.sh - helpers for the Lindos installer scripts.
#
#  Sourced by target-config.sh (the Ubiquity target-config hook, deployed as
#  /usr/lib/ubiquity/target-config/50lindos-install) and by finalize.sh (the
#  ubiquity/success_command).  Also executable: 'bash lib.sh --enter TARGET CMD...'
#  is the runner that enters the new system (see li_enter_main).
#
#  Rules that hold for everything in here (a hook that hangs or corrupts dpkg
#  breaks EVERY install, so safety beats features):
#    * no 'set -e' and no 'set -u' - the callers must survive any failing command;
#    * stdout is Ubiquity's debconf pipe: nothing in here writes to it, all output
#      goes to the log files (children get stdin from /dev/null, never the pipe);
#    * every command that touches the target runs through li_run/li_dl/li_inst with
#      a 'timeout -k'; killable work (downloads) is clamped to the wall-clock budget,
#      dpkg runs (li_inst) only have a generous hang guard;
#    * loading this file has no side effects (it only defines variables and functions).
#
#  Test seams (unset on a real installation): LINDOS_TARGET, LINDOS_INSTALLER_LOG,
#  LINDOS_PYTHON, LINDOS_EXTRAS_JSON, LINDOS_TARGET_RUNNER, LINDOS_INSTALL_BUDGET,
#  LINDOS_TIMEOUT_PCT, LINDOS_TIMEOUT_MIN, LINDOS_FREE_KB, LINDOS_TEST_CMDLINE, LINDOS_MANIFEST_REMOVE, LINDOS_RESOLV_SOURCES,
#  LINDOS_SYS_EFI.
# ============================================================================

LI_LIB="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
LI_VERSION="1.0.0"

TGT="${LINDOS_TARGET:-/target}"
LI_LOG_LIVE="${LINDOS_INSTALLER_LOG:-/var/log/lindos/installer-hook.log}"
LI_PY="${LINDOS_PYTHON:-python3}"
LI_EXTRAS="${LINDOS_EXTRAS_JSON:-/usr/share/lindos/installer/extras.json}"
LI_TEMPLATES="${LINDOS_INSTALLER_TEMPLATES:-/usr/share/lindos/installer/lindos-installer.templates}"
LI_CMDLINE="${LINDOS_TEST_CMDLINE:-/proc/cmdline}"
LI_BUDGET="${LINDOS_INSTALL_BUDGET:-2700}"
LI_TPCT="${LINDOS_TIMEOUT_PCT:-100}"
LI_TMIN="${LINDOS_TIMEOUT_MIN:-1}"
LI_T0="$(date +%s)"
LI_TMPD=""
LI_DB=0
LI_CHILD=""
LI_RC=0
LI_DIRTY=0
LI_AUDIT=""
LI_MSG_ID="lindos-installer/msg"

# The steps of install-state.json, in the order the installer runs them: what matters most to a new
# user first (the default browser, working hardware), the big optional downloads last.
LI_STEPS=(browser drivers updates compat gaming mode_extras flatpaks)
# test seam: run only some of the steps (a space separated list of step ids)
[ -z "${LINDOS_INSTALLER_STEPS:-}" ] || read -r -a LI_STEPS <<<"${LINDOS_INSTALLER_STEPS}"

# Package families that stay exactly as the medium has them until Ubiquity has finished: its own
# files, the kernel and the boot loader (Ubiquity's later steps build the initramfs and install GRUB
# for the kernel that is on the image), and the packages an upgrade would pull those along with.
LI_HOLD_RE='^(ubiquity|oem-config|casper|linux-(image|modules|headers|signed|generic|hwe|oem|tools|lowlatency)|lindos-kernel|grub|shim|mokutil|efibootmgr|os-prober|initramfs-tools|plymouth)'

LI_STATE_DIR_REL="/var/lib/lindos"
LI_HOLD_FILE_REL="/var/lib/lindos/installer-holds"
LI_APT_CONF_REL="/var/lib/lindos/installer-apt.conf"
LI_PIN_FILE_REL="/etc/apt/preferences.d/00lindos-installer.pref"

# --- logging -----------------------------------------------------------------------------
# li_log MSG... - to stderr (Ubiquity sends it to syslog), the live log and the target's log.
li_log() {
    local line
    line="$(date '+%F %T') lindos-installer: $*"
    printf '%s\n' "${line}" >&2
    printf '%s\n' "${line}" >>"${LI_LOG_LIVE}" 2>/dev/null
    printf '%s\n' "${line}" >>"${TGT}/var/log/lindos/installer.log" 2>/dev/null
    return 0
}

# --- time --------------------------------------------------------------------------------
# li_scale SECONDS - a timeout scaled by LINDOS_TIMEOUT_PCT (100 on a real install), at least
# LINDOS_TIMEOUT_MIN seconds (1).
li_scale() {
    local v=$(( $1 * LI_TPCT / 100 ))
    [ "${v}" -ge "${LI_TMIN}" ] || v="${LI_TMIN}"
    printf '%s\n' "${v}"
}

# li_left - seconds left of the wall-clock budget (negative once it is used up).
li_left() {
    printf '%s\n' $(( LI_BUDGET - ( $(date +%s) - LI_T0 ) ))
}

# --- kernel command line -----------------------------------------------------------------
# li_cmdline_words - the words of the kernel command line, one per line.
li_cmdline_words() {
    local -a words=()
    read -r -a words <"${LI_CMDLINE}" 2>/dev/null
    [ "${#words[@]}" -gt 0 ] || return 0
    printf '%s\n' "${words[@]}" | tr -d '\r'
}

# li_cmdline_has WORD - true when the kernel command line has exactly this word.
li_cmdline_has() {
    li_cmdline_words | grep -qxF -- "$1"
}

# li_cmdline_value KEY - the value of KEY=value ("" when absent).
li_cmdline_value() {
    li_cmdline_words | grep -F -- "$1=" | head -n 1 | cut -d= -f2-
}

# --- install-state.json ------------------------------------------------------------------
# What the hook (or finalize.sh) knows about the steps is kept in LI_ST as well, so nothing has to ask
# the file again; every change still goes through 'lindos.installstate mark', one atomic write each.
declare -A LI_ST=()

# li_mark STEP STATUS [DETAIL] - record an outcome (never fatal).  'done' only ever after a verified success.
li_mark() {
    local step="$1" status="$2"
    shift 2
    li_log "step ${step}: ${status}${1:+ - $*}"
    LI_ST["${step}"]="${status}"
    "${LI_PY}" -m lindos.installstate --root "${TGT}" mark "${step}" "${status}" "$*" \
        </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- || li_log "could not record ${step}=${status} in install-state.json"
    return 0
}

# li_status STEP - the recorded status ("" when nothing is recorded).
li_status() {
    printf '%s\n' "${LI_ST[$1]:-}"
}

# li_load_status - fill LI_ST from the state file (one read); finalize.sh needs what the hook recorded.
li_load_status() {
    local step status
    while read -r step status; do
        [ -n "${step}" ] && LI_ST["${step}"]="${status}"
    done < <("${LI_PY}" - "${TGT}" 3>&- <<'PYEOF' 2>/dev/null | tr -d '\r'
import sys

try:
    from lindos import installstate

    for step, entry in installstate.load(sys.argv[1])["steps"].items():
        sys.stdout.buffer.write(("%s %s\n" % (step, entry["status"])).encode("utf-8"))
except Exception:
    pass
PYEOF
    )
    return 0
}

# li_mark_missing STATUS DETAIL - every step without a recorded status gets STATUS.
li_mark_missing() {
    local s
    for s in "${LI_STEPS[@]}"; do
        [ -n "${LI_ST[${s}]:-}" ] || li_mark "${s}" "$1" "$2"
    done
    return 0
}

# li_set_online true|false
li_set_online() {
    "${LI_PY}" -m lindos.installstate --root "${TGT}" online "$1" </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- || true
    return 0
}

# --- extras.json -------------------------------------------------------------------------
LI_X_APT=()
LI_X_COMPAT=()
LI_X_GAMING=()
LI_X_FLATPAKS=()
LI_X_FIRMWARE=()

# li_extras_load - read extras.json once into LI_X_APT, LI_X_COMPAT, LI_X_GAMING, LI_X_FLATPAKS, LI_X_FIRMWARE.
li_extras_load() {
    local key item tab=$'\t'
    LI_X_APT=(); LI_X_COMPAT=(); LI_X_GAMING=(); LI_X_FLATPAKS=(); LI_X_FIRMWARE=()
    while IFS="${tab}" read -r key item; do
        [ -n "${item}" ] || continue
        case "${key}" in
            apt) LI_X_APT+=("${item}") ;;
            compat) LI_X_COMPAT+=("${item}") ;;
            gaming) LI_X_GAMING+=("${item}") ;;
            flatpaks) LI_X_FLATPAKS+=("${item}") ;;
            firmware) LI_X_FIRMWARE+=("${item}") ;;
        esac
    done < <("${LI_PY}" - "${LI_EXTRAS}" 3>&- <<'PYEOF' 2>/dev/null | tr -d '\r'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
    lists = {"apt": doc.get("apt"), "compat": doc.get("compat"), "gaming": doc.get("gaming"),
             "flatpaks": doc.get("flatpaks"), "firmware": (doc.get("drivers") or {}).get("firmware")}
    for key, items in lists.items():
        for item in items or []:
            if isinstance(item, str) and item.strip():
                sys.stdout.buffer.write(("%s\t%s\n" % (key, item.strip())).encode("utf-8"))
except Exception:
    pass
PYEOF
    )
    return 0
}

# li_json_get FILE KEY DEFAULT - a top-level string of a JSON file, DEFAULT when missing or unreadable.
li_json_get() {
    "${LI_PY}" - "$1" "$2" "$3" 3>&- <<'PYEOF' 2>/dev/null | tr -d '\r'
import json
import sys

value = sys.argv[3]
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        got = json.load(fh).get(sys.argv[2])
    if isinstance(got, str) and got:
        value = got
except Exception:
    pass
sys.stdout.buffer.write((value + "\n").encode("utf-8"))
PYEOF
}

# --- debconf progress text -----------------------------------------------------------------
# li_say TEXT - one line in the installer window (db_progress INFO with a template of ours), plus the log.
# No nested progress bars: a missed STOP after a timeout would desynchronise the filter's bar stack.
li_say() {
    li_log "$*"
    [ "${LI_DB}" = 1 ] || return 0
    db_subst "${LI_MSG_ID}" MSG "$*" >/dev/null 2>&1 || true
    db_progress INFO "${LI_MSG_ID}" >/dev/null 2>&1 || true
    return 0
}

# --- running things in the new system ------------------------------------------------------
# li_enter_prefix - fills LI_ENTER with the command that runs its arguments inside the target: a private
# mount namespace (proc, sys, dev, run vanish with the process - nothing can leak into Ubiquity's own mount
# accounting, even when the command is killed) around 'chroot' (see li_enter_main).
li_enter_prefix() {
    if [ -n "${LINDOS_TARGET_RUNNER:-}" ]; then
        LI_ENTER=("${LINDOS_TARGET_RUNNER}" "${TGT}")
    else
        LI_ENTER=(unshare --mount --propagation private -- "${BASH}" "${LI_LIB}" --enter "${TGT}")
    fi
}

# li_child SECONDS CMD... - run CMD in the target with a time limit; output to the live log.  The child is
# started in the background and waited for, so a signal to the hook is handled at once.  Returns the
# command's status (124 = timed out, 137 = killed after the grace period, 97 = could not enter the target).
li_child() {
    local secs="$1" grace
    shift
    grace="$(li_scale 20)"
    li_enter_prefix
    timeout -k "${grace}" "${secs}" "${LI_ENTER[@]}" "$@" </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- &
    LI_CHILD=$!
    wait "${LI_CHILD}"
    LI_RC=$?
    LI_CHILD=""
    if [ "${LI_RC}" -eq 124 ] || [ "${LI_RC}" -eq 137 ]; then
        li_log "timed out after ${secs}s: $*"
    fi
    return "${LI_RC}"
}

# li_run SECONDS CMD... - a short job (fixed limit, not clamped to the budget).
li_run() {
    local secs
    secs="$(li_scale "$1")"
    shift
    li_child "${secs}" "$@"
}

# li_dl SECONDS CMD... - a killable job (download, package lists): clamped to what is left of the
# budget; 125 = no time left.
li_dl() {
    local secs left
    secs="$(li_scale "$1")"
    shift
    left="$(li_left)"
    [ "${secs}" -le "${left}" ] || secs="${left}"
    if [ "${secs}" -lt "$(li_scale 10)" ]; then
        li_log "no time left for: $*"
        return 125
    fi
    li_child "${secs}" "$@"
}

# li_inst SECONDS CMD... - a dpkg run: SECONDS is only a hang guard, generous and NOT clamped to the
# budget, because killing dpkg mid-transaction is what leaves the system half-configured.
li_inst() {
    local secs
    secs="$(li_scale "$1")"
    shift
    li_child "${secs}" "$@"
}

# li_run_out SECONDS CMD... - like li_run, but the command's stdout is our stdout (stderr goes to the log).
li_run_out() {
    local secs grace
    secs="$(li_scale "$1")"
    grace="$(li_scale 20)"
    shift
    li_enter_prefix
    timeout -k "${grace}" "${secs}" "${LI_ENTER[@]}" "$@" </dev/null 2>>"${LI_LOG_LIVE}" 3>&-
}

# li_settle - a moment for the kernel to reap a killed apt/dpkg before the next run (dpkg's own lock
# wait, Dpkg::Lock::Timeout in the installer's apt.conf, covers the rest).
li_settle() {
    sleep "$(li_scale 3)"
}

# The apt configuration every apt call inside the target uses (APT_CONFIG): the 'deb cdrom:' source is
# never consulted, the medium's lists are never cleaned, waiting for a lock beats failing.
li_apt_conf_write() {
    local f="${TGT}${LI_APT_CONF_REL}"
    mkdir -p "${TGT}${LI_STATE_DIR_REL}" 2>/dev/null
    cat >"${f}" 2>/dev/null <<'CONFEOF'
// Written by the Lindos installer hook; removed when the hook ends.
Dir::Etc::SourceList "/dev/null";
APT::Get::List-Cleanup "0";
Acquire::Retries "2";
Acquire::http::Timeout "20";
Acquire::https::Timeout "20";
Acquire::Languages "none";
DPkg::Lock::Timeout "120";
Dpkg::Options { "--force-confdef"; "--force-confold"; };
CONFEOF
    [ -s "${f}" ] || return 1
    export LINDOS_CHROOT_APT_CONFIG="${LI_APT_CONF_REL}"
    return 0
}

li_apt_conf_remove() {
    rm -f "${TGT}${LI_APT_CONF_REL}" 2>/dev/null
    unset LINDOS_CHROOT_APT_CONFIG
    return 0
}

# --- network -----------------------------------------------------------------------------
# li_reach URL - true when the URL answers within a few seconds (wget, then curl; both time-boxed).
li_reach() {
    if command -v wget >/dev/null 2>&1; then
        timeout -k 2 "$(li_scale 15)" wget -q --spider --timeout=8 --tries=1 "$1" </dev/null >/dev/null 2>&1 3>&- && return 0
    fi
    if command -v curl >/dev/null 2>&1; then
        timeout -k 2 "$(li_scale 15)" curl -fsSI --connect-timeout 5 --max-time 10 "$1" </dev/null >/dev/null 2>&1 3>&- && return 0
    fi
    return 1
}

# li_online - true when the package archives answer.  Ubiquity's own connectivity test is not exposed
# to hooks, so the hook asks for itself (a captive portal or a black-holed route fails within seconds).
li_online() {
    li_reach "http://archive.ubuntu.com/ubuntu/" || li_reach "http://packages.linuxmint.com/"
}

# --- what the archives carry ---------------------------------------------------------------
LI_CAND=()

# li_candidates PKG... - fills LI_CAND with those packages that apt has a candidate for (a package no archive
# carries is not an error: apt-cache only warns).  Returns the status of 'apt-cache policy'.
li_candidates() {
    local policy rc
    LI_CAND=()
    policy="$(li_run_out 120 apt-cache policy "$@")"
    rc=$?
    [ "${rc}" -eq 0 ] || return "${rc}"
    mapfile -t LI_CAND < <(printf '%s\n' "${policy}" | awk '
        /^[^ ]/ { name = $1; sub(/:$/, "", name) }
        /^ +Candidate:/ { if ($2 != "(none)") print name }')
    return 0
}

# --- disk space --------------------------------------------------------------------------
li_free_kb() {
    if [ -n "${LINDOS_FREE_KB:-}" ]; then
        printf '%s\n' "${LINDOS_FREE_KB}"
        return 0
    fi
    df -Pk "${TGT}" 2>/dev/null </dev/null | awk 'NR==2 {print $4}'
}

# li_free_ok KILOBYTES - true when the target has at least that much free (or when df cannot tell).
li_free_ok() {
    local have
    have="$(li_free_kb)"
    [ -n "${have}" ] || return 0
    [ "${have}" -ge "$1" ] 2>/dev/null
}

# --- hardware facts for the drivers step -------------------------------------------------
# li_secure_boot - enabled | disabled | unknown.  Legacy BIOS boots have no Secure Boot.
li_secure_boot() {
    local out efi f last
    if command -v mokutil >/dev/null 2>&1; then
        out="$(timeout -k 2 "$(li_scale 10)" mokutil --sb-state </dev/null 2>&1 3>&-)"
        case "${out}" in
            *"SecureBoot enabled"*) echo enabled; return 0 ;;
            *"SecureBoot disabled"*) echo disabled; return 0 ;;
        esac
    fi
    efi="${LINDOS_SYS_EFI:-/sys/firmware/efi}"
    if [ ! -d "${efi}" ]; then
        echo disabled
        return 0
    fi
    for f in "${efi}"/efivars/SecureBoot-*; do
        [ -r "${f}" ] || continue
        last="$(tail -c 1 "${f}" 2>/dev/null </dev/null | od -An -tu1 | tr -d '[:space:]')"
        case "${last}" in
            1) echo enabled; return 0 ;;
            0) echo disabled; return 0 ;;
        esac
    done
    echo unknown
}

# li_gpu_may_be_nvidia - true unless lspci shows display adapters and none of them is NVIDIA
# (unknown hardware counts as "maybe": Secure Boot then decides the same way).
li_gpu_may_be_nvidia() {
    local gpus
    command -v lspci >/dev/null 2>&1 || return 0
    gpus="$(lspci -nn 2>/dev/null </dev/null | grep -Ei 'VGA compatible|3D controller|Display controller')"
    [ -n "${gpus}" ] || return 0
    printf '%s\n' "${gpus}" | grep -qi nvidia
}

# li_nonfree_consent - the user agreed to proprietary drivers: the installer's "third-party software"
# checkbox (ubiquity/use_nonfree) or the kernel word lindos.proprietary_drivers=1.
li_nonfree_consent() {
    li_cmdline_has "lindos.proprietary_drivers=1" && return 0
    [ "${LI_DB}" = 1 ] || return 1
    db_get ubiquity/use_nonfree >/dev/null 2>&1 || return 1
    # shellcheck disable=SC2154  # RET is set by debconf's db_get
    [ "${RET:-}" = "true" ]
}

# --- the target's state: policy-rc.d, DNS, holds, repair ------------------------------------
LI_PRC_ON=0
LI_PRC_SAVED=0
LI_RC_KIND=""
LI_RC_LINK=""

# li_prc_on / li_prc_off - keep maintainer scripts from starting services in the new system.
li_prc_on() {
    local f="${TGT}/usr/sbin/policy-rc.d"
    [ "${LI_PRC_ON}" = 1 ] && return 0
    if [ -e "${f}" ] || [ -L "${f}" ]; then
        mv -f "${f}" "${f}.lindos-orig" 2>/dev/null && LI_PRC_SAVED=1
    fi
    printf '#!/bin/sh\n# Lindos installer: never start services inside the new system\nexit 101\n' >"${f}" 2>/dev/null
    chmod 0755 "${f}" 2>/dev/null
    LI_PRC_ON=1
    return 0
}

li_prc_off() {
    local f="${TGT}/usr/sbin/policy-rc.d"
    [ "${LI_PRC_ON}" = 1 ] || return 0
    rm -f "${f}" 2>/dev/null
    if [ "${LI_PRC_SAVED}" = 1 ]; then
        mv -f "${f}.lindos-orig" "${f}" 2>/dev/null
    fi
    LI_PRC_ON=0
    LI_PRC_SAVED=0
    return 0
}

# li_dns_prepare - name resolution inside the target.  Its /etc/resolv.conf is a symlink into /run, which
# is a fresh tmpfs there, so the live system's resolver list is copied in for the duration of the hook.
li_dns_prepare() {
    local rc="${TGT}/etc/resolv.conf" src="" f
    if li_run 20 getent hosts archive.ubuntu.com; then
        li_log "name resolution works inside the target"
        return 0
    fi
    for f in ${LINDOS_RESOLV_SOURCES:-/run/systemd/resolve/resolv.conf /etc/resolv.conf}; do
        if [ -s "${f}" ]; then
            src="${f}"
            break
        fi
    done
    [ -n "${src}" ] || return 1
    if [ -L "${rc}" ]; then
        LI_RC_KIND="link"
        LI_RC_LINK="$(readlink "${rc}")"
    elif [ -e "${rc}" ]; then
        LI_RC_KIND="file"
        cp -p "${rc}" "${rc}.lindos-orig" 2>/dev/null
    else
        LI_RC_KIND="none"
    fi
    rm -f "${rc}" 2>/dev/null
    cat "${src}" >"${rc}" 2>/dev/null
    chmod 0644 "${rc}" 2>/dev/null
    li_log "resolv.conf inside the target now copies ${src}"
    li_run 20 getent hosts archive.ubuntu.com
}

li_dns_restore() {
    local rc="${TGT}/etc/resolv.conf"
    [ -n "${LI_RC_KIND}" ] || return 0
    rm -f "${rc}" 2>/dev/null
    case "${LI_RC_KIND}" in
        link) ln -s "${LI_RC_LINK}" "${rc}" 2>/dev/null ;;
        file) mv -f "${rc}.lindos-orig" "${rc}" 2>/dev/null ;;
    esac
    LI_RC_KIND=""
    return 0
}

# li_manifest_remove_file - the packages Ubiquity removes from the new system anyway (one per line, maybe
# with :arch); /dev/null when the medium has no such list.
li_manifest_remove_file() {
    local f="${LINDOS_MANIFEST_REMOVE:-/cdrom/casper/filesystem.manifest-remove}"
    [ -r "${f}" ] || f=/dev/null
    printf '%s\n' "${f}"
}

# li_hold - 'apt-mark hold' the installed packages that must stay exactly as the medium has them until
# Ubiquity has finished: the LI_HOLD_RE families and everything in filesystem.manifest-remove (Ubiquity
# removes those from the new system, so upgrading them would only waste the download).  With these held,
# plain 'apt-get upgrade' is safe AND consistent: apt keeps back whatever cannot be upgraded next to a held
# package, where an explicit package list would fail as a whole on a single 'Breaks' (libplymouth5 against
# a held plymouth).  The names are written to a file BEFORE the hold, so li_unhold (the exit trap) and
# finalize.sh can always undo exactly this.  LI_HOLD_OK=1 only when the holds are in place.
LI_HOLD_OK=0
li_hold() {
    local rows names f="${TGT}${LI_HOLD_FILE_REL}"
    local -a arr
    LI_HOLD_OK=0
    rows="$(li_run_out 60 dpkg-query -W -f='${binary:Package} ${db:Status-Want} ${db:Status-Status}\n')" || return 1
    # (the list is read in BEGIN: an empty list file must not be mistaken for the first file of 'NR == FNR')
    names="$(printf '%s\n' "${rows}" | awk -v re="${LI_HOLD_RE}" -v rmfile="$(li_manifest_remove_file)" '
        BEGIN { while ((getline line < rmfile) > 0) { split(line, a, /[: \t]/); if (a[1] != "") skip[a[1]] = 1 } close(rmfile) }
        $2 == "install" && $3 == "installed" { n = $1; sub(/:.*/, "", n); if (n ~ re || (n in skip)) print $1 }')"
    if [ -z "${names}" ]; then
        return 1
    fi
    mkdir -p "${TGT}${LI_STATE_DIR_REL}" 2>/dev/null
    printf '%s\n' "${names}" >"${f}"
    mapfile -t arr <<<"${names}"
    li_log "holding ${#arr[@]} package(s): the installer, kernel and boot-loader families and what the installer removes anyway"
    li_run 120 apt-mark hold "${arr[@]}" || return 1
    LI_HOLD_OK=1
    return 0
}

# li_unhold - undo li_hold; the list file stays when apt-mark fails, so finalize.sh can try again.
li_unhold() {
    local f="${TGT}${LI_HOLD_FILE_REL}"
    local -a arr
    [ -s "${f}" ] || return 0
    mapfile -t arr <"${f}"
    if li_run 120 apt-mark unhold "${arr[@]}"; then
        rm -f "${f}"
        li_log "released the holds"
        return 0
    fi
    li_log "WARNING: could not release the holds (finalize.sh retries)"
    return 1
}

# li_pin_installer_family - keep apt's candidate for the Ubiquity family at the version that is
# installed.  The hook refreshes the lists from the network; without this pin a newer +mintNN there
# outranks the medium's copy, and Ubiquity's own later install of oem-config-gtk (which needs the exact
# same version of ubiquity-frontend-gtk/ubiquity) would ask apt to upgrade the installer under its own feet.
# finalize.sh removes the pin once Ubiquity is done.
li_pin_installer_family() {
    local ver f="${TGT}${LI_PIN_FILE_REL}"
    ver="$(li_run_out 30 dpkg-query -W -f='${Version}' ubiquity | tr -d '\r\n')"
    if [ -z "${ver}" ]; then
        li_log "ubiquity is not installed in the target - no version pin written"
        return 0
    fi
    mkdir -p "${TGT}/etc/apt/preferences.d" 2>/dev/null
    {
        printf '%s\n' "# Written by the Lindos installer hook; removed by finalize.sh at the end of the installation."
        printf 'Package: ubiquity ubiquity-* oem-config oem-config-*\n'
        printf 'Pin: version %s\n' "${ver}"
        printf 'Pin-Priority: 1001\n'
    } >"${f}" 2>/dev/null
    li_log "pinned the Ubiquity family to ${ver}"
    return 0
}

# li_audit - true when 'dpkg --audit' ran and reported nothing (LI_AUDIT keeps its output).
li_audit() {
    local rc
    LI_AUDIT="$(li_run_out 60 dpkg --audit)"
    rc=$?
    [ "${rc}" -eq 0 ] && [ -z "${LI_AUDIT}" ]
}

# li_rollback_new - last resort when dpkg is still not clean after li_repair: remove the half-installed
# packages the hook itself brought in (never one that was on the image).
li_rollback_new() {
    local name count=0
    [ -n "${LI_TMPD}" ] && [ -s "${LI_TMPD}/pkgs.before" ] || return 0
    while read -r name; do
        [ -n "${name}" ] || continue
        name="${name%%:*}"
        if grep -qxF "${name}" "${LI_TMPD}/pkgs.before" 2>/dev/null; then
            li_log "left alone (it was on the image): ${name}"
            continue
        fi
        count=$(( count + 1 ))
        [ "${count}" -le 8 ] || break
        li_log "removing the broken new package ${name}"
        li_run 180 dpkg --remove --force-remove-reinstreq "${name}" \
            || li_run 180 dpkg --purge --force-remove-reinstreq --force-depends "${name}"
    done < <(printf '%s\n' "${LI_AUDIT}" | awk '/^ [A-Za-z0-9][A-Za-z0-9+.:-]* / {print $1}')
    return 0
}

# li_repair - ALWAYS leave the target dpkg-clean: Ubiquity's later python-apt steps skip everything
# (language packs, codecs, removing the installer) or abort the whole install when dpkg is broken.
li_repair() {
    li_log "repair: dpkg --configure -a, apt-get -f install, dpkg --audit"
    li_inst 900 dpkg --configure -a --force-confold
    li_inst 600 apt-get -y -q -f install --no-download || li_dl 300 apt-get -y -q -f install
    if li_audit; then
        li_log "dpkg is clean"
        return 0
    fi
    li_log "dpkg --audit is not clean: ${LI_AUDIT}"
    li_rollback_new
    if li_audit; then
        li_log "dpkg is clean after removing the broken new packages"
        return 0
    fi
    li_log "WARNING: dpkg is still not clean - Ubiquity's later package steps may skip work"
    return 1
}

# --- the runner that enters the new system (bash lib.sh --enter TARGET CMD...) ----------------
# Runs inside 'unshare --mount --propagation private': what is mounted here is gone when the
# command ends, whatever way it ends.  /run is a fresh tmpfs, NOT the live system's: postinst scripts
# must not reach the live D-Bus or systemd.  The environment is clean (env -i): no debconf variables
# of Ubiquity's pipe, LINDOS_INSTALLER=1 because /proc/cmdline still says boot=casper in there.
li_enter_main() {
    local t="$1"
    local -a envv
    shift
    envv=(PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=dumb
          LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true
          APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1 UCF_FORCE_CONFOLD=1
          LINDOS_INSTALLER=1)
    [ -z "${http_proxy:-}" ] || envv+=("http_proxy=${http_proxy}")
    [ -z "${https_proxy:-}" ] || envv+=("https_proxy=${https_proxy}")
    [ -z "${LINDOS_CHROOT_APT_CONFIG:-}" ] || envv+=("APT_CONFIG=${LINDOS_CHROOT_APT_CONFIG}")
    mount -t proc proc "${t}/proc" || exit 97
    mount -t sysfs sysfs "${t}/sys" || exit 97
    mount --rbind /dev "${t}/dev" || exit 97
    mount -t tmpfs tmpfs "${t}/run" -o mode=0755,nosuid,nodev || exit 97
    mkdir -p "${t}/run/lock" 2>/dev/null
    exec chroot "${t}" /usr/bin/env -i "${envv[@]}" "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        --enter)
            shift
            li_enter_main "$@"
            ;;
        *)
            echo "lib.sh is a library for the Lindos installer scripts (use: lib.sh --enter TARGET CMD...)" >&2
            exit 2
            ;;
    esac
fi
