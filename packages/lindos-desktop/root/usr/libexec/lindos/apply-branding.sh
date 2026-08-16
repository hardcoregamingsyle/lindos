#!/bin/bash
# apply-branding.sh — idempotent Lindos branding of a Mint/Ubuntu system (SPEC §2, §5).
#
#   * /etc/os-release: sed the KEY=VALUE pairs of /usr/share/lindos/os-release.d/lindos.conf
#     into the file (replace existing keys, append new ones); ID/ID_LIKE/codenames untouched.
#     The symlink /etc/os-release → ../usr/lib/os-release is preserved (the target is edited);
#     the untouched original is kept once as /etc/os-release.lindos-orig for --revert / purge.
#   * /etc/issue and /etc/issue.net: "Lindos 1.0.0 (Aurora) \n \l" (backup *.lindos-orig).
#   * Plymouth: register + select /usr/share/plymouth/themes/lindos/lindos.plymouth as
#     default.plymouth via update-alternatives (initramfs is NOT rebuilt unless
#     --update-initramfs is given; build/chroot/40-theme.sh does that once).
#   * Default wallpaper alternative "desktop-background" → aurora-dark.svg.
#
# Called by the lindos-desktop postinst and by build/chroot/40-theme.sh; safe to re-run.
# Runs as root (no sudo inside).  Options: --dry-run, --revert, --update-initramfs, --quiet.
# Env: LINDOS_ROOT (prefix for tests), LINDOS_OS_RELEASE_FRAGMENT (alternate fragment).
set -Eeuo pipefail

ROOT="${LINDOS_ROOT:-}"
FRAGMENT="${LINDOS_OS_RELEASE_FRAGMENT:-${ROOT}/usr/share/lindos/os-release.d/lindos.conf}"
OS_RELEASE="${ROOT}/etc/os-release"
OS_RELEASE_BACKUP="${ROOT}/etc/os-release.lindos-orig"
ISSUE="${ROOT}/etc/issue"
ISSUE_NET="${ROOT}/etc/issue.net"
LINDOS_RELEASE="${ROOT}/etc/lindos-release"
PLYMOUTH_THEME="${ROOT}/usr/share/plymouth/themes/lindos/lindos.plymouth"
PLYMOUTH_LINK="/usr/share/plymouth/themes/default.plymouth"
WALLPAPER="/usr/share/backgrounds/lindos/aurora-dark.svg"
WALLPAPER_LINK="/usr/share/images/desktop-base/desktop-background"

DRY_RUN=0
REVERT=0
UPDATE_INITRAMFS=0
QUIET=0

log() { [ "${QUIET}" -eq 1 ] || printf 'apply-branding: %s\n' "$*" >&2; }
warn() { printf 'apply-branding: WARNING: %s\n' "$*" >&2; }
die() { printf 'apply-branding: ERROR: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --revert) REVERT=1; shift ;;
        --update-initramfs) UPDATE_INITRAMFS=1; shift ;;
        --quiet|-q) QUIET=1; shift ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

# resolve the file actually holding os-release (keeps the /etc symlink intact)
os_release_target() {
    if [ -L "${OS_RELEASE}" ]; then
        readlink -f "${OS_RELEASE}"
    else
        printf '%s\n' "${OS_RELEASE}"
    fi
}

# fragment_pairs → prints "KEY<TAB>VALUE" for every KEY=VALUE line of the fragment
fragment_pairs() {
    [ -r "${FRAGMENT}" ] || die "fragment not found: ${FRAGMENT}"
    local line key value
    while IFS= read -r line || [ -n "${line}" ]; do
        case "${line}" in
            ''|'#'*) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        case "${key}" in
            [A-Z_][A-Z0-9_]*) ;;
            *) warn "ignoring odd fragment line: ${line}"; continue ;;
        esac
        printf '%s\t%s\n' "${key}" "${value}"
    done <"${FRAGMENT}"
}

fragment_value() {   # fragment_value KEY → unquoted value
    local want="$1" key value
    while IFS=$'\t' read -r key value; do
        if [ "${key}" = "${want}" ]; then
            value="${value%\"}"
            value="${value#\"}"
            printf '%s\n' "${value}"
            return 0
        fi
    done < <(fragment_pairs)
    return 0
}

apply_os_release() {
    local target tmp key value changed=0
    target="$(os_release_target)"
    if [ ! -f "${target}" ]; then
        warn "${target} not found; skipping os-release branding"
        return 0
    fi
    tmp="$(mktemp)"
    cp -f "${target}" "${tmp}"
    while IFS=$'\t' read -r key value; do
        if grep -q "^${key}=" "${tmp}"; then
            if ! grep -qxF "${key}=${value}" "${tmp}"; then
                # replace the whole line; value may contain / so use | as delimiter
                sed -i "s|^${key}=.*|${key}=$(printf '%s' "${value}" | sed 's/[&|\\]/\\&/g')|" "${tmp}"
                changed=1
            fi
        else
            printf '%s=%s\n' "${key}" "${value}" >>"${tmp}"
            changed=1
        fi
    done < <(fragment_pairs)
    if [ "${changed}" -eq 0 ]; then
        log "os-release already branded (${target})"
        rm -f "${tmp}"
        return 0
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "would update ${target}:"
        diff -u "${target}" "${tmp}" >&2 || true
        rm -f "${tmp}"
        return 0
    fi
    if [ ! -f "${OS_RELEASE_BACKUP}" ]; then
        cp -f "${target}" "${OS_RELEASE_BACKUP}"
        log "saved original os-release → ${OS_RELEASE_BACKUP}"
    fi
    # write through 'cat >' so ownership/permissions of the target survive
    cat "${tmp}" >"${target}"
    rm -f "${tmp}"
    log "branded ${target}"
}

