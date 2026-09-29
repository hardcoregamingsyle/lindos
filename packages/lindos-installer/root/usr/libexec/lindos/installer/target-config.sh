#!/bin/bash
# ============================================================================
#  target-config.sh - the Lindos installer hook.
#
#  Deployed (by build/chroot/79-installer-flow.sh) as
#      /usr/lib/ubiquity/target-config/50lindos-install
#  Ubiquity runs every executable, dot-less file of that directory once per installation, in the
#  live environment as root, after the user account and the locale exist but BEFORE the kernel's
#  initramfs and the boot loader are set up:  log-output -t ubiquity --pass-stdout HOOK
#  This is where the installer does everything heavy: it downloads and installs into /target the
#  browser, the drivers, the system updates, Wine and the game launchers, the extra apps and the
#  Flatpaks - so the first boot of the new system only asks for an account and a few choices.
#
#  THE CONTRACT (a hook that hangs or breaks dpkg breaks EVERY install; safety beats features):
#    * always exit 0 - Ubiquity ignores the status anyway, but a crash must never look like one;
#    * stdout IS Ubiquity's debconf pipe: nothing is ever printed to it (debconf's confmodule
#      moves stdout to stderr; without a frontend it is redirected here); every child runs with
#      stdin from /dev/null and no access to the pipe; all output goes to the log files:
#      /var/log/lindos/installer-hook.log (live) and /target/var/log/lindos/installer.log;
#    * cd /, no 'set -e', every step independently guarded and time-boxed ('timeout -k'), one
#      wall-clock budget for the whole hook (default 45 minutes, lindos.install_budget=SECONDS on
#      the kernel command line; lindos.install=off skips the hook);
#    * downloads first ('apt-get -d', safe to kill), then dpkg runs from the downloaded files (never
#      killed mid-transaction), then ALWAYS a repair pass (dpkg --configure -a, apt-get -f install,
#      dpkg --audit): Ubiquity's later python-apt steps skip everything or abort when dpkg is broken;
#    * the kernel, the boot loader and the Ubiquity/oem-config/casper packages are held (apt-mark) and
#      pinned for the whole hook and released again on EVERY exit path (trap);
#    * no proc/sys/dev/run mounts are left behind: every command enters the target through a private
#      mount namespace, policy-rc.d and resolv.conf are restored by the exit trap;
#    * each step records its result in /var/lib/lindos/install-state.json (python3 -m
#      lindos.installstate --root /target mark ...): 'done' only after a verified success; anything
#      that could not be done (offline, timeout, failure) is 'pending'/'failed' and is retried
#      silently by the first-boot units or from Settings > Apps.
#
#  Progress text: one line per phase in the installer window (db_progress INFO with the template in
#  /usr/share/lindos/installer/lindos-installer.templates); no progress bars of our own.
#
#  Test seams (unset on a real installation): LINDOS_INSTALLER_LIB and the ones in lib.sh,
#  LINDOS_CONFMODULE (a stand-in for debconf's confmodule), LINDOS_DRY_RUN=1 (do nothing but log).
# ============================================================================

umask 022
cd / || true
export PATH="${PATH:+${PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LC_ALL=C.UTF-8 LANG=C.UTF-8

# --- keep the machine awake: re-run ourselves under a logind inhibitor -------------------------
# Sleep, idle and the lid switch are blocked while the hook works.  If the inhibitor cannot be
# started (no logind), the sentinel file tells us the body never ran and we carry on without it.
if [ -z "${LINDOS_INHIBITED:-}" ] && [ "${LINDOS_DRY_RUN:-0}" != 1 ] && command -v systemd-inhibit >/dev/null 2>&1; then
    LINDOS_INHIBIT_SENTINEL="$(mktemp 2>/dev/null)"
    if [ -n "${LINDOS_INHIBIT_SENTINEL}" ]; then
        export LINDOS_INHIBITED=1 LINDOS_INHIBIT_SENTINEL
        systemd-inhibit --what=sleep:idle:handle-lid-switch --mode=block \
            --who="Lindos installer" --why="Installing Lindos" "${BASH}" "$0" "$@"
        if [ -s "${LINDOS_INHIBIT_SENTINEL}" ]; then
            rm -f "${LINDOS_INHIBIT_SENTINEL}"
            exit 0
        fi
        rm -f "${LINDOS_INHIBIT_SENTINEL}"
        unset LINDOS_INHIBIT_SENTINEL
    fi
