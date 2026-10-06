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
#  LINDOS_TIMEOUT_PCT, LINDOS_TIMEOUT_MIN, LINDOS_FREE_KB, LINDOS_TOTAL_KB (both: a fixed answer instead of 'df'),
#  LINDOS_INSTALL_RESERVE_GB (the kernel word lindos.install_reserve=GB wins over it), LINDOS_TEST_CMDLINE,
#  LINDOS_MANIFEST_REMOVE, LINDOS_RESOLV_SOURCES, LINDOS_SYS_EFI.
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
LI_CHILD_OUT=""   # when set, li_child writes the command's output there instead of to the live log (li_apt_update)
LI_RC=0
LI_DIRTY=0
LI_AUDIT=""
LI_MSG_ID="lindos-installer/msg"

# The steps of install-state.json, in the order the installer runs them - which is also the order in which they get
# the disk when it is too small for all of them (every step is run only if it fits above the reserve, li_space_gate):
# the system updates first (security fixes), then what a new user needs at once (the default browser, working
# hardware), the big optional downloads after that, the Flatpaks last (the biggest and the first to be skipped).
LI_STEPS=(updates browser drivers compat gaming mode_extras flatpaks)
# test seam: run only some of the steps (a space separated list of step ids)
[ -z "${LINDOS_INSTALLER_STEPS:-}" ] || read -r -a LI_STEPS <<<"${LINDOS_INSTALLER_STEPS}"

# Package families that stay exactly as the medium has them until Ubiquity has finished: its own
# files, the kernel and the boot loader (Ubiquity's later steps build the initramfs and install GRUB
# for the kernel that is on the image), and the packages an upgrade would pull those along with.
LI_HOLD_RE='^(ubiquity|oem-config|casper|linux-(image|modules|headers|signed|generic|hwe|oem|tools|lowlatency)|lindos-kernel|grub|shim|mokutil|efibootmgr|os-prober|initramfs-tools|plymouth)'

# What an install of extras may REMOVE without being refused (li_guard): Ubuntu's own wine is what winehq-staging
# replaces on purpose.  Everything Ubiquity removes anyway (filesystem.manifest-remove) is allowed as well.
LI_REMOVE_OK_RE='^(wine|wine32|wine64|libwine|fonts-wine)$'

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
declare -A LI_DT=()   # the detail text that goes with each recorded status

# li_mark STEP STATUS [DETAIL] - record an outcome (never fatal).  'done' only ever after a verified success.
li_mark() {
    local step="$1" status="$2"
    shift 2
    li_log "step ${step}: ${status}${1:+ - $*}"
    LI_ST["${step}"]="${status}"
    LI_DT["${step}"]="$*"
    "${LI_PY}" -m lindos.installstate --root "${TGT}" mark "${step}" "${status}" "$*" \
        </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- || li_log "could not record ${step}=${status} in install-state.json"
    return 0
}

# li_status STEP - the recorded status ("" when nothing is recorded).
li_status() {
    printf '%s\n' "${LI_ST[$1]:-}"
}

