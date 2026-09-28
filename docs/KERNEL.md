# The Lindos kernel

> **What it is.** `lindos-kernel` is a Linux kernel tuned for desktop latency and Wine/Proton
> gaming — ntsync, sched_ext, 1000 Hz + full preemption, MGLRU and BBR — **and** for the
> Windows-format/transfer/play-anywhere work in Addendum W: NTFS3 (incl. CompactOS-compressed
> files), exFAT, ext4 casefold, binfmt_misc, BitLocker plumbing and Windows dynamic disks.
> Shipped as a **build recipe, config fragment, boot integration and a runtime CLI**. The
> compiled kernel is a build artifact you (or CI) build on a Linux host; it is never committed
> and never built on Windows. The stock Mint/Ubuntu kernel stays installed as a fallback, so an
> install that has no Lindos kernel simply keeps booting the stock one.
>
> **What it is not.** A faster kernel is still a **Linux** kernel. It does not run Windows
> kernel-mode anti-cheat and changes nothing about the anti-cheat reality in
> [ANTI-CHEAT.md](ANTI-CHEAT.md). Do not expect Valorant, Fortnite or Call of Duty to work because
> the kernel is tuned — they cannot, for the reasons in that document. It also does not forge,
> spoof or bypass Secure Boot: §6 below signs the kernel with a key **you** create and enrol, the
> same way any self-compiled kernel has to.

The kernel layer is specified in [`SPEC-KERNEL.md`](../SPEC-KERNEL.md) §15 and extended by
[`SPEC-WINDOWS.md`](../SPEC-WINDOWS.md) §31 (Windows-format kernel support and Secure Boot).
Package tree: `packages/lindos-kernel/`; build recipe: `build/kernel/`.

## 1. What the Lindos kernel enables

Every feature below is declared in the manifest
(`/usr/share/lindos/kernel/manifest.json`) and enabled by the kconfig fragment
(`/usr/share/lindos/kernel/lindos.config`), which is the single source of truth the build script
merges onto a base defconfig.

| Feature | kconfig | Since | Why it matters |
|---|---|---|---|
| **ntsync** | `CONFIG_NTSYNC` (`/dev/ntsync`) | 6.14 | In-kernel NT synchronisation primitives — replaces esync/fsync for Wine/Proton, a large frametime and CPU-overhead win. Proton uses it automatically when `/dev/ntsync` is present. |
| **sched_ext / scx** | `CONFIG_SCHED_CLASS_EXT` | 6.12 | Pluggable BPF schedulers (`scx_lavd`, `scx_bpfland`, …) loaded at runtime for low-latency gaming without recompiling. Driven by `lindos-tune sched` (see §6). |
| **1000 Hz + full preempt** | `CONFIG_HZ_1000` (+`CONFIG_HZ=1000`), `CONFIG_PREEMPT` | — | Finer timer granularity and lower scheduling latency for the desktop and games. |
| **MGLRU** | `CONFIG_LRU_GEN`, `CONFIG_LRU_GEN_ENABLED` | — | Multi-generational LRU — better page reclaim under RAM pressure, which fits the Lindos 350–500 MB idle target and the Lite mode. |
| **BBR + fq** | `CONFIG_TCP_CONG_BBR`, `CONFIG_NET_SCH_FQ` | — | Lower-latency TCP congestion control and fair queueing (matches the base sysctl tune). |
| **zram / zswap / THP** | `CONFIG_ZRAM`, `CONFIG_ZSWAP`, `CONFIG_TRANSPARENT_HUGEPAGE(_MADVISE)` | — | Compressed swap and transparent huge pages that `lindos-tune` configures per mode. |
| **AMD P-State, BFQ/Kyber, user namespaces, CRIU** | `CONFIG_X86_AMD_PSTATE`, `CONFIG_IOSCHED_BFQ`/`CONFIG_MQ_IOSCHED_KYBER`, `CONFIG_USER_NS`, `CONFIG_CHECKPOINT_RESTORE`, `CONFIG_FUTEX` | — | Modern CPU frequency scaling, desktop-tuned I/O schedulers, sandboxing and umu/Proton requirements. |

