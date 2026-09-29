# Lindos — Continuation Guide (agent handoff)

_Last updated: 2026-09-29 (the installer flow was rebuilt — see §3 and §4 item 9; boot-test green since
2026-09-28, §4 item 2). Written for the next agent/developer taking over this project._

Lindos is a **Windows-11-style remaster of Linux Mint 22.x XFCE** (Ubuntu 24.04 "noble" base):
first-boot OOBE, 5 modes, a Win11 desktop, one-click Wine/Proton `.exe`, a gaming stack, heavy
RAM tuning (350–500 MB idle target), a custom tuned kernel, an honest KVM Windows VM + seamless
Windows apps, driver automation, and real CI. **It is not Windows and never pretends to be.**

---

## 0. The single most important thing to preserve: the honesty rules

These are **binding** and enforced by tests. Do **not** weaken or remove them, and do not implement
anything that violates them, even if asked — explain why instead. They are specified in
`SPEC.md §0.1`, `SPEC-KERNEL.md §14`, and `SPEC-VM.md §20`.

- **No anti-cheat / DRM circumvention of any kind.** No "Windows attester", no attestation /
  TPM-EK / Secure-Boot / PatchGuard / HVCI forger, no HWID/UUID/SMBIOS/ACPI/CPUID spoofing, no
  hypervisor-hiding (`kvm=off`, `hv-vendor-id` masquerade, hiding the hypervisor CPUID bit), no
  VM-detection evasion. **Why:** such a tool cannot produce the hardware-rooted signature the game
  server verifies, so it does not work; it only gets the user **permanently hardware-banned**, and
  it is a cheat/evasion tool. Kernel-mode anti-cheat (Riot Vanguard; titles whose publisher
  disabled EAC/BattlEye on Linux) is **impossible on any Linux kernel**, including the tuned
  `lindos-kernel` and inside `lindos-vm` (Vanguard blocks VMs). Say so plainly; never imply Lindos
  changes it.
- **`lindos-vm` is a truthful VM.** Standard libvirt XML, real host-passthrough CPU, genuine
  emulated TPM, fresh per-VM UUID. Enforced by `packages/lindos-vm/tests/test_no_spoof.py` and
  `packages/lindos-gaming/tests/test_*` FORBIDDEN_TOKENS lists — these **fail the build** if any
  evasion token ever appears. Keep them.
- **No license/credential handling.** Lindos never downloads Windows, never enters or stores the
  user's Windows/app credentials, never bypasses activation. `lindos-winapps` uses the user's own
  licensed Windows over RDP.

The user asked for a spoofer/attester several times; each time it was declined for the reasons
above and the honest alternative (tuned kernel, Proton stack, real VM, WinApps) was built instead.
If asked again, decline the circumvention and offer the honest capability.

---

## 1. Where everything is

- **Local repo:** `C:\Users\Hp\Desktop\Nitish-Code\MiniLLM`'s sibling → `C:\Users\Hp\Desktop\Nitish-Code\Lindos`
- **GitHub:** https://github.com/hardcoregamingsyle/lindos (private), branch `main`
- **Contract files (code is law; these are the binding spec):**
  - `SPEC.md` — base distro (§0–§13, + §16 pointer to Addendum W)
  - `SPEC-KERNEL.md` — Addendum K: kernel + compat-performance (§14–§19)
  - `SPEC-VM.md` — Addendum V: VM/WinApps/Proton-system/drivers/CI (§20–§26)
  - `SPEC-WINDOWS.md` — Addendum W: every Windows format, Transfer, Play-anywhere (§27–§34)
  - Rule: if code and SPEC disagree, one of them is wrong — never silently diverge. Update the SPEC
    when you intentionally change a contract.
- **CI log/history:** `CI-LOGS.md` (this repo).

## 2. Repo map