# li_load_status - fill LI_ST and LI_DT from the state file (one read); finalize.sh needs what the hook recorded.
li_load_status() {
    local step status detail tab=$'\t'
    while IFS="${tab}" read -r step status detail; do
        if [ -n "${step}" ]; then
            LI_ST["${step}"]="${status}"
            LI_DT["${step}"]="${detail}"
        fi
    done < <("${LI_PY}" - "${TGT}" 3>&- <<'PYEOF' 2>/dev/null | tr -d '\r'
import sys

try:
    from lindos import installstate

    for step, entry in installstate.load(sys.argv[1])["steps"].items():
        detail = " ".join(str(entry.get("detail", "")).split())
        sys.stdout.buffer.write(("%s\t%s\t%s\n" % (step, entry["status"], detail)).encode("utf-8"))
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

# The state file is armed (the hook got past its early exits, so there is an installed system whose steps it owns):
# from then on every exit path leaves a truthful status for every step (li_preseed_state, li_mark_unfinished).
LI_STATE_ARMED=0
LI_CUR_STEP=""    # the step that is running right now ('' between steps)

# li_preseed_state - write 'pending: the installer ended before this step' for every step that has no record yet, BEFORE
# the first step runs.  Each write is atomic (lindos.installstate: temp file, fsync, rename).  If the machine hangs hard
# or the power goes, nothing can be written any more - but the file already tells the truth: Settings lists these steps
# as left to finish.  Steps record their real result over it; LI_ST is not touched (li_step still sees "no result yet").
li_preseed_state() {
    "${LI_PY}" -c '
import sys

try:
    from lindos import installstate as st

    root = sys.argv[1]
    known = st.load(root)["steps"]
    for step in sys.argv[2:]:
        if step not in known:
            st.mark(step, "pending", "the installer ended before this step", root)
except Exception as exc:
    sys.stderr.write("could not pre-write the install state: %s\n" % exc)
    sys.exit(1)
' "${TGT}" "${LI_STEPS[@]}" </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- || li_log "could not pre-write the steps in install-state.json"
    return 0
}

# li_mark_unfinished - last word on every exit path (the exit trap, a signal): a step that never started is 'pending: the
# installer ended before this step', the one that was running is 'pending: the installer was stopped during this step'.
# Only once the state file is armed.
li_mark_unfinished() {
    local s
    [ "${LI_STATE_ARMED}" = 1 ] || return 0
    for s in "${LI_STEPS[@]}"; do
        [ -z "${LI_ST[${s}]:-}" ] || continue
        if [ "${s}" = "${LI_CUR_STEP}" ]; then
            li_mark "${s}" pending "the installer was stopped during this step"
        else
            li_mark "${s}" pending "the installer ended before this step"
        fi
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
declare -A LI_X_MODES=()   # apt package -> the Modes that want it (space separated), from extras.json "apt_sources"
declare -A LI_X_EVID=()    # "compat/wine" -> what proves it is installed: "pkg:NAME file:PATH flatpak:ID ..." (extras.json "evidence")

# li_extras_load - read extras.json once into LI_X_APT, LI_X_COMPAT, LI_X_GAMING, LI_X_FLATPAKS, LI_X_FIRMWARE,
# LI_X_MODES and LI_X_EVID.
li_extras_load() {
    local key item rest tab=$'\t'
    LI_X_APT=(); LI_X_COMPAT=(); LI_X_GAMING=(); LI_X_FLATPAKS=(); LI_X_FIRMWARE=()
    LI_X_MODES=(); LI_X_EVID=()
    while IFS="${tab}" read -r key item rest; do
        [ -n "${item}" ] || continue
        case "${key}" in
            apt) LI_X_APT+=("${item}") ;;
            compat) LI_X_COMPAT+=("${item}") ;;
            gaming) LI_X_GAMING+=("${item}") ;;
            flatpaks) LI_X_FLATPAKS+=("${item}") ;;
            firmware) LI_X_FIRMWARE+=("${item}") ;;
            modes) LI_X_MODES["${item}"]="${rest}" ;;
            evidence) LI_X_EVID["${item}"]="${rest}" ;;
        esac
    done < <("${LI_PY}" - "${LI_EXTRAS}" 3>&- <<'PYEOF' 2>/dev/null | tr -d '\r'
import json
import sys


def out(*cols):
    sys.stdout.buffer.write(("\t".join(cols) + "\n").encode("utf-8"))


try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        doc = json.load(fh)
    lists = {"apt": doc.get("apt"), "compat": doc.get("compat"), "gaming": doc.get("gaming"),
             "flatpaks": doc.get("flatpaks"), "firmware": (doc.get("drivers") or {}).get("firmware")}
    for key, items in lists.items():
        for item in items or []:
            if isinstance(item, str) and item.strip():
                out(key, item.strip())
    for pkg, modes in (doc.get("apt_sources") or {}).items():
        words = [m for m in modes if isinstance(m, str) and m.strip()] if isinstance(modes, list) else []
        if isinstance(pkg, str) and pkg.strip() and words:
            out("modes", pkg.strip(), " ".join(words))
    for step, items in (doc.get("evidence") or {}).items():
        for item, ev in (items or {}).items():
            tokens = ["pkg:" + p for p in ev.get("pkgs", [])] + ["file:" + f for f in ev.get("files", [])] \
                + ["flatpak:" + a for a in ev.get("flatpaks", [])]
            if tokens:
                out("evidence", "%s/%s" % (step, item), " ".join(tokens))
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
    timeout -k "${grace}" "${secs}" "${LI_ENTER[@]}" "$@" </dev/null >>"${LI_CHILD_OUT:-${LI_LOG_LIVE}}" 2>&1 3>&- &
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
# budget; 125 = no time left (also in LI_RC, like every other status of li_child: a caller that reads LI_RC
# after a failed download must see "no time left" and not the status of some earlier command).
li_dl() {
    local secs left
    secs="$(li_scale "$1")"
    shift
    left="$(li_left)"
    [ "${secs}" -le "${left}" ] || secs="${left}"
    if [ "${secs}" -lt "$(li_scale 10)" ]; then
        li_log "no time left for: $*"
        LI_RC=125
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

# The apt configuration the hook's own apt calls inside the target use: the 'deb cdrom:' source is never
# consulted, the medium's lists are never cleaned, waiting for a lock beats failing.  It reaches apt as
# '-c FILE' on the command line (LI_APTC) - NEVER as the APT_CONFIG environment variable: that would be
# inherited by dpkg's maintainer scripts, and google-chrome-stable's postinst assigns APT_CONFIG=/usr/bin/apt-config
# (a shell variable it later runs), which turns into "E: Syntax error /usr/bin/apt-config:13" once the variable
# is already exported.  The two helpers that start apt themselves (ubuntu-drivers, lindos-drivers) get it as an
# 'env APT_CONFIG=...' prefix on their own command only (LI_APTENV).
LI_APTC=()
LI_APTENV=()
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
// Stock 'apt-get update' exits 0 when index fetches fail with transient errors (timeouts, DNS, refused
// connections: only a "W: Failed to fetch" line).  With 'any' those failures are an exit status too.
APT::Update::Error-Mode "any";
DPkg::Lock::Timeout "120";
Dpkg::Options { "--force-confdef"; "--force-confold"; };
CONFEOF
    [ -s "${f}" ] || return 1
    LI_APTC=(-c "${LI_APT_CONF_REL}")
    LI_APTENV=(env "APT_CONFIG=${LI_APT_CONF_REL}")
    return 0
}

li_apt_conf_remove() {
    rm -f "${TGT}${LI_APT_CONF_REL}" 2>/dev/null
    LI_APTC=()
    LI_APTENV=()
    return 0
}

# li_lists_present - the target has at least one NON-EMPTY package list of a NETWORK source.  The medium's
# own 'cdrom:' lists (apt-setup adds them) do not count: with only those, "nothing to upgrade" would be a
# statement about the medium, not about the archives.
li_lists_present() {
    local f name
    for f in "${TGT}"/var/lib/apt/lists/*Packages*; do
        [ -s "${f}" ] || continue
        name="${f##*/}"
        case "${name}" in
            cdrom*) continue ;;
        esac
        return 0
    done
    return 1
}