The manifest's `recommended` series is **6.14** (the first release with in-tree ntsync), with
`localversion = -lindos` so a Lindos build is recognisable. An optional out-of-tree **BORE**
scheduler patch is listed under `patches` and is off by default.

### 1.1 Windows-format support (SPEC-WINDOWS §31.1)

Added to the same `lindos.config` fragment, for `lindos-compat`/`lindos-transfer`/`lindos-gaming`
(Addendum W) rather than for raw performance:

| Feature | kconfig | Why |
|---|---|---|
| **NTFS3** (read/write) | `CONFIG_NTFS3_FS=m` | The in-kernel driver Easy Transfer and `lindos-compat` mount Windows volumes with (always `-o ro` for Transfer — see [TRANSFER.md](TRANSFER.md)). |
| **CompactOS/WOF-compressed files** | `CONFIG_NTFS3_LZX_XPRESS=y` | Reads files Windows compressed with `compact /exe` or CompactOS (XPRESS4K/8K/16K, LZX32K) instead of silently truncating or erroring on them. |
| **NTFS POSIX ACLs** | `CONFIG_NTFS3_FS_POSIX_ACL=y` | Preserves Windows ACL metadata as POSIX ACLs where NTFS3 supports it. |
| **exFAT** | `CONFIG_EXFAT_FS=m` | USB drives/SD cards formatted by Windows (exFAT is the default for large removable media). |
| **ext4 casefold** | `CONFIG_UNICODE=y` | Kernel-side Unicode support so *the filesystem* can be created with the casefold feature — this only enables the **kernel's** support; a specific ext4 filesystem still needs `tune2fs -O casefold` (or `mkfs.ext4 -O casefold`) run **offline, from live media** (see §28.8 case-insensitive `C:\` drives in `SPEC-WINDOWS.md`; e2fsprogs refuses to (de)activate it on a mounted filesystem). |
| **binfmt_misc** | `CONFIG_BINFMT_MISC=y` | Lets `./setup.exe` run directly from a terminal path, routed through `lindos-binfmt` to `lindos-run` (umu/Wine/Bottles). |
| **efivarfs** | `CONFIG_EFIVAR_FS=y` | Reads the `SecureBoot` UEFI variable (§6 below) and lets `efibootmgr` set the one-shot `BootNext` variable for "restart into Windows". |
| **dm-crypt + userspace skcipher** | `CONFIG_DM_CRYPT=m`, `CONFIG_CRYPTO_USER_API_SKCIPHER=m` | What `cryptsetup --type bitlk` needs to unlock BitLocker/BitLocker-To-Go volumes (password or 48-digit recovery key only — Lindos never sees a TPM-sealed key; see `SPEC-WINDOWS.md` §27.3/§29). |
| **Loop devices + ISO9660/Joliet/UDF** | `CONFIG_BLK_DEV_LOOP=y`, `CONFIG_ISO9660_FS=m`, `CONFIG_JOLIET=y`, `CONFIG_UDF_FS=m` | Mounting a Windows setup/recovery ISO read-only (Windows install media is usually UDF-bridged, so it normally mounts as `udf`, not `iso9660`). |
| **FUSE** | `CONFIG_FUSE_FS=y` | The ntfs-3g fallback driver and (if the user needs it) `dislocker`. |
| **Windows dynamic disks** | `CONFIG_LDM_PARTITION=y` | So Easy Transfer can read an MBR dynamic disk's partitions read-only. |

These are unconditionally **required** keys (`lindos_kernel.kconfig.REQUIRED_KEYS`, checked by
`tests/test_kconfig.py`), same enforcement as the performance keys above — a fragment missing any
of them fails validation.

### 1.2 Display/GPU support (CONTINUATION.md item 2 / boot-test run 36319809802)

