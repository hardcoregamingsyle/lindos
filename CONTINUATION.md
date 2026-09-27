# Lindos — Continuation Guide (agent handoff)

_Last updated: 2026-09-26 (Addendum W integration pass). Written for the next agent/developer
taking over this project._

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
packages/                             12 Debian packages (DEBIAN/ + root/ trees):
  lindos-core      python lib `lindos.*` (paths/config/modes/browsers/hardware/helper/theme/compat/
                   ram/dualboot) + privileged helper /usr/libexec/lindos/lindos-helper + polkit +
                   CLIs incl. lindos-dualboot (one-shot restart into an existing Windows install)
  lindos-desktop   XFCE Win11 look: panel, themes, shortcuts, branding, autostart
  lindos-setup     first-boot OOBE (GTK) — mode, browser, personalise, apps, privacy, transfer, apply
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
build/             ISO remaster + .deb pipeline: build-iso.sh, mkdeb.sh, fetch-assets.sh,
                   chroot/00..80 hooks (+35-kernel.sh, 75-vm.sh), kernel/build-kernel.sh, Docker, config.env
docs/              README-level docs incl. COMPATIBILITY.md (GENERATED — see below), ANTI-CHEAT.md,
                   KERNEL.md, VM.md, WINAPPS.md, DRIVERS.md, MODES.md, SETTINGS.md, WINDOWS-FORMATS.md,
                   WINGET.md, TRANSFER.md, DUALBOOT.md, etc.
tests/             run.sh (the gate), conftest.py, lindos_testsupport.py (gi stub + sys.path),
                   gen-compat-doc.py (regenerates docs/COMPATIBILITY.md)
.github/workflows/ci.yml   lint+pytest (Ubuntu+Windows), debs+lintian, kernel (manual), iso (manual)
out/               git-ignored build products (ISO, debs, kernel, acceptance logs)
```

## 3. Current state

- **2026-09-27 (this session, continued — self-diagnosis, real-desktop wait, merged
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
   Linux). The **no-kernel loop is fully green** (run `36288980753`): all 10 smoke checks pass for
   real. **`build_kernel=true` has run twice and both times failed in the kernel job itself**
   (never-before-exercised build-dep gaps): first missing `debhelper` (fixed), then missing
   `libdw-dev` for `gendwarfksyms`/`dwarf.h` — **found but not yet fixed** (2-round fix cap for
   that task reached first; see `CI-LOGS.md`'s "Round 3" for the exact error and the almost-
   certainly-right next step: add `libdw-dev` to ci.yml's "Install kernel build dependencies"
   step, next to the already-present `dwarves`, then re-verify — kernel 6.14's full build-dep
   surface still hasn't been exercised to completion on this runner image). Once the kernel job
   succeeds, boot-test should pass end-to-end (the smoke-test layer has been fully green for two
   consecutive runs already; the only open question is whether `uname -r` then correctly ends in
   `-lindos`). Re-dispatch after that fix:
   ```bash
   gh workflow run CI --ref feature/windows-transfer-updates-boottest -f build_kernel=true -f build_iso=true -f boot_test=true
   ```
   Artifacts: `lindos-debs`, `lindos-kernel-debs`, `lindos-iso` (+ `.sha256`, `build.log`),
   `lindos-boot-test` (serial log + a screenshot of the live desktop — see item 2). Watch:
   `gh run watch <id> --exit-status` (never through `tail`, it hides the real exit code).
2. **Boot-test the ISO** — automated in CI (`boot-test` job, `build/qa/boot_test.py`): it extracts
   `casper/vmlinuz`+`initrd` from the built ISO, boots them directly under QEMU (KVM-accelerated —
   `ci.yml`'s "Check for KVM" step now `chmod 0666`s the device so this ephemeral runner can
   actually use it) with the ISO attached as a CD-ROM, injects
   `packages/lindos-core/.../usr/libexec/lindos/qa/ci-boot-smoke-test.sh` via
   `systemd.run=` (runs alongside the normal boot, never delays/replaces the real desktop),
   and asserts every shipped CLI (`lindos-mode`, `lindos-tune`, `lindos-compat doctor`,
   `lindos-game`, `lindos-transfer`, `lindos-dualboot`, `lindos-update`, a plain `python3 -c
   "import lindos"`) actually runs, self-diagnoses any failure (per-check log tails, and
   per-required-check detail for `lindos-compat doctor`), waits for a detached watcher's
   `LINDOS_DESKTOP_READY` signal, then captures a screenshot via the QEMU monitor's `screendump`.
   **All 10 checks now genuinely pass** on every run since the `$?`-clobbering bug was fixed. The
   desktop-ready watcher reliably *starts* (`LINDOS_DESKTOP_WATCH_STARTED` confirms it) but has
   never once reached its own final `LINDOS_DESKTOP_READY`/`_TIMEOUT` line — **still unresolved**;
   candidates worth checking next: whether this runner's systemd still ties a `systemd-run
   --no-block` transient unit to the calling unit's cgroup lifecycle, or whether
   `systemctl is-system-running`/`pgrep` themselves hang partway through the watcher's loop. The
   screenshot has been black (`is_system_running=initializing`, cursor blink only) on every run so
   far regardless — informational only (never gates the job — see `boot_test.py`'s
   `take_screenshot()`). Manual interactive boot-testing (`make qemu` / `build/test-qemu.sh`) or
   real hardware is
   still worth doing separately for a visual/hands-on check.
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