# li_apt_update SECONDS - 'apt-get update' with an honest verdict: 0 only when the lists really are complete.
# Stock apt exits 0 when index fetches fail with transient errors, so the exit status alone would let
# missing or partial lists count as "up to date" (and every step that learns from them record a terminal
# 'done').  Three independent checks, any of which fails the update:
#   * the exit status - APT::Update::Error-Mode "any" (li_apt_conf_write) makes transient failures one;
#   * the output - an 'Err:' or 'E:' line, "Failed to fetch", "index files failed to download" (the
#     installer's apt runs with LC_ALL=C.UTF-8, so the words are English);
#   * the lists themselves - at least one non-empty network Packages list must exist afterwards.
# The output goes to the live log either way.
li_apt_update() {
    local out="" rc bad=0 sample
    if [ -n "${LI_TMPD:-}" ] && : >"${LI_TMPD}/apt-update.out" 2>/dev/null; then
        out="${LI_TMPD}/apt-update.out"
    fi
    LI_CHILD_OUT="${out}"
    li_dl "$1" apt-get "${LI_APTC[@]}" -y -q update
    rc=$?
    LI_CHILD_OUT=""
    if [ -n "${out}" ]; then
        cat "${out}" >>"${LI_LOG_LIVE}" 2>/dev/null
        if grep -Eq '^(Err:|E: )|Failed to fetch|index files failed to download' "${out}" 2>/dev/null; then
            sample="$(grep -E '^(Err:|E: )|Failed to fetch' "${out}" 2>/dev/null | head -n 3 | tr '\r\n' '  ')"
            li_log "apt-get update (exit ${rc}) reported failed fetches: ${sample}"
            bad=1
        fi
        rm -f "${out}" 2>/dev/null
    fi
    if [ "${rc}" -eq 0 ] && [ "${bad}" = 1 ]; then
        rc=1
    fi
    if [ "${rc}" -eq 0 ] && ! li_lists_present; then
        li_log "apt-get update exited 0 but no package list of a network source is there"
        rc=1
    fi
    return "${rc}"
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
    policy="$(li_run_out 120 apt-cache "${LI_APTC[@]}" policy "$@")"
    rc=$?
    [ "${rc}" -eq 0 ] || return "${rc}"
    mapfile -t LI_CAND < <(printf '%s\n' "${policy}" | awk '
        /^[^ ]/ { name = $1; sub(/:$/, "", name) }
        /^ +Candidate:/ { if ($2 != "(none)") print name }')
    return 0
}

# --- disk space --------------------------------------------------------------------------
# The target's root filesystem is what every step spends and nothing gives back: the full install adds roughly 15-25 GB
# (a few hundred upgrades, Chrome, Wine, LibreOffice/GIMP/Krita/Kdenlive, four Flatpaks), and a disk that fills up in the
# middle of a dpkg run leaves a half-unpacked system (the first real laptop install ended in "Unable to load failsafe
# session / xfconfd isn't running" at the first start).  So the hook measures the target at the start, estimates what
# each step needs (ONE table, below), keeps a RESERVE free for the person who will use the PC, and runs a step only when
# it fits above that reserve at the moment its turn comes.  What does not fit is left 'pending' with the reason, never
# started.  Units are KB (1024 bytes), like 'df -Pk'.
LI_GB_KB=1048576
LI_FLOOR_KB=$(( 2 * LI_GB_KB ))             # never leave less than this free, whatever the reserve is set to
LI_RESERVE_MIN_KB=$(( 8 * LI_GB_KB ))       # the reserve is max(8 GB, 12 % of the partition) ...
LI_RESERVE_PCT=12
LI_SMALL_PART_KB=$(( 40 * LI_GB_KB ))       # a partition below this gets no extra apps and no Flatpaks
LI_RESERVE_KB=${LI_RESERVE_MIN_KB}          # ... li_disk_init works it out (lindos.install_reserve=GB overrides it)
LI_TOTAL_KB=""
LI_START_FREE_KB=""
LI_SMALL_PART=0
LI_STEP_FREE0=""                            # the free space when the running step started (li_space_gate)
LI_SPACE_WHY=""                             # why the last li_space_gate / li_sim_fits / li_room_left_ok said no
LI_SPACE_SHORT=""                           # the same without the parenthesis: "not enough disk space: needs ~X GB, Y GB free"

# What each step is expected to need at its peak - downloads plus unpacked files, before 'apt-get clean' - in KB.  This is
# the ONE table of estimates; li_need_kb derives the figure it uses from it and from extras.json (a step with nothing to
# install needs nothing, the Flatpak figure is per app).  They are estimates from the first real install, not promises:
# apt's own simulation is consulted too wherever there is one (li_sim_fits).
declare -A LI_NEED_KB=(
    [updates]=3145728         # ~3 GB    a few hundred upgrades (316 in the first real install)
    [browser]=629146          # ~0.6 GB  Google Chrome
    [drivers]=838861          # ~0.8 GB  firmware and the free drivers
    [compat]=2097152          # ~2 GB    WineHQ staging, winetricks, umu-launcher
    [gaming]=1572864          # ~1.5 GB  Steam and Lutris
    [mode_extras]=6291456     # ~6 GB    LibreOffice, GIMP, Krita, Kdenlive and the rest, of every Mode
    [flatpaks]=5242880        # ~5 GB    LI_FLATPAK_APPS apps and the runtimes they share
)
LI_FLATPAK_APPS=4

# li_gb KB - "12.3": gigabytes with one decimal, for the log and for the reasons the state file keeps.
li_gb() {
    local tenths=$(( ( ${1:-0} * 10 + LI_GB_KB / 2 ) / LI_GB_KB ))
    printf '%s.%s\n' $(( tenths / 10 )) $(( tenths % 10 ))
}

# li_df_field N - column N of 'df -Pk TARGET' (2 = size, 4 = available) in KB; nothing when df cannot say (it is
# time-boxed: a hung disk must not hang the hook here).
li_df_field() {
    local v
    v="$(timeout -k 2 15 df -Pk "${TGT}" 2>/dev/null </dev/null 3>&- | awk -v n="$1" 'NR == 2 { print $n }')"
    case "${v}" in ''|*[!0-9]*) return 0 ;; esac
    printf '%s\n' "${v}"
}

# li_free_kb - the free space of the target (what 'df' calls Available: root's reserved blocks are not counted);
# nothing when it cannot be measured.  LINDOS_FREE_KB is a fixed answer for the tests.
li_free_kb() {
    local v="${LINDOS_FREE_KB:-}"
    if [ -z "${v}" ]; then
        li_df_field 4
        return 0
    fi
    case "${v}" in *[!0-9]*) return 0 ;; esac
    printf '%s\n' "${v}"
}