A kernel with no working KMS/DRM driver never draws a frame — on real hardware, not just in a VM:
LightDM/logind waits on seat0's `CanGraphical`, which needs a GPU driver to have registered a DRM
device, so this would hang `graphical.target` forever on a real laptop too. `--base-config ubuntu`
(§2.1) already carries Ubuntu's own generic-flavour values for all of these — confirmed present
with these exact values in the actual built `6.14.0-lindos` config used by CI run `36319809802`
(extracted from the `linux-image` `.deb` and diffed against every key/value in this fragment) —
so these keys are a **regression guard**, not what caused that run's boot-test hang (a stuck
`lightdm.service`/`plymouth-quit-wait.service`; see `CI-LOGS.md`'s matching entry for the real
root cause and fix).

| Feature | kconfig | Why |
|---|---|---|
| **DRM core + KMS/fbdev helpers** | `CONFIG_DRM=y`, `CONFIG_DRM_KMS_HELPER=y`, `CONFIG_DRM_FBDEV_EMULATION=y`, `CONFIG_FRAMEBUFFER_CONSOLE=y` | The kernel modesetting stack every DRM driver below sits on top of, plus a text console over it. |
| **Firmware framebuffer → early DRM** | `CONFIG_SYSFB_SIMPLEFB=y`, `CONFIG_DRM_SIMPLEDRM=y` | Turns a firmware-provided framebuffer (UEFI GOP on a real laptop, or a VESA/Bochs-VBE mode set by SeaBIOS) into a real, driver-independent DRM/KMS device — a working display even before, or without, the real GPU driver. |
| **Real-laptop GPU drivers** | `CONFIG_DRM_I915=m` (Intel, pre-Meteor Lake), `CONFIG_DRM_XE=m` (Intel, Meteor Lake/Arc/Battlemage+), `CONFIG_DRM_AMDGPU=m` (AMD GCN+), `CONFIG_DRM_RADEON=m` (AMD, pre-GCN), `CONFIG_DRM_NOUVEAU=m` (Nvidia; the proprietary driver is not redistributable/buildable in-tree) | So the built kernel actually drives the GPU real Lindos hardware ships with, not just a generic framebuffer. |
| **QEMU/CI/virt GPUs** | `CONFIG_DRM_BOCHS=m`, `CONFIG_DRM_VIRTIO_GPU=m`, `CONFIG_DRM_QXL=m` | So `build/qa/boot_test.py`'s QEMU boot test (virtio-vga) — and anyone using virt-manager/VirtualBox-style tooling — also gets a real KMS device. |

Same enforcement as the tables above: unconditionally required
(`lindos_kernel.kconfig.REQUIRED_KEYS_DISPLAY`, a subset of `REQUIRED_KEYS`), checked by
`tests/test_kconfig.py`.

## 2. How to build it (Linux host only)

The kernel is compiled by `build/kernel/build-kernel.sh`. It **only runs on Linux** — on Windows
or any non-Linux host it refuses with a clear message and exits 2, and the ISO build stays green
whether or not `out/kernel/*.deb` exists. Building a kernel needs a real Linux toolchain that does
not exist on the Windows host this repository is often edited from; there is no honest way to fake
it, so the script is guarded rather than stubbed.

```sh
# On an Ubuntu/Debian Linux host, from the repo root:
build/kernel/build-kernel.sh --version 6.14.0 --jobs "$(nproc)"
# or drive it through the CLI (which execs the script when present):
lindos-kernel build --jobs 8 --out out/kernel
```

Build-time requirements (the script reports any that are missing and exits 2):
`bison flex libssl-dev libelf-dev bc dpkg-dev`.

Steps the script performs:

1. Fetch `linux-X.Y.Z.tar.xz` from kernel.org and verify it (or reuse an existing tree with
   `--skip-fetch --kdir DIR`).
