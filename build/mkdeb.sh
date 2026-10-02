#!/bin/bash
# ============================================================================
#  build/mkdeb.sh — build packages/<name> into out/debs/<name>_<ver>_<arch>.deb
#
#  Usage: build/mkdeb.sh [options] [name…|all]
#     name…        package directories under packages/ (default: all)
#     --out DIR    output directory (default: ${DEBS_DIR:-out/debs})
#     --version V  release version to stamp into every package (also LINDOS_PKG_VERSION);
#                  default: the VERSION file at the repository root
#     --lintian    run lintian on each result if lintian is installed
#     --keep       keep the staging directory (out/work/deb-staging/<name>)
#     -h, --help
#
#  Versions move in lock-step (docs/RELEASING.md): every lindos-* package of a release carries
#  the same version, and lindos-meta pins its parts with exact '(= V)' dependencies. The version
#  is stamped in the STAGING copy only - the Version fields, every exact 'lindos-x (= old)' pin
#  in a relationship field, and lindos.__version__ - so the source tree (and its tests) keep the
#  development version 1.0.0. LINDOS_PACKAGES_DIR points at another packages/ tree (the e2e job).
#
#  Per SPEC §1.1:
#     * staging copy of root/ + DEBIAN/
#     * perms: dirs 755, files 644; executables 755 for usr/bin/*, usr/sbin/*,
#       usr/games/*, usr/libexec/**, *.sh, DEBIAN/{preinst,postinst,prerm,postrm,config}
#     * CRLF stripped defensively from DEBIAN/* text files, every file with a
#       '#!' shebang and common text extensions (scripts run on Linux)
#     * DEBIAN/control validated (Package, Version, Architecture, Maintainer,
#       Description); maintainer scripts syntax-checked (sh -n / bash -n)
#     * DEBIAN/md5sums generated
#     * dpkg-deb --build --root-owner-group, then 'dpkg-deb -I' summary
#  Exit codes: 0 ok · 1 build error · 2 usage
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="mkdeb"

ROOT="$(repo_root)"
lindos_load_config

PACKAGES_DIR="${LINDOS_PACKAGES_DIR:-${ROOT}/packages}"
OUT="${DEBS_DIR:-out/debs}"
STAGING_BASE="${WORK_DIR:-out/work}/deb-staging"
RUN_LINTIAN=0
KEEP_STAGING=0
PKG_VERSION="${LINDOS_PKG_VERSION:-}"
NAMES=()

usage() {
    sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --out) [ $# -ge 2 ] || die "--out needs an argument" 2; OUT="$2"; shift 2 ;;
        --out=*) OUT="${1#*=}"; shift ;;
        --version) [ $# -ge 2 ] || die "--version needs an argument" 2; PKG_VERSION="$2"; shift 2 ;;
        --version=*) PKG_VERSION="${1#*=}"; shift ;;
        --lintian) RUN_LINTIAN=1; shift ;;
        --keep) KEEP_STAGING=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; while [ $# -gt 0 ]; do NAMES+=("$1"); shift; done ;;
        -*) die "unknown option: $1 (see --help)" 2 ;;
        *) NAMES+=("$1"); shift ;;
    esac
done

APT_HINT_PACKAGES="dpkg-dev" require_cmd dpkg-deb find sed md5sum

case "${OUT}" in /*) ;; *) OUT="${ROOT}/${OUT}" ;; esac
case "${STAGING_BASE}" in /*) ;; *) STAGING_BASE="${ROOT}/${STAGING_BASE}" ;; esac
ensure_dir "${OUT}" "${STAGING_BASE}"

# One release version for every package: --version, else LINDOS_PKG_VERSION, else the VERSION file.
valid_pkg_version() {
    [[ "$1" =~ ^[0-9]+(\.[0-9]+)*([~+][A-Za-z0-9.+~]+)?$ ]]
}
if [ -z "${PKG_VERSION}" ] && [ -f "${ROOT}/VERSION" ]; then
    PKG_VERSION="$(tr -d ' \r\n' < "${ROOT}/VERSION")"
fi
if [ -n "${PKG_VERSION}" ] && ! valid_pkg_version "${PKG_VERSION}"; then
    die "invalid release version '${PKG_VERSION}' (want e.g. 1.2.3, or 1.2.3~rc1 / 1.2.3+ci.7)" 2
fi

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
control_field() {
    # control_field FILE FIELD → value (first line only, trimmed)
    local file="$1" field="$2"
    awk -v f="${field}:" 'index($0, f) == 1 { sub(/^[^:]*:[ \t]*/, ""); print; exit }' "${file}"
}

