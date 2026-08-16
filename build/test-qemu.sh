#!/bin/bash
# ============================================================================
#  build/test-qemu.sh — boot the Lindos ISO in QEMU (legacy BIOS or UEFI/OVMF)
#
#  Usage: build/test-qemu.sh [options] [ISO]
#     ISO                 path to the ISO (default: newest out/lindos-*.iso)
#     --uefi              boot with OVMF (UEFI) instead of SeaBIOS
#     --headless          no window: -display none, serial console on stdio
#     --disk [SIZE_GB]    attach a persistent qcow2 (created if missing,
#                         default ${QEMU_DISK_GB} GB) for install tests
#     --boot-disk         boot from that disk instead of the ISO (after install)
#     --ram MB            memory (default ${QEMU_RAM_MB})
#     --cpus N            vCPUs (default ${QEMU_CPUS})
#     --display gtk|sdl|spice-app|none   (default: gtk, falls back to sdl)
#     --no-kvm            do not use KVM acceleration (slow!)
#     --snapshot          discard disk writes when the VM exits
#     --dry-run           print the qemu command line and exit
#     -h, --help
#
#  KVM needs /dev/kvm access (group kvm) — no root required for that; the
#  script never calls sudo.  UEFI needs the 'ovmf' package
#  (/usr/share/OVMF/OVMF_CODE_4M.fd); the VARS file is copied to out/qemu/ so
#  the firmware can save boot entries.
# ============================================================================
set -Eeuo pipefail

BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=build/lib/common.sh
. "${BUILD_DIR}/lib/common.sh"
LOG_PREFIX="test-qemu"

ROOT="$(repo_root)"
lindos_load_config

ISO=""
UEFI=0
HEADLESS=0
DISK=0
DISK_SIZE="${QEMU_DISK_GB}"
BOOT_DISK=0
RAM="${QEMU_RAM_MB}"
CPUS="${QEMU_CPUS}"
DISPLAY_MODE="${QEMU_DISPLAY:-gtk}"
USE_KVM=1
SNAPSHOT=0
DRY_RUN=0

usage() { sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --uefi) UEFI=1; shift ;;
        --headless) HEADLESS=1; shift ;;
        --disk)
            DISK=1; shift
            if [ $# -gt 0 ] && [[ "$1" =~ ^[0-9]+$ ]]; then DISK_SIZE="$1"; shift; fi ;;
        --disk=*) DISK=1; DISK_SIZE="${1#*=}"; shift ;;
        --boot-disk) DISK=1; BOOT_DISK=1; shift ;;
        --ram) [ $# -ge 2 ] || die "--ram needs MB" 2; RAM="$2"; shift 2 ;;
        --ram=*) RAM="${1#*=}"; shift ;;
        --cpus) [ $# -ge 2 ] || die "--cpus needs N" 2; CPUS="$2"; shift 2 ;;
        --cpus=*) CPUS="${1#*=}"; shift ;;
        --display) [ $# -ge 2 ] || die "--display needs a mode" 2; DISPLAY_MODE="$2"; shift 2 ;;
        --display=*) DISPLAY_MODE="${1#*=}"; shift ;;
        --no-kvm) USE_KVM=0; shift ;;
        --snapshot) SNAPSHOT=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1 (see --help)" 2 ;;
        *) ISO="$1"; shift ;;
    esac
done

OUT_DIR="$(cd "${ROOT}" && abs_path "${OUT_DIR}")"
QEMU_DIR="${OUT_DIR}/qemu"
ensure_dir "${QEMU_DIR}"

# ---------------------------------------------------------------------------
# ISO
# ---------------------------------------------------------------------------
if [ -z "${ISO}" ]; then
    ISO="$(find "${OUT_DIR}" -maxdepth 1 -name 'lindos-*.iso' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n1 | cut -d' ' -f2-)"
    if [ -z "${ISO}" ] && [ -f "${OUT_DIR}/${ISO_NAME}" ]; then ISO="${OUT_DIR}/${ISO_NAME}"; fi
fi
if [ "${BOOT_DISK}" -eq 0 ]; then
    [ -n "${ISO}" ] && [ -f "${ISO}" ] || die "no ISO found (pass a path or run 'make iso' first)"
    ISO="$(abs_path "${ISO}")"
    log "ISO: ${ISO} ($(human_size "$(stat -c %s "${ISO}")"))"
fi

# ---------------------------------------------------------------------------
# qemu binary + acceleration
# ---------------------------------------------------------------------------
APT_HINT_PACKAGES="qemu-system-x86 qemu-utils ovmf" require_cmd qemu-system-x86_64
QEMU=(qemu-system-x86_64)
ACCEL_ARGS=()
if [ "${USE_KVM}" -eq 1 ]; then
    if [ -w /dev/kvm ]; then
        ACCEL_ARGS=(-enable-kvm -cpu host)
    else
        warn "/dev/kvm not writable (add yourself to the 'kvm' group) — running without KVM (slow)"
        USE_KVM=0
    fi
