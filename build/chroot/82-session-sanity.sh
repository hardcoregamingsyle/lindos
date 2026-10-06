#!/bin/bash
# ============================================================================
#  82-session-sanity.sh - can this image start an XFCE session at all?
#
#  Runs INSIDE the squashfs chroot as root, as the very last hook (81-unrecognisable-gate.sh only reports what looks
#  like Mint) and only READS.  The report goes to the hook log, out/hooks/82-session-sanity.log.
#
#  Why it exists: the CI boot test of the ISO built from 7fc3aae ended on xfce4-session's "Unable to load a failsafe
#  session - Unable to determine failsafe session name".  Lindos's xfce4-session.xml (and three more xfconf defaults:
#  xsettings, xfce4-keyboard-shortcuts, xfce4-power-manager - each also a conffile of a stock package) had never been
#  installed: dpkg kept the stock conffile as "deleted" and parked Lindos's copy as *.dpkg-dist.  Linux Mint's
#  /etc/xdg/xdg-xfce -> mint-artwork link used to hide that (it carries an xfce4-session.xml of its own); taking Mint's
#  defaults out of the way exposed it.  This hook fails the build for that kind of mistake instead of an hour of
#  CI later.
#
#  Lines: SESSION-FAIL [category] detail   unambiguous: the build stops at the end of this hook
#         SESSION-WARN [category] detail   report only
#         SESSION-NOTE [category] detail   informational
#         SESSION-SANITY failures=N warnings=M mode=strict|report-only
#
#  Checks:
#    packages   what a graphical session cannot start without (FAIL): xfce4-session xfwm4 xfce4-panel xfdesktop4
#               xfconf xfce4-settings, lightdm and a greeter, an X server, a session bus (dbus-x11 or
#               dbus-user-session); libpam-systemd, network-manager and plymouth are warnings
#    files      the binaries and entries behind those packages (FAIL): the xfconfd binary and its D-Bus activation
#               file (without it "xfconfd isn't running"), xfsettingsd, the .desktop files lightdm.conf names
#    xfconf     every Lindos channel file exists in /etc/xdg/xfce4/xfconf/xfce-perchannel-xml (not only as
#               *.dpkg-dist) and parses (FAIL; a stock file that does not parse is a warning); xfce4-session.xml
#               defines /general/FailsafeSessionName and sessions/Failsafe, looked up in the directories an XFCE
#               session reads (/etc/xdg/xdg-xfce, /etc/xdg)
#    xdg        /etc/xdg/xdg-xfce, when it exists, is a plain directory (a link, or a directory that carries xfconf
#               defaults of its own, is a warning: it outranks /etc/xdg/xfce4)
#
#  LINDOS_SESSION_SANITY=report never fails the build, it only reports; =0 skips the hook.  The hooks run under env -i:
#  the variable reaches this one only when it is listed in LINDOS_PASSTHRU_VARS.
#  Test seams (unset in real builds): LINDOS_MINT_ROOT (prefix for every path), LINDOS_HOOK_PATH_PREFIX (lib.sh:
#  fake dpkg-query first in PATH), LINDOS_PYTHON (the interpreter that parses the XML; default python3).
# ============================================================================
set -Eeuo pipefail
# shellcheck source=build/chroot/lib.sh
. "$(dirname "$(readlink -f "$0")")/lib.sh"

hook_begin "session sanity"

ROOT="${LINDOS_MINT_ROOT:-}"
: "${LINDOS_SESSION_SANITY:=1}"
PYTHON="${LINDOS_PYTHON:-python3}"

# The report describes the image, not the machine that builds it.
if [ -z "${ROOT}" ] && [ "${LINDOS_CHROOT:-}" != "1" ] && ! in_chroot; then
    die "refusing to inspect the build host outside the build chroot (set LINDOS_MINT_ROOT for a dry run)"
fi

if [ "${LINDOS_SESSION_SANITY}" = "0" ]; then
    log "LINDOS_SESSION_SANITY=0 - the session sanity check is switched off"
    hook_end
    exit 0
fi

FAILURES=0
WARNINGS=0

