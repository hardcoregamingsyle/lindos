#!/bin/bash
# ============================================================================
#  build/fetch-assets.sh — fetch the third-party look-and-feel assets of Lindos
#  (SPEC §1, §2, §5, §8) on the BUILD HOST and stage them under out/assets/.
#
#  Fetched (pinned refs, SHA256 verified when known, else recorded + warned):
#    * vinceliuice/Fluent-gtk-theme   → GTK 2/3/4 + xfwm4 theme, built as
#                                       Lindos-Dark / Lindos-Light (-n Lindos --tweaks round)
#    * vinceliuice/Fluent-icon-theme  → icon theme "Lindos" / "Lindos-dark" / "Lindos-light"
#                                       and its cursors/ dir → Fluent-cursors, Fluent-dark-cursors
#    * microsoft/Selawik (release zip)→ /usr/share/fonts/truetype/selawik   (OFL-1.1)
#    * rsms/inter (release zip)       → /usr/share/fonts/truetype/inter     (OFL-1.1)
#
#  Nothing here touches the running system.  The result is a self-contained
#  directory (out/assets/) with a generated install-into-chroot.sh that
#  build/chroot/40-theme.sh runs INSIDE the chroot (assets staged at
#  /tmp/lindos/assets).  The lindos-desktop .deb never downloads anything.
#
#  Usage:  build/fetch-assets.sh [--offline] [--clean] [--out DIR] [--no-fonts]
#                                [--no-themes] [--verify-only]
#          (--out DIR = --assets-dir DIR; default out/assets — SPEC §8)
#  Env overrides (defaults below): FLUENT_GTK_REPO/REF, FLUENT_ICON_REPO/REF,
#     SELAWIK_URL/SHA256, INTER_URL/SHA256, ASSETS_DIR, GIT_DEPTH, LINDOS_ASSETS_LOCK
#  Requires: git, curl or wget, unzip, sha256sum, tar.  Exit 0 ok, 1 error, 2 usage.
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${BUILD_DIR}/.." && pwd -P)"

# --- logging (use build/lib/common.sh when present, minimal fallbacks otherwise) ---
if [ -f "${BUILD_DIR}/lib/common.sh" ]; then
    # shellcheck source=build/lib/common.sh
    . "${BUILD_DIR}/lib/common.sh"
    lindos_load_config 2>/dev/null || true
else
    log()  { printf '[fetch-assets] %s\n' "$*" >&2; }
    warn() { printf '[fetch-assets] WARNING: %s\n' "$*" >&2; }
    die()  {
        local code=1
        if [ "$#" -ge 2 ] && [[ "${*: -1}" =~ ^[0-9]{1,3}$ ]]; then
            code="${*: -1}"
            set -- "${@:1:$#-1}"
        fi
        printf '[fetch-assets] ERROR: %s\n' "$*" >&2
        exit "${code}"
    }
    have() { command -v "$1" >/dev/null 2>&1; }
    ok()   { log "OK $*"; }
fi
LOG_PREFIX="fetch-assets"

# --- pins ------------------------------------------------------------------------
# Refs are tags/branches/commits of the upstream repos.  vinceliuice tags releases
# by date; "master" is the fallback so a renamed tag can never break the build —
# the commit actually checked out is recorded in assets.lock and can be replayed
# with LINDOS_ASSETS_LOCK=<file>.
: "${FLUENT_GTK_REPO:=https://github.com/vinceliuice/Fluent-gtk-theme.git}"
: "${FLUENT_GTK_REF:=master}"
: "${FLUENT_ICON_REPO:=https://github.com/vinceliuice/Fluent-icon-theme.git}"
: "${FLUENT_ICON_REF:=master}"
: "${SELAWIK_URL:=https://github.com/microsoft/Selawik/releases/download/1.01/Selawik_Release.zip}"
: "${SELAWIK_SHA256:=}"          # fill in for reproducible release builds
: "${INTER_URL:=https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip}"
: "${INTER_URL_FALLBACK:=https://github.com/rsms/inter/releases/download/v4.0/Inter-4.0.zip}"
: "${INTER_SHA256:=}"
: "${GIT_DEPTH:=1}"
: "${ASSETS_DIR:=${REPO_ROOT}/out/assets}"
: "${LINDOS_ASSETS_LOCK:=}"
# 'make assets' as a user followed by 'sudo make iso' (root) would trip git's
# "dubious ownership" check on the cached checkouts; the clones are ours.
export GIT_CONFIG_PARAMETERS="${GIT_CONFIG_PARAMETERS:+${GIT_CONFIG_PARAMETERS} }'safe.directory=*'"
export GIT_TERMINAL_PROMPT=0

