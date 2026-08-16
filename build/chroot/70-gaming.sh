#!/bin/bash
# ============================================================================
#  70-gaming.sh — gaming stack on the ISO (SPEC §8, §10, §13)
#
#  Runs INSIDE the squashfs chroot as root.
#    always            → 32-bit Mesa/Vulkan (mesa-vulkan-drivers:i386,
#                        libgl1-mesa-dri:i386, libvulkan1:i386), steam-devices,
#                        vulkan-tools, mesa-utils, mangohud:i386 (best effort)
#    INCLUDE_STEAM=1   → /usr/libexec/lindos/install-gaming.sh --from-chroot ${GAMING_ITEMS}
#                        (default items: steam lutris; Valve's steam-launcher
#                        from the repo added by 00-repos.sh, lutris from noble)
#    INCLUDE_FLATPAK_LAUNCHERS=1 → Prism/Heroic/Sober Flatpaks preinstalled
#                        (big; off by default — OOBE installs them on demand)
#  NVIDIA drivers are NOT preinstalled (mintdrivers / lindos-drivers at first
#  boot) to keep the ISO small and avoid a proprietary blob in the live image.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "gaming"

: "${INCLUDE_STEAM:=1}"
: "${INCLUDE_FLATPAK_LAUNCHERS:=0}"
: "${GAMING_ITEMS:=steam lutris}"
: "${FLATPAK_LAUNCHERS:=org.prismlauncher.PrismLauncher com.heroicgameslauncher.hgl org.vinegarhq.Sober}"

INSTALLER=/usr/libexec/lindos/install-gaming.sh
read -r -a GAMING_ITEM_LIST <<< "${GAMING_ITEMS}"
read -r -a FLATPAK_LIST <<< "${FLATPAK_LAUNCHERS}"

apt_update

# ---------------------------------------------------------------------------
# 1. Drivers / libraries every game needs (64 + 32 bit)
# ---------------------------------------------------------------------------
apt_try_install libvulkan1 mesa-vulkan-drivers vulkan-tools mesa-utils steam-devices
if dpkg --print-foreign-architectures | grep -qx i386; then
    apt_try_install libvulkan1:i386 mesa-vulkan-drivers:i386 libgl1-mesa-dri:i386 libgl1:i386 mangohud:i386
else
    warn "i386 architecture not enabled — 32-bit games (Steam, most Wine titles) will not run until it is"
fi

# ---------------------------------------------------------------------------
# 1b. gamescope + sched_ext user-space schedulers (SPEC-KERNEL §16, §17.2)
#   gamescope : micro-compositor wrapped by lindos-gamescope / lindos-run
#               --gamescope / lindos-game (noble universe).
#   scx       : the sched_ext BPF schedulers (scx_lavd, scx_bpfland, scx_flash,
#               scx_rustland) that `lindos-tune sched set` loads.  They need a
#               CONFIG_SCHED_CLASS_EXT kernel — the Lindos kernel (35-kernel.sh)
#               provides it; the stock Mint/Ubuntu kernel does not, in which
#               case the binaries install but sit idle until the user boots the
#               Lindos kernel.  Package name differs across releases: try both
#               'scx' (current Debian/Ubuntu binary name) and 'scx-scheds'
#               (older name); apt_try_install skips whichever is unavailable, so
#               on noble — where neither is in the default archive — this is a
#               logged skip and the build stays green.
# ---------------------------------------------------------------------------
: "${INCLUDE_GAMESCOPE:=1}"
if [ "${INCLUDE_GAMESCOPE}" = "1" ]; then
    apt_try_install gamescope
else
    log "INCLUDE_GAMESCOPE=0 — gamescope not pre-installed (installed on demand)"
fi
: "${INCLUDE_SCX:=1}"
: "${SCX_PACKAGES:=scx scx-scheds}"
if [ "${INCLUDE_SCX}" = "1" ]; then
    read -r -a SCX_LIST <<< "${SCX_PACKAGES}"
    installed_scx=0
    for scx_pkg in "${SCX_LIST[@]}"; do
        if pkg_available "${scx_pkg}"; then
            apt_try_install "${scx_pkg}"
            if pkg_installed "${scx_pkg}"; then installed_scx=1; break; fi
        fi
    done
    if [ "${installed_scx}" -eq 0 ]; then
        warn "no sched_ext scheduler package (${SCX_PACKAGES}) in the archive — 'lindos-tune sched' will report none until scx is installed (needs the Lindos kernel + an scx backport/PPA)"
    fi
else
    log "INCLUDE_SCX=0 — sched_ext schedulers not pre-installed"
fi

# ---------------------------------------------------------------------------
# 2. Launchers
# ---------------------------------------------------------------------------
if [ "${INCLUDE_STEAM}" = "1" ]; then
    if [ -x "${INSTALLER}" ]; then
        log "${INSTALLER} --from-chroot ${GAMING_ITEM_LIST[*]}"
        rc=0
        "${INSTALLER}" --from-chroot "${GAMING_ITEM_LIST[@]}" || rc=$?
        case "${rc}" in
            0) log "install-gaming.sh finished" ;;
            3) warn "install-gaming.sh: offline (exit 3) — launchers installed at OOBE" ;;
            *) warn "install-gaming.sh exited ${rc} — some items failed (see /var/log/lindos/install-gaming.log)" ;;
        esac
    else
        warn "${INSTALLER} not found (lindos-gaming not installed?) — installing steam-launcher/lutris directly"
        # Valve's repo (00-repos.sh) provides steam-launcher; Ubuntu multiverse has steam-installer.
        if pkg_available steam-launcher; then
            apt_try_install steam-launcher
        else
            apt_try_install steam-installer
        fi
        apt_try_install lutris
    fi
else
    log "INCLUDE_STEAM=0 — no launchers pre-installed"
fi

if [ "${INCLUDE_FLATPAK_LAUNCHERS}" = "1" ]; then
    if have flatpak; then
        for app in "${FLATPAK_LIST[@]}"; do
            log "flatpak install flathub ${app}"
            flatpak install --system -y --noninteractive flathub "${app}" \
                || warn "flatpak install ${app} failed (offline / no flathub remote?)"
        done
    else
        warn "flatpak missing — cannot preinstall Flatpak launchers"
    fi
else
    log "INCLUDE_FLATPAK_LAUNCHERS=0 — Prism/Heroic/Sober are installed on demand (lindos-game install …)"
fi

# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------
for p in steam-launcher steam-installer lutris heroic gamemode mangohud steam-devices gamescope scx scx-scheds; do
    if pkg_installed "${p}"; then log "installed: ${p}"; fi
done
if have lindos-game; then
    log "lindos-game status:"
    lindos-game status 2>&1 | head -n 40 | sed 's/^/  /' >&2 || true
fi

hook_end
