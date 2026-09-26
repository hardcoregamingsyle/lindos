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

- **2026-09-26 (Addendum U, uncommitted working tree):** `SPEC-UPDATE.md` adds a 13th
  component, `lindos-update` (in `lindos-core`): checks/applies updates to Lindos's own 12
  packages (kernel excluded unless explicitly opted in), an apt-repo publishing pipeline
  (`build/publish-apt-repo.sh`, signed, flat-format), a sideload fallback (install `.deb`s from a
  local folder — usable today, no hosting needed), a `lindos-settings` "Updates" page, and a
  read-only login/6-hourly notifier (`lindos-update-notify`). `LINDOS_APT_REPO_URL` is a
  **placeholder** — nothing hosts the repo yet, so live apt updates don't actually flow until
  someone points it at real hosting (see `docs/UPDATES.md`). Also new: a CI `boot-test` job that
  actually boots the built ISO (see item 2 below) — the first time anything here has been booted
  at all. All of this is built and hermetically tested on Windows only; **nothing has run on CI or
  real Linux yet.**
- **2026-09-26 (Addendum W, uncommitted working tree):** `bash tests/run.sh` exits 0 on Windows:
  bash -n 41, sh -n 27, py_compile 244, ShellCheck clean, JSON 36, XML 34, .desktop 18, **0 CRLF**,
  **2929 pytest passed / 4 Windows-only skips**, compat-doc in sync. 12 packages (new:
  `lindos-transfer`). Addendum W went through research (verified against primary sources),
  implementation, and an adversarial review. The review confirmed 22 findings; 21 are fixed with
  regression tests. That includes a **critical, pre-existing root-helper path traversal**
  (`apply-mode`'s `apply_system` could be walked out of the modes dir with `..`), plus
  `PACKAGE_RE`/`FLATPAK_RE` accepting `-`-prefixed "package names", a non-executable kernel
  sbsign hook, and a dpkg conffile conflict in the kernel grub drop-in. **Not yet run on CI**:
  push and let GitHub Actions confirm on real Linux.
- 2026-08-16: CI GREEN on real Linux (run 31926992548): Ubuntu lint+pytest, Windows pytest, `.deb`
  build + lintian (artifact `lindos-debs`). The first CI run failed with 6 Linux-only issues
  (tests leaking host state + one `.desktop` typo), fixed in `8c07619`. See `CI-LOGS.md`.

## 4. What is NOT done yet (the actual TODO / next steps)

1. **Dispatch the full Linux build on CI** (nothing needs the local machine):
   ```bash
   gh workflow run CI -f build_kernel=true -f build_iso=true -f boot_test=true
   ```
   This compiles the tuned kernel (`build/kernel/build-kernel.sh`, ~30–60 min), builds a bootable
   ISO (downloads the ~2.9 GB Mint base), and (new) actually **boots it** under QEMU/KVM and runs
   a smoke test inside it — see item 2. Artifacts: `lindos-debs`, `lindos-kernel-debs`,
   `lindos-iso` (+ `.sha256`, `build.log`), `lindos-boot-test` (serial log + a screenshot of the
   live desktop). Watch: `gh run watch <id> --exit-status`. Expect first-time-on-real-Linux
   shake-out (root/loop-device/lintian issues) — read the failing log and fix, same as the 6
   already fixed.
2. **Boot-test the ISO** — now automated in CI (`boot-test` job, `build/qa/boot_test.py`): it
   extracts `casper/vmlinuz`+`initrd` from the built ISO, boots them directly under QEMU with the
   ISO attached as a CD-ROM, injects
   `packages/lindos-core/.../usr/libexec/lindos/qa/ci-boot-smoke-test.sh` via
   `systemd.run=` (runs alongside the normal boot, never delays/replaces the real desktop),
   and asserts every shipped CLI (`lindos-mode`, `lindos-tune`, `lindos-compat doctor`,
   `lindos-game`, `lindos-transfer`, `lindos-dualboot`, `lindos-update`, a plain `python3 -c
   "import lindos"`) actually runs, plus captures a screenshot of the live desktop via the QEMU
   monitor's `screendump`. Hermetically unit-tested (`build/tests/test_boot_test_qa.py`, 14
   tests) but **never run for real yet** — that only happens once item 1 is dispatched. Manual
   interactive boot-testing (`make qemu` / `build/test-qemu.sh`) or real hardware is still worth
   doing separately for a visual/hands-on check.
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