# li_total_kb - the size of the partition the target is on (nothing when unknown).  LINDOS_TOTAL_KB: the test seam.
li_total_kb() {
    local v="${LINDOS_TOTAL_KB:-}"
    if [ -z "${v}" ]; then
        li_df_field 2
        return 0
    fi
    case "${v}" in *[!0-9]*) return 0 ;; esac
    printf '%s\n' "${v}"
}

# li_disk_init - once, at the start: size and free space of the target, the reserve, whether the partition is too small
# for the optional extras.  The reserve is max(8 GB, 12 % of the partition); 'lindos.install_reserve=GB' on the kernel
# command line (or LINDOS_INSTALL_RESERVE_GB) replaces it, but never with less than the 2 GB floor.
li_disk_init() {
    local gb="${LINDOS_INSTALL_RESERVE_GB:-}" word pct total free
    local src="default: the larger of 8 GB and ${LI_RESERVE_PCT} % of the partition"
    LI_TOTAL_KB="$(li_total_kb)"
    LI_START_FREE_KB="$(li_free_kb)"
    LI_RESERVE_KB="${LI_RESERVE_MIN_KB}"
    if [ -n "${LI_TOTAL_KB}" ]; then
        pct=$(( LI_TOTAL_KB * LI_RESERVE_PCT / 100 ))
        [ "${pct}" -le "${LI_RESERVE_KB}" ] || LI_RESERVE_KB="${pct}"
    fi
    word="$(li_cmdline_value lindos.install_reserve)"
    [ -z "${word}" ] || gb="${word}"
    case "${gb}" in
        '') ;;
        *[!0-9]*) li_log "ignoring lindos.install_reserve=${gb}: a whole number of GB is expected" ;;
        *)
            if [ "${#gb}" -le 6 ]; then
                LI_RESERVE_KB=$(( gb * LI_GB_KB ))
                src="lindos.install_reserve=${gb}"
            fi ;;
    esac
    if [ "${LI_RESERVE_KB}" -lt "${LI_FLOOR_KB}" ]; then
        LI_RESERVE_KB="${LI_FLOOR_KB}"
        src="${src}; raised to the 2 GB floor"
    fi
    LI_SMALL_PART=0
    if [ -n "${LI_TOTAL_KB}" ] && [ "${LI_TOTAL_KB}" -lt "${LI_SMALL_PART_KB}" ]; then
        LI_SMALL_PART=1
    fi
    total="unknown size"
    [ -z "${LI_TOTAL_KB}" ] || total="$(li_gb "${LI_TOTAL_KB}") GB"
    free="an unknown amount"
    [ -z "${LI_START_FREE_KB}" ] || free="$(li_gb "${LI_START_FREE_KB}") GB"
    li_log "disk: the target partition is ${total} with ${free} free; the reserve kept free for the user is $(li_gb "${LI_RESERVE_KB}") GB (${src})"
    if [ "${LI_SMALL_PART}" = 1 ]; then
        li_log "disk: the partition is below $(li_gb "${LI_SMALL_PART_KB}") GB - the extra apps and the Flatpaks are skipped"
    fi
    return 0
}

