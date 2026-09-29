#!/bin/bash
# ============================================================================
#  79-installer-flow.sh — wire the Lindos installer flow into Ubiquity (SPEC §2, §8)
#
#  Runs INSIDE the squashfs chroot as root, after 78-installer-brand.sh and before 80-cleanup.sh.
#  The flow: the live session shows nothing but the installer; the installer does everything heavy
#  (updates, drivers, Chrome, apps) while it installs; the first boot of the new system only asks for
#  the account (Ubiquity's oem-config wizard).  The parts, all shipped by the lindos-installer package
#  (packages/lindos-installer, installed by 30-lindos-debs.sh):
#
#    /usr/libexec/lindos/installer/target-config.sh  ->  /usr/lib/ubiquity/target-config/50lindos-install
#        the hook Ubiquity runs after the copy: downloads and installs into /target (time-boxed,
#        always exits 0).  Ubiquity runs only executable files WITHOUT a '.' in their name, in the live
#        system's directory - so this hook COPIES the script there with 'install -m 0755' (git on
#        Windows loses exec bits; mkdeb.sh only marks *.sh) under the dot-less name.
#    /usr/share/lindos/installer/lindos.seed  ->  the image's debconf database (oem-config/enable,
#        ubiquity/success_command = finalize.sh, ...): the same database casper fills from
#        'owner/key=value' kernel words, which the boot entries carry as well (belt and braces).
#
#  What it does, in order: refuse to run outside the build chroot; assert Chrome/Edge are NOT on the
#  image (their licences forbid it, SPEC §0.1); deploy the hook (name rule, mode, syntax); bake the
#  debconf selections and read one back; log an audit of what Ubiquity will run.
#  It DIES (unlike the branding hook) when a required piece is missing: an ISO without the hook would
#  silently install the old way.
#
#  Not done here on purpose: the medium's oem-config packages (build-iso.sh checks the pool with
#  build/lib/verify_oem_pool.py - they are not part of the squashfs), apt lists (80-cleanup.sh still
#  deletes them: stale lists are worse than none, and the hook refreshes them in the target).
#
#  Idempotent.  Test seams (unset in real builds):
#    LINDOS_INSTALLER_ROOT      prefix for every absolute path (fake root in tests)
#    LINDOS_DEBCONF_SET         debconf-set-selections replacement
#    LINDOS_DEBCONF_COMMUNICATE debconf-communicate replacement
#    LINDOS_DPKG_QUERY          dpkg-query replacement
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "installer flow"

ROOT="${LINDOS_INSTALLER_ROOT:-}"
DEBCONF_SET="${LINDOS_DEBCONF_SET:-debconf-set-selections}"
DEBCONF_COMMUNICATE="${LINDOS_DEBCONF_COMMUNICATE:-debconf-communicate}"
DPKG_QUERY="${LINDOS_DPKG_QUERY:-dpkg-query}"
PKG_LIBEXEC="${ROOT}/usr/libexec/lindos/installer"
PKG_SHARE="${ROOT}/usr/share/lindos/installer"
HOOK_DIR="${ROOT}/usr/lib/ubiquity/target-config"
HOOK_NAME="50lindos-install"
HOOK="${HOOK_DIR}/${HOOK_NAME}"
SUCCESS_COMMAND="/usr/libexec/lindos/installer/finalize.sh"

# This hook rewrites system files; never let it run against a build host by accident.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to edit the host's Ubiquity files outside the build chroot (set LINDOS_INSTALLER_ROOT for a dry run)"
fi

rel() { printf '%s' "${1#"${ROOT}"}"; }

# ---------------------------------------------------------------------------
# 1. Chrome and Edge are downloaded at install time, never shipped (SPEC §0.1)
# ---------------------------------------------------------------------------
step_licence() {
    local pkg st
    for pkg in google-chrome-stable microsoft-edge-stable; do
        st="$("${DPKG_QUERY}" --admindir="${ROOT}/var/lib/dpkg" -W -f='${db:Status-Status}' "${pkg}" 2>/dev/null || true)"
        if [ "${st}" = "installed" ]; then
            die "${pkg} is installed in the image: its licence forbids shipping it (SPEC 0.1) - the installer downloads it"
        fi
    done
    log "no browser binary on the image (Chrome/Edge come from their own repositories at install time)"
}

