# Kernel patches (optional, out-of-tree)

`build-kernel.sh` builds a **stock upstream kernel** by default. The only Lindos change baked in
is the config fragment (`packages/lindos-kernel/root/usr/share/lindos/kernel/lindos.config`):
ntsync, sched_ext, 1000 Hz + full preempt, MGLRU, zram/zswap, BBR. That is enough for the
headline Wine/Proton and latency wins and needs **no patches at all**.

If you want an out-of-tree scheduler on top, drop the patch files in this directory and list
them, in apply order, in [`series`](series). The build applies each with `patch -p1` from the
kernel source root, before `make ... bindeb-pkg`.

## Where to get the patches (fetch them yourself — none are shipped here)

This repository **does not fabricate or vendor any patch contents**. Obtain them from their
upstream projects, verify them, and place them here:

- **BORE** (Burst-Oriented Response Enhancer scheduler) — <https://github.com/firelzrd/bore-scheduler>.
  Pick the `.patch` matching your kernel series (e.g. the `6.14` directory) and add it to `series`.
- **sched_ext / scx schedulers** (`scx_lavd`, `scx_bpfland`, …) — these are **already enabled by
  the config fragment** via `CONFIG_SCHED_CLASS_EXT=y` on kernel ≥ 6.12; the schedulers themselves
  are BPF programs shipped by the `scx-scheds` package and loaded at runtime by
  `lindos-tune sched set …`. You normally need **no kernel patch** for scx.
- **CachyOS / XanMod patch sets** — if you prefer a curated stack, take individual patches from
  their trees (respecting their licences) and list them here.

## Rules

- Keep `series` in the intended apply order; the build fails loudly if a listed patch is missing.
- Patches are applied **after** `make defconfig` + fragment merge + `make olddefconfig`, so a
  patch that adds new Kconfig symbols may need a matching `CONFIG_*` line in `lindos.config`.
- Licence compliance is on you: only add patches whose licence permits redistribution/building,
  and record them in the repo's `THIRD_PARTY.md` if you commit them.

## Honesty

No patch here can, or will, defeat an anti-cheat, forge attestation/TPM/Secure-Boot state, or
hide a VM/HWID. Kernel-level anti-cheat (Riot Vanguard; titles where the publisher disabled
EAC/BattlEye on Linux) is impossible on any Linux kernel — a patch cannot change that, it only
gets the user hardware-banned. Lindos ships nothing of the sort.