# li_need_kb STEP - what STEP is expected to need, from LI_NEED_KB and extras.json: 0 when it has nothing to install.
# (li_extras_load has run before the first step.)
li_need_kb() {
    local n="${LI_NEED_KB[$1]:-0}"
    case "$1" in
        browser) [ "$(li_json_get "${TGT}/etc/lindos/system.json" browser chrome)" = chrome ] || n=0 ;;
        compat) [ "${#LI_X_COMPAT[@]}" -gt 0 ] || n=0 ;;
        gaming) [ "${#LI_X_GAMING[@]}" -gt 0 ] || n=0 ;;
        mode_extras) [ "${#LI_X_APT[@]}" -gt 0 ] || n=0 ;;
        flatpaks) n=$(( n * ${#LI_X_FLATPAKS[@]} / LI_FLATPAK_APPS )) ;;
    esac
    printf '%s\n' "${n}"
}

# li_space_fail WHAT NEED_KB FREE_KB - fill LI_SPACE_SHORT and LI_SPACE_WHY: WHAT needs NEED_KB, and the reserve has to
# stay free on top of that, which is what the first number says.
li_space_fail() {
    local total=$(( $2 + LI_RESERVE_KB ))
    LI_SPACE_SHORT="not enough disk space: needs ~$(li_gb "${total}") GB, $(li_gb "$3") GB free"
    if [ "$2" -gt 0 ]; then
        LI_SPACE_WHY="${LI_SPACE_SHORT} (about $(li_gb "$2") GB for $1 plus the $(li_gb "${LI_RESERVE_KB}") GB kept free for you)"
    else
        LI_SPACE_WHY="${LI_SPACE_SHORT} (only the $(li_gb "${LI_RESERVE_KB}") GB kept free for you is left, $1 stop here)"
    fi
}

# li_space_unknown - the free space could not be measured: nothing is judged blind (that is how a disk gets filled).
li_space_unknown() {
    LI_SPACE_SHORT="the free disk space could not be measured"
    LI_SPACE_WHY="${LI_SPACE_SHORT}, so nothing that needs room is started"
}

# li_space_gate STEP - may STEP run now?  Re-measures the free space and wants it to be at least the step's estimate
# plus the reserve; a partition below 40 GB gets no extra apps and no Flatpaks at all.  Fills LI_SPACE_WHY when not.
# A step with nothing to install always passes.  Remembers the free space the step starts with (LI_STEP_FREE0).
li_space_gate() {
    local step="$1" need free
    LI_SPACE_WHY=""
    LI_SPACE_SHORT=""
    need="$(li_need_kb "${step}")"
    LI_STEP_FREE0="$(li_free_kb)"
    [ "${need}" -gt 0 ] || return 0
    case "${step}" in
        mode_extras|flatpaks)
            if [ "${LI_SMALL_PART}" = 1 ]; then
                LI_SPACE_SHORT="partition too small"
                LI_SPACE_WHY="partition too small: $(li_gb "${LI_TOTAL_KB}") GB is below the $(li_gb "${LI_SMALL_PART_KB}") GB that the optional apps need to leave room for you"
                return 1
            fi ;;
    esac
    free="${LI_STEP_FREE0}"
    if [ -z "${free}" ]; then
        li_space_unknown
        return 1
    fi
    if [ "${free}" -lt $(( need + LI_RESERVE_KB )) ]; then
        li_space_fail "this step" "${need}" "${free}"
        return 1
    fi
    return 0
}

# --- apt's own estimate -------------------------------------------------------------------------
# 'apt-get -s' prints "Need to get 456 MB of archives." and "After this operation, 1,234 MB of additional disk space will
# be used." (or "... 12 kB disk space will be freed.").  Sizes are powers of 1000 (B kB MB GB TB), a thousands separator
# may be there.  Downloads and unpacked files are on the disk at the same time (the archives stay until 'apt-get clean'),
# so the peak is the sum of the two.
LI_SIM_KNOWN=0
LI_SIM_GET_KB=0
LI_SIM_ADD_KB=0

# li_sim_parse TEXT - fills LI_SIM_GET_KB / LI_SIM_ADD_KB from a simulation's output; LI_SIM_KNOWN=1 when the
# "After this operation" line was there.
li_sim_parse() {
    local row got added
    LI_SIM_KNOWN=0
    LI_SIM_GET_KB=0
    LI_SIM_ADD_KB=0
    row="$(printf '%s\n' "$1" | awk '
        function kb(s,    n, u, m) {
            gsub(/,/, "", s)
            if (!match(s, /[0-9]+(\.[0-9]+)?/)) return -1
            n = substr(s, RSTART, RLENGTH) + 0
            u = substr(s, RSTART + RLENGTH)
            gsub(/[ \t]/, "", u)
            m = 1
            if (u ~ /^[kK]/) m = 1000
            else if (u ~ /^M/) m = 1000000
            else if (u ~ /^G/) m = 1000000000
            else if (u ~ /^T/) m = 1000000000000
            return int(n * m / 1024 + 0.999)
        }
        /^Need to get / {
            s = $0
            sub(/^Need to get /, "", s)
            sub(/ of archives.*$/, "", s)
            n = split(s, parts, "/")
            get = kb(parts[n])
        }
        /^After this operation, / {
            s = $0
            sub(/^After this operation, /, "", s)
            if (s ~ /will be freed/) { add = 0; seen = 1 }
            else if (s ~ /will be used/) { sub(/ (of )?additional.*$/, "", s); add = kb(s); if (add >= 0) seen = 1 }
        }
        END { if (seen) printf "%d %d\n", (get > 0 ? get : 0), add }')"
    [ -n "${row}" ] || return 0
    read -r got added <<<"${row}"
    case "${got}${added}" in ''|*[!0-9]*) return 0 ;; esac
    LI_SIM_GET_KB="${got}"
    LI_SIM_ADD_KB="${added}"
    LI_SIM_KNOWN=1
    return 0
}

# li_sim_fits WHAT [FALLBACK_KB] - after li_sim_parse / li_guard: do the archives plus the added space of that
# transaction fit above the reserve right now?  Without apt's figures FALLBACK_KB (an estimate; 0 = no opinion) is used.
# Fills LI_SPACE_WHY / LI_SPACE_SHORT when it does not.
li_sim_fits() {
    local what="$1" fallback="${2:-0}" need free
    LI_SPACE_WHY=""
    LI_SPACE_SHORT=""
    if [ "${LI_SIM_KNOWN}" = 1 ]; then
        need=$(( LI_SIM_GET_KB + LI_SIM_ADD_KB ))
    else
        need="${fallback}"
    fi
    [ "${need}" -gt 0 ] || return 0
    free="$(li_free_kb)"
    if [ -z "${free}" ]; then
        li_space_unknown
        return 1
    fi
    [ "${free}" -ge $(( need + LI_RESERVE_KB )) ] && return 0
    li_space_fail "${what}" "${need}" "${free}"
    return 1
}

# li_above_reserve WHAT - between package groups and Flatpaks: true while the free space is still at least the reserve.
li_above_reserve() {
    local free
    LI_SPACE_WHY=""
    LI_SPACE_SHORT=""
    free="$(li_free_kb)"
    if [ -z "${free}" ]; then
        li_space_unknown
        return 1
    fi
    [ "${free}" -ge "${LI_RESERVE_KB}" ] && return 0
    li_space_fail "$1" 0 "${free}"
    return 1
}

# li_remaining_kb STEP - what is left of STEP's estimate after what it has used since it started (downloads are on the
# disk by then), never negative.
li_remaining_kb() {
    local need now used
    need="$(li_need_kb "$1")"
    now="$(li_free_kb)"
    if [ -z "${now}" ] || [ -z "${LI_STEP_FREE0}" ]; then
        printf '%s\n' "${need}"
        return 0
    fi
    used=$(( LI_STEP_FREE0 - now ))
    [ "${used}" -gt 0 ] || used=0
    if [ "${used}" -ge "${need}" ]; then
        printf '0\n'
    else
        printf '%s\n' $(( need - used ))
    fi
}

