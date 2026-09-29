#!/bin/bash
# ============================================================================
#  78-installer-brand.sh — make the live installer say "Lindos" (SPEC §2, §8)
#
#  Runs INSIDE the squashfs chroot as root, after every hook that installs
#  packages (so nothing reinstalls ubiquity over these edits) and before
#  80-cleanup.sh.  The installer is Ubiquity (Linux Mint's fork, GTK frontend).
#  Where its Mint-isms come from, and what this hook does about each:
#
#    /var/cache/debconf/templates.dat    every ubiquity/text/* string (all
#        languages): ${RELEASE}/${DISTRO} and the few hard-coded "Linux Mint"
#        become "Lindos"; the window title "Install" becomes "Lindos Setup"
#    /usr/share/ubiquity/gtk/*.ui        hard-coded fall-back labels ("Linux Mint"
#        on the disk-resize bar) become "Lindos"
#    /usr/share/applications/ubiquity.desktop   the live-desktop launcher: name
#        "Install RELEASE" (casper substitutes RELEASE from /cdrom/.disk/info at
#        boot and copies the file to the live user's Desktop) becomes
#        "Install Lindos", Lindos icon, and the Lindos-Setup GTK skin
#    /usr/share/ubiquity-slideshow/      slideshow shown while files are copied:
#        replaced by the static, offline Lindos slideshow (build/installer/)
#    /usr/share/ubiquity/pixmaps/*.png   welcome-page images, redrawn from the
#        Lindos logo at their original pixel size
#    /usr/share/icons/hicolor/*/apps/{ubiquity,mintubiquity}.svg   window and
#        launcher icon: the Lindos logo
#    /sbin/casper-stop                   "remove the installation medium" text
#        (carries no product name; only a defensive Mint -> Lindos rewrite)
#  Not touched on purpose: .mo catalogues (compiled), Python code, partitioning
#  behaviour.  /cdrom/.disk/info (get_release() input) is written by
#  build-iso.sh, not here.
#
#  Every step is guarded — a missing file is a warning, never a build failure —
#  and idempotent.  Test seams (unset in real builds):
#    LINDOS_INSTALLER_ROOT   prefix for every absolute path (fake root in tests)
#    LINDOS_INSTALLER_SRC    where build/installer/ was staged
#    LINDOS_RSVG             rsvg-convert replacement
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "installer branding"

ROOT="${LINDOS_INSTALLER_ROOT:-}"
SRC="${LINDOS_INSTALLER_SRC:-${LINDOS_STAGE_DIR}/installer}"
RSVG="${LINDOS_RSVG:-rsvg-convert}"
PRODUCT="Lindos"
SETUP_TITLE="Lindos Setup"
THEME="Lindos-Setup"
LOGO="${ROOT}/usr/share/pixmaps/lindos-logo.svg"
EDITS=0
SKIN=0

# This hook rewrites system files; never let it run against a build host by accident.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to edit the host's installer files outside the build chroot (set LINDOS_INSTALLER_ROOT for a dry run)"
fi

rel() { printf '%s' "${1#"${ROOT}"}"; }