rel() { printf '%s' "${1#"${ROOT}"}"; }
fail() { FAILURES=$(( FAILURES + 1 )); warn "SESSION-FAIL [$1] $2"; }
softwarn() { WARNINGS=$(( WARNINGS + 1 )); warn "SESSION-WARN [$1] $2"; }
note() { log "SESSION-NOTE [$1] $2"; }

# ---------------------------------------------------------------------------
# rules (one place; the tests keep them in step with 76-mint-purge.sh and lindos-desktop)
# ---------------------------------------------------------------------------
# no graphical session without these
REQUIRED_PKGS="xfce4-session xfwm4 xfce4-panel xfdesktop4 xfconf xfce4-settings lightdm"
# the channels lindos-desktop ships in /etc/xdg/xfce4/xfconf/xfce-perchannel-xml (what its dpkg list names is added)
LINDOS_CHANNELS="xfce4-session xsettings xfwm4 xfce4-panel xfce4-desktop xfce4-keyboard-shortcuts xfce4-notifyd xfce4-power-manager thunar keyboards"
PERCHANNEL="/etc/xdg/xfce4/xfconf/xfce-perchannel-xml"

INSTALLED=""
load_installed() {
    # shellcheck disable=SC2016  # dpkg-query's own ${...} format, not shell
    INSTALLED="$(dpkg-query -W -f='${db:Status-Status} ${Package}\n' 2>/dev/null | awk '$1=="installed"{print $2}' | sed 's/:.*$//' | sort -u || true)"
}
is_installed() { grep -qx -- "$1" <<< "${INSTALLED}"; }

# ---------------------------------------------------------------------------
# 1. packages
# ---------------------------------------------------------------------------
check_packages() {
    local p
    load_installed
    if [ -z "${INSTALLED}" ]; then
        softwarn packages "dpkg-query lists no installed package: the package checks are skipped"
        return 0
    fi
    for p in ${REQUIRED_PKGS}; do
        is_installed "${p}" || fail packages "${p} is not installed (a graphical XFCE session cannot start without it; see MINT-PURGE-* in out/hooks/76-mint-purge.log)"
    done
    if ! grep -Eq '^[a-z0-9.+-]*greeter[a-z0-9.+-]*$' <<< "${INSTALLED}"; then
        fail packages "no LightDM greeter is installed (slick-greeter is the Lindos one)"
    fi
    if ! is_installed xserver-xorg-core && ! is_installed xorg; then
        fail packages "no X server (xserver-xorg-core) is installed"
    fi
    if ! is_installed dbus-x11 && ! is_installed dbus-user-session; then
        fail packages "neither dbus-x11 nor dbus-user-session is installed: the session has no D-Bus session bus and xfconfd cannot be started"
    fi
    for p in libpam-systemd network-manager plymouth; do
        is_installed "${p}" || softwarn packages "${p} is not installed"
    done
    return 0
}

# ---------------------------------------------------------------------------
# 2. binaries and session entries
# ---------------------------------------------------------------------------
# need_file WHAT PATH... - FAIL when none of the (globbed) paths exists
need_file() {
    local what="$1" pat shown=""
    shift
    for pat in "$@"; do
        if compgen -G "${pat}" > /dev/null; then
            return 0
        fi
        shown="${shown}${shown:+ or }$(rel "${pat}")"
    done
    fail files "${what} is missing (${shown})"
    return 0
}

# conf_value KEY FILE... - the last KEY=value of the LightDM configuration files
conf_value() {
    local key="$1" f
    shift
    for f in "$@"; do
        [ -f "${f}" ] || continue
        sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "${f}" | tail -n 1 | tr -d '\r'
    done | tail -n 1
}

