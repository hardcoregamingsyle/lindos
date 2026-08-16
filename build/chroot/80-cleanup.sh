#!/bin/bash
# ============================================================================
#  80-cleanup.sh — make the chroot ready to be squashed (SPEC §8)
#
#  Runs INSIDE the squashfs chroot as root, last.
#    * apt-get autoremove --purge, apt-get clean, drop apt lists + caches
#      (the installed system runs 'apt update' itself; casper does not need
#      the lists and they cost ~100 MB in the squashfs)
#    * machine-id reset (/etc/machine-id empty, /var/lib/dbus/machine-id gone)
#      so every installation gets a fresh id
#    * resolv.conf restored from the backup build-iso.sh made
#      (/etc/resolv.conf.lindos-orig) — build-iso.sh repeats this on the host
#      side in case this hook was skipped
#    * logs truncated, root's caches/history removed, /tmp and /var/tmp emptied
#      (except /tmp/lindos, which build-iso.sh removes after unmounting)
#    * dpkg/debconf backup files, crash reports, journal removed
#  build-iso.sh removes policy-rc.d and /tmp/lindos itself after unmount.
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "cleanup"

# ---------------------------------------------------------------------------
# 1. apt
# ---------------------------------------------------------------------------
safe_autoremove
log "apt-get clean"
apt-get clean || true
rm -rf /var/cache/apt/archives/*.deb /var/cache/apt/archives/partial/* 2>/dev/null || true
rm -f /var/cache/apt/pkgcache.bin /var/cache/apt/srcpkgcache.bin 2>/dev/null || true
if [ -d /var/lib/apt/lists ]; then
    log "removing apt lists"
    find /var/lib/apt/lists -mindepth 1 -maxdepth 1 ! -name lock ! -name partial -exec rm -rf {} + 2>/dev/null || true
    rm -rf /var/lib/apt/lists/partial/* 2>/dev/null || true
fi
rm -f /var/cache/debconf/*-old /var/lib/dpkg/*-old 2>/dev/null || true
rm -rf /var/lib/apt/periodic/* 2>/dev/null || true
# Remove leftover backup copies created by our config changes (the packaged
# *.dpkg-dist / *.ucf-dist references and *.lindos-orig backups are kept).
find /etc \( -name '*.dpkg-old' -o -name '*.ucf-old' \) -type f -delete 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Identity: machine-id, ssh host keys (if any), random seed
# ---------------------------------------------------------------------------
log "resetting machine-id"
rm -f /etc/machine-id
touch /etc/machine-id
chmod 0444 /etc/machine-id
rm -f /var/lib/dbus/machine-id
# dbus falls back to /etc/machine-id when its own file is missing (systemd creates
# it at first boot).  Some tools expect the symlink, so recreate it as a symlink.
ln -s /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true
# ssh host keys only if no sshd is installed (sshd would not regenerate them)
if ! pkg_installed openssh-server; then
    rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
fi
rm -f /var/lib/systemd/random-seed 2>/dev/null || true
rm -f /var/lib/systemd/timers/stamp-* 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. resolv.conf restore
# ---------------------------------------------------------------------------
if [ -e /etc/resolv.conf.lindos-orig ] || [ -L /etc/resolv.conf.lindos-orig ]; then
    log "restoring original /etc/resolv.conf"
    rm -f /etc/resolv.conf
    mv -f /etc/resolv.conf.lindos-orig /etc/resolv.conf
else
    log "no resolv.conf backup to restore (host side handles it)"
fi

# ---------------------------------------------------------------------------
# 4. Logs, caches, history
# ---------------------------------------------------------------------------
log "truncating logs"
find /var/log -type f -exec truncate -s0 {} + 2>/dev/null || true
rm -rf /var/log/journal/* 2>/dev/null || true
rm -rf /var/crash/* /var/lib/apport/coredump/* 2>/dev/null || true
rm -rf /root/.cache /root/.wget-hsts /root/.bash_history /root/.python_history /root/.lesshst 2>/dev/null || true
rm -rf /root/.local/share/recently-used.xbel /root/.gnupg/*.lock 2>/dev/null || true
rm -rf /var/tmp/* /var/tmp/.[!.]* 2>/dev/null || true
# /tmp: everything except our own staging dir (removed by build-iso.sh after unmount)
find /tmp -mindepth 1 -maxdepth 1 ! -name lindos -exec rm -rf {} + 2>/dev/null || true
# Byte-compile the Lindos python code (root-owned __pycache__ so unprivileged
# users do not recompile on every start); harmless if already done by postinst.
if command -v py3compile >/dev/null 2>&1; then
    py3compile /usr/lib/python3/dist-packages/lindos 2>/dev/null || true
    for d in /usr/lib/lindos-setup /usr/lib/lindos-settings /usr/lib/lindos-compat /usr/lib/lindos-tune; do
        [ -d "${d}" ] && py3compile "${d}" 2>/dev/null || true
    done
fi
# fontconfig user cache dirs of root
rm -rf /root/.fontconfig /root/.cache 2>/dev/null || true
# leftover flatpak temp objects
rm -rf /var/tmp/flatpak-cache-* 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Sanity report
# ---------------------------------------------------------------------------
if have dpkg; then
    log "installed packages: $(dpkg-query -W 2>/dev/null | wc -l)"
fi
if have du; then
    log "root filesystem size (approx): $(du -sxh / 2>/dev/null | cut -f1)"
fi
sync
hook_end
