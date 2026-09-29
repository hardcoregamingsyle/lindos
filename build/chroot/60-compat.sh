#!/bin/bash
# ============================================================================
#  60-compat.sh — Windows-app compatibility layer on the ISO (SPEC §8, §9, §13)
#
#  Runs INSIDE the squashfs chroot as root.
#    INCLUDE_WINE=1 → /usr/libexec/lindos/install-compat.sh --minimal --from-chroot --no-update
#                     i.e. Ubuntu's own 'wine' (+ wine32:i386), winetricks,
#                     cabextract, 32-bit GL/Vulkan libs.  WineHQ *staging* and
#                     umu-launcher are NOT put on the ISO (size); the installer
#                     (or Settings > Apps later) installs them from the WineHQ
#                     repo that 00-repos.sh configured.
#    INCLUDE_WINE=0 → skipped (lindos-compat still installed; installs later).
#    Always          → MIME/desktop database refresh so .exe/.msi open with
#                     lindos-run, and 'lindos-compat doctor' as a report.
#  Honest fallback when the package's installer is missing: apt-get install
#  wine wine32:i386 winetricks cabextract directly.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "compat (Wine)"

: "${INCLUDE_WINE:=1}"

INSTALLER=/usr/libexec/lindos/install-compat.sh

if [ "${INCLUDE_WINE}" = "1" ]; then
    apt_update
    if [ -x "${INSTALLER}" ]; then
        log "${INSTALLER} --minimal --from-chroot --no-update"
        rc=0
        "${INSTALLER}" --minimal --from-chroot --no-update || rc=$?
        case "${rc}" in
            0) log "install-compat.sh finished" ;;
            3) warn "install-compat.sh: offline (exit 3) — the installer installs Wine when online" ;;
            *) warn "install-compat.sh exited ${rc} — some items failed (see /var/log/lindos/install-compat.log)" ;;
        esac
    else
        warn "${INSTALLER} not found (lindos-compat not installed?) — installing Ubuntu wine directly"
        apt_try_install wine wine64 wine32:i386 winetricks cabextract winbind
        apt_try_install libvulkan1:i386 mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 libgl1:i386
    fi
else
    log "INCLUDE_WINE=0 — Wine not pre-installed (lindos-compat installs it on demand)"
fi

# ---------------------------------------------------------------------------
# Vulkan translation layers + compositor (SPEC-KERNEL §17.2).
#   dxvk / vkd3d-proton : Ubuntu noble ships the DXVK/VKD3D runtime as the
#     'dxvk-wine32-development'/'dxvk-wine64-development' Debian split (installed
#     into Wine prefixes with setup_dxvk), or the generic 'vkd3d'/'libvkd3d1'
#     runtime.  lindos-compat's 'install-dxvk'/'install-vkd3d' subcommands drop
#     the pinned Proton tarballs into a prefix at runtime; here we only make the
#     32/64-bit vkd3d loader + gamescope available so games launch cleanly.
#   gamescope : the micro-compositor lindos-gamescope / lindos-run --gamescope
#     wrap; best effort (present in noble universe).
# These are all best effort: a missing package is warned, never fatal.
# ---------------------------------------------------------------------------
: "${INCLUDE_DXVK:=1}"
if [ "${INCLUDE_DXVK}" = "1" ]; then
    apt_try_install gamescope libvkd3d1 vkd3d
    if dpkg --print-foreign-architectures | grep -qx i386; then
        apt_try_install libvkd3d1:i386
    fi
else
    log "INCLUDE_DXVK=0 — gamescope/VKD3D not pre-installed (lindos-compat install-dxvk/install-vkd3d on demand)"
fi

# ---------------------------------------------------------------------------
# MIME defaults: lindos-compat's postinst merges /usr/share/lindos/mimeapps-lindos.list
# into /etc/xdg/mimeapps.list; refresh the databases so the live session sees it.
# ---------------------------------------------------------------------------
if have update-desktop-database; then
    update-desktop-database -q /usr/share/applications || true
fi
if have update-mime-database && [ -d /usr/share/mime ]; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi
if [ -f /usr/share/applications/lindos-run.desktop ]; then
    log "lindos-run.desktop present"
    if [ -f /etc/xdg/mimeapps.list ] && grep -q 'lindos-run.desktop' /etc/xdg/mimeapps.list; then
        log "system mimeapps.list routes Windows executables to lindos-run"
    else
        warn "/etc/xdg/mimeapps.list has no lindos-run default (postinst not run?) — adding minimal defaults"
        mkdir -p /etc/xdg
        touch /etc/xdg/mimeapps.list
        if ! grep -q '^\[Default Applications\]' /etc/xdg/mimeapps.list; then
            printf '[Default Applications]\n' >> /etc/xdg/mimeapps.list
        fi
        for m in application/x-ms-dos-executable application/x-msdownload application/x-msi application/x-ms-shortcut application/x-bat; do
            grep -q "^${m}=" /etc/xdg/mimeapps.list || \
                sed -i "/^\[Default Applications\]/a ${m}=lindos-run.desktop" /etc/xdg/mimeapps.list
        done
    fi
else
    warn "lindos-run.desktop missing (lindos-compat not installed?)"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if have wine; then
    log "wine: $(wine --version 2>/dev/null || echo '?')"
else
    log "wine not present on the ISO (added by the installer, or from Settings > Apps later)"
fi
if have lindos-compat; then
    log "lindos-compat doctor:"
    lindos-compat doctor 2>&1 | head -n 60 | sed 's/^/  /' >&2 || true
fi

hook_end
