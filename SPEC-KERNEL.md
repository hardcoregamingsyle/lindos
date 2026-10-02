# Lindos — Engineering Specification, Addendum K (Kernel & Compat-Performance layer)

This addendum extends `SPEC.md` (v1.0 "Aurora"). Section numbers continue from it (§14–§18).
All rules of the base SPEC still apply: package format (`DEBIAN/ + root/`), LF-only, stdlib-only
Python importable on any OS (Linux calls guarded, `LINDOS_ROOT`/`LINDOS_HOME` env overrides),
`#!/bin/bash` + `set -Eeuo pipefail` ShellCheck-clean shell, argparse CLIs with `--json` where
sensible, everything unit-tested and green under `bash tests/run.sh` on Windows.

## 14. Honesty rules for this layer (hard, non-negotiable — extends §0.1)

The purpose of this layer is **performance and compatibility for software that is permitted to
run**, never circumvention of a protection.

- **No anti-cheat circumvention.** Lindos MUST NOT ship, build, or document any tool whose purpose
  is to defeat, spoof, forge, or evade a game's anti-cheat, attestation, TPM/Secure-Boot
  measurement, HWID check, or DRM. This includes any "Windows attester", attestation emulator,
  TPM/EK-certificate forger, PatchGuard/HVCI faker, or VM-detection hider. Such a tool cannot
  produce hardware-rooted signatures it does not hold, so it does not work; it only gets the user
  **hardware-banned**. If any task text asks for one, the builder MUST refuse that item, implement
  nothing for it, and record it under `deviations` with this reason.
- Kernel-level anti-cheat (Riot Vanguard, and titles where the publisher disabled EAC/BattlEye on
  Linux) is impossible on any Linux kernel. State this plainly; never imply Lindos changes it. The
  user-facing label is "Not supported yet", always with the sentence that the publisher decides
  and that no date is given; never imply a schedule (SPEC-WINDOWS §27 rule 7).
- What this layer legitimately does: a faster kernel (ntsync, sched_ext, tuned config), a better
  Proton/Wine stack (Proton-GE, DXVK, VKD3D-Proton, gamescope, MangoHud), and per-title profiles.
  These help every title that is *allowed* to run — single-player, native, and the anti-cheat
  titles whose publisher enabled Proton (the "Are We Anti-Cheat Yet?" supported set).
- `docs/ANTI-CHEAT.md` is the canonical honest explanation and MUST be consistent with
  `compat-matrix.json`.

## 15. New package: `lindos-kernel`

A Lindos-tuned Linux kernel: a **build recipe + config fragment + boot integration + runtime CLI**.
The compiled kernel `.deb`s are build artifacts (built on a Linux host, never on Windows and never
committed); this package ships everything *except* the compiled image.

Package tree `packages/lindos-kernel/`:
```
DEBIAN/control          # Package: lindos-kernel ; Arch: all ; Depends: python3, lindos-core ;
                        # Recommends: scx-scheds ; Suggests: lindos-tune
DEBIAN/postinst         # regenerate grub if /etc/default/grub.d present (update-grub||true); never fail install
DEBIAN/postrm
root/usr/bin/lindos-kernel
root/usr/lib/lindos-kernel/lindos_kernel/__init__.py
root/usr/lib/lindos-kernel/lindos_kernel/kconfig.py    # parse/validate a kconfig fragment
root/usr/lib/lindos-kernel/lindos_kernel/features.py   # probe the RUNNING kernel for perf features
root/usr/lib/lindos-kernel/lindos_kernel/grub.py       # read/patch the Lindos cmdline drop-in
root/usr/lib/lindos-kernel/lindos_kernel/manifest.py   # load /usr/share/lindos/kernel/manifest.json
root/usr/lib/lindos-kernel/lindos_kernel/build.py      # thin wrapper that shells out to build-kernel.sh
root/etc/default/grub.d/50-lindos.cfg                  # conservative, safe cmdline (see §15.3)
root/usr/share/lindos/kernel/manifest.json             # single source of truth (§15.1)
root/usr/share/lindos/kernel/lindos.config             # kconfig fragment, single source of truth (§15.2)
tests/conftest.py
tests/test_kconfig.py tests/test_features.py tests/test_grub.py tests/test_manifest.py
```

