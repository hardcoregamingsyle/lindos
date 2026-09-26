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
    # check NAME COMMAND...                  -- exit 0 is the only acceptable result
    # check NAME --ok CSV_CODES COMMAND...   -- any code listed in CSV_CODES also counts as OK
    #   (e.g. lindos-update's "check" subcommand deliberately exits 3, EXIT_NOTHING, when there
    #   is nothing to update -- a designed result, not a failure; see lindos-update's own
    #   EXIT_OK/EXIT_ERROR/EXIT_USAGE/EXIT_NOTHING constants)
    #
    # NOTE: the command runs as its own statement, and $? is captured on the very next line --
    # deliberately NOT via 'if "$@" ...; then ... fi' with no else, because when that condition
    # is false, POSIX defines the *if statement's own* exit status as 0 (no branch ran), which
    # clobbers $? back to 0 before it can be read after the 'fi'. That bug previously made every
    # failing check here silently report rc=0 regardless of the command's real exit code.
    local name="$1"; shift
    local ok="0"
    if [ "$1" = "--ok" ]; then
        ok="$2"; shift 2
    fi
    "$@" >/tmp/lindos-smoke-"${name}".log 2>&1
    local rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "LINDOS_CHECK ${name}=OK"
        return
    fi
    case ",${ok}," in
        *",${rc},"*) echo "LINDOS_CHECK ${name}=OK rc=${rc}" ;;
        *)           echo "LINDOS_CHECK ${name}=FAIL rc=${rc}"; RC=1 ;;
    esac
}

check_compat_doctor() {
    # lindos-compat doctor's exit code is EXIT_ERROR (1) if ANY 'required'-level check failed
    # (doctor.py: DoctorReport.ok / cmd_doctor). This smoke test runs as a systemd.run= oneshot
    # unit very early in boot, outside any logged-in desktop session -- so doctor's 'required'
    # "Graphical session (DISPLAY)" check genuinely and correctly reports failure here (Windows
    # programs really can't run without a desktop); that is expected in *this* invocation
    # context, not a real problem. Parse the JSON and only fail this check if some OTHER
    # required check failed.
    local logf="/tmp/lindos-smoke-lindos-compat-doctor.log"
    /usr/bin/lindos-compat doctor --json >"${logf}" 2>&1
    local rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "LINDOS_CHECK lindos-compat-doctor=OK"
        return
    fi
    if python3 -c "
import json, sys
with open('${logf}', encoding='utf-8') as fh:
    data = json.load(fh)
bad = [c.get('id') for c in data.get('checks', [])
       if c.get('level') == 'required' and not c.get('ok') and c.get('id') != 'display']
sys.exit(1 if bad else 0)
" 2>/dev/null; then
        echo "LINDOS_CHECK lindos-compat-doctor=OK rc=${rc} (no DISPLAY in this early-boot context, expected)"
    else
        echo "LINDOS_CHECK lindos-compat-doctor=FAIL rc=${rc}"
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
check lindos-config /usr/bin/lindos-config show --json
check lindos-ram /usr/bin/lindos-ram --json
check lindos-tune /usr/bin/lindos-tune status --json
check_compat_doctor
check lindos-run-version /usr/bin/lindos-run --version
check lindos-game-list /usr/bin/lindos-game list --json
check lindos-transfer-sources /usr/bin/lindos-transfer sources --json
check lindos-dualboot-status /usr/bin/lindos-dualboot status --json
check lindos-update-check --ok 3 /usr/bin/lindos-update check --json

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
