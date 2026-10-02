#!/bin/bash
# ============================================================================
#  81-unrecognisable-gate.sh — report what still makes Lindos recognisable as Linux Mint
#
#  Runs INSIDE the squashfs chroot as root, last (after 80-cleanup.sh), and only READS.  The report goes to the
#  hook log, out/hooks/81-unrecognisable-gate.log (build-iso.sh tees every hook there).  Report-only by default;
#  with LINDOS_STRICT_UNRECOGNISABLE=1 (it reaches the hook through config.env only when it is listed in
#  LINDOS_PASSTHRU_VARS) any finding fails the build.
#
#  Lines: UNRECOGNISABLE-FINDING [category] detail   counts towards strict mode
#         UNRECOGNISABLE-NOTE [category] detail      informational: kept on purpose or hidden
#         UNRECOGNISABLE-GATE findings=N notes=M mode=report-only|strict
#
#  Checks (docs/BUILDING.md, "Unrecognisable"):
#    packages   the Mint apps / artwork / metapackages that 76-mint-purge.sh removes must be gone (deny list); the
#               interim tools (mintupdate, mintinstall, mintdrivers, mintsources, mintreport, mintlocale, the
#               mintsystem chain) are notes: they stay until their Lindos replacements ship
#    desktop    a visible menu or autostart entry (usr/share/applications, /etc/xdg/autostart, /usr/local/share,
#               /etc/skel) whose Name/Comment/Keywords say Mint, whose Icon is a Mint icon, or that starts an
#               app on the deny list (Mint's apps, the duplicates Thunar/Xfce Terminal, the Xfce settings entries)
#    themes     Mint-* GTK/icon themes; Yaru / Papirus / Humanity / Mint cursor packs that are not Hidden=true
#    xdg        /etc/xdg/xdg-* symlinks into mint-artwork (Mint's xfconf defaults would outrank Lindos's)
#    wrapper    Mint's apt/search/highlight-mint/rtfm wrappers and completion in /usr/local/bin, /usr/bin
#    text       "Linux Mint" in the files a user or a script prints (/etc, /usr/local, menu entries, /usr/share/lindos
#               ...) outside an explicit allow-list (identity files, apt sources, legal text, the sweep itself)
#    grub       the Lindos GRUB_DISTRIBUTOR drop-in must sort after Mint's 50_linuxmint.cfg
#    adjust     mint-adjust's .preserve list must keep Lindos's Firefox distribution.ini
#    skel       the Matrix web app in /etc/skel
#
#  Test seams (unset in real builds): LINDOS_MINT_ROOT (prefix for every path), LINDOS_HOOK_PATH_PREFIX (lib.sh).
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "unrecognisable gate"

ROOT="${LINDOS_MINT_ROOT:-}"
: "${LINDOS_STRICT_UNRECOGNISABLE:=0}"

# The report describes the image, not the machine that builds it.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to inspect the build host outside the build chroot (set LINDOS_MINT_ROOT for a dry run)"
fi

FINDINGS=0
NOTES=0
MAX_LISTED=200

rel() { printf '%s' "${1#"${ROOT}"}"; }
finding() { FINDINGS=$(( FINDINGS + 1 )); warn "UNRECOGNISABLE-FINDING [$1] $2"; }
note() { NOTES=$(( NOTES + 1 )); log "UNRECOGNISABLE-NOTE [$1] $2"; }

