#!/bin/bash
# ============================================================================
#  finalize.sh - the Lindos installer's ubiquity/success_command.
#
#  Ubiquity runs it once, after the files are copied, the boot loader is installed and the packages
#  of the new system are settled: 'sh -c COMMAND' as root, synchronously inside the installer's GTK
#  main loop - the window cannot repaint while it runs.  So this is a SHORT finalisation (a few
#  seconds, every command time-boxed); all heavy work is in the target-config hook (target-config.sh).
#  Ubiquity ignores the exit status, so everything is logged to /target/var/log/lindos/installer.log
#  and this script always exits 0.
#
#  Ubiquity's OEM mode installs a temporary account ('oem') and leaves the new system booting into it
#  until somebody runs oem-config-prepare - which arms Ubiquity's first-boot wizard (oem-config) for the
#  next start.  Nothing does that for a consumer installer, so this script does the essentials of
#  oem-config-prepare - WITHOUT its deletion of the NetworkManager profiles - and cleans up after the
#  installer:
#    1. verify that oem-config really is in the new system (Ubiquity installs it from the medium's
#       pool and silently skips a package it cannot find); if not, install the bundled copy;
#    2. arm oem-config: its service and target go to /lib/systemd/system, both are enabled, the
#       default target becomes oem-config.target;
#    3. remove the stale 'autologin-user=oem' lines Ubiquity's OEM mode wrote into
#       /etc/lightdm/lightdm.conf (the account is deleted after the wizard);
#    4. lock the temporary account and make sure the first-run wizard never runs for it;
#    5. set user-setup/allow-password-empty back to false in the new system's debconf database (the
#       image bakes it true so that the temporary account needs no password; the first-boot wizard
#       reads the same database and must not accept an empty password for the real account);
#    6. remove what the installer left behind: the hook copy, the version pin, holds that a killed hook
#       could not release, install-state entries the hook never recorded.
#  If oem-config is NOT there, nothing is locked or stripped: the machine then still boots into the
#  temporary account's desktop, which is bad but usable - a machine nobody can log in to is worse.
# ============================================================================

umask 022
cd / || true
export PATH="${PATH:+${PATH}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export LC_ALL=C.UTF-8 LANG=C.UTF-8

LI_LIB_PATH="${LINDOS_INSTALLER_LIB:-$(dirname "$(readlink -f "$0")")/lib.sh}"
if [ ! -r "${LI_LIB_PATH}" ]; then
    echo "lindos-installer: ${LI_LIB_PATH} is missing - finalize does nothing" >&2
    exit 0
fi
# shellcheck source=lib.sh
. "${LI_LIB_PATH}"

# --- 1. is oem-config really in the new system? --------------------------------------------
# fin_oem_ready - the service, the target and the program the service runs are all there.
fin_oem_ready() {
    local svc="${TGT}/usr/lib/oem-config/oem-config.service" unit="${TGT}/usr/lib/oem-config/oem-config.target"
    local start bin
    [ -s "${svc}" ] && [ -s "${unit}" ] || return 1
    start="$(sed -n 's/^ExecStart=//p' "${svc}" | head -n 1 | tr -d '\r')"
    bin="${start%% *}"
    bin="${bin#[-@+!:]}"
    [ -n "${bin}" ] || return 1
    [ -e "${TGT}${bin}" ]
}