check_files() {
    local greeter session
    need_file "xfce4-session" "${ROOT}/usr/bin/xfce4-session"
    need_file "startxfce4" "${ROOT}/usr/bin/startxfce4"
    need_file "xfwm4" "${ROOT}/usr/bin/xfwm4"
    need_file "xfce4-panel" "${ROOT}/usr/bin/xfce4-panel"
    need_file "xfdesktop" "${ROOT}/usr/bin/xfdesktop"
    need_file "xfsettingsd (xfce4-settings)" "${ROOT}/usr/bin/xfsettingsd"
    need_file "the xfconfd binary (xfconf)" "${ROOT}/usr/lib/"*"/xfce4/xfconf/xfconfd" "${ROOT}/usr/lib/xfce4/xfconf/xfconfd" \
        "${ROOT}/usr/libexec/xfce4/xfconf/xfconfd"
    need_file "xfconf's D-Bus activation file org.xfce.Xfconf.service" "${ROOT}/usr/share/dbus-1/services/org.xfce.Xfconf.service"
    need_file "lightdm" "${ROOT}/usr/sbin/lightdm"
    need_file "the X server" "${ROOT}/usr/lib/xorg/Xorg" "${ROOT}/usr/bin/Xorg"
    # the sessions the LightDM drop-ins ask for
    session="$(conf_value user-session "${ROOT}/etc/lightdm/lightdm.conf" "${ROOT}"/etc/lightdm/lightdm.conf.d/*.conf)"
    greeter="$(conf_value greeter-session "${ROOT}/etc/lightdm/lightdm.conf" "${ROOT}"/etc/lightdm/lightdm.conf.d/*.conf)"
    session="${session:-xfce}"
    need_file "the session entry ${session}.desktop that lightdm starts (user-session=${session})" "${ROOT}/usr/share/xsessions/${session}.desktop"
    if [ -n "${greeter}" ]; then
        need_file "the greeter entry ${greeter}.desktop (greeter-session=${greeter})" "${ROOT}/usr/share/xgreeters/${greeter}.desktop"
    else
        softwarn files "no lightdm configuration names a greeter-session"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 3. xfconf defaults: present, parsable, and enough for xfce4-session
# ---------------------------------------------------------------------------
# the perchannel files lindos-desktop's dpkg list names
packaged_channels() {
    local list="${ROOT}/var/lib/dpkg/info/lindos-desktop.list"
    [ -f "${list}" ] || return 0
    { sed -n "s|^${PERCHANNEL}/\\(.*\\)\\.xml\$|\\1|p" "${list}" | tr -d '\r'; } || true
}

# the XML checks, in Python (stdlib), run with the image root as the working directory so that no absolute path has to
# cross the command line.  argv: the perchannel directory (relative), the Lindos channel names.
# Prints "FAIL|WARN|NOTE category text" lines.
PY_XFCONF="$(cat <<'PYEOF'
import os
import sys
import xml.etree.ElementTree as ET

perdir = sys.argv[1]
lindos = set(sys.argv[2].split())


def out(level, cat, text):
    print("%s %s %s" % (level, cat, text))


def shown(path):
    return "/" + path.replace(os.sep, "/").lstrip("/")


def props(path):
    """'/a/b' -> (type, value) of every property, like xfconf-query names them; raises on a parse error."""
    found = {}

    def walk(el, prefix):
        for child in el.findall("property"):
            p = prefix + "/" + child.get("name", "")
            found[p] = (child.get("type"), child.get("value"))
            walk(child, p)

    walk(ET.parse(path).getroot(), "")
    return found


names = set()
if os.path.isdir(perdir):
    for fn in sorted(os.listdir(perdir)):
        full = os.path.join(perdir, fn)
        if not fn.endswith(".xml") or not os.path.isfile(full):
            continue
        names.add(fn[:-4])
        try:
            props(full)
        except (ET.ParseError, OSError, ValueError) as exc:
            out("FAIL" if fn[:-4] in lindos else "WARN", "xfconf", "%s does not parse: %s" % (shown(full), exc))
for ch in sorted(lindos):
    if ch in names:
        continue
    there = [ch + ".xml" + suffix + " exists" for suffix in (".dpkg-dist", ".dpkg-new", ".lindos-orig")
             if os.path.exists(os.path.join(perdir, ch + ".xml" + suffix))]
    hint = ""
    if there:
        hint = " (only " + " and ".join(there) + ": dpkg kept another package's conffile of the same name as deleted and never installed the Lindos file)"
    out("FAIL", "xfconf", "%s/%s.xml is missing%s" % (shown(perdir), ch, hint))

# xfce4-session: the failsafe session is what a first login without a saved session loads
chain = ["etc/xdg/xdg-xfce", "etc/xdg"]
seen = {}
for d in chain:
    f = os.path.join(d, "xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml")
    if os.path.isfile(f):
        try:
            for k, v in props(f).items():
                seen.setdefault(k, (v, f))
        except (ET.ParseError, OSError, ValueError):
            pass            # reported above when it is the /etc/xdg copy
where = " or ".join(shown(os.path.join(d, "xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml")) for d in chain)
name = seen.get("/general/FailsafeSessionName")
if not name or not name[0][1]:
    out("FAIL", "xfconf", "/general/FailsafeSessionName is not defined by any xfce4-session.xml an XFCE session reads (%s): "
        "xfce4-session ends in 'Unable to determine failsafe session name'" % where)
else:
    out("NOTE", "xfconf", "FailsafeSessionName=%s (from %s)" % (name[0][1], shown(name[1])))
    session = "/sessions/" + name[0][1]
    if session not in seen:
        out("FAIL", "xfconf", "%s is not defined in xfce4-session.xml: the failsafe session has nothing to start" % session)
    elif (session + "/Client0_Command") not in seen:
        out("WARN", "xfconf", "%s has no Client0_Command" % session)
PYEOF
)"

