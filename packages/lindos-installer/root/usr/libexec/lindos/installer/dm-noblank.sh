#!/bin/sh
# ============================================================================
#  dm-noblank.sh - ubiquity-dm hook: keep the installer's own X server from blanking the screen.
#
#  Deployed by build/chroot/79-installer-flow.sh as
#      /usr/lib/ubiquity/dm-scripts/install/50lindos-noblank
#  The 'Install Lindos' boot entries (only-ubiquity) start NO desktop session: ubiquity.service runs
#  ubiquity-dm, which starts a bare X server ('X -br -ac -noreset -nolisten tcp') and the installer on it.
#  Nothing there configures the X server's screensaver or DPMS, whose defaults blank the display after
#  ten minutes without input - during the long download step the installer then looks asleep or hung.
#  lindos-live-inhibit.service does not help: a logind inhibitor cannot stop X's own timers (it only
#  covers suspend, hibernate and the lid).  The live DESKTOP (Try Lindos) has live-session-power.sh instead.
#
#  How ubiquity-dm runs this (bin/ubiquity-dm, run_hooks): once, in the installer's start-up, after X
#  is up (DISPLAY is set, X accepts local clients: it is started with -ac) and BEFORE the installer's
#  window exists, for every executable file of /usr/lib/ubiquity/dm-scripts/install whose name contains
#  no '.', in os.listdir() order, with subprocess.call: no shell, so the shebang and the exec bit
#  matter, the process is the live user (privileges dropped), it is waited for - a hang here would
#  hang the start of the installer - and its exit status is ignored.  The X server runs with -noreset,
#  so the settings stay when xset exits.  Only the install pass runs this directory (oem-config's first
#  boot uses dm-scripts/oem).
#
#  THE CONTRACT: no 'set -e', ALWAYS exit 0, everything time-boxed (a hung X server must not hang the
#  installer), nothing on stdout, a missing DISPLAY / xset / timeout means "do nothing".  Each setting
#  is its own xset call: a server without the DPMS extension refuses '-dpms' and must not take the
#  other two settings with it.
#
#  Test seams (unset on a real installation): LINDOS_XSET (the xset program), LINDOS_XSET_TIMEOUT
#  (seconds per call, default 5).
# ============================================================================

XSET="${LINDOS_XSET:-xset}"
LIMIT="${LINDOS_XSET_TIMEOUT:-5}"

say() {
    printf 'lindos-noblank: %s\n' "$*" >&2
}

if [ -z "${DISPLAY:-}" ]; then
    say "no DISPLAY - nothing to do"
    exit 0
fi
if ! command -v "${XSET}" >/dev/null 2>&1; then
    say "no xset - the display may blank after ten minutes"
    exit 0
fi
if ! command -v timeout >/dev/null 2>&1; then
    say "no timeout command - refusing to run an untimed call in the installer's start-up"
    exit 0
fi

failed=0
hung=0
for setting in "s off" "s noblank" "-dpms"; do
    # (unquoted on purpose: 's off' is two arguments)
    # shellcheck disable=SC2086
    timeout -k 1 "${LIMIT}" "${XSET}" ${setting} </dev/null >/dev/null 2>&1
    rc=$?
    if [ "${rc}" -ne 0 ]; then
        say "xset ${setting} failed (exit ${rc})"
        failed=1
        case "${rc}" in
            124|137) hung=1; break ;;   # the server does not answer: no second and third wait
        esac
    fi
done

# what the server says now (best effort, for the installer log): the screensaver timeout and DPMS
state=""
if [ "${hung}" = 0 ]; then
    state="$(timeout -k 1 "${LIMIT}" "${XSET}" q </dev/null 2>/dev/null | grep -E 'timeout:|DPMS is' | tr -s ' \n' ' ')"
fi
if [ "${failed}" = 0 ]; then
    say "screen blanking and DPMS are off (${state:-state unknown})"
else
    say "screen blanking may still be on (${state:-state unknown})"
fi
exit 0
