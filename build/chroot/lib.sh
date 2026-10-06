#!/bin/bash
# shellcheck shell=bash
# ============================================================================
#  build/chroot/lib.sh — shared helpers for the Lindos chroot hooks.
#
#  build/build-iso.sh stages the whole build/chroot/ directory to
#  /tmp/lindos/hooks/ inside the squashfs chroot and runs every
#  NN-*.sh hook there.  This file has no NN- prefix, so it is never run;
#  every hook sources it:
#
#      # shellcheck source=build/chroot/lib.sh
#      . "$(dirname "$(readlink -f "$0")")/lib.sh"
#
#  Provides
#      log / warn / die         prefixed, timestamped messages
#      hook_begin / hook_end    banner + elapsed time
#      apt_update               apt-get update (once per hook unless forced)
#      apt_install PKG…         install without recommends (APT_OPTS honoured)
#      apt_install_full PKG…    install with recommends
#      apt_try_install PKG…     install each package on its own, warn on failure
#      apt_purge PKG…           purge only the packages that are installed
#      pkg_installed PKG        true if dpkg says "installed"
#      pkg_available PKG        true if apt has a candidate for PKG
#      pkgs_installed_matching GLOB…   installed packages matching dpkg-query globs
#      mark_manual_installed PKG…      apt-mark manual, installed names only
#      meta_deps_installed META…       installed Depends/Recommends of installed metapackages
#      mark_meta_deps_manual           keep everything mint-meta-* pulled in before it goes
#      SESSION_PKGS / session_pkg_re   the packages a graphical XFCE session cannot start without
#      mark_session_manual             apt-mark manual for those (installed ones)
#      unneeded_pkgs                   what 'apt-get -s autoremove' would remove right now (changes nothing)
#      svc_disable UNIT…        systemctl disable (offline, chroot-safe)
#      svc_mask UNIT…           systemctl mask
#      fetch URL DEST           curl/wget download to a temp file, atomic move
#      online                   quick reachability check (archive.ubuntu.com)
#      in_chroot                true when running inside the build chroot
#
#  Every hook is expected to be idempotent, non-interactive and to source
#  /tmp/lindos/config.env (done here via lindos_hook_config).
# ============================================================================

if [ -n "${__LINDOS_HOOK_LIB:-}" ]; then
    return 0 2>/dev/null || true
fi
__LINDOS_HOOK_LIB=1

HOOK_NAME="$(basename "${0:-hook}" .sh)"
HOOK_START="$(date +%s)"

# Where build-iso.sh stages our inputs inside the chroot.
: "${LINDOS_STAGE_DIR:=/tmp/lindos}"
: "${LINDOS_HOOKS_DIR:=${LINDOS_STAGE_DIR}/hooks}"
: "${LINDOS_DEBS_DIR:=${LINDOS_STAGE_DIR}/debs}"
: "${LINDOS_ASSETS_DIR:=${LINDOS_STAGE_DIR}/assets}"
: "${LINDOS_CONFIG_ENV:=${LINDOS_STAGE_DIR}/config.env}"
export LINDOS_STAGE_DIR LINDOS_HOOKS_DIR LINDOS_DEBS_DIR LINDOS_ASSETS_DIR LINDOS_CONFIG_ENV

export DEBIAN_FRONTEND=noninteractive
export DEBCONF_NONINTERACTIVE_SEEN=true
export LC_ALL=C.UTF-8
export LANG=C.UTF-8
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1
export APT_LISTCHANGES_FRONTEND=none
export UCF_FORCE_CONFFOLD=1
# LINDOS_HOOK_PATH_PREFIX is a test seam (fake apt-get/dpkg-query first in PATH); unset in real builds.
export PATH="${LINDOS_HOOK_PATH_PREFIX:+${LINDOS_HOOK_PATH_PREFIX}:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_hts() { date '+%T'; }