fi
if [ -n "${LINDOS_INHIBIT_SENTINEL:-}" ]; then
    echo started >"${LINDOS_INHIBIT_SENTINEL}" 2>/dev/null
fi

LI_LIB_PATH="${LINDOS_INSTALLER_LIB:-/usr/libexec/lindos/installer/lib.sh}"
if [ ! -r "${LI_LIB_PATH}" ]; then
    echo "lindos-installer: ${LI_LIB_PATH} is missing - the installer hook does nothing" >&2
    exec 1>&2
    exit 0
fi
# shellcheck source=lib.sh
. "${LI_LIB_PATH}"

# --- stdout: the debconf pipe --------------------------------------------------------------
# With a frontend, debconf's confmodule takes over (fd 3 = protocol, stdout -> stderr, replies on
# stdin).  Without one, stdout goes to stderr and stdin is closed.  Never both, never neither.
li_stdio() {
    local confmodule="${LINDOS_CONFMODULE:-/usr/share/debconf/confmodule}"
    if [ -n "${DEBIAN_HAS_FRONTEND:-}" ] && [ "${LINDOS_DRY_RUN:-0}" != 1 ] && [ -r "${confmodule}" ]; then
        # shellcheck disable=SC1090
        . "${confmodule}"
        if db_x_loadtemplatefile "${LI_TEMPLATES}" lindos-installer >/dev/null 2>&1; then
            # shellcheck disable=SC2034  # read by li_say and li_nonfree_consent in lib.sh
            LI_DB=1
        fi
        if db_get mirror/http/proxy >/dev/null 2>&1 && [ -n "${RET:-}" ]; then
            export http_proxy="${RET}" https_proxy="${RET}"
        fi
    else
        exec 1>&2
        exec </dev/null
    fi
}

# --- exit handling ------------------------------------------------------------------------
LI_CLEANED=0

# li_cleanup - undo everything the hook changed in the target; runs on EVERY exit path.
li_cleanup() {
    [ "${LI_CLEANED}" = 0 ] || return 0
    LI_CLEANED=1
    if [ -n "${LI_CHILD:-}" ]; then
        kill -TERM "${LI_CHILD}" 2>/dev/null
        sleep 1
        kill -KILL "${LI_CHILD}" 2>/dev/null
    fi
    li_log "cleaning up"
    li_unhold
    if [ "${LI_PRC_ON}" = 1 ]; then
        # the downloaded packages are installed (or useless): keep the new system lean
        li_run 120 apt-get clean
    fi
    li_apt_conf_remove
    li_prc_off
    li_dns_restore
    if [ -n "${LI_TMPD:-}" ]; then
        rm -rf "${LI_TMPD}"
    fi
    cp -f "${LI_LOG_LIVE}" "${TGT}/var/log/lindos/installer.log" 2>/dev/null
    return 0
}

li_on_exit() {
    li_cleanup
    exit 0
}

li_on_signal() {
    li_log "signal received - stopping"
    li_cleanup
    exit 0
}

# ======================================================================================
#  the steps
# ======================================================================================

# Set when 'apt-get update' did not complete cleanly (li_apt_update: a non-zero exit, a failed fetch in its
# output that apt only warned about, or no network list on disk): what the steps learn from the lists is then
# incomplete, and an "up to date", "not in the archives" or "no drivers" answer (updates, mode_extras,
# drivers) must not be recorded as a finished job.
LI_LISTS_PARTIAL=0

# li_step NAME MIN_SECONDS_LEFT FUNCTION - run one step if the budget allows, always record a result.
li_step() {
    local name="$1" min left
    min="$(li_scale "$2")"
    left="$(li_left)"
    if [ "${left}" -lt "${min}" ]; then
        li_mark "${name}" pending "time budget used up (${left}s left)"
        return 0
    fi
    "$3"
    if [ -z "$(li_status "${name}")" ]; then
        li_mark "${name}" failed "no result was recorded"
    fi
    if [ "${LI_DIRTY}" = 1 ]; then
        LI_DIRTY=0
        li_repair
    fi
    return 0
}

