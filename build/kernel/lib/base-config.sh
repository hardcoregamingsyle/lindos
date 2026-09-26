#!/bin/bash
# base-config.sh — resolve a *base* kernel .config for --base-config ubuntu (SPEC-WINDOWS §31.2).
#
# Sourced (never executed directly) by build/kernel/build-kernel.sh.  Kept as its own file, with
# every function taking its inputs as arguments rather than reading globals directly, so a test
# harness can source it standalone and exercise the resolution logic with a fake /boot directory
# and fake apt-cache/apt-get/dpkg-deb on PATH -- no real kernel source tree, network access or
# root needed.
#
# Why this exists at all: an upstream `make defconfig` kernel lacks almost every real Wi-Fi/GPU/
# audio driver (defconfig is a *minimal* sanity-check config, not a hardware-support one), so a
# kernel built from it alone would not boot usably on real hardware.  Ubuntu's own "generic"
# flavour config already enables the drivers real PCs need; Lindos reuses it as the base and
# merges its own tuning fragment on top (build-kernel.sh does the merge + olddefconfig).
set -Eeuo pipefail

# --- find the newest already-present Ubuntu generic config, if any ------------------------- #
# lindos_kernel_find_boot_generic_config BOOT_DIR
# Prints the newest "${BOOT_DIR}/config-*-generic" path and returns 0, or prints nothing and
# returns 1 when none exist.
lindos_kernel_find_boot_generic_config() {
    local boot_dir="$1" newest
    newest="$(ls -1 "${boot_dir}"/config-*-generic 2>/dev/null | sort -V | tail -n1 || true)"
    if [ -n "${newest}" ] && [ -f "${newest}" ]; then
        printf '%s\n' "${newest}"
        return 0
    fi
    return 1
}

# --- resolve the latest linux-modules-<ver>-generic package name via apt-cache ------------- #
# lindos_kernel_resolve_modules_pkg
# Prints the newest matching package name and returns 0, or prints nothing and returns 1.
lindos_kernel_resolve_modules_pkg() {
    local pkg
    command -v apt-cache >/dev/null 2>&1 || return 1
    pkg="$(apt-cache search --names-only '^linux-modules-[0-9]' 2>/dev/null \
        | awk '{print $1}' \
        | grep -E '^linux-modules-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-generic$' \
        | sort -V | tail -n1 || true)"
    if [ -n "${pkg}" ]; then
        printf '%s\n' "${pkg}"
        return 0
    fi
    return 1
}

# --- download that package (no root, no install) and extract /boot/config-* --------------- #
# lindos_kernel_fetch_generic_config WORK_DIR PKG_NAME
# Prints the path of the extracted config file and returns 0, or returns 1/2 with nothing.
lindos_kernel_fetch_generic_config() {
    local work="$1" pkg="$2" debfile cfgfile
    command -v apt-get  >/dev/null 2>&1 || return 2
    command -v dpkg-deb >/dev/null 2>&1 || return 2
    mkdir -p "${work}"
    ( cd "${work}" && apt-get download "${pkg}" ) >&2 || return 1
    debfile="$(ls "${work}/${pkg}"_*.deb 2>/dev/null | head -n1 || true)"
    [ -n "${debfile}" ] && [ -f "${debfile}" ] || return 1
    mkdir -p "${work}/extracted"
    dpkg-deb --fsys-tarfile "${debfile}" 2>/dev/null \
        | tar -x -C "${work}/extracted" --wildcards './boot/config-*' 2>/dev/null || return 1
    cfgfile="$(find "${work}/extracted/boot" -maxdepth 1 -name 'config-*' 2>/dev/null | head -n1 || true)"
    [ -n "${cfgfile}" ] && [ -f "${cfgfile}" ] || return 1
    printf '%s\n' "${cfgfile}"
    return 0
}

# --- top-level: resolve the Ubuntu generic base config, host-first then download ---------- #
# lindos_kernel_ubuntu_base_config BOOT_DIR WORK_DIR
# Prints the resolved config path on stdout and returns 0.  On failure, prints nothing on
# stdout, prints a reason on stderr, and returns non-zero (1 = tried and failed, 2 = required
# tooling missing).
lindos_kernel_ubuntu_base_config() {
    local boot_dir="$1" work="$2" src pkg rc
    if src="$(lindos_kernel_find_boot_generic_config "${boot_dir}")"; then
        printf '%s\n' "${src}"
        return 0
    fi
    if pkg="$(lindos_kernel_resolve_modules_pkg)"; then
        if src="$(lindos_kernel_fetch_generic_config "${work}" "${pkg}")"; then
            printf '%s\n' "${src}"
            return 0
        else
            rc=$?
            if [ "${rc}" -eq 2 ]; then
                echo "apt-get/dpkg-deb not found; cannot download ${pkg}" >&2
            else
                echo "failed to download or unpack ${pkg}" >&2
            fi
            return "${rc}"
        fi
    else
        echo "no linux-modules-*-generic package found via apt-cache" \
             "(run 'apt-get update' first, or pass --base-config defconfig|PATH)" >&2
        return 2
    fi
}
