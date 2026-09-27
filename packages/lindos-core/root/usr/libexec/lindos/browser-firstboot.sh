#!/bin/bash
# browser-firstboot.sh — install Google Chrome from Google's official apt repository, once, on
# the first boot of the INSTALLED system (SPEC §0.1, §4.4, §6, §8).
#
# Called by lindos-browser-firstboot.service (oneshot, After=network-online.target). Google's
# licence forbids shipping Chrome on the ISO, so this is the legal path: it reuses the existing
# install-browser.sh helper (same repo/key logic as the OOBE and 'lindos-browser install chrome'
# — never duplicated here) to add Google's signed apt repository and install google-chrome-stable.
#
# Safety:
#   * never runs in the live/ISO session — the systemd unit's ConditionKernelCommandLine already
#     refuses to start while 'boot=casper' is present, and this script re-checks /proc/cmdline
#     itself so it is also safe to run by hand for testing.
#   * idempotent / run-once — guarded by a marker file, only ever written on a *terminal*
#     outcome (success, or "nothing to do because the user picked another browser"); offline or a
#     failed install leaves the marker absent so systemd retries on a later boot.
#   * never blocks or fails the boot — every exit path is 0 (best effort), all output is logged.
#   * sets Chrome as the SYSTEM-WIDE default for new users (via /etc/xdg/mimeapps.list, the
#     standard xdg fallback consulted by any account without its own ~/.config/mimeapps.list) —
#     it never touches an existing user's personal choice (xdg-settings/OOBE already covered that
#     in the user's own session; this script runs as root with no user/session/$DISPLAY at all).
#
# Usage: browser-firstboot.sh [--force]
#   --force   ignore the marker file and run again (testing only)
set -Eeuo pipefail

STATE_DIR="${LINDOS_ROOT:-}/var/lib/lindos"
MARKER="${STATE_DIR}/browser-firstboot.done"
SYSTEM_JSON="${LINDOS_ROOT:-}/etc/lindos/system.json"
LOG_DIR="${LINDOS_ROOT:-}/var/log/lindos"
LOG_FILE="${LOG_DIR}/browser-firstboot.log"
INSTALL_BROWSER="${LINDOS_ROOT:-}/usr/libexec/lindos/install-browser.sh"
MIMEAPPS="${LINDOS_ROOT:-}/etc/xdg/mimeapps.list"
FORCE=0

