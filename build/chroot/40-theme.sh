#!/bin/bash
# ============================================================================
#  40-theme.sh — themes, icons, fonts, branding, panel profiles (SPEC §8, §2, §5)
#
#  Runs INSIDE the squashfs chroot as root, after the Lindos debs (30-*).
#    1. /tmp/lindos/assets/install-into-chroot.sh  (written by
#       build/fetch-assets.sh: Fluent GTK/icon/cursor themes renamed to
#       Lindos-* / Lindos-Cursors*, Selawik + Inter fonts; JetBrains Mono comes
#       from apt via 20-base.sh fonts-jetbrains-mono, best effort) — warns if absent
#    2. /usr/libexec/lindos/apply-branding.sh (lindos-desktop): os-release sed,
#       /etc/issue, /etc/lindos-release, plymouth + wallpaper alternatives
#    3. /usr/libexec/lindos/build-panel-profiles.sh (lindos-desktop):
#       panel.tar.bz2 for every mode
#    4. caches: fc-cache, icon caches, glib schemas, desktop/mime databases
#    5. plymouth-set-default-theme lindos && update-initramfs -u (guarded)
#    6. /etc/skel defaults from /usr/share/lindos/skel (if shipped)
#  Idempotent — every step can be re-run.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "theme & branding"

: "${PLYMOUTH_THEME:=lindos}"
: "${LINDOS_VERSION:=1.0.0}"
: "${LINDOS_CODENAME:=Aurora}"

# ---------------------------------------------------------------------------
# 1. Fetched assets (themes/icons/cursors/fonts)
# ---------------------------------------------------------------------------
ASSETS_INSTALLER="${LINDOS_ASSETS_DIR}/install-into-chroot.sh"
if [ -f "${ASSETS_INSTALLER}" ]; then
    log "running $(basename "${ASSETS_INSTALLER}") from ${LINDOS_ASSETS_DIR}"
    if ! (cd "${LINDOS_ASSETS_DIR}" && bash "${ASSETS_INSTALLER}"); then
        warn "install-into-chroot.sh reported errors — theme may be incomplete"
    fi
else
    warn "no ${ASSETS_INSTALLER} (build/fetch-assets.sh not run or --skip-assets): Lindos GTK/icon themes and Selawik font will be missing; XFCE falls back to the plain Adwaita/Default look"
fi

# Sanity: report what we ended up with.
for t in Lindos-Dark Lindos-Light; do
    if [ -d "/usr/share/themes/${t}" ]; then log "GTK theme present: ${t}"; else warn "GTK theme missing: ${t}"; fi
done
for i in Lindos Lindos-dark; do
    if [ -d "/usr/share/icons/${i}" ]; then log "icon theme present: ${i}"; else warn "icon theme missing: ${i}"; fi
done
for c in Lindos-Cursors-Dark Lindos-Cursors; do
    if [ -d "/usr/share/icons/${c}/cursors" ]; then log "cursor theme present: ${c}"; else warn "cursor theme missing: ${c}"; fi
done
if fc-list 2>/dev/null | grep -qi selawik; then log "font present: Selawik"; else warn "font missing: Selawik (UI falls back to Inter/Noto Sans)"; fi

# ---------------------------------------------------------------------------
# 2. Branding (os-release, issue, lindos-release, plymouth + wallpaper alternatives)
# ---------------------------------------------------------------------------
if [ -x /usr/libexec/lindos/apply-branding.sh ]; then
    log "apply-branding.sh"
    /usr/libexec/lindos/apply-branding.sh || warn "apply-branding.sh returned non-zero"
else
    warn "/usr/libexec/lindos/apply-branding.sh missing (lindos-desktop not installed?) — applying minimal branding here"
    # Honest fallback: the same os-release edits SPEC §2 requires.
    OSR="$(readlink -f /etc/os-release 2>/dev/null || printf '/etc/os-release')"
    if [ -f "${OSR}" ]; then
        set_kv() {
            local k="$1" v="$2"
            if grep -q "^${k}=" "${OSR}"; then
                sed -i "s|^${k}=.*|${k}=${v}|" "${OSR}"
            else
                printf '%s=%s\n' "${k}" "${v}" >> "${OSR}"
            fi
        }
        set_kv NAME '"Lindos"'
        set_kv PRETTY_NAME "\"Lindos ${LINDOS_VERSION%.*} (${LINDOS_CODENAME})\""
        set_kv HOME_URL '"https://lindos.dev"'
        set_kv LINDOS_VERSION "${LINDOS_VERSION}"
        set_kv LINDOS_CODENAME "${LINDOS_CODENAME}"
    fi
    printf 'Lindos %s (%s)\n' "${LINDOS_VERSION}" "${LINDOS_CODENAME}" > /etc/lindos-release
fi

# ---------------------------------------------------------------------------
# 3. Panel profiles per mode
# ---------------------------------------------------------------------------
if [ -x /usr/libexec/lindos/build-panel-profiles.sh ]; then
    log "build-panel-profiles.sh"
    /usr/libexec/lindos/build-panel-profiles.sh || warn "build-panel-profiles.sh returned non-zero (panel/ dirs remain the fallback)"
