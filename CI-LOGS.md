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

## Next CI step (not yet run)

The kernel-compile and ISO-build jobs have **never run** — they're manual. To produce the first
bootable image and the tuned kernel `.deb`s:

```bash
gh workflow run CI -f build_kernel=true -f build_iso=true
gh run list --limit 1                 # get the new run id
gh run watch <run-id> --exit-status   # not through tail
```

Expect first-time-on-real-Linux shake-out (root/loop-device/`lintian`/ISO-tooling issues). Read
`gh run view <run-id> --log-failed`, fix like the 6 above, push, repeat until green. Artifacts land
under the run: `lindos-debs`, `lindos-kernel-debs`, `lindos-iso` (+ `.sha256`, `build.log`).
