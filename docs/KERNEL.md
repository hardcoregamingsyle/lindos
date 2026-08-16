# The Lindos kernel

> **What it is.** `lindos-kernel` is a Linux kernel tuned for desktop latency and Wine/Proton
> gaming — ntsync, sched_ext, 1000 Hz + full preemption, MGLRU and BBR — shipped as a **build
> recipe, config fragment, boot integration and a runtime CLI**. The compiled kernel is a build
> artifact you (or CI) build on a Linux host; it is never committed and never built on Windows.
> The stock Mint/Ubuntu kernel stays installed as a fallback, so an install that has no Lindos
> kernel simply keeps booting the stock one.
>
> **What it is not.** A faster kernel is still a **Linux** kernel. It does not run Windows
> kernel-mode anti-cheat and changes nothing about the anti-cheat reality in
> [ANTI-CHEAT.md](ANTI-CHEAT.md). Do not expect Valorant, Fortnite or Call of Duty to work because
> the kernel is tuned — they cannot, for the reasons in that document.

The kernel layer is specified in [`SPEC-KERNEL.md`](../SPEC-KERNEL.md) §15. Package tree:
`packages/lindos-kernel/`; build recipe: `build/kernel/`.

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
2. `make defconfig` → `./scripts/kconfig/merge_config.sh -m .config <lindos.config>` →
   `make olddefconfig`. The fragment is intentionally a **fragment** merged onto the base
   defconfig, not a whole kernel config.
3. Apply `build/kernel/patches/series` if it is non-empty (quilt-style; this is where BORE / scx
   patches are dropped in — see `build/kernel/patches/README.md`; nothing is bundled that is not
   really there).
4. `make -j N bindeb-pkg LOCALVERSION=-lindos` and move the resulting
   `linux-image-*.deb` / `linux-headers-*.deb` into `--out` (default `out/kernel`).

Flags: `--version X.Y.Z`, `--config PATH` (default the package's `lindos.config`),
`--localversion -lindos`, `--jobs N` (default `nproc`), `--out DIR`, `--skip-fetch`, `--menuconfig`.

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
lindos-kernel build [--kdir DIR] [--jobs N] [--menuconfig] [--out DIR]
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

## 6. How modes pick a scheduler

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

## 7. See also

* [ANTI-CHEAT.md](ANTI-CHEAT.md) — why a tuned Linux kernel still cannot run kernel-level
  anti-cheat, and why Lindos ships no spoofer.
* [HARDWARE-CONTROL.md](HARDWARE-CONTROL.md) — `lindos-tune sched`, governor/EPP, zram, THP, MGLRU.
* [MODES.md](MODES.md) — which scheduler and THP mode each mode selects.
* [BUILDING.md](BUILDING.md) — the ISO pipeline the `35-kernel.sh` hook belongs to.
