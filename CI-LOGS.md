# Lindos — CI Log & History

CI runs on **GitHub Actions** (`.github/workflows/ci.yml`) — a real Ubuntu 24.04 machine, which is
where the Linux-only verification this Windows dev box can't do actually happens. This file records
the run history and the fixes, so the next agent has the context without digging through the Actions
UI.

- **Repo:** https://github.com/hardcoregamingsyle/lindos (private), branch `main`
- **View runs:** `gh run list --limit 10`
- **View a run:** `gh run view <run-id>` · failing steps only: `gh run view <run-id> --log-failed`
- **Watch a run:** `gh run watch <run-id> --exit-status`  ⚠️ do **not** pipe through `tail` — the
  pipe's exit status is `tail`'s (always 0) and hides whether the run passed or failed.

## Jobs in the workflow

| Job | Trigger | What it does |
|---|---|---|
| `lint + pytest (Ubuntu 24.04)` | every push / PR | `tests/run.sh` with **real** shellcheck + desktop-file-validate + dash; compat-doc check |
| `pytest (Windows, gi stub)` | every push / PR | the suite on Windows — proves the "importable on any OS" rule |
| `build .deb packages` | every push / PR | `build/mkdeb.sh --lintian all` → uploads `lindos-debs` artifact |
| `build Lindos kernel` | **manual** (`build_kernel=true`) or tags | `build/kernel/build-kernel.sh` → `lindos-kernel-debs` artifact |
| `build ISO` | **manual** (`build_iso=true`) | `build/build-iso.sh` in a privileged container → `lindos-iso` artifact |

Trigger the heavy builds:
```bash
gh workflow run CI -f build_kernel=true -f build_iso=true
```

## Run history

| Run ID | Commit | Result | Notes |
|---|---|---|---|
| 31926418356 | `a3474ed` ci: GitHub Actions | ❌ failure (1m15s) | First run on real Linux; surfaced 6 Linux-only issues (below). Downstream debs/kernel/iso skipped because `lint-test` failed. |
| 31926992548 | `8c07619` ci: fix 6 Linux-only failures | ✅ **success** (2m20s) | Ubuntu lint+pytest ✓, Windows pytest ✓, debs+lintian ✓ (artifact `lindos-debs`). Kernel/ISO manual → skipped. |

Windows harness had reported **ALL CHECKS PASSED** (970 pytest) for both commits — these 6 failures
are exactly the class of Linux-only problem the Windows gate structurally cannot see, which is why
running CI on Actions mattered.

---

## Run 31926418356 — the 6 Linux-only failures (and their fixes in `8c07619`)

### Section: `.desktop sanity` — `desktop-file-validate` reported errors (2)

```
packages/lindos-desktop/root/usr/share/applications/lindos-files.desktop: error:
  value "thunar %h" for key "Exec" in group "Desktop Action open-home" contains an invalid field code "%h"
packages/lindos-gaming/root/usr/share/wayland-sessions/lindos-gaming.desktop: error:
  file contains key "DesktopNames" in group "Desktop Entry", but keys extending the format should start with "X-"
 FAIL  desktop-file-validate reported errors
```

- **Cause 1:** `%h` is not a valid Desktop Entry field code (Windows harness only checks structure,
  not field codes). **Fix:** `Exec=thunar` (Thunar opens the home folder with no argument).
- **Cause 2:** `DesktopNames` is a valid *session* key (read by the display manager), but
  `desktop-file-validate` validates *application* menu entries and rejects it — GNOME/KDE session
  files trip the same false positive. **Fix:** `tests/run.sh` now skips `*/wayland-sessions/*` and
  `*/xsessions/*` in the desktop-file-validate pass (still structurally checked).

### Section: `pytest` — 6 failed, 966 passed (rc=1)

```
FAILED packages/lindos-compat/tests/test_run_profiles.py::test_cli_refuses_not_possible_profile
        AssertionError: assert ('cannot run on Lindos' in '')     # caplog.text empty
FAILED packages/lindos-core/tests/test_core.py::test_install_browser_script_dry_run
        AssertionError: assert ('google-chrome.list' in '...already installed...')
FAILED packages/lindos-settings/tests/test_model.py::test_backend_browsers_and_apps_page
        AttributeError: 'NoneType' object has no attribute 'ScrolledWindow'
FAILED packages/lindos-setup/tests/test_plan.py::test_ui_modules_import_with_gi_stub
        ValueError: Namespace Gtk not available
FAILED packages/lindos-setup/tests/test_plan.py::test_page_context_connectivity_state
        ValueError: Namespace Gtk not available
FAILED packages/lindos-tune/tests/test_sched.py::test_cli_sched_set_exit3_without_sched_ext
        AssertionError: assert 1 == 3
```

Every one was a **test-hermeticity bug** — the test leaked the runner's host state — plus the two
desktop errors above. None was a product-behavior or honesty-rule problem.

