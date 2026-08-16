#!/bin/bash
# ============================================================================
#  20-base.sh — base packages every Lindos mode relies on (SPEC §8, §5, §11)
#
#  Runs INSIDE the squashfs chroot as root.  Package names are the real
#  Ubuntu 24.04 "noble" / Mint 22 names:
#    desktop : xfce4-docklike-plugin xfce4-panel-profiles xfce4-clipman-plugin
#              xfce4-notifyd xfce4-whiskermenu-plugin picom
#    tuning  : systemd-zram-generator earlyoom power-profiles-daemon lm-sensors
#              fancontrol
#    gaming  : gamemode mangohud
#    python  : python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0
#    system  : polkitd pkexec flatpak xdg-desktop-portal-gtk fonts-noto-color-emoji
#              winbind cabextract icoutils zenity librsvg2-bin curl wget gpg
#  Everything is installed with --no-install-recommends (RAM/size budget);
#  a second best-effort pass installs "nice to have" packages one by one so a
#  missing package on a mirror can never abort the build.  EXTRA_PACKAGES
#  from build/config.env is appended to the required list.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "base packages"

: "${EXTRA_PACKAGES:=}"
: "${ADD_FLATHUB:=1}"

apt_update

# ---------------------------------------------------------------------------
# Required set — one transaction; failure here is a real build error.
# ---------------------------------------------------------------------------
REQUIRED=(
    # XFCE bits Lindos-desktop depends on
    xfce4-docklike-plugin
    xfce4-panel-profiles
    xfce4-clipman-plugin
    xfce4-notifyd
    xfce4-whiskermenu-plugin
    xfce4-pulseaudio-plugin
    xfce4-power-manager
    xfce4-screenshooter
    xfce4-taskmanager
    xfce4-appfinder
    picom
    # RAM / performance tuning
    systemd-zram-generator
    earlyoom
    power-profiles-daemon
    lm-sensors
    fancontrol
    # gaming basics (launchers come in 70-gaming.sh)
    gamemode
    mangohud
    # python GTK stack for lindos-setup / lindos-settings
    python3
    python3-gi
    gir1.2-gtk-3.0
    gir1.2-gdkpixbuf-2.0
    gir1.2-glib-2.0
    # system
    polkitd
    pkexec
    flatpak
    xdg-desktop-portal-gtk
    xdg-utils
    desktop-file-utils
    shared-mime-info
    fonts-noto-color-emoji
    winbind
    cabextract
    icoutils
    zenity
    librsvg2-bin
    librsvg2-common
    curl
    wget
    gpg
    ca-certificates
    zstd
)

EXTRA=()
read -r -a EXTRA <<< "${EXTRA_PACKAGES}"

# Filter out packages that are not available (e.g. a stripped local mirror)
# and say so loudly rather than aborting the whole transaction.
to_install=()
for p in "${REQUIRED[@]}" "${EXTRA[@]}"; do
    [ -n "${p}" ] || continue
    if pkg_installed "${p}"; then
        continue
    fi
    if pkg_available "${p}"; then
        to_install+=("${p}")
    else
        warn "required package not available in apt sources: ${p} (skipped — check 00-repos.sh / mirror)"
    fi
done

if [ "${#to_install[@]}" -gt 0 ]; then
    apt_install "${to_install[@]}"
else
    log "all required packages already installed"
fi

# ---------------------------------------------------------------------------
# Nice-to-have set — best effort, per package.
# ---------------------------------------------------------------------------
NICE=(
    fonts-noto-core
    fonts-inter            # fallback UI font when Selawik is unavailable (noble: fonts-inter)
    fonts-jetbrains-mono
    fonts-liberation
    libnotify-bin
    xdotool
    wmctrl
    x11-xserver-utils
    mesa-utils
    vulkan-tools
    libvulkan1
    mesa-vulkan-drivers
    pciutils
    usbutils
    hdparm
    nvme-cli
    smartmontools
    inxi
    yad
    emote
    baobab
    gnome-disk-utility
    pavucontrol
    blueman
    gufw
    timeshift
    mugshot
    ubuntu-drivers-common
    plymouth-themes
    plymouth-label
)
apt_try_install "${NICE[@]}"

# ---------------------------------------------------------------------------
# Flathub remote (in case flatpak was only installed just now)
# ---------------------------------------------------------------------------
if [ "${ADD_FLATHUB}" = "1" ] && have flatpak; then
    if flatpak remotes --system 2>/dev/null | awk '{print $1}' | grep -qx flathub; then
        log "flathub remote present"
    else
        log "flatpak remote-add flathub"
        flatpak remote-add --system --if-not-exists flathub \
            https://dl.flathub.org/repo/flathub.flatpakrepo \
            || warn "flatpak remote-add flathub failed (offline?) — added later at OOBE"
    fi
fi

# ---------------------------------------------------------------------------
# Small sanity summary
# ---------------------------------------------------------------------------
for p in xfce4-docklike-plugin xfce4-panel-profiles picom systemd-zram-generator earlyoom gamemode mangohud flatpak; do
    if pkg_installed "${p}"; then
        log "ok: ${p} $(dpkg-query -W -f='${Version}' "${p}" 2>/dev/null)"
    else
        warn "MISSING after install: ${p}"
    fi
done

hook_end
