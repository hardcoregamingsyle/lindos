#!/bin/bash
# ============================================================================
#  76-mint-purge.sh — take Linux Mint's own apps and artwork out of the image
#
#  Runs INSIDE the squashfs chroot as root, after every hook that installs
#  packages (70-gaming, 75-vm) and before 77-mint-sweep.sh.  Mint is kept only
#  as the base system (kernel, apt, casper, ubiquity, ...); what a user sees
#  must be Lindos's or thoroughly re-skinned (docs/BUILDING.md, "Unrecognisable").
#
#    1. keep-set   apt-mark manual for everything that must stay (apt trust, the
#                  mintsystem/ubuntu-system-adjustments chain that Mint's Firefox
#                  Pre-Depends on, the interim update/store/driver tools, the
#                  XApp plumbing, Thunar ...) and for everything mint-meta-*
#                  Depends on: a purge must never orphan the desktop.  The packages a graphical
#                  session cannot start without (lib.sh SESSION_PKGS: xfce4-session, xfwm4, the
#                  panel, xfdesktop4, xfconf, xfce4-settings, the session bus, lightdm, the
#                  greeter, Xorg ...) are marked manual explicitly as well
#    2. purge      in small groups, each behind a simulation:
#                    mint-meta-xfce / mint-meta-core        (nothing depends on them)
#                    the artwork stack (mint-artwork is what puts Mint's xfconf
#                      defaults ahead of Lindos's through /etc/xdg/xdg-xfce)
#                    Mint's own apps that Lindos has no use for
#                    xed / xviewer / pix / xreader+thingy / celluloid - only when
#                      their replacement (mousepad / ristretto / evince / vlc)
#                      is installed
#                  A group is purged only when 'apt-get -s purge' would remove
#                  NOTHING outside the group AND 'apt-get -s --auto-remove purge' would not
#                  leave a session package behind as an autoremove candidate (it is marked
#                  manual first; the group is skipped when that does not help).  Otherwise
#                  the group is skipped, MINT-PURGE-SKIPPED is logged loudly and the build
#                  goes on: this hook never fails the ISO build (only the host guard dies).
#    3. afterwards the session packages are checked once more (a simulated autoremove
#                  must not list one: MINT-SESSION-OK / MINT-SESSION-KEPT), a leftover
#                  /etc/xdg/xdg-{xfce,default,default.desktop} symlink into mint-artwork
#                  is moved aside (MINT-XDG-FIXED), the Matrix web app of /etc/skel goes,
#                  caches are rebuilt, dpkg is audited.
#
#  Deliberately NOT purged (no Lindos replacement yet, or the base needs them):
#    mintupdate mintinstall mintdrivers mintsources mintreport   interim tools, re-skinned by the sweep
#    mintlocale (+im)            Ubiquity's language-pack step may call it (unverified)
#    mint-meta-codecs            purging it would orphan the codecs it pulled in
#    mintsystem mint-common mint-info-xfce mint-translations ubuntu-system-adjustments linuxmint-keyring
#                                Firefox Pre-Depends the chain; the GRUB title comes from it; apt trusts the key
#
#  Test seams (unset in real builds): LINDOS_MINT_ROOT (prefix for the files this hook edits),
#  LINDOS_HOOK_PATH_PREFIX (lib.sh: fake apt-get/apt-mark/dpkg-query first in PATH),
#  LINDOS_MINT_PURGE=0 (skip the whole hook).
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "mint purge"

ROOT="${LINDOS_MINT_ROOT:-}"
: "${LINDOS_MINT_PURGE:=1}"

# This hook removes packages; never let it run against a build host by accident.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to purge packages outside the build chroot (set LINDOS_MINT_ROOT for a dry run)"
fi

if [ "${LINDOS_MINT_PURGE}" = "0" ]; then
    log "LINDOS_MINT_PURGE=0 - the Mint purge is switched off"
    hook_end
    exit 0
fi

rel() { printf '%s' "${1#"${ROOT}"}"; }

PURGED=()
SKIPPED=()
FAILED=()

