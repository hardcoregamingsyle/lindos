#!/bin/bash
# install-into-prefix.sh — install/refresh DXVK or VKD3D-Proton into a Wine C:\ drive.
#
# SPEC-KERNEL §17.2.  Copies the release DLLs into a prefix and marks them "native" so
# Wine loads them over its built-ins.  Pins come from the caller (lindos-compat
# install-dxvk / install-vkd3d, which reads /usr/share/lindos/compat/components.json);
# this script is deliberately "dumb": it is given the component and the tarball URL.
#
# Usage:
#   install-into-prefix.sh --component dxvk|vkd3d --prefix DIR --url URL
#                          [--tag TAG] [--sha256 HEX] [--arch win64|win32]
#                          [--cache DIR] [--dry-run] [--uninstall]
#
# Not for Proton/umu prefixes: those ship DXVK/VKD3D already.  Runs as the user (no
# root, no sudo).  Exit codes: 0 ok · 2 usage · 3 offline/download failed · 1 other.
set -Eeuo pipefail

PROG="install-into-prefix"

COMPONENT=""
PREFIX=""
URL=""
TAG=""
SHA256=""
ARCH="win64"
CACHE=""
DRY_RUN=0
UNINSTALL=0

DXVK_DLLS=(d3d8 d3d9 d3d10core d3d11 dxgi)
VKD3D_DLLS=(d3d12 d3d12core)

log() { printf '%s: %s\n' "${PROG}" "$*" >&2; }
die() { log "ERROR: $1"; exit "${2:-1}"; }
have() { command -v "$1" >/dev/null 2>&1; }

run() {
    if [ "${DRY_RUN}" = 1 ]; then log "(dry-run) $*"; return 0; fi
    "$@"
}

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --component) COMPONENT="${2:-}"; shift 2 ;;
            --prefix)    PREFIX="${2:-}"; shift 2 ;;
            --url)       URL="${2:-}"; shift 2 ;;
            --tag)       TAG="${2:-}"; shift 2 ;;
            --sha256)    SHA256="${2:-}"; shift 2 ;;
            --arch)      ARCH="${2:-}"; shift 2 ;;
            --cache)     CACHE="${2:-}"; shift 2 ;;
            --dry-run)   DRY_RUN=1; shift ;;
            --uninstall) UNINSTALL=1; shift ;;
            -h|--help)   usage; exit 0 ;;
            *)           usage >&2; die "unknown argument '$1'" 2 ;;
        esac
    done
    case "${COMPONENT}" in
        dxvk|vkd3d) ;;
        *) die "must pass --component dxvk|vkd3d" 2 ;;
    esac
    [ -n "${PREFIX}" ] || die "must pass --prefix DIR (a Wine C:\\ drive)" 2
    [ "${UNINSTALL}" = 1 ] || [ -n "${URL}" ] || die "must pass --url URL (the release tarball)" 2
    [ "${ARCH}" = win64 ] || [ "${ARCH}" = win32 ] || die "--arch must be win64 or win32" 2
    [ -n "${CACHE}" ] || CACHE="${XDG_CACHE_HOME:-${HOME}/.cache}/lindos/compat"
}

check_prefix() {
    if [ ! -d "${PREFIX}/drive_c" ] && [ ! -f "${PREFIX}/system.reg" ]; then
        die "'${PREFIX}' is not a Wine C:\\ drive (no drive_c/ or system.reg). Run the program once first." 1
    fi
}

fetch() {
    # fetch URL DEST (curl or wget, atomic)
    local url="$1" dest="$2" tmp
    tmp="$(mktemp "${dest}.XXXXXX")"
    if have curl; then
        curl -fsSL --retry 3 --max-time 300 -o "${tmp}" "${url}" || { rm -f "${tmp}"; return 1; }
    elif have wget; then
        wget -q --tries=3 --timeout=300 -O "${tmp}" "${url}" || { rm -f "${tmp}"; return 1; }
    else
        rm -f "${tmp}"; log "neither curl nor wget is installed"; return 1
    fi
    [ -s "${tmp}" ] || { rm -f "${tmp}"; return 1; }
    mv -f "${tmp}" "${dest}"
}

verify_sha() {
    local file="$1" expected="$2" actual
    [ -n "${expected}" ] || { log "no sha256 pin - not verifying $(basename "${file}")"; return 0; }
    have sha256sum || { log "sha256sum not available - skipping verification"; return 0; }
    actual="$(sha256sum "${file}" | cut -d' ' -f1)"
    if [ "${actual}" != "${expected}" ]; then
        die "sha256 mismatch for $(basename "${file}"): expected ${expected}, got ${actual}" 1
    fi
    log "sha256 ok"
}