```
SPEC.md, SPEC-KERNEL.md, SPEC-VM.md, SPEC-WINDOWS.md   the binding contracts
README.md, CONTRIBUTING.md, LICENSE (GPL-3.0-or-later), THIRD_PARTY.md
Makefile                              make debs | assets | iso | iso-docker | qemu | kernel | test | lint
packages/                             13 Debian packages (DEBIAN/ + root/ trees):
  lindos-core      python lib `lindos.*` (paths/config/modes/browsers/hardware/helper/theme/compat/
                   ram/dualboot) + privileged helper /usr/libexec/lindos/lindos-helper + polkit +
                   CLIs incl. lindos-dualboot (one-shot restart into an existing Windows install)
  lindos-desktop   XFCE Win11 look: panel, themes, shortcuts, branding, autostart
  lindos-setup     first-boot OOBE (GTK) — mode, browser, personalise, privacy, transfer, apply; install-free
                   (the installer does the installing); gated off in the live session and for the `oem` user
  lindos-settings  Win11-style settings centre (12 pages, GTK)
  lindos-compat    Windows apps: lindos-run (every Windows file type: .exe/.msi/MSIX-APPX/.reg/.ps1/
                   .vbs/.url/.scr/.cpl/.inf/.cab/disk images/DOS/16-bit/ClickOnce/…), recipes,
                   profiles loader, perf env, winget install, binfmt_misc (./setup.exe in a terminal)
  lindos-gaming    lindos-proton / lindos-drivers / lindos-game (route/play/shortcut/cloud install) /
                   lindos-gamescope + compat-matrix (incl. cloud_providers/routes) + profiles
  lindos-tune      RAM/perf: zram, earlyoom, sysctl, governor, sched_ext (scx), THP, fan/power, per-mode
  lindos-kernel    tuned kernel: kconfig fragment (ntsync/sched_ext/1000Hz/MGLRU) + build recipe + CLI + grub
  lindos-vm        honest KVM/VFIO Windows VM (q35+OVMF+virtio+swtpm), single-GPU passthrough
  lindos-winapps   seamless Adobe/Office over FreeRDP RemoteApp against a Windows backend
  lindos-transfer  Windows Easy Transfer-style migration (read-only) + Windows-side kit + GUI
  lindos-meta      metapackage (Depends the nine packages above incl. lindos-transfer;
                   Recommends lindos-kernel/-vm/-winapps)
  lindos-installer the installer flow (13th package, installation medium only, removed from the installed
                   system): Ubiquity target-config hook `target-config.sh` + `lib.sh` + success command
                   `finalize.sh`, `extras.json`, `lindos.seed` (SPEC §17). Not a Depends of lindos-meta
build/             ISO remaster + .deb pipeline: build-iso.sh, mkdeb.sh, fetch-assets.sh,
                   chroot/00..80 hooks (+35-kernel.sh, 75-vm.sh, 79-installer-flow.sh), lib/ (boot_menu.py,
                   installer_extras.py, verify_oem_pool.py), kernel/build-kernel.sh, Docker, config.env
docs/              README-level docs incl. COMPATIBILITY.md (GENERATED — see below), ANTI-CHEAT.md,
                   KERNEL.md, VM.md, WINAPPS.md, DRIVERS.md, MODES.md, SETTINGS.md, WINDOWS-FORMATS.md,
                   WINGET.md, TRANSFER.md, DUALBOOT.md, etc.
tests/             run.sh (the gate), conftest.py, lindos_testsupport.py (gi stub + sys.path),
                   gen-compat-doc.py (regenerates docs/COMPATIBILITY.md)
.github/workflows/ci.yml   lint+pytest (Ubuntu+Windows), debs+lintian, kernel (manual), iso (manual)
out/               git-ignored build products (ISO, debs, kernel, acceptance logs)
```

## 3. Current state

