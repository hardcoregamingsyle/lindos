#!/bin/bash
# ============================================================================
#  77-mint-sweep.sh — last pass over what still says "Linux Mint" (SPEC §8)
#
#  Runs INSIDE the squashfs chroot as root, after every hook that installs
#  packages and before 78-installer-brand.sh / 80-cleanup.sh.  Most of the work
#  is done by lindos-desktop (installed by 30-lindos-debs.sh, re-run after every
#  apt run by /etc/apt/apt.conf.d/99lindos-branding):
#
#    /usr/libexec/lindos/apply-branding.sh   os-release, /etc/issue, and the
#        sweep script rebrand-base.py: Mint Welcome + Software Manager hidden from
#        the menu (Lindos Setup / Lindos Store replace them), Mint Welcome hidden
#        from autostart, display fields of /etc/lsb-release, /etc/linuxmint/info
#        and /etc/casper.conf, Firefox's Linux Mint start page
#    /etc/default/grub.d/49-lindos-distributor.cfg       boot menu title on the installed system
#    /etc/skel/.config/autostart/mintwelcome.desktop   per-user off switch
#
#  This hook does what only a build can do:
#    1. run the sweep once more over the final image
#    2. check that Mint Welcome really is out of autostart (any file name)
#    3. purge the Mint wallpaper packs (mint-backgrounds-*) — only when apt
#       says that removes nothing else (a metapackage that depends on them
#       would take the desktop along; then they stay and are listed)
#    4. audit: list, in out/hooks/77-mint-sweep.log, everything that still says
#       Linux Mint, so a real boot can be checked against it
#
#  Deliberately NOT changed (Mint's tools and the installer recognise the system
#  by them): ID/ID_LIKE/codenames in /etc/os-release, DISTRIB_ID and the
#  release number in /etc/lsb-release, RELEASE/CODENAME/EDITION in
#  /etc/linuxmint/info, apt sources, package names.
#
#  Every step is guarded (a missing file is a warning, never a build failure)
#  and idempotent.  Test seams (unset in real builds):
#    LINDOS_SWEEP_ROOT       prefix for every absolute path (fake root in tests)
#    LINDOS_SWEEP_APT_GET    apt-get replacement
#    LINDOS_SWEEP_DPKG_QUERY dpkg-query replacement
#    LINDOS_PYTHON           python3 replacement (also honoured by apply-branding.sh)
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "mint sweep"

ROOT="${LINDOS_SWEEP_ROOT:-}"
APPLY="${ROOT}/usr/libexec/lindos/apply-branding.sh"
REBRAND="${ROOT}/usr/libexec/lindos/rebrand-base.py"
APT_GET="${LINDOS_SWEEP_APT_GET:-apt-get}"
DPKG_QUERY="${LINDOS_SWEEP_DPKG_QUERY:-dpkg-query}"
PYTHON="${LINDOS_PYTHON:-python3}"

# This hook rewrites system files; never let it run against a build host by accident.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to edit the host's files outside the build chroot (set LINDOS_SWEEP_ROOT for a dry run)"
fi

rel() { printf '%s' "${1#"${ROOT}"}"; }

# ---------------------------------------------------------------------------
# 1. Branding + sweep over the final image
# ---------------------------------------------------------------------------
step_sweep() {
    if [ ! -f "${APPLY}" ]; then
        warn "$(rel "${APPLY}") missing (lindos-desktop not installed?) — nothing swept"
        return 0
    fi
    LINDOS_ROOT="${ROOT}" bash "${APPLY}" --files-only || warn "apply-branding.sh reported a problem (continuing)"
    return 0
}

# ---------------------------------------------------------------------------
# 2. Mint Welcome must not autostart, whatever the base calls the file
# ---------------------------------------------------------------------------
step_welcome_check() {
    local f left=0
    for f in "${ROOT}"/etc/xdg/autostart/*.desktop; do
        [ -f "${f}" ] || continue
        if grep -Eiq '^Exec=.*mintwelcome' "${f}" && ! grep -Eiq '^(Hidden|NoDisplay)[[:space:]]*=[[:space:]]*true' "${f}"; then
            warn "$(rel "${f}") still starts Mint Welcome"
            left=$(( left + 1 ))
        fi
    done
    if [ "${left}" -eq 0 ]; then
        log "no visible autostart entry starts Mint Welcome"
    fi
    if [ ! -f "${ROOT}/etc/skel/.config/autostart/mintwelcome.desktop" ]; then
        warn "/etc/skel/.config/autostart/mintwelcome.desktop missing (lindos-desktop too old?) — new users rely on the system-wide hide only"
    fi
    if [ ! -f "${ROOT}/etc/default/grub.d/49-lindos-distributor.cfg" ]; then
        warn "/etc/default/grub.d/49-lindos-distributor.cfg missing — the installed system's boot menu keeps the base's title"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 3. Mint wallpaper packs: purge only when nothing else has to go with them
# ---------------------------------------------------------------------------
step_backgrounds() {
    local pkgs=() sim extra
    # shellcheck disable=SC2016  # dpkg-query's own ${...} format, not shell
    mapfile -t pkgs < <("${DPKG_QUERY}" -W -f='${db:Status-Status} ${Package}\n' 'mint-backgrounds-*' 2>/dev/null | awk '$1=="installed"{print $2}' | sort -u || true)
    if [ "${#pkgs[@]}" -eq 0 ]; then
        log "no mint-backgrounds-* package installed"
        return 0
    fi
    sim="$("${APT_GET}" -s purge "${pkgs[@]}" 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u || true)"
    if [ -z "${sim}" ]; then
        warn "could not simulate purging ${pkgs[*]}; the Mint wallpapers stay (listed in the audit below)"
        return 0
    fi
    extra="$(printf '%s\n' "${sim}" | grep -v '^mint-backgrounds-' | tr '\n' ' ' || true)"
    if [ -n "${extra}" ]; then
        warn "purging ${pkgs[*]} would also remove: ${extra}— keeping the Mint wallpapers (they show in the desktop background picker)"
        return 0
    fi
    log "purging the Mint wallpaper packs: ${pkgs[*]}"
    "${APT_GET}" "${APT_ARGS[@]}" purge "${pkgs[@]}" || warn "purge of ${pkgs[*]} failed (continuing)"
    return 0
}

# ---------------------------------------------------------------------------
# 4. Audit: what still says Linux Mint
# ---------------------------------------------------------------------------
step_audit() {
    local rargs=() out line
    if [ ! -f "${REBRAND}" ] || ! have "${PYTHON}"; then
        warn "cannot audit: rebrand-base.py or python3 missing"
        return 0
    fi
    [ -z "${ROOT}" ] || rargs+=(--root "${ROOT}")
    out="$("${PYTHON}" "${REBRAND}" --audit "${rargs[@]}" 2>/dev/null || true)"
    while IFS= read -r line; do
        [ -n "${line}" ] && log "  ${line}"
    done <<< "${out}"
    return 0
}

step_sweep         || warn "sweep step failed (continuing)"
step_welcome_check || warn "welcome check failed (continuing)"
step_backgrounds   || warn "wallpaper step failed (continuing)"
step_audit         || warn "audit step failed (continuing)"

hook_end