# ---------------------------------------------------------------------------
# rules (one place; tests keep them in step with 76-mint-purge.sh)
# ---------------------------------------------------------------------------
# packages that must be gone after 76-mint-purge.sh
DENY_PKG_RE='^(mint-meta-(core|xfce)|mint-artwork.*|mint-themes.*|mint-[xyl]-icons|mint-l-theme|mint-cursor-themes|mint-backgrounds-.*|mintbackup|mintdesktop|mintstick|mintwelcome|captain|lightdm-settings|fingwit|libpam-fingwit|mintchat|webapp-manager|thingy|xed|xed-common|xed-dbg|xviewer.*|gir1\.2-xviewer.*|xreader.*|libxreader.*|gir1\.2-xreader.*|pix(-.*)?|celluloid|warpinator|sticky|hypnotix|neofetch|mint-upgrade-info|xfce4-(eyes|mailwatch|timer|time-out|verve|cpufreq|systemload)-plugin)$'
# kept on purpose until their Lindos replacements ship (owner decision), so notes, not findings
INTERIM_PKG_RE='^(mintupdate|mintinstall|mintdrivers|mintsources|mintreport|mintlocale.*|mint-meta-codecs|mintsystem|mint-common|mint-info-xfce|mint-translations|mint-mirrors|ubuntu-system-adjustments|linuxmint-keyring|timeshift)$'
# menu / autostart entries (file name without .desktop, or the command) that must not be visible
DENY_DESKTOP_RE='^(xed|xviewer|xreader|pix|celluloid|io\.github\.celluloid_player\.celluloid|warpinator|org\.x\.warpinator|sticky|org\.x\.sticky|hypnotix|org\.x\.hypnotix|thingy|mintwelcome|mintbackup|mintstick|mintdesktop|mintchat|webapp-manager|lightdm-settings|fingwit|mint-meta-codecs|thunar|thunar-.*|xfce4-terminal|xfce4-settings-manager|xfce-settings-manager|xfce4-appfinder|xfce4-run|xfce4-about|xfce4-.*-settings|xfce-.*-settings|xfwm4-.*|xfdesktop-settings|xfce4-notifyd-config|ccsm|menulibre|exo-.*)$'
# theme / icon / cursor packs that must be gone or hidden from the pickers
THEME_PACK_RE='^(Mint-|mint-|Yaru|Papirus|ePapirus|Humanity|ubuntu-mono|Bibata|GoogleDot|XCursor-Pro|DMZ-)'
# "Linux Mint" text that is allowed to stay (paths relative to the image root)
TEXT_ALLOW_RE='^/(etc/(os-release|lsb-release|linuxmint(/|$)|apt(/|$)|dpkg(/|$)|upstream-release(/|$)|casper\.conf|init\.d/mintsystem|default/grub\.d/50_linuxmint\.cfg|X11/Xsession\.d/99mint|ssl(/|$)|ca-certificates(/|$)|alternatives(/|$)|ld\.so\.cache|bash_completion\.d(/|$)|xdg/xdg-[a-z.]+\.lindos-orig(/|$))|usr/share/lindos/(legal/|tune/ram-budget\.json|branding/base-sweep\.json)|usr/libexec/lindos/rebrand-base\.py|usr/local/bin/[a-z-]+\.lindos-orig)|\.lindos-orig$'

# ---------------------------------------------------------------------------
# 1. packages
# ---------------------------------------------------------------------------
check_packages() {
    local p listed=0 installed
    # shellcheck disable=SC2016  # dpkg-query's own ${...} format, not shell
    installed="$(dpkg-query -W -f='${db:Status-Status} ${Package}\n' 2>/dev/null | awk '$1=="installed"{print $2}' | sort -u || true)"
    while IFS= read -r p; do
        [ -n "${p}" ] || continue
        if printf '%s\n' "${p}" | grep -Eq "${DENY_PKG_RE}"; then
            finding packages "installed: ${p} (76-mint-purge.sh should have removed it - see MINT-PURGE-SKIPPED in out/hooks/76-mint-purge.log)"
        elif printf '%s\n' "${p}" | grep -Eq "${INTERIM_PKG_RE}"; then
            listed=$(( listed + 1 ))
            note packages "kept on purpose (no Lindos replacement yet): ${p}"
        fi
    done <<< "${installed}"
    [ "${listed}" -gt 0 ] || log "no interim Mint tool installed"
    return 0
}

# ---------------------------------------------------------------------------
# 2. menu and autostart entries
# ---------------------------------------------------------------------------
desktop_visible() {   # FILE - true when the entry shows up in an XFCE menu / starts in an XFCE session
    local f="$1"
    if grep -qiE '^(NoDisplay|Hidden)[[:space:]]*=[[:space:]]*true[[:space:]]*$' "${f}"; then return 1; fi
    if grep -qiE '^NotShowIn[[:space:]]*=.*XFCE' "${f}"; then return 1; fi
    if grep -qiE '^OnlyShowIn[[:space:]]*=' "${f}" && ! grep -qiE '^OnlyShowIn[[:space:]]*=.*XFCE' "${f}"; then return 1; fi
    return 0
}