# What must survive every purge below (installed names only; apt-mark manual is idempotent).
KEEP_SET=(
    # apt trust and the mintsystem chain Mint's Firefox Pre-Depends (GRUB title, sysctl/modprobe tweaks)
    linuxmint-keyring mint-info-xfce mintsystem mint-common mint-translations mint-mirrors aptkit
    ubuntu-system-adjustments aptitude plymouth-theme-ubuntu-text ubuntu-dbgsym-keyring
    # interim tools: their Lindos replacements ship later, the sweep re-skins them meanwhile
    mintupdate mintinstall mintdrivers mintsources mintreport python3-repolib timeshift
    # XApp plumbing, GTK/portal bits and the desktop parts the metapackages pulled in
    libxapp1 gir1.2-xapp-1.0 python3-xapp xapps-common xdg-desktop-portal-xapp xfce4-xapp-status-plugin
    libadwaita-1-0 packagekit network-manager-gnome blueman ubuntu-drivers-common
    thunar thunar-data thunar-archive-plugin thunar-volman tumbler xfce4-session xfce4-terminal
    gnome-calculator file-roller firefox thunderbird
    # the installer and the live system
    casper ubiquity ubiquity-frontend-gtk ubiquity-casper
    # the default apps that take over from xed / xviewer / pix / xreader / celluloid
    mousepad ristretto evince vlc
)
KEEP_GLOBS=('mintlocale*' 'mint-meta-codecs')

skip_group() {   # skip_group NAME REASON
    SKIPPED+=("$1")
    warn "MINT-PURGE-SKIPPED group '$1': $2"
}

# sim_removals PKG… — names 'apt-get -s purge' would remove, one per line (empty when apt gives no answer).
sim_removals() {
    apt-get -s purge "$@" 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u || true
}

# sim_orphans PKG… — names 'apt-get -s --auto-remove purge' would remove: the packages plus everything that is unneeded afterwards.
sim_orphans() {
    { apt-get -s --auto-remove purge "$@" 2>/dev/null | awk '/^(Remv|Purg) /{print $2}' | sed 's/:.*$//' | sort -u; } || true
}

# session_hits LIST — the lines of LIST (names, one per line) that are session packages (lib.sh SESSION_PKGS)
session_hits() {
    grep -E "$(session_pkg_re)" <<< "$1" || true
}