# fin_install_bundled - the medium's bundled copy of oem-config (build-iso.sh puts one at
# /lindos/oem-debs when its pool cannot be trusted), installed with dpkg inside the new system.
fin_install_bundled() {
    local dir="${LINDOS_OEM_DEBS_DIR:-/cdrom/lindos/oem-debs}" deb
    local -a debs=()
    for deb in "${dir}"/*.deb; do
        [ -f "${deb}" ] && debs+=("${deb}")
    done
    if [ "${#debs[@]}" -eq 0 ]; then
        li_log "finalize: no bundled oem-config packages in ${dir}"
        return 1
    fi
    li_log "finalize: installing the bundled oem-config packages (${#debs[@]} file(s))"
    mkdir -p "${TGT}/tmp/lindos-oem-debs" || return 1
    cp -f "${debs[@]}" "${TGT}/tmp/lindos-oem-debs/" || return 1
    li_run 90 sh -c 'dpkg -i --force-confold /tmp/lindos-oem-debs/*.deb'
    rm -rf "${TGT}/tmp/lindos-oem-debs"
    fin_oem_ready
}

# --- 2. arm oem-config ---------------------------------------------------------------------
fin_arm() {
    local dst="${TGT}/lib/systemd/system" f link ok=1
    mkdir -p "${dst}" || return 1
    for f in oem-config.service oem-config.target; do
        cp -f "${TGT}/usr/lib/oem-config/${f}" "${dst}/${f}" || { li_log "finalize: could not copy ${f}"; ok=0; }
    done
    if command -v systemctl >/dev/null 2>&1; then
        timeout -k 2 10 systemctl --root="${TGT}" enable oem-config.service oem-config.target </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- \
            || li_log "finalize: systemctl enable reported a problem"
        timeout -k 2 10 systemctl --root="${TGT}" set-default oem-config.target </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&- \
            || li_log "finalize: systemctl set-default reported a problem"
    else
        li_log "finalize: no systemctl in the installer - linking the default target by hand"
    fi
    # whatever systemctl did, the default target must point at oem-config.target
    link="$(readlink "${TGT}/etc/systemd/system/default.target" 2>/dev/null)"
    if [ "${link##*/}" != "oem-config.target" ]; then
        mkdir -p "${TGT}/etc/systemd/system"
        rm -f "${TGT}/etc/systemd/system/default.target"
        ln -s /lib/systemd/system/oem-config.target "${TGT}/etc/systemd/system/default.target" \
            || { li_log "finalize: could not link default.target"; ok=0; }
    fi
    [ "${ok}" = 1 ]
}

# --- 3. the stale autologin of the temporary account -----------------------------------------
fin_strip_autologin() {
    local f="${TGT}/etc/lightdm/lightdm.conf"
    [ -f "${f}" ] || return 0
    if grep -Eq '^[[:space:]]*autologin-user[[:space:]]*=[[:space:]]*oem[[:space:]]*$' "${f}"; then
        sed -i -E '/^[[:space:]]*(autologin-user|autologin-user-timeout|autologin-guest)[[:space:]]*=/d' "${f}"
        li_log "finalize: removed the autologin lines of the temporary account from lightdm.conf"
    fi
    return 0
}

# --- 4. the temporary account ----------------------------------------------------------------
fin_lock_oem() {
    local home="${TGT}/home/oem" owner
    if timeout -k 2 10 chroot "${TGT}" passwd -l oem </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&-; then
        li_log "finalize: the temporary account is locked"
    else
        li_log "finalize: WARNING - could not lock the temporary account"
    fi
    # the first-run wizard belongs to the real user's first login, never to the temporary account
    if [ -d "${home}" ]; then
        owner="$(stat -c '%u:%g' "${home}" 2>/dev/null)"
        mkdir -p "${home}/.config/lindos"
        printf 'done\n' >"${home}/.config/lindos/setup-done"
        if [ -n "${owner}" ]; then
            chown "${owner}" "${home}/.config/lindos" "${home}/.config/lindos/setup-done" 2>/dev/null
        fi
    fi
    return 0
}

