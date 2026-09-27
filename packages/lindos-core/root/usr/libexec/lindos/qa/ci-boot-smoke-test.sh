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
        *)
            echo "LINDOS_CHECK ${name}=FAIL rc=${rc}"
            # Self-diagnosing: the CI artifact only ever carries serial.log, never the guest's
            # own /tmp -- so a failure with nothing more than an rc is a dead end. Surface the
            # tail of this check's own captured output right into the console/serial log.
            tail -n 10 "/tmp/lindos-smoke-${name}.log" 2>/dev/null | while IFS= read -r line; do
                echo "LINDOS_FAIL_LOG ${name}: ${line}"
            done
            RC=1
            ;;
    esac
}

check_compat_doctor() {
    # lindos-compat doctor's exit code is EXIT_ERROR (1) if ANY 'required'-level check failed
    # (doctor.py: DoctorReport.ok / cmd_doctor). This smoke test runs as a systemd.run= oneshot
    # unit very early in boot, outside any logged-in desktop session -- so doctor's 'required'
    # "Graphical session (DISPLAY)" check genuinely and correctly reports failure here (Windows
    # programs really can't run without a desktop); that is expected in *this* invocation
    # context, not a real problem. Parse the JSON and only fail this check if some OTHER
    # required check failed -- and when it does, print exactly which one(s) and why, instead of
    # a bare rc (the JSON itself never leaves the guest otherwise; only serial.log does).
    #
    # stdout and stderr are captured to SEPARATE files, deliberately never merged with `2>&1`:
    # doctor.py's own module and the libraries it calls (lindos_compat.get_logger()) attach a
    # plain logging.StreamHandler(), which defaults to stderr and flushes every record
    # immediately, while doctor's --json payload is built with plain print() to stdout, which
    # Python fully block-buffers once it isn't a TTY (only flushed at process exit). Merged into
    # one file, any log record emitted during the run lands *before* the buffered JSON at exit --
    # corrupting the start of the file with non-JSON text. This was seen for real in CI (run
    # 36286387867: "Expecting value: line 1 column 1 (char 0)" even though the file's tail was
    # well-formed JSON) -- keeping the streams apart makes the JSON stream itself always pure.
    local logf="/tmp/lindos-smoke-lindos-compat-doctor.log"
    local errf="/tmp/lindos-smoke-lindos-compat-doctor.stderr.log"
    /usr/bin/lindos-compat doctor --json >"${logf}" 2>"${errf}"
    local rc=$?
    if [ "${rc}" -eq 0 ]; then
        echo "LINDOS_CHECK lindos-compat-doctor=OK"
        return
    fi
    # Runs as its own statement (never as an if-condition) for the same $?-capture reason as
    # check() above. Exit 0 = only 'display' (or nothing) failed; 1 = some other required check
    # failed (already printed as LINDOS_DOCTOR_FAIL lines below); 2 = couldn't even parse the
    # JSON (unexpected -- fall back to a raw log tail same as check()).
    python3 -c "
import json, sys
try:
    with open('${logf}', encoding='utf-8') as fh:
        data = json.load(fh)
    checks = data.get('checks', [])
except Exception as exc:
    print('LINDOS_DOCTOR_PARSE_ERROR ' + str(exc))
    sys.exit(2)
bad = [c for c in checks if c.get('level') == 'required' and not c.get('ok') and c.get('id') != 'display']
for c in bad:
    msg = (c.get('detail') or '').replace(chr(10), ' ').replace(chr(13), ' ')
    fix = (c.get('fix') or '').replace(chr(10), ' ').replace(chr(13), ' ')
    if fix:
        msg = (msg + ' ' if msg else '') + '(fix: ' + fix + ')'
    print('LINDOS_DOCTOR_FAIL id=' + str(c.get('id')) + ' msg=' + msg)
sys.exit(1 if bad else 0)
"
    local pyrc=$?
    if [ "${pyrc}" -eq 0 ]; then
        echo "LINDOS_CHECK lindos-compat-doctor=OK rc=${rc} (no DISPLAY in this early-boot context, expected)"
        return
    fi
    echo "LINDOS_CHECK lindos-compat-doctor=FAIL rc=${rc}"
    if [ "${pyrc}" -eq 2 ]; then
        tail -n 10 "${logf}" 2>/dev/null | while IFS= read -r line; do
            echo "LINDOS_FAIL_LOG lindos-compat-doctor(stdout): ${line}"
        done
        tail -n 10 "${errf}" 2>/dev/null | while IFS= read -r line; do
            echo "LINDOS_FAIL_LOG lindos-compat-doctor(stderr): ${line}"
        done
    fi
    RC=1
}