# li_room_left_ok REMAINING_KB - right before a dpkg run, which is never stopped half way: the free space still has to
# cover what is left of the job plus the 2 GB floor (the reserve was checked before the download started).
li_room_left_ok() {
    local remaining="${1:-0}" free
    LI_SPACE_WHY=""
    LI_SPACE_SHORT=""
    free="$(li_free_kb)"
    if [ -z "${free}" ]; then
        li_space_unknown
        return 1
    fi
    [ "${free}" -ge $(( remaining + LI_FLOOR_KB )) ] && return 0
    LI_SPACE_SHORT="not enough disk space: needs ~$(li_gb $(( remaining + LI_FLOOR_KB ))) GB, $(li_gb "${free}") GB free"
    LI_SPACE_WHY="${LI_SPACE_SHORT} (about $(li_gb "${remaining}") GB still to unpack plus the $(li_gb "${LI_FLOOR_KB}") GB the installer never goes below)"
    return 1
}

# li_cache_clean - the downloaded .deb files are installed by now: give their room back (they would stay until the very
# end otherwise, and a big step leaves gigabytes of them).  Never fatal.
li_cache_clean() {
    li_run 120 apt-get "${LI_APTC[@]}" clean
    return 0
}

# --- crash breadcrumbs ------------------------------------------------------------------------
# One line in /var/lib/lindos/installer-progress, rewritten before and after every step and every package group: the
# step, the phase, the free space and the UTC time.  It is written to a new file and renamed (never half a line), then the
# target's filesystem is flushed ('sync -f', time-boxed, in the background so a stuck disk cannot block the hook), so when
# the machine hangs hard the last thing the installer was doing is on the disk.
LI_PROGRESS_REL="/var/lib/lindos/installer-progress"
LI_SYNC_PID=""
LI_SYNC_STUCK=0

# li_sync_target - start 'sync -f' on the target's filesystem in the background (time-boxed by 'timeout').  At most one at
# a time.  If the previous one is still running it gets a one second grace; if it is STILL running the disk is not
# answering, no second one is piled on top of it and nothing waits for it again: a breadcrumb never blocks the hook.
li_sync_target() {
    local polls=0
    if [ -n "${LI_SYNC_PID}" ]; then
        if kill -0 "${LI_SYNC_PID}" 2>/dev/null; then
            [ "${LI_SYNC_STUCK}" != 1 ] || return 0
            while kill -0 "${LI_SYNC_PID}" 2>/dev/null && [ "${polls}" -lt 5 ]; do
                sleep 0.2
                polls=$(( polls + 1 ))
            done
            if kill -0 "${LI_SYNC_PID}" 2>/dev/null; then
                LI_SYNC_STUCK=1
                return 0
            fi
        fi
        LI_SYNC_PID=""
        LI_SYNC_STUCK=0
    fi
    timeout -k 2 "$(li_scale 20)" sync -f "${TGT}${LI_PROGRESS_REL}" </dev/null >/dev/null 2>&1 3>&- &
    LI_SYNC_PID=$!
    return 0
}

# li_sync_wait - at the end: give the last 'sync -f' a moment to finish (three seconds at most, none when it is stuck), so
# the last breadcrumb is on the disk and nothing is left running behind us.
li_sync_wait() {
    local polls=0
    [ -n "${LI_SYNC_PID}" ] || return 0
    if [ "${LI_SYNC_STUCK}" != 1 ]; then
        while kill -0 "${LI_SYNC_PID}" 2>/dev/null && [ "${polls}" -lt 15 ]; do
            sleep 0.2
            polls=$(( polls + 1 ))
        done
    fi
    LI_SYNC_PID=""
    return 0
}

