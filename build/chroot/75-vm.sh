#!/bin/bash
# ============================================================================
#  75-vm.sh — install the optional Windows-VM / WinApps packages (SPEC-VM §21-22)
#
#  Runs INSIDE the squashfs chroot as root, AFTER 70-gaming.sh and BEFORE
#  80-cleanup.sh.  The hook order is the NN- numeric sort build-iso.sh applies
#  to build/chroot/[0-9][0-9]-*.sh, so this file lands at 75 automatically:
#      00 10 20 30 35 40 50 60 70 75 80.
#
#  It ensures the two honest Windows-integration packages are installed from
#  the staged local debs WHEN THEY WERE BUILT:
#
#      lindos-vm        real KVM/QEMU Windows VM (optional GPU passthrough)
#      lindos-winapps   seamless Windows apps over RDP (Adobe / Office)
#
#  Both are OPTIONAL: they are owned by other engineers and may not be present
#  in every checkout.  30-lindos-debs.sh already installs any staged .deb it
#  finds (its "extra deb" pass), so for a full build this hook is normally a
#  no-op re-assertion — exactly like 35-kernel.sh re-asserting lindos-kernel.
#  When a package is absent the hook logs 'not built — skipped' and continues
#  WITHOUT failing, so the ISO builds green with or without these packages.
#
#  Honesty (SPEC-VM §20): this hook installs standard packages only.  It ships,
#  generates and enables NO anti-cheat / VM-detection evasion of any kind — no
#  hypervisor hiding, no SMBIOS/UUID/ACPI/CPUID spoofing, no licence bypass.
#  Windows is never downloaded; the user supplies a licensed Windows.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "lindos vm / winapps"

: "${LINDOS_DEBS_DIR:=${LINDOS_STAGE_DIR}/debs}"
# Space-separated list of the optional packages this hook manages.
: "${LINDOS_VM_PACKAGES:=lindos-vm lindos-winapps}"

# ---------------------------------------------------------------------------
# install_local_deb PKG — install the newest staged PKG_*.deb via dpkg -i and
# 'apt-get -f install' (so its Depends/Recommends resolve from the archive).
# Never aborts the build: any failure is warned and swallowed.
# ---------------------------------------------------------------------------
install_local_deb() {
    local pkg="$1"
    shopt -s nullglob
    local matches=("${LINDOS_DEBS_DIR}/${pkg}_"*.deb)
    shopt -u nullglob
    if [ "${#matches[@]}" -eq 0 ]; then
        log "${pkg}: not built — skipped (no ${pkg}_*.deb in ${LINDOS_DEBS_DIR})"
        return 0
    fi
    # Newest version last in glob order.
    local deb="${matches[$(( ${#matches[@]} - 1 ))]}"
    log "installing ${pkg} from $(basename "${deb}")"
    if ! dpkg -i "${deb}"; then
        warn "dpkg -i ${pkg} reported an error — running apt-get -f install"
    fi
    apt-get "${APT_ARGS[@]}" -f install || warn "apt-get -f install reported an error (continuing)"
    if pkg_installed "${pkg}"; then
        log "installed: ${pkg} $(dpkg-query -W -f='${Version}' "${pkg}" 2>/dev/null || echo '?')"
    else
        warn "${pkg} still not installed after fallback (continuing)"
    fi
    return 0
}

apt_update

read -r -a VM_PKGS <<< "${LINDOS_VM_PACKAGES}"
for pkg in "${VM_PKGS[@]}"; do
    if pkg_installed "${pkg}"; then
        log "${pkg} already installed: $(dpkg-query -W -f='${Version}' "${pkg}" 2>/dev/null || echo '?') — re-assert is a no-op"
        continue
    fi
    install_local_deb "${pkg}"
done

# ---------------------------------------------------------------------------
# Report (never fatal).  These CLIs are provided by the two packages; when a
# package was not built the CLI is simply absent.
# ---------------------------------------------------------------------------
if have lindos-vm; then
    log "lindos-vm present; a host capability probe is available at runtime via 'lindos-vm check'"
fi
if have lindos-winapps; then
    log "lindos-winapps present; run 'lindos-winapps check' on the installed system"
fi

hook_end
