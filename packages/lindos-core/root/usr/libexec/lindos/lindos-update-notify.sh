#!/bin/bash
# lindos-update-notify.sh — read-only, unprivileged "Lindos updates are available" desktop
# toast (SPEC-UPDATE.md §37).
#
# Started two ways, both shipped elsewhere on purpose (this package, lindos-core, only ships
# the logic; the triggers live with the desktop session, SPEC §5 convention):
#   * /etc/xdg/autostart/lindos-update-notify.desktop (lindos-desktop) — once per XFCE login
#   * lindos-update-notify.timer, a systemd --user unit (this package) — every 6 hours so a
#     long-running session still notices updates that appeared after login
#
# This script only ever *reads*: `lindos-update check --json` is a read-only, unprivileged query
# (SPEC-UPDATE.md §36.2) that never touches the network unless a real apt repo is already
# configured, and this script never calls anything privileged, never installs anything, and
# never elevates. It shows at most one notify-send toast per boot for a given exact set of
# available lindos-* component updates -- a small marker file remembers what was already shown,
# so the 6-hour timer re-checking the same unchanged state never nags twice.
#
# Every external dependency is overridable for testing (LINDOS_UPDATE_BIN, NOTIFY_SEND_BIN,
# LINDOS_PYTHON, LINDOS_BOOT_ID) -- the same "injectable" spirit as lindos.update's own
# run=/which=/fetch= parameters, just at the shell layer.
set -Eeuo pipefail

LINDOS_UPDATE_BIN="${LINDOS_UPDATE_BIN:-lindos-update}"
NOTIFY_SEND_BIN="${NOTIFY_SEND_BIN:-notify-send}"
PY="${LINDOS_PYTHON:-python3}"

# --- where the "already notified this boot" marker lives --------------------------------------
state_dir() {
    # A boot-scoped tmpfs (XDG_RUNTIME_DIR) is nice -- it is cleared at reboot on its own -- but
    # is not guaranteed to be set (e.g. this script run outside a full graphical session), so
    # XDG_STATE_HOME/HOME is the durable fallback. Either way the marker's own content embeds
    # the boot id (below), so "once per boot" holds regardless of which directory is used.
    if [ -n "${XDG_RUNTIME_DIR:-}" ]; then
        printf '%s/lindos\n' "${XDG_RUNTIME_DIR}"
    else
        printf '%s/lindos\n' "${XDG_STATE_HOME:-${HOME}/.local/state}"
    fi
}

boot_id() {
    if [ -n "${LINDOS_BOOT_ID:-}" ]; then
        printf '%s' "${LINDOS_BOOT_ID}"
        return 0
    fi
    # best-effort: a non-Linux host or a locked-down /proc simply yields "" (the marker then
    # only throttles "per exact update set", never re-shown until the set itself changes --
    # honest degradation, never a crash).
    tr -d '\n\r' </proc/sys/kernel/random/boot_id 2>/dev/null || true
}

main() {
    if ! command -v "${LINDOS_UPDATE_BIN}" >/dev/null 2>&1; then
        exit 0   # lindos-core's CLI is not on PATH: nothing to check, never an error
    fi

    local json rc=0
    json="$("${LINDOS_UPDATE_BIN}" check --json 2>/dev/null)" || rc=$?
    # lindos-update's own exit codes: 0 = updates found, 3 = nothing to do -- both mean this
    # read-only check itself worked; 1 (error) / 2 (usage) mean it did not.
    if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 3 ]; then
        exit 0
    fi
    [ -n "${json}" ] || exit 0

    if ! command -v "${PY}" >/dev/null 2>&1; then
        exit 0   # cannot parse JSON without python3 -- never crash the session over this
    fi

    # a python interpreter with a text-mode stdout (notably on Windows, e.g. this repo's own
    # cross-platform pytest run) can emit CRLF; strip any '\r' the same way tests/run.sh's own
    # first_line() does, so parsing is identical everywhere.
    local raw
    raw="$(printf '%s' "${json}" | "${PY}" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    data = {}
updates = [u for u in (data.get("lindos_updates") or []) if isinstance(u, dict) and u.get("name")]
updates.sort(key=lambda u: u["name"])
signature = ",".join("%s=%s" % (u["name"], u.get("candidate", "")) for u in updates)
names = ",".join(u["name"] for u in updates)
print(signature)
print(names)
print(len(updates))
' 2>/dev/null | tr -d '\r')" || raw=""

    local fields=()
    while IFS= read -r line; do
        fields+=("${line}")
    done <<<"${raw}"

    local signature="${fields[0]:-}" names="${fields[1]:-}" count="${fields[2]:-0}"
    [ -n "${signature}" ] || exit 0   # no Lindos component updates right now -- nothing to show

    if ! command -v "${NOTIFY_SEND_BIN}" >/dev/null 2>&1; then
        exit 0   # no notification daemon reachable (e.g. headless/minimal session)
    fi

    local dir marker want
    dir="$(state_dir)"
    marker="${dir}/update-notify-seen"
    want="$(boot_id)|${signature}"
    if [ -f "${marker}" ] && [ "$(cat "${marker}" 2>/dev/null || true)" = "${want}" ]; then
        exit 0   # already shown this exact set of updates this boot
    fi

    mkdir -p "${dir}" 2>/dev/null || true
    "${NOTIFY_SEND_BIN}" -a "Lindos" -i software-update-available -u normal \
        "Lindos updates available" \
        "${count} component update(s): ${names} — open Settings > Updates" \
        >/dev/null 2>&1 || true
    printf '%s' "${want}" >"${marker}" 2>/dev/null || true
    exit 0
}

main "$@"
