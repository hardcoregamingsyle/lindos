#!/bin/bash
# browser-firstboot.sh — the SILENT retry for Google Chrome on the installed system (SPEC §0.1,
# §4.4, §6, §8).
#
# Google's licence forbids shipping Chrome on the ISO, so it is downloaded from Google's official
# apt repository.  The INSTALLER normally does that (its result is in install-state.json, step
# 'browser'); this script only covers what the installer could not do (offline, timeout, failure):
#   * install-state says 'done' or 'skipped' -> nothing to do, write the marker and exit at once;
#   * 'pending', 'failed' or no record       -> retry via the existing install-browser.sh helper
#     (same repo/key logic as the OOBE and 'lindos-browser install chrome' - never duplicated
#     here) while online (it first waits a bounded time for NetworkManager: wait-for-network),
#     and record the outcome with 'lindos.installstate mark'.
# It never shows anything: no window, no wizard, no notification.
#
# Called by lindos-browser-firstboot.service (oneshot, After=network-online.target).
#
# Safety:
#   * never runs in the live/ISO session — the systemd unit's ConditionKernelCommandLine already
#     refuses to start while 'boot=casper' is present, and this script re-asks the shared
#     is-live-session helper itself so it is also safe to run by hand for testing.
#   * never runs while Ubiquity's oem-config first-boot wizard is still pending (the new user's
#     account does not exist yet) - the shared oem-config-pending helper says so.
#   * idempotent / run-once — guarded by a marker file, only ever written on a *terminal*
#     outcome (success, "installer already did it", or "nothing to do because the user picked
#     another browser"); offline or a failed install leaves the marker absent so systemd retries
#     on a later boot.
#   * never blocks or fails the boot — every exit path is 0 (best effort), all output is logged.
#   * sets Chrome as the SYSTEM-WIDE default for new users (via /etc/xdg/mimeapps.list, the
#     standard xdg fallback consulted by any account without its own ~/.config/mimeapps.list) —
#     it never touches an existing user's personal choice (xdg-settings/OOBE already covered that
#     in the user's own session; this script runs as root with no user/session/$DISPLAY at all).
#
# Usage: browser-firstboot.sh [--force]
#   --force   ignore the marker file and run again (testing only)
# The installer may also run it inside 'chroot /target' with LINDOS_INSTALLER=1 (the kernel
# command line still says boot=casper there); the explicit flag then counts as "not live".
set -Eeuo pipefail

ROOT="${LINDOS_ROOT:-}"
LIBEXEC="${ROOT}/usr/libexec/lindos"
IS_LIVE_SESSION="${LIBEXEC}/is-live-session"
OEM_CONFIG_PENDING="${LIBEXEC}/oem-config-pending"
WAIT_FOR_NETWORK="${LIBEXEC}/wait-for-network"
PY="${LINDOS_PYTHON:-python3}"
STATE_DIR="${ROOT}/var/lib/lindos"
MARKER="${STATE_DIR}/browser-firstboot.done"
SYSTEM_JSON="${ROOT}/etc/lindos/system.json"
LOG_DIR="${ROOT}/var/log/lindos"
LOG_FILE="${LOG_DIR}/browser-firstboot.log"
INSTALL_BROWSER="${LIBEXEC}/install-browser.sh"
MIMEAPPS="${ROOT}/etc/xdg/mimeapps.list"
FORCE=0