check_xfconf() {
    local channels line level cat text result rc=0
    channels="${LINDOS_CHANNELS} $(packaged_channels | tr '\n' ' ')"
    if ! command -v "${PYTHON}" > /dev/null 2>&1; then
        softwarn xfconf "${PYTHON} is not available: the xfconf XML checks are skipped"
        return 0
    fi
    result="$(cd "${ROOT:-/}" && "${PYTHON}" -c "${PY_XFCONF}" "${PERCHANNEL#/}" "${channels}" 2>&1)" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        softwarn xfconf "the XML check itself failed (exit ${rc}): $(printf '%s' "${result}" | tail -n 3 | tr '\n' ' ')"
        return 0
    fi
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        level="${line%% *}"
        text="${line#* }"
        cat="${text%% *}"
        text="${text#* }"
        case "${level}" in
            FAIL) fail "${cat}" "${text}" ;;
            WARN) softwarn "${cat}" "${text}" ;;
            *) note "${cat}" "${text}" ;;
        esac
    done <<< "${result}"
    return 0
}

# ---------------------------------------------------------------------------
# 4. /etc/xdg/xdg-xfce
# ---------------------------------------------------------------------------
check_xdg() {
    local p="${ROOT}/etc/xdg/xdg-xfce" target n
    if [ -L "${p}" ]; then
        target="$(readlink -f "${p}" 2>/dev/null || true)"
        softwarn xdg "$(rel "${p}") is a symlink (-> ${target:-?}): an XFCE session puts it in front of /etc/xdg, so what it points to outranks the Lindos defaults"
    elif [ -d "${p}" ]; then
        n="$(find "${p}" -name '*.xml' 2> /dev/null | wc -l | tr -d ' ')"
        if [ "${n}" -gt 0 ]; then
            softwarn xdg "$(rel "${p}") carries ${n} xfconf default(s) of its own: they outrank the Lindos ones in /etc/xdg/xfce4"
        else
            note xdg "$(rel "${p}") is a plain directory without xfconf defaults"
        fi
    elif [ -e "${p}" ]; then
        softwarn xdg "$(rel "${p}") is neither a directory nor a symlink"
    else
        note xdg "$(rel "${p}") does not exist (the session skips a missing directory)"
    fi
    return 0
}

check_packages || warn "package check failed (continuing)"
check_files    || warn "file check failed (continuing)"
check_xfconf   || warn "xfconf check failed (continuing)"
check_xdg      || warn "xdg check failed (continuing)"

if [ "${LINDOS_SESSION_SANITY}" = "report" ]; then
    mode="report-only"
else
    mode="strict"
fi
log "SESSION-SANITY failures=${FAILURES} warnings=${WARNINGS} mode=${mode}"
if [ "${mode}" = "strict" ] && [ "${FAILURES}" -gt 0 ]; then
    die "the image cannot start an XFCE session: ${FAILURES} unambiguous problem(s) above (SESSION-FAIL); LINDOS_SESSION_SANITY=report only reports them"
fi

hook_end