start_desktop_watch() {
    # Prints LINDOS_DESKTOP_READY once the live desktop is actually up (systemctl
    # is-system-running reports running/degraded AND a real X/lightdm session process exists),
    # or LINDOS_DESKTOP_READY_TIMEOUT if that never happens within its own internal bound. Read
    # by build/qa/boot_test.py, which waits for one of those two lines before its grace period +
    # screendump, instead of screenshotting mid-boot.
    #
    # MUST run as an INDEPENDENT unit/process, never inline in this script: this script is the
    # ExecStart of a systemd.run= transient service that is itself one of default.target's start
    # jobs, so 'is-system-running' can never report running/degraded while THIS unit is still
    # active -- waiting for that in-line here would deadlock boot forever. systemd-run --no-block
    # starts the watcher as its own separate unit and returns immediately, letting this script
    # (and its unit) finish normally; setsid'd background process is the fallback if systemd-run
    # is somehow unavailable.
    local watch_script="/tmp/lindos-desktop-watch.sh"
    cat > "${watch_script}" <<'WATCH_EOF'
#!/bin/bash
exec >/dev/console 2>&1
i=0
while [ "${i}" -lt 150 ]; do
    state="$(systemctl is-system-running 2>/dev/null || true)"
    if [ "${state}" = "running" ] || [ "${state}" = "degraded" ]; then
        if pgrep -x Xorg >/dev/null 2>&1 || pgrep -x lightdm >/dev/null 2>&1 \
           || pgrep -x Xwayland >/dev/null 2>&1; then
            echo "LINDOS_DESKTOP_READY"
            exit 0
        fi
    fi
    sleep 1
    i=$((i + 1))
done
echo "LINDOS_DESKTOP_READY_TIMEOUT"
WATCH_EOF
    chmod +x "${watch_script}"
    # Launched via BOTH systemd-run --no-block (a real, independent unit -- immune to whatever
    # cgroup cleanup systemd does to THIS unit's own children once its main process exits) AND a
    # setsid'd background process (works even where systemd-run/its bus connection isn't usable
    # this early in boot). Harmless if both get through: the watcher only ever prints its one
    # outcome once and exits, and tail_for_desktop_ready() on the host just needs ANY matching
    # line. The systemd-run attempt's own result is logged (LINDOS_INFO desktop_watch_launch=...)
    # so a run where NEITHER sentinel ever appears is diagnosable instead of a silent mystery.
    if command -v systemd-run >/dev/null 2>&1; then
        local sdr_out sdr_rc
        sdr_out="$(systemd-run --no-block --unit=lindos-desktop-watch --collect \
            /bin/bash "${watch_script}" 2>&1)"
        sdr_rc=$?
        echo "LINDOS_INFO desktop_watch_launch=systemd-run rc=${sdr_rc} out=${sdr_out:-<empty>}"
    else
        echo "LINDOS_INFO desktop_watch_launch=no-systemd-run"
    fi
    setsid /bin/bash "${watch_script}" </dev/null >/dev/console 2>&1 &
    disown 2>/dev/null || true
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

start_desktop_watch

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