log() {
    printf 'lindos-browser firstboot: %s\n' "$*" >&2
    if [ -d "${LOG_DIR}" ] || mkdir -p "${LOG_DIR}" 2>/dev/null; then
        printf '%s %s\n' "$(date '+%F %T')" "$*" >>"${LOG_FILE}" 2>/dev/null || true
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# Extra safety net even though the unit's ConditionKernelCommandLine already guards this: never
# touch a live/casper session (e.g. if invoked by hand for testing).  The answer comes from the
# shared is-live-session helper (LINDOS_TEST_CMDLINE points it at a fake file in the hermetic
# tests); if that helper is missing we cannot tell, and the safe answer is "live".
# Exception: the installer runs this inside 'chroot /target' with LINDOS_INSTALLER=1, where
# the kernel command line still says boot=casper (proc is the live kernel's) but the target IS the
# installed system - that explicit flag wins over the kernel command line.
is_live_session() {
    [ "${LINDOS_INSTALLER:-}" = 1 ] && return 1
    [ -f "${IS_LIVE_SESSION}" ] || return 0
    bash "${IS_LIVE_SESSION}"
}

# True while the oem-config first-boot wizard has not finished (shared helper; "not pending" when
# the helper is missing - the unit's own ConditionPathExists covers that case).
oem_config_pending() {
    [ -f "${OEM_CONFIG_PENDING}" ] && bash "${OEM_CONFIG_PENDING}"
}

# False only when NetworkManager is running and no connection came up within the helper's bounded
# wait.  Lindos masks NetworkManager-wait-online, so network-online.target (the unit's After=) is
# reached before Wi-Fi/DHCP is up; without this wait the retry would find "offline" on every boot.
# "Cannot tell" (helper missing, no NetworkManager) counts as online: install-browser.sh probes itself.
network_ready() {
    [ -f "${WAIT_FOR_NETWORK}" ] || return 0
    bash "${WAIT_FOR_NETWORK}"
}

# install-state.json (lindos.installstate; honours LINDOS_ROOT): the recorded status of the
# 'browser' step, or "" when unknown / the module is unavailable.  Never fails the caller.
state_status() {
    "${PY}" -m lindos.installstate status browser 2>/dev/null | tr -d '\r' || true
}

# state_mark <status> [detail] — record the outcome; a failure to record is only a warning.
state_mark() {
    "${PY}" -m lindos.installstate mark browser "$1" "${2:-}" >/dev/null 2>&1 || \
        log "warning: could not record install-state browser=$1 (non-fatal)"
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
    if oem_config_pending; then
        log "the oem-config first-boot wizard is still pending — not before the account exists"
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

    local want recorded
    want="$(wanted_browser)"
    # The installer already did (or consciously skipped) this step: nothing left to retry.  When it
    # installed Chrome and Chrome is still the configured browser, make sure new users default to
    # it (an idempotent edit of /etc/xdg/mimeapps.list) - the only part the installer leaves to us.
    recorded="$(state_status)"
    case "${recorded}" in
        done|skipped)
            log "install-state says browser=${recorded} — the installer handled it, nothing to retry"
            if [ "${recorded}" = "done" ] && [ "${want}" = "chrome" ] && have python3; then
                set_system_default_browser "google-chrome.desktop"
            fi
            : >"${MARKER}"
            exit 0
            ;;
    esac

    if [ "${want}" != "chrome" ]; then
        log "configured default browser is '${want}', not chrome — nothing to do"
        state_mark skipped "default browser is ${want}"
        : >"${MARKER}"
        exit 0
    fi

    if [ ! -f "${INSTALL_BROWSER}" ]; then
        log "error: ${INSTALL_BROWSER} not found — will retry on a later boot"
        state_mark failed "install-browser.sh missing"
        exit 0
    fi

    if ! network_ready; then
        log "no network connection yet — will retry on a later boot"
        state_mark pending "offline at first boot"
        exit 0
    fi

    # Reuse install-browser.sh entirely (repo/key/apt logic, and its own online check +
    # exit-code 3 for offline) — never duplicated here. rc: 0 ok · 3 offline · else failure;
    # both non-zero cases leave the marker unwritten so systemd retries on a later boot.
    log "retrying the Google Chrome install via ${INSTALL_BROWSER} (install-state: ${recorded:-none})"
    set +e
    bash "${INSTALL_BROWSER}" chrome
    rc=$?
    set -e
    if [ "${rc}" -eq 0 ]; then
        log "Google Chrome installed"
    elif [ "${rc}" -eq 3 ]; then
        log "offline — will retry on a later boot (network-online.target)"
        state_mark pending "offline at first boot"
        exit 0
    else
        log "install-browser.sh chrome failed (exit ${rc}) — will retry on a later boot"
        state_mark failed "install-browser.sh exit ${rc}"
        exit 0
    fi

    if have python3; then
        set_system_default_browser "google-chrome.desktop"
    else
        log "python3 unavailable — skipped setting the system-wide default browser for new users"
    fi

    state_mark "done" "google-chrome-stable installed at first boot"
    : >"${MARKER}"
    log "done"
    exit 0
}

main "$@"