OFFLINE=0
CLEAN=0
DO_FONTS=1
DO_THEMES=1
VERIFY_ONLY=0

usage() {
    sed -n '3,24p' "${BASH_SOURCE[0]}" | sed -e 's/^#  \{0,1\}//' -e 's/^#$//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --offline) OFFLINE=1; shift ;;
        --clean) CLEAN=1; shift ;;
        --no-fonts) DO_FONTS=0; shift ;;
        --no-themes) DO_THEMES=0; shift ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        --assets-dir|--out) [ $# -ge 2 ] || die "$1 needs an argument" 2; ASSETS_DIR="$2"; shift 2 ;;
        --assets-dir=*|--out=*) ASSETS_DIR="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "unknown argument: $1" 2 ;;
    esac
done

case "${ASSETS_DIR}" in
    /*) ;;
    *) ASSETS_DIR="${REPO_ROOT}/${ASSETS_DIR}" ;;
esac
THEMES_DIR="${ASSETS_DIR}/themes"
FONTS_DIR="${ASSETS_DIR}/fonts"
DL_DIR="${ASSETS_DIR}/downloads"
SUMS_FILE="${ASSETS_DIR}/SHA256SUMS"
LOCK_FILE="${ASSETS_DIR}/assets.lock"
MANIFEST="${ASSETS_DIR}/MANIFEST.txt"
LICENSES="${ASSETS_DIR}/THIRD_PARTY-assets.md"
INSTALLER="${ASSETS_DIR}/install-into-chroot.sh"

# --- helpers ---------------------------------------------------------------------
sha256_of() {
    if have sha256sum; then
        sha256sum -- "$1" | awk '{print $1}'
    elif have shasum; then
        shasum -a 256 -- "$1" | awk '{print $1}'
    else
        die "need sha256sum or shasum"
    fi
}

# record_sum FILE — append "<sha>  <relative path>" to SHA256SUMS (dedup by path)
record_sum() {
    local f="$1" rel sum
    rel="${f#"${ASSETS_DIR}"/}"
    sum="$(sha256_of "${f}")"
    if [ -f "${SUMS_FILE}" ]; then
        grep -v "  ${rel}\$" "${SUMS_FILE}" >"${SUMS_FILE}.tmp" || true
        mv -f "${SUMS_FILE}.tmp" "${SUMS_FILE}"
    fi
    printf '%s  %s\n' "${sum}" "${rel}" >>"${SUMS_FILE}"
    printf '%s\n' "${sum}"
}

lock_set() {   # lock_set KEY VALUE
    local key="$1" value="$2"
    touch "${LOCK_FILE}"
    grep -v "^${key}=" "${LOCK_FILE}" >"${LOCK_FILE}.tmp" || true
    printf '%s=%s\n' "${key}" "${value}" >>"${LOCK_FILE}.tmp"
    sort -o "${LOCK_FILE}" "${LOCK_FILE}.tmp"
    rm -f "${LOCK_FILE}.tmp"
}

lock_get() {   # lock_get KEY FILE
    [ -f "$2" ] || return 0
    sed -n "s/^$1=//p" "$2" | head -n 1
}

fetch_url() {   # fetch_url URL DEST
    local url="$1" dest="$2"
    if have curl; then
        curl -fL --retry 3 --connect-timeout 30 -o "${dest}.part" "${url}"
    elif have wget; then
        wget -q -O "${dest}.part" "${url}"
    else
        die "need curl or wget"
    fi
    mv -f "${dest}.part" "${dest}"
}

# download_verified NAME URL SHA(optional) [FALLBACK_URL] → path
download_verified() {
    local name="$1" url="$2" want="$3" fallback="${4:-}" got
    local dest="${DL_DIR}/${name}"
    mkdir -p "${DL_DIR}"
    if [ -f "${dest}" ]; then
        got="$(sha256_of "${dest}")"
        if [ -n "${want}" ] && [ "${got}" != "${want}" ]; then
            warn "${name}: cached file hash mismatch, re-downloading"
            rm -f "${dest}"
        fi
    fi
    if [ ! -f "${dest}" ]; then
        [ "${OFFLINE}" -eq 0 ] || die "${name} not cached and --offline given"
        log "downloading ${url}"
        if ! fetch_url "${url}" "${dest}"; then
            rm -f "${dest}.part"
            if [ -n "${fallback}" ]; then
                warn "${name}: download failed, trying fallback ${fallback}"
                fetch_url "${fallback}" "${dest}" || die "${name}: download failed (${fallback})"
                url="${fallback}"
            else
                die "${name}: download failed (${url})"
            fi
        fi
    fi
    got="$(sha256_of "${dest}")"
    if [ -n "${want}" ]; then
        [ "${got}" = "${want}" ] || die "${name}: SHA256 mismatch: expected ${want}, got ${got}"
        log "${name}: SHA256 verified"
    else
        warn "${name}: no pinned SHA256 — observed ${got} (recorded in SHA256SUMS; pin it in config.env for releases)"
    fi
    record_sum "${dest}" >/dev/null
    lock_set "$(printf '%s' "${name}" | tr 'a-z.-' 'A-Z__')_URL" "${url}"
    lock_set "$(printf '%s' "${name}" | tr 'a-z.-' 'A-Z__')_SHA256" "${got}"
    printf '%s\n' "${dest}"
}

# clone_pinned NAME REPO REF DEST — shallow clone/update at REF (tag/branch/commit),
# honouring a commit recorded in LINDOS_ASSETS_LOCK; records the resolved commit.
clone_pinned() {
    local name="$1" repo="$2" ref="$3" dest="$4" locked commit
    locked="$(lock_get "${name}_COMMIT" "${LINDOS_ASSETS_LOCK}")"
    if [ -d "${dest}/.git" ]; then
        if [ "${OFFLINE}" -eq 1 ]; then
            log "${name}: using cached checkout (offline)"
        else
            log "${name}: updating ${dest}"
            git -C "${dest}" fetch --depth "${GIT_DEPTH}" origin "${locked:-${ref}}" 2>/dev/null \
                || git -C "${dest}" fetch origin 2>/dev/null \
                || warn "${name}: fetch failed, using cached checkout"
            git -C "${dest}" checkout -q --detach "${locked:-FETCH_HEAD}" 2>/dev/null \
                || git -C "${dest}" checkout -q --detach "${ref}" 2>/dev/null \
                || warn "${name}: checkout of ${locked:-${ref}} failed, keeping current"
        fi
    else
        [ "${OFFLINE}" -eq 0 ] || die "${name}: not cached and --offline given"
        mkdir -p "$(dirname "${dest}")"
        if [ -n "${locked}" ]; then
            log "${name}: cloning ${repo} @ ${locked} (from lock)"
            git init -q "${dest}"
            git -C "${dest}" remote add origin "${repo}"
            if git -C "${dest}" fetch --depth "${GIT_DEPTH}" origin "${locked}"; then
                git -C "${dest}" checkout -q --detach FETCH_HEAD
            else
                warn "${name}: locked commit ${locked} not fetchable, falling back to ${ref}"
                git -C "${dest}" fetch --depth "${GIT_DEPTH}" origin "${ref}" || git -C "${dest}" fetch origin
                git -C "${dest}" checkout -q --detach FETCH_HEAD
            fi
        else
            log "${name}: cloning ${repo} @ ${ref}"
            if ! git clone -q --depth "${GIT_DEPTH}" --branch "${ref}" "${repo}" "${dest}" 2>/dev/null; then
                warn "${name}: ref '${ref}' not found as tag/branch, trying default branch + commit"
                git clone -q --depth "${GIT_DEPTH}" "${repo}" "${dest}"
                if ! git -C "${dest}" checkout -q --detach "${ref}" 2>/dev/null; then
                    git -C "${dest}" fetch --depth "${GIT_DEPTH}" origin "${ref}" 2>/dev/null \
                        && git -C "${dest}" checkout -q --detach FETCH_HEAD \
                        || warn "${name}: using default branch head instead of '${ref}'"
                fi
            fi
        fi
    fi
    commit="$(git -C "${dest}" rev-parse HEAD 2>/dev/null || echo unknown)"
    lock_set "${name}_REPO" "${repo}"
    lock_set "${name}_REF" "${ref}"
    lock_set "${name}_COMMIT" "${commit}"
    log "${name}: ${commit}"
}

# extract_fonts ZIP DEST — pull every .ttf/.otf out of a release zip (flat)
extract_fonts() {
    local zip="$1" dest="$2" tmp
    tmp="$(mktemp -d)"
    if have unzip; then
        unzip -q -o "${zip}" -d "${tmp}"
    elif have python3; then
        python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "${zip}" "${tmp}"
    else
        die "need unzip or python3 to extract ${zip}"
    fi
    mkdir -p "${dest}"
    # Inter ships static TTFs under extras/ttf/ (variable fonts at the top level); Selawik
    # ships selawk*.ttf.  Prefer static instances; fall back to whatever ttf/otf exists.
    local found=0 f
    while IFS= read -r -d '' f; do
        cp -f "${f}" "${dest}/"
        found=$((found + 1))
    done < <(find "${tmp}" -type f \( -iname '*.ttf' -o -iname '*.otf' \) ! -iname '*Variable*' ! -path '*/web/*' ! -path '*/__MACOSX/*' -print0)
    if [ "${found}" -eq 0 ]; then
        while IFS= read -r -d '' f; do
            cp -f "${f}" "${dest}/"
            found=$((found + 1))
        done < <(find "${tmp}" -type f \( -iname '*.ttf' -o -iname '*.otf' \) -print0)
    fi
    # licence file next to the fonts
    while IFS= read -r -d '' f; do
        cp -f "${f}" "${dest}/$(basename "${f}")"
    done < <(find "${tmp}" -maxdepth 3 -type f \( -iname 'LICENSE*' -o -iname 'OFL*' -o -iname 'COPYING*' \) -print0)
    rm -rf "${tmp}"
    [ "${found}" -gt 0 ] || die "no font files found in ${zip}"
    log "extracted ${found} font file(s) → ${dest}"
}

# --- generated installer (runs inside the chroot as root) ------------------------
write_installer() {
    cat >"${INSTALLER}" <<'EOS'
#!/bin/bash
# install-into-chroot.sh — GENERATED by build/fetch-assets.sh; runs INSIDE the Lindos
# chroot as root (build/chroot/40-theme.sh) with the assets directory as $1
# (default: the directory containing this script, normally /tmp/lindos/assets).
#
# Installs (SPEC §2):
#   * Fluent-gtk-theme  → /usr/share/themes/Lindos-Dark, Lindos-Light  (-n Lindos -t default
#                         -c dark light --tweaks round; needs sassc for --tweaks, apt-installed
#                         when missing and online, else installed without tweaks)
#   * xfwm4 themes      → ensures /usr/share/themes/Lindos-Dark/xfwm4 + Lindos-Light/xfwm4
#                         exist (Fluent's xfwm4 assets, renamed/copied when needed)
#   * Fluent-icon-theme → /usr/share/icons/Lindos, Lindos-dark, Lindos-light (-n Lindos)
#   * cursors           → /usr/share/icons/Fluent-cursors, Fluent-dark-cursors
#   * fonts             → /usr/share/fonts/truetype/selawik, /usr/share/fonts/truetype/inter
#   * caches            → fc-cache -f, gtk-update-icon-cache
# Idempotent; never downloads theme sources; exit 0 when the mandatory parts succeeded.
set -Eeuo pipefail

ASSETS="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)}"
THEMES_DEST="${THEMES_DEST:-/usr/share/themes}"
ICONS_DEST="${ICONS_DEST:-/usr/share/icons}"
FONTS_DEST="${FONTS_DEST:-/usr/share/fonts/truetype}"
THEME_NAME="${THEME_NAME:-Lindos}"
FAILED=0

log()  { printf '[install-assets] %s\n' "$*" >&2; }
warn() { printf '[install-assets] WARNING: %s\n' "$*" >&2; }
die()  { printf '[install-assets] ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ -d "${ASSETS}" ] || die "assets directory not found: ${ASSETS}"
[ "$(id -u)" -eq 0 ] || warn "not running as root; installs may fail"

ensure_sassc() {
    have sassc && return 0
    if have apt-get; then
        log "installing sassc (needed for Fluent --tweaks)"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q sassc >/dev/null 2>&1 && return 0
        warn "could not install sassc (offline?)"
    fi
    return 1
}

install_gtk_theme() {
    local src="${ASSETS}/themes/Fluent-gtk-theme"
    if [ ! -f "${src}/install.sh" ]; then
        warn "Fluent-gtk-theme sources missing (${src}); GTK theme NOT installed"
        FAILED=1
        return 0
    fi
    local tweaks=(--tweaks round)
    if ! ensure_sassc; then
        warn "sassc unavailable: installing Fluent without --tweaks round"
        tweaks=()
    fi
    log "installing Fluent-gtk-theme as ${THEME_NAME}-Dark / ${THEME_NAME}-Light"
    mkdir -p "${THEMES_DEST}"
    local ok=0
    # 1st try: exact SPEC flags; 2nd: without '-t default' (older install.sh); 3rd: no tweaks
    if bash "${src}/install.sh" -d "${THEMES_DEST}" -n "${THEME_NAME}" -t default -c dark light ${tweaks[@]+"${tweaks[@]}"} >/tmp/fluent-gtk.log 2>&1; then
        ok=1
    elif bash "${src}/install.sh" -d "${THEMES_DEST}" -n "${THEME_NAME}" -c dark light ${tweaks[@]+"${tweaks[@]}"} >>/tmp/fluent-gtk.log 2>&1; then
        ok=1
    elif bash "${src}/install.sh" -d "${THEMES_DEST}" -n "${THEME_NAME}" -c dark light >>/tmp/fluent-gtk.log 2>&1; then
        warn "installed Fluent without tweaks (see /tmp/fluent-gtk.log)"
        ok=1
    fi
    if [ "${ok}" -eq 0 ]; then
        warn "Fluent-gtk-theme install.sh failed (see /tmp/fluent-gtk.log)"
        FAILED=1
        return 0
    fi
    # Fluent names variants "<name>-Dark"/"<name>-Light", sometimes with tweak/size infixes
    # ("<name>-round-Dark", "<name>-Dark-compact"): make sure the SPEC names exist.
    local variant want dir
    for variant in Dark Light; do
        want="${THEME_NAME}-${variant}"
        if [ -d "${THEMES_DEST}/${want}" ]; then
            continue
        fi
        dir="$(find "${THEMES_DEST}" -maxdepth 1 -type d -iname "${THEME_NAME}*${variant}*" 2>/dev/null \
               | awk '{ print length($0) "\t" $0 }' | sort -n | head -n 1 | cut -f2- || true)"
        if [ -n "${dir}" ]; then
            cp -a "${dir}" "${THEMES_DEST}/${want}"
            log "aliased $(basename "${dir}") → ${want}"
        else
            warn "theme ${want} not produced by Fluent install.sh"
            FAILED=1
        fi
    done
}

install_xfwm_themes() {
    # SPEC: xfwm4 themes Lindos-Dark / Lindos-Light = Fluent xfwm themes renamed.  Fluent's
    # install.sh already drops an xfwm4/ dir into each theme; make sure both exist, copying
    # from any Fluent/Lindos variant that has one, else fall back to Mint's dark xfwm theme.
    local variant target cand src=""
    for variant in Dark Light; do
        target="${THEMES_DEST}/${THEME_NAME}-${variant}/xfwm4"
        if [ -f "${target}/themerc" ]; then
            log "xfwm4 theme ${THEME_NAME}-${variant}: ok"
            continue
        fi
        src=""
        for cand in "${THEMES_DEST}/${THEME_NAME}-${variant}"*/xfwm4 \
                    "${THEMES_DEST}/${THEME_NAME}"*"${variant}"*/xfwm4 \
                    "${THEMES_DEST}/Fluent-${variant}"*/xfwm4 \
                    "${THEMES_DEST}/Fluent"*"${variant}"*/xfwm4 \
                    "${ASSETS}/themes/Fluent-gtk-theme/src/assets/xfwm4"*; do
            if [ -f "${cand}/themerc" ] || [ -f "${cand}/themerc-${variant}" ] || [ -f "${cand}/themerc-${variant,,}" ]; then
                src="${cand}"
                break
            fi
        done
        if [ -z "${src}" ]; then
            if [ "${variant}" = "Dark" ] && [ -d "${THEMES_DEST}/Mint-Y-Dark/xfwm4" ]; then
                src="${THEMES_DEST}/Mint-Y-Dark/xfwm4"
            elif [ -d "${THEMES_DEST}/Mint-Y/xfwm4" ]; then
                src="${THEMES_DEST}/Mint-Y/xfwm4"
            elif [ -d "${THEMES_DEST}/Default/xfwm4" ]; then
                src="${THEMES_DEST}/Default/xfwm4"
            fi
            [ -n "${src}" ] && warn "no Fluent xfwm4 assets for ${variant}; using $(basename "$(dirname "${src}")") as ${THEME_NAME}-${variant}/xfwm4"
        fi
        if [ -z "${src}" ]; then
            warn "cannot provide xfwm4 theme ${THEME_NAME}-${variant}"
            FAILED=1
            continue
        fi
        mkdir -p "${target}"
        cp -a "${src}/." "${target}/"
        # Fluent's raw src assets keep per-variant files as themerc-<variant> + assets-<variant>/
        local sub
        for sub in "${target}/assets-${variant}" "${target}/assets${variant}" "${target}/assets-${variant,,}"; do
            if [ -d "${sub}" ]; then
                cp -a "${sub}/." "${target}/"
            fi
        done
        if [ ! -f "${target}/themerc" ]; then
            if [ -f "${target}/themerc-${variant}" ]; then cp -f "${target}/themerc-${variant}" "${target}/themerc"; fi
            if [ -f "${target}/themerc-${variant,,}" ]; then cp -f "${target}/themerc-${variant,,}" "${target}/themerc"; fi
        fi
        if [ ! -f "${target}/themerc" ]; then
            warn "xfwm4 theme ${THEME_NAME}-${variant}: no themerc found in ${src}"
            FAILED=1
        fi
        log "xfwm4 theme ${THEME_NAME}-${variant}: copied from ${src}"
    done
}

