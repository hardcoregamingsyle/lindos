#!/bin/bash
# build-kernel.sh — build the Lindos-tuned kernel .debs (SPEC-KERNEL §15.5).
#
#   fetch linux-X.Y.Z  →  make defconfig  →  merge lindos.config  →  make olddefconfig
#   →  apply patches/series (if any)  →  make -j N bindeb-pkg LOCALVERSION=-lindos
#   →  move linux-image-*.deb / linux-headers-*.deb into --out (default out/kernel)
#
# LINUX HOST ONLY.  It refuses to run anywhere else (exit 2): you cannot compile a Linux
# kernel on Windows, and Lindos never fakes a build artifact.  It NEVER installs anything and
# NEVER calls sudo; the resulting .debs are handed to the ISO builder's 35-kernel.sh hook.
#
# It builds NOTHING related to anti-cheat circumvention: this is a stock upstream kernel plus a
# performance/compat config fragment (ntsync, sched_ext, 1000 Hz, MGLRU, BBR).  Kernel-level
# anti-cheat (Vanguard; publisher-disabled EAC/BattlEye) is impossible on any Linux kernel.
#
# Usage: build-kernel.sh [--version X.Y.Z] [--config PATH] [--localversion -lindos]
#                        [--jobs N] [--out DIR] [--kdir DIR] [--skip-fetch] [--menuconfig]
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PATCHES_DIR="${SCRIPT_DIR}/patches"

log()  { printf '[build-kernel] %s\n' "$*"; }
warn() { printf '[build-kernel] warning: %s\n' "$*" >&2; }
die()  { printf '[build-kernel] fatal: %s\n' "$*" >&2; exit "${2:-1}"; }

# --- defaults ----------------------------------------------------------------------------- #
VERSION=""
CONFIG=""
LOCALVERSION="-lindos"
JOBS=""
OUT_DIR="${REPO_ROOT}/out/kernel"
KDIR=""
SKIP_FETCH=0
MENUCONFIG=0

CONFIG_IN_REPO="${REPO_ROOT}/packages/lindos-kernel/root/usr/share/lindos/kernel/lindos.config"
CONFIG_INSTALLED="/usr/share/lindos/kernel/lindos.config"
MANIFEST_IN_REPO="${REPO_ROOT}/packages/lindos-kernel/root/usr/share/lindos/kernel/manifest.json"

usage() {
    sed -n '2,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | grep -v '^set -Eeuo' | sed 's/^# \{0,1\}//'
}

# --- argument parsing --------------------------------------------------------------------- #
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)      VERSION="${2:?--version needs a value}"; shift 2 ;;
        --config)       CONFIG="${2:?--config needs a path}"; shift 2 ;;
        --localversion) LOCALVERSION="${2:?--localversion needs a value}"; shift 2 ;;
        --jobs)         JOBS="${2:?--jobs needs a number}"; shift 2 ;;
        --out)          OUT_DIR="${2:?--out needs a dir}"; shift 2 ;;
        --kdir)         KDIR="${2:?--kdir needs a dir}"; SKIP_FETCH=1; shift 2 ;;
        --skip-fetch)   SKIP_FETCH=1; shift ;;
        --menuconfig)   MENUCONFIG=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              printf 'build-kernel: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done

# --- host guard: Linux only (SPEC-KERNEL §15.5) ------------------------------------------- #
case "$(uname -s 2>/dev/null || echo unknown)" in
    Linux) : ;;
    *) die "kernels can only be built on a Linux host (got '$(uname -s 2>/dev/null || echo unknown)'). \
Run this on the Lindos build machine/CI, never on Windows." 2 ;;
esac

# --- resolve config fragment -------------------------------------------------------------- #
if [ -z "${CONFIG}" ]; then
    if [ -f "${CONFIG_IN_REPO}" ]; then
        CONFIG="${CONFIG_IN_REPO}"
    elif [ -f "${CONFIG_INSTALLED}" ]; then
        CONFIG="${CONFIG_INSTALLED}"
    else
        die "no lindos.config found (looked in repo and ${CONFIG_INSTALLED}); pass --config PATH" 2
    fi
fi
[ -f "${CONFIG}" ] || die "config fragment not found: ${CONFIG}" 2

# --- resolve version (default = manifest recommended.series) ------------------------------ #
resolve_series() {
    [ -f "${MANIFEST_IN_REPO}" ] || return 1
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${MANIFEST_IN_REPO}" <<'PY' 2>/dev/null || return 1
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(json.load(fh)["recommended"]["series"])
PY
    else
        return 1
    fi
}
if [ -z "${VERSION}" ] && [ "${SKIP_FETCH}" -eq 0 ]; then
    if series="$(resolve_series)" && [ -n "${series}" ]; then
        VERSION="${series}"
        log "no --version given; using manifest recommended series ${VERSION}"
    else
        die "cannot resolve a default kernel version offline; pass --version X.Y.Z" 2
    fi
fi

# --- jobs default = nproc ----------------------------------------------------------------- #
if [ -z "${JOBS}" ]; then
    JOBS="$(nproc 2>/dev/null || echo 4)"
fi
case "${JOBS}" in
    ''|*[!0-9]*) die "--jobs must be a positive integer, got '${JOBS}'" 2 ;;
esac