2. Resolve the **base config** (see §2.1) → `./scripts/kconfig/merge_config.sh -m .config
   <lindos.config>` → `make olddefconfig`. The fragment is intentionally a **fragment** merged
   onto the base config, not a whole kernel config.
3. Apply `build/kernel/patches/series` if it is non-empty (quilt-style; this is where BORE / scx
   patches are dropped in — see `build/kernel/patches/README.md`; nothing is bundled that is not
   really there).
4. `make -j N bindeb-pkg LOCALVERSION=-lindos` and move the resulting
   `linux-image-*.deb` / `linux-headers-*.deb` into `--out` (default `out/kernel`).

Flags: `--version X.Y.Z`, `--config PATH` (default the package's `lindos.config`),
`--base-config ubuntu|defconfig|PATH` (default `ubuntu`, see §2.1), `--localversion -lindos`,
`--jobs N` (default `nproc`), `--out DIR`, `--skip-fetch`, `--menuconfig`.

### 2.1 `--base-config`: why the default changed to `ubuntu` (SPEC-WINDOWS §31.2)

Earlier builds started from `make defconfig`. **`defconfig` is a minimal sanity-check config, not
a hardware-support one** — it lacks almost every real Wi-Fi/GPU/audio driver a real PC needs, so a
kernel built from it alone boots but is barely usable on real hardware (no network, no display
driver beyond the generic framebuffer, etc.).

`--base-config ubuntu` (the new **default**) fixes this by starting from Ubuntu's own **generic**
flavour config instead — the same config `linux-image-*-generic` ships, with the real driver set
noble/Mint 22 users already rely on:

1. If this host already has one (a real Ubuntu/Mint machine, or the GitHub Actions `ubuntu-24.04`
   runner), it reuses the newest `/boot/config-*-generic` — no network needed.
2. Otherwise it resolves the latest `linux-modules-<ver>-generic` package via `apt-cache search`
   and fetches it with `apt-get download` (no root, no install — just downloads the `.deb` next to
   the kernel source tree), then extracts its `/boot/config-*` with
   `dpkg-deb --fsys-tarfile <deb> | tar -x ./boot/config-*` (this logic lives in
   `build/kernel/lib/base-config.sh`, kept as its own sourceable file so it can be unit-tested
   with a fake `/boot` and fake `apt-cache`/`apt-get`/`dpkg-deb` on `PATH` — see
   `build/tests/test_build_kernel.py`).
3. Either way, `scripts/config --set-str SYSTEM_TRUSTED_KEYS ""` and `--set-str
   SYSTEM_REVOCATION_KEYS ""` blank Ubuntu's own module-signing certificate paths (Ubuntu's
   config points these at `debian/canonical-certs.pem`-style paths inside *Canonical's* build
   tree, which do not exist here and would otherwise fail the build with a missing-file error —
   blanking them does not weaken anything Lindos itself signs; see §6, which is a completely
   separate, later signing step over the finished kernel *image*, not the in-tree module-signing
   keys).
4. The Lindos fragment is merged and `make olddefconfig` fills in every remaining/new symbol with
   its default.

`--base-config defconfig` keeps the old upstream-only behaviour, still useful for a fast CI smoke
build that does not need real hardware to boot. `--base-config PATH` copies in any other `.config`
(treated like `ubuntu` for the trusted-keys blanking, since it may also be Ubuntu-derived). CI's
`kernel` job (`.github/workflows/ci.yml`) passes `--base-config ubuntu`.

## 3. How the kernel reaches the ISO

The build hook `build/chroot/35-kernel.sh` (hook order `00,10,20,30,35-kernel,40,50,60,70,80`) is
idempotent and tolerant: if `out/kernel/*.deb` exists it `dpkg -i`'s the images into the pool and
runs `update-grub`; if it does not, it logs *"no Lindos kernel built — using stock"* and continues.
It never fails the ISO build. `lindos-meta` **Recommends** `lindos-kernel`, and the stock kernel
always stays installed as a fallback — the Lindos kernel is only selected by default in GRUB when
it is actually present.

