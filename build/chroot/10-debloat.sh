#!/bin/bash
# ============================================================================
#  10-debloat.sh — remove/disable what Lindos does not need (SPEC §8, §11)
#
#  Runs INSIDE the squashfs chroot as root.
#    * purge the (tiny) DEBLOAT_PURGE list from build/config.env, one package
#      at a time with '|| true' semantics — an absent package is not an error
#    * disable (never purge) background services from DEBLOAT_DISABLE_SERVICES
#      (ModemManager, apport, whoopsie, kerneloops, brltty, speech-dispatcher,
#      NetworkManager-wait-online).  cups stays enabled (socket-activated) and
#      avahi-daemon stays enabled for printer discovery.  bluetooth.service is
#      deliberately NOT disabled here — it is cheap when idle and laptops need
#      it (Bluetooth headphones/mice); only Lite mode's own tune.d/lite.conf
#      turns it off (lindos-tune apply --mode lite), see docs/RAM-BUDGET.md.
#    * mintwelcome is KEPT (its autostart is hidden by lindos-tune's base
#      tune / lindos-desktop, so it can still be opened from the menu).
#    * never touches network/printing basics.
#  The list and the estimated savings live in docs/RAM-BUDGET.md.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "debloat"

: "${DEBLOAT_PURGE:=hexchat rhythmbox hypnotix onboard gnome-calendar}"
: "${DEBLOAT_DISABLE_SERVICES:=ModemManager.service apport.service whoopsie.service kerneloops.service brltty.service speech-dispatcher.service NetworkManager-wait-online.service}"

# Safety net: refuse to purge anything from this list even if config says so.
PROTECTED="network-manager network-manager-gnome cups cups-browsed system-config-printer avahi-daemon \
xfce4-panel xfwm4 xfdesktop4 xfce4-session lightdm slick-greeter thunar firefox casper ubiquity \
ubiquity-frontend-gtk ubiquity-casper linux-image-generic grub-efi-amd64-signed grub-pc shim-signed \
mintinstall mintupdate mintdrivers mintwelcome thunderbird warpinator simple-scan celluloid drawing"

read -r -a PROTECTED_LIST <<< "${PROTECTED}"
read -r -a PURGE_LIST <<< "${DEBLOAT_PURGE}"
read -r -a DISABLE_LIST <<< "${DEBLOAT_DISABLE_SERVICES}"

is_protected() {
    local p
    for p in "${PROTECTED_LIST[@]}"; do
        [ "${p}" = "$1" ] && return 0
    done
    return 1
}

# ---------------------------------------------------------------------------
# 1. Purge list
# ---------------------------------------------------------------------------
purged=0
for pkg in "${PURGE_LIST[@]}"; do
    if is_protected "${pkg}"; then
        warn "refusing to purge protected package: ${pkg}"
        continue
    fi
    if pkg_installed "${pkg}"; then
        log "purging ${pkg}"
        if apt-get "${APT_ARGS[@]}" purge "${pkg}"; then
            purged=$(( purged + 1 ))
        else
            warn "purge of ${pkg} failed (continuing)"
        fi
    else
        log "not installed: ${pkg}"
    fi
done
log "purged ${purged} package(s)"

# Autoremove what became orphaned (only auto-installed leaf packages) — through
# safe_autoremove: purging a seeded app removes Mint's mint-meta-* metapackage,
# and a plain autoremove could then take the whole desktop with it.
safe_autoremove

# ---------------------------------------------------------------------------
# 2. Disable (not purge) services
# ---------------------------------------------------------------------------
for unit in "${DISABLE_LIST[@]}"; do
    svc_disable "${unit}"
done

# NetworkManager-wait-online blocks boot for up to 30 s on machines without a
# link; the desktop does not need it (network-online.target is only for servers).
if unit_exists NetworkManager-wait-online.service; then
    svc_mask NetworkManager-wait-online.service
fi

# Ubuntu's error reporting stack — Mint does not ship apport, but if a package
# pulled it in, keep it quiet.
if [ -f /etc/default/apport ]; then
    sed -i 's/^enabled=1/enabled=0/' /etc/default/apport
    log "apport disabled in /etc/default/apport"
fi

# ---------------------------------------------------------------------------
# 3. Autostart entries that only cost RAM in a live/desktop session
#    (hidden with Hidden=true overrides in /etc/xdg/autostart, never deleted
#    from the packages so the tools stay usable from the menu).
# ---------------------------------------------------------------------------
hide_autostart() {
    local name="$1"
    local f="/etc/xdg/autostart/${name}.desktop"
    if [ -f "${f}" ] && ! grep -q '^Hidden=true' "${f}"; then
        # Put the key inside the [Desktop Entry] group (appending at the end
        # would land in a trailing [Desktop Action …] group if there is one).
        if grep -q '^\[Desktop Entry\]' "${f}"; then
            sed -i '0,/^\[Desktop Entry\]/s//[Desktop Entry]\nHidden=true/' "${f}"
        else
            printf '\nHidden=true\n' >> "${f}"
        fi
        log "hidden autostart: ${name}"
    fi
}
# mintwelcome is handled by lindos-tune / lindos-desktop (first-run gate is
# lindos-setup).  Accessibility (orca) and input-method (im-launch) autostarts
# are deliberately left alone.
hide_autostart "mintreport"
hide_autostart "update-notifier"

hook_end