log()  { printf '[%s] %s: %s\n' "$(_hts)" "${HOOK_NAME}" "$*" >&2; }
warn() { printf '[%s] %s: WARNING: %s\n' "$(_hts)" "${HOOK_NAME}" "$*" >&2; }
die()  { printf '[%s] %s: ERROR: %s\n' "$(_hts)" "${HOOK_NAME}" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

hook_begin() {
    log "==================== ${HOOK_NAME} ${*:-} ===================="
}

hook_end() {
    local now dur
    now="$(date +%s)"
    dur=$(( now - HOOK_START ))
    log "-------------------- ${HOOK_NAME} done in ${dur}s --------------------"
}

# lindos_hook_config — source the staged config.env (defaults only; env wins).
lindos_hook_config() {
    if [ -f "${LINDOS_CONFIG_ENV}" ]; then
        # shellcheck source=build/config.env
        . "${LINDOS_CONFIG_ENV}"
    else
        warn "config not found at ${LINDOS_CONFIG_ENV}; using built-in defaults"
        : "${LINDOS_VERSION:=1.0.0}"
        : "${LINDOS_CODENAME:=Aurora}"
        : "${BASE_UBUNTU_CODENAME:=noble}"
        : "${APT_OPTS:=-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o Acquire::Retries=3}"
    fi
    # APT_OPTS is a whitespace separated string in config.env → array here.
    read -r -a APT_ARGS <<< "${APT_OPTS:--y}"
}
lindos_hook_config

in_chroot() {
    # ischroot(1) exits 0 in a chroot; fall back to comparing root inodes.
    if have ischroot; then
        ischroot 2>/dev/null && return 0
    fi
    [ "$(stat -c %i / 2>/dev/null)" != "2" ]
}

# ---------------------------------------------------------------------------
# apt helpers
# ---------------------------------------------------------------------------
__APT_UPDATED=0

apt_update() {
    if [ "${__APT_UPDATED}" -eq 1 ] && [ "${1:-}" != "--force" ]; then
        return 0
    fi
    log "apt-get update"
    if apt-get "${APT_ARGS[@]}" update; then
        __APT_UPDATED=1
    else
        warn "apt-get update failed (network?) — continuing with existing lists"
    fi
}

apt_install() {
    [ "$#" -gt 0 ] || return 0
    log "apt-get install --no-install-recommends: $*"
    apt-get "${APT_ARGS[@]}" install --no-install-recommends "$@"
}

apt_install_full() {
    [ "$#" -gt 0 ] || return 0
    log "apt-get install (with recommends): $*"
    apt-get "${APT_ARGS[@]}" install "$@"
}

# apt_try_install PKG… — best effort, one transaction per package so a single
# unavailable package does not abort the rest.  Returns 0 always.
apt_try_install() {
    local p
    for p in "$@"; do
        if pkg_installed "${p}"; then
            log "already installed: ${p}"
            continue
        fi
        if ! pkg_available "${p}"; then
            warn "not available in configured repositories, skipping: ${p}"
            continue
        fi
        if ! apt-get "${APT_ARGS[@]}" install --no-install-recommends "${p}"; then
            warn "failed to install ${p} (continuing)"
        fi
    done
    return 0
}

pkg_installed() {
    local st
    st="$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true)"
    [ "${st}" = "installed" ]
}

pkg_available() {
    local cand
    cand="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2; exit}')"
    [ -n "${cand}" ] && [ "${cand}" != "(none)" ]
}

# pkgs_installed_matching GLOB… — installed packages matching any dpkg-query glob, one per line.
pkgs_installed_matching() {
    [ "$#" -gt 0 ] || return 0
    # one dpkg-query for all the globs; an unmatched glob only makes it exit non-zero
    # shellcheck disable=SC2016  # dpkg-query's own ${...} format, not shell
    { dpkg-query -W -f='${db:Status-Status} ${Package}\n' "$@" 2>/dev/null || true; } | awk '$1=="installed"{print $2}' | sort -u
}

# mark_manual_installed PKG… — apt-mark manual for the installed ones (one unknown name would fail the whole call).
mark_manual_installed() {
    local keep=()
    [ "$#" -gt 0 ] || return 0
    mapfile -t keep < <(pkgs_installed_matching "$@")
    [ "${#keep[@]}" -gt 0 ] || return 0
    apt-mark manual "${keep[@]}" >/dev/null 2>&1 || warn "apt-mark manual failed for: ${keep[*]}"
    return 0
}

# meta_deps_installed META… — installed packages that the (installed) metapackages Depend on or Recommend.
meta_deps_installed() {
    local m names=()
    mapfile -t names < <(
        for m in "$@"; do
            # shellcheck disable=SC2016  # dpkg-query's own ${...} format, not shell
            dpkg-query -W -f='${Depends}\n${Recommends}\n' "${m}" 2>/dev/null || true
        done | tr ',|' '\n\n' | sed -E 's/\([^)]*\)//g; s/:[A-Za-z0-9_-]+$//; s/[[:space:]]+//g' | sort -u | grep -v '^$' || true)
    [ "${#names[@]}" -gt 0 ] || return 0
    pkgs_installed_matching "${names[@]}"
}

# mark_meta_deps_manual — Mint's mint-meta-* metapackages Depend on the whole desktop (Thunar, the panel plugins,
# file-roller, …).  Purging one (or letting a purge take it along) turns every one of those into an autoremove
# candidate, so they are marked manual first.  Idempotent; a no-op when no metapackage is installed.
mark_meta_deps_manual() {
    local metas=() deps=()
    mapfile -t metas < <(pkgs_installed_matching 'mint-meta-*')
    [ "${#metas[@]}" -gt 0 ] || { log "no mint-meta-* package installed: nothing to keep"; return 0; }
    mapfile -t deps < <(meta_deps_installed "${metas[@]}")
    if [ "${#deps[@]}" -eq 0 ]; then
        warn "could not read the dependencies of ${metas[*]} (dpkg-query); relying on AUTOREMOVE_PROTECT_RE"
        return 0
    fi
    log "marking ${#deps[@]} package(s) that ${metas[*]} pulled in as manually installed"
    mark_manual_installed "${deps[@]}"
    return 0
}

# The packages a graphical XFCE session cannot start without: the session, window manager, panel, desktop and settings
# daemons, xfconf (xfce4-session asks it for its failsafe session), the session bus (dbus-x11 or dbus-user-session, with
# libpam-systemd), the display manager with its greeter, an X server, the network and boot-splash basics.
# 76-mint-purge.sh marks them manual and skips a purge that would leave one an autoremove candidate; 82-session-sanity.sh
# requires them in the finished image.
SESSION_PKGS=(xfce4-session xfwm4 xfce4-panel xfdesktop4 xfconf xfce4-settings dbus-x11 dbus-user-session libpam-systemd
    lightdm slick-greeter xorg xserver-xorg-core network-manager plymouth)

# session_pkg_re - SESSION_PKGS as an anchored extended regular expression
session_pkg_re() {
    local IFS='|'
    printf '^(%s)$' "${SESSION_PKGS[*]}"
}

# mark_session_manual - the installed SESSION_PKGS become manually installed (idempotent)
mark_session_manual() {
    mark_manual_installed "${SESSION_PKGS[@]}"
}

# unneeded_pkgs - names 'apt-get -s autoremove' would remove right now, one per line (a simulation: nothing changes)
unneeded_pkgs() {
    { apt-get -s autoremove 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u; } || true
}

apt_purge() {
    local p
    local todo=()
    for p in "$@"; do
        if pkg_installed "${p}"; then
            todo+=("${p}")
        else
            log "not installed, nothing to purge: ${p}"
        fi
    done
    [ "${#todo[@]}" -gt 0 ] || return 0
    for p in "${todo[@]}"; do
        log "apt-get purge ${p}"
        apt-get "${APT_ARGS[@]}" purge "${p}" || warn "purge failed for ${p} (continuing)"
    done
    return 0
}

# Packages that 'apt-get autoremove' must never take away.  Mint's mint-meta-* metapackages *Depend* on the seeded
# desktop apps, so purging one of them (hexchat, rhythmbox, ...) removes the metapackage - and everything the
# metapackage pulled in becomes an autoremove candidate.  mark_meta_deps_manual (above) marks those manual before
# any purge; this pattern is the second line of defence.  It names what must stay instead of the blanket 'mint',
# 'gnome-' and 'mate-' prefixes, so the Mint artwork and apps that 76-mint-purge.sh removes are not protected here
# (they go through its guarded, simulated purge, not through an autoremove sweep).
AUTOREMOVE_PROTECT_RE='^(xfce4|xfwm4|xfdesktop4|xfconf|thunar|tumbler|lightdm|slick-greeter|light-locker|linuxmint-keyring|mintupdate|mintinstall|mintdrivers|mintsources|mintreport|mintsystem|mintlocale|mint-common|mint-info|mint-mirrors|mint-translations|ubuntu-system-adjustments|aptitude|aptkit|timeshift|network-manager|nm-|cups|system-config-printer|avahi|casper|ubiquity|linux-|grub|shim|plymouth|pulseaudio|pipewire|wireplumber|mesa|libgl|libegl|libdrm|xserver|xorg|xinit|x11|python3|gir1\.2|libgtk|gtk|glib|gvfs|udisks|upower|policykit|polkit|systemd|libpam-systemd|libxfce4|libxfconf|dbus|firefox|thunderbird|blueman|bluez|gnome-(keyring|themes|calculator|disk-utility|system-tools|online-accounts|font-viewer)|libreoffice|fonts-|hicolor|adwaita|mate-polkit|xdg-|initramfs|busybox|lupin|memtest|efibootmgr|os-prober|file-roller|mousepad|ristretto|evince|vlc|lindos-)'

# safe_autoremove — apt-get autoremove --purge that cannot dismantle the
# desktop: candidates matching AUTOREMOVE_PROTECT_RE are marked "manually
# installed" first (which also keeps their dependencies); if the remaining
# candidate list is still suspiciously long, everything is kept.
safe_autoremove() {
    local max="${1:-60}"
    local victims protected=() rest=() p n
    victims="$(apt-get -s autoremove --purge 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u)" || victims=""
    if [ -z "${victims}" ]; then
        log "autoremove: nothing to remove"
        return 0
    fi
    while IFS= read -r p; do
        [ -n "${p}" ] || continue
        if printf '%s\n' "${p}" | grep -qE "${AUTOREMOVE_PROTECT_RE}"; then
            protected+=("${p}")
        else
            rest+=("${p}")
        fi
    done <<< "${victims}"
    if [ "${#protected[@]}" -gt 0 ]; then
        warn "autoremove would remove protected packages — marking them manual: ${protected[*]}"
        apt-mark manual "${protected[@]}" >/dev/null 2>&1 || warn "apt-mark manual failed"
        victims="$(apt-get -s autoremove --purge 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u)" || victims=""
        rest=()
        while IFS= read -r p; do
            [ -n "${p}" ] && rest+=("${p}")
        done <<< "${victims}"
    fi
    n="${#rest[@]}"
    if [ "${n}" -eq 0 ]; then
        log "autoremove: nothing left to remove"
        return 0
    fi
    if [ "${n}" -gt "${max}" ]; then
        warn "autoremove would still remove ${n} packages (> ${max}); keeping them all (marking manual)"
        apt-mark manual "${rest[@]}" >/dev/null 2>&1 || true
        return 0
    fi
    log "apt-get autoremove --purge (${n} package(s): ${rest[*]})"
    apt-get "${APT_ARGS[@]}" autoremove --purge || warn "autoremove reported an error (continuing)"
    return 0
}

# ---------------------------------------------------------------------------
# systemd helpers — work offline in a chroot (systemctl only edits symlinks)
# ---------------------------------------------------------------------------
unit_exists() {
    local u="$1"
    [ -e "/usr/lib/systemd/system/${u}" ] || [ -e "/lib/systemd/system/${u}" ] || [ -e "/etc/systemd/system/${u}" ]
}

svc_disable() {
    local u
    for u in "$@"; do
        if unit_exists "${u}"; then
            log "systemctl disable ${u}"
            systemctl disable "${u}" >/dev/null 2>&1 || warn "could not disable ${u}"
        else
            log "unit not present, skipping: ${u}"
        fi
    done
    return 0
}

svc_mask() {
    local u
    for u in "$@"; do
        log "systemctl mask ${u}"
        systemctl mask "${u}" >/dev/null 2>&1 || warn "could not mask ${u}"
    done
    return 0
}

svc_enable() {
    local u
    for u in "$@"; do
        if unit_exists "${u}"; then
            log "systemctl enable ${u}"
            systemctl enable "${u}" >/dev/null 2>&1 || warn "could not enable ${u}"
        else
            log "unit not present, skipping: ${u}"
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# network helpers
# ---------------------------------------------------------------------------
# fetch URL DEST — download atomically (temp file + mv); curl first, wget fallback.
fetch() {
    local url="${1:?url}" dest="${2:?dest}"
    local tmp
    tmp="$(mktemp "${dest}.XXXXXX")"
    if have curl; then
        if curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 20 -o "${tmp}" "${url}"; then
            mv -f "${tmp}" "${dest}"
            return 0
        fi
    fi
    if have wget; then
        if wget -q --tries=3 --timeout=20 -O "${tmp}" "${url}"; then
            mv -f "${tmp}" "${dest}"
            return 0
        fi
    fi
    rm -f "${tmp}"
    return 1
}

online() {
    local host="${1:-archive.ubuntu.com}"
    if have curl; then
        curl -fsI --connect-timeout 8 --max-time 15 "http://${host}/" >/dev/null 2>&1 && return 0
    fi
    if have wget; then
        wget -q --spider --timeout=8 "http://${host}/" >/dev/null 2>&1 && return 0
    fi
    return 1
}