# li_progress STEP PHASE [NOTE] - the breadcrumb.  STEP is a step id or '-' (the hook itself); NOTE is a few
# 'key=value' words ('group=creator', 'result=done').  Best effort: a full disk can refuse even this one line.
li_progress() {
    local f="${TGT}${LI_PROGRESS_REL}" free line
    free="$(li_free_kb)"
    line="step=$1 phase=$2 free_kb=${free:-unknown} utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ')${3:+ $3}"
    if printf '%s\n' "${line}" >"${f}.new" 2>/dev/null && mv -f "${f}.new" "${f}" 2>/dev/null; then
        li_sync_target
    else
        rm -f "${f}.new" 2>/dev/null
        li_log "could not write ${LI_PROGRESS_REL} (is the disk full?)"
    fi
    return 0
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

# li_hold_names KIND - the installed packages to hold, one per line.  KIND 'families': the LI_HOLD_RE families
# that must stay exactly as the medium has them until Ubiquity has finished (its own files, the kernel, the boot
# loader).  KIND 'removable': everything in filesystem.manifest-remove that is not a family already - Ubiquity
# removes those from the new system, so upgrading them would only waste the download.
li_hold_names() {
    local rows
    rows="$(li_run_out 60 dpkg-query -W -f='${binary:Package} ${db:Status-Want} ${db:Status-Status}\n')" || return 1
    # (the list is read in BEGIN: an empty list file must not be mistaken for the first file of 'NR == FNR')
    printf '%s\n' "${rows}" | awk -v re="${LI_HOLD_RE}" -v kind="$1" -v rmfile="$(li_manifest_remove_file)" '
        BEGIN { while ((getline line < rmfile) > 0) { split(line, a, /[: \t]/); if (a[1] != "") skip[a[1]] = 1 } close(rmfile) }
        $2 == "install" && $3 == "installed" {
            n = $1; sub(/:.*/, "", n)
            if (kind == "families" && n ~ re) print $1
            if (kind == "removable" && n !~ re && (n in skip)) print $1
        }'
}

# li_hold - 'apt-mark hold' the installed packages of the LI_HOLD_RE families for the whole hook.  With these
# held, plain 'apt-get upgrade' is safe AND consistent: apt keeps back whatever cannot be upgraded next to a held
# package, where an explicit package list would fail as a whole on a single 'Breaks' (libplymouth5 against
# a held plymouth).  The names are written to a file BEFORE the hold, so li_unhold (the exit trap) and
# finalize.sh can always undo exactly this.  LI_HOLD_OK=1 only when the holds are in place.
LI_HOLD_OK=0
li_hold() {
    local names f="${TGT}${LI_HOLD_FILE_REL}"
    local -a arr
    LI_HOLD_OK=0
    names="$(li_hold_names families)" || return 1
    if [ -z "${names}" ]; then
        return 1
    fi
    mkdir -p "${TGT}${LI_STATE_DIR_REL}" 2>/dev/null
    printf '%s\n' "${names}" >"${f}"
    mapfile -t arr <<<"${names}"
    li_log "holding ${#arr[@]} package(s): the installer, kernel and boot-loader families"
    li_run 120 apt-mark hold "${arr[@]}" || return 1
    LI_HOLD_OK=1
    return 0
}

# li_hold_removable / li_unhold_removable - the packages Ubiquity removes anyway are held ONLY while the updates
# step upgrades everything else (nothing is downloaded for them).  They must never be held while extras are
# installed: an exact-version dependency of a held libreoffice-l10n-* / -help-* pack on libreoffice-common made
# apt refuse every libreoffice-* extra ("pkgProblemResolver::Resolve generated breaks, this may be caused by held
# packages").  Holding them is best effort - a failure only means some wasted downloads.
LI_REMOVABLE=()
li_hold_removable() {
    local names f="${TGT}${LI_HOLD_FILE_REL}"
    LI_REMOVABLE=()
    names="$(li_hold_names removable)" || return 0
    [ -n "${names}" ] || return 0
    mapfile -t LI_REMOVABLE <<<"${names}"
    mkdir -p "${TGT}${LI_STATE_DIR_REL}" 2>/dev/null
    printf '%s\n' "${names}" >>"${f}"
    li_log "holding ${#LI_REMOVABLE[@]} more package(s) for the updates step: what the installer removes anyway"
    li_run 120 apt-mark hold "${LI_REMOVABLE[@]}" || li_log "could not hold the packages the installer removes anyway (only some downloads are wasted)"
    return 0
}

li_unhold_removable() {
    local f="${TGT}${LI_HOLD_FILE_REL}" tmp
    [ "${#LI_REMOVABLE[@]}" -gt 0 ] || return 0
    if li_run 120 apt-mark unhold "${LI_REMOVABLE[@]}"; then
        tmp="${f}.new"
        if [ -f "${f}" ] && printf '%s\n' "${LI_REMOVABLE[@]}" | grep -vxFf - "${f}" >"${tmp}" 2>/dev/null; then
            mv -f "${tmp}" "${f}"
        elif [ -f "${f}" ]; then
            # nothing else was in the list (grep found no line left): the file is empty, not missing
            : >"${f}"
        fi
        rm -f "${tmp}" 2>/dev/null
        li_log "released the holds on the packages the installer removes anyway"
        LI_REMOVABLE=()
        return 0
    fi
    li_log "WARNING: could not release the holds on the packages the installer removes anyway (they stay listed for the final release)"
    return 1
}

# li_unhold - undo every hold; the list file stays when apt-mark fails, so finalize.sh can try again.
li_unhold() {
    local f="${TGT}${LI_HOLD_FILE_REL}"
    local -a arr
    [ -s "${f}" ] || { rm -f "${f}" 2>/dev/null; return 0; }
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
    li_inst 600 apt-get "${LI_APTC[@]}" -y -q -f install --no-download || li_dl 300 apt-get "${LI_APTC[@]}" -y -q -f install
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

# --- what an install of extras may remove -----------------------------------------------------
# An apt transaction that installs one package can remove another that conflicts with it, silently, because
# 'apt-get -y' agrees to everything: 'apt-get install steam-devices' removed Valve's steam-launcher (the step
# for the Steam launcher had just said "done").  li_guard simulates the install first; a removal is only fine
# when it is on purpose (LI_REMOVE_OK_RE) or Ubiquity removes that package from the new system anyway.
declare -A LI_RM_OK=()
LI_RM_OK_LOADED=0
LI_REMOVES=()
LI_GUARD_WHY=""

li_removal_set_load() {
    local line name
    [ "${LI_RM_OK_LOADED}" = 0 ] || return 0
    LI_RM_OK_LOADED=1
    while read -r line; do
        name="${line%%[:[:space:]]*}"
        [ -z "${name}" ] || LI_RM_OK["${name}"]=1
    done <"$(li_manifest_remove_file)"
    return 0
}

# li_removal_allowed NAME - true when removing NAME (an installed package) as a side effect is acceptable.
li_removal_allowed() {
    local n="${1%%:*}"
    [[ "${n}" =~ ${LI_REMOVE_OK_RE} ]] && return 0
    li_removal_set_load
    [ -n "${LI_RM_OK[${n}]:-}" ]
}

# li_guard PKG... - simulate 'apt-get install --no-install-recommends PKG...'.  0 = safe.  1 = it would remove an
# installed package that has to stay (LI_REMOVES: everything it would remove, LI_GUARD_WHY: "would remove X Y").
# 2 = apt cannot resolve the request at all (LI_GUARD_WHY: apt's first error line; what a held package that breaks
# the request looks like) - the caller must not try to install it.  Also fills LI_SIM_KNOWN / LI_SIM_GET_KB /
# LI_SIM_ADD_KB from the simulation's "Need to get" / "After this operation" lines (li_sim_parse).
li_guard() {
    local out="" rc n
    local -a bad=()
    LI_REMOVES=()
    LI_GUARD_WHY=""
    li_sim_parse ""
    if [ -n "${LI_TMPD:-}" ] && : >"${LI_TMPD}/guard.out" 2>/dev/null; then
        out="${LI_TMPD}/guard.out"
    else
        li_log "WARNING: no scratch space to check what installing $* would remove - not checked"
        return 0
    fi
    LI_CHILD_OUT="${out}"
    li_run 180 apt-get "${LI_APTC[@]}" -q -s install --no-install-recommends "$@"
    rc=$?
    LI_CHILD_OUT=""
    grep -E '^(Remv |E: )' "${out}" >>"${LI_LOG_LIVE}" 2>/dev/null
    mapfile -t LI_REMOVES < <(awk '/^Remv / { n = $2; sub(/:.*/, "", n); print n }' "${out}" 2>/dev/null | sort -u)
    if [ "${rc}" -ne 0 ]; then
        LI_GUARD_WHY="$(grep -m1 '^E: ' "${out}" 2>/dev/null | cut -c1-110 | tr '\r\n' '  ')"
        [ -n "${LI_GUARD_WHY}" ] || LI_GUARD_WHY="apt cannot resolve it (exit ${rc})"
        rm -f "${out}"
        return 2
    fi
    # apt's own disk figures for this transaction (LI_SIM_*): the callers check them against the free space
    li_sim_parse "$(cat "${out}" 2>/dev/null)"
    rm -f "${out}"
    for n in "${LI_REMOVES[@]}"; do
        li_removal_allowed "${n}" || bad+=("${n}")
    done
    if [ "${#bad[@]}" -gt 0 ]; then
        LI_GUARD_WHY="would remove ${bad[*]}"
        return 1
    fi
    return 0
}

# --- did a step's result survive the later steps? ----------------------------------------------
# A step records 'done' when ITS check passes; a later step (or Ubiquity's own package clean-up, before
# finalize.sh) can undo it.  li_verify_steps asks the new system again, once, at the end, and turns a 'done'
# that no longer holds into 'failed' - so the silent retry and Settings > Apps see the truth.
declare -A LI_INSTALLED=()

# li_installed_load - LI_INSTALLED[name]=1 for every installed package of the new system, by its BARE name.  dpkg-query
# prints ${binary:Package} as 'name:arch' not only for foreign architectures but for every Multi-Arch: same package too
# (libvulkan1:amd64, mesa-vulkan-drivers:amd64 on the real system), so the qualified spelling is registered as well and
# the bare name is registered for the native architecture (and 'all'): a package that is only installed for i386 does
# not count as installed for amd64.  Fails when the list cannot be read (then nothing is judged).
li_installed_load() {
    local rows name status bare arch native
    LI_INSTALLED=()
    rows="$(li_run_out 30 dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n')" || return 1
    [ -n "${rows}" ] || return 1
    native="$(li_run_out 30 dpkg --print-architecture | tr -d '\r\n')"
    while read -r name status; do
        [ "${status}" = installed ] || continue
        LI_INSTALLED["${name}"]=1
        bare="${name%%:*}"
        [ "${bare}" != "${name}" ] || continue
        arch="${name#*:}"
        # (the native architecture could not be read: do not fail the step for it, treat every spelling as installed)
        if [ -z "${native}" ] || [ "${arch}" = "${native}" ] || [ "${arch}" = all ]; then
            LI_INSTALLED["${bare}"]=1
        fi
    done <<<"${rows}"
    return 0
}

# li_have_evidence KEY - 0: something that proves the item is installed exists (extras.json "evidence"), 1: nothing
# does, 2: no evidence is known for KEY (not judged).
li_have_evidence() {
    local tok
    [ -n "${LI_X_EVID[$1]+x}" ] || return 2
    for tok in ${LI_X_EVID[$1]}; do
        case "${tok}" in
            pkg:*) [ -z "${LI_INSTALLED[${tok#pkg:}]:-}" ] || return 0 ;;
            file:*) [ ! -e "${TGT}/${tok#file:}" ] || return 0 ;;
            flatpak:*) [ ! -d "${TGT}/var/lib/flatpak/app/${tok#flatpak:}" ] || return 0 ;;
        esac
    done
    return 1
}

