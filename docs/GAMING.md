# Gaming on Lindos

> **Reality first.** Windows games run through **Proton** (Steam / umu-launcher / Heroic) or
> Wine (Lutris / Bottles) — a translation layer, not Windows. Most single-player titles work at
> near-native speed. Whether a **multiplayer** game runs is decided by its anti-cheat vendor and
> the publisher: **Valorant, Fortnite, League of Legends, Apex Legends, Rainbow Six Siege,
> Destiny 2, PUBG, Rust, Call of Duty, GTA Online, Battlefield do not run on any Linux, including
> Lindos.** Roblox works through **Sober**, not the Windows client. Minecraft Java is native;
> Bedrock only via the unofficial `mcpelauncher` (partial). The full, generated list is
> [COMPATIBILITY.md](COMPATIBILITY.md); the always-current sources are
> [ProtonDB](https://www.protondb.com/) and [Are We Anti-Cheat Yet?](https://areweanticheatyet.com/).
> The full, honest explanation of *why* — and why Lindos ships no anti-cheat spoofer — is in
> [ANTI-CHEAT.md](ANTI-CHEAT.md).

## 1. Launchers — `lindos-game`

```
lindos-game list [--json]                       the catalogue (/usr/share/lindos/gaming/launchers.json)
lindos-game status [--json]                     which launchers are installed and how (apt / flatpak / path)
lindos-game install <ID…|all> [--dry-run] [--force] [--json]
lindos-game launch <ID> [--install]             start a launcher (offers to install it when missing)
```

| id | Launcher | How it is installed | Notes |
|---|---|---|---|
| `steam` | Steam | Valve's apt repo (`repo.steampowered.com`, `steam-launcher`; fallback `steam-installer`) — **on the ISO** when `INCLUDE_STEAM=1` | enable *Steam Play for all titles* in Steam → Settings → Compatibility; multiplayer only works when the developer enabled its anti-cheat for Proton |
| `lutris` | Lutris | apt `lutris` (noble universe; fallback Flatpak `net.lutris.Lutris`) — on the ISO with Steam | Battle.net, EA app, Ubisoft Connect, GOG, Amazon, emulators |
| `heroic` | Heroic Games Launcher | pinned GitHub .deb (`HEROIC_VERSION` 2.22.1; fallback Flatpak `com.heroicgameslauncher.hgl`) | Epic Games Store, GOG, Amazon — **Fortnite excluded** (Epic keeps EAC-Linux disabled) |
| `prism` | Prism Launcher | Flatpak `org.prismlauncher.PrismLauncher` (bundles Java; apt only with `PRISM_PPA`) | Minecraft **Java** (native); needs a Microsoft account that owns the game |
| `sober` | Roblox (Sober) | Flatpak `org.vinegarhq.Sober` | community runtime for the official Android build; normal Roblox account; not affiliated with Roblox Corporation; `lindos-roblox.desktop` = *Roblox* |
| `vinegar` | Roblox Studio (Vinegar) | Flatpak `org.vinegarhq.Vinegar` | Studio only (Vinegar no longer runs the player); `lindos-roblox-studio.desktop` |
| `mcpelauncher` | Minecraft Bedrock | Flatpak `io.mrarm.mcpelauncher` | unofficial; needs the Android version bought on Google Play; Marketplace/Realms may not work — *partial* |
| `bottles` | Bottles | Flatpak `com.usebottles.bottles` | Wine prefix manager used by the Creator recipes |

Non-root `lindos-game install` goes through the polkit helper action `install-gaming` → root runs
`/usr/libexec/lindos/install-gaming.sh <items…>` (`[--from-chroot] [--no-flatpak] [--dry-run]
[--list]`; exit 0 ok / 1 an item failed / 2 usage or not root / 3 offline; log
`/var/log/lindos/install-gaming.log`). The Gaming mode pins Steam, Lutris, Heroic, Prism and
Roblox; the OOBE offers the same items.

Menu entries shipped: **Roblox** (`flatpak run org.vinegarhq.Sober`), **Roblox Studio**
(`flatpak run org.vinegarhq.Vinegar`), **Minecraft** (`lindos-game launch prism`).

## 2. GE-Proton — `lindos-proton`

```
lindos-proton list [--json] [--check] [--no-link] [--system]
lindos-proton update [--json] [--tag TAG] [--force] [--keep-download] [--no-verify] [-q]
lindos-proton install --system [<tag>] [--json]
lindos-proton remove <tag> [--json]
lindos-proton path [--json]
```

Downloads the latest (or `--tag`) GE-Proton x86_64 tarball from
`GloriousEggroll/proton-ge-custom`, verifies the published `.sha512sum`, installs it into
`~/.steam/root/compatibilitytools.d/` (or `~/.local/share/umu/compatibilitytools/` when Steam is
absent) and cross-links both directories, so Steam **and** `lindos-run`/umu see the same builds.
Exit 0 / 1 / 2 usage / 3 offline. `GITHUB_TOKEN` raises the API rate limit. Also reachable as
`lindos-compat proton …` and from Settings → Gaming → Proton-GE.

**System-wide GE-Proton.** `install --system [<tag>]` installs a GE-Proton build to
`/usr/share/lindos/proton/<tag>/` so it is available to **every user** on the machine, in addition to
the per-user `compatibilitytools.d`. Writing that system directory is a privileged action, so a
non-root call goes through the polkit helper (the tool never calls `sudo` itself). `list --system`
lists the system builds and `path` prints the resolved Proton directory.

**Resolution order** (which Proton `lindos-run` / umu actually use): an explicit
`UMU_PROTONPATH` or a per-title profile first, then the newest **system** GE
(`/usr/share/lindos/proton/`), then the newest **user** GE (`compatibilitytools.d`). `path` reports
exactly which one wins on your machine.

## 3. GPU drivers — `lindos-drivers`

```
lindos-drivers detect [--json]                                     GPUs (lspci -nn / sysfs)
lindos-drivers autodetect [--json]                                 GPU + Broadcom Wi-Fi + audio recs
lindos-drivers status [--json]      (also: lindos-drivers --status)  driver status report
lindos-drivers install [--auto|--nvidia-open|--nvidia-proprietary|--amd|--intel] [--dry-run] [--json]
lindos-drivers gui                                                 Driver Manager (mintdrivers)
lindos-drivers xpadneo install|remove|status
lindos-drivers xone install|remove|status [--accept-firmware-license] [--skip-firmware]
```

`autodetect` and `install --auto` extend the GPU logic to **Broadcom Wi-Fi** and **audio firmware**,
and a first-boot service offers the install on a fresh machine — see the dedicated
[DRIVERS.md](DRIVERS.md). The GPU-specific behaviour below is unchanged.

* NVIDIA drivers are **not** on the ISO (size, proprietary blob). `install --nvidia-open` /
  `--nvidia-proprietary` uses `ubuntu-drivers`' recommendation → `nvidia-driver-<N>[-open]`,
  `nvidia-settings`, `libnvidia-gl-<N>:i386` (+ `nvidia-prime` on hybrids), writes
  `/etc/modprobe.d/nvidia-lindos.conf` (`options nvidia-drm modeset=1 fbdev=1`), runs
  `update-initramfs -u` and asks for a reboot. `LINDOS_NVIDIA_DRIVER` overrides the package.
* `--amd` / `--intel`: `mesa-vulkan-drivers` (+`:i386`), `libvulkan1`, `vulkan-tools`,
  `libgl1-mesa-dri` (+`:i386`), `libgl1:i386`, firmware — the 32-bit halves are what Proton
  needs.
* Non-root `install` goes through the helper action `install-drivers`. `LINDOS_OFFLINE=1` forces
  the offline path (exit 3).
* **xpadneo** (Xbox controllers over Bluetooth, dkms, pinned v0.9.6) and **xone** (Xbox
  wireless adapter, dkms; Microsoft's dongle firmware is only downloaded with
  `--accept-firmware-license`) are optional and built on request.

## 4. Game Mode, MangoHud, controllers

* **Feral GameMode** — `/etc/gamemode.ini`: `desiredgov=performance`, `defaultgov=schedutil`,
  `renice=10`, `ioprio=0`, `softrealtime=auto`, `inhibit_screensaver=1`, `disable_splitlock=1`,
  GPU overclocking off; `[custom]` scripts `/usr/libexec/lindos/gamemode-start.sh` /
  `gamemode-end.sh` pause and restore the picom compositor (`lindos-compositor stop|start`,
  nothing to do in Lite) and log to `~/.local/state/lindos/gamemode.log`
  (`~/.config/lindos/gamemode-quiet` silences the notification). Steam: launch option
  `gamemoderun %command%`; Lutris/Heroic: their Game Mode toggle; `lindos-run`: `--gamemode` or
  automatically for games when the config key `gamemode_auto` is on (default on; Settings → Home
  → Game Mode).
* **MangoHud** — `/etc/xdg/MangoHud/MangoHud.conf`: `position=top-left`, `font_size=20`, FPS,
  frametime, CPU/GPU stats and temperatures, RAM/VRAM, engine/Wine/Vulkan driver, Lindos accent
  colours, **`no_display` by default, `Shift_R+F12` toggles the HUD**, launchers blacklisted.
  MangoHud only reads the per-user file, so `lindos-mangohud` manages
  `~/.config/MangoHud/MangoHud.conf`:

  ```
  lindos-mangohud status [--json] | on | off | toggle | sync [--json] | reset | edit
  ```

  `on`/`off`/`toggle` also write the config key `mangohud`; `sync` applies that key (called by
  the mode switch and Settings → Gaming); `edit` opens GOverlay or the file.
* **Controllers** — `/etc/udev/rules.d/60-lindos-controllers.rules` grants the logged-in user
  hidraw/USB/uinput access for Microsoft, Sony, Nintendo, 8BitDo, Valve and Logitech gamepads
  (complements Valve's `steam-devices`). Settings → Gaming → Controllers shows what is
  connected; `piper` for gaming mice, `antimicrox` for pad-to-keyboard mapping.
* **`vm.max_map_count = 2147483642`** is shipped always-on in
  `/etc/sysctl.d/80-lindos-gaming.conf` (many Proton titles need it).
* **32-bit Vulkan/GL** (`mesa-vulkan-drivers:i386`, `libgl1-mesa-dri:i386`, `libvulkan1:i386`),
  `steam-devices`, `vulkan-tools`, `mesa-utils` are on the ISO (`build/chroot/70-gaming.sh`).
* **Gaming mode** (`lindos-mode set gaming`) adds: `performance` governor, `ananicy-cpp` rules
  (`/etc/ananicy.d/lindos/`: games high priority, browsers normal, compilers idle), zram 75 %,
  `kernel.split_lock_mitigate=0`, `vm.compaction_proactiveness=0`, launcher pins. See
  [MODES.md](MODES.md).

## 5. Compatibility data

`/usr/share/lindos/compat-matrix.json` (lindos-gaming) is the single source of truth: 56 titles
with `status ∈ native | works | partial | not_possible | unknown`, `how`, `anticheat`, `reason`,
`link`. Lindos Settings → Gaming → Compatibility renders it, and `python3 tests/gen-compat-doc.py`
generates [COMPATIBILITY.md](COMPATIBILITY.md) from it (CI fails when the document is stale).
Statuses are a snapshot — a single publisher update can change them; always check ProtonDB and
Are We Anti-Cheat Yet before buying. Entries that work *because* the publisher enabled EAC/BattlEye
for Proton (Halo Infinite, Marvel Rivals, Elden Ring, …) carry an `anticheat` note saying so; the
`not_possible` entries keep the honest reason. [ANTI-CHEAT.md](ANTI-CHEAT.md) is the canonical
explanation of both columns.

## 6. Performance: the Lindos kernel, gamescope, DXVK/VKD3D and per-title profiles

The compat-performance layer makes every *allowed* title run fast and launch cleanly. None of it
changes the anti-cheat reality of §5 — it helps native, single-player and publisher-enabled
anti-cheat titles.

* **The Lindos kernel** ([KERNEL.md](KERNEL.md)) adds `ntsync` (Proton uses it automatically when
  `/dev/ntsync` exists — a big frametime win over esync/fsync), sched_ext low-latency schedulers,
  1000 Hz + full preemption and MGLRU. It is a Recommends of `lindos-meta`; the stock kernel stays
  as a fallback. Gaming mode selects the `scx_lavd` scheduler (`lindos-tune sched`).
* **gamescope** — `lindos-gamescope` wraps `gamescope <opts> -- <cmd>` with sane defaults from the
  mode/profile (`--help` for the flags); it is folded into `lindos-game` and `lindos-run
  --gamescope [WxH]` (with `--hdr`, `--fsr`). If `gamescope` is not installed the wrapper degrades
  with a clear message instead of failing.
* **A "Lindos Gaming" session** — `lindos-gaming` ships `/usr/bin/lindos-gamescope-session` and
  `/usr/share/wayland-sessions/lindos-gaming.desktop`, so **Lindos Gaming** appears as an optional
  session at the login screen. Choosing it boots straight into a gamescope Wayland micro-compositor
  hosting Steam Big Picture (or a chosen launcher) — a console-like experience — instead of the XFCE
  desktop. It **does not change the default XFCE session**; it is an extra option you pick at login.
  If gamescope or Steam are absent it degrades with a clear message rather than dropping you into a
  black screen. Wayland/gamescope makes *allowed* titles run smoothly; it does **not** make
  anti-cheat titles work (see [ANTI-CHEAT.md](ANTI-CHEAT.md)).
* **DXVK / VKD3D-Proton** — `lindos-compat install-dxvk` / `install-vkd3d` install or refresh
  DXVK and VKD3D-Proton into a Wine prefix from pinned release tarballs; version pins live in
  `/usr/share/lindos/compat/components.json`. Proton already bundles both — these are for plain
  Wine prefixes. `lindos-compat doctor` additionally checks Vulkan, `/dev/ntsync`, sched_ext,
  gamescope and DXVK/VKD3D presence and prints the exact fix for each.
* **Per-title profiles** — `/usr/share/lindos/gaming/profiles/*.json` (user overrides in
  `~/.config/lindos/gaming/profiles/`) set the runner, Proton build, env, gamescope size, FSR/HDR,
  DXVK-async and MangoHud per game; `lindos-run` and `lindos-game` resolve one by exe name / Steam
  AppID before launch. The starter set includes ordinary "works" titles and a `not_possible`
  example that only **documents why it cannot run and refuses to fake anything** — consistent with
  [ANTI-CHEAT.md](ANTI-CHEAT.md).

The performance env `lindos-run` sets for games (`PROTON_USE_NTSYNC` when `/dev/ntsync` exists,
`DXVK_ASYNC`, MangoHud/gamescope per flags) is documented in [WINDOWS-APPS.md](WINDOWS-APPS.md).

## 7. Play-anywhere — honest routes for blocked titles

For the 15 `not_possible` titles in the compat matrix, `lindos-game` adds routing commands instead
of pretending Wine/Proton can run them (SPEC-WINDOWS §30; the full honest explanation is
[ANTI-CHEAT.md](ANTI-CHEAT.md)):

```
lindos-game route <title|exe|appid> [--json] [--region CC]
lindos-game play  <title> [--route cloud|windows|proton|native] [--provider ID] [--region CC] [--yes]
lindos-game shortcut <title> --route cloud|windows [--provider ID] [--region CC]
lindos-game cloud install geforce-now [--system] [--print] [--json]
```

* **`route`** prints (or `--json`s) every honest route for a title: each **cloud** provider that
  actually carries it, and **restart into Windows** — never the Lindos VM (`type: vm` is always
  `available: false`; kernel-mode anti-cheat blocks virtual machines too, so a VM would only risk a
  hardware ban). Region comes from `--region`, `~/.config/lindos/config.json` `"region"`, the system
  locale (`LC_ALL`/`LANG`) or the timezone (`/etc/timezone`) — **never** IP geolocation. Cloud
  availability also checks whether the provider's client is actually installed (GeForce NOW's
  Flatpak, Boosteroid's `.deb`, or a Chrome/Edge binary for a browser provider — **never** Firefox
  for Xbox Cloud Gaming or Amazon Luna, and never a spoofed user agent). The Windows route checks
  `lindos-dualboot status --json` (`can_reboot_to_windows`) and warns when a title's
  `windows_requires` (`secure-boot`/`tpm2`) is not yet met on your Windows install.
* **`play`** launches the recommended (or `--route`-chosen) route: the GeForce NOW Flatpak or a
  provider's official page in Chrome/Edge for `cloud`; `lindos-dualboot reboot-to-windows` for
  `windows`, after a confirmation (skip with `--yes`) that shows the Secure-Boot/TPM checklist when
  needed. `--route vm` is always refused with the reason named.
* **`shortcut`** writes `~/.local/share/applications/lindos-play-<id>-<route>.desktop` (e.g.
  *"Valorant — restarts into Windows"*, *"Fortnite — NVIDIA GeForce NOW"*) that re-runs `lindos-game
  play … --yes` at click time, so it always re-checks region/dual-boot state rather than baking in a
  stale answer.
* **`cloud install geforce-now`** installs NVIDIA's own Linux Flatpak (`com.nvidia.geforcenow` from
  NVIDIA's `GeForceNOW` remote, **not Flathub** — Flathub does not carry it). `--system`
  (system-wide, every user) goes through `lindos.helper.install_flatpaks(ids, remote={"name":
  "GeForceNOW", "url": …})` → the privileged helper's `install-flatpaks` action (SPEC-WINDOWS
  §33.1/§30.4), which adds NVIDIA's remote instead of assuming Flathub; without `--system` (or if
  the helper is unavailable/fails), it runs the same two documented `flatpak remote-add`/`flatpak
  install --user` commands itself — a user-scope Flatpak needs no root at all — and always prints
  them too. `--print` shows the commands without running anything; `--json` returns the exact argv
  plus which helper action would run. Every other provider (`xbox-cloud`/`boosteroid`/
  `amazon-luna`) has no scripted installer here; `cloud install <id>` prints that provider's
  official download/play page instead.

The compat matrix's `cloud_providers` object (NVIDIA GeForce NOW, Xbox Cloud Gaming, Boosteroid,
Amazon Luna) and each blocked title's `routes` (`cloud`, `windows`, `windows_requires`, `vm: false`,
`verified`) are the single source of truth for all of this — the same JSON `python3
tests/gen-compat-doc.py` renders into [COMPATIBILITY.md](COMPATIBILITY.md)'s **Other ways to play**
section.