check_desktop_entries() {
    local d f base cmd why n=0
    for d in "${ROOT}/usr/share/applications" "${ROOT}/usr/local/share/applications" "${ROOT}/etc/xdg/autostart" \
             "${ROOT}/etc/skel/.local/share/applications" "${ROOT}/etc/skel/.config/autostart"; do
        [ -d "${d}" ] || continue
        for f in "${d}"/*.desktop; do
            [ -f "${f}" ] || continue
            n=$(( n + 1 ))
            desktop_visible "${f}" || continue
            base="$(basename "${f}" .desktop)"
            case "${base}" in lindos-*|ubiquity*) continue ;; esac
            cmd="$(sed -n 's/^Exec=//p' "${f}" | head -n 1 | awk '{print $1}' | sed 's|.*/||' || true)"
            why=""
            if printf '%s\n' "${base}" | grep -Eiq "${DENY_DESKTOP_RE}"; then
                why="the entry is on the deny list"
            elif [ -n "${cmd}" ] && printf '%s\n' "${cmd}" | grep -Eiq "${DENY_DESKTOP_RE}"; then
                why="it starts ${cmd}, which is on the deny list"
            elif grep -qiE '^(Name|GenericName|Comment|Keywords)(\[[^]]*\])?[[:space:]]*=.*\bmint\b' "${f}"; then
                why="a visible text says Mint"
            elif grep -qiE '^Icon[[:space:]]*=[[:space:]]*mint' "${f}"; then
                why="it uses a Mint icon"
            fi
            [ -z "${why}" ] || finding desktop "$(rel "${f}"): ${why}"
        done
    done
    log "checked ${n} menu/autostart entries"
    return 0
}