install_icon_theme() {
    local src="${ASSETS}/themes/Fluent-icon-theme"
    if [ ! -f "${src}/install.sh" ]; then
        warn "Fluent-icon-theme sources missing (${src}); icon theme NOT installed"
        FAILED=1
        return 0
    fi
    log "installing Fluent-icon-theme as ${THEME_NAME} / ${THEME_NAME}-dark / ${THEME_NAME}-light"
    mkdir -p "${ICONS_DEST}"
    if ! bash "${src}/install.sh" -d "${ICONS_DEST}" -n "${THEME_NAME}" >/tmp/fluent-icons.log 2>&1; then
        warn "Fluent-icon-theme install.sh failed (see /tmp/fluent-icons.log)"
        FAILED=1
        return 0
    fi
    local want dir
    for want in "${THEME_NAME}" "${THEME_NAME}-dark"; do
        [ -d "${ICONS_DEST}/${want}" ] && continue
        dir="$(find "${ICONS_DEST}" -maxdepth 1 -type d -iname "${want}" | head -n 1 || true)"
        if [ -n "${dir}" ]; then
            cp -a "${dir}" "${ICONS_DEST}/${want}"
        else
            warn "icon theme ${want} not produced by Fluent install.sh"
        fi
    done
    # keep our own hicolor icons (lindos-start …) findable from Lindos: hicolor is always
    # in the inheritance chain, nothing to do — just refresh caches
    if have gtk-update-icon-cache; then
        local d
        for d in "${ICONS_DEST}/${THEME_NAME}" "${ICONS_DEST}/${THEME_NAME}-dark" "${ICONS_DEST}/${THEME_NAME}-light" "${ICONS_DEST}/hicolor"; do
            [ -f "${d}/index.theme" ] && gtk-update-icon-cache -q -t -f "${d}" 2>/dev/null || true
        done
    fi
}