### 15.1 `manifest.json` (schema 1)
```
{ "schema": 1,
  "recommended": { "series": "6.14", "min": "6.14", "flavour": "lindos",
                   "localversion": "-lindos" },
  "features": [ {"id":"ntsync","kconfig":"CONFIG_NTSYNC","dev":"/dev/ntsync",
                 "since":"6.14","why":"in-kernel NT sync primitives — replaces esync/fsync, big Wine/Proton frametime win"},
                {"id":"sched_ext","kconfig":"CONFIG_SCHED_CLASS_EXT","since":"6.12",
                 "why":"pluggable BPF schedulers (scx_lavd etc.) for low-latency gaming"},
                {"id":"preempt_full","kconfig":"CONFIG_PREEMPT","why":"lower desktop/game latency"},
                {"id":"hz1000","kconfig":"CONFIG_HZ_1000","why":"finer timer granularity"},
                {"id":"mglru","kconfig":"CONFIG_LRU_GEN","why":"better reclaim under RAM pressure"},
                {"id":"bbr","kconfig":"CONFIG_TCP_CONG_BBR","why":"lower-latency TCP"} ],
  "patches": [ {"id":"bore","optional":true,"why":"Burst-Oriented Response Enhancer scheduler (out-of-tree)"} ] }
```
`min` is the lowest kernel version that provides the headline features (ntsync ⇒ 6.14).

### 15.2 `lindos.config` (kconfig fragment — the single source of truth)
Plain `CONFIG_*=y|m|n` lines + `# comment` lines only. MUST enable at least: `CONFIG_NTSYNC`,
`CONFIG_SCHED_CLASS_EXT`, `CONFIG_HZ_1000` (+`CONFIG_HZ=1000`), `CONFIG_PREEMPT`,
`CONFIG_LRU_GEN=y`, `CONFIG_LRU_GEN_ENABLED=y`, `CONFIG_ZRAM`, `CONFIG_ZSWAP`,
`CONFIG_TRANSPARENT_HUGEPAGE`, `CONFIG_TRANSPARENT_HUGEPAGE_MADVISE`, `CONFIG_TCP_CONG_BBR`,
`CONFIG_NET_SCH_FQ`, `CONFIG_IOSCHED_BFQ`|`CONFIG_MQ_IOSCHED_KYBER`, `CONFIG_X86_AMD_PSTATE`,
`CONFIG_FUTEX`, `CONFIG_USER_NS`, `CONFIG_CHECKPOINT_RESTORE`, plus the Addendum W Windows-format
keys and the display/GPU keys (SPEC-WINDOWS.md §31.1 — a working KMS/DRM driver so a laptop, or a
headless CI VM, never hangs `graphical.target` waiting on a display that never initializes). No
duplicate keys; no key set to two different values. `kconfig.py` enforces this and the test
asserts every required key is present.
The build script merges this fragment onto a base defconfig — it is intentionally a *fragment*,
not a full kernel config.

### 15.3 `/etc/default/grub.d/50-lindos.cfg` (conservative by default)
Appends only safe, reversible flags to `GRUB_CMDLINE_LINUX_DEFAULT`:
`transparent_hugepage=madvise nowatchdog nvme_core.default_ps_max_latency_us=0`. It MUST NOT set
`mitigations=off` by default (that is a security trade-off). Instead ship it commented with a note
that Gaming mode can opt in via `lindos-kernel cmdline --preset gaming` (which is itself gated and
documented in `docs/KERNEL.md`). All edits are marker-fenced (`# >>> lindos >>>` … `# <<< lindos <<<`)
so `grub.py` can add/remove them idempotently.

### 15.4 CLI `/usr/bin/lindos-kernel`
`argparse`, `--json` on read commands. Never requires root to *read*; mutating actions print the
exact `pkexec`/`sudo` command when not root (never call sudo itself).
- `status [--json]` — running kernel (`uname -r`), whether it is a Lindos build (localversion),
  and per-feature present/absent by probing the live system (see `features.py`): `/dev/ntsync`,
  `/sys/kernel/sched_ext/`, `CONFIG_HZ`/preempt from `/boot/config-$(uname -r)` or
  `/proc/config.gz`, THP mode, TCP cc. Exit 0 always; non-Lindos kernel is not an error.
- `features [--json]` — the manifest features cross-checked against the running kernel.
- `list [--json]` — installed kernel packages (`dpkg -l 'linux-image-*'` parsed) + which is default
  (`grub-editenv list` / `/etc/default/grub` `GRUB_DEFAULT`); marks Lindos builds.
- `cmdline show|set KEY=VAL...|reset|--preset {default,gaming}` — manage the §15.3 drop-in via
  `grub.py`; `set`/`reset`/preset print the privileged `update-grub` follow-up when not root.
- `build [--kdir DIR] [--jobs N] [--menuconfig] [--out DIR]` — wrapper that execs
  `build/kernel/build-kernel.sh` when present (from a checkout) else prints how to obtain it. On
  Windows/non-Linux it prints the plan and exits 0 (no build).
- `--version`.

