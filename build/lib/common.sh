#!/bin/bash
# shellcheck shell=bash
# ============================================================================
#  build/lib/common.sh — shared helpers for the Lindos build scripts.
#
#  Source it (do not execute):
#      # shellcheck source=build/lib/common.sh
#      . "${BUILD_DIR}/lib/common.sh"
#
#  Provides:
#      log MSG…            timestamped info line (stderr)
#      warn MSG…           timestamped WARNING line (stderr)
#      die MSG… [CODE]     log ERROR and exit (default code 1)
#      have CMD            true if CMD is on PATH
#      require_cmd CMD…    die listing every missing command
#      need_root           die unless EUID == 0
#      human_size BYTES    "1.2 GiB"
#      timer_start NAME / timer_end NAME   → logs elapsed seconds
#      elapsed SECONDS     "1h 02m 03s"
#      abs_path PATH       absolute, normalised path (no symlink resolution needed)
#      repo_root           absolute repo root (parent of build/)
#      lindos_load_config  source build/config.env (respecting env overrides)
#      strip_crlf FILE…    remove trailing CR from text files (in place)
#      is_text_file FILE   heuristic: no NUL byte in first 8 KiB
#      free_gb DIR         free GiB on DIR's filesystem (integer)
#
#  Conventions: '#!/bin/bash' + 'set -Eeuo pipefail' in every caller, quote
#  everything, no sudo inside scripts (callers use sudo/pkexec/Docker).
# ============================================================================

# Guard against double sourcing.
if [ -n "${__LINDOS_COMMON_SH:-}" ]; then
    return 0 2>/dev/null || true
fi
__LINDOS_COMMON_SH=1

# Name shown in log lines; callers may override before/after sourcing.
: "${LOG_PREFIX:=$(basename "${0:-lindos}")}"

# Colours only when stderr is a terminal and NO_COLOR is unset.
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
    _C_INFO=$'\033[1;34m'; _C_WARN=$'\033[1;33m'; _C_ERR=$'\033[1;31m'; _C_OK=$'\033[1;32m'; _C_RST=$'\033[0m'
else
    _C_INFO=''; _C_WARN=''; _C_ERR=''; _C_OK=''; _C_RST=''
fi

_ts() { date '+%F %T'; }

log() {
    printf '%s[%s] %s:%s %s\n' "${_C_INFO}" "$(_ts)" "${LOG_PREFIX}" "${_C_RST}" "$*" >&2
}

ok() {
    printf '%s[%s] %s: OK%s %s\n' "${_C_OK}" "$(_ts)" "${LOG_PREFIX}" "${_C_RST}" "$*" >&2
}

warn() {
    printf '%s[%s] %s: WARNING:%s %s\n' "${_C_WARN}" "$(_ts)" "${LOG_PREFIX}" "${_C_RST}" "$*" >&2
}

# die MSG… [exit code as last numeric arg]
die() {
    local code=1
    local msg="$*"
    # If the last argument is a small integer, treat it as the exit code.
    if [ "$#" -ge 2 ]; then
        local last="${*: -1}"
        if [[ "${last}" =~ ^[0-9]{1,3}$ ]]; then
            code="${last}"
            msg="${*:1:$#-1}"
        fi
    fi
    printf '%s[%s] %s: ERROR:%s %s\n' "${_C_ERR}" "$(_ts)" "${LOG_PREFIX}" "${_C_RST}" "${msg}" >&2
    exit "${code}"
}

have() { command -v "$1" >/dev/null 2>&1; }

# require_cmd CMD… — dies listing all missing commands (and an apt hint if given
# via APT_HINT_PACKAGES).
require_cmd() {
    local missing=()
    local c
    for c in "$@"; do
        have "${c}" || missing+=("${c}")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        if [ -n "${APT_HINT_PACKAGES:-}" ]; then
            die "missing commands: ${missing[*]} — install with: apt-get install -y ${APT_HINT_PACKAGES}"
        fi
        die "missing commands: ${missing[*]}"
    fi
}

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "this script must run as root (use: sudo $0 $* — or build/docker-build.sh)"
    fi
}

# human_size BYTES → e.g. "3.4 GiB"
human_size() {
    local bytes="${1:-0}"
    local units=(B KiB MiB GiB TiB)
    local i=0
    local val="${bytes}"
    local frac=0
    while [ "${val}" -ge 1024 ] && [ "${i}" -lt 4 ]; do
        frac=$(( (val % 1024) * 10 / 1024 ))
        val=$(( val / 1024 ))
        i=$(( i + 1 ))
    done
    if [ "${i}" -eq 0 ]; then
        printf '%d %s' "${val}" "${units[$i]}"
    else
        printf '%d.%d %s' "${val}" "${frac}" "${units[$i]}"
    fi
}

