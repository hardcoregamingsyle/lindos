# Hardware control

Where each hardware knob lives, which command drives it and what needs admin rights. The GUI for
all of it is **Lindos Settings → Hardware** (and → Gaming for refresh rate / controllers); the
CLI is `lindos-tune` (lindos-tune) plus `lindos-drivers` (lindos-gaming). Reading never asks for
a password; changing goes through the polkit helper (`org.lindos.helper`, one prompt covers a
burst of changes) — `lindos-tune` never calls `sudo` itself. `LINDOS_TUNE_NO_ESCALATE=1` turns
escalation off (it then prints the `sudo lindos-tune …` hint instead).

```
lindos-tune status [--json]
lindos-tune apply --mode <id> [--system] [--offline] [--dry-run] [--json]
lindos-tune services list|disable|enable <unit>… [--dry-run] [--json]
lindos-tune zram <percent> [--dry-run] [--json]
lindos-tune governor <g> [--dry-run] [--json]
lindos-tune fan list|set <profile> [--dry-run] [--json]
lindos-tune power <performance|balanced|power-saver> [--dry-run] [--json]
lindos-tune sched list|status|set <profile> [--dry-run] [--json]
lindos-tune report [-o FILE]
```

Exit 0 ok / 1 error / 2 usage. `--dry-run` never needs root. Log: `/var/log/lindos/tune.log`,
state `/var/lib/lindos-tune/state.json`.

## 1. CPU governor and EPP

`lindos-tune governor <schedutil|performance|powersave|ondemand|conservative|userspace>`
(Settings → Hardware → Processor → CPU governor; helper action `set-governor`).

Three layers, in order of preference:
1. **power-profiles-daemon present** → `powerprofilesctl set` (`performance` → performance,
   `powersave` → power-saver, anything else → balanced). PPD owns governor + EPP on such
   systems and persists its own choice.
2. Otherwise the governor is written to `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`
   (mapped onto what the driver offers — `intel_pstate` in active mode only knows
   `performance`/`powersave`, so `schedutil` becomes `powersave` there), the **EPP**
   (`energy_performance_preference`) is set to `performance` / `power` / `balance_performance`
   according to the governor, and both are persisted in `/etc/tmpfiles.d/lindos-governor.conf`.
3. No cpufreq (virtual machine) → recorded as *skipped*, never an error.

The EPP has no helper action of its own; Settings shows it read-only and it follows the governor.
Modes: Everyday/Work/Creator/Lite `schedutil`, Gaming `performance` (see [MODES.md](MODES.md));
GameMode switches to `performance` while a game runs and back to `schedutil` afterwards
(`/etc/gamemode.ini`).

### 1.1 sched_ext scheduler (needs the Lindos kernel)

`lindos-tune sched list|status|set <scx_lavd|scx_bpfland|scx_flash|scx_rustland|none>` loads a
runtime **sched_ext** BPF scheduler for lower latency, no reboot required. It needs a kernel with
`CONFIG_SCHED_CLASS_EXT` — the [Lindos kernel](KERNEL.md) has it; the stock Mint kernel does not.

* **list** shows the available scx schedulers: from `scx_loader` over D-Bus if present, else the
  `scx_*` binaries on `PATH`, else the built-in known list marked *"not installed"*.
* **status** reports the active scheduler (`/sys/kernel/sched_ext/state` + `root/ops` name).
* **set** loads a scheduler through the privileged helper action `set-sched` (loads/stops scx via
  `scx_loader`/systemd; never `sudo` directly); `set none` stops any running scx scheduler.

If the running kernel has no sched_ext, `set` prints *"running kernel has no sched_ext — install
lindos-kernel"* and exits **3**; a missing scheduler binary degrades to a **logged skip, never an
error**. Modes select a scheduler through the `tune.d/<id>.conf` `sched=` key — Gaming `scx_lavd`,
Creator `scx_bpfland`, the rest `none` — applied by `lindos-tune apply --mode` and reported by
`lindos-tune status`. Any systemd/ananicy artifacts a scheduler needs are shipped as **conffiles**.
See [KERNEL.md](KERNEL.md) §6 and [MODES.md](MODES.md).

## 2. Power profile