install_cursors() {
    local base="${ASSETS}/themes/Fluent-icon-theme/cursors"
    if [ ! -d "${base}" ]; then
        base="${ASSETS}/themes/Fluent-cursors"
    fi
    local pair src dest ok=0
    for pair in "dist:Fluent-cursors" "dist-dark:Fluent-dark-cursors"; do
        src="${base}/${pair%%:*}"
        dest="${ICONS_DEST}/${pair##*:}"
        if [ -d "${src}/cursors" ]; then
            rm -rf "${dest}"
            mkdir -p "${dest}"
            cp -a "${src}/." "${dest}/"
            log "cursor theme ${pair##*:} installed"
            ok=$((ok + 1))
        fi
    done
    if [ "${ok}" -eq 0 ] && [ -f "${base}/install.sh" ]; then
        # prebuilt dist/ missing: try the upstream installer (needs xcursorgen for build.sh)
        if bash "${base}/install.sh" >/tmp/fluent-cursors.log 2>&1; then
            ok=1
        fi
    fi
    if [ "${ok}" -eq 0 ]; then
        warn "Fluent cursors not installed (missing ${base}/dist*); xsettings falls back to the default cursor"
    fi
}

install_fonts() {
    local name src dest n
    for name in selawik inter; do
        src="${ASSETS}/fonts/${name}"
        dest="${FONTS_DEST}/${name}"
        if [ ! -d "${src}" ]; then
            warn "fonts/${name} missing in assets; skipping (Selawik/Inter fall back to Noto Sans)"
            continue
        fi
        mkdir -p "${dest}"
        n=0
        while IFS= read -r -d '' f; do
            install -m 0644 "${f}" "${dest}/"
            n=$((n + 1))
        done < <(find "${src}" -maxdepth 1 -type f \( -iname '*.ttf' -o -iname '*.otf' \) -print0)
        while IFS= read -r -d '' f; do
            install -m 0644 "${f}" "${dest}/"
        done < <(find "${src}" -maxdepth 1 -type f \( -iname 'LICENSE*' -o -iname 'OFL*' \) -print0)
        log "installed ${n} ${name} font file(s) → ${dest}"
    done
    if have fc-cache; then
        fc-cache -f >/dev/null 2>&1 || warn "fc-cache failed"
    fi
}