# purge_group NAME REQUIRES PATTERN…
#   REQUIRES  space separated packages that must be installed first ("-" = none): the replacements
#   PATTERN   dpkg-query globs; the installed packages they match are purged together, and are also
#             the allow-list: the simulation may not name anything else
#   GROUP_EXCLUDE_RE (optional, global)  names to drop from the matches
purge_group() {
    local name="$1" requires="$2" r
    shift 2
    local targets=() sim extra orph hits=()
    if [ "${requires}" != "-" ]; then
        for r in ${requires}; do
            if ! pkg_installed "${r}"; then
                skip_group "${name}" "replacement '${r}' is not installed - the Mint app stays (it is hidden by the sweep)"
                return 0
            fi
        done
    fi
    mapfile -t targets < <(pkgs_installed_matching "$@" | { grep -Ev "${GROUP_EXCLUDE_RE:-^\$}" || true; })
    if [ "${#targets[@]}" -eq 0 ]; then
        log "group ${name}: nothing installed"
        return 0
    fi
    sim="$(sim_removals "${targets[@]}")"
    if [ -z "${sim}" ]; then
        skip_group "${name}" "apt-get -s purge ${targets[*]} gave no answer"
        return 0
    fi
    extra="$(comm -13 <(printf '%s\n' "${targets[@]}" | sort -u) <(printf '%s\n' "${sim}") | tr '\n' ' ' || true)"
    if [ -n "${extra}" ]; then
        skip_group "${name}" "purging ${targets[*]} would also remove: ${extra}"
        return 0
    fi
    # the purge itself touches nothing else; what would be left unneeded afterwards (an autoremove, here or in the installer)?
    # a session package on that list is kept first (manual); the group is skipped when apt still lists it
    orph="$(comm -13 <(printf '%s
' "${targets[@]}" | sort -u) <(sim_orphans "${targets[@]}") || true)"
    mapfile -t hits < <(session_hits "${orph}")
    if [ "${#hits[@]}" -gt 0 ]; then
        warn "MINT-PURGE-SESSION-KEPT group '${name}': purging ${targets[*]} would leave session package(s) unneeded (${hits[*]}) - marking them manual"
        mark_manual_installed "${hits[@]}"
        orph="$(comm -13 <(printf '%s
' "${targets[@]}" | sort -u) <(sim_orphans "${targets[@]}") || true)"
        mapfile -t hits < <(session_hits "${orph}")
        if [ "${#hits[@]}" -gt 0 ]; then
            skip_group "${name}" "purging ${targets[*]} would still leave session package(s) as autoremove candidates: ${hits[*]}"
            return 0
        fi
    fi
    log "group ${name}: apt-get purge ${targets[*]}"
    if apt-get "${APT_ARGS[@]}" purge "${targets[@]}"; then
        PURGED+=("${targets[@]}")
    else
        FAILED+=("${name}")
        warn "MINT-PURGE-FAILED group '${name}': apt-get purge exited non-zero (continuing; dpkg is audited below)"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 1. keep-set: manual before anything goes
# ---------------------------------------------------------------------------
step_keep() {
    local globbed=()
    mark_manual_installed "${KEEP_SET[@]}"
    mapfile -t globbed < <(pkgs_installed_matching "${KEEP_GLOBS[@]}")
    [ "${#globbed[@]}" -eq 0 ] || mark_manual_installed "${globbed[@]}"
    mark_meta_deps_manual
    mark_session_manual
}

# ---------------------------------------------------------------------------
# 2. the purges
# ---------------------------------------------------------------------------
step_purge() {
    # metapackages first (nothing depends on them); mint-meta-codecs stays, its codecs would be orphaned
    GROUP_EXCLUDE_RE='^mint-meta-codecs$' purge_group "metapackages" - 'mint-meta-*'
    # artwork stack: the source of the xdg-xfce precedence bug, the Mint wallpapers and the 100+ Mint-* themes
    purge_group "artwork" - 'mint-artwork*' 'mint-themes*' 'mint-x-icons' 'mint-y-icons' 'mint-l-icons' 'mint-l-theme' \
        'mint-cursor-themes' 'mint-backgrounds-*'
    # Mint's own apps that Lindos does not replace one by one
    purge_group "welcome" - mintwelcome captain
    purge_group "backup" - mintbackup
    purge_group "desktop-settings" - mintdesktop
    purge_group "usb-writer" - mintstick
    purge_group "login-window" - lightdm-settings
    purge_group "fingerprints" - fingwit libpam-fingwit
    purge_group "upgrade-info" - mint-upgrade-info
    purge_group "webapps" - mintchat webapp-manager
    purge_group "file-sharing" - warpinator
    purge_group "notes" - sticky
    purge_group "iptv" - hypnotix
    purge_group "neofetch" - neofetch
    purge_group "toy-plugins" - xfce4-eyes-plugin xfce4-mailwatch-plugin xfce4-timer-plugin xfce4-time-out-plugin \
        xfce4-verve-plugin xfce4-cpufreq-plugin xfce4-systemload-plugin
    # apps that have a default replacement: only once that replacement is installed
    purge_group "text-editor" mousepad xed xed-common xed-dbg
    purge_group "image-viewers" ristretto 'xviewer*' 'gir1.2-xviewer*' pix 'pix-*'
    purge_group "pdf-viewer" evince thingy 'xreader*' 'libxreader*' 'gir1.2-xreader*'
    purge_group "media-player" vlc celluloid
}

# ---------------------------------------------------------------------------
# 3. after the purges
# ---------------------------------------------------------------------------
# What would an autoremove take now (here at the end of the build, or Ubiquity's in the target)?  The session packages
# are manual since step_keep, so this only fires when apt still lists one (an apt-mark that failed, a hook that installed
# one afterwards).  It keeps them; it never fails the build (82-session-sanity.sh checks what is installed at the very end).
step_session_guard() {
    local hits=() un
    un="$(unneeded_pkgs)"
    mapfile -t hits < <(session_hits "${un}")
    if [ "${#hits[@]}" -eq 0 ]; then
        log "MINT-SESSION-OK: no session package is an autoremove candidate"
        return 0
    fi
    warn "MINT-SESSION-KEPT: apt would autoremove session package(s) (${hits[*]}) - marking them manual"
    mark_manual_installed "${hits[@]}"
    un="$(unneeded_pkgs)"
    mapfile -t hits < <(session_hits "${un}")
    if [ "${#hits[@]}" -gt 0 ]; then
        warn "MINT-SESSION-AT-RISK: apt still lists session package(s) as autoremove candidates: ${hits[*]}"
    fi
    return 0
}

# The Debian Xsession script prepends /etc/xdg/xdg-$DESKTOP_SESSION to XDG_CONFIG_DIRS, and mint-artwork makes
# that (and xdg-default*) a symlink to its own xfconf defaults, which then outrank the Lindos ones in /etc/xdg.
# lindos-desktop diverts the symlinks; if one still points into mint-artwork, move it aside here.
step_xdg_shadow() {
    local n p target left=0
    for n in xdg-xfce xdg-default xdg-default.desktop; do
        p="${ROOT}/etc/xdg/${n}"
        [ -L "${p}" ] || continue
        target="$(readlink -f "${p}" 2>/dev/null || true)"
        case "${target}" in
            *mint-artwork*)
                if mv -f "${p}" "${p}.lindos-orig" 2>/dev/null; then
                    log "MINT-XDG-FIXED: $(rel "${p}") pointed into mint-artwork; moved to $(rel "${p}").lindos-orig"
                    mkdir -p "${p}" 2>/dev/null || true
                else
                    warn "MINT-XDG-SHADOW: $(rel "${p}") still points into mint-artwork and could not be moved - Mint's xfconf defaults outrank Lindos's"
                    left=$(( left + 1 ))
                fi
                ;;
            *) log "$(rel "${p}") -> ${target:-?}: not Mint's" ;;
        esac
    done
    [ "${left}" -eq 0 ] || warn "MINT-XDG-SHADOW: ${left} link(s) into mint-artwork remain"
    return 0
}

