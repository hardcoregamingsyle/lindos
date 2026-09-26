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

## Next CI step (not yet run)

The kernel-build job (`build_kernel=true`) has still never run on this branch — per the original
task plan, dispatch it only after the no-kernel loop is fully green (still pending, see above):

```bash
gh workflow run CI --ref feature/windows-transfer-updates-boottest -f build_iso=true -f boot_test=true -f build_kernel=true
gh run list --workflow CI --branch feature/windows-transfer-updates-boottest --limit 1 --json databaseId
gh run watch <run-id> --exit-status   # not through tail
```

Expect the same kind of first-time-on-real-Linux shake-out documented above. Artifacts land under
the run: `lindos-debs`, `lindos-kernel-debs`, `lindos-iso` (+ `.sha256`, `build.log`),
`lindos-boot-test` (serial log + screenshot; `boot_test=true build_kernel=true` also asserts the
booted kernel version ends in `-lindos`).