apply_issue() {
    local version codename text
    version="$(fragment_value LINDOS_VERSION)"
    codename="$(fragment_value LINDOS_CODENAME)"
    [ -n "${version}" ] || version="1.0.0"
    [ -n "${codename}" ] || codename="Aurora"
    text="Lindos ${version} (${codename})"
    if [ -f "${ISSUE}" ] && grep -q "^${text} " "${ISSUE}"; then
        log "/etc/issue already branded"
    elif [ "${DRY_RUN}" -eq 1 ]; then
        log "would write ${ISSUE}: '${text} \\n \\l'"
    else
        if [ -f "${ISSUE}" ] && [ ! -f "${ISSUE}.lindos-orig" ]; then
            cp -f "${ISSUE}" "${ISSUE}.lindos-orig"
        fi
        mkdir -p "$(dirname "${ISSUE}")"
        printf '%s \\n \\l\n\n' "${text}" >"${ISSUE}"
        log "wrote ${ISSUE}"
    fi
    if [ -f "${ISSUE_NET}" ] && grep -qx "${text}" "${ISSUE_NET}"; then
        :
    elif [ "${DRY_RUN}" -eq 0 ]; then
        if [ -f "${ISSUE_NET}" ] && [ ! -f "${ISSUE_NET}.lindos-orig" ]; then
            cp -f "${ISSUE_NET}" "${ISSUE_NET}.lindos-orig"
        fi
        printf '%s\n' "${text}" >"${ISSUE_NET}"
        log "wrote ${ISSUE_NET}"
    fi
    if [ ! -f "${LINDOS_RELEASE}" ] && [ "${DRY_RUN}" -eq 0 ]; then
        printf '%s\n' "${text}" >"${LINDOS_RELEASE}"
        log "wrote ${LINDOS_RELEASE}"
    fi
}

apply_plymouth() {
    if [ -n "${ROOT}" ]; then
        log "LINDOS_ROOT set; skipping update-alternatives (plymouth)"
        return 0
    fi
    if ! command -v update-alternatives >/dev/null 2>&1; then
        warn "update-alternatives not found; Plymouth default not changed"
        return 0
    fi
    if [ ! -f "${PLYMOUTH_THEME}" ]; then
        warn "${PLYMOUTH_THEME} missing; Plymouth default not changed"
        return 0
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "would set default.plymouth → ${PLYMOUTH_THEME}"
        return 0
    fi
    update-alternatives --install "${PLYMOUTH_LINK}" default.plymouth "${PLYMOUTH_THEME}" 200 >/dev/null 2>&1 \
        || warn "update-alternatives --install default.plymouth failed"
    update-alternatives --set default.plymouth "${PLYMOUTH_THEME}" >/dev/null 2>&1 \
        || warn "update-alternatives --set default.plymouth failed"
    log "Plymouth default theme → lindos"
    if [ "${UPDATE_INITRAMFS}" -eq 1 ]; then
        if command -v update-initramfs >/dev/null 2>&1; then
            update-initramfs -u >/dev/null 2>&1 || warn "update-initramfs -u failed"
            log "initramfs updated"
        else
            warn "update-initramfs not found"
        fi
    fi
}

apply_wallpaper_alternative() {
    if [ -n "${ROOT}" ] || ! command -v update-alternatives >/dev/null 2>&1; then
        return 0
    fi
    [ -f "${WALLPAPER}" ] || return 0
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "would register desktop-background → ${WALLPAPER}"
        return 0
    fi
    mkdir -p "$(dirname "${WALLPAPER_LINK}")"
    update-alternatives --install "${WALLPAPER_LINK}" desktop-background "${WALLPAPER}" 100 >/dev/null 2>&1 \
        || warn "update-alternatives desktop-background failed"
    log "desktop-background alternative → ${WALLPAPER}"
}

revert_all() {
    local target
    target="$(os_release_target)"
    if [ -f "${OS_RELEASE_BACKUP}" ]; then
        if [ "${DRY_RUN}" -eq 0 ]; then
            cat "${OS_RELEASE_BACKUP}" >"${target}"
            rm -f "${OS_RELEASE_BACKUP}"
        fi
        log "restored ${target}"
    fi
    local f
    for f in "${ISSUE}" "${ISSUE_NET}"; do
        if [ -f "${f}.lindos-orig" ]; then
            if [ "${DRY_RUN}" -eq 0 ]; then
                mv -f "${f}.lindos-orig" "${f}"
            fi
            log "restored ${f}"
        fi
    done
    if [ -z "${ROOT}" ] && command -v update-alternatives >/dev/null 2>&1 && [ "${DRY_RUN}" -eq 0 ]; then
        update-alternatives --remove default.plymouth "${PLYMOUTH_THEME}" >/dev/null 2>&1 || true
        update-alternatives --remove desktop-background "${WALLPAPER}" >/dev/null 2>&1 || true
    fi
    log "branding reverted"
}

main() {
    if [ "${REVERT}" -eq 1 ]; then
        revert_all
        return 0
    fi
    apply_os_release
    apply_issue
    apply_plymouth
    apply_wallpaper_alternative
    log "branding applied"
}

main "$@"