# ---------------------------------------------------------------------------
# 3. themes, icons, cursors
# ---------------------------------------------------------------------------
check_themes() {
    local d p name idx
    for d in "${ROOT}/usr/share/themes" "${ROOT}/usr/share/icons"; do
        [ -d "${d}" ] || continue
        for p in "${d}"/*; do
            [ -d "${p}" ] || continue
            name="$(basename "${p}")"
            printf '%s\n' "${name}" | grep -Eq "${THEME_PACK_RE}" || continue
            idx="${p}/index.theme"
            if [ -f "${idx}" ] && grep -qiE '^Hidden[[:space:]]*=[[:space:]]*true' "${idx}"; then
                note themes "$(rel "${p}") is hidden from the pickers (Hidden=true)"
            elif [ "${d}" = "${ROOT}/usr/share/themes" ] && ! printf '%s\n' "${name}" | grep -Eq '^(Mint-|mint-)'; then
                note themes "$(rel "${p}") cannot be hidden from the GTK theme picker"
            else
                finding themes "$(rel "${p}") is a base theme pack and is not hidden from the pickers"
            fi
        done
    done
    return 0
}

# ---------------------------------------------------------------------------
# 4. /etc/xdg/xdg-* precedence
# ---------------------------------------------------------------------------
check_xdg() {
    local p target
    for p in "${ROOT}"/etc/xdg/xdg-*; do
        [ -e "${p}" ] || [ -L "${p}" ] || continue
        case "${p}" in *.lindos-orig) continue ;; esac
        target="$(readlink -f "${p}" 2>/dev/null || true)"
        case "${target}" in
            *mint-artwork*)
                finding xdg "$(rel "${p}") resolves into mint-artwork: its xfconf defaults outrank the Lindos ones in /etc/xdg" ;;
            *)
                if [ -d "${p}/xfce4" ]; then
                    note xdg "$(rel "${p}") has an xfce4/ tree: it outranks /etc/xdg/xfce4 in an XFCE session"
                fi ;;
        esac
    done
    return 0
}

# ---------------------------------------------------------------------------
# 5. command wrappers
# ---------------------------------------------------------------------------
check_wrappers() {
    local f name
    for f in "${ROOT}"/usr/local/bin/*; do
        [ -e "${f}" ] || [ -L "${f}" ] || continue
        name="$(basename "${f}")"
        case "${name}" in
            lindos*|*.lindos-orig) continue ;;
            apt|search|highlight-mint|rtfm) finding wrapper "$(rel "${f}") is Mint's command wrapper" ;;
            *) note wrapper "$(rel "${f}") is not a Lindos file" ;;
        esac
    done
    for f in "${ROOT}/usr/bin/rtfm" "${ROOT}/usr/share/bash-completion/completions/apt-linux-mint" \
             "${ROOT}/etc/bash_completion.d/apt-linux-mint"; do
        if [ -e "${f}" ] || [ -L "${f}" ]; then
            finding wrapper "$(rel "${f}") is a Mint command helper"
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# 6. "Linux Mint" in what users and scripts print
# ---------------------------------------------------------------------------
check_text() {
    local d f r listed=0 total=0 hits=()
    for d in /etc /usr/local /usr/share/applications /usr/share/lindos /usr/share/backgrounds /usr/share/pixmaps \
             /usr/share/xsessions /usr/share/lightdm /usr/share/slick-greeter /usr/lib/firefox/distribution; do
        [ -d "${ROOT}${d}" ] || continue
        while IFS= read -r f; do
            [ -n "${f}" ] || continue
            r="$(rel "${f}")"
            printf '%s\n' "${r}" | grep -Eq "${TEXT_ALLOW_RE}" && continue
            hits+=("${r}")
        done < <(grep -rIliE 'linux ?mint|linuxmint' "${ROOT}${d}" 2>/dev/null | sort -u || true)
    done
    total="${#hits[@]}"
    for r in ${hits[@]+"${hits[@]}"}; do
        listed=$(( listed + 1 ))
        [ "${listed}" -le "${MAX_LISTED}" ] || break
        finding text "${r} mentions Linux Mint (allow-list: TEXT_ALLOW_RE in 81-unrecognisable-gate.sh)"
    done
    [ "${total}" -le "${MAX_LISTED}" ] || warn "text: ${total} file(s) mention Linux Mint, only the first ${MAX_LISTED} are listed"
    return 0
}

# ---------------------------------------------------------------------------
# 7. GRUB drop-in order, mint-adjust, skel
# ---------------------------------------------------------------------------
check_grub() {
    local d="${ROOT}/etc/default/grub.d" mint="" lindos="" f base
    [ -d "${d}" ] || return 0
    for f in "${d}"/*.cfg; do
        [ -f "${f}" ] || continue
        base="$(basename "${f}")"
        case "${base}" in
            *linuxmint*) mint="${base}" ;;
            *lindos*) lindos="${base}" ;;
        esac
    done
    [ -n "${mint}" ] || return 0
    if [ -z "${lindos}" ]; then
        finding grub "${mint} sets GRUB_DISTRIBUTOR and no Lindos drop-in follows it: the boot menu title stays the base's"
    elif [ "$(printf '%s\n%s\n' "${mint}" "${lindos}" | LC_ALL=C sort | tail -n 1)" != "${lindos}" ]; then
        finding grub "${lindos} sorts before ${mint}: update-grub sources it first, so the base's GRUB_DISTRIBUTOR wins"
    else
        note grub "${lindos} sorts after ${mint}"
    fi
    if [ -e "${d}/49-lindos-distributor.cfg" ]; then
        finding grub "the obsolete 49-lindos-distributor.cfg is still installed"
    fi
    return 0
}

check_adjust() {
    local d="${ROOT}/usr/share/linuxmint/adjustments" p="${ROOT}/usr/share/linuxmint/adjustments/99-lindos.preserve"
    [ -d "${d}" ] || return 0
    if [ ! -f "${p}" ] || ! grep -q 'distribution\.ini' "${p}"; then
        finding adjust "mint-adjust would put the base's Firefox distribution.ini back at every boot (no 99-lindos.preserve)"
    else
        note adjust "99-lindos.preserve keeps Lindos's Firefox distribution.ini"
    fi
    return 0
}

check_skel() {
    local f
    for f in "${ROOT}"/etc/skel/.local/share/applications/webapp-*.desktop; do
        [ -f "${f}" ] || continue
        if grep -qiE 'linuxmint|mintchat' "${f}"; then
            finding skel "$(rel "${f}") puts the Matrix web app into every new user's menu"
        fi
    done
    return 0
}

check_packages        || warn "package check failed (continuing)"
check_desktop_entries || warn "menu check failed (continuing)"
check_themes          || warn "theme check failed (continuing)"
check_xdg             || warn "xdg check failed (continuing)"
check_wrappers        || warn "wrapper check failed (continuing)"
check_text            || warn "text check failed (continuing)"
check_grub            || warn "grub check failed (continuing)"
check_adjust          || warn "adjustments check failed (continuing)"
check_skel            || warn "skel check failed (continuing)"

if [ "${LINDOS_STRICT_UNRECOGNISABLE}" = "1" ]; then
    mode="strict"
else
    mode="report-only"
fi
log "UNRECOGNISABLE-GATE findings=${FINDINGS} notes=${NOTES} mode=${mode}"
if [ "${mode}" = "strict" ] && [ "${FINDINGS}" -gt 0 ]; then
    die "LINDOS_STRICT_UNRECOGNISABLE=1: ${FINDINGS} finding(s) - the image is still recognisable as Linux Mint"
fi

hook_end