## 4. Install and select it — `lindos-kernel`

The CLI reads without root; mutating actions print the exact `pkexec`/`update-grub` command to run
rather than ever calling `sudo` themselves.

```
lindos-kernel status [--json]        running kernel, whether it is a Lindos build, per-feature present/absent
lindos-kernel features [--json]      manifest features cross-checked against the running kernel
lindos-kernel list [--json]          installed linux-image-* packages + which is the GRUB default; marks Lindos builds
lindos-kernel cmdline show|set KEY=VAL…|reset|--preset {default,gaming}
lindos-kernel secureboot status [--json] | apply-selection [--json]   (see §6)
lindos-kernel build [--kdir DIR] [--jobs N] [--menuconfig] [--out DIR] [--base-config ubuntu|defconfig|PATH]
lindos-kernel --version
```

* **`status`** probes the live system: `/dev/ntsync`, `/sys/kernel/sched_ext/`, `CONFIG_HZ` and
  preemption from `/boot/config-$(uname -r)` or `/proc/config.gz`, the THP mode and the TCP
  congestion control. It always exits 0 — running a non-Lindos (stock) kernel is **not** an error,
  just reported as such.
* **`list`** parses `dpkg -l 'linux-image-*'` and the GRUB default (`grub-editenv list` /
  `/etc/default/grub`) so you can see every installed kernel and pick one in GRUB at boot
  (hold <kbd>Shift</kbd> / press <kbd>Esc</kbd> → *Advanced options*).
* Installing a Lindos-kernel `.deb` (from `out/kernel/`, or the pool on an ISO that shipped one)
  and running `update-grub` is all that is needed; the newest Lindos build becomes the default
  boot entry.

## 5. The cmdline drop-in and the `mitigations=off` note

`lindos-kernel cmdline` manages `/etc/default/grub.d/50-lindos.cfg`, which appends only **safe,
reversible** flags to `GRUB_CMDLINE_LINUX_DEFAULT`, inside marker fences
(`# >>> lindos >>>` … `# <<< lindos <<<`) so they can be added and removed idempotently. The
default flags are conservative:

```
transparent_hugepage=madvise nowatchdog nvme_core.default_ps_max_latency_us=0
```

**`mitigations=off` is deliberately NOT set by default.** Disabling CPU vulnerability mitigations
(Spectre/Meltdown/MDS/…) can raise gaming performance but is a real **security trade-off**: it
leaves the machine exposed to those side-channel attacks. It ships **commented out** with a note,
and is strictly **opt-in**:

```sh
# Turn it on for a gaming machine you accept the trade-off on:
lindos-kernel cmdline --preset gaming     # adds mitigations=off (opt-in, security trade-off)
# then run the privileged follow-up the command prints:
pkexec update-grub
# Undo it at any time:
lindos-kernel cmdline --preset default     # or: lindos-kernel cmdline reset
pkexec update-grub
```

`set`, `reset` and every `--preset` print the exact `update-grub` command to run as root; the CLI
never calls it for you. Reverting is always a one-liner because every Lindos edit is fenced.

## 6. Secure Boot and MOK signing (SPEC-WINDOWS §31.3)

A **self-compiled** kernel is unsigned by default. With Secure Boot **on**, firmware/shim will
refuse to boot an unsigned kernel — which is exactly the correct, honest behaviour of a Secure
Boot chain; Lindos does not (and cannot, and would never try to) fake, bypass or disable that
check. Instead it lets you sign your own kernel with your own key, the same mechanism Ubuntu
itself documents for out-of-tree/DKMS modules (`shim` → your enrolled Machine Owner Key → your
signed kernel).

### 6.1 Check the current state

```
lindos-kernel secureboot status [--json]
```

