# build/kernel — the Lindos kernel build recipe (SPEC-KERNEL §15.5)

This directory builds the Lindos-tuned Linux kernel `.deb`s. The compiled images are **build
artifacts**: they are produced on a Linux host, land in `out/kernel/`, and are **never committed**
to the repository. The `lindos-kernel` package (under `packages/`) ships everything *except* the
compiled image — the config fragment, feature manifest, GRUB drop-in and the `lindos-kernel` CLI.

## What the tuned kernel changes

A stock upstream kernel plus the config fragment
`packages/lindos-kernel/root/usr/share/lindos/kernel/lindos.config`:

| Feature | Symbol | Why |
|---|---|---|
| ntsync | `CONFIG_NTSYNC` | in-kernel NT sync primitives — replaces esync/fsync, big Wine/Proton frametime win (kernel ≥ 6.14) |
| sched_ext | `CONFIG_SCHED_CLASS_EXT` | pluggable BPF schedulers (`scx_lavd`, `scx_bpfland`) for low-latency gaming (≥ 6.12) |
| full preempt | `CONFIG_PREEMPT` | lower desktop/game latency |
| 1000 Hz | `CONFIG_HZ_1000` | finer timer granularity |
| MGLRU | `CONFIG_LRU_GEN` | better reclaim under RAM pressure |
| BBR + fq | `CONFIG_TCP_CONG_BBR`, `CONFIG_NET_SCH_FQ` | lower-latency networking |
| zram/zswap, THP, amd_pstate | … | the Lindos RAM/perf story (§11) |

## Build it (Linux only)

```sh
# from a repo checkout, on a Debian/Ubuntu host:
sudo apt-get install bison flex libssl-dev libelf-dev bc dpkg-dev build-essential
build/kernel/build-kernel.sh                 # uses the manifest's recommended series (6.14)
build/kernel/build-kernel.sh --version 6.14.2 --jobs 8
build/kernel/build-kernel.sh --kdir ~/src/linux --skip-fetch   # use an existing tree
```

Steps: `make defconfig` → `scripts/kconfig/merge_config.sh -m .config lindos.config` →
`make olddefconfig` → apply `patches/series` (if non-empty) → `make -j N bindeb-pkg
LOCALVERSION=-lindos` → move `linux-image-*.deb` / `linux-headers-*.deb` into `--out`
(default `out/kernel`).

It **refuses to run on non-Linux** (exit 2) and reports missing dependencies (exit 2). It never
installs anything and never calls `sudo`.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--version X.Y.Z` | manifest `recommended.series` (6.14) | kernel version to fetch |
| `--config PATH` | the package's `lindos.config` | config fragment to merge |
| `--localversion` | `-lindos` | appended to the kernel release string |
| `--jobs N` | `nproc` | parallel make jobs |
| `--out DIR` | `out/kernel` | where the `.deb`s go |
| `--kdir DIR` | — | use an existing kernel tree (implies `--skip-fetch`) |
| `--skip-fetch` | off | do not download; requires `--kdir` |
| `--menuconfig` | off | run `menuconfig` after merging the fragment |

## How it plugs into the ISO build

The ISO build's `35-kernel.sh` hook (owned by the build subtree) does
`dpkg -i out/kernel/*.deb` **if present**, otherwise it logs "no Lindos kernel built — using
stock" and continues. **The ISO build stays green whether or not `out/kernel/*.deb` exists**, so
you can ship Lindos on the stock Mint/Ubuntu kernel and add the tuned kernel later. `lindos-meta`
only *Recommends* `lindos-kernel`; the stock kernel always stays installed as a fallback.

## Patches

Out-of-tree patches (BORE, etc.) are optional and **not shipped** — see
[`patches/README.md`](patches/README.md). scx schedulers need **no** patch; they are runtime BPF
programs enabled by `CONFIG_SCHED_CLASS_EXT` and loaded by `lindos-tune sched set`.

## Honesty

This recipe builds a performance/compat kernel for software that is **allowed** to run. It does
not, and cannot, defeat anti-cheat, forge attestation/TPM/Secure-Boot state, or hide a VM/HWID.
Kernel-level anti-cheat (Vanguard; publisher-disabled EAC/BattlEye) is impossible on Linux.