install_gtk_theme
install_xfwm_themes
install_icon_theme
install_cursors
install_fonts

if [ "${FAILED}" -ne 0 ]; then
    warn "finished with problems (see messages above)"
    exit 1
fi
log "all assets installed"
EOS
    chmod 0755 "${INSTALLER}"
    log "wrote ${INSTALLER}"
}

write_licenses() {
    cat >"${LICENSES}" <<'EOS'
# Third-party assets staged by build/fetch-assets.sh

| Asset | Upstream | Licence | Used as |
|---|---|---|---|
| Fluent-gtk-theme | https://github.com/vinceliuice/Fluent-gtk-theme | GPL-3.0 | GTK 2/3/4 + xfwm4 themes `Lindos-Dark` / `Lindos-Light` |
| Fluent-icon-theme (+ cursors) | https://github.com/vinceliuice/Fluent-icon-theme | GPL-3.0 | icon theme `Lindos`, `Lindos-dark`; cursors `Fluent-cursors`, `Fluent-dark-cursors` |
| Selawik | https://github.com/microsoft/Selawik | SIL OFL 1.1 | default UI font, alias target for "Segoe UI" |
| Inter | https://github.com/rsms/inter | SIL OFL 1.1 | fallback UI font |

Exact commits / URLs / SHA256 of what was fetched: see `assets.lock` and `SHA256SUMS`
in this directory.  Lindos' own artwork (wallpapers, logo, icons) is GPL-3.0-or-later.
Edge and Chrome are NOT fetched (their licences forbid redistribution; the installer downloads
them from the vendors' apt repositories).
EOS
}

write_manifest() {
    {
        printf 'Lindos assets manifest — generated %s by build/fetch-assets.sh\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'ASSETS_DIR=%s\n\n' "${ASSETS_DIR}"
        if [ -f "${LOCK_FILE}" ]; then cat "${LOCK_FILE}"; fi
        printf '\nFiles:\n'
        (cd "${ASSETS_DIR}" && find . -maxdepth 2 -mindepth 1 \( -type d -o -type f \) | sort)
    } >"${MANIFEST}"
}

verify_sums() {
    [ -f "${SUMS_FILE}" ] || { warn "no SHA256SUMS to verify"; return 0; }
    local bad=0 sum rel got
    while read -r sum rel; do
        [ -n "${rel}" ] || continue
        if [ ! -f "${ASSETS_DIR}/${rel}" ]; then
            warn "missing: ${rel}"
            bad=1
            continue
        fi
        got="$(sha256_of "${ASSETS_DIR}/${rel}")"
        if [ "${got}" != "${sum}" ]; then
            warn "hash mismatch: ${rel}"
            bad=1
        fi
    done <"${SUMS_FILE}"
    [ "${bad}" -eq 0 ] && log "SHA256SUMS verified"
    return "${bad}"
}

# --- main ------------------------------------------------------------------------
main() {
    if [ "${VERIFY_ONLY}" -eq 1 ]; then
        verify_sums
        return $?
    fi
    if [ "${CLEAN}" -eq 1 ]; then
        log "removing ${ASSETS_DIR}"
        rm -rf "${ASSETS_DIR}"
    fi
    mkdir -p "${ASSETS_DIR}" "${THEMES_DIR}" "${FONTS_DIR}" "${DL_DIR}"
    have git || die "git is required"
    have tar || die "tar is required"
    if [ "${OFFLINE}" -eq 0 ] && ! have curl && ! have wget; then
        die "curl or wget is required"
    fi
    if [ -n "${LINDOS_ASSETS_LOCK}" ] && [ ! -f "${LINDOS_ASSETS_LOCK}" ]; then
        die "LINDOS_ASSETS_LOCK=${LINDOS_ASSETS_LOCK} not found"
    fi

    if [ "${DO_THEMES}" -eq 1 ]; then
        clone_pinned FLUENT_GTK "${FLUENT_GTK_REPO}" "${FLUENT_GTK_REF}" "${THEMES_DIR}/Fluent-gtk-theme"
        clone_pinned FLUENT_ICON "${FLUENT_ICON_REPO}" "${FLUENT_ICON_REF}" "${THEMES_DIR}/Fluent-icon-theme"
        [ -f "${THEMES_DIR}/Fluent-gtk-theme/install.sh" ] || die "Fluent-gtk-theme/install.sh missing after clone"
        [ -f "${THEMES_DIR}/Fluent-icon-theme/install.sh" ] || die "Fluent-icon-theme/install.sh missing after clone"
        if [ ! -d "${THEMES_DIR}/Fluent-icon-theme/cursors/dist" ]; then
            warn "Fluent-icon-theme/cursors/dist not present in this checkout; cursors will need build.sh (xcursorgen) in the chroot"
        fi
        # sanity: the flags we rely on
        if ! grep -q -- '--tweaks' "${THEMES_DIR}/Fluent-gtk-theme/install.sh"; then
            warn "Fluent-gtk-theme install.sh has no --tweaks option in this ref; installer falls back automatically"
        fi
        if ! grep -q -- '-n|--name' "${THEMES_DIR}/Fluent-icon-theme/install.sh"; then
            warn "Fluent-icon-theme install.sh has no -n/--name option in this ref"
        fi
        # record checksums of the two install scripts (change detection between builds)
        record_sum "${THEMES_DIR}/Fluent-gtk-theme/install.sh" >/dev/null
        record_sum "${THEMES_DIR}/Fluent-icon-theme/install.sh" >/dev/null
    else
        log "themes skipped (--no-themes)"
    fi

    if [ "${DO_FONTS}" -eq 1 ]; then
        local zip
        zip="$(download_verified selawik.zip "${SELAWIK_URL}" "${SELAWIK_SHA256}")"
        rm -rf "${FONTS_DIR}/selawik"
        extract_fonts "${zip}" "${FONTS_DIR}/selawik"
        zip="$(download_verified inter.zip "${INTER_URL}" "${INTER_SHA256}" "${INTER_URL_FALLBACK}")"
        rm -rf "${FONTS_DIR}/inter"
        extract_fonts "${zip}" "${FONTS_DIR}/inter"
        local f
        while IFS= read -r -d '' f; do
            record_sum "${f}" >/dev/null
        done < <(find "${FONTS_DIR}" -type f \( -iname '*.ttf' -o -iname '*.otf' \) -print0)
    else
        log "fonts skipped (--no-fonts)"
    fi

    write_installer
    write_licenses
    lock_set GENERATED "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    write_manifest
    log "assets ready in ${ASSETS_DIR} (installer: ${INSTALLER})"
}

main "$@"
