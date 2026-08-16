#!/bin/bash
# tests/run.sh — lint + test everything in the Lindos repository (SPEC §12).
#
# Runs from the repository root on Linux AND under Git Bash on Windows.
#
#   bash -n            every file with a bash shebang (or *.sh without shebang)
#   sh -n              every '#!/bin/sh' file (bash --posix -n when sh is missing)
#   py_compile         every *.py and every python-shebang'ed script (bins)
#   ShellCheck         if installed  (-S warning, excluding SC1090/SC1091)
#   JSON validity      every *.json
#   XML well-formed    every *.xml *.policy *.svg *.ui  (python xml.etree)
#   .desktop sanity    [Desktop Entry] + Name= + Exec= (desktop-file-validate if present)
#   CRLF detection     FAIL if any text file contains a carriage return
#   exec-bit reminder  Linux only: bin/ libexec/ *.sh DEBIAN/{post,pre}* should be 755 (warning)
#   pytest -q          tests/ packages/*/tests build/tests  (plugin: tests/lindos_testsupport.py)
#   compat doc         tests/gen-compat-doc.py --check
#
# Usage: tests/run.sh [--quick] [--skip-pytest] [--skip-shellcheck] [--verbose] [--help]
#   --quick           only static checks (no pytest, no compat-doc check)
#   --skip-pytest     skip the pytest run
#   --skip-shellcheck never run shellcheck even if installed
#   --verbose         list every checked file
# Exit status: 0 = everything passed, 1 = at least one failure, 2 = usage / environment error.
set -Eeuo pipefail

# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

QUICK=0
SKIP_PYTEST=0
SKIP_SHELLCHECK=0
VERBOSE=0

usage() {
    sed -n '2,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | grep -v '^set -Eeuo' | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --quick) QUICK=1 ;;
        --skip-pytest) SKIP_PYTEST=1 ;;
        --skip-shellcheck) SKIP_SHELLCHECK=1 ;;
        --verbose|-v) VERBOSE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'run.sh: unknown option: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_OFF=""
fi

FAILURES=0
WARNINGS=0
CHECKS_RUN=0
declare -a FAIL_LIST=()
declare -a SUMMARY=()

log()  { printf '%s[run.sh]%s %s\n' "${C_BLU}" "${C_OFF}" "$*"; }
ok()   { printf '%s  ok  %s %s\n' "${C_GRN}" "${C_OFF}" "$*"; }
warn() { printf '%s warn %s %s\n' "${C_YEL}" "${C_OFF}" "$*" >&2; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '%s FAIL %s %s\n' "${C_RED}" "${C_OFF}" "$*" >&2; FAILURES=$((FAILURES + 1)); FAIL_LIST+=("$*"); }
die()  { printf '%s[run.sh] fatal:%s %s\n' "${C_RED}" "${C_OFF}" "$*" >&2; exit 2; }
section() { CHECKS_RUN=$((CHECKS_RUN + 1)); printf '\n%s== %s ==%s\n' "${C_BLU}" "$*" "${C_OFF}"; }
record() { SUMMARY+=("$*"); }

is_windows() {
    case "$(uname -s 2>/dev/null || true)" in
        MINGW*|MSYS*|CYGWIN*) return 0 ;;
    esac
    return 1
}

# Python interpreter: python3 (Linux) → python (Windows) → py -3
PY=""
for cand in python3 python; do
    if command -v "${cand}" >/dev/null 2>&1; then
        if "${cand}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PY="${cand}"
            break
        fi
    fi
done
if [ -z "${PY}" ] && command -v py >/dev/null 2>&1; then
    PY="py -3"
fi
[ -n "${PY}" ] || die "no python3 (>= 3.8) found on PATH"
# shellcheck disable=SC2086  # PY may be 'py -3'
PY_VERSION="$(${PY} -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t lindos-run)"
trap 'rm -rf "${TMP_DIR}"' EXIT
export PYTHONPYCACHEPREFIX="${TMP_DIR}/pycache"   # keep py_compile from littering the tree
export PYTHONDONTWRITEBYTECODE=1
export PYTHONIOENCODING=utf-8

log "repository: ${REPO_ROOT}"
log "python: ${PY} (${PY_VERSION}); bash: ${BASH_VERSION}; host: $(uname -s 2>/dev/null || echo unknown)"

