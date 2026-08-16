# Lindos — Continuation Guide (agent handoff)

_Last updated: 2026-08-16. Written for the next agent/developer taking over this project._

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
  - `SPEC.md` — base distro (§0–§13)
  - `SPEC-KERNEL.md` — Addendum K: kernel + compat-performance (§14–§19)
  - `SPEC-VM.md` — Addendum V: VM/WinApps/Proton-system/drivers/CI (§20–§26)
  - Rule: if code and SPEC disagree, one of them is wrong — never silently diverge. Update the SPEC
    when you intentionally change a contract.
- **CI log/history:** `CI-LOGS.md` (this repo).

## 2. Repo map

```
SPEC.md, SPEC-KERNEL.md, SPEC-VM.md   the binding contracts
README.md, CONTRIBUTING.md, LICENSE (GPL-3.0-or-later), THIRD_PARTY.md
Makefile                              make debs | assets | iso | iso-docker | qemu | kernel | test | lint
packages/                             11 Debian packages (DEBIAN/ + root/ trees):
  lindos-core      python lib `lindos.*` (paths/config/modes/browsers/hardware/helper/theme/compat/ram)
                   + privileged helper /usr/libexec/lindos/lindos-helper + polkit + CLIs
  lindos-desktop   XFCE Win11 look: panel, themes, shortcuts, branding, autostart
  lindos-setup     first-boot OOBE (GTK) — mode, browser, personalise, apps, privacy, apply
  lindos-settings  Win11-style settings centre (12 pages, GTK)
  lindos-compat    Windows apps: lindos-run (.exe→umu/wine/bottles), recipes, profiles loader, perf env
  lindos-gaming    lindos-proton / lindos-drivers / lindos-game / lindos-gamescope + compat-matrix + profiles
  lindos-tune      RAM/perf: zram, earlyoom, sysctl, governor, sched_ext (scx), THP, fan/power, per-mode
  lindos-kernel    tuned kernel: kconfig fragment (ntsync/sched_ext/1000Hz/MGLRU) + build recipe + CLI + grub
  lindos-vm        honest KVM/VFIO Windows VM (q35+OVMF+virtio+swtpm), single-GPU passthrough
  lindos-winapps   seamless Adobe/Office over FreeRDP RemoteApp against a Windows backend
  lindos-meta      metapackage (Recommends the others)
build/             ISO remaster + .deb pipeline: build-iso.sh, mkdeb.sh, fetch-assets.sh,
                   chroot/00..80 hooks (+35-kernel.sh, 75-vm.sh), kernel/build-kernel.sh, Docker, config.env
docs/              README-level docs incl. COMPATIBILITY.md (GENERATED — see below), ANTI-CHEAT.md,
                   KERNEL.md, VM.md, WINAPPS.md, DRIVERS.md, MODES.md, SETTINGS.md, etc.
tests/             run.sh (the gate), conftest.py, lindos_testsupport.py (gi stub + sys.path),
                   gen-compat-doc.py (regenerates docs/COMPATIBILITY.md)
.github/workflows/ci.yml   lint+pytest (Ubuntu+Windows), debs+lintian, kernel (manual), iso (manual)
out/               git-ignored build products (ISO, debs, kernel, acceptance logs)
```

## 3. Current state (all green as of 2026-08-16)

- **11 packages built**, `bash tests/run.sh` exits 0 on Windows: bash -n 39, sh -n 25, py_compile
  167, ShellCheck clean, JSON 35, XML 33, .desktop 16, **0 CRLF**, **970 pytest passed / 2
  platform-skips**, compat-doc in sync.
- **CI is GREEN on real Linux** (GitHub Actions, run 31926992548): Ubuntu lint+pytest ✓, Windows
  pytest ✓, `.deb` build + lintian ✓ (artifact `lindos-debs`). See `CI-LOGS.md`.
- The first CI run failed with **6 Linux-only issues** (tests leaking host state + one `.desktop`
  typo); all fixed in commit `8c07619`. Details in `CI-LOGS.md`.

## 4. What is NOT done yet (the actual TODO / next steps)

1. **Dispatch the full Linux build on CI** (nothing needs the local machine):
   ```bash
   gh workflow run CI -f build_kernel=true -f build_iso=true
   ```
   This compiles the tuned kernel (`build/kernel/build-kernel.sh`, ~30–60 min) and builds a
   bootable ISO (downloads the ~2.9 GB Mint base). Artifacts: `lindos-debs`, `lindos-kernel-debs`,
   `lindos-iso` (+ `.sha256`, `build.log`). Watch: `gh run watch <id> --exit-status`.
   Expect first-time-on-real-Linux shake-out (root/loop-device/lintian issues) — read the failing
   log and fix, same as the 6 already fixed.
2. **Boot-test the ISO** in QEMU (`make qemu` / `build/test-qemu.sh`) or on hardware. Nothing here
   has ever been booted yet.
3. **Real-Linux GTK visual pass** for `lindos-setup` (OOBE) and `lindos-settings` — they are only
   import-smoke-tested against the gi stub; construct/paint them under a real GTK + display.
4. **`lindos-vm` / `lindos-winapps` live test** on a machine with KVM + a licensed Windows.
5. Cosmetic: CI warns `actions/checkout@v4` runs on Node20-deprecated shim — bump action versions
   when convenient (not blocking).

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