extract() {
    # extract TARBALL DESTDIR  (handles .tar.gz / .tar.xz / .tar.zst)
    local tarball="$1" dest="$2"
    mkdir -p "${dest}"
    case "${tarball}" in
        *.tar.zst|*.tzst)
            if tar --help 2>/dev/null | grep -q -- --zstd; then
                run tar --zstd -xf "${tarball}" -C "${dest}"
            elif have zstd; then
                zstd -dc "${tarball}" | tar -xf - -C "${dest}"
            else
                die "cannot unpack ${tarball}: install zstd (apt install zstd)" 1
            fi
            ;;
        *.tar.xz|*.txz)  run tar -xJf "${tarball}" -C "${dest}" ;;
        *.tar.gz|*.tgz)  run tar -xzf "${tarball}" -C "${dest}" ;;
        *)               die "unknown archive type: ${tarball}" 1 ;;
    esac
}

dll_list() {
    if [ "${COMPONENT}" = dxvk ]; then printf '%s\n' "${DXVK_DLLS[@]}"; else printf '%s\n' "${VKD3D_DLLS[@]}"; fi
}

src_dir_for() {
    # echo the extracted architecture sub-directory (x64 / x32 / x86) under $1
    local base="$1" want="$2" d
    for d in "${base}"/*/"${want}" "${base}/${want}"; do
        [ -d "${d}" ] && { printf '%s' "${d}"; return 0; }
    done
    return 1
}

copy_dlls() {
    # copy_dlls SRCDIR SYSDIR
    local src="$1" sysdir="$2" dll
    [ -n "${src}" ] || return 0
    run mkdir -p "${sysdir}"
    while IFS= read -r dll; do
        if [ -f "${src}/${dll}.dll" ]; then
            run cp -f "${src}/${dll}.dll" "${sysdir}/${dll}.dll"
        fi
    done < <(dll_list)
}

set_overrides() {
    # mark each DLL "native" (or delete the override on --uninstall) via wine reg
    local mode="$1" dll wine
    wine="$(command -v wine || true)"
    if [ -z "${wine}" ]; then
        log "wine not found - DLLs copied but overrides not set; set them with winecfg (Libraries)"
        return 0
    fi
    while IFS= read -r dll; do
        if [ "${mode}" = delete ]; then
            run env WINEPREFIX="${PREFIX}" WINEDEBUG=-all "${wine}" reg delete \
                'HKCU\Software\Wine\DllOverrides' /v "${dll}" /f >/dev/null 2>&1 || true
        else
            run env WINEPREFIX="${PREFIX}" WINEDEBUG=-all "${wine}" reg add \
                'HKCU\Software\Wine\DllOverrides' /v "${dll}" /d native /f >/dev/null 2>&1 || true
        fi
    done < <(dll_list)
}

do_uninstall() {
    local sys="${PREFIX}/drive_c/windows/system32" wow="${PREFIX}/drive_c/windows/syswow64" dll
    while IFS= read -r dll; do
        run rm -f "${sys}/${dll}.dll" "${wow}/${dll}.dll"
    done < <(dll_list)
    set_overrides delete
    log "${COMPONENT} removed from ${PREFIX} (Wine built-ins restored)"
}

main() {
    parse_args "$@"
    check_prefix

    if [ "${UNINSTALL}" = 1 ]; then
        do_uninstall
        exit 0
    fi

    mkdir -p "${CACHE}"
    local tarball
    tarball="${CACHE}/$(basename "${URL}")"
    if [ ! -s "${tarball}" ] || [ -n "${SHA256}" ]; then
        log "downloading ${COMPONENT} ${TAG:-} from ${URL}"
        fetch "${URL}" "${tarball}" || die "cannot download ${URL} (offline?)" 3
    else
        log "using cached ${tarball}"
    fi
    verify_sha "${tarball}" "${SHA256}"

    local workdir
    workdir="$(mktemp -d "${CACHE}/extract.XXXXXX")"
    # shellcheck disable=SC2064
    trap "rm -rf '${workdir}'" EXIT
    extract "${tarball}" "${workdir}"

    local sys="${PREFIX}/drive_c/windows/system32" wow="${PREFIX}/drive_c/windows/syswow64"
    local x64 x32
    x64="$(src_dir_for "${workdir}" x64 || true)"
    x32="$(src_dir_for "${workdir}" x32 || src_dir_for "${workdir}" x86 || true)"

    if [ "${ARCH}" = win64 ]; then
        copy_dlls "${x64}" "${sys}"
        copy_dlls "${x32}" "${wow}"
    else
        copy_dlls "${x32}" "${sys}"
    fi
    set_overrides native
    log "${COMPONENT} ${TAG:-} installed into ${PREFIX} (${ARCH})"
    exit 0
}

main "$@"