# The Matrix web app (mintchat) is copied into every new user's menu from /etc/skel.
step_skel_webapps() {
    local f d n=0
    for f in "${ROOT}"/etc/skel/.local/share/applications/webapp-*.desktop; do
        [ -f "${f}" ] || continue
        if grep -qiE 'linuxmint|mintchat' "${f}"; then
            rm -f "${f}" && n=$(( n + 1 )) && log "removed the Mint web app entry $(rel "${f}")"
        fi
    done
    for d in "${ROOT}/etc/skel/.local/share/applications" "${ROOT}/etc/skel/.local/share" "${ROOT}/etc/skel/.local"; do
        rmdir "${d}" 2>/dev/null || true
    done
    [ "${n}" -gt 0 ] || log "no Mint web app entry in /etc/skel"
    return 0
}

step_default_apps() {
    local p
    for p in mousepad ristretto evince vlc; do
        if pkg_installed "${p}"; then
            log "default app installed: ${p}"
        else
            warn "MINT-DEFAULT-APP-MISSING: ${p} is not installed (20-base.sh installs it best effort) - its Mint counterpart stays"
        fi
    done
    return 0
}

step_caches() {
    [ "${#PURGED[@]}" -gt 0 ] || return 0
    [ -z "${ROOT}" ] || return 0
    if have glib-compile-schemas && [ -d /usr/share/glib-2.0/schemas ]; then
        glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || warn "glib-compile-schemas failed"
    fi
    if have update-icon-caches; then
        update-icon-caches /usr/share/icons/* >/dev/null 2>&1 || warn "update-icon-caches reported errors"
    fi
    if have update-desktop-database; then
        update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
    fi
    if have update-mime-database && [ -d /usr/share/mime ]; then
        update-mime-database /usr/share/mime >/dev/null 2>&1 || true
    fi
    return 0
}

step_dpkg_audit() {
    local bad
    bad="$(dpkg --audit 2>/dev/null || true)"
    [ -n "${bad}" ] || return 0
    warn "MINT-PURGE-DPKG-AUDIT: dpkg reports unfinished packages after the purges; trying to repair"
    printf '%s\n' "${bad}" >&2
    dpkg --configure -a >/dev/null 2>&1 || true
    apt-get "${APT_ARGS[@]}" -f install >/dev/null 2>&1 || warn "apt-get -f install failed (continuing)"
    return 0
}

step_keep          || warn "keep-set step failed (continuing)"
step_purge         || warn "purge step failed (continuing)"
step_session_guard || warn "session package check failed (continuing)"
step_xdg_shadow    || warn "xdg check failed (continuing)"
step_skel_webapps  || warn "skel web app step failed (continuing)"
step_default_apps  || warn "default app check failed (continuing)"
step_caches        || warn "cache refresh failed (continuing)"
step_dpkg_audit    || warn "dpkg audit failed (continuing)"

log "MINT-PURGE-RESULT purged=${#PURGED[@]} skipped=${#SKIPPED[@]} failed=${#FAILED[@]}"
[ "${#PURGED[@]}" -eq 0 ] || log "purged: ${PURGED[*]}"
[ "${#SKIPPED[@]}" -eq 0 ] || warn "MINT-PURGE-SKIPPED groups: ${SKIPPED[*]}"

hook_end
