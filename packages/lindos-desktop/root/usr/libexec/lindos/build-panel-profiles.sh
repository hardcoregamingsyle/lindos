#!/bin/bash
# build-panel-profiles.sh — generate /usr/share/lindos/modes/<id>/panel.tar.bz2 for every
# Lindos mode from the shipped panel/ directories (SPEC §3, §13: called by
# build/chroot/40-theme.sh inside the chroot and by the lindos-desktop postinst).
#
# The tarballs are real xfce4-panel-profiles profiles (config.txt + rc files) so that
# `xfce4-panel-profiles load <tarball>` (used by lindos.modes.apply_mode) works.  They are
# produced by the pure-python packer /usr/libexec/lindos/panel-profile-pack.py; when
# PyGObject is installed every value is additionally round-tripped through GLib.Variant.
# If python3 is missing nothing is generated and the panel/ directory stays as the
# documented fallback (apply_mode copies the xml + rc files and restarts the panel).
#
# Usage: build-panel-profiles.sh [--modes-dir DIR] [--force] [--quiet] [--verify|--no-verify]
#        Exit 0 on success (or when nothing can be done but the fallback is intact),
#        1 when a profile failed to build.
set -Eeuo pipefail

MODES_DIR="${LINDOS_MODES_DIR:-/usr/share/lindos/modes}"
PACKER="${LINDOS_PANEL_PACKER:-$(dirname "$(readlink -f "$0")")/panel-profile-pack.py}"
FORCE=0
QUIET=0
VERIFY=auto

log() { [ "${QUIET}" -eq 1 ] || printf 'build-panel-profiles: %s\n' "$*" >&2; }
warn() { printf 'build-panel-profiles: WARNING: %s\n' "$*" >&2; }
die() { printf 'build-panel-profiles: ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --modes-dir) [ $# -ge 2 ] || die "--modes-dir needs an argument"; MODES_DIR="$2"; shift 2 ;;
        --modes-dir=*) MODES_DIR="${1#*=}"; shift ;;
        --force|-f) FORCE=1; shift ;;
        --quiet|-q) QUIET=1; shift ;;
        --verify) VERIFY=yes; shift ;;
        --no-verify) VERIFY=no; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

[ -d "${MODES_DIR}" ] || die "modes directory not found: ${MODES_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found; leaving panel/ directories as the fallback (no panel.tar.bz2 generated)"
    exit 0
fi
[ -f "${PACKER}" ] || die "packer not found: ${PACKER}"

verify_flag=()
case "${VERIFY}" in
    yes) verify_flag=(--verify) ;;
    no) ;;
    auto)
        if python3 -c 'import gi' >/dev/null 2>&1; then
            verify_flag=(--verify)
        fi
        ;;
esac

quiet_flag=()
[ "${QUIET}" -eq 1 ] && quiet_flag=(--quiet)

failures=0
built=0
skipped=0
for mode_dir in "${MODES_DIR}"/*/; do
    mode_dir="${mode_dir%/}"
    mode="$(basename "${mode_dir}")"
    panel_dir="${mode_dir}/panel"
    xml="${panel_dir}/xfce4-panel.xml"
    out="${mode_dir}/panel.tar.bz2"
    if [ ! -f "${xml}" ]; then
        log "${mode}: no panel/xfce4-panel.xml — skipped"
        continue
    fi
    if [ "${FORCE}" -eq 0 ] && [ -f "${out}" ]; then
        # rebuild only when any source is newer than the tarball
        newer="$(find "${panel_dir}" -type f -newer "${out}" -print -quit 2>/dev/null || true)"
        if [ -z "${newer}" ]; then
            log "${mode}: ${out} is up to date"
            skipped=$((skipped + 1))
            continue
        fi
    fi
    if python3 "${PACKER}" ${quiet_flag[@]+"${quiet_flag[@]}"} pack "${panel_dir}" "${out}" ${verify_flag[@]+"${verify_flag[@]}"}; then
        chmod 0644 "${out}" 2>/dev/null || true
        built=$((built + 1))
        log "${mode}: wrote ${out}"
    else
        warn "${mode}: failed to build ${out}; panel/ fallback stays usable"
        rm -f "${out}" "${out}.tmp"
        failures=$((failures + 1))
    fi
done

log "done: ${built} built, ${skipped} up to date, ${failures} failed"
[ "${failures}" -eq 0 ]
