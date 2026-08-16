#!/bin/bash
# ============================================================================
#  50-tune.sh — apply the Lindos base tune to the image (SPEC §8, §11, §13)
#
#  Runs INSIDE the squashfs chroot as root:
#      lindos-tune apply --mode ${TUNE_MODE:-everyday} --system --offline
#  which installs the systemd presets, sysctl drop-ins, zram configuration,
#  journald limits, tmpfiles and earlyoom defaults shipped by lindos-tune,
#  without touching the network (--offline: package installs are skipped and
#  logged).  If lindos-tune is somehow missing we apply the honest minimum
#  ourselves (systemd preset + fstrim.timer) and warn loudly.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "tune"

: "${TUNE_MODE:=everyday}"

if have lindos-tune; then
    log "lindos-tune apply --mode ${TUNE_MODE} --system --offline"
    if lindos-tune apply --mode "${TUNE_MODE}" --system --offline; then
        log "base tune applied"
    else
        warn "lindos-tune apply exited non-zero — check the log above; continuing"
    fi
else
    warn "lindos-tune not installed — applying minimal fallback tune"
    # systemd presets shipped by the package (if the files exist even though the
    # CLI is missing) — otherwise nothing to do but enable the safe defaults.
    if [ -f /usr/lib/systemd/system-preset/90-lindos.preset ]; then
        log "systemctl preset-all (90-lindos.preset present)"
        systemctl preset-all >/dev/null 2>&1 || warn "systemctl preset-all failed"
    fi
    svc_enable fstrim.timer
    if pkg_installed earlyoom; then
        svc_enable earlyoom.service
    fi
fi

# zram: make sure exactly one implementation is configured.  lindos-tune
# prefers systemd-zram-generator (installed by 20-base.sh); zram-tools'
# zramswap.service would double-configure, so disable it if both exist.
if pkg_installed zram-tools && pkg_installed systemd-zram-generator; then
    svc_disable zramswap.service
fi

# Show what we have (informational; never fatal).
if [ -f /etc/systemd/zram-generator.conf ]; then
    log "zram-generator.conf:"
    sed 's/^/  /' /etc/systemd/zram-generator.conf >&2 || true
fi
if [ -f /etc/sysctl.d/70-lindos-base.conf ]; then
    log "sysctl 70-lindos-base.conf present"
else
    warn "/etc/sysctl.d/70-lindos-base.conf missing"
fi
if [ -f /usr/lib/systemd/system-preset/90-lindos.preset ]; then
    log "systemd preset 90-lindos.preset present"
fi

if have lindos-tune; then
    log "lindos-tune status (chroot values are meaningless for RAM, shown for services/config only):"
    lindos-tune status 2>&1 | head -n 40 | sed 's/^/  /' >&2 || true
fi

hook_end