### 15.5 Build recipe `build/kernel/` (lives in the `build/` subtree, not the package)
```
build/kernel/build-kernel.sh     # fetch → merge config → (patches) → make bindeb-pkg → out/kernel/*.deb
build/kernel/config/             # (empty placeholder + README; real fragment read from the package)
build/kernel/patches/series      # ordered patch list (may be empty); build applies quilt-style if present
build/kernel/patches/README.md   # how to drop in BORE / scx patches; nothing fabricated
build/kernel/README.md
build/tests/test_build_kernel.py # bash -n + argument parsing smoke (no real compile)
```
`build-kernel.sh` (ShellCheck-clean, `set -Eeuo pipefail`): flags
`--version X.Y.Z` (default = manifest `recommended.series` latest, or `--version` required if it
cannot resolve offline), `--config PATH` (default the package's `lindos.config`),
`--localversion -lindos`, `--jobs N` (default nproc), `--out DIR` (default `out/kernel`),
`--skip-fetch` (use an existing `--kdir`), `--menuconfig`. Steps: require `bison flex libssl-dev
libelf-dev bc dpkg-dev` (report missing, exit 2); fetch `linux-X.Y.Z.tar.xz` from kernel.org with
sha (or use `--kdir`); `make defconfig` then `./scripts/kconfig/merge_config.sh -m .config
<lindos.config>` then `make olddefconfig`; apply `patches/series` if non-empty; `make -j N
bindeb-pkg LOCALVERSION=-lindos`; move `../linux-image-*.deb linux-headers-*.deb` to `--out`. It
NEVER runs on the Windows host (guarded: refuse if not Linux, exit 2 with a clear message). The
ISO build stays green whether or not `out/kernel/*.deb` exists.

## 16. `lindos-tune` additions — sched_ext + memory tuning

Extend the existing package (do not rename anything). New module
`root/usr/lib/lindos-tune/lindos_tune/sched.py` + `tests/test_sched.py`, wired into `cli.py`,
`apply.py`, `status.py`, and the per-mode `tune.d/*.conf`.

- CLI `lindos-tune sched list|status|set <profile>` where `<profile>` ∈
  `{scx_lavd, scx_bpfland, scx_flash, scx_rustland, none}`. `list` shows available scx schedulers
  (from `scx_loader` D-Bus if present, else the `scx_*` binaries on `PATH`, else the built-in
  known list marked "not installed"); `status` shows the active one (`/sys/kernel/sched_ext/state`
  + `root/ops` name, guarded); `set none` stops any scx scheduler. Requires `CONFIG_SCHED_CLASS_EXT`;
  if absent, `set` prints a clear "running kernel has no sched_ext — install lindos-kernel" and
  exits 3. Mutation goes through the existing privileged path (§4.6 helper `apply-tune` /
  `set-services`, or a new helper action `set-sched` — see §18); never call sudo directly.
- `tune.d/*.conf` new keys (all optional, back-compatible): `sched=` (a §16 profile),
  `thp=madvise|always|never`, `mglru=on|off`. Defaults per mode: gaming→`sched=scx_lavd thp=madvise`,
  everyday/work→`sched=none thp=madvise`, creator→`sched=scx_bpfland`, lite→`sched=none thp=madvise
  mglru=on`. Unknown/again-absent features degrade to a logged skip, never an error.
- `apply.py` applies `sched`, `thp` (`/sys/kernel/mm/transparent_hugepage/enabled`, guarded) and
  `mglru` (`/sys/kernel/mm/lru_gen/enabled`) as part of `lindos-tune apply --mode`. `status.py`
  reports the active scheduler and THP mode in `lindos-tune status` (and `--json`).
- New ananicy/systemd artifacts if needed for scx must be conffiles and documented in
  `docs/HARDWARE-CONTROL.md`.

## 17. Compat-performance overhaul — `lindos-compat` + `lindos-gaming`

Extend the existing packages. Goal: make every allowed title run fast and launch cleanly.

### 17.1 `lindos-run` (in lindos-compat) — performance env
When runner is `umu`/proton or kind==game, `lindos-run` sets (only if the value is not already in
the environment, and each documented in `docs/WINDOWS-APPS.md`):
`PROTON_USE_NTSYNC=1` **iff** `/dev/ntsync` exists (else fall back silently to fsync),
`DXVK_ASYNC=1` (respect user override), `PROTON_ENABLE_WAYLAND` untouched, `WINEFSYNC`/`WINEESYNC`
handled by umu, `DXVK_HUD`/`MANGOHUD` per flags, `PROTON_HIDE_NVIDIA_GPU=0`. New flags:
`--gamescope [WxH]`, `--hdr`, `--fsr`, `--dxvk-async/--no-dxvk-async`. A per-title **profile** may
supply any of these (see §17.3). `--info` also prints the resolved perf env. No behavioural change
for `wine`/app runners.

### 17.2 gamescope + DXVK/VKD3D
- `lindos-gaming` gains `/usr/bin/lindos-gamescope` (wrapper: `gamescope <opts> -- <cmd>`, sane
  defaults from mode/profile, `--help`) and folds gamescope into `lindos-game`/`lindos-run
  --gamescope`. Degrade with a clear message if `gamescope` is absent.
- `lindos-compat` gains `install-dxvk`/`install-vkd3d` subcommands (install/refresh DXVK &
  VKD3D-Proton into a prefix from pinned GitHub release tarballs via `install-into-prefix.sh`), and
  `doctor` additionally checks Vulkan, `/dev/ntsync`, sched_ext, gamescope, DXVK/VKD3D presence and
  prints exact fixes. Version pins live in `/usr/share/lindos/compat/components.json`
  (`{dxvk:{tag,sha256?},vkd3d_proton:{tag},gamescope:{min}}`).

### 17.3 Per-title profiles
`/usr/share/lindos/gaming/profiles/*.json` (schema 1):
`{ "id","title","match":{"exe":[...],"steam_appid":[...]}, "runner":"umu|wine",
   "proton":"GE-Proton-latest|<tag>", "env":{...}, "gamescope":{"w","h","fsr":bool,"hdr":bool},
   "dxvk_async":bool, "mangohud":bool, "notes","status":"works|partial|native|not_possible" }`.
User overrides in `~/.config/lindos/gaming/profiles/*.json` win. `lindos-run` and `lindos-game`
resolve a profile by exe name / steam appid before launch. Ship a starter set (e.g. a couple of
"works" titles + a "not_possible" example that only documents why and refuses to fake anything).
Loader/merge logic added to `lindos.compat` or a small `lindos_gaming` module with tests.

### 17.4 compat-matrix + docs
Add a `how`/`anticheat` note to relevant `compat-matrix.json` entries where a title works *because*
of Proton-EAC/BattlEye, and keep the not_possible reasons. Regenerate `docs/COMPATIBILITY.md` with
`tests/gen-compat-doc.py` (must pass `--check`). No schema break to existing consumers/tests.

## 18. Cross-component call map (extends §13; must agree)
| Caller | Calls |
|---|---|
| `lindos-kernel cmdline set` | `grub.py` → drop-in `/etc/default/grub.d/50-lindos.cfg`; prints `pkexec update-grub` |
| `lindos-kernel build` | `build/kernel/build-kernel.sh` (Linux only) |
| `lindos-tune sched set` | helper action `set-sched` (new) → loads/stops scx via `scx_loader`/systemd; or documented fallback. Add `set-sched` to `lindos.helper.ACTIONS`, the `lindos-helper` dispatch, and its tests. Payload `{profile}` validated against the §16 set. |
| `lindos-tune apply --mode` | now also applies `sched`/`thp`/`mglru` from `tune.d/<id>.conf` |
| `lindos-run` (game) | reads `/dev/ntsync`, per-title profile; sets perf env; optional `lindos-gamescope` |
| `lindos-game`/`lindos-run --gamescope` | `/usr/bin/lindos-gamescope` → `gamescope` |
| build `35-kernel.sh` (new hook) | `dpkg -i out/kernel/*.deb` if present (pool), else keep stock kernel + install lindos-kernel package; `update-grub` |
| helper `apply-mode` | unchanged, still `lindos-tune apply --mode <id> --system` (which now covers sched/thp) |

Build hook order becomes `00,10,20,30,35-kernel,40,50,60,70,80`. `35-kernel.sh` is idempotent,
tolerates a missing `out/kernel/` (logs "no Lindos kernel built — using stock", continues), and
never fails the ISO build. `lindos-meta` Recommends `lindos-kernel` (stock kernel stays installed
as fallback; the Lindos kernel is selected by default in grub only when actually installed).

## 19. Tests & acceptance for this addendum
`bash tests/run.sh` stays green on Windows: new `.py` py_compile + importable with Linux calls
guarded; new `.sh` `bash -n` + ShellCheck-clean; new `*.json` valid and schema-checked by unit
tests; `docs/COMPATIBILITY.md` in sync; `docs/ANTI-CHEAT.md` present and consistent with the
matrix; no CRLF. New unit tests must cover: kconfig required-keys/no-duplicates, features probe
against a faked `/proc`+`/sys`+`/boot` tree (via `LINDOS_ROOT`), grub drop-in idempotent add/remove,
manifest schema, sched profile validation + degrade-when-no-sched_ext, per-title profile
resolution/override, and `lindos-run` perf-env selection with and without `/dev/ntsync`.