# edit_file FILE SED-ARGS… — sed -i on a text file (symlinks resolved so they survive);
# counts and logs real changes; never fails the build.
edit_file() {
    local f="$1" real before after
    shift
    [ -f "${f}" ] || return 0
    if ! grep -Iq . "${f}" 2>/dev/null; then
        warn "not a text file, left alone: $(rel "${f}")"
        return 0
    fi
    real="$(readlink -f "${f}")"
    before="$(cksum < "${real}")"
    if ! sed -i "$@" "${real}"; then
        warn "sed failed on $(rel "${real}") (continuing)"
        return 0
    fi
    after="$(cksum < "${real}")"
    if [ "${before}" != "${after}" ]; then
        log "edited $(rel "${real}")"
        EDITS=$(( EDITS + 1 ))
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 1. GTK skin (must come first: the launcher edit depends on it)
# ---------------------------------------------------------------------------
step_skin() {
    local src="${SRC}/themes/${THEME}" dst="${ROOT}/usr/share/themes/${THEME}"
    SKIN=0
    if [ ! -f "${src}/gtk-3.0/gtk.css" ]; then
        warn "no ${THEME} skin staged in ${SRC}; the installer keeps the session theme"
        return 0
    fi
    if [ ! -f "${ROOT}/usr/share/themes/Lindos-Dark/gtk-3.0/gtk.css" ]; then
        warn "Lindos-Dark GTK theme missing; not installing the ${THEME} skin (it builds on it)"
        return 0
    fi
    rm -rf "${dst}"
    if mkdir -p "${dst}/gtk-3.0" && cp -f "${src}/gtk-3.0/gtk.css" "${dst}/gtk-3.0/gtk.css"; then
        chmod 0755 "${dst}" "${dst}/gtk-3.0"
        chmod 0644 "${dst}/gtk-3.0/gtk.css"
        SKIN=1
        log "installed GTK skin ${THEME}"
    else
        warn "could not install the ${THEME} skin (continuing)"
        rm -rf "${dst}"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 2. The live-desktop launcher
# ---------------------------------------------------------------------------
step_launcher() {
    local name f found=0 args
    for name in ubiquity.desktop ubiquity-gtkui.desktop; do
        f="${ROOT}/usr/share/applications/${name}"
        [ -f "${f}" ] || continue
        found=1
        args=(-E
              -e "/^Name(\\[[^]]*\\])?=/ s/RELEASE/${PRODUCT}/g"
              -e "s/Linux Mint/${PRODUCT}/g")
        if [ -f "${LOGO}" ]; then
            args+=(-e "s/^Icon=(mint)?ubiquity\$/Icon=lindos-logo/")
        fi
        if [ "${SKIN}" -eq 1 ]; then
            # inside the existing "sh -c '…ubiquity gtk_ui'": one more env assignment, nothing else changes
            args+=(-e "/^Exec=.*sh -c '.*ubiquity gtk_ui/{/GTK_THEME=${THEME}/!s/sh -c '/sh -c 'GTK_THEME=${THEME} /;}")
        else
            args+=(-e "s/GTK_THEME=${THEME} //")
        fi
        edit_file "${f}" "${args[@]}"
        if [ "${SKIN}" -eq 1 ] && ! grep -q "GTK_THEME=${THEME}" "${f}"; then
            warn "$(rel "${f}"): Exec= is not in the expected sh -c form; the installer keeps the session theme"
        fi
        if grep -q 'Name=Install RELEASE' "${f}"; then
            warn "$(rel "${f}"): RELEASE marker still present in Name="
        fi
    done
    if [ "${found}" -eq 0 ]; then
        warn "no ubiquity.desktop under /usr/share/applications: no live-desktop launcher to rebrand"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 3. debconf templates: every ubiquity/text/* string
# ---------------------------------------------------------------------------
step_templates() {
    local f="${ROOT}/var/cache/debconf/templates.dat" tmp n_in n_out
    if [ ! -f "${f}" ]; then
        warn "no $(rel "${f}"): installer strings keep the base ISO's product name"
        return 0
    fi
    tmp="$(mktemp)" || { warn "mktemp failed; templates not branded"; return 0; }
    # Stanzas are blank-line separated and start with "Name:" or "Template:"; only
    # ubiquity/* stanzas are rewritten, and only inside their own lines.
    if ! awk -v product="${PRODUCT}" -v title="${SETUP_TITLE}" '
        /^[[:space:]]*$/ { scope = 0; nm = ""; print; next }
        /^(Name|Template): / { if (nm == "") { nm = $2; scope = (nm ~ /^ubiquity\//) } }
        {
            if (scope) {
                gsub(/\$[{]RELEASE[}]/, product)
                gsub(/\$[{]DISTRO[}]/, product)
                gsub(/Linux Mint/, product)
                if (nm == "ubiquity/text/live_installer" && $0 ~ /^Description(-en[^:]*)?: Install$/) {
                    sub(/: Install$/, ": " title)
                }
            }
            print
        }' "${f}" > "${tmp}"; then
        warn "awk failed on $(rel "${f}"); templates not branded"
        rm -f "${tmp}"
        return 0
    fi
    n_in=$(( $(wc -l < "${f}") ))
    n_out=$(( $(wc -l < "${tmp}") ))
    if [ ! -s "${tmp}" ] || [ "${n_in}" -ne "${n_out}" ]; then
        warn "branded templates differ in line count (${n_in} vs ${n_out}); keeping the original"
        rm -f "${tmp}"
        return 0
    fi
    if cmp -s "${f}" "${tmp}"; then
        log "$(rel "${f}"): already branded"
    elif cat "${tmp}" > "${f}"; then
        log "branded $(rel "${f}") (ubiquity/* stanzas: product name, window title \"${SETUP_TITLE}\")"
        EDITS=$(( EDITS + 1 ))
    else
        warn "could not write $(rel "${f}")"
    fi
    rm -f "${tmp}"
    return 0
}

# ---------------------------------------------------------------------------
# 4. GtkBuilder files and casper's eject prompt
# ---------------------------------------------------------------------------
step_ui() {
    local d f n=0
    for d in "${ROOT}/usr/share/ubiquity/gtk" "${ROOT}/usr/lib/ubiquity/gtk"; do
        [ -d "${d}" ] || continue
        for f in "${d}"/*.ui; do
            [ -f "${f}" ] || continue
            grep -q 'Linux Mint' "${f}" || continue
            edit_file "${f}" -e "s/Linux Mint/${PRODUCT}/g"
            n=$(( n + 1 ))
        done
    done
    if [ "${n}" -eq 0 ]; then
        log "no GtkBuilder (.ui) file mentions Linux Mint"
    fi
    for f in "${ROOT}/sbin/casper-stop" "${ROOT}/usr/sbin/casper-stop"; do
        [ -f "${f}" ] || continue
        grep -q 'Linux Mint' "${f}" || continue
        edit_file "${f}" -e "s/Linux Mint/${PRODUCT}/g"
    done
    return 0
}

# ---------------------------------------------------------------------------
# 5. Artwork: welcome-page PNGs and the hicolor icons
# ---------------------------------------------------------------------------
png_size() {
    # png_size FILE → "W H" from the IHDR chunk, empty when it is not a PNG
    local sig
    sig="$(od -An -tx1 -N8 "$1" 2>/dev/null | tr -d ' \n')"
    [ "${sig}" = "89504e470d0a1a0a" ] || return 0
    od -An -tu1 -j16 -N8 "$1" | awk '{ printf "%d %d", $1*16777216+$2*65536+$3*256+$4, $5*16777216+$6*65536+$7*256+$8 }'
}

render_logo_png() {
    # render_logo_png OUT WIDTH HEIGHT OPACITY — the logo centred on a WIDTHxHEIGHT transparent canvas
    local out="$1" w="$2" h="$3" op="$4" tmpd got
    tmpd="$(mktemp -d)" || return 1
    cp -f "${LOGO}" "${tmpd}/lindos-logo.svg" || { rm -rf "${tmpd}"; return 1; }
    cat > "${tmpd}/canvas.svg" <<EOF
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
  <image xlink:href="lindos-logo.svg" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="xMidYMid meet" opacity="${op}"/>
</svg>
EOF
    if ! "${RSVG}" -o "${tmpd}/out.png" "${tmpd}/canvas.svg" >/dev/null 2>&1; then
        rm -rf "${tmpd}"
        return 1
    fi
    got="$(png_size "${tmpd}/out.png")"
    if [ "${got}" != "${w} ${h}" ]; then
        warn "rsvg-convert produced '${got:-not a PNG}' instead of ${w}x${h}; keeping the original image"
        rm -rf "${tmpd}"
        return 1
    fi
    cat "${tmpd}/out.png" > "${out}" || { rm -rf "${tmpd}"; return 1; }
    rm -rf "${tmpd}"
    return 0
}

step_artwork() {
    local pix="${ROOT}/usr/share/ubiquity/pixmaps" spec file def_w def_h op cur w h f icons d n=0
    if [ ! -f "${LOGO}" ]; then
        warn "no $(rel "${LOGO}") (lindos-desktop not installed?): installer artwork left as shipped"
        return 0
    fi
    # name:default-width:default-height:opacity  (defaults only if the existing file is not a readable PNG)
    for spec in "ubuntu_installed.png:234:165:1" "cd_in_tray.png:154:165:0.55" "ubuntu/logo.png:454:90:1"; do
        IFS=: read -r file def_w def_h op <<< "${spec}"
        f="${pix}/${file}"
        [ -f "${f}" ] || continue
        if ! command -v "${RSVG}" >/dev/null 2>&1; then
            warn "${RSVG} not found: $(rel "${f}") keeps its original artwork"
            continue
        fi
        cur="$(png_size "${f}")"
        if [ -n "${cur}" ]; then
            read -r w h <<< "${cur}"
        else
            w="${def_w}"; h="${def_h}"
        fi
        if render_logo_png "${f}" "${w}" "${h}" "${op}"; then
            log "redrew $(rel "${f}") (${w}x${h})"
            EDITS=$(( EDITS + 1 ))
        else
            warn "could not redraw $(rel "${f}"); it keeps its original artwork"
        fi
    done

    icons="${ROOT}/usr/share/icons/hicolor"
    if [ -d "${icons}" ]; then
        for f in "${icons}"/*/apps/ubiquity.svg "${icons}"/*/apps/mintubiquity.svg; do
            [ -f "${f}" ] || continue
            if ! cmp -s "${LOGO}" "${f}"; then
                cat "${LOGO}" > "${f}" && n=$(( n + 1 ))
            fi
        done
        if [ -d "${icons}/scalable/apps" ]; then
            for d in ubiquity mintubiquity; do
                if [ ! -e "${icons}/scalable/apps/${d}.svg" ]; then
                    cp -f "${LOGO}" "${icons}/scalable/apps/${d}.svg" && n=$(( n + 1 ))
                fi
            done
        fi
        if [ "${n}" -gt 0 ]; then
            log "installer icons in hicolor now show the Lindos logo (${n} file(s))"
            EDITS=$(( EDITS + 1 ))
            if [ -z "${ROOT}" ] && have gtk-update-icon-cache; then
                gtk-update-icon-cache -q -t -f "${icons}" >/dev/null 2>&1 || warn "gtk-update-icon-cache failed on hicolor"
            fi
        fi
    else
        log "no hicolor icon theme: launcher icon relies on /usr/share/pixmaps/lindos-logo.svg"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 6. The slideshow shown while files are copied
# ---------------------------------------------------------------------------
step_slideshow() {
    local src="${SRC}/slideshow" dir="${ROOT}/usr/share/ubiquity-slideshow" f
    if [ ! -f "${src}/index.html" ] || [ ! -f "${src}/slides.css" ]; then
        warn "no Lindos slideshow staged in ${SRC}; the installer keeps the base ISO's slideshow"
        return 0
    fi
    if [ -L "${dir}/slides" ]; then
        rm -f "${dir}/slides"
    fi
    mkdir -p "${dir}/slides" || { warn "cannot create $(rel "${dir}")/slides"; return 0; }
    # start from an empty slides/ so no slide of the previous product survives
    find "${dir}/slides" -mindepth 1 -delete 2>/dev/null || warn "could not clear the old slides"
    for f in index.html slides.css; do
        cp -f "${src}/${f}" "${dir}/slides/${f}" || { warn "could not copy ${f}"; return 0; }
    done
    if [ -f "${LOGO}" ]; then
        cp -f "${LOGO}" "${dir}/slides/lindos-logo.svg" || warn "could not copy the logo"
    else
        warn "no logo file: the slideshow shows without its mark"
    fi
    # keep the base slideshow's window size when it has one (never change the installer layout)
    if [ ! -f "${dir}/slideshow.conf" ]; then
        printf '[Slideshow]\nwidth:752\nheight:442\n' > "${dir}/slideshow.conf"
    fi
    find "${dir}" -type d -exec chmod 0755 {} + 2>/dev/null || true
    find "${dir}" -type f -exec chmod 0644 {} + 2>/dev/null || true
    log "installed the Lindos slideshow in $(rel "${dir}")/slides"
    EDITS=$(( EDITS + 1 ))
    return 0
}

# ---------------------------------------------------------------------------
# 7. Audit: tell the build log what still says Mint inside the installer
# ---------------------------------------------------------------------------
step_audit() {
    local paths=() p left
    for p in usr/share/ubiquity usr/lib/ubiquity usr/share/ubiquity-slideshow usr/share/applications/ubiquity.desktop; do
        [ -e "${ROOT}/${p}" ] && paths+=("${ROOT}/${p}")
    done
    [ "${#paths[@]}" -gt 0 ] || return 0
    left="$(grep -rIl -e 'Linux Mint' -e 'LinuxMint' "${paths[@]}" 2>/dev/null | head -n 20 || true)"
    if [ -n "${left}" ]; then
        warn "these installer files still mention Linux Mint (Python code / catalogues — not rewritten by design):"
        printf '%s\n' "${left}" | while IFS= read -r p; do
            log "    $(rel "${p}")"
        done
    else
        log "no text file in the installer directories mentions Linux Mint any more"
    fi
    return 0
}

step_skin      || warn "skin step failed (continuing)"
step_launcher  || warn "launcher step failed (continuing)"
step_templates || warn "templates step failed (continuing)"
step_ui        || warn "ui step failed (continuing)"
step_artwork   || warn "artwork step failed (continuing)"
step_slideshow || warn "slideshow step failed (continuing)"
step_audit     || warn "audit step failed (continuing)"

log "installer branding done (${EDITS} change(s))"
hook_end