# li_download_failed STEP RC WHAT - record the result of a download phase that did not work.
li_download_failed() {
    local step="$1" rc="$2" what="$3"
    case "${rc}" in
        3) li_mark "${step}" pending "offline: ${what}" ;;
        124|137|125) li_mark "${step}" pending "${what} did not finish in time" ;;
        *) li_mark "${step}" failed "${what} failed (exit ${rc})" ;;
    esac
}

# --- browser: Google Chrome from Google's own apt repository ---------------------------------
li_step_browser() {
    local ib=/usr/libexec/lindos/install-browser.sh want rc status
    want="$(li_json_get "${TGT}/etc/lindos/system.json" browser chrome)"
    if [ "${want}" != "chrome" ]; then
        li_mark browser skipped "the default browser is ${want}"
        return 0
    fi
    if [ ! -e "${TGT}${ib}" ]; then
        li_mark browser failed "install-browser.sh is missing"
        return 0
    fi
    if ! li_free_ok 1500000; then
        li_mark browser pending "not enough disk space for Google Chrome"
        return 0
    fi
    li_say "Downloading Google Chrome..."
    li_dl 900 "${ib}" chrome --in-installer --download-only
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_download_failed browser "${rc}" "the Chrome download"
        return 0
    fi
    li_say "Installing Google Chrome..."
    li_inst 1800 "${ib}" chrome --in-installer --no-download
    rc=$?
    status="$(li_run_out 30 dpkg-query -W -f='${db:Status-Status}' google-chrome-stable | tr -d '\r\n')"
    if [ "${rc}" -ne 0 ] || [ "${status}" != "installed" ]; then
        LI_DIRTY=1
        li_mark browser failed "google-chrome-stable is not installed (exit ${rc})"
        return 0
    fi
    li_mark browser "done" "google-chrome-stable installed"
    # The system-wide default browser for new users and the run-once marker: browser-firstboot.sh does
    # both when the state says 'done'.  The marker is only ever written after a verified install.
    li_run 60 /usr/libexec/lindos/browser-firstboot.sh
    if [ ! -e "${TGT}/var/lib/lindos/browser-firstboot.done" ]; then
        : >"${TGT}/var/lib/lindos/browser-firstboot.done"
    fi
    return 0
}