# --------------------------------------------------------------------------- #
# file discovery (null-delimited, prunes build products and caches)
# --------------------------------------------------------------------------- #
declare -a ALL_FILES=()
while IFS= read -r -d '' f; do
    ALL_FILES+=("${f#./}")
done < <(find . \( -name .git -o -name out -o -name __pycache__ -o -name .pytest_cache \
                    -o -name .mypy_cache -o -name .ruff_cache -o -name node_modules \
                    -o -name .venv -o -name venv \) -prune -o -type f -print0 | sort -z)
log "files discovered: ${#ALL_FILES[@]}"

first_line() {
    # print the first line of a file (no newline), tolerant to CRLF / binary
    head -n 1 -- "$1" 2>/dev/null | tr -d '\r' | head -c 200
}

declare -a BASH_FILES=()
declare -a SH_FILES=()
declare -a PY_FILES=()
declare -a JSON_FILES=()
declare -a XML_FILES=()
declare -a DESKTOP_FILES=()
declare -a EXEC_EXPECTED=()

for f in "${ALL_FILES[@]}"; do
    case "${f}" in
        *.json) JSON_FILES+=("${f}") ;;
        *.xml|*.policy|*.svg|*.ui) XML_FILES+=("${f}") ;;
        *.desktop) DESKTOP_FILES+=("${f}") ;;
    esac
    case "${f}" in
        *.py) PY_FILES+=("${f}") ;;
    esac
    shebang=""
    case "${f}" in
        *.sh|*/bin/*|*/libexec/*|*/sbin/*|*/DEBIAN/*|*/chroot/*|*.py|Makefile|*.env)
            shebang="$(first_line "${f}")" ;;
    esac
    case "${shebang}" in
        '#!/bin/bash'*|'#!/usr/bin/env bash'*|'#!/usr/bin/bash'*)
            BASH_FILES+=("${f}") ;;
        '#!/bin/sh'*|'#!/usr/bin/env sh'*|'#!/usr/bin/sh'*|'#!/bin/dash'*)
            SH_FILES+=("${f}") ;;
        '#!/usr/bin/env python'*|'#!/usr/bin/python'*|'#!/bin/python'*)
            case "${f}" in *.py) ;; *) PY_FILES+=("${f}") ;; esac ;;
        *)
            case "${f}" in
                *.sh)
                    # No/unknown shebang: still bash syntax
                    BASH_FILES+=("${f}") ;;
            esac ;;
    esac
    case "${f}" in
        *.sh|*/usr/bin/*|*/usr/libexec/*|*/usr/sbin/*|*/DEBIAN/postinst|*/DEBIAN/prerm|*/DEBIAN/postrm|*/DEBIAN/preinst|build/chroot/*|tests/gen-compat-doc.py)
            EXEC_EXPECTED+=("${f}") ;;
    esac
done

# --------------------------------------------------------------------------- #
# 1. bash -n
# --------------------------------------------------------------------------- #
section "bash -n (${#BASH_FILES[@]} files)"
bash_fail=0
for f in "${BASH_FILES[@]}"; do
    if bash -n "${f}" 2>"${TMP_DIR}/err"; then
        [ "${VERBOSE}" -eq 1 ] && ok "${f}"
    else
        fail "bash -n ${f}: $(tr '\n' ' ' <"${TMP_DIR}/err")"
        bash_fail=$((bash_fail + 1))
    fi
done
[ "${bash_fail}" -eq 0 ] && ok "all ${#BASH_FILES[@]} bash scripts parse"
record "bash -n: ${#BASH_FILES[@]} files, ${bash_fail} failed"

# --------------------------------------------------------------------------- #
# 2. sh -n
# --------------------------------------------------------------------------- #
section "sh -n (${#SH_FILES[@]} files)"
SH_CMD=()
if command -v sh >/dev/null 2>&1 && sh -c 'exit 0' 2>/dev/null; then
    SH_CMD=(sh -n)
elif command -v dash >/dev/null 2>&1; then
    SH_CMD=(dash -n)
else
    SH_CMD=(bash --posix -n)
    warn "no 'sh' on PATH — using 'bash --posix -n' as a fallback"
fi
sh_fail=0
for f in "${SH_FILES[@]}"; do
    if "${SH_CMD[@]}" "${f}" 2>"${TMP_DIR}/err"; then
        [ "${VERBOSE}" -eq 1 ] && ok "${f}"
    else
        fail "${SH_CMD[*]} ${f}: $(tr '\n' ' ' <"${TMP_DIR}/err")"
        sh_fail=$((sh_fail + 1))
    fi
done
[ "${sh_fail}" -eq 0 ] && ok "all ${#SH_FILES[@]} POSIX sh scripts parse (${SH_CMD[*]})"
record "sh -n: ${#SH_FILES[@]} files, ${sh_fail} failed"

# --------------------------------------------------------------------------- #
# 3. python -m py_compile
# --------------------------------------------------------------------------- #
section "python -m py_compile (${#PY_FILES[@]} files)"
py_fail=0
if [ "${#PY_FILES[@]}" -gt 0 ]; then
    printf '%s\0' "${PY_FILES[@]}" >"${TMP_DIR}/pyfiles"
    # One interpreter, all files; prints "path: error" per failure.
    # shellcheck disable=SC2086
    if ! ${PY} - "${TMP_DIR}/pyfiles" <<'PYEOF' 2>&1 | tee "${TMP_DIR}/pyout"
import py_compile, sys
listing = open(sys.argv[1], "rb").read().split(b"\0")
bad = 0
for raw in listing:
    if not raw:
        continue
    path = raw.decode("utf-8", "surrogateescape")
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        bad += 1
        print("py_compile FAIL %s: %s" % (path, exc.msg.strip().replace("\n", " ")))
    except OSError as exc:
        bad += 1
        print("py_compile FAIL %s: %s" % (path, exc))
sys.exit(1 if bad else 0)
PYEOF
    then
        py_fail="$(grep -c '^py_compile FAIL' "${TMP_DIR}/pyout" || true)"
        [ "${py_fail}" -gt 0 ] || py_fail=1
        fail "py_compile: ${py_fail} file(s) failed to compile (see above)"
    else
        ok "all ${#PY_FILES[@]} python files compile"
    fi
fi
record "py_compile: ${#PY_FILES[@]} files, ${py_fail} failed"

# --------------------------------------------------------------------------- #
# 4. shellcheck (optional)
# --------------------------------------------------------------------------- #
section "shellcheck"
sc_status="skipped"
# Prefer a ShellCheck on PATH; otherwise fall back to the binary bundled by the
# pip package 'shellcheck-py' (pip install shellcheck-py), which is not always
# on PATH (e.g. Windows per-user Python installs).
SHELLCHECK_BIN=""
if command -v shellcheck >/dev/null 2>&1; then
    SHELLCHECK_BIN="shellcheck"
else
    # The pip package 'shellcheck-py' drops 'shellcheck[.exe]' into the interpreter's scripts dir.
    # shellcheck disable=SC2086
    sc_py="$(${PY} - <<'PYEOF' 2>/dev/null || true
import os, sys, sysconfig
cands = []
for scheme in (None, "nt_user", "posix_user"):
    try:
        cands.append(sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts"))
    except Exception:  # noqa: BLE001
        pass
try:
    import shellcheck_py  # older releases expose SHELLCHECK_PATH
    print(shellcheck_py.SHELLCHECK_PATH)
    sys.exit(0)
except Exception:  # noqa: BLE001
    pass
for d in cands:
    if not d:
        continue
    for name in ("shellcheck", "shellcheck.exe"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            print(p)
            sys.exit(0)
PYEOF
)"
    if [ -n "${sc_py}" ] && [ -x "${sc_py}" ]; then
        SHELLCHECK_BIN="${sc_py}"
    fi
fi
if [ "${SKIP_SHELLCHECK}" -eq 1 ]; then
    warn "shellcheck skipped (--skip-shellcheck)"
elif [ -z "${SHELLCHECK_BIN}" ]; then
    warn "shellcheck not installed — skipping (apt install shellcheck  or  pip install shellcheck-py)"
else
    sc_files=("${BASH_FILES[@]}" "${SH_FILES[@]}")
    if [ "${#sc_files[@]}" -gt 0 ]; then
        if "${SHELLCHECK_BIN}" -S warning -e SC1090,SC1091 -x "${sc_files[@]}"; then
            ok "shellcheck clean (${#sc_files[@]} files, $("${SHELLCHECK_BIN}" --version | sed -n 's/^version: //p'))"
            sc_status="clean"
        else
            fail "shellcheck reported problems (see above)"
            sc_status="failed"
        fi
    fi
fi
record "shellcheck: ${sc_status}"

# --------------------------------------------------------------------------- #
# 5. JSON validity
# --------------------------------------------------------------------------- #
section "JSON validity (${#JSON_FILES[@]} files)"
json_fail=0
if [ "${#JSON_FILES[@]}" -gt 0 ]; then
    printf '%s\0' "${JSON_FILES[@]}" >"${TMP_DIR}/jsonfiles"
    # shellcheck disable=SC2086
    if ! ${PY} - "${TMP_DIR}/jsonfiles" <<'PYEOF' 2>&1 | tee "${TMP_DIR}/jsonout"
import json, sys
bad = 0
for raw in open(sys.argv[1], "rb").read().split(b"\0"):
    if not raw:
        continue
    path = raw.decode("utf-8", "surrogateescape")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            json.load(fh)
    except Exception as exc:  # noqa: BLE001
        bad += 1
        print("JSON FAIL %s: %s" % (path, exc))
sys.exit(1 if bad else 0)
PYEOF
    then
        json_fail="$(grep -c '^JSON FAIL' "${TMP_DIR}/jsonout" || true)"
        [ "${json_fail}" -gt 0 ] || json_fail=1
        fail "JSON: ${json_fail} invalid file(s)"
    else
        ok "all ${#JSON_FILES[@]} JSON files parse"
    fi
fi
record "json: ${#JSON_FILES[@]} files, ${json_fail} failed"

# --------------------------------------------------------------------------- #
# 6. XML well-formedness
# --------------------------------------------------------------------------- #
section "XML well-formedness (${#XML_FILES[@]} files: *.xml *.policy *.svg *.ui)"
xml_fail=0
if [ "${#XML_FILES[@]}" -gt 0 ]; then
    printf '%s\0' "${XML_FILES[@]}" >"${TMP_DIR}/xmlfiles"
    # shellcheck disable=SC2086
    if ! ${PY} - "${TMP_DIR}/xmlfiles" <<'PYEOF' 2>&1 | tee "${TMP_DIR}/xmlout"
import sys
import xml.etree.ElementTree as ET
bad = 0
for raw in open(sys.argv[1], "rb").read().split(b"\0"):
    if not raw:
        continue
    path = raw.decode("utf-8", "surrogateescape")
    try:
        ET.parse(path)
    except Exception as exc:  # noqa: BLE001
        bad += 1
        print("XML FAIL %s: %s" % (path, exc))
sys.exit(1 if bad else 0)
PYEOF
    then
        xml_fail="$(grep -c '^XML FAIL' "${TMP_DIR}/xmlout" || true)"
        [ "${xml_fail}" -gt 0 ] || xml_fail=1
        fail "XML: ${xml_fail} malformed file(s)"
    else
        ok "all ${#XML_FILES[@]} XML files are well-formed"
    fi
fi
record "xml: ${#XML_FILES[@]} files, ${xml_fail} failed"

# --------------------------------------------------------------------------- #
# 7. .desktop sanity
# --------------------------------------------------------------------------- #
section ".desktop sanity (${#DESKTOP_FILES[@]} files)"
desktop_fail=0
for f in "${DESKTOP_FILES[@]}"; do
    content="$(tr -d '\r' <"${f}")"
    problems=""
    if ! printf '%s\n' "${content}" | grep -q '^\[Desktop Entry\]'; then
        problems="${problems} missing [Desktop Entry];"
    fi
    if ! printf '%s\n' "${content}" | grep -Eq '^Name(\[[^]]*\])?='; then
        problems="${problems} missing Name=;"
    fi
    dtype="$(printf '%s\n' "${content}" | sed -n 's/^Type=//p' | head -n 1)"
    case "${dtype}" in
        Link|Directory) ;;   # no Exec required
        *)
            if ! printf '%s\n' "${content}" | grep -q '^Exec='; then
                problems="${problems} missing Exec=;"
            fi ;;
    esac
    if [ -n "${problems}" ]; then
        fail ".desktop ${f}:${problems}"
        desktop_fail=$((desktop_fail + 1))
    elif [ "${VERBOSE}" -eq 1 ]; then
        ok "${f}"
    fi
done
if command -v desktop-file-validate >/dev/null 2>&1 && [ "${#DESKTOP_FILES[@]}" -gt 0 ]; then
    dfv_out="${TMP_DIR}/dfv"
    : >"${dfv_out}"
    for f in "${DESKTOP_FILES[@]}"; do
        # desktop-file-validate validates *application* entries (the menu spec).
        # Session files under wayland-sessions/ and xsessions/ follow the
        # display-manager session spec instead (their 'DesktopNames' key is
        # valid there but flagged as an unknown key here), so skip them — like
        # GNOME/KDE session files, they are not menu entries.
        case "${f}" in
            */wayland-sessions/*|*/xsessions/*) continue ;;
        esac
        # desktop-file-validate is strict about unknown keys/categories; only
        # 'error:' lines fail the build, hints/warnings are printed.
        desktop-file-validate "${f}" >>"${dfv_out}" 2>&1 || true
    done
    if grep -q ': error:' "${dfv_out}"; then
        grep ': error:' "${dfv_out}" >&2
        fail "desktop-file-validate reported errors"
        desktop_fail=$((desktop_fail + 1))
    elif [ -s "${dfv_out}" ]; then
        warn "desktop-file-validate hints/warnings:"
        cat "${dfv_out}" >&2
    fi
    ok "desktop-file-validate ran on ${#DESKTOP_FILES[@]} files"
else
    warn "desktop-file-validate not installed — structural check only"
fi
[ "${desktop_fail}" -eq 0 ] && ok "all ${#DESKTOP_FILES[@]} .desktop files have [Desktop Entry]/Name/Exec"
record "desktop: ${#DESKTOP_FILES[@]} files, ${desktop_fail} failed"

# --------------------------------------------------------------------------- #
# 8. CRLF detection (every text file)
# --------------------------------------------------------------------------- #
section "CRLF detection (${#ALL_FILES[@]} files)"
printf '%s\0' "${ALL_FILES[@]}" >"${TMP_DIR}/allfiles"
crlf_fail=0
# shellcheck disable=SC2086
if ! ${PY} - "${TMP_DIR}/allfiles" <<'PYEOF' 2>&1 | tee "${TMP_DIR}/crlfout"
import sys
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".iso", ".img", ".deb", ".ttf",
              ".otf", ".woff", ".woff2", ".tar", ".gz", ".bz2", ".xz", ".zst", ".squashfs",
              ".qcow2", ".pyc", ".pyo", ".so", ".o", ".zip", ".7z", ".pdf", ".efi", ".bin"}
bad = 0
checked = 0
for raw in open(sys.argv[1], "rb").read().split(b"\0"):
    if not raw:
        continue
    path = raw.decode("utf-8", "surrogateescape")
    low = path.lower()
    if any(low.endswith(ext) for ext in BINARY_EXT):
        continue
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
            if b"\0" in head:
                continue  # binary
            data = head + fh.read()
    except OSError as exc:
        print("CRLF FAIL %s: cannot read: %s" % (path, exc))
        bad += 1
        continue
    checked += 1
    if b"\r" in data:
        n = data.count(b"\r")
        print("CRLF FAIL %s: %d carriage return(s) — convert to LF (git config core.autocrlf false; "
              "dos2unix)" % (path, n))
        bad += 1
print("CRLF checked %d text files" % checked)
sys.exit(1 if bad else 0)
PYEOF
then
    crlf_fail="$(grep -c '^CRLF FAIL' "${TMP_DIR}/crlfout" || true)"
    [ "${crlf_fail}" -gt 0 ] || crlf_fail=1
    fail "CRLF: ${crlf_fail} file(s) contain \\r"
else
    ok "no CRLF line endings"
fi
record "crlf: ${crlf_fail} files with CR"

# --------------------------------------------------------------------------- #
# 9. executable-bit reminder (Linux only; mkdeb.sh normalises perms anyway)
# --------------------------------------------------------------------------- #
section "executable bits"
if is_windows; then
    warn "executable-bit check not applicable on Windows (mkdeb.sh sets 755 for bin/, libexec/, *.sh, DEBIAN/*)"
    record "exec-bit: n/a on Windows"
else
    noexec=0
    for f in "${EXEC_EXPECTED[@]}"; do
        if [ ! -x "${f}" ]; then
            noexec=$((noexec + 1))
            [ "${VERBOSE}" -eq 1 ] && warn "not executable: ${f}"
        fi
    done
    if [ "${noexec}" -gt 0 ]; then
        warn "${noexec} of ${#EXEC_EXPECTED[@]} script/bin files lack +x (harmless for .deb builds: mkdeb.sh fixes perms; run 'chmod +x' for in-tree use)"
    else
        ok "all ${#EXEC_EXPECTED[@]} script/bin files are executable"
    fi
    record "exec-bit: ${noexec} of ${#EXEC_EXPECTED[@]} lack +x (warning only)"
fi

# --------------------------------------------------------------------------- #
# 10. pytest
# --------------------------------------------------------------------------- #
section "pytest"
if [ "${QUICK}" -eq 1 ] || [ "${SKIP_PYTEST}" -eq 1 ]; then
    warn "pytest skipped"
    record "pytest: skipped"
else
    declare -a PYTEST_DIRS=()
    [ -d tests ] && PYTEST_DIRS+=(tests)
    for d in packages/*/tests build/tests; do
        [ -d "${d}" ] && PYTEST_DIRS+=("${d}")
    done
    # shellcheck disable=SC2086
    if ! ${PY} -c 'import pytest' >/dev/null 2>&1; then
        fail "pytest is not installed for ${PY} (pip install pytest)"
        record "pytest: NOT INSTALLED"
    else
        log "pytest dirs: ${PYTEST_DIRS[*]}"
        # tests/lindos_testsupport.py is loaded as a plugin (-p) so the gi stub
        # and sys.path additions apply to every test tree regardless of rootdir.
        set +e
        # shellcheck disable=SC2086
        ${PY} - -q -c tests/pytest.ini --rootdir . -p lindos_testsupport "${PYTEST_DIRS[@]}" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
