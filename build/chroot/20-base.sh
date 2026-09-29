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
#    best effort: one GUI polkit agent (policykit-1-gnome | mate-polkit | lxpolkit)
#  Everything is installed with --no-install-recommends (RAM/size budget);
#  a second best-effort pass installs "nice to have" packages one by one so a
#  missing package on a mirror can never abort the build.  EXTRA_PACKAGES
#  from build/config.env is appended to the required list.
#  A third best-effort pass installs LAPTOP_ESSENTIALS (build/config.env): firmware, audio
#  UCM configs, Bluetooth, driver metadata, fwupd and a print driver — see the block below and
#  docs/RAM-BUDGET.md for the estimated added size and the printer-driver-all/hplip trade-off.
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
    gufw
    timeshift
    mugshot
    plymouth-themes
    plymouth-label
)
apt_try_install "${NICE[@]}"

# A graphical polkit authentication agent (best effort; the first one that installs is enough).
# Every Lindos password prompt (first-boot setup, Settings, Windows-app installers, updates) goes
# through pkexec, which needs an agent registered for the desktop session to show a real dialog -
# without one it falls back to a text prompt.  /etc/xdg/autostart/lindos-polkit-agent.desktop
# (lindos-desktop) starts whichever of these is installed; no polkit rule is added, the password
# is still asked.  noble universe: policykit-1-gnome (/usr/lib/policykit-1-gnome/...), mate-polkit,
# lxpolkit.
POLKIT_AGENTS=(policykit-1-gnome mate-polkit lxpolkit)
polkit_agent=""
for p in "${POLKIT_AGENTS[@]}"; do
    if pkg_installed "${p}"; then
        polkit_agent="${p}"
        break
    fi
done
if [ -z "${polkit_agent}" ]; then
    for p in "${POLKIT_AGENTS[@]}"; do
        apt_try_install "${p}"
        if pkg_installed "${p}"; then
            polkit_agent="${p}"
            break
        fi
    done
fi
if [ -z "${polkit_agent}" ]; then
    warn "no graphical polkit authentication agent could be installed (${POLKIT_AGENTS[*]}); password prompts will fall back to pkexec's text prompt"
fi

# ---------------------------------------------------------------------------
# Laptop hardware-enablement packages (SPEC §8; build/config.env LAPTOP_ESSENTIALS) — best
# effort, same as NICE above.  Covers firmware (linux-firmware, Intel SOF audio DSP + its
# Secure-Boot-signed variant, Intel/AMD microcode), the audio stack's UCM configs, Bluetooth
# (bluez + the blueman GUI — moved here from NICE, single source of truth), driver metadata
# (ubuntu-drivers-common, also moved here — used by lindos-drivers/lindos-driver-firstboot),
# firmware updates (fwupd) and a moderate-size universal print driver + driverless-USB-printing
# helper.  PipeWire packages are listed only as a safety net: Mint 22 / Ubuntu 24.04 already
# default to PipeWire, and apt_try_install is a no-op for anything already installed, so this can
# never fight Mint's own audio configuration.  See docs/RAM-BUDGET.md for why printer-driver-all
# and hplip are deliberately NOT here (size).
: "${LAPTOP_ESSENTIALS:=linux-firmware firmware-sof-signed alsa-ucm-conf pipewire-audio wireplumber pipewire-pulse bluez blueman intel-microcode amd64-microcode ubuntu-drivers-common fwupd printer-driver-gutenprint ipp-usb}"
LAPTOP_LIST=()
read -r -a LAPTOP_LIST <<< "${LAPTOP_ESSENTIALS}"
apt_try_install "${LAPTOP_LIST[@]}"
# mesa-vulkan-drivers (64-bit) is in NICE above; the i386 half + libgl1-mesa-dri:i386 is already
# installed unconditionally by 70-gaming.sh (SPEC §8) whenever i386 is enabled — not repeated here.

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
for p in xfce4-docklike-plugin xfce4-panel-profiles picom systemd-zram-generator earlyoom gamemode mangohud flatpak \
         bluez blueman fwupd ubuntu-drivers-common linux-firmware; do
    if pkg_installed "${p}"; then
        log "ok: ${p} $(dpkg-query -W -f='${Version}' "${p}" 2>/dev/null)"
    else
        warn "MISSING after install: ${p}"
    fi
done
if [ -n "${polkit_agent}" ]; then
    log "ok: polkit authentication agent ${polkit_agent}"
fi

hook_end