# --- drivers: free drivers + firmware always; proprietary ones only with consent -------------
li_step_drivers() {
    local consent=0 sb proprietary=0 note="" rc failed="" detail status detected=1
    if ! li_free_ok 800000; then
        li_mark drivers pending "not enough disk space for drivers"
        return 0
    fi
    li_say "Installing drivers..."
    # firmware: on the image already, this makes sure of it (a no-op when up to date); a package no archive
    # carries is left out instead of failing the whole install
    if [ "${#LI_X_FIRMWARE[@]}" -gt 0 ]; then
        if li_candidates "${LI_X_FIRMWARE[@]}"; then
            if [ "${#LI_CAND[@]}" -gt 0 ]; then
                if li_dl 600 apt-get -y -q -d install --no-install-recommends "${LI_CAND[@]}"; then
                    if ! li_inst 900 apt-get -y -q --no-download install --no-install-recommends "${LI_CAND[@]}"; then
                        failed="the firmware install"
                        LI_DIRTY=1
                    fi
                else
                    failed="the firmware download"
                fi
            fi
        else
            failed="asking apt about the firmware packages"
        fi
    fi
    # free drivers: never anything proprietary here
    if [ -e "${TGT}/usr/bin/ubuntu-drivers" ]; then
        # "nothing to install" is not a failure (whether a given ubuntu-drivers version says it with exit 1 is not verified)
        li_inst 900 sh -c 'out="$(ubuntu-drivers install --free-only 2>&1)"; rc=$?; printf "%s\n" "${out}"; [ "${rc}" -eq 0 ] && exit 0; case "${out}" in *"No drivers found"*|*"already installed"*) exit 0 ;; esac; exit "${rc}"'
        rc=$?
        if [ "${rc}" -ne 0 ]; then
            failed="${failed:+${failed}; }ubuntu-drivers install --free-only (exit ${rc})"
            LI_DIRTY=1
        fi
    else
        detected=0
        li_log "ubuntu-drivers is not installed in the target - no driver detection"
    fi
    # proprietary drivers: only with the user's consent, and never when Secure Boot would need a
    # key enrolment (an NVIDIA DKMS module is unsigned: an interactive blue screen at the next start)
    if li_nonfree_consent; then
        consent=1
        : >"${TGT}/var/lib/lindos/driver-proprietary-consent"
        sb="$(li_secure_boot)"
        if [ "${sb}" != "disabled" ] && li_gpu_may_be_nvidia; then
            note="proprietary NVIDIA driver left for Settings: Secure Boot needs a key enrolment"
            li_log "${note} (Secure Boot: ${sb})"
        elif [ -e "${TGT}/usr/bin/lindos-drivers" ]; then
            proprietary=1
        else
            note="lindos-drivers is not installed: proprietary drivers left for Settings"
        fi
    fi
    if [ "${proprietary}" = 1 ] && [ -z "${failed}" ]; then
        li_say "Installing graphics drivers..."
        li_inst 1500 lindos-drivers install --auto
        rc=$?
        if [ "${rc}" -ne 0 ]; then
            failed="lindos-drivers install --auto (exit ${rc})"
            LI_DIRTY=1
        fi
    fi
    if [ -n "${failed}" ]; then
        li_mark drivers failed "${failed} failed"
        return 0
    fi
    if [ -n "${note}" ]; then
        status=skipped
        detail="${note}"
    else
        status="done"
        if [ "${detected}" = 1 ]; then
            detail="free drivers and firmware installed"
        else
            detail="firmware checked; driver detection is not available (ubuntu-drivers is missing)"
        fi
        if [ "${proprietary}" = 1 ]; then
            detail="${detail}, plus the proprietary driver you agreed to"
        elif [ "${consent}" = 0 ]; then
            detail="${detail} (a proprietary GPU driver needs your consent: Settings)"
        fi
    fi
    if [ "${LI_LISTS_PARTIAL}" = 1 ]; then
        # "no firmware or driver candidates" says nothing about the archives when their lists are incomplete:
        # not final, and no marker, so the silent first-boot retry and Settings still have this to do
        li_mark drivers pending "${detail}, but the package lists were only partly refreshed: more drivers may be available"
        return 0
    fi
    li_mark drivers "${status}" "${detail}"
    # only a terminal outcome silences the first-boot retry unit
    : >"${TGT}/var/lib/lindos/driver-firstboot.done"
    return 0
}

# li_updates_done DETAIL - the updates step ended well; it only counts as done when the lists it looked at were complete.
li_updates_done() {
    if [ "${LI_LISTS_PARTIAL}" = 1 ]; then
        li_mark updates pending "$1, but the package lists were only partly refreshed: more updates may be waiting"
    else
        li_mark updates "done" "$1"
    fi
}

# --- updates: 'apt-get upgrade', never a dist-upgrade; kernel, boot loader and Ubiquity stay --------
# li_hold has held those families (and what the installer removes anyway), so a plain 'upgrade' is safe:
# apt keeps back whatever cannot be upgraded next to a held package.  The simulation is checked first - if a
# held family shows up in it the holds did not take effect and nothing is upgraded at all.
li_step_updates() {
    local sim rc name bad=""
    local -a pkgs
    if [ "${LI_HOLD_OK}" != 1 ]; then
        li_mark updates pending "the installer, kernel and boot-loader packages could not be held, so nothing is upgraded now"
        return 0
    fi
    if ! li_free_ok 3000000; then
        li_mark updates pending "not enough disk space for the system updates"
        return 0
    fi
    li_say "Checking for system updates..."
    sim="$(li_run_out 180 apt-get -q -s upgrade)"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_mark updates failed "could not list the available updates (exit ${rc})"
        return 0
    fi
    mapfile -t pkgs < <(printf '%s\n' "${sim}" | awk '/^Inst / {print $2}')
    if [ "${#pkgs[@]}" -eq 0 ]; then
        li_updates_done "already up to date"
        return 0
    fi
    for name in "${pkgs[@]}"; do
        if [[ "${name}" =~ ${LI_HOLD_RE} ]]; then
            bad="${bad} ${name}"
        fi
    done
    if [ -n "${bad}" ]; then
        li_mark updates failed "the simulated upgrade would touch held packages:${bad}"
        return 0
    fi
    li_say "Downloading ${#pkgs[@]} system updates..."
    li_dl 1500 apt-get -y -q -d upgrade
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_download_failed updates "${rc}" "the update download"
        return 0
    fi
    li_say "Installing system updates..."
    li_inst 2400 apt-get -y -q --no-download upgrade
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        LI_DIRTY=1
        li_mark updates failed "installing the updates failed (exit ${rc})"
        return 0
    fi
    li_updates_done "${#pkgs[@]} packages upgraded"
    return 0
}