fi
if [ "${USE_KVM}" -eq 0 ]; then
    ACCEL_ARGS=(-cpu max)
fi

ARGS=(
    -name "Lindos ${LINDOS_VERSION} test"
    -machine q35
    "${ACCEL_ARGS[@]}"
    -m "${RAM}"
    -smp "${CPUS}"
    -rtc base=utc
    -usb -device usb-tablet
    -device "virtio-net-pci,netdev=n0" -netdev "user,id=n0"
    -audiodev "none,id=snd0"
    -device intel-hda -device "hda-duplex,audiodev=snd0"
)

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
if [ "${HEADLESS}" -eq 1 ]; then
    ARGS+=(-display none -vga none -serial stdio -monitor none)
    log "headless: serial console on stdio (add console=ttyS0 in the GRUB menu with 'e' to see the kernel)"
else
    ARGS+=(-vga virtio)
    case "${DISPLAY_MODE}" in
        gtk)
            if qemu-system-x86_64 -display help 2>/dev/null | grep -qx gtk; then
                ARGS+=(-display gtk)
            elif qemu-system-x86_64 -display help 2>/dev/null | grep -qx sdl; then
                warn "gtk display not available — using sdl"
                ARGS+=(-display sdl)
            else
                warn "neither gtk nor sdl display available — using default"
            fi ;;
        sdl) ARGS+=(-display sdl) ;;
        spice-app) ARGS+=(-display spice-app) ;;
        none) ARGS+=(-display none) ;;
        *) ARGS+=(-display "${DISPLAY_MODE}") ;;
    esac
fi

# ---------------------------------------------------------------------------
# UEFI (OVMF)
# ---------------------------------------------------------------------------
if [ "${UEFI}" -eq 1 ]; then
    code=""
    vars=""
    for c in "${OVMF_CODE}" /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
             /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/edk2-ovmf/x64/OVMF_CODE.fd \
             /usr/share/qemu/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
        if [ -f "${c}" ]; then code="${c}"; break; fi
    done
    [ -n "${code}" ] || die "no OVMF firmware found (apt-get install ovmf) — looked for ${OVMF_CODE}"
    for v in "${OVMF_VARS}" "${code%CODE*}VARS${code##*CODE}" /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
             /usr/share/edk2/ovmf/OVMF_VARS.fd /usr/share/edk2-ovmf/x64/OVMF_VARS.fd; do
        if [ -f "${v}" ]; then vars="${v}"; break; fi
    done
    vars_copy="${QEMU_DIR}/OVMF_VARS.fd"
    if [ -n "${vars}" ]; then
        if [ ! -f "${vars_copy}" ] || [ "${vars}" -nt "${vars_copy}" ]; then
            cp -f "${vars}" "${vars_copy}"
        fi
        ARGS+=(-drive "if=pflash,format=raw,readonly=on,file=${code}"
               -drive "if=pflash,format=raw,file=${vars_copy}")
    else
        warn "no OVMF_VARS template found — using ${code} read-only without NVRAM"
        ARGS+=(-drive "if=pflash,format=raw,readonly=on,file=${code}")
    fi
    log "UEFI firmware: ${code}"
else
    log "legacy BIOS (SeaBIOS) — use --uefi for OVMF"
fi

# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
if [ "${DISK}" -eq 1 ]; then
    require_cmd qemu-img
    disk_img="${QEMU_DIR}/lindos-test.qcow2"
    if [ ! -f "${disk_img}" ]; then
        log "creating ${disk_img} (${DISK_SIZE}G)"
        qemu-img create -f qcow2 "${disk_img}" "${DISK_SIZE}G" >/dev/null
    fi
    drive="file=${disk_img},if=virtio,format=qcow2,discard=unmap"
    [ "${SNAPSHOT}" -eq 1 ] && drive+=",snapshot=on"
    ARGS+=(-drive "${drive}")
    log "disk: ${disk_img}"
fi

if [ "${BOOT_DISK}" -eq 1 ]; then
    ARGS+=(-boot c)
    [ -n "${ISO}" ] && [ -f "${ISO}" ] && ARGS+=(-cdrom "${ISO}")
else
    ARGS+=(-cdrom "${ISO}" -boot d)
fi

# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------
log "qemu command:"
printf '  %q' "${QEMU[@]}" "${ARGS[@]}" >&2
printf '\n' >&2
if [ "${DRY_RUN}" -eq 1 ]; then
    exit 0
fi
exec "${QEMU[@]}" "${ARGS[@]}"