`lindos-tune power <performance|balanced|power-saver>` (Settings → Hardware → Power). Back-ends
in order: `powerprofilesctl` (power-profiles-daemon; works unprivileged for the logged-in user)
→ TLP (`tlp ac` / `tlp start` / `tlp bat`; needs a root shell: `sudo lindos-tune power …`) →
plain cpufreq governor (`performance` / `schedutil` / `powersave`) through the helper. The Work
mode enables `power-profiles-daemon` and picks *balanced*.

## 3. Fans and temperatures

`lindos-tune fan list [--json]` reads fans and temperatures from `sensors -j` (lm-sensors),
falling back to `/sys/class/hwmon`, and reports which controllers exist: **nbfc-linux**
(`nbfc`), **fancontrol** (`/etc/fancontrol`), **thinkpad_acpi** (`/proc/acpi/ibm/fan`).

`lindos-tune fan set <auto|quiet|balanced|max>` (helper action `set-fan-profile`):
* nbfc: `nbfc set -a` for auto, else `nbfc set -s <percent>` (quiet 30 %, balanced 55 %, max 100 %);
* thinkpad_acpi (`fan_control=1`): `level auto | 2 | 4 | 7`;
* otherwise the honest answer *"no controllable fans detected"* with the install hint.

nbfc-linux is **not** on the ISO; install it on request with
`pkexec /usr/libexec/lindos/install-nbfc.sh --yes` (`[--version VER] [--sha256 HEX] [--dry-run]
[--no-enable]`; pinned 0.5.3 Linux Mint 22 .deb from GitHub, enables `nbfc_service`), then
`nbfc config -r` (recommend a model config), `nbfc config -a "<name>"`, `nbfc start`,
`lindos-tune fan set balanced`. Desktop PCs: run `pwmconfig` once to create `/etc/fancontrol`.

`lm-sensors` autodetection (`sensors-detect --auto`) runs **once on the installed system's first boot** (never in the live USB session) through
`lindos-sensors-detect.service` → `/usr/libexec/lindos/sensors-detect-once.sh` (never inside the
ISO chroot, never twice, marker `/var/lib/lindos-tune/sensors-detect.done`).

## 4. GPU drivers

`lindos-drivers detect | status [--json] | install [--nvidia-open|--nvidia-proprietary|--amd|
--intel] [--dry-run] | gui | xpadneo … | xone …` (Settings → Hardware → Graphics; helper action
`install-drivers`). NVIDIA drivers are never preinstalled; `install --nvidia-open` (Turing and
newer) or `--nvidia-proprietary` follows `ubuntu-drivers`' recommendation, adds
`libnvidia-gl-<N>:i386` for Proton, `nvidia-settings`, `nvidia-prime` on hybrids, writes
`options nvidia-drm modeset=1 fbdev=1`, rebuilds the initramfs and asks for a reboot. `--amd` /
`--intel` install Mesa Vulkan + 32-bit libs + firmware. Mint's Driver Manager (`mintdrivers`) is
one click away for Wi-Fi/firmware. GPU clocks/fan curves: **CoreCtrl** (AMD/Intel) or
**NVIDIA Settings** — Lindos never touches clocks or power limits automatically
(`apply_gpu_optimisations=0` in `/etc/gamemode.ini`). Details: [GAMING.md](GAMING.md).

## 5. Memory: zram, earlyoom, sysctl

* `lindos-tune zram <percent>` (0 = off; helper action `set-zram`) — detects
  `systemd-zram-generator` (`/etc/systemd/zram-generator.conf`: `zram-size = ram * <frac>`,
  `compression-algorithm = zstd`, `swap-priority = 100`) or `zram-tools` (`/etc/default/zramswap`:
  `ALGO=zstd PERCENT=n PRIORITY=100`). Modes: 50 % / Gaming 75 % / Lite 100 %.
* earlyoom: `/etc/default/earlyoom` `EARLYOOM_ARGS="-m 4 -s 100 --avoid '(^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$'"`
  (Lite `-m 8`); helper action `enable-earlyoom`; `systemctl disable --now earlyoom` to opt out.
* sysctl base tune `/etc/sysctl.d/70-lindos-base.conf` and per-mode
  `/etc/sysctl.d/90-lindos-mode.conf` — see [RAM-BUDGET.md](RAM-BUDGET.md) and
  [MODES.md](MODES.md); `/etc/lindos/tune.d/<mode>.conf` overrides
  `ZRAM_PERCENT GOVERNOR EARLYOOM_MIN COMPOSITOR ANANICY` (+ `EARLYOOM_SWAP_MIN SWAPPINESS
  SERVICES_DISABLE SERVICES_ENABLE`).