# --- compat and gaming: the install scripts' installer mode, download phase then dpkg phase ----
# li_script_step STEP SCRIPT LABEL MINKB ITEM... - install-compat.sh / install-gaming.sh
li_script_step() {
    local step="$1" script="$2" label="$3" minkb="$4" rc
    shift 4
    if [ "$#" -eq 0 ]; then
        li_mark "${step}" "done" "nothing to install"
        return 0
    fi
    if [ ! -e "${TGT}${script}" ]; then
        li_mark "${step}" failed "${script##*/} is missing"
        return 0
    fi
    if ! li_free_ok "${minkb}"; then
        li_mark "${step}" pending "not enough disk space for ${label}"
        return 0
    fi
    li_say "Downloading ${label}..."
    li_dl 1800 "${script}" --in-installer --download-only "$@"
    rc=$?
    if [ "${rc}" -eq 3 ] || [ "${rc}" -eq 124 ] || [ "${rc}" -eq 137 ] || [ "${rc}" -eq 125 ]; then
        li_download_failed "${step}" "${rc}" "the ${label} download"
        return 0
    fi
    # exit 1 = one item could not be downloaded: the rest still installs, the outcome is 'failed'
    li_say "Installing ${label}..."
    li_inst 2400 "${script}" --in-installer --no-download "$@"
    local rc2=$?
    if [ "${rc}" -eq 0 ] && [ "${rc2}" -eq 0 ]; then
        li_mark "${step}" "done" "$*"
    else
        [ "${rc2}" -eq 0 ] || LI_DIRTY=1
        li_mark "${step}" failed "not everything was installed (download exit ${rc}, install exit ${rc2})"
    fi
    return 0
}

li_step_compat() {
    li_script_step compat /usr/libexec/lindos/install-compat.sh "Windows app support (Wine)" 2500000 "${LI_X_COMPAT[@]}"
}

li_step_gaming() {
    li_script_step gaming /usr/libexec/lindos/install-gaming.sh "game launchers" 1500000 "${LI_X_GAMING[@]}"
}