else
    warn "/usr/libexec/lindos/build-panel-profiles.sh missing — modes keep their panel/ directories as fallback"
fi

# ---------------------------------------------------------------------------
# 4. Caches
# ---------------------------------------------------------------------------
if have fc-cache; then
    log "fc-cache -f"
    fc-cache -f >/dev/null 2>&1 || warn "fc-cache failed"
fi

if have update-icon-caches; then
    log "update-icon-caches /usr/share/icons/*"
    update-icon-caches /usr/share/icons/* >/dev/null 2>&1 || warn "update-icon-caches reported errors"
elif have gtk-update-icon-cache; then
    for d in /usr/share/icons/*/; do
        [ -f "${d}index.theme" ] || continue
        gtk-update-icon-cache -f -t -q "${d}" >/dev/null 2>&1 || true
    done
    log "gtk-update-icon-cache done"
fi

if have glib-compile-schemas && [ -d /usr/share/glib-2.0/schemas ]; then
    log "glib-compile-schemas"
    glib-compile-schemas /usr/share/glib-2.0/schemas || warn "glib-compile-schemas failed"
fi

if have update-desktop-database; then
    update-desktop-database -q /usr/share/applications || true
fi
if have update-mime-database && [ -d /usr/share/mime ]; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi
if have dconf && [ -d /etc/dconf/db ]; then
    dconf update || true
fi

# ---------------------------------------------------------------------------
# 5. Plymouth theme + initramfs (guarded: theme dir + tools must exist)
# ---------------------------------------------------------------------------
if [ -d "/usr/share/plymouth/themes/${PLYMOUTH_THEME}" ]; then
    if have plymouth-set-default-theme; then
        log "plymouth-set-default-theme ${PLYMOUTH_THEME}"
        if plymouth-set-default-theme "${PLYMOUTH_THEME}"; then
            log "plymouth default theme is now: $(plymouth-set-default-theme 2>/dev/null || echo '?')"
        else
            warn "plymouth-set-default-theme failed"
        fi
    else
        warn "plymouth-set-default-theme not found; relying on apply-branding.sh alternatives"
    fi
    if have update-initramfs; then
        # Rebuild per kernel: '-u' only refreshes an initrd that already
        # exists; a live squashfs whose /boot has vmlinuz-* but no
        # initrd.img-* needs '-c'.  build-iso.sh copies the newest pair into
        # casper/ afterwards (SYNC_CASPER_KERNEL=1).
        n_k=0
        for kimg in /boot/vmlinuz-*; do
            [ -f "${kimg}" ] || continue
            kver="${kimg#/boot/vmlinuz-}"
            n_k=$(( n_k + 1 ))
            if [ -f "/boot/initrd.img-${kver}" ]; then
                log "update-initramfs -u -k ${kver} (this takes a while)"
                update-initramfs -u -k "${kver}" || warn "update-initramfs -u -k ${kver} failed — boot splash keeps the previous theme"
            else
                log "update-initramfs -c -k ${kver} (no initrd yet; this takes a while)"
                update-initramfs -c -k "${kver}" || warn "update-initramfs -c -k ${kver} failed"
            fi
        done
        [ "${n_k}" -gt 0 ] || warn "no /boot/vmlinuz-* in the image — initramfs not rebuilt (casper keeps the base initrd)"
    else
        warn "update-initramfs not available; initramfs not rebuilt"
    fi
else
    warn "plymouth theme /usr/share/plymouth/themes/${PLYMOUTH_THEME} missing — boot splash unchanged"
fi

# ---------------------------------------------------------------------------
# 6. /etc/skel defaults (per-user defaults shipped by lindos-desktop, if any)
# ---------------------------------------------------------------------------
if [ -d /usr/share/lindos/skel ]; then
    log "copying /usr/share/lindos/skel → /etc/skel"
    mkdir -p /etc/skel
    cp -a /usr/share/lindos/skel/. /etc/skel/
fi
# The live user (casper) and every new account get the Lindos first-run gate.
if [ -f /etc/xdg/autostart/lindos-setup.desktop ]; then
    log "lindos-setup autostart present (first-run OOBE)"
else
    warn "/etc/xdg/autostart/lindos-setup.desktop missing — OOBE will not auto-start"
fi

# Default wallpaper alternative (belt and braces; apply-branding does it too)
if have update-alternatives && [ -f /usr/share/backgrounds/lindos/aurora-dark.svg ]; then
    update-alternatives --install /usr/share/images/desktop-base/desktop-background \
        desktop-background /usr/share/backgrounds/lindos/aurora-dark.svg 60 >/dev/null 2>&1 || true
fi

log "branding check: $(cat /etc/lindos-release 2>/dev/null || echo 'no /etc/lindos-release')"
grep -E '^(NAME|PRETTY_NAME|ID|VERSION_CODENAME)=' /etc/os-release 2>/dev/null | sed 's/^/  os-release: /' >&2 || true

hook_end