| Test | Why it only fails on the runner | Fix |
|---|---|---|
| `test_backend_browsers_and_apps_page`, `test_ui_modules_import_with_gi_stub`, `test_page_context_connectivity_state` | Runner has `python3-gi` but **no GTK typelib**, so the shared stub (installed only if `import gi` fails) was **not** injected; then `require_version("Gtk","3.0")` raised "Namespace Gtk not available". | `tests/lindos_testsupport.py`: probe for a **usable** Gtk (`import gi` **and** `require_version("Gtk","3.0")` **and** import `gi.repository.Gtk`); inject the stub if any step fails — so the runner behaves like Windows. |
| `test_cli_sched_set_exit3_without_sched_ext` | The runner's own recent kernel **has `sched_ext`** (`/sys/kernel/sched_ext` exists), so the "no sched_ext → exit 3" path wasn't taken; it got exit 1 instead. | `packages/lindos-tune/tests/test_sched.py`: add the `staging` fixture so `LINDOS_ROOT` points at an empty tree (no `/sys/kernel/sched_ext`) → deterministic exit 3 on any host. |
| `test_install_browser_script_dry_run` | GitHub runners ship **google-chrome preinstalled**, so `install-browser.sh` hit "already installed" and short-circuited before printing the repo/`.list` plan. | `install-browser.sh`: gate the "already installed" short-circuit on `DRY_RUN -eq 0`, so `--dry-run` always prints the full plan regardless of host state (more honest dry-run, too). |
| `test_cli_refuses_not_possible_profile` | `lindos-run`'s logger sets `propagate=False` (via `get_logger`), so `caplog`'s root-logger handler never saw the record; visibility then depended on fragile stream/timing. | `packages/lindos-compat/tests/test_run_profiles.py`: attach `caplog.handler` **directly** to the `lindos-run` logger (the documented pattern for non-propagating loggers). |

Fix commit: `8c07619` "ci: fix 6 Linux-only test/lint failures surfaced by GitHub Actions".

---

## Run 31926992548 — GREEN

```
✓ pytest (Windows, gi stub)        in 52s
✓ lint + pytest (Ubuntu 24.04)     in 1m6s     (real shellcheck/desktop-file-validate/dash)
✓ build .deb packages              in 1m3s     → artifact: lindos-debs (mkdeb.sh --lintian all)
- build Lindos kernel (manual)                 skipped (needs build_kernel=true)
- build ISO (manual)                           skipped (needs build_iso=true)
```

**Non-blocking warning** seen on all jobs (cosmetic, ignore or bump later):
`actions/checkout@v4` / `actions/setup-python@v5` / `actions/upload-artifact@v4` are being forced
onto Node 24 because they target the now-deprecated Node 20. Bump the action versions when
convenient.

---

## 2026-09-26 — Addendum W/U + CI boot-test, branch `feature/windows-transfer-updates-boottest`

Repo went **public** partway through this session (user-approved, after a git-history secret
scan found nothing — see the session transcript) specifically so Actions minutes are free;
before that, every dispatch failed instantly with "recent account payments have failed or your
spending limit needs to be increased" (run `36244669243` — a billing/account issue, not a code
problem, not counted below). All runs below are `workflow_dispatch` with
`build_iso=true boot_test=true build_kernel=false` on
`feature/windows-transfer-updates-boottest`, dispatched after `git push` of each fix.

| Run ID | Result | What failed | Fix |
|---|---|---|---|
| `36244834192` | ❌ `lint-test` + `pytest-windows` failed | 3 issues, see below | commit `d98347a` |
| `36245958614` | ❌ `iso` failed | `xfce4-docklike-plugin` Depends unsatisfiable | commit `c86c75f` |
| `36247525043` | ❌ `boot-test` failed | **first real boot!** KVM permission denied → TCG fallback → Wine crash; 2 smoke-test script bugs | commit `cde7e72` |
| `36250154186` | ❌ `boot-test` failed | KVM fixed (crash gone), but a bash `$?`-capture bug in the smoke test masked the real result of 2 checks | commit `04c7a1c` |
| `36253218688` | ❌ `boot-test` failed | `lindos-compat-doctor` genuinely fails one required check beyond the (correctly-excused) missing DISPLAY — **root cause not yet identified**, round cap reached | not fixed (see below) |

### Round 1 (`36244834192`) — 3 Linux/runner-host-state issues, same class as the original 6

- `packages/lindos-desktop/DEBIAN/conffiles` was missing `/etc/xdg/autostart/lindos-update-notify.desktop`
  (Addendum U shipped the file but never added it to conffiles) → `test_debian_metadata` failed on
  both Ubuntu and Windows. Fix: added the line (alphabetically sorted).
- `packages/lindos-gaming/root/usr/bin/lindos-game`'s `build_routes()` called
  `provider_client_ready(provider)` without forwarding `which=_which` — that function's own
  `which=_which` default parameter was bound to the *real* `shutil.which` at import time, so a
  test's `monkeypatch.setattr(game, "_which", ...)` never reached it. On this Ubuntu runner (which
  ships a real browser), that made `xbox-cloud` look "available" when the test expected "no browser
  found". Same bug class as the pre-existing "runner host state leaks into tests" entries below.
  Fix: pass `which=_which` explicitly at the call site (a plain identifier there resolves through
  the module's current global at call time, unlike a function's own default parameter).
- `build/tests/test_build_kernel.py`'s two "apt tooling absent" tests prepended an *empty* directory
  to the real `PATH` — Ubuntu's runner has a **real** `apt-cache`, so it was still found further
  down `PATH`; only a bare Windows host (no apt at all, ever) made the old approach work. Fix: added
  `_no_apt_tooling_env()` — a `PATH` built only from the coreutils these functions actually need
  (`ls sort tail mkdir tar find ...`), with `apt-cache`/`apt-get`/`dpkg-deb` deliberately never
  included, regardless of what the host provides.

### Round 2 (`36245958614`) — ISO build: `xfce4-docklike-plugin` isn't on Ubuntu 24.04

The first-ever ISO build got past `debs`/`lintian` but failed installing the Lindos `.deb`s inside
the chroot: `apt-get install ./debs/*.deb` refused the whole transaction —
`lindos-desktop : Depends: xfce4-docklike-plugin but it is not installable`. Confirmed against
packages.ubuntu.com: this plugin is **not packaged for Ubuntu 24.04 "noble"** at all (only Ubuntu
25.10+ / Debian trixie+ carry it) — a genuine, pre-existing packaging gap, not something this
branch introduced or broke. The failed transaction also meant `steam-devices` and
`freerdp3-x11|freerdp2-x11` (real, available Depends of lindos-gaming/lindos-winapps) never got
resolved either, and the `dpkg -i` fallback left four packages unconfigured.