# --- mode_extras: the union of every Mode's apt packages ------------------------------------
li_step_mode_extras() {
    local rc n
    local -a want avail missing done_pkgs bad
    want=("${LI_X_APT[@]}")
    if [ "${#want[@]}" -eq 0 ]; then
        li_mark mode_extras "done" "no extra packages defined"
        return 0
    fi
    if ! li_free_ok 1500000; then
        li_mark mode_extras pending "not enough disk space for the extra apps"
        return 0
    fi
    li_say "Looking for extra apps..."
    li_candidates "${want[@]}"
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_mark mode_extras failed "could not ask apt about the extra apps (exit ${rc})"
        return 0
    fi
    avail=("${LI_CAND[@]}")
    missing=()
    for n in "${want[@]}"; do
        printf '%s\n' "${avail[@]}" | grep -qxF -- "${n}" || missing+=("${n}")
    done
    if [ "${#avail[@]}" -eq 0 ]; then
        if [ "${LI_LISTS_PARTIAL}" = 1 ]; then
            li_mark mode_extras pending "the package lists were only partly refreshed and list none of the extra apps"
        else
            li_mark mode_extras "done" "none of the extra apps is available in the archives"
        fi
        return 0
    fi
    li_say "Downloading extra apps..."
    done_pkgs=()
    bad=()
    li_dl 1500 apt-get -y -q -d install --no-install-recommends "${avail[@]}"
    rc=$?
    case "${rc}" in
        124|137|125)
            li_download_failed mode_extras "${rc}" "the download of the extra apps"
            return 0 ;;
    esac
    if [ "${rc}" -eq 0 ]; then
        li_say "Installing extra apps..."
        if li_inst 2400 apt-get -y -q --no-download install --no-install-recommends "${avail[@]}"; then
            done_pkgs=("${avail[@]}")
        else
            LI_DIRTY=1
        fi
    elif ! li_online; then
        # a lost connection would make the one-by-one fallback below wait for every single package
        li_mark mode_extras pending "the connection was lost during the download"
        return 0
    fi
    if [ "${#done_pkgs[@]}" -eq 0 ]; then
        # the whole group did not work: one package at a time, so one bad package cannot take the rest along
        [ "${LI_DIRTY}" = 0 ] || { LI_DIRTY=0; li_repair; }
        for n in "${avail[@]}"; do
            if li_dl 600 apt-get -y -q -d install --no-install-recommends "${n}" \
                    && li_inst 900 apt-get -y -q --no-download install --no-install-recommends "${n}"; then
                done_pkgs+=("${n}")
            else
                bad+=("${n}")
                LI_DIRTY=1
            fi
        done
    fi
    if [ "${#bad[@]}" -gt 0 ] || { [ "${#done_pkgs[@]}" -eq 0 ] && [ "${#avail[@]}" -gt 0 ]; }; then
        li_mark mode_extras failed "not installed: ${bad[*]:-${avail[*]}}"
    elif [ "${#missing[@]}" -gt 0 ] && [ "${LI_LISTS_PARTIAL}" = 1 ]; then
        li_mark mode_extras pending "${#done_pkgs[@]} packages installed; the package lists were only partly refreshed, so not found: ${missing[*]}"
    elif [ "${#missing[@]}" -gt 0 ]; then
        li_mark mode_extras "done" "${#done_pkgs[@]} packages installed; not in the archives: ${missing[*]}"
    else
        li_mark mode_extras "done" "${#done_pkgs[@]} packages installed"
    fi
    return 0
}

# --- flatpaks: the Flathub remote and the Mode Flatpaks (best effort) ---------------------------
li_step_flatpaks() {
    local id rc
    local -a ids missing
    ids=("${LI_X_FLATPAKS[@]}")
    if [ "${#ids[@]}" -eq 0 ]; then
        li_mark flatpaks "done" "no Flatpak apps defined"
        return 0
    fi
    if [ ! -e "${TGT}/usr/bin/flatpak" ]; then
        li_mark flatpaks pending "flatpak is not installed"
        return 0
    fi
    if ! li_free_ok 4000000; then
        li_mark flatpaks pending "not enough disk space for the Flatpak apps"
        return 0
    fi
    li_say "Adding Flatpak apps..."
    li_dl 300 flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_download_failed flatpaks "${rc}" "adding the Flathub remote"
        return 0
    fi
    missing=()
    for id in "${ids[@]}"; do
        if [ "$(li_left)" -lt "$(li_scale 120)" ]; then
            missing+=("${id}")
            continue
        fi
        li_say "Installing ${id##*.}..."
        li_dl 1500 flatpak install --system -y --noninteractive flathub "${id}"
        if ! li_run 60 flatpak info --system "${id}"; then
            missing+=("${id}")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        li_mark flatpaks "done" "${#ids[@]} Flatpak apps installed"
    else
        # Flatpak inside the installer's chroot is unproven: whatever is missing is retried from Settings > Apps
        li_mark flatpaks pending "not installed: ${missing[*]}"
    fi
    return 0
}