- **2026-09-29 (installer flow rebuilt — written and unit-tested, NEVER run end to end):** the user's
  contract is (1) the live USB session is only the installer (no wizard, no installs, no prompts, never
  sleeps); (2) the installer does everything heavy while installing — updates, drivers, Chrome (downloaded
  from Google's apt repo), Wine/Proton, launchers, Mode apps, Flatpaks; (3) after the reboot the first boot
  only asks for the account and personalisation. Implemented with **Ubiquity OEM mode**: boot entries
  `Install Lindos` (`only-ubiquity oem-config/enable=true …`) / compat / `Try Lindos` / compat / integrity
  check (GRUB, loopback and the generated isolinux menu; the old "OEM install" entry is gone); one target-config
  hook `/usr/lib/ubiquity/target-config/50lindos-install` (deployed by `build/chroot/79-installer-flow.sh`)
  does all downloading/installing, time-boxed, always exit 0, kernel/boot-loader/Ubiquity held; the
  `ubiquity/success_command` `finalize.sh` (<5 s) arms oem-config and locks the temporary account; install
  results go to `/var/lib/lindos/install-state.json` (`lindos.installstate`), the silent retries
  (`browser-firstboot`, `driver-firstboot`) and Settings › Apps › "Left to finish from setup" pick up what is
  pending; `lindos.session` / `is-live-session` gate the live session; `lindos-setup` is install-free (no Apps
  page). Docs: `docs/INSTALLER.md` (users + the honest known-limitations list), `docs/BUILDING.md` "Installer
  flow" (maintainers, QEMU procedure), SPEC §17.
  - **Verified only by hermetic tests** (fake `/target`, fake runners, `LINDOS_TEST_CMDLINE`; the last
    reported runs: ~2300 tests in installer/build/tests/tests/compat/gaming/core and 700 in
    tests/desktop/core/settings/setup, 0 failures, the only skips being POSIX-only on this Windows host;
    `bash tests/run.sh --quick` green incl. ShellCheck 0.11.0). Nothing ran on Linux: no `.deb` was built,
    no ISO built, no QEMU install. The full-suite gate (`bash tests/run.sh`) was not re-run by the docs pass.
  - **Base ISO facts** came from reading the real Mint 22.2 XFCE ISO's file tree over HTTP and Ubiquity's
    upstream source (not from running anything): no `preseed/` directory (so `@PRESEED@` is empty),
    `oem-config`/`oem-config-gtk` `24.04.3+mint18` in `/pool`, isolinux menu with `username=mint`,
    Ubiquity's target-config hook semantics (`plugininstall.py`). Re-check them when `MINT_VERSION` changes.
  - What to do next: §4 item 9.
- **2026-09-27 (new session — `libdw-dev`, the real black-screen root cause, 3 rounds, now
  blocked on a newly-found lightdm hang):** Full detail in `CI-LOGS.md`; summary here.
  - **Kernel job fixed**: added `libdw-dev` (provides `dwarf.h`, needed by
    `scripts/gendwarfksyms`) to ci.yml + `build-kernel.sh`'s own `check_deps()`. The kernel now
    builds clean every run: `uname -r=6.14.0-lindos`, confirmed booting via
    `--require-kernel-suffix=-lindos` (run `36300817476`).
  - **Black-screenshot root cause found and fixed (round 1, run `36300817476`)**: `systemd.run=`'s
    generated `kernel-command-line.service` defaults to `SuccessAction=exit`/`FailureAction=exit`
    (systemd's own docs) — the guest was powering itself off the instant the smoke-test command
    finished (well under a second), never giving LightDM/XFCE a chance to start. A test-harness
    bug, not a Lindos bug. Fixed by appending `systemd.run_success_action=none
    systemd.run_failure_action=none` to `boot_test.py`'s kernel command line.
  - **Second real bug found and fixed (round 2, run `36313769193`)**: with the premature shutdown
    gone, the watcher's new diagnostics showed `is-system-running=running` with zero failed units,
    yet `lightdm.service` was `inactive (dead)` with not one journal line ever recorded for it —
    `graphical.target` was simply never the active default target. Fixed by adding an explicit
    `systemctl set-default graphical.target` + `systemctl enable lightdm.service` to
    `packages/lindos-desktop/DEBIAN/postinst` (lindos-desktop owns the desktop experience; it
    should not depend on incidental base-image state). **This visibly worked**: `lightdm.service`
    started for the first time ever, alongside a real full graphical-session boot (NetworkManager,
    wpa_supplicant, udisks2, gpu-manager, ubiquity, blueman, polkit all genuinely running).
  - **Third, currently-open bug found (round 3, run `36319809802`, the final round — not yet
    fixed)**: `lightdm.service` starts and then never finishes starting — stays `Starting` forever,
    the guest's display stays QEMU's literal "Guest has not initialized the display (yet)"
    placeholder, for at least 25 real minutes under real KVM with 4 vCPUs (ruled out "just needs
    more time/CPU" by giving it both and seeing byte-for-byte the same stopping point). Our own
    `systemd.run=` smoke-test script never runs at all once this happens, apparently gated behind
    `default.target` (= `graphical.target`) actually settling. Next things to try, cheapest first:
    swap `-vga std` for `-vga virtio`/`-device virtio-vga` in `boot_test.py` (bochs-drm + Xorg
    modesetting is one of the original suspects and the least battle-tested combination for
    headless/CI X); confirm the ordering theory with `systemctl show kernel-command-line.service
    -p After -p Requires -p Wants`; consider a `TimeoutStartSec=` on lightdm so a real install
    fails fast instead of hanging forever if this is real (not just a QEMU artefact). Once the
    guest can run the smoke test again, the `LINDOS_DESKTOP_DIAG` diagnostics already in place
    (Xorg.0.log EE/WW tail, display-manager status, DRM status, lsmod) should show the failure
    signature directly.
  - Local gate stayed green throughout: `bash tests/run.sh --quick` (shellcheck clean, 0 failures)
    and the touched pytest files (up to 152 passed) before every commit; the full suite was not
    re-run this session, only the files actually touched, per this task's own scope.
  - Repo working copy moved from `C:\Users\Hp\Desktop\Nitish-Code\Lindos` to `E:\Nitish\Lindos`
    mid-session (C: drive full); the old C: folder was deliberately left in place (not deleted —
    permanent file deletion is outside what gets done without a person doing it directly).
- **2026-09-28 (boot-test GREEN — round 3's blocker fixed, 2 more rounds, same 3-round cap):** Full
  detail in `CI-LOGS.md`'s 2026-09-28 entry; summary here.
  - **Round 4 (`36362190703` → `36370906849`, diagnostic)**: added `plymouth.enable=0` to
    `boot_test.py`'s kernel command line to rule out "plymouth's working splash is swallowing
    console status text" as the cause of round 3's silence. **The fix worked as a diagnostic but
    disproved its own theory**: with plymouth fully disabled, serial.log showed the *entire* boot
    in the clear (full NetworkManager/ubiquity/polkit startup) yet still **zero
    `LINDOS_SMOKE_START`** for the full 25-minute timeout, and — the key new evidence — **never
    once printed "Reached target multi-user.target" or "Reached target graphical.target"**, even
    though ordinary `multi-user.target` `Wants=` units ran fine in parallel the whole time. This
    confirmed round 3's "gated behind `default.target` settling" hypothesis directly, ruling out
    the console-visibility theory.
  - **Round 5 (`36381907329` — ALL GREEN)**: stopped relying on the kernel command line's
    `systemd.run=` mechanism entirely. Added a real shipped unit,
    `lindos-ci-boot-smoke-test.service` (packages/lindos-core), gated by
    `ConditionKernelCommandLine=lindos.ci_boot_test` and ordered only against `basic.target` +
    `WantedBy=multi-user.target` (proven-safe pattern, matching `lindos-sensors-detect.service`).
    **Result: all 7 jobs green, including boot-test in 6m18s** (down from a 25-minute
    timeout-and-fail): `LINDOS_SMOKE_DONE rc=0`, all 10 CLI checks `OK`, kernel
    `uname -r=6.14.0-lindos` confirmed, harness verdict `===== PASS =====`. The live desktop
    rendered for the first time ever in this harness — `desktop.png` is a real, legible screenshot
    of the `lindos-setup` "Welcome to Lindos" OOBE wizard. ISO size: 3.58 GiB
    (3,842,011,136 bytes), confirmed built with the `-dbg` kernel-debug-symbols exclusion in
    effect end-to-end.
  - **Minor open loose end (non-blocking)**: the desktop-ready watcher itself still hit its own
    internal 150s timeout before `is-system-running` reached `running`/`degraded` — its own
    diagnostics at that moment showed `lightdm.service` already `active (running)` (a genuine
    success, unlike round 3's real hang), with `multi-user.target`/`graphical.target` still
    `start waiting` behind `lindos-sensors-detect.service` (still running) and
    `casper-md5check.service` (a live-ISO integrity check), both plausibly just slow under CI's
    shared vCPUs rather than hung. `boot_test.py`'s own unconditional post-watcher grace period
    (90s more) was enough extra time for the real screenshot to still come out fine. Worth a longer
    watcher poll next time to get a clean `is-system-running=running` confirmation directly instead
    of inferring it from the screenshot.
  - Local gate: `bash tests/run.sh --quick` (shellcheck clean) before every commit, plus — round 5
    only, since it touched cross-package plumbing — the full repository pytest suite via the
    project's real config: 3138 passed, 18 skipped (the only errors, 14 in lindos-transfer's
    Windows junction-harness tests, were a same-session `TMPDIR`-on-`E:`-drive environment
    artifact, confirmed unrelated to either round's changes).
- **2026-09-27 (earlier session — self-diagnosis, real-desktop wait, merged
  `feature/preinstall-essentials`, first `build_kernel=true` runs):** Full detail in `CI-LOGS.md`;
  summary here.
  - The **no-kernel loop is now fully green** (`36288980753`): all 10 shipped CLI smoke checks
    pass for real, including `lindos-compat doctor` (its one previously-unexplained required-check
    failure turned out to be a POSIX `if`-with-no-`else` bug clobbering `$?` back to 0, which also
    hid `lindos-update-check`'s designed `rc=3`; both now fixed and self-diagnosing). A detached
    desktop-readiness watcher was added (`LINDOS_DESKTOP_READY`/`_TIMEOUT`, via `systemd-run
    --no-block`) so `boot_test.py` waits for the real desktop before screenshotting — it reliably
    *starts* (confirmed via a new `LINDOS_DESKTOP_WATCH_STARTED` marker) but has never yet reached
    its own final line on any run so far; non-blocking (never gates pass/fail), still unresolved.
  - Merged `feature/preinstall-essentials` (Chrome auto-install on the *installed* system's first
    boot only, laptop firmware/audio/Bluetooth/microcode/printing essentials, Bluetooth no longer
    disabled) — clean merge, no conflicts. Added the same live-session guard to
    `lindos-driver-firstboot.service` that the incoming `lindos-browser-firstboot.service` has.
  - **`build_kernel=true` has now run 2 times on this branch — both still failing, in the kernel
    job itself** (never-before-exercised build-dependency gaps on this runner image): first missing
    `debhelper` (fixed), then missing `libdw-dev` for `gendwarfksyms`/`dwarf.h` (found, **not yet
    fixed** — the 2-round fix cap for that task was reached first). Every OTHER job passes,
    including the smoke-test layer inside boot-test (`LINDOS_SMOKE_DONE rc=0`, all 10 checks OK);
    boot-test's overall FAIL is *only* because no Lindos kernel was built to boot
    (`uname -r=6.14.0-29-generic`, correctly detected and reported by `--require-kernel-suffix`).
    Also fixed along the way: a real argparse gotcha (`--require-kernel-suffix -lindos`'s value
    starts with `-`, so argparse refused to consume it — needed `=` form — this flag had never
    been exercised before since it's only appended when `build_kernel=true`), and a test bug from
    the merged branch (`_firstboot_sandbox()`'s fake `id` binary had no `chmod(0o755)` — invisible
    on Windows, breaks on real Linux where a non-executable PATH match is skipped).
  - The live-desktop screenshot is still black (cursor blink, `is_system_running=initializing`) on
    every run so far, real KVM or not — informational only, never gates the job.
  - Along the way (this and the prior sub-session), real bugs were found and fixed on real Linux
    that Windows-only testing structurally could not have caught: a missing `conffiles` entry, a
    test-unfriendly default parameter in `lindos-game` that silently used the real (unpatched)
    `shutil.which`, a `build/tests/test_build_kernel.py` isolation gap that only worked because a
    Windows host never has a real `apt-cache`, the `xfce4-docklike-plugin` packaging gap (moved
    Depends→Recommends — it isn't on Ubuntu 24.04 noble at all), the POSIX if-without-else `$?`
    bug, a stdout/stderr race in doctor's own JSON output, the fake-`id`-chmod test bug, the
    missing-`debhelper`/`libdw-dev` kernel build deps, and the `--require-kernel-suffix` argparse
    gotcha. Added ~15 new hermetic tests total across `build/tests/test_boot_test_qa.py` for
    everything above; `ci-boot-smoke-test.sh` and `boot_test.py` had zero test coverage before this
    session beyond `bash -n`/shellcheck.
  - Local gate stayed green throughout: `bash tests/run.sh --quick` (0 failures) and the full
    pytest suite (3115→3153 passed as tests were added, 4 pre-existing Windows-only skips) before
    every commit in this sequence.
- **2026-09-26 (Addendum U, as originally written/merged into the above):** `SPEC-UPDATE.md` adds
  a 13th component, `lindos-update` (in `lindos-core`): checks/applies updates to Lindos's own 12
  packages (kernel excluded unless explicitly opted in), an apt-repo publishing pipeline
  (`build/publish-apt-repo.sh`, signed, flat-format), a sideload fallback (install `.deb`s from a
  local folder — usable today, no hosting needed), a `lindos-settings` "Updates" page, and a
  read-only login/6-hourly notifier (`lindos-update-notify`). `LINDOS_APT_REPO_URL` is a
  **placeholder** — nothing hosts the repo yet, so live apt updates don't actually flow until
  someone points it at real hosting (see `docs/UPDATES.md`). Also new: the CI `boot-test` job
  described above.
- **2026-09-26 (Addendum W, as originally written/merged into the above):** 12 packages (new:
  `lindos-transfer`). Addendum W went through research (verified against primary sources),
  implementation, and an adversarial review. The review confirmed 22 findings; 21 are fixed with
  regression tests. That includes a **critical, pre-existing root-helper path traversal**
  (`apply-mode`'s `apply_system` could be walked out of the modes dir with `..`), plus
  `PACKAGE_RE`/`FLATPAK_RE` accepting `-`-prefixed "package names", a non-executable kernel
  sbsign hook, and a dpkg conffile conflict in the kernel grub drop-in.
- 2026-08-16: CI GREEN on real Linux (run 31926992548): Ubuntu lint+pytest, Windows pytest, `.deb`
  build + lintian (artifact `lindos-debs`). The first CI run failed with 6 Linux-only issues
  (tests leaking host state + one `.desktop` typo), fixed in `8c07619`. See `CI-LOGS.md`.

## 4. What is NOT done yet (the actual TODO / next steps)

1. **~~Dispatch the full Linux build on CI~~ — done, on `feature/windows-transfer-updates-boottest`
   (not yet merged to `main`).** `lint-test`/`pytest-windows`/`debs`/`iso` are all green (real
   Linux). **The kernel job is now fully fixed and green** (`libdw-dev` added; run `36300817476`
   onward all build a clean `6.14.0-lindos`, confirmed booting via `--require-kernel-suffix`).
   `lindos-debs`/`lindos-kernel-debs`/`lindos-iso` are all produced correctly every run.
   Re-dispatch:
   ```bash
   gh workflow run CI --ref feature/windows-transfer-updates-boottest -f build_kernel=true -f build_iso=true -f boot_test=true
   ```
   Artifacts: `lindos-debs`, `lindos-kernel-debs`, `lindos-iso` (+ `.sha256`, `build.log`),
   `lindos-boot-test` (serial log + a screenshot of the live desktop — see item 2). Watch:
   `gh run watch <id> --exit-status` (never through `tail`, it hides the real exit code).
2. **~~Boot-test the ISO~~ — GREEN as of run `36381907329` (2026-09-28).** Automated in CI
   (`boot-test` job, `build/qa/boot_test.py`): extracts `casper/vmlinuz`+`initrd` from the built
   ISO, boots them directly under QEMU (KVM-accelerated) with the ISO attached as a CD-ROM, and
   runs `packages/lindos-core/.../usr/libexec/lindos/qa/ci-boot-smoke-test.sh` via a real shipped
   unit, `lindos-ci-boot-smoke-test.service` (`ConditionKernelCommandLine=lindos.ci_boot_test`,
   ordered only against `basic.target` — **not** the kernel command line's `systemd.run=` anymore;
   see below for why), asserting every shipped CLI (`lindos-mode`, `lindos-tune`, `lindos-compat
   doctor`, `lindos-game`, `lindos-transfer`, `lindos-dualboot`, `lindos-update`, a plain
   `python3 -c "import lindos"`) actually runs, then waits for a detached watcher's
   `LINDOS_DESKTOP_READY` signal and captures a screenshot via the QEMU monitor's `screendump`.
   Three real bugs were found and fixed across 2026-09-27/28 (full detail in `CI-LOGS.md`'s
   2026-09-27/28 entries): (1) `systemd.run=`'s generated unit defaulting to
   `SuccessAction=exit`/`FailureAction=exit`, powering the VM off before the desktop could start;
   (2) `lindos-desktop`'s postinst not setting `graphical.target`/enabling `lightdm.service`; (3)
   **the smoke test itself silently never running at all** whenever `multi-user.target`/
   `graphical.target` took a while to settle, because `systemd.run=`'s generated
   `kernel-command-line.service` is gated behind `default.target` actually reaching "active" —
   fixed by replacing it with the real shipped unit above, ordered only against `basic.target` so
   it runs regardless of the desktop's own state. **Result (run `36381907329`): all 7 CI jobs
   green, boot-test in 6m18s, `LINDOS_SMOKE_DONE rc=0`, all 10 CLI checks `OK`, kernel
   `uname -r=6.14.0-lindos` confirmed, and — for the first time ever — a real, legible screenshot
   of the live `lindos-setup` "Welcome to Lindos" OOBE wizard** (not a black screen). ISO size:
   3.58 GiB, confirmed built with the `-dbg` kernel-debug-symbols exclusion in effect.
   **Minor open loose end (non-blocking, never gates pass/fail)**: the desktop-ready watcher's own
   150s poll still timed out before `is-system-running` reached `running`/`degraded` — diagnostics
   at that moment showed `lightdm.service` genuinely `active (running)` (unlike the old hang) with
   `multi-user.target`/`graphical.target` still `start waiting` behind
   `lindos-sensors-detect.service` and `casper-md5check.service`, both plausibly just slow rather
   than hung; `boot_test.py`'s own extra 90s grace period was enough for the real screenshot to
   still land fine. Worth a longer watcher poll sometime to get a direct
   `is-system-running=running` confirmation instead of inferring it from the screenshot alone.
   Manual interactive boot-testing (`make qemu` / `build/test-qemu.sh`) or real hardware is still
   worth doing separately for a visual/hands-on check.
3. **Real-Linux GTK visual pass** for `lindos-setup` (OOBE) and `lindos-settings` — they are only
   import-smoke-tested against the gi stub; construct/paint them under a real GTK + display.
4. **`lindos-vm` / `lindos-winapps` live test** on a machine with KVM + a licensed Windows.
5. Cosmetic: CI warns `actions/checkout@v4` runs on Node20-deprecated shim — bump action versions
   when convenient (not blocking).
6. **Addendum W real-Linux verification** (everything below was built and unit-tested hermetically
   on this Windows host — real Linux/hardware is the next step, same category as items 1–2 above):
   - `lindos-run` against a real ISO/UDF disk image (`udisksctl` loop-mount + AutoPlay), a real
     `binfmt_misc` (`./setup.exe` from a terminal), real DOSBox-X and Wine WoW64 launches, a real
     MSIX/APPX package/bundle from the Store, and a real `.appinstaller` download.
   - `lindos-compat winget install <id>` against the real `cdn.winget.microsoft.com` MSZIP
     container (the byte layout was verified against the research digest and round-tripped with a
     synthetic encoder only — see W-C's `known_gaps`).
   - `lindos-transfer` against a real Windows partition (NTFS3/ntfs-3g, real registry hives,
     BitLocker/dislocker) and a real double-click run of the Windows kit
     (`LindosTransfer.ps1`/`.cmd`).
   - `lindos-dualboot` against real `efibootmgr`/`grub-reboot`/`mokutil`/`nmcli` binaries and real
     firmware (UEFI Secure Boot + TPM), and a live `lindos-game cloud install geforce-now`.
   - A GTK visual pass of the new OOBE "transfer" page and the Settings "Windows apps"/"Gaming"
     additions (folds into item 3 above).
7. `packages/lindos-transfer/root/usr/share/lindos/transfer/app-map.json`'s Flatpak/winget ids were
   written offline (no network in this sandbox) — worth a live cross-check against Flathub/
   winget-pkgs before relying on it for a real migration.
8. **MSI exit codes above 255 are truncated on Linux** (POSIX wait status keeps 8 bits: msiexec's
   3010 "restart required" arrives as 194). `lindos-run`/`winget install` therefore fall back to the
   honest generic "ended with exit code N" message, and the MSI code-to-message tables never fire.
   A mod-256 guess was rejected because it could report a real failure as "restart needed". The
   likely proper fix: run msiexec with `/l*v <log>` and read the full return code from the log, but
   first verify what Wine's msi logging actually writes.
9. **Run the installer flow for real (highest priority).** Everything in SPEC §17 is untested on Linux:
   - Build the ISO (`gh workflow run CI … -f build_iso=true`), then follow the QEMU procedure at the end of
     `docs/BUILDING.md` "Installer flow": boot both menus (BIOS and UEFI), install online to a blank disk,
     inspect `/target` before rebooting (mounts, `dpkg --audit`, holds, `install-state.json`, default target,
     lightdm autologin, locked `oem`), boot the disk and check the account wizard → LightDM → Lindos Setup
     sequence; then the failure drills (offline, black-holed network, `kill -TERM` the hook, `lindos.install=off`).
   - Get the unattended QEMU install test green on CI: `build/qa/install_test.py` (blank disk,
     `automatic-ubiquity` + a CI-only seed), `build/qa/install_checks.py` (the assertions above on the mounted
     disk) and `build/qa/ci-observer.sh` (serial-console log of the live and first-boot guests) were being added
     by the CI work when this was written and have not run on Actions; check `build/qa/` and `ci.yml`.
     `boot_test.py` still boots the kernel directly and never sees the installer, the hook or oem-config.
     Expect several rounds, like the boot-test one.
   - Product decisions still open (from the installer/OOBE streams): the **temporary-account page** Ubiquity
     always shows in OEM mode (options: keep it, patch `ubi-usersetup.py`, or a hidden-page plugin + a
     hook-created `oem` user); **consent wording** (the "Install multimedia codecs" checkbox doubles as the
     proprietary-GPU-driver consent; relabelling needs a patch of `ubi-prepare.py`); **theming** the oem-config
     first-boot wizard (`GTK_THEME=Lindos-Setup` via systemd drop-ins is an untested idea) or a Lindos-native
     account page (phase 2); copying the live **Wi-Fi/Bluetooth profiles** to the new system (a privacy choice);
     the **one pkexec prompt** at Lindos Setup's Apply (a polkit rule or skipping it for Everyday would remove it);
     a **minimal-install switch** (today it is everything-for-every-Mode or `lindos.install=off`); an OEM entry
     for real manufacturers.
   - Watch for: Ubiquity's own later steps now going online (the hook refreshes the target's apt lists, which
     the deleted lists had silently disabled) — a dropped connection there can abort an install; `oem-config-gtk`'s
     Recommends pulling extras (`base-installer/install-recommends=false` if seen); `linux-firmware`/`systemd`
     upgrades rebuilding the initramfs in the hook (slow; shorten `lindos.install_budget` or exclude them).
   - The GTK visual pass for the new OOBE/Settings pieces (pending banners, the done-page recap, Apps › Left to
     finish from setup) folds into item 3.

## 4a. Addendum W (`SPEC-WINDOWS.md`, every Windows format / Transfer / Play-anywhere) — status

Ten engineers implemented Addendum W in parallel (formats/dos/diskimage/binfmt, MSIX, winget,
lindos-transfer + its Windows kit/GUI, play-anywhere routes, core dualboot + helper, kernel config,
and the Settings/OOBE UI); this integration pass (W-I) read every report, applied the cross-owner
requests that were still outstanding, added `packages/lindos-transfer` to the dependency graph
(`lindos-meta`, `build/chroot/30-lindos-debs.sh`, `build/config.env`), added Addendum-W
cross-component checks to `tests/test_integration.py`, and re-ran the full gate.

- **12th package**: `lindos-transfer` (Depends `lindos-core`; `lindos-meta` now Depends
  `lindos-transfer (= 1.0.0)`; installs before `lindos-setup` in `LINDOS_DEB_ORDER` so the OOBE's
  transfer page can call it).
- `lindos-compat` gained `formats.py`/`dos.py`/`diskimage.py`/`binfmt.py`/`msix.py`/`winget.py`/
  `wingetyaml.py` (the format engine, MSIX, winget) wired into the existing `cli_run.py`/
  `cli_compat.py` exactly per SPEC-WINDOWS §28's binding module map — verified by hand (every
  lazy `_mod("formats"|"msix"|"dos"|"diskimage")` import site and every `winget`/`binfmt` call in
  `cli_compat.py` cross-checked against the real modules' signatures) and by the full test suite.
- `lindos-core` gained `lindos.dualboot` + `/usr/bin/lindos-dualboot` + four helper actions
  (`reboot-to-windows`, `firmware-setup`, `import-wifi`, `set-binfmt`) with `stdin_payload` support;
  `lindos-gaming`'s `lindos-game` gained `route`/`play`/`shortcut`/`cloud install`; both integration
  points the other engineers' reports flagged as pending (transfer's Wi-Fi import via helper
  stdin, and lindos-game calling lindos-dualboot) were already correctly wired by the time this
  pass started — verified by direct code inspection, not just trusting the reports.
- Every id in SPEC-WINDOWS §28.3's format table exists in `formats.FORMATS` (29/29, exact match,
  no more/no fewer); every §30.4 helper action exists in `lindos.helper.ACTIONS` with a real
  `lindos-helper` handler; every CLI in §33.1's cross-component call map is shipped with a valid
  shebang; every MIME type `lindos-run.desktop` claims is defined either by
  `lindos-windows.xml` or documented there as an existing shared-mime-info 2.4 type. These are now
  permanent regression tests in `tests/test_integration.py` (`test_addendum_w_*`).
- Cross-owner requests from the ten reports: all were either already satisfied by the time this
  pass started (wifi.py's `stdin_payload=True`, lindos-game's `lindos-dualboot` calls, the
  LoL/GeForce-NOW errata in `docs/COMPATIBILITY.md`/`docs/WINDOWS-APPS.md`), or were mine to apply
  directly (`lindos-meta` Depends, `30-lindos-debs.sh`/`config.env` order, the README docs table).
  None required editing a file outside this engineer's ownership without one already covering it.
- Full gate result (this pass, Windows dev host): `bash tests/run.sh` → **ALL CHECKS PASSED**
  (bash -n 41/41, sh -n 27/27, py_compile 242/242, shellcheck clean 68 files, JSON 36/36,
  XML 34/34, .desktop 18/18, 0 CRLF across 524 files, `docs/COMPATIBILITY.md` up to date) and
  **2847 passed, 3 skipped, 0 failed** in `tests packages/*/tests build/tests` (897 s; the 3 skips
  are the same pre-existing platform-only ones from before Addendum W — a POSIX-FIFO test, a
  Windows-drive-letter test, and an lspci-covered autodetect test).
- Real-Linux verification is still outstanding for the Addendum-W-specific pieces — see item 6
  under §4 above; nothing in Addendum W has been exercised against real udisks2/binfmt_misc/
  DOSBox/Wine/efibootmgr/grub/NetworkManager/hivex or a real winget CDN response yet.

## 5. How to work on it (environment gotchas)

- **Host is Windows 11**, no WSL/Docker/shellcheck by default. Python 3.14 + Git Bash + Node present.
  Real kernel/ISO/boot work happens on **GitHub Actions (Ubuntu)**, not locally.
- **Run the gate locally:** `pip install pytest shellcheck-py` once, then `bash tests/run.sh`
  (Git Bash). It must stay green before every push.
- **LF line endings only** (shell runs on Linux). Do **not** write repo text files with PowerShell
  (it writes CRLF/BOM). Use the editor tools / Git Bash.
- **PowerShell 5.1 has no `&&`** — chain with `;` or use Git Bash. `gh`/`git` are available.
- **Python rule:** stdlib only (gi only in UI, lazily+guarded), every module importable on any OS.
  System paths honour `LINDOS_ROOT`; the home dir honours `LINDOS_HOME` — use these for testable,
  sandboxed file access (all tests rely on them).
- **Shell rule:** `#!/bin/bash` + `set -Eeuo pipefail`, ShellCheck-clean, no `sudo` inside scripts
  (callers use pkexec), `log()`/`die()`.
- **`docs/COMPATIBILITY.md` is generated** from `packages/lindos-gaming/root/usr/share/lindos/
  compat-matrix.json` by `tests/gen-compat-doc.py`. Edit the JSON, then run the generator; CI checks
  they match (`python tests/gen-compat-doc.py --check`).
- **Tests must be hermetic** — the 6 CI failures were all tests accidentally depending on the
  runner's host state (preinstalled Chrome, real `gi` without GTK typelibs, the runner kernel having
  `sched_ext`). Sandbox via `LINDOS_ROOT`/monkeypatch; never assume host state.

### Working on the installer flow (gotchas)

- **The hook must never break an install.** `target-config.sh` always exits 0, has no `set -e`, never writes
  to stdout (Ubiquity's debconf pipe), time-boxes every child, keeps dpkg clean and releases its holds on every
  exit path. Ubiquity has no timeout for hooks and ignores their exit status: a hung hook hangs every install.
  Keep ShellCheck clean (`tests/run.sh --quick`); `set -Eeuo pipefail` is the rule everywhere **except** the hook
  and `lib.sh`.
- **The deployed hook name has no `.` and the file needs the exec bit**, or Ubiquity skips it silently. Git on
  Windows loses exec bits, so `79-installer-flow.sh` does `install -m 0755` (and strips CRs); `mkdeb.sh` only marks
  `*.sh`/`libexec` files executable and strips CRs from `*.templates` and `*.seed`.
- Do not print, `echo` or let a child inherit stdout/stdin in the hook; do not call `dist-upgrade`; do not touch the
  kernel, boot loader or the ubiquity/oem-config/casper families (they are held for a reason).
- Test seams (unset on a real system): `LINDOS_TARGET`, `LINDOS_TARGET_RUNNER`, `LINDOS_DRY_RUN`,
  `LINDOS_INSTALL_BUDGET`, `LINDOS_TIMEOUT_PCT`, `LINDOS_INSTALLER_STEPS`, `LINDOS_FREE_KB`, `LINDOS_TEST_CMDLINE`
  (also for `is-live-session`), `LINDOS_INSTALLER_ROOT` (the 79 hook). `extras.json` is generated: after changing a
  `mode.json` or the OOBE `apps.json`, run `python3 build/lib/installer_extras.py --write` (a test fails when stale).
- Anything that can only be settled by a real install is written down in `docs/INSTALLER.md` ("Known limitations and
  what is unverified"): keep that list honest when you verify or change something.

## 6. Command cheat-sheet

```bash
# verify locally (Git Bash), must be green before pushing
bash tests/run.sh                      # add --verbose for per-file output
python -m pytest -q -c tests/pytest.ini --rootdir . -p lindos_testsupport tests packages/*/tests build/tests

# git / github (repo already exists & is pushed)
git add -A && git commit -m "..."      # end commits with the Co-Authored-By line if you use one
git push
gh run list --limit 5
gh run watch <run-id> --exit-status    # do NOT pipe through tail — it hides the real exit code
gh run view <run-id> --log-failed      # only the failing steps

# trigger the heavy Linux builds (kernel + ISO) as artifacts
gh workflow run CI -f build_kernel=true -f build_iso=true

# build locally on a Linux host (not this Windows box)
make debs        # build/mkdeb.sh all  → out/debs/*.deb
make kernel      # build/kernel/build-kernel.sh (needs bc bison flex libssl-dev libelf-dev dpkg-dev)
make iso         # build/build-iso.sh (root, ~15 GB, downloads Mint base) → out/*.iso
make qemu        # boot-test the built ISO
```

## 7. Pointers

- The user's role/prefs and project state also live in Claude Code memory at
  `C:\Users\Hp\.claude\projects\C--Users-Hp-Desktop-Nitish-Code-MiniLLM\memory\` (`lindos-project.md`).
  A non-Claude agent won't have that — this file is the self-contained handoff.
- When in doubt about a name/path/API, the SPEC files are authoritative. Keep changes additive and
  the build green; keep the honesty rules intact.