validate_control() {
    local ctl="$1" name="$2"
    local missing=() f v
    [ -f "${ctl}" ] || die "packages/${name}: DEBIAN/control missing"
    for f in Package Version Architecture Maintainer Description; do
        v="$(control_field "${ctl}" "${f}")"
        [ -n "${v}" ] || missing+=("${f}")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        die "packages/${name}/DEBIAN/control lacks required field(s): ${missing[*]}"
    fi
    local pkg
    pkg="$(control_field "${ctl}" Package)"
    if [ "${pkg}" != "${name}" ]; then
        warn "packages/${name}: control says Package: ${pkg} (directory name differs)"
    fi
    if ! [[ "$(control_field "${ctl}" Version)" =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]]; then
        die "packages/${name}: invalid Version '$(control_field "${ctl}" Version)'"
    fi
    if [ "$(control_field "${ctl}" Architecture)" != "all" ]; then
        warn "packages/${name}: Architecture is not 'all' ($(control_field "${ctl}" Architecture))"
    fi
    if [ "$(control_field "${ctl}" Maintainer)" != "Lindos Team <team@lindos.dev>" ]; then
        warn "packages/${name}: Maintainer differs from SPEC ('$(control_field "${ctl}" Maintainer)')"
    fi
}

is_exec_path() {
    # is_exec_path REL — REL relative to staging root, e.g. usr/bin/lindos-mode
    local rel="$1"
    case "${rel}" in
        usr/bin/*|usr/sbin/*|usr/games/*|bin/*|sbin/*) return 0 ;;
        usr/libexec/*) return 0 ;;
        usr/lib/*/bin/*) return 0 ;;
        *.sh) return 0 ;;
        DEBIAN/preinst|DEBIAN/postinst|DEBIAN/prerm|DEBIAN/postrm|DEBIAN/config) return 0 ;;
        etc/gamemode.d/*|etc/lindos/hooks.d/*) return 0 ;;
        # Debian kernel-hook convention (kernel-package/run-parts): scripts under
        # /etc/kernel/{preinst,postinst,prerm,postrm}.d/ must be executable or
        # run-parts silently skips them, e.g. packages/lindos-kernel's
        # etc/kernel/postinst.d/zz-lindos-sbsign Secure-Boot signing hook.
        etc/kernel/*.d/*) return 0 ;;
    esac
    return 1
}

has_shebang() {
    local f="$1"
    [ -f "${f}" ] || return 1
    [ "$(head -c 2 -- "${f}" 2>/dev/null)" = "#!" ]
}

strip_crlf_tree() {
    # strip CRLF from DEBIAN/*, shebang files and common text extensions.
    local stage="$1"
    local f
    while IFS= read -r -d '' f; do
        strip_crlf "${f}"
    done < <(find "${stage}/DEBIAN" -type f -print0 2>/dev/null)
    while IFS= read -r -d '' f; do
        if has_shebang "${f}"; then
            strip_crlf "${f}"
            continue
        fi
        case "${f}" in
            *.sh|*.py|*.desktop|*.conf|*.cfg|*.json|*.xml|*.css|*.policy|*.rules|*.service|*.timer|*.preset|*.list|*.sources|*.ini|*.txt|*.md|*.svg|*.pref|*.yaml|*.yml|*.plymouth|*.script|*.theme|*.rc|*.templates|*.seed)
                strip_crlf "${f}" ;;
        esac
    done < <(find "${stage}" -path "${stage}/DEBIAN" -prune -o -type f -print0)
}

fix_perms() {
    local stage="$1"
    local f rel
    find "${stage}" -type d -exec chmod 0755 {} +
    find "${stage}" -type f -exec chmod 0644 {} +
    # Executables per SPEC §1.1.  A shebang'd file elsewhere (e.g. a Python
    # module under usr/lib/…) deliberately stays 644: it is imported or run
    # via 'python3 file', never executed directly.
    while IFS= read -r -d '' f; do
        rel="${f#"${stage}"/}"
        if is_exec_path "${rel}"; then
            chmod 0755 "${f}"
        fi
    done < <(find "${stage}" -type f -print0)
    # DEBIAN metadata files must be plain files with sane perms.
    for f in control conffiles md5sums triggers shlibs templates; do
        [ -f "${stage}/DEBIAN/${f}" ] && chmod 0644 "${stage}/DEBIAN/${f}"
    done
    return 0
}

check_maintainer_scripts() {
    local stage="$1" name="$2"
    local s first
    for s in preinst postinst prerm postrm config; do
        [ -f "${stage}/DEBIAN/${s}" ] || continue
        first="$(head -n1 -- "${stage}/DEBIAN/${s}")"
        case "${first}" in
            '#!/bin/sh'*)
                if have sh; then sh -n "${stage}/DEBIAN/${s}" || die "packages/${name}/DEBIAN/${s}: sh syntax error"; fi ;;
            '#!/bin/bash'*|'#!/usr/bin/env bash'*)
                bash -n "${stage}/DEBIAN/${s}" || die "packages/${name}/DEBIAN/${s}: bash syntax error" ;;
            *)
                warn "packages/${name}/DEBIAN/${s}: unusual shebang '${first}'" ;;
        esac
    done
    if [ -f "${stage}/DEBIAN/conffiles" ]; then
        # every conffile must exist in the tree and be absolute
        while IFS= read -r line; do
            [ -n "${line}" ] || continue
            case "${line}" in
                /*) [ -f "${stage}${line}" ] || die "packages/${name}: conffile ${line} listed but not shipped" ;;
                *) die "packages/${name}: conffiles entry must be absolute: ${line}" ;;
            esac
        done < "${stage}/DEBIAN/conffiles"
    fi
}

gen_md5sums() {
    local stage="$1"
    ( cd "${stage}" && find . -path ./DEBIAN -prune -o -type f -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 -r md5sum \
        | sed 's|^\([0-9a-f]*\) [ *]\./|\1  |' ) > "${stage}/DEBIAN/md5sums"
    chmod 0644 "${stage}/DEBIAN/md5sums"
}

stamp_version() {
    # stamp_version STAGE VERSION - lock-step release version, in the staging copy only
    local stage="$1" version="$2"
    local ctl="${stage}/DEBIAN/control"
    awk -v newv="${version}" '
        /^[A-Za-z]/ {
            field = $0
            sub(/:.*/, "", field)
            inrel = (field ~ /^(Depends|Pre-Depends|Recommends|Suggests|Breaks|Conflicts|Replaces|Enhances)$/)
            if (field == "Version") { print "Version: " newv; next }
        }
        {
            line = $0
            if (inrel) {
                out = ""
                while (match(line, /lindos-[a-z0-9.+-]+ \(= [^)]*\)/)) {
                    seg = substr(line, RSTART, RLENGTH)
                    sub(/\(= [^)]*\)/, "(= " newv ")", seg)
                    out = out substr(line, 1, RSTART - 1) seg
                    line = substr(line, RSTART + RLENGTH)
                }
                line = out line
            }
            print line
        }' "${ctl}" > "${ctl}.stamped"
    mv -f "${ctl}.stamped" "${ctl}"
    local init="${stage}/usr/lib/python3/dist-packages/lindos/__init__.py"
    if [ -f "${init}" ]; then
        sed -i -E "s/^__version__ = \".*\"\$/__version__ = \"${version}\"/" "${init}"
    fi
}