Fix: moved `xfce4-docklike-plugin` from `Depends` to `Recommends` in
`packages/lindos-desktop/DEBIAN/control` (matching how `lindos-meta` already listed it, and how
`build/chroot/20-base.sh` already treats it as best-effort with a loud warning).
`30-lindos-debs.sh` installs with `--no-install-recommends` by design, so this Recommends is never
even attempted there; `xfce4-panel` leaves that panel slot empty if the plugin genuinely isn't
installed — the desktop stays usable either way. Updated `SPEC.md`, `docs/ARCHITECTURE.md`, and
`packages/lindos-desktop/tests/test_xml.py` to match.

### Round 3 (`36247525043`) — first real boot, and it mostly worked

The ISO built, booted under QEMU, ran the smoke test, and took a screenshot — the very first time
anything in this repo has been booted at all. Three things surfaced:

1. **KVM permission denied → silent TCG fallback → a real kernel oops.** `/dev/kvm` existed
   (`crw-rw---- root:kvm`) but the `runner` user's `kvm`-group membership (granted by udev when the
   device was created) doesn't apply to the already-running job shell, so `-accel kvm` failed and
   qemu fell back to `-accel tcg` (this is pre-existing, intentional behavior in `boot_test.py` —
   never treated as fatal). Under TCG's software CPU emulation, `wineboot.exe`/`start.exe` (spawned
   by `lindos-compat doctor`'s real Wine prefix bootstrap) hit `BUG: unable to handle page fault ...
   LDTR: NULL` — a known category of TCG correctness gap in 32-bit WoW64 LDT/segment-reload
   handling that real KVM (hardware segmentation) doesn't have. Fix: `.github/workflows/ci.yml`'s
   "Check for KVM" step now does `sudo chmod 0666 /dev/kvm` when the device exists.
2. The smoke test called `lindos-config get --json` with no `key` argument (`get` requires one;
   `show` is the no-argument variant) — argparse's usage-error exit 2 was misreported as a check
   failure. Fixed the invocation to `lindos-config show --json`.
3. The smoke test treated any non-zero exit as failure, but `lindos-update check`'s own contract
   (`EXIT_NOTHING = 3`, matched by the pre-existing `lindos-update-notify.sh` comment: "0 = updates
   found, 3 = nothing to do — both mean this succeeded") deliberately returns 3 when there's
   nothing to update, which is exactly the state of a fresh live ISO with no apt repo configured.
   Added `check() ... --ok CSV_CODES` so a designed non-zero exit isn't flagged as a failure.

### Round 4 (`36250154186`) — the KVM fix worked; a bash bug in the smoke test masked the rest

Boot went **8x faster** (166s → 20s to `LINDOS_SMOKE_DONE`) and the Wine/TCG page-fault did not
recur — real KVM was in use. But `lindos-compat-doctor` and `lindos-update-check` both reported
the impossible `rc=0` alongside a FAIL/mismatched-OK verdict. Root cause: `check()` used
`if "$@" ...; then ok; fi` with **no else** — POSIX defines the exit status of an `if` whose
condition was false and which ran no branch as **0**, which clobbered `$?` back to 0 immediately
before `local rc=$?` could read the command's real (non-zero) exit code. This made every failing
check's `rc` bogus: a default (`ok="0"`) check would always "match" (false OK), and an `--ok N`
check could never match (false FAIL) since the real code was never visible. Fixed by running the
command as its own statement and reading `$?` on the very next line — never through an
if-without-else. Also added `check_compat_doctor()`: `lindos-compat doctor`'s exit code is
`EXIT_ERROR` if *any* `required`-level check fails, and its "Graphical session (DISPLAY)" check
correctly and expectedly fails here (the smoke test runs as an early-boot `systemd.run=` oneshot,
outside any logged-in desktop — Windows programs really can't run without one), so that specific,
expected failure is now excused; any *other* required check failing still fails the smoke test.
Added 3 new hermetic regression tests in `build/tests/test_boot_test_qa.py` that extract and run
the actual shipped function bodies (not a re-typed copy) — this script had zero test coverage
before, which is exactly how the `$?`-clobbering bug shipped unnoticed through `bash -n`/shellcheck.

### Round 5 (`36253218688`) — round cap reached; genuine, unidentified `lindos-compat-doctor` failure remains

With the `$?` bug fixed, the real picture: 9 of 10 checks pass, including `lindos-update-check=OK
rc=3` (exactly the designed result) and no kernel oops. `lindos-compat-doctor=FAIL rc=1` is now a
**confirmed-genuine** failure — `check_compat_doctor()`'s JSON-aware logic (verified correct by 3
passing unit tests) only reports FAIL when a `required`-level check *other than* `display` is
unhappy, so something concrete is wrong. **Not yet diagnosed**: the doctor JSON itself went to
`/tmp/lindos-smoke-lindos-compat-doctor.log` inside the guest, which isn't part of the uploaded
artifact (only `serial.log` + `desktop.png` are) — so the *which* required check failed is unknown
from what CI captured. Leading candidates from `doctor.py`'s `required`-level checks: `wine` (is
Wine actually present/found on this ISO — `INCLUDE_WINE` default is `1`, but this is the first time
that path has ever been exercised for real) or `prefixes-dir` (the `~/.local/share/lindos/prefixes`-
style folder writable check, on a live "mint" user's home). This is where the 4-fix-and-rerun-round
cap for the no-kernel loop was reached, per instructions — stopping here rather than attempting a
5th fix blind. **Next step for whoever picks this up:** have `ci-boot-smoke-test.sh` also echo the
raw `lindos-compat doctor --json` output (or at least the list of failing required check ids) to
the console/serial log on failure, so the next run's artifact is self-diagnosing instead of needing
a guess.

The live-desktop screenshot (`desktop.png`) is still black (a lone blinking cursor, top-left) on
every run so far, with `is_system_running=initializing` at smoke-test time — the 90s grace period
in `boot_test.py` has not yet been enough for XFCE/lightdm to finish rendering by the time
`screendump` runs, even under KVM. This does **not** fail the job (the screenshot's pixel content
is never gated on, only whether the file was captured at all — see `build/qa/boot_test.py`'s
`take_screenshot()`), but it means the boot-test has never yet visually confirmed the live desktop
itself, only the CLI layer. Worth a longer grace period or a monitor-based "wait for Xorg" signal
if/when this is revisited.

**Local verification for every round above:** `bash tests/run.sh --quick` (0 failures, shellcheck
clean) and the full `pytest` suite (3115–3118 passed depending on the round, 4 pre-existing
Windows-only skips) were run before every commit in this sequence.

---

## 2026-09-27 — self-diagnosis, real-desktop wait, merge, and the first `build_kernel=true` runs

### Round: self-diagnosing smoke test (`36286387867` ✅, then `36288980753` ✅)

Two asks: make a failing `lindos-compat-doctor` check say *why* (not just a bare rc — the CI
artifact only ever carries `serial.log`, never the guest's own `/tmp`), and get a screenshot of
the *actual rendered desktop* instead of mid-boot black.

- `check()` (the generic per-CLI check helper) now tails the last 10 lines of a failing check's
  own captured output straight to the console, prefixed `LINDOS_FAIL_LOG <name>: `.
- `check_compat_doctor()` parses the doctor JSON on failure and prints one
  `LINDOS_DOCTOR_FAIL id=<id> msg=<detail> (fix: <fix>)` line per `required`-level check that
  failed for a real reason (excusing only the expected missing-DISPLAY one — this smoke test runs
  as an early-boot `systemd.run=` oneshot, outside any logged-in desktop session).
- Added `start_desktop_watch()`: a **detached** watcher (launched via `systemd-run --no-block`,
  since this script is itself part of `default.target`'s own start-job graph — waiting for
  `is-system-running` in-line here would deadlock boot forever) that prints
  `LINDOS_DESKTOP_READY` once `systemctl is-system-running` reports running/degraded *and* a real
  X/lightdm/Xwayland process exists, or `LINDOS_DESKTOP_READY_TIMEOUT` after its own bound.
  `boot_test.py` waits up to `--desktop-timeout` (default 180s) for that sentinel before its
  existing `--grace` + screendump, still screenshotting on timeout for debugging.
- Run `36286387867` surfaced two real bugs found via the new diagnostics themselves: (1)
  `lindos-config get --json` called with no `key` (fixed to `show --json`) — no longer relevant,
  superseded by later findings; (2) a **POSIX `if cond; then ok; fi` with no `else` reports the
  *if statement's own* exit status (0) when the condition is false**, clobbering `$?` right
  before `local rc=$?` could read the real (non-zero) code — every failing check had been
  silently recording `rc=0`. Fixed by running the command as its own statement and reading `$?`
  immediately after, never through an if-without-else. Also found: `logging.StreamHandler()`
  defaults to stderr and flushes every record immediately, while `doctor --json`'s payload is
  plain `print()`ed to stdout, which Python fully block-buffers once it isn't a TTY — merged via
  `2>&1`, a log record could land *before* the buffered JSON, corrupting the start of the file
  ("Expecting value: line 1 column 1 (char 0)" even though the tail was well-formed JSON). Fixed
  by capturing doctor's stdout/stderr to **separate** files; only the pure-stdout one is parsed.
- Run `36288980753`: fully green, including `boot-test`. All 10 checks genuinely OK (including
  `lindos-update-check=OK rc=3`, the designed "nothing to update" result). The desktop-ready
  watcher's own `systemd-run` dispatch succeeded (`rc=0 out=Running as unit:
  lindos-desktop-watch.service`) but neither `LINDOS_DESKTOP_READY` nor its own `_TIMEOUT`
  sentinel ever appeared — added one more diagnostic (`LINDOS_DESKTOP_WATCH_STARTED`, printed as
  the watcher's very first action) rather than burn a full kernel+ISO+boot-test cycle guessing
  blind, since this is non-blocking (never gates pass/fail).
- Added 12 new hermetic tests across these two rounds in `build/tests/test_boot_test_qa.py`
  (`check()`'s FAIL_LOG tail, `check_compat_doctor()`'s DOCTOR_FAIL lines + parse-error fallback +
  the stdout/stderr-race regression itself, the desktop-watch script's ready/timeout paths run
  standalone against faked `systemctl`/`pgrep`, and `parse_report`/`tail_for_desktop_ready`
  coverage) — this script had *zero* test coverage before beyond `bash -n`/shellcheck, which is
  exactly how the `$?`-clobbering bug shipped unnoticed.

### Merge: `feature/preinstall-essentials` → this branch (clean, no conflicts)

A parallel agent's branch (Chrome auto-install on the *installed* system's first boot only —
never baked into the ISO, per Google's redistribution terms; laptop firmware/audio/Bluetooth/
microcode/printing essentials via a new `LAPTOP_ESSENTIALS` list; Bluetooth no longer disabled by
debloat) merged cleanly. Follow-up: `lindos-driver-firstboot.service` had no live-session guard
unlike the new `lindos-browser-firstboot.service` — added the same
`ConditionKernelCommandLine=!boot=casper` (a boot-test/live-USB session would otherwise run full
hardware autodetect every single boot, since it can never persist its done-marker to a live
session's non-persistent `/var/lib`).

### Round 1 of the post-merge final run (`36294817595` ❌ lint-test, ❌ never reached kernel/boot-test)

`lint-test` failed 4 tests in `packages/lindos-core/tests/test_core.py`
(`test_browser_firstboot_*`), all with `"...must run as root"` instead of their intended
behavior. Root cause: `_firstboot_sandbox()`'s fake `id` binary (simulates root for
`browser-firstboot.sh`'s `[ "$(id -u)" != 0 ]` check) was written via `Path.write_text()` with
**no `.chmod(0o755)`**. Windows/git-bash never notices (NTFS has no POSIX exec bit, a shebang
script "just runs"), but real Linux skips a non-executable match during `PATH` search and falls
through to the *real* `/usr/bin/id`, which reports the CI runner's actual non-root uid. The fake
`install-browser.sh` had the same gap (harmless only because every failure happened before
reaching it). Fixed both with `.chmod(0o755)`.

### Round 2 (`36295427771` ❌ kernel job, ❌ boot-test — different failures each)

- **Kernel job**: `dpkg-checkbuilddeps: error: Unmet build dependencies: debhelper-compat (= 12)`.
  The "Install kernel build dependencies" ci.yml step never included `debhelper`. Added it.
- **boot-test**: crashed before producing *any* artifact —
  `boot_test.py: error: argument --require-kernel-suffix: expected one argument`. `ci.yml` passed
  `--require-kernel-suffix -lindos` as two argv tokens; since the value itself starts with `-`,
  argparse mistakes it for another option and refuses to consume it — a textbook gotcha, fixed
  with the `--opt=value` form (`--require-kernel-suffix=-lindos`), which is unambiguous regardless
  of what the value looks like. This flag is only ever appended when `build_kernel=true`, so it
  had literally never been exercised before this run (first time that combination ran on this
  branch, or possibly ever in this repo's history). Added a regression test using the real
  argparse parser via `boot_test.main()`.

### Round 3 (`36297620250` ❌ kernel job — new dependency; boot-test — expected downstream failure only)

This was the **cap** (2 fix-and-rerun rounds for this final run) — stopped here rather than
attempting a 4th fix round; reporting root causes for whoever picks this up next.

- **Kernel job**, new failure (progress — got past `debhelper`, into the actual compile):
  `scripts/gendwarfksyms/gendwarfksyms.h:6:10: fatal error: dwarf.h: No such file or directory` →
  `dpkg-buildpackage: error: make -f debian/rules binary subprocess returned exit status 2`.
  Kernel 6.14's build needs `libdw-dev` (provides `dwarf.h`) for `gendwarfksyms`, a DWARF-based
  symbol-versioning tool — **not** the same package as the already-installed `dwarves` (which
  provides `pahole`). **Not yet fixed** (out of round budget) — the fix is almost certainly just
  adding `libdw-dev` to ci.yml's "Install kernel build dependencies" step, but that is unverified
  since the round cap was hit first.
- **boot-test**: the argparse fix from round 2 is confirmed working (no more crash, an artifact
  was produced). The smoke test itself is **fully green** —
  `LINDOS_SMOKE_DONE rc=0`, all 10 `LINDOS_CHECK`s `OK` (including `lindos-update-check=OK rc=3`).
  The job still reports overall FAIL solely because `--require-kernel-suffix=-lindos` correctly
  detected that the booted kernel (`uname -r=6.14.0-29-generic`) is the **stock** kernel, not
  Lindos's — the exact, expected, consistent downstream consequence of the kernel job failing
  above (no `lindos-kernel-debs` artifact existed for `35-kernel.sh`/`build-iso.sh`'s
  `sync_casper_kernel()` to install and promote into `casper/vmlinuz`). Not a new/independent bug.
  `desktop_watch_started=True` (the new `LINDOS_DESKTOP_WATCH_STARTED` marker confirms the watcher
  process really does start), but neither `LINDOS_DESKTOP_READY` nor its own `_TIMEOUT` sentinel
  ever appeared, same as the previous round — **still unresolved and undiagnosed further** (round
  cap reached before this could be investigated deeper); the live-desktop screenshot is still
  black (`is_system_running=initializing`) on every run so far, informational only (never gates
  pass/fail — see `boot_test.py`'s `take_screenshot()`).

**Local verification for every round above:** `bash tests/run.sh --quick` (0 failures, shellcheck
clean) and the full `pytest` suite (3128 → 3153 passed as tests were added across rounds, 4
pre-existing Windows-only skips) were run before every commit in this sequence.

---

## 2026-09-27 — libdw-dev + the real black-screen root cause, 3 rounds (repo migrated C: → E: mid-session)

Picked up exactly where the previous entry left off. Three fix-and-rerun rounds (the budgeted
cap), each finding one real, previously-invisible bug — every one of them the direct, honest
consequence of finally getting one layer deeper into a boot that had never gotten this far before.

### Round 1 (`36300817476` — ✅ all 7 jobs green, including kernel and boot-test)

- **Kernel job**: added `libdw-dev` (provides `dwarf.h`, needed by `scripts/gendwarfksyms` on
  this kernel series — a different package from `dwarves`, which only provides `pahole`) to
  ci.yml's "Install kernel build dependencies" step, and a matching `dpkg-query` check in
  `build/kernel/build-kernel.sh`'s own `check_deps()` (kept in sync, per the same pattern as the
  existing `libssl-dev`/`libelf-dev` checks). Kernel job went from failing on
  `gendwarfksyms.h: fatal error: dwarf.h: No such file or directory` to a clean build:
  `uname -r=6.14.0-lindos`, confirmed by `--require-kernel-suffix=-lindos`.
- **Black screenshot — real root cause, not a guess.** Grepped `out/ci-boot-test/serial.log`
  (per the task) and found the guest beginning a full `shutdown.target` teardown — including
  "Stopping lindos-desktop-watch.service" — moments after `LINDOS_SMOKE_DONE`, with
  `is_system_running` still `initializing`. Cross-checked against systemd's own
  `kernel-command-line(7)`/`systemd-run-generator(8)` docs: **`systemd.run=`'s generated
  `kernel-command-line.service` defaults to `SuccessAction=exit`/`FailureAction=exit`** — i.e. it
  powers the whole VM off the instant the smoke-test command finishes. The smoke test itself
  (a handful of quick CLI checks) completes in well under a second, so the guest was shutting
  down before LightDM/XFCE — or the detached desktop-ready watcher's own 150s loop — ever got
  anywhere near finishing. This is a test-harness artefact of how `boot_test.py` invokes
  `systemd.run=`, not a Lindos bug (a real boot never passes `systemd.run=` at all). Fixed by
  appending `systemd.run_success_action=none systemd.run_failure_action=none` to the kernel
  command line in `build_qemu_argv()`.
- Extended the detached desktop-ready watcher (never the main smoke script, which must stay
  non-blocking) with `LINDOS_DESKTOP_DIAG`-prefixed diagnostics printed right before either of its
  own sentinels: `systemctl is-system-running`/`list-jobs`/`--failed`/`status lightdm`, an
  Xorg.0.log EE/WW tail, `loginctl list-sessions`, `fgconsole`, `/sys/class/drm/*/status`, and
  `lsmod | grep bochs|drm|virtio`. `boot_test.py`'s `parse_report()`/`main()` now surface these in
  the report too.
- With the shutdown fixed, the watcher's own diagnostics finally told us something new: the
  system genuinely reached `is-system-running=running` (0 failed units) — but `lightdm.service`
  was `inactive (dead)`, `Loaded: ...; indirect; preset: enabled`, with **zero journal lines ever
  recorded for it** — never crashed, never even triggered. DRM was fine
  (`/sys/class/drm/card0-Virtual-1/status=connected`, `bochs` module loaded), so this wasn't the
  QEMU-display suspect from the original brief. Everything pointed at `graphical.target` simply
  never being the active default target.

### Round 2 (`36313769193` — ✅ kernel/ISO green, ❌ boot-test)

- Added an explicit, guarded, idempotent `systemctl set-default graphical.target` +
  `systemctl enable lightdm.service` (no `--now`) to `packages/lindos-desktop/DEBIAN/postinst`.
  lindos-desktop owns the desktop experience, so it should not depend on some other package (or
  incidental base-image state) having already gotten this right.
- Added `systemctl get-default`, the `display-manager` alias's own status, its symlink target,
  and `/etc/X11/default-display-manager` to the watcher's diagnostics, to get a decisive answer
  either way on the next run.
- **This fix visibly worked**: for the first time ever, serial.log showed `Starting
  lightdm.service - Light Display Manager...`, alongside a real full graphical-session boot —
  NetworkManager, wpa_supplicant, udisks2, gpu-manager, ubiquity, blueman, polkit, all genuinely
  running (none of this had ever happened on any previous run; `is-system-running` had always
  either stayed `initializing` or (round 1) reached `running` via a much smaller, non-graphical
  target). But `LINDOS_SMOKE_DONE` never arrived within the 600s `--timeout` — a clean "TIMEOUT
  waiting for the boot smoke test to finish" with **zero `LINDOS_*` output at all**, not even
  `LINDOS_SMOKE_START`. The desktop.png screenshot was 53 KB this round (vs. ~4.8 KB on every
  prior black-screen run) — real rendered content, not flat black.

### Round 3 (`36319809802` — the final round; ✅ kernel/ISO green, ❌ boot-test — new blocker found, not yet fixed)

- Reasoned the 600s timeout was simply too short now that the guest does dramatically more real
  work (30+ concurrent unit starts) on the job's default 2 vCPUs. Bumped `boot_test.py`'s
  `--timeout` 600s → 1500s, added `--cpus 4` (matching the `ubuntu-24.04` runner's real core
  count), and the job's own `timeout-minutes` 40 → 55.
- **This did not help, and disproved the "just needs more time" theory**: serial.log for this
  round is close to byte-for-byte identical to round 2's, stopping at the exact same point —
  `Starting lightdm.service...` → `Starting plymouth-quit-wait.service...` →
  `Finished nvmf-autoconnect.service` → a bare `mint login:` getty prompt on `ttyS0` — regardless
  of whether the harness waited 600s or the full 1500s. `lightdm.service` never printed a
  `Started`/`Failed` line either time. **This is a stable end state, not a slow-but-progressing
  boot**: lightdm.service starts and then simply never finishes starting, for at least 25 real
  minutes under real KVM with 4 vCPUs. `kernel-command-line.service` (our own `systemd.run=` unit)
  never ran at all in either round 2 or round 3 — strong evidence it is ordered/gated behind
  `default.target` settling (matching this repo's own existing comment that it "is itself part
  of default.target's own start-job graph"), and since `default.target` is now correctly
  `graphical.target`, a hung `lightdm.service` blocks the whole transaction from ever completing,
  taking our own smoke-test script down with it. The screenshot this round was QEMU's own literal
  placeholder, **"Guest has not initialized the display (yet)"** — the guest had not drawn a
  single frame to the emulated VGA device in 25 minutes, consistent with Xorg (lightdm's child)
  never getting far enough to do a modeset.
- **Not yet fixed** — this is a genuinely new, real problem (present on real hardware too, not a
  QEMU-only artefact, though QEMU's `-vga std`/bochs-drm combination with Xorg's modesetting DDX
  is one of the "likely suspects" named at the start of this work and is the most promising next
  thing to try) — found honestly via the round-3 evidence above, not fixed, because the 3-round
  cap for this task was reached first.

**Local verification for every round above:** `bash tests/run.sh --quick` (shellcheck clean, 0
failures) and the touched pytest files (up to 152 passed) were run before every commit. The full
pytest suite was not re-run in full this session (only the files actually touched), per the task's
own scope.

**Mid-session infrastructure note**: the working copy was migrated from
`C:\Users\Hp\Desktop\Nitish-Code\Lindos` to `E:\Nitish\Lindos` partway through round 2 (the C:
drive was full). One round of edits (the postinst fix + extra diagnostics + their tests) was
made on the stale C: copy before the migration was fully internalized and had to be re-applied
file-by-file onto E: before it could be committed from there — worth double-checking working
directory discipline explicitly after any future workspace move, since the tools that take
absolute paths (Read/Edit/Write) don't follow a shell `cd`.

## Next CI step (not yet run)

The kernel build, the ISO build, and the "no real desktop attempted" black-screen bug are now
**fully fixed and confirmed** (rounds 1–2). The **new, currently-open blocker** is: once LightDM
genuinely tries to start (which is the correct, intended behavior now), it never finishes —
`lightdm.service` stays `Starting` forever, the display stays un-initialized, and our own
`systemd.run=` smoke-test script never even runs because it appears to be gated behind
`default.target` (= `graphical.target`) actually settling. Candidates for whoever picks this up
next, roughly in order of how cheap they are to try:

1. Swap the boot-test harness's `-vga std` for `-vga virtio` / `-device virtio-vga` in
   `build/qa/boot_test.py`'s `build_qemu_argv()` — `bochs-drm` + Xorg's modesetting DDX is a much
   less battle-tested combination than `virtio-gpu`, which is the standard for headless/CI X
   testing. This is the cheapest thing to try and was explicitly flagged as a candidate at the
   start of this work.
2. Confirm the dependency-ordering theory directly: `systemctl show kernel-command-line.service
   -p After -p Requires -p Wants` on a real boot (or a local QEMU session with a monitor attached
   interactively) would show definitively whether it is really gated on `graphical.target`.
3. If it is real hardware-affecting (not just a QEMU-display artefact), consider giving
   `lightdm.service` (or its Xorg child) an explicit `TimeoutStartSec=` so a real install fails
   fast and visibly instead of hanging the whole boot forever — hanging forever with no operator
   feedback is a bad failure mode regardless of root cause.
4. Once the guest can actually run `ci-boot-smoke-test.sh` again, the `LINDOS_DESKTOP_DIAG`
   diagnostics already added (Xorg.0.log EE/WW tail, `display-manager` status, DRM status, lsmod)
   should immediately show Xorg's own failure/hang signature — no further blind guessing needed.

```bash
gh workflow run CI --ref feature/windows-transfer-updates-boottest -f build_kernel=true -f build_iso=true -f boot_test=true
gh run list --workflow CI --branch feature/windows-transfer-updates-boottest --limit 1 --json databaseId
gh run watch <run-id> --exit-status   # not through tail
```

## 2026-09-28 — boot-test finally GREEN (2 more rounds, same 3-round cap; `36362190703` → `36370906849` → `36381907329`)

Picked up exactly where the 2026-09-27 session left off (round 3's open blocker above). Two more
rounds, both real bugs found and fixed, not guesses:

### Round 4 (`36362190703` → fix → `36370906849`, diagnostic round)

- `36362190703` (the round-3-era run, re-checked at the start of this session) showed the same
  "`lightdm.service`/`plymouth-quit-wait.service` start, then serial.log goes silent" symptom.
  Leading theory: once the round-3 `-vga virtio`/`-device virtio-vga` fix (already landed,
  `93fa12f`) gave plymouth a *working* graphical splash for the first time, systemd's normal
  practice of not mirroring unit-status text to the console while a splash is up would explain the
  silence. Fixed by appending `plymouth.enable=0` to `boot_test.py`'s kernel command line (`quiet`/
  `splash` were never appended here to begin with) so systemd's verbose status keeps flowing to
  `ttyS0` regardless of the display.
- **This fix worked as a diagnostic, but disproved its own leading theory.** Run `36370906849`
  (plymouth fully disabled) showed the *entire* boot in the clear — full NetworkManager/ubiquity/
  polkit/etc. startup, `lightdm.service`/`plymouth-quit-wait.service` starting — and then **still
  zero `LINDOS_SMOKE_START`**, for the full 1500s/25-real-minute timeout, ending in a bare `mint
  login:` getty prompt. Crucially, `serial.log` **never once printed "Reached target
  multi-user.target" or "Reached target graphical.target"**, even though ordinary
  `multi-user.target` `Wants=` units (`lindos-sensors-detect.service`, `NetworkManager`, `ubiquity`,
  …) ran fine in parallel the whole time. This is the real, now-confirmed cause: `systemd.run=`'s
  generated transient unit (`kernel-command-line.service`) is gated behind `default.target` (=
  `graphical.target` here) actually reaching "active" — exactly the "round 3" hypothesis from
  2026-09-27's entry above, now proven rather than guessed at.

### Round 5 (`36381907329` — ✅ ALL GREEN, including boot-test)

- Stopped relying on the kernel command line's `systemd.run=` mechanism entirely. Added
  `lindos-ci-boot-smoke-test.service` (packages/lindos-core), a real shipped oneshot unit —
  `ExecStart=` the same `ci-boot-smoke-test.sh` — gated by
  `ConditionKernelCommandLine=lindos.ci_boot_test` (a bespoke flag only `boot_test.py` ever
  appends; never on a real end-user boot) and ordered only against `basic.target` +
  `WantedBy=multi-user.target` (the pull-in point, not an ordering dependency — proven safe by
  `lindos-sensors-detect.service`, which uses the identical pattern and was directly observed
  starting right after `basic.target`, independent of whether `multi-user.target` itself ever
  settles). Enabled via `lindos-tune`'s `90-lindos.preset` (+ its `apply.py` source of truth).
- **Result: full green run, all 7 jobs, including `boot-test the ISO (QEMU/KVM)` in 6m18s** (down
  from the previous 25-minute timeout-and-fail). The smoke test's own report:
  `LINDOS_SMOKE_DONE rc=0`, all 10 `LINDOS_CHECK` lines `OK` (`python-import`, `lindos-mode`,
  `lindos-config`, `lindos-ram`, `lindos-tune`, `lindos-compat-doctor` — `OK rc=1`, no DISPLAY in
  this early-boot context, expected — `lindos-run-version`, `lindos-game-list`,
  `lindos-transfer-sources`, `lindos-dualboot-status`, `lindos-update-check` — `OK rc=3`, the
  designed `EXIT_NOTHING`), `failed_units=0`, kernel `uname -r=6.14.0-lindos` (confirmed via
  `--require-kernel-suffix=-lindos`), and the harness's own final verdict: **`===== PASS =====`**.
- **The live desktop rendered for the first time ever in this harness.** `desktop.png` (52.8 KB,
  vs. ~4.8 KB for every prior black-screen run) is a full, legible screenshot of the real
  `lindos-setup` first-boot OOBE wizard ("Welcome to Lindos" — mode picker, RAM detection banner,
  "Get started" button). The desktop-ready watcher itself still reported
  `LINDOS_DESKTOP_READY_TIMEOUT` (its own internal 150s poll loop ran out before
  `is-system-running` reached `running`/`degraded`) — its own diagnostics at that moment showed
  `lightdm.service` was actually `Active: active (running)` already, with `multi-user.target`/
  `graphical.target` still `start waiting` behind a handful of slow-but-not-hung units
  (`lindos-sensors-detect.service` still `start running`, `lm-sensors.service`/
  `fancontrol.service` waiting on it, `casper-md5check.service` — a live-session ISO integrity
  check — also still waiting). `boot_test.py`'s own unconditional post-watcher `--grace` period
  (90s, on top of the watcher's 150s) was enough extra time for the desktop to actually finish
  rendering, hence the real screenshot despite the watcher's own timeout. This watcher-timeout-vs-
  actually-fine gap is a minor, non-blocking loose end (the watcher never gates pass/fail) worth a
  closer look later — possibly just bump its poll count, or extend `LINDOS_DESKTOP_DIAG` to log
  `casper-md5check.service`'s own progress — but is **not** a repeat of round 3's real hang:
  `lightdm.service` genuinely started and reached `active (running)` this time, with a
  `Drop-In: lindos-timeout.conf` confirmed loaded.
- **ISO size**: `lindos-1.0.0-xfce-64bit.iso` = 3,842,011,136 bytes (3.58 GiB), confirmed built
  with the `-dbg` kernel-debug-symbols exclusion (`f652d95`, already landed before this session)
  in effect end-to-end: `linux-image-6.14.0-lindos-dbg_*.deb` (1.33 GB) was produced by the kernel
  build but excluded from both the `lindos-kernel-debs` artifact upload (`!out/kernel/linux-image-
  *-dbg_*.deb`) and the ISO chroot install (`35-kernel.sh` only ever saw/`dpkg -i`'d the
  non-`-dbg` `linux-image`/`linux-headers` debs).

**Local verification for both rounds**: `bash tests/run.sh --quick` (shellcheck clean) before every
commit, plus — round 5 only, since it touched cross-package plumbing (a new lindos-core unit
enabled via a lindos-tune preset) — the full repository pytest suite via the project's real
config (`python -m pytest -c tests/pytest.ini --rootdir . -p lindos_testsupport tests
packages/*/tests build/tests`): **3138 passed, 18 skipped**. The only errors (14, confined to
`lindos-transfer`'s Windows junction-harness tests) were a same-session environment artifact —
`mklink /J` failing with "Local NTFS volumes are required to complete the operation" specifically
because `TMPDIR` was redirected to the `E:` drive (`C:` was at 0 bytes free all session) — confirmed
unrelated to any file either round touched, and not expected to reproduce on the real Windows CI
runner (a genuine local NTFS volume).

**Still open, not addressed this session** (none block the ISO/kernel/boot-test pass/fail; ordered
by how much they'd matter for a real user):
1. Why `multi-user.target`/`graphical.target` take long enough to still be `start waiting` at the
   desktop-watcher's own 150s mark (see above) — likely just `casper-md5check.service` (live-ISO
   integrity check over the whole squashfs) plus `lindos-sensors-detect.service`'s deliberately
   `Nice=10`/`IOSchedulingClass=idle` hardware scan being slower than 150s under CI's shared vCPUs,
   not a hang — but never confirmed with a direct trace on this run since the VM was torn down
   right after the screenshot. Worth a longer/instrumented watcher poll next time to get a clean
   `is-system-running=running` confirmation instead of inferring it from the screenshot alone.
2. `docs/COMPATIBILITY.md` freshness / cosmetic Node20-deprecation warnings from `CONTINUATION.md`
   §4 items 3–6 remain outstanding, unrelated to this session's scope.