Reports: firmware type (UEFI/BIOS) and Secure Boot on/off (read from the `SecureBoot` UEFI
variable, falling back to `mokutil --sb-state`); whether a Lindos Machine Owner Key exists on disk
(`/var/lib/shim-signed/mok/MOK.priv` + `MOK.der`) and is enrolled (`mokutil --list-enrolled`);
and, for every installed `-lindos` kernel image, whether it is already signed
(`sbverify --list`). Reading never needs root and never changes anything.

### 6.2 Create and enrol a Machine Owner Key (one-time, per machine)

```sh
sudo update-secureboot-policy --new-key      # generates /var/lib/shim-signed/mok/MOK.{priv,der}
sudo update-secureboot-policy --enroll-key   # queues the enrolment
# reboot: MokManager (blue screen before GRUB) asks you to confirm it — type the
# password update-secureboot-policy printed, then continue booting.
```

This is standard `shim-signed` tooling already on the system (Recommended by `lindos-kernel`) —
Lindos does not write or manage the key itself, only *uses* it once it exists.

### 6.3 The postinst signing hook

`/etc/kernel/postinst.d/zz-lindos-sbsign` (installed by `lindos-kernel`, plain POSIX `sh`) runs
automatically every time **any** `linux-image-*` package is installed or upgraded (the standard
Debian/Ubuntu kernel-hook mechanism: every executable script under `/etc/kernel/postinst.d/` is
run as `<script> <version> <path-to-image>`). It only acts on `-lindos` kernels — every other
kernel is left untouched. For each one:

1. If `sbsign` is not installed, or no MOK exists yet, or `openssl`/PEM conversion fails, it prints
   plain-language guidance (the §6.2 commands) and **exits 0** — the kernel install itself never
   fails because of this.