# ---------------------------------------------------------------------------
# 2. The hook
# ---------------------------------------------------------------------------
step_hook() {
    local f
    for f in target-config.sh lib.sh finalize.sh; do
        [ -f "${PKG_LIBEXEC}/${f}" ] || die "$(rel "${PKG_LIBEXEC}")/${f} is missing: is the lindos-installer package installed (LINDOS_DEB_ORDER)?"
    done
    for f in lindos-installer.templates lindos.seed extras.json; do
        [ -f "${PKG_SHARE}/${f}" ] || die "$(rel "${PKG_SHARE}")/${f} is missing: is the lindos-installer package installed?"
    done
    [ -d "${ROOT}/usr/lib/ubiquity" ] || die "no $(rel "${ROOT}/usr/lib/ubiquity"): the image has no Ubiquity installer to hook into"

    case "${HOOK_NAME}" in
        *.*) die "the hook name '${HOOK_NAME}' contains a '.': Ubiquity skips such files" ;;
    esac
    [[ "${HOOK_NAME}" =~ ^[0-9]{2}[A-Za-z0-9_-]+$ ]] || die "the hook name '${HOOK_NAME}' does not look like a target-config entry"

    mkdir -p "${HOOK_DIR}"
    if [ "$(id -u)" = "0" ]; then
        install -m 0755 -o root -g root "${PKG_LIBEXEC}/target-config.sh" "${HOOK}"
    else
        install -m 0755 "${PKG_LIBEXEC}/target-config.sh" "${HOOK}"
    fi
    # a CR before the shebang would make Ubiquity's exec fail silently (its exit status is ignored)
    sed -i 's/\r$//' "${HOOK}"
    chmod 0755 "${PKG_LIBEXEC}/target-config.sh" "${PKG_LIBEXEC}/lib.sh" "${PKG_LIBEXEC}/finalize.sh"
    bash -n "${HOOK}" || die "the deployed hook has a syntax error"
    [ "$(head -n 1 "${HOOK}")" = "#!/bin/bash" ] || die "the deployed hook does not start with #!/bin/bash"
    [ -x "${HOOK}" ] || die "the deployed hook is not executable: Ubiquity would skip it"
    [ ! -L "${HOOK}" ] || die "the deployed hook is a symlink"
    if grep -Eq '^[[:space:]]*set[[:space:]]+-[A-Za-z]*e' "${HOOK}"; then
        die "the deployed hook uses 'set -e': a failing command would end it before it could clean up"
    fi
    log "deployed $(rel "${HOOK}") (mode 0755, from $(rel "${PKG_LIBEXEC}")/target-config.sh)"
}

# ---------------------------------------------------------------------------
# 3. debconf selections
# ---------------------------------------------------------------------------
step_seed() {
    local seed="${PKG_SHARE}/lindos.seed" got
    if ! have "${DEBCONF_SET}"; then
        warn "no ${DEBCONF_SET}: the selections in $(rel "${seed}") are NOT baked (the boot entries still carry the essential ones)"
        return 0
    fi
    if ! "${DEBCONF_SET}" < "${seed}"; then
        warn "${DEBCONF_SET} reported a problem with $(rel "${seed}") (continuing: the boot entries carry the essential answers)"
        return 0
    fi
    log "baked $(rel "${seed}") into the image's debconf database"
    if have "${DEBCONF_COMMUNICATE}"; then
        got="$(printf 'GET ubiquity/success_command\n' | "${DEBCONF_COMMUNICATE}" ubiquity 2>/dev/null || true)"
        case "${got}" in
            *"${SUCCESS_COMMAND}"*) log "read back ubiquity/success_command: ${SUCCESS_COMMAND}" ;;
            *) warn "ubiquity/success_command did not read back as ${SUCCESS_COMMAND} (debconf said: ${got:-nothing})" ;;
        esac
    fi
}

# ---------------------------------------------------------------------------
# 4. Audit: what Ubiquity will find
# ---------------------------------------------------------------------------
step_audit() {
    local ver
    log "Ubiquity's target-config directory:"
    ls -l "${HOOK_DIR}" 2>/dev/null | sed 's/^/    /' >&2 || true
    ver="$("${DPKG_QUERY}" --admindir="${ROOT}/var/lib/dpkg" -W -f='${Version}' ubiquity 2>/dev/null || true)"
    log "ubiquity in the image: ${ver:-unknown} (the medium's pool must carry oem-config of exactly this version: build-iso.sh checks)"
    if [ -e "${ROOT}/usr/lib/oem-config/oem-config.service" ]; then
        warn "oem-config is already installed in the live image - its launcher and diversions are not what this flow expects"
    fi
    [ -f "${ROOT}${SUCCESS_COMMAND}" ] || warn "${SUCCESS_COMMAND} is not in the image: ubiquity/success_command would do nothing"
}

step_licence
step_hook
step_seed
step_audit

log "installer flow wired"
hook_end