* `vm.max_map_count=2147483642` (`/etc/sysctl.d/80-lindos-gaming.conf`, lindos-gaming) is always
  on.
* **Transparent Huge Pages** — the `thp=madvise|always|never` key (`tune.d/<id>.conf`) is applied
  by `lindos-tune apply --mode` to `/sys/kernel/mm/transparent_hugepage/enabled` (guarded; every
  mode defaults to `madvise`) and reported by `lindos-tune status`.
* **MGLRU** — the `mglru=on|off` key writes `/sys/kernel/mm/lru_gen/enabled` (multi-generational
  LRU; the [Lindos kernel](KERNEL.md) builds it in). Lite turns it `on`; other modes leave the
  kernel default. A kernel without the sysfs knob degrades to a logged skip.

## 6. Services

`lindos-tune services list` — every unit in the whitelist
(`/usr/share/lindos/tune/services-whitelist.txt`, mirrored by `lindos.helper.SERVICE_WHITELIST`)
with enabled/active state; `enable|disable <unit>…` toggles them (`systemctl enable/disable
--now`; unprivileged runs go through the helper action `set-services`, which calls back into
`lindos-tune services` as root so the conditional autostart entries — blueman-applet ⇄
`bluetooth.service` — are hidden/unhidden too). Units outside the whitelist are refused
(exit 1). Whitelisted:
`bluetooth ModemManager avahi-daemon(.socket) cups(.socket/.path) cups-browsed(.socket)
NetworkManager-wait-online apport whoopsie kerneloops brltty speech-dispatcher ubuntu-report
mintreport systemd-oomd motd-news.timer apt-daily(.timer) apt-daily-upgrade.timer
unattended-upgrades fwupd(-refresh.timer) packagekit geoclue switcheroo-control colord upower
udisks2 cron anacron e2scrub_all.timer plymouth-quit-wait snapd(.socket) ufw wpa_supplicant
blueman-mechanism warpinator preload haveged smartmontools earlyoom fstrim.timer tmp.mount
zramswap systemd-zram-setup@zram0 ananicy-cpp power-profiles-daemon tlp thermald irqbalance
nbfc_service fancontrol lm-sensors openrgb gamemoded`.

Note: unprivileged `lindos-tune services enable|disable` goes through the helper and does not
hide/unhide the conditional `blueman-applet` autostart; `lindos-tune apply` (and root runs) do.

## 7. Storage: TRIM

`fstrim.timer` is enabled by the base preset (weekly TRIM); Settings → Hardware → SSD TRIM
shows its state and offers to enable it (`set-services {enable:[fstrim.timer]}`).

## 8. Display refresh rate

Settings → Gaming → Display refresh rate lists each monitor's modes from `xrandr` and switches
with `xrandr --output <name> --rate <hz>` — **session-only**; make it permanent in Settings →
System → Display (`xfce4-display-settings`). `Super+P` opens the quick display switcher.

## 9. RGB, mice, controllers

* RGB lighting → **OpenRGB** (Settings → Hardware → RGB lighting; installed on demand).
* Gaming mice → **Piper** (libratbag).
* Controllers → udev rules `60-lindos-controllers.rules` (Xbox, PlayStation, Nintendo, 8BitDo,
  Steam, Logitech), optional `lindos-drivers xpadneo install` (Xbox over Bluetooth) and
  `lindos-drivers xone install --accept-firmware-license` (Xbox wireless adapter),
  `antimicrox` for mapping. See [GAMING.md](GAMING.md).

## 10. Everything at once: `lindos-tune apply` and `lindos-tune report`

`lindos-tune apply --mode <id> --system [--offline]` is what the mode switch and the ISO build
run: base sysctl, zram, earlyoom, journald, `tmp.mount`, presets, mode services, mode sysctl,
governor + EPP, ananicy on/off, autostart hiding — idempotent, `--offline` never starts units or
touches the network. `lindos-tune report [-o FILE]` writes the Markdown you should attach to a bug
report (RAM, zram, top RSS, units, governor, kernel, mode, GPU, config).