# --- dependency check (report ALL missing, exit 2) ---------------------------------------- #
check_deps() {
    local missing=() cmd pkg
    for cmd in bison flex bc make gcc; do
        command -v "${cmd}" >/dev/null 2>&1 || missing+=("${cmd}")
    done
    command -v dpkg-buildpackage >/dev/null 2>&1 || command -v dpkg-deb >/dev/null 2>&1 || missing+=("dpkg-dev")
    if command -v dpkg-query >/dev/null 2>&1; then
        for pkg in libssl-dev libelf-dev; do
            dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "install ok installed" \
                || missing+=("${pkg}")
        done
    else
        warn "dpkg-query unavailable — cannot verify libssl-dev/libelf-dev; assuming present"
    fi
    if [ "${#missing[@]}" -gt 0 ]; then
        die "missing build dependencies: ${missing[*]}
Install them with:  sudo apt-get install bison flex libssl-dev libelf-dev bc dpkg-dev build-essential" 2
    fi
}
check_deps

# --- fetch / locate the kernel source ----------------------------------------------------- #
fetch_kernel() {
    local ver="$1" major url tarball work
    major="${ver%%.*}"
    work="${REPO_ROOT}/out/kernel-src"
    mkdir -p "${work}"
    tarball="linux-${ver}.tar.xz"
    KDIR="${work}/linux-${ver}"
    if [ -d "${KDIR}" ]; then
        log "reusing existing source tree ${KDIR}"
        return 0
    fi
    url="https://cdn.kernel.org/pub/linux/kernel/v${major}.x/${tarball}"
    log "fetching ${url}"
    local dl
    if command -v curl >/dev/null 2>&1; then
        dl=(curl -fL --retry 3 -o "${work}/${tarball}" "${url}")
    elif command -v wget >/dev/null 2>&1; then
        dl=(wget -O "${work}/${tarball}" "${url}")
    else
        die "need curl or wget to fetch the kernel (or use --kdir DIR)" 2
    fi
    "${dl[@]}" || die "failed to download ${url}" 1
    verify_sha "${work}" "${tarball}" "${major}"
    log "extracting ${tarball}"
    tar -C "${work}" -xf "${work}/${tarball}" || die "failed to extract ${tarball}" 1
    [ -d "${KDIR}" ] || die "extracted tree not found at ${KDIR}" 1
}

verify_sha() {
    local work="$1" tarball="$2" major="$3" shafile url
    shafile="sha256sums.asc"
    url="https://cdn.kernel.org/pub/linux/kernel/v${major}.x/${shafile}"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 2 -o "${work}/${shafile}" "${url}" 2>/dev/null || { warn "could not fetch ${shafile}; skipping checksum verification"; return 0; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${work}/${shafile}" "${url}" 2>/dev/null || { warn "could not fetch ${shafile}; skipping checksum verification"; return 0; }
    else
        return 0
    fi
    local want
    want="$(grep -E "  ${tarball}\$" "${work}/${shafile}" | awk '{print $1}' | head -n1 || true)"
    if [ -z "${want}" ]; then
        warn "no checksum line for ${tarball} in ${shafile}; skipping verification"
        return 0
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        local got
        got="$(sha256sum "${work}/${tarball}" | awk '{print $1}')"
        [ "${got}" = "${want}" ] || die "sha256 mismatch for ${tarball} (want ${want}, got ${got})" 1
        log "sha256 verified for ${tarball}"
    else
        warn "sha256sum not available; skipping verification"
    fi
}

if [ "${SKIP_FETCH}" -eq 1 ]; then
    [ -n "${KDIR}" ] || die "--skip-fetch requires --kdir DIR (an existing kernel source tree)" 2
    [ -d "${KDIR}" ] || die "kernel dir does not exist: ${KDIR}" 2
    log "using existing kernel tree ${KDIR} (fetch skipped)"
else
    fetch_kernel "${VERSION}"
fi

# --- configure ---------------------------------------------------------------------------- #
cd "${KDIR}"
log "make defconfig"
make defconfig
log "merging Lindos fragment: ${CONFIG}"
./scripts/kconfig/merge_config.sh -m .config "${CONFIG}"
log "make olddefconfig"
make olddefconfig

# --- apply out-of-tree patches (patches/series, if non-empty) ----------------------------- #
apply_patches() {
    local series="${PATCHES_DIR}/series" line patch
    [ -f "${series}" ] || { log "no patches/series — building a stock upstream tree"; return 0; }
    while IFS= read -r line || [ -n "${line}" ]; do
        case "${line}" in ''|\#*) continue ;; esac
        patch="${PATCHES_DIR}/${line}"
        [ -f "${patch}" ] || die "patch listed in series is missing: ${patch}" 1
        log "applying patch ${line}"
        patch -p1 <"${patch}" || die "failed to apply ${line}" 1
    done <"${series}"
}
apply_patches

# --- optional interactive menuconfig ------------------------------------------------------ #
if [ "${MENUCONFIG}" -eq 1 ]; then
    log "launching menuconfig"
    make menuconfig
fi

# --- build -------------------------------------------------------------------------------- #
log "building: make -j ${JOBS} bindeb-pkg LOCALVERSION=${LOCALVERSION}"
make -j "${JOBS}" bindeb-pkg LOCALVERSION="${LOCALVERSION}"

# --- collect artifacts -------------------------------------------------------------------- #
mkdir -p "${OUT_DIR}"
moved=0
for deb in "${KDIR}/../"linux-image-*"${LOCALVERSION}"*.deb \
           "${KDIR}/../"linux-headers-*"${LOCALVERSION}"*.deb \
           "${KDIR}/../"linux-libc-dev*.deb; do
    [ -e "${deb}" ] || continue
    mv -f "${deb}" "${OUT_DIR}/" && moved=$((moved + 1))
done
[ "${moved}" -gt 0 ] || die "build finished but no .deb artifacts were found next to ${KDIR}" 1

log "done: ${moved} package(s) in ${OUT_DIR}"
ls -1 "${OUT_DIR}"/*.deb 2>/dev/null || true
exit 0