# --- 5. answers that were baked for the installer only ------------------------------------------
# 79-installer-flow.sh bakes lindos.seed into the image, so the new system's debconf database carries the
# same answers - and Ubiquity's first-boot wizard reads that database too.  user-setup/allow-password-empty
# lets the TEMPORARY account be created without a password; it must not stay true for the REAL account.
fin_reset_seed() {
    local v
    # (a subshell without Ubiquity's own debconf variables: they may point at the live system's database)
    if (
        for v in $(compgen -v DEBCONF_); do unset "${v}"; done
        unset DEBIAN_HAS_FRONTEND DEBIAN_FRONTEND
        printf 'd-i user-setup/allow-password-empty boolean false\n' \
            | timeout -k 2 10 chroot "${TGT}" debconf-set-selections >>"${LI_LOG_LIVE}" 2>&1 3>&-
    ); then
        li_log "finalize: user-setup/allow-password-empty is false again in the new system"
    else
        li_log "finalize: WARNING - could not reset user-setup/allow-password-empty (the first-boot wizard may accept an empty password)"
    fi
    return 0
}

# --- 6. leftovers ------------------------------------------------------------------------------
fin_cleanup() {
    local f="${TGT}${LI_HOLD_FILE_REL}"
    local -a arr
    rm -f "${TGT}/usr/lib/ubiquity/target-config/50lindos-install"
    # the directories only when nothing else is in them (no file of any package is touched)
    rmdir "${TGT}/usr/lib/ubiquity/target-config" "${TGT}/usr/lib/ubiquity" 2>/dev/null
    rm -f "${TGT}${LI_PIN_FILE_REL}" "${TGT}${LI_APT_CONF_REL}"
    rm -rf "${TGT}/var/cache/lindos-installer"
    # a hook that was killed could not release its holds: do it here (a held Ubiquity could not be removed)
    if [ -s "${f}" ]; then
        mapfile -t arr <"${f}"
        if timeout -k 2 20 chroot "${TGT}" apt-mark unhold "${arr[@]}" </dev/null >>"${LI_LOG_LIVE}" 2>&1 3>&-; then
            rm -f "${f}"
            li_log "finalize: released the package holds the hook left behind"
        else
            li_log "finalize: WARNING - could not release the package holds"
        fi
    fi
    return 0
}

fin_main() {
    local armed=0 reason=""
    if [ ! -d "${TGT}/etc" ]; then
        # Not run by the installer: the debconf answer that names this script is also read in Ubiquity's OEM
        # first-boot pass on the installed system, where there is no /target.  Nothing to do, nothing created.
        li_log "finalize: no installed system at ${TGT} - nothing to do"
        return 0
    fi
    mkdir -p "${TGT}/var/lib/lindos" "${TGT}/var/log/lindos" "$(dirname "${LI_LOG_LIVE}")" 2>/dev/null
    li_log "finalize: start"

    fin_cleanup
    li_load_status
    li_mark_missing pending "the installer hook did not record a result"

    if ! grep -q '^oem:' "${TGT}/etc/passwd" 2>/dev/null; then
        # a normal (non-OEM) installation: the real account exists, there is no wizard to arm
        li_log "finalize: no temporary 'oem' account - not an OEM installation, nothing to arm"
        return 0
    fi
    fin_reset_seed
    if ! fin_oem_ready; then
        li_log "finalize: oem-config is NOT in the new system - trying the bundled copy"
        fin_install_bundled || true
    fi
    if fin_oem_ready; then
        if fin_arm; then
            armed=1
        else
            reason="the systemd units could not be armed"
        fi
    else
        reason="oem-config is not in the new system and no bundled copy could be installed"
    fi
    if [ "${armed}" = 1 ]; then
        fin_strip_autologin
        fin_lock_oem
        rm -f "${TGT}/var/lib/lindos/oem-config-not-armed"
        li_log "finalize: oem-config is armed - the first start asks for the account and the computer name"
    else
        li_log "finalize: CRITICAL - ${reason}. The temporary 'oem' account stays open so that the machine can still be used; run oem-config-prepare as root."
        printf '%s\n' "${reason}" >"${TGT}/var/lib/lindos/oem-config-not-armed"
    fi
    return 0
}

fin_main
li_log "finalize: done"
cp -f "${LI_LOG_LIVE}" "${TGT}/var/log/lindos/installer.log" 2>/dev/null
exit 0
