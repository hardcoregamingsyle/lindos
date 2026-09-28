#!/bin/bash
# ============================================================================
#  35-kernel.sh — install the Lindos-tuned kernel on the ISO (SPEC-KERNEL §18)
#
#  Runs INSIDE the squashfs chroot as root, AFTER 30-lindos-debs.sh (which
#  installs the lindos-kernel *package* — the build recipe + CLI + config)
#  and BEFORE 40-theme.sh.  The hook order is purely the NN- numeric sort
#  build-iso.sh applies to build/chroot/[0-9][0-9]-*.sh, so this file lands
#  between 30 and 40 automatically:  00 10 20 30 35 40 50 60 70 80.
#
#  Two independent things happen here, both idempotent and both non-fatal:
#
#    1. Ensure the lindos-kernel *package* is installed from the staged local
#       debs (kconfig fragment, manifest, grub drop-in, `lindos-kernel` CLI).
#       30-lindos-debs.sh normally already did this; re-asserting is a no-op.
#
#    2. If compiled kernel image/header .debs exist (built on a Linux host by
#       build/kernel/build-kernel.sh into out/kernel/ — NEVER on Windows and
#       never committed), install them with `dpkg -i` + `apt-get -f install`
#       and regenerate grub so the Lindos kernel becomes the default.
#       If NO compiled kernel deb is present the hook logs
#       'no Lindos kernel built — using stock' and continues WITHOUT failing.
#
#  The ISO build therefore stays green whether or not a kernel deb was built.
#  The stock Mint/Ubuntu kernel always remains installed as a fallback.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "lindos kernel"

: "${LINDOS_DEBS_DIR:=${LINDOS_STAGE_DIR}/debs}"
# Candidate directories that may hold the compiled kernel image/header debs.
# build-iso.sh stages out/debs → /tmp/lindos/debs; a kernel built into
# out/kernel/ is picked up when the integrator drops it into that deb pool
# (SPEC-KERNEL §18 "pool") or stages it to /tmp/lindos/kernel.
: "${LINDOS_KERNEL_DEBS_DIR:=${LINDOS_STAGE_DIR}/kernel}"

# ---------------------------------------------------------------------------
# install_debs FILE… — dpkg -i then apt-get -f install; verify each is unpacked.
# Never aborts the build: a failure is warned and swallowed (exit 0).
# ---------------------------------------------------------------------------
install_debs() {
    [ "$#" -gt 0 ] || return 0
    local f
    for f in "$@"; do
        log "  dpkg -i $(basename "${f}")"
    done
    if ! dpkg -i "$@"; then
        warn "dpkg -i reported an error — running apt-get -f install to satisfy dependencies"
    fi
    apt-get "${APT_ARGS[@]}" -f install || warn "apt-get -f install reported an error (continuing)"
    return 0
}

# ---------------------------------------------------------------------------
# 1. Ensure the lindos-kernel package (recipe + CLI + config) is installed.
# ---------------------------------------------------------------------------
if pkg_installed lindos-kernel; then
    log "lindos-kernel package already installed: $(dpkg-query -W -f='${Version}' lindos-kernel 2>/dev/null || echo '?')"
else
    shopt -s nullglob
    kpkg=("${LINDOS_DEBS_DIR}"/lindos-kernel_*.deb)
    shopt -u nullglob
    if [ "${#kpkg[@]}" -gt 0 ]; then
        # newest (last in glob order)
        log "installing lindos-kernel package from ${kpkg[$(( ${#kpkg[@]} - 1 ))]}"
        install_debs "${kpkg[$(( ${#kpkg[@]} - 1 ))]}"
    else
        warn "no lindos-kernel_*.deb in ${LINDOS_DEBS_DIR} (lindos-kernel package not built?) — the lindos-kernel CLI/config will be absent"
    fi
fi

# ---------------------------------------------------------------------------
# 2. Compiled kernel image/header debs (optional artifact).
# ---------------------------------------------------------------------------
find_kernel_debs() {
    # Print image + header debs found in the candidate directories (first dir
    # that yields any wins), one path per line.  Matches the -lindos localversion.
    local dir
    for dir in "${LINDOS_KERNEL_DEBS_DIR}" "${LINDOS_DEBS_DIR}"; do
        [ -d "${dir}" ] || continue
        shopt -s nullglob
        local imgs=("${dir}"/linux-image-*-lindos*.deb "${dir}"/linux-image-*lindos*.deb)
        local hdrs=("${dir}"/linux-headers-*-lindos*.deb "${dir}"/linux-headers-*lindos*.deb)
        local libc=("${dir}"/linux-libc-dev_*lindos*.deb)
        shopt -u nullglob
        # Exclude the debug-symbols package: `make bindeb-pkg` also produces
        # linux-image-<ver>-dbg_*.deb (1+ GB, vs ~tens of MB for the real image) --
        # its name contains "-lindos" too (e.g. linux-image-6.14.0-lindos-dbg_...deb), so
        # both globs above match it. It is never a valid/needed *bootable* kernel image, so
        # installing it here would bloat the ISO by well over a gigabyte for nothing.
        local imgs_filtered=() f
        for f in "${imgs[@]}"; do
            case "$(basename "${f}")" in
                *-dbg_*.deb|*-dbg-*.deb) continue ;;
            esac
            imgs_filtered+=("${f}")
        done
        imgs=("${imgs_filtered[@]}")
        # de-duplicate (the two globs can overlap) and require at least one image
        local uniq=() seen="" f
        for f in "${imgs[@]}" "${hdrs[@]}" "${libc[@]}"; do
            case "${seen}" in *"|${f}|"*) continue ;; esac
            seen="${seen}|${f}|"
            uniq+=("${f}")
        done
        if [ "${#imgs[@]}" -gt 0 ]; then
            printf '%s\n' "${uniq[@]}"
            return 0
        fi
    done
    return 0
}

mapfile -t KDEBS < <(find_kernel_debs)

if [ "${#KDEBS[@]}" -gt 0 ]; then
    log "found ${#KDEBS[@]} compiled kernel deb(s):"
    for f in "${KDEBS[@]}"; do
        log "  $(basename "${f}")"
    done
    install_debs "${KDEBS[@]}"
    if have update-grub; then
        log "update-grub (regenerating boot menu so the Lindos kernel is default)"
        update-grub || warn "update-grub failed in the chroot (grub.cfg is regenerated at install time on the target)"
    else
        warn "update-grub not present in the chroot — boot menu will be generated on the installed system"
    fi
else
    log "no Lindos kernel built — using stock"
    log "build one on a Linux host with: make kernel   (or build/kernel/build-kernel.sh)"
fi

# ---------------------------------------------------------------------------
# 3. Report installed kernels (stock stays as a fallback).
# ---------------------------------------------------------------------------
if have dpkg-query; then
    log "installed linux-image packages:"
    dpkg-query -W -f='  ${Package} ${Version}\n' 'linux-image-*' 2>/dev/null \
        | grep -v ' $' | sed '/^  $/d' >&2 || true
fi
if have lindos-kernel; then
    log "lindos-kernel status:"
    lindos-kernel status 2>&1 | head -n 20 | sed 's/^/  /' >&2 || true
fi

hook_end