log() {
    printf 'lindos-browser firstboot: %s\n' "$*" >&2
    if [ -d "${LOG_DIR}" ] || mkdir -p "${LOG_DIR}" 2>/dev/null; then
        printf '%s %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# Extra safety net even though the unit's ConditionKernelCommandLine already guards this: never
# touch a live/casper session (e.g. if invoked by hand for testing).  LINDOS_TEST_CMDLINE lets the
# hermetic test suite point this at a fake file instead of the real /proc/cmdline; never set on a
# real system.
is_live_session() {
    local cmdline="${LINDOS_TEST_CMDLINE:-/proc/cmdline}"
    [ -r "${cmdline}" ] && grep -Eq '(^| )boot=casper( |$)' "${cmdline}" 2>/dev/null
}

# Configured default browser id from /etc/lindos/system.json; "chrome" (the shipped default) if
# the file is missing/unreadable/malformed — never crashes this script either way.
wanted_browser() {
    if [ ! -f "${SYSTEM_JSON}" ]; then
        echo "chrome"
        return 0
    fi
    if have python3; then
        python3 - "${SYSTEM_JSON}" <<'PY' 2>/dev/null || echo "chrome"
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        val = json.load(fh).get("browser", "chrome")
    print(val if val in ("edge", "chrome", "firefox") else "chrome")
except Exception:
    print("chrome")
PY
    else
        echo "chrome"
    fi
}

# set_system_default_browser <desktop-id> — make <desktop-id> the fallback default for any
# account without its own ~/.config/mimeapps.list, by setting the keys in
# /etc/xdg/mimeapps.list's [Default Applications] group (creating the file/section if needed).
# Safe to call with no user session/$DISPLAY: unlike xdg-settings, this only edits a system file.
set_system_default_browser() {
    local desktop="$1"
    mkdir -p "$(dirname "${MIMEAPPS}")" 2>/dev/null || true
    LINDOS_MIMEAPPS="${MIMEAPPS}" LINDOS_DESKTOP="${desktop}" python3 - <<'PY' 2>/dev/null || \
        log "warning: could not update ${MIMEAPPS} (non-fatal)"
import os

dst = os.environ["LINDOS_MIMEAPPS"]
desktop = os.environ["LINDOS_DESKTOP"]
keys = ("x-scheme-handler/http", "x-scheme-handler/https", "text/html", "application/xhtml+xml")

lines = []
if os.path.exists(dst):
    with open(dst, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

# find (or create) the [Default Applications] section
start = None
for i, raw in enumerate(lines):
    if raw.strip() == "[Default Applications]":
        start = i
        break
if start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("[Default Applications]")
    start = len(lines) - 1

end = len(lines)
for i in range(start + 1, len(lines)):
    if lines[i].strip().startswith("[") and lines[i].strip().endswith("]"):
        end = i
        break

section = lines[start + 1:end]
new_section = []
seen = set()
for ln in section:
    if "=" in ln and not ln.strip().startswith("#"):
        k = ln.split("=", 1)[0].strip()
        if k in keys:
            new_section.append(f"{k}={desktop}")
            seen.add(k)
            continue
    new_section.append(ln)
# insert missing keys before the section's trailing blank lines (keeps the file tidy)
pos = len(new_section)
while pos > 0 and not new_section[pos - 1].strip():
    pos -= 1
for k in keys:
    if k not in seen:
        new_section.insert(pos, f"{k}={desktop}")
        pos += 1

lines[start + 1:end] = new_section
tmp = dst + ".lindos-tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines).rstrip("\n") + "\n")
os.chmod(tmp, 0o644)
os.replace(tmp, dst)
PY
    log "system-wide default browser (new users) set to ${desktop} in ${MIMEAPPS}"
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) FORCE=1 ;;
            -h|--help) printf 'usage: browser-firstboot.sh [--force]\n'; exit 0 ;;
            *) printf 'browser-firstboot.sh: unknown argument %s\n' "$1" >&2; exit 2 ;;
        esac
        shift
    done
    # Read-only guards first (checked regardless of privilege, so they are honestly "no" even
    # if invoked as root by mistake — and so the hermetic tests can exercise them without root):
    if is_live_session; then
        log "live/ISO session detected (boot=casper) — refusing to run (installed systems only)"
        exit 0
    fi
    if [ "${FORCE}" != 1 ] && [ -e "${MARKER}" ]; then
        log "already done (${MARKER}); use --force to re-run"
        exit 0
    fi
    if [ "$(id -u)" != 0 ]; then
        log "error: must run as root"
        exit 0
    fi
    mkdir -p "${STATE_DIR}" "${LOG_DIR}" 2>/dev/null || true
    chmod 0755 "${STATE_DIR}" 2>/dev/null || true

    local want
    want="$(wanted_browser)"
    if [ "${want}" != "chrome" ]; then
        log "configured default browser is '${want}', not chrome — nothing to do"
        : >"${MARKER}"
        exit 0
    fi

    if [ ! -f "${INSTALL_BROWSER}" ]; then
        log "error: ${INSTALL_BROWSER} not found — will retry on a later boot"
        exit 0
    fi

    # Reuse install-browser.sh entirely (repo/key/apt logic, and its own online check +
    # exit-code 3 for offline) — never duplicated here. rc: 0 ok · 3 offline · else failure;
    # both non-zero cases leave the marker unwritten so systemd retries on a later boot.
    log "installing Google Chrome via ${INSTALL_BROWSER}"
    set +e
    bash "${INSTALL_BROWSER}" chrome
    rc=$?
    set -e
    if [ "${rc}" -eq 0 ]; then
        log "Google Chrome installed"
    elif [ "${rc}" -eq 3 ]; then
        log "offline — will retry on a later boot (network-online.target)"
        exit 0
    else
        log "install-browser.sh chrome failed (exit ${rc}) — will retry on a later boot"
        exit 0
    fi

    if have python3; then
        set_system_default_browser "google-chrome.desktop"
    else
        log "python3 unavailable — skipped setting the system-wide default browser for new users"
    fi

    : >"${MARKER}"
    log "done"
    exit 0
}

main "$@"