# ======================================================================================
#  main
# ======================================================================================
li_main() {
    local rc step
    li_log "start (version ${LI_VERSION}, target ${TGT}, budget ${LI_BUDGET}s)"
    if [ "${LINDOS_DRY_RUN:-0}" != 1 ] && [ ! -d "${TGT}/etc" ]; then
        # nothing is created where there is no installed system to work on
        li_log "no installed system at ${TGT} - nothing to do"
        return 0
    fi
    mkdir -p "${TGT}/var/lib/lindos" "${TGT}/var/log/lindos" "$(dirname "${LI_LOG_LIVE}")" 2>/dev/null
    LI_TMPD="$(mktemp -d 2>/dev/null)"

    if [ "${LINDOS_DRY_RUN:-0}" = 1 ]; then
        li_log "dry run: nothing is done"
        return 0
    fi
    if [ ! -e "${TGT}/usr/bin/dpkg" ] || [ ! -e "${TGT}/usr/bin/apt-get" ]; then
        li_log "no usable target system at ${TGT} - nothing to do"
        li_mark_missing pending "the installer hook found no target system"
        return 0
    fi
    if li_cmdline_has "lindos.install=off"; then
        li_log "lindos.install=off on the kernel command line - nothing to do"
        li_mark_missing skipped "disabled with lindos.install=off"
        return 0
    fi
    if ! command -v timeout >/dev/null 2>&1; then
        li_log "no 'timeout' command - refusing to run untimed downloads"
        li_mark_missing pending "timeout is not available in the installer"
        return 0
    fi
    if [ -z "${LINDOS_TARGET_RUNNER:-}" ] && ! command -v unshare >/dev/null 2>&1; then
        li_log "no 'unshare' command - refusing to mount into the target without a private namespace"
        li_mark_missing pending "unshare is not available in the installer"
        return 0
    fi

    # the user's consent is a fact about the user, not about the network: record it before any early return so
    # Settings and the silent retry still know it after an offline install
    if li_nonfree_consent; then
        mkdir -p "${TGT}/var/lib/lindos" 2>/dev/null && : >"${TGT}/var/lib/lindos/driver-proprietary-consent" 2>/dev/null
    fi

    li_say "Checking the internet connection..."
    if ! li_online; then
        li_say "No internet connection - downloads are skipped"
        li_set_online false
        li_mark_missing pending "offline while installing"
        return 0
    fi
    li_set_online true

    if ! li_run 60 dpkg --print-architecture; then
        li_log "cannot enter the target system (exit ${LI_RC})"
        li_mark_missing pending "could not enter the new system"
        return 0
    fi
    li_apt_conf_write || li_log "could not write the installer's apt.conf"
    li_prc_on
    li_dns_prepare || li_log "name resolution inside the target does not work"
    li_run_out 60 dpkg-query -W -f='${Package}\n' >"${LI_TMPD}/pkgs.before" 2>/dev/null
    li_hold
    li_pin_installer_family

    # Refresh the target's package lists: the medium carries none (they would be stale) and Ubiquity's
    # own later steps (language packs, codecs) only see the archives after this.  li_apt_update does not
    # trust apt's exit status alone (apt exits 0 after transient fetch failures): a failed fetch in the
    # output, or no network list on disk, is a failed update too, and is retried once like a non-zero exit.
    li_say "Refreshing package lists..."
    li_apt_update 600
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        li_settle
        li_apt_update 300
        rc=$?
    fi
    if [ "${rc}" -ne 0 ]; then
        if ! li_lists_present; then
            li_log "the package lists could not be refreshed and there are none"
            li_mark_missing pending "the package lists could not be refreshed"
            return 0
        fi
        li_log "apt-get update did not complete cleanly, continuing with the lists that arrived: nothing learned from them is final"
        LI_LISTS_PARTIAL=1
    fi

    li_extras_load
    li_log "Secure Boot: $(li_secure_boot)"
    for step in "${LI_STEPS[@]}"; do
        case "${step}" in
            browser)     li_step browser 120 li_step_browser ;;
            drivers)     li_step drivers 300 li_step_drivers ;;
            updates)     li_step updates 300 li_step_updates ;;
            compat)      li_step compat 240 li_step_compat ;;
            gaming)      li_step gaming 180 li_step_gaming ;;
            mode_extras) li_step mode_extras 180 li_step_mode_extras ;;
            flatpaks)    li_step flatpaks 180 li_step_flatpaks ;;
        esac
    done

    li_say "Finishing up..."
    li_repair
    li_mark_missing pending "the installer hook ended before this step"
    li_log "finished in $(( $(date +%s) - LI_T0 ))s"
    return 0
}

li_stdio
LI_BUDGET_WORD="$(li_cmdline_value lindos.install_budget)"
case "${LI_BUDGET_WORD}" in
    ''|*[!0-9]*) ;;
    *) LI_BUDGET="${LI_BUDGET_WORD}" ;;
esac
trap li_on_exit EXIT
trap li_on_signal HUP INT TERM
export LINDOS_INSTALLER_PID=$$
li_main
exit 0