2. Otherwise it converts `MOK.der` (DER) to PEM in a `mktemp` file and runs:
   `sbsign --key MOK.priv --cert <pem> --output <image>.tmp <image>`, then replaces the image with
   the signed one on success. A signing failure is reported (with `sbsign`'s own error text) and
   the **original, unsigned image is left in place** — nothing is ever left half-written.
3. On a successful sign, it also best-effort re-runs `lindos-kernel secureboot apply-selection`
   and `update-grub` (same "never fails" contract as everything else in this hook). This is what
   makes reinstalling the kernel package after enrolling a MOK self-heal `GRUB_DEFAULT` back to
   the Lindos kernel without you having to run anything by hand — the `lindos-kernel` *package's*
   own postinst (§6.4 below) only runs when that package itself is installed/reconfigured, not
   when a `linux-image-*-lindos` package is reinstalled, so this hook is what actually re-applies
   the selection in that case.

### 6.4 Which kernel GRUB boots by default

The binding rule (SPEC-WINDOWS §27.5 + §31.3): **the Lindos kernel is only allowed to be GRUB's
default boot entry when Secure Boot is off, or the installed Lindos image is itself signed.**
Otherwise GRUB is pointed at a kernel shim will actually boot, so turning Secure Boot on for a
Windows game never leaves you stuck at a Secure Boot violation screen with no obvious way back in
— the stock kernel is never removed and stays selectable regardless.

This is implemented as a **second**, independent marker-fenced drop-in,
`/etc/default/grub.d/51-lindos-kernel-select.cfg` (same idempotent-fence idiom as
`50-lindos.cfg` above, kept in its own file so cmdline flags and kernel selection stay separately
readable/reversible), managed by:

```
lindos-kernel secureboot apply-selection [--json]
```

which recomputes `GRUB_DEFAULT` from the current Secure Boot/signing state (`lindos_kernel.grub`:
`may_boot_lindos_by_default`/`apply_kernel_selection`) and prints the `pkexec update-grub`
follow-up, exactly like `cmdline set/reset` do. The postinst script also calls this, best-effort,
before its existing `update-grub` step, so the drop-in reflects reality right after a kernel
install without you having to run anything by hand.

> **Implementation note (read before relying on this in production):** when Lindos must steer GRUB
> *away* from the (unsigned) Lindos kernel, it targets the specific fallback kernel's "Advanced
> options" submenu entry using Debian's standard `10_linux` id scheme
> (`gnulinux-<version>-advanced-<boot-device-id>`, with `<boot-device-id>` resolved via
> `grub-probe --target=fs_uuid /boot`). That id format was not re-verified against a live
> `grub.cfg` in this session (the Addendum-W research pass covered `30_os-prober`, not `10_linux`);
> confirm it on a real Ubuntu/Mint boot before depending on it. If the exact entry cannot be named
> (e.g. `grub-probe` unavailable), it conservatively falls back to opening the "Advanced options"
> submenu (`GRUB_DEFAULT=1`) rather than guessing further. `lindos-kernel secureboot status` is
> the authoritative live signal either way.

### 6.5 The 2026 Microsoft CA expiry

Microsoft's original 2011 UEFI CA (which signs `shim`) expired **27 June 2026**; the 2011 KEK CA
expired 24 June 2026; the 2011 Windows Production PCA (which signs the Windows boot manager)
expires **19 October 2026**. None of this breaks an already-enrolled system: a shim signed with
the 2011 key keeps booting as long as that certificate stays in firmware's `db` and is not
revoked. Microsoft has been dual-signing shims with both the 2011 and the replacement
"Microsoft UEFI CA 2023" keys since October 2025. Practically:

* Keep `shim-signed` (and `grub-efi-amd64-signed` if installed) updated through normal system
  updates — that is how the 2023 keys reach your firmware's `db` over time.
* Do **not** remove the 2011 certificate from `db` yourself.
* If you ever see a Secure Boot/shim/SBAT verification error after a Windows or firmware update,
  it is almost always a certificate-rotation issue like this one, not a Lindos problem —
  `lindos-kernel secureboot status` still tells you plainly whether *Lindos's own* kernel is
  signed and whether Secure Boot is currently on.

## 7. How modes pick a scheduler

sched_ext lets a mode load a low-latency BPF scheduler at runtime (no reboot, no recompile). This
is wired through `lindos-tune sched` and the per-mode `tune.d/*.conf` `sched=` key
([HARDWARE-CONTROL.md](HARDWARE-CONTROL.md), [MODES.md](MODES.md)):

```
lindos-tune sched list      available scx schedulers (scx_loader / PATH / built-in known list)
lindos-tune sched status    the active scheduler (/sys/kernel/sched_ext/state)
lindos-tune sched set <scx_lavd|scx_bpfland|scx_flash|scx_rustland|none>
```

Defaults per mode: **Gaming → `scx_lavd`** (latency-optimised), **Creator → `scx_bpfland`**,
**Everyday / Work / Lite → `none`** (the in-tree scheduler on the tuned kernel is already good).
`set` requires `CONFIG_SCHED_CLASS_EXT`; on a kernel without sched_ext it prints *"running kernel
has no sched_ext — install lindos-kernel"* and exits 3. A missing scheduler binary degrades to a
logged skip, never an error — modes stay safe on the stock kernel.

## 8. See also

* [ANTI-CHEAT.md](ANTI-CHEAT.md) — why a tuned Linux kernel still cannot run kernel-level
  anti-cheat, and why Lindos ships no spoofer.
* [HARDWARE-CONTROL.md](HARDWARE-CONTROL.md) — `lindos-tune sched`, governor/EPP, zram, THP, MGLRU.
* [MODES.md](MODES.md) — which scheduler and THP mode each mode selects.
* [BUILDING.md](BUILDING.md) — the ISO pipeline the `35-kernel.sh` hook belongs to.
* [TRANSFER.md](TRANSFER.md) — how NTFS3/exFAT/BitLocker plumbing (§1.1) is actually used to read
  a Windows partition read-only.
* [DUALBOOT.md](DUALBOOT.md) — "restart into Windows" and the Secure-Boot/TPM checklist for
  Windows' own anti-cheat requirements (a different concern from §6's kernel-signing).
