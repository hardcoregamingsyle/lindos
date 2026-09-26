#!/bin/bash
# ============================================================================
#  ci-boot-smoke-test.sh — QA-only boot smoke test invoked by CI (SPEC-UPDATE
#  Addendum U is silent about this file; it is a GitHub Actions boot-test
#  helper, not a user-facing tool).
#
#  Invoked via the kernel command line: `systemd.run=/usr/libexec/lindos/qa/
#  ci-boot-smoke-test.sh`. systemd's kernel-command-line generator turns that
#  into a transient oneshot service that runs alongside the normal boot
#  (default.target), so this never delays or blocks the real desktop from
#  starting. It is READ-ONLY — it never installs, modifies or deletes
#  anything on the system — and harmless if ever run by hand on a real
#  install (it just prints a report and exits 0).
#
#  Everything is written straight to /dev/console so it lands on QEMU's
#  serial log regardless of how systemd would otherwise capture this unit's
#  stdout (journal vs console differs by systemd version/config).
#
#  No `set -e`: one failing check must not stop the rest from running — the
#  point is a single combined PASS/FAIL report, not an early abort.
# ============================================================================
exec >/dev/console 2>&1

echo "LINDOS_SMOKE_START"
RC=0

check() {
    # check NAME -- COMMAND...   (everything after NAME is the command/argv)
    local name="$1"; shift
    if "$@" >/tmp/lindos-smoke-"${name}".log 2>&1; then
        echo "LINDOS_CHECK ${name}=OK"
    else
        echo "LINDOS_CHECK ${name}=FAIL rc=$?"
        RC=1
    fi
}

echo "LINDOS_INFO uname=$(uname -r)"
if [ -r /etc/lindos-release ]; then
    echo "LINDOS_INFO release=$(cat /etc/lindos-release)"
fi
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "LINDOS_INFO os_release_id=${ID:-unknown} version=${VERSION_ID:-unknown} pretty=${PRETTY_NAME:-unknown}"
fi

check python-import python3 -c "import lindos, lindos.paths, lindos.modes, lindos.config, lindos.hardware, lindos.ram, lindos.theme, lindos.compat, lindos.browsers"
check lindos-mode /usr/bin/lindos-mode list --json
check lindos-config /usr/bin/lindos-config get --json
check lindos-ram /usr/bin/lindos-ram --json
check lindos-tune /usr/bin/lindos-tune status --json
check lindos-compat-doctor /usr/bin/lindos-compat doctor --json
check lindos-run-version /usr/bin/lindos-run --version
check lindos-game-list /usr/bin/lindos-game list --json
check lindos-transfer-sources /usr/bin/lindos-transfer sources --json
check lindos-dualboot-status /usr/bin/lindos-dualboot status --json
check lindos-update-check /usr/bin/lindos-update check --json

failed_units="$(systemctl --failed --no-legend --plain 2>/dev/null | wc -l)"
echo "LINDOS_INFO failed_units=${failed_units}"
if [ -n "${failed_units}" ] && [ "${failed_units}" -gt 0 ]; then
    systemctl --failed --no-legend --plain 2>/dev/null | while IFS= read -r line; do
        echo "LINDOS_FAILED_UNIT ${line}"
    done
fi

is_system_running="$(systemctl is-system-running 2>/dev/null || true)"
echo "LINDOS_INFO is_system_running=${is_system_running}"

echo "LINDOS_SMOKE_DONE rc=${RC}"
exit 0