# elapsed SECONDS → "1h 02m 03s" / "02m 03s" / "3s"
elapsed() {
    local s="${1:-0}"
    local h=$(( s / 3600 ))
    local m=$(( (s % 3600) / 60 ))
    local sec=$(( s % 60 ))
    if [ "${h}" -gt 0 ]; then
        printf '%dh %02dm %02ds' "${h}" "${m}" "${sec}"
    elif [ "${m}" -gt 0 ]; then
        printf '%dm %02ds' "${m}" "${sec}"
    else
        printf '%ds' "${sec}"
    fi
}

# Simple named timers (bash 4 associative array).
declare -gA __LINDOS_TIMERS=()
declare -ga __LINDOS_TIMER_SUMMARY=()

timer_start() {
    local name="${1:?timer name}"
    __LINDOS_TIMERS["${name}"]="$(date +%s)"
    log ">>> ${name}"
}

timer_end() {
    local name="${1:?timer name}"
    local start="${__LINDOS_TIMERS[${name}]:-}"
    local now
    now="$(date +%s)"
    if [ -z "${start}" ]; then
        warn "timer_end: unknown timer '${name}'"
        return 0
    fi
    local dur=$(( now - start ))
    __LINDOS_TIMER_SUMMARY+=("$(printf '%-28s %s' "${name}" "$(elapsed "${dur}")")")
    ok "<<< ${name} ($(elapsed "${dur}"))"
}

timer_summary() {
    local line
    if [ "${#__LINDOS_TIMER_SUMMARY[@]}" -eq 0 ]; then
        return 0
    fi
    log "----- timing summary -----"
    for line in "${__LINDOS_TIMER_SUMMARY[@]}"; do
        log "  ${line}"
    done
}

# abs_path PATH — absolute path without requiring the target to exist.
abs_path() {
    local p="${1:?path}"
    case "${p}" in
        /*) printf '%s\n' "${p}" ;;
        *)  printf '%s/%s\n' "$(pwd -P)" "${p}" ;;
    esac
}

# repo_root — the directory containing build/ (derived from this file).
repo_root() {
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
    (cd "${here}/../.." && pwd -P)
}

# lindos_load_config — source build/config.env once (env values win because
# config.env only sets defaults with ': "${VAR:=…}"').
lindos_load_config() {
    local root
    root="$(repo_root)"
    local cfg="${LINDOS_CONFIG_ENV:-${root}/build/config.env}"
    if [ ! -f "${cfg}" ]; then
        die "config file not found: ${cfg}"
    fi
    # shellcheck source=build/config.env
    . "${cfg}"
    # Optional local overrides (git-ignored).
    if [ -f "${root}/build/config.local.env" ]; then
        # shellcheck source=/dev/null
        . "${root}/build/config.local.env"
    fi
}

# is_text_file FILE — true when the first 8 KiB contain no NUL byte.
is_text_file() {
    local f="${1:?file}"
    [ -f "${f}" ] || return 1
    local n_raw n_nonul
    n_raw="$(head -c 8192 -- "${f}" | wc -c)"
    n_nonul="$(head -c 8192 -- "${f}" | LC_ALL=C tr -d '\000' | wc -c)"
    [ "${n_raw}" -eq "${n_nonul}" ]
}

# strip_crlf FILE… — remove trailing CR from every line, in place, text files only.
strip_crlf() {
    local f
    for f in "$@"; do
        [ -f "${f}" ] || continue
        is_text_file "${f}" || continue
        # -U (binary) is a no-op on Linux but required for CR detection on
        # Windows/Git Bash, where grep would otherwise eat the CR itself.
        if LC_ALL=C grep -qU $'\r$' -- "${f}" 2>/dev/null; then
            sed -i 's/\r$//' -- "${f}"
            log "stripped CRLF: ${f}"
        fi
    done
}

# free_gb DIR — free space of the filesystem holding DIR, in whole GiB.
free_gb() {
    local d="${1:-.}"
    while [ ! -d "${d}" ] && [ "${d}" != "/" ] && [ -n "${d}" ]; do
        d="$(dirname "${d}")"
    done
    df -Pk -- "${d}" 2>/dev/null | awk 'NR==2 {printf "%d\n", $4/1024/1024}'
}

# ensure_dir DIR… — mkdir -p with a log line on creation.
ensure_dir() {
    local d
    for d in "$@"; do
        if [ ! -d "${d}" ]; then
            mkdir -p -- "${d}"
        fi
    done
}

# run_logged CMD… — echo the command then run it (used for long external tools).
run_logged() {
    log "\$ $*"
    "$@"
}