li_verify_steps() {
    local step item detail missing excused any=0
    for step in "${LI_STEPS[@]}"; do
        case "${step}:$(li_status "${step}")" in
            browser:done|compat:done|gaming:done|mode_extras:done) any=1 ;;
        esac
    done
    [ "${any}" = 1 ] || return 0            # nothing that could have been undone (an offline install asks no question)
    if ! li_installed_load; then
        li_log "final check: the package list of the new system cannot be read - the recorded results stay as they are"
        return 0
    fi
    for step in "${LI_STEPS[@]}"; do
        [ "$(li_status "${step}")" = "done" ] || continue
        missing=""
        case "${step}" in
            browser)
                [ -n "${LI_INSTALLED[google-chrome-stable]:-}" ] || missing="google-chrome-stable"
                ;;
            compat|gaming)
                if [ "${step}" = compat ]; then
                    for item in "${LI_X_COMPAT[@]}"; do
                        li_have_evidence "compat/${item}"
                        [ $? -ne 1 ] || missing="${missing} ${item}"
                    done
                else
                    for item in "${LI_X_GAMING[@]}"; do
                        li_have_evidence "gaming/${item}"
                        [ $? -ne 1 ] || missing="${missing} ${item}"
                    done
                fi
                ;;
            mode_extras)
                detail="${LI_DT[mode_extras]:-}"
                case "${detail}" in
                    "none of the extra apps"*|"no extra packages defined"*) continue ;;
                esac
                excused=" "
                case "${detail}" in
                    *"not in the archives: "*) excused=" ${detail##*not in the archives: } " ;;
                esac
                for item in "${LI_X_APT[@]}"; do
                    case "${excused}" in *" ${item} "*) continue ;; esac
                    [ -n "${LI_INSTALLED[${item}]:-}" ] || missing="${missing} ${item}"
                done
                ;;
            *) continue ;;
        esac
        missing="${missing# }"
        [ -n "${missing}" ] || continue
        li_mark "${step}" failed "no longer installed at the end of the installation: ${missing:0:140}"
        if [ "${step}" = browser ]; then
            # the marker says "handled": without it the silent first-boot retry installs Chrome again
            rm -f "${TGT}/var/lib/lindos/browser-firstboot.done" 2>/dev/null
        fi
        li_log "final check: step ${step} said done but ${missing} is not installed (something removed it again)"
    done
    return 0
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
    # (no APT_CONFIG here: see li_apt_conf_write - every maintainer script of every package would inherit it)
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