build_one() {
    local name="$1"
    local src="${PACKAGES_DIR}/${name}"
    local ctl="${src}/DEBIAN/control"
    [ -d "${src}" ] || die "packages/${name}: no such package directory"
    validate_control "${ctl}" "${name}"

    # The archive keyring: refuse a switched-on source without a real, pinned key, and never ship
    # the placeholder that stands in for the key until the owner commits the real one.
    local keyring_state=""
    if [ "${name}" = "lindos-archive-keyring" ]; then
        keyring_state="$(bash "${BUILD_DIR}/tools/check-archive-keyring.sh" "${src}/root")" ||
            die "packages/${name}: refusing to build (see above; docs/RELEASING.md)"
    fi

    local pkg ver arch
    pkg="$(control_field "${ctl}" Package)"
    arch="$(control_field "${ctl}" Architecture)"
    local stage="${STAGING_BASE}/${name}"

    timer_start "deb ${name}"
    rm -rf "${stage}"
    ensure_dir "${stage}"

    if [ -d "${src}/root" ]; then
        # cp -a keeps symlinks; perms are normalised afterwards.
        cp -a "${src}/root/." "${stage}/"
    else
        warn "packages/${name}: no root/ directory (metadata-only package)"
    fi
    ensure_dir "${stage}/DEBIAN"
    cp -a "${src}/DEBIAN/." "${stage}/DEBIAN/"

    # Never ship editor/OS junk or Python caches.
    find "${stage}" \( -name '__pycache__' -o -name '.pytest_cache' \) -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "${stage}" \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' -o -name 'Thumbs.db' -o -name '*~' -o -name '*.swp' \) -type f -delete 2>/dev/null || true
    rm -f "${stage}/DEBIAN/md5sums"

    strip_crlf_tree "${stage}"
    if [ -n "${PKG_VERSION}" ]; then
        stamp_version "${stage}" "${PKG_VERSION}"
        log "${name}: stamped release version ${PKG_VERSION}"
    fi
    ver="$(control_field "${stage}/DEBIAN/control" Version)"
    [[ "${ver}" =~ ^[0-9][A-Za-z0-9.+~:-]*$ ]] || die "packages/${name}: invalid Version '${ver}' after stamping"
    local out_deb="${OUT}/${pkg}_${ver}_${arch}.deb"
    if [ "${keyring_state}" = "placeholder" ]; then
        rm -f "${stage}/usr/share/keyrings/lindos-archive-keyring.gpg"
        warn "${name}: built WITHOUT a signing key (the placeholder was dropped) - its source stays disabled"
    fi
    fix_perms "${stage}"
    check_maintainer_scripts "${stage}" "${name}"
    gen_md5sums "${stage}"

    # Informational: files under etc/ that are not declared conffiles.
    if [ -d "${stage}/etc" ]; then
        local n_etc n_conf
        n_etc="$(find "${stage}/etc" -type f | wc -l)"
        n_conf=0
        [ -f "${stage}/DEBIAN/conffiles" ] && n_conf="$(grep -c . "${stage}/DEBIAN/conffiles" || true)"
        log "${name}: ${n_etc} file(s) under /etc, ${n_conf} declared conffile(s)"
    fi

    rm -f "${out_deb}"
    log "dpkg-deb --build --root-owner-group ${stage} → ${out_deb}"
    dpkg-deb --build --root-owner-group "${stage}" "${out_deb}"

    log "dpkg-deb -I ${out_deb}:"
    dpkg-deb -I "${out_deb}" | sed 's/^/    /' >&2
    local size
    size="$(stat -c %s "${out_deb}" 2>/dev/null || wc -c < "${out_deb}")"
    ok "${name}: $(basename "${out_deb}") ($(human_size "${size}"))"

    if [ "${RUN_LINTIAN}" -eq 1 ]; then
        if have lintian; then
            log "lintian ${out_deb}"
            lintian --no-tag-display-limit "${out_deb}" || warn "lintian reported issues for ${name} (non-fatal)"
        else
            warn "--lintian requested but lintian is not installed"
        fi
    fi

    if [ "${KEEP_STAGING}" -eq 0 ]; then
        rm -rf "${stage}"
    fi
    timer_end "deb ${name}"
    BUILT+=("${out_deb}")
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
[ -d "${PACKAGES_DIR}" ] || die "packages directory not found: ${PACKAGES_DIR}"

if [ "${#NAMES[@]}" -eq 0 ] || [ "${NAMES[*]}" = "all" ]; then
    NAMES=()
    for d in "${PACKAGES_DIR}"/*/; do
        d="${d%/}"
        n="$(basename "${d}")"
        if [ -f "${d}/DEBIAN/control" ]; then
            NAMES+=("${n}")
        else
            warn "packages/${n}: no DEBIAN/control — skipped"
        fi
    done
    [ "${#NAMES[@]}" -gt 0 ] || die "no buildable packages under ${PACKAGES_DIR}"
fi

BUILT=()
for n in "${NAMES[@]}"; do
    [ "${n}" = "all" ] && continue
    build_one "${n}"
done

log "built ${#BUILT[@]} package(s) into ${OUT}:"
for b in "${BUILT[@]}"; do
    log "  $(basename "${b}")"
done
timer_summary