import pytest
sys.exit(pytest.main(sys.argv[1:]))
PYEOF
        rc=$?
        set -e
        case "${rc}" in
            0) ok "pytest passed"; record "pytest: passed" ;;
            5) warn "pytest: no tests collected"; record "pytest: no tests collected" ;;
            *) fail "pytest exited with status ${rc}"; record "pytest: FAILED (rc=${rc})" ;;
        esac
    fi
fi

# --------------------------------------------------------------------------- #
# 11. generated compatibility doc
# --------------------------------------------------------------------------- #
section "docs/COMPATIBILITY.md freshness"
if [ "${QUICK}" -eq 1 ]; then
    warn "compat-doc check skipped (--quick)"
    record "compat-doc: skipped"
elif [ ! -f tests/gen-compat-doc.py ]; then
    fail "tests/gen-compat-doc.py is missing"
    record "compat-doc: generator missing"
else
    set +e
    # shellcheck disable=SC2086
    ${PY} tests/gen-compat-doc.py --check
    rc=$?
    set -e
    case "${rc}" in
        0) ok "docs/COMPATIBILITY.md matches compat-matrix.json"; record "compat-doc: up to date" ;;
        1) fail "docs/COMPATIBILITY.md is out of date — run: python3 tests/gen-compat-doc.py"; record "compat-doc: STALE" ;;
        *) if [ -f packages/lindos-gaming/root/usr/share/lindos/compat-matrix.json ]; then
               fail "gen-compat-doc.py --check failed (rc=${rc})"; record "compat-doc: ERROR (rc=${rc})"
           else
               warn "compat-matrix.json not present yet — compat-doc check skipped"; record "compat-doc: source missing (skipped)"
           fi ;;
    esac
fi

# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
printf '\n%s== summary ==%s\n' "${C_BLU}" "${C_OFF}"
for line in "${SUMMARY[@]}"; do
    printf '  %s\n' "${line}"
done
printf '  sections: %d, warnings: %d, failures: %d\n' "${CHECKS_RUN}" "${WARNINGS}" "${FAILURES}"
if [ "${FAILURES}" -gt 0 ]; then
    printf '\n%sFAILED%s (%d):\n' "${C_RED}" "${C_OFF}" "${FAILURES}"
    for line in "${FAIL_LIST[@]}"; do
        printf '  - %s\n' "${line}"
    done
    exit 1
fi
printf '\n%sALL CHECKS PASSED%s\n' "${C_GRN}" "${C_OFF}"
exit 0
