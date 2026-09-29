# Lindos Modes

A **Mode** is a named bundle of taskbar pins, packages, services, kernel settings, CPU governor,
zram size and compositor choice. You pick one in **Lindos Setup** (the wizard on your first login) and can
switch any time — in **Lindos Settings → Lindos Mode** or with `lindos-mode set <id>`. Exactly five modes
exist: `everyday`, `gaming`, `work`, `creator`, `lite`.

> **Mode apps are installed by the installer, for every Mode.** The Mode is chosen only after the
> installation, so the Lindos installer installs the *union* of every Mode's `packages`, `flatpaks` and
> extras once, while it installs ([INSTALLER.md](INSTALLER.md)). Choosing or switching a Mode therefore
> normally installs nothing: Lindos Setup applies a Mode's configuration only (`install: false`), and
> `lindos-mode set` / Settings install just what is still missing. If the install was offline, the missing
> apps are listed in Lindos Settings › Apps › Left to finish from setup.

Every value in the table below is read from the shipped files:
`packages/lindos-core/root/usr/share/lindos/modes/<id>/mode.json` (definition),
`packages/lindos-tune/root/etc/lindos/tune.d/<id>.conf` (tune overrides) and
`packages/lindos-desktop/root/usr/share/lindos/modes/<id>/panel/` (panel profile).

## 1. At a glance

| | Everyday | Gaming | Work | Creator | Lite |
|---|---|---|---|---|---|
| id | `everyday` | `gaming` | `work` | `creator` | `lite` |
| One line | Balanced default for browsing, media and everyday work. | Max FPS: performance governor, Game Mode, MangoHud toggle, launchers pinned. | Productivity: office and mail pinned, balanced power profile, night light on. | Creative apps and Adobe-era recipes through Wine/Bottles; GIMP, Krita, Kdenlive suggested. | For 4 GB RAM or old PCs: no compositor, no animations, minimal tray, aggressive earlyoom. |
| Icon | `user-home` | `applications-games` | `x-office-document` | `applications-graphics` | `battery-good` |
| RAM hint | Idle target 350–500 MB | Idle target 350–500 MB, more while gaming | Idle target 350–500 MB | Idle target 350–500 MB | Idle ≈ 300–380 MB (no compositor) |
| CPU governor | `schedutil` | `performance` | `schedutil` | `schedutil` | `schedutil` |
| Compositor | picom | picom | picom | picom | **none** (xfwm4 compositing off, no animations) |
| zram | 50 % of RAM | 75 % | 50 % | 50 % | 100 % |
| earlyoom `-m` | 4 % | 4 % | 4 % | 4 % | **8 %** |
| ananicy-cpp | off | **on** | off | off | off |
| sched_ext scheduler (`sched=`, needs [lindos-kernel](KERNEL.md)) | `none` | **`scx_lavd`** (low-latency) | `none` | **`scx_bpfland`** | `none` |
| Transparent Huge Pages (`thp=`) | `madvise` | `madvise` | `madvise` | `madvise` | `madvise` |
| MGLRU (`mglru=`) | default | default | default | default | **`on`** |
| Game Mode auto (`gamemode_auto`) | on | on | off | off | off |
| MangoHud default | off (Shift_R+F12 toggles) | off (Shift_R+F12 toggles) | off | off | off |
| apt packages (installed by the installer for every Mode; a switch installs any still missing, if online) | — | `gamemode mangohud steam-devices mesa-vulkan-drivers libvulkan1 vulkan-tools ananicy-cpp lutris` | `libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 thunderbird power-profiles-daemon redshift-gtk hunspell-en-us` | `winetricks cabextract icoutils fonts-liberation colord` | — |
| Flatpaks (installed by the installer for every Mode, best effort; a switch installs any still missing, if online) | — | `org.prismlauncher.PrismLauncher`, `org.vinegarhq.Sober`, `com.heroicgameslauncher.hgl` | — (suggested: `org.onlyoffice.desktopeditors`) | `com.usebottles.bottles` | — |
| services enabled | `earlyoom`, `fstrim.timer` | `ananicy-cpp`, `earlyoom` | `power-profiles-daemon`, `earlyoom`, `fstrim.timer` | `colord`, `earlyoom` | `earlyoom` |
| services disabled | — | `ModemManager`, `cups-browsed` | — | — | `bluetooth cups-browsed ModemManager avahi-daemon NetworkManager-wait-online colord switcheroo-control geoclue packagekit` (tune.d wins: `bluetooth.service ModemManager.service cups-browsed.service`) — **the only mode that turns Bluetooth off**; every other mode keeps `bluetooth.service` enabled (SPEC §8) |
| sysctl (`/etc/sysctl.d/90-lindos-mode.conf`) | — | `vm.max_map_count=2147483642`, `vm.compaction_proactiveness=0`, `kernel.split_lock_mitigate=0` | `vm.dirty_writeback_centisecs=1500` | `fs.inotify.max_user_watches=524288` | `vm.dirty_ratio=10`, `vm.dirty_background_ratio=5`, `kernel.nmi_watchdog=0` |
| Taskbar pins (in order) | File Explorer · Firefox · Store · Settings · Terminal | File Explorer · Firefox · Steam · Lutris · Heroic · Minecraft (Prism) · Roblox (Sober) · Settings | File Explorer · Firefox · LibreOffice Writer · LibreOffice Calc · Thunderbird · Settings · Terminal | File Explorer · Firefox · Bottles · GIMP · Krita · Kdenlive · Settings | File Explorer · Firefox · Settings |
| Extras | — | `gaming_items: steam lutris heroic prism sober` (all installed by the installer) | `power_profile: balanced`, `night_light: true` (redshift-gtk) | `compat_items: wine winetricks bottles`; suggested apt: `gimp krita kdenlive inkscape blender obs-studio`; colour-profile hint | `minimal_tray`, `animations: false` |

> `vm.max_map_count=2147483642` is shipped **always-on** by lindos-gaming's
> `/etc/sysctl.d/80-lindos-gaming.conf`; only Gaming mode repeats it in the mode drop-in, the
> other modes deliberately do not set it (a lower value there would sort after and clamp it).

> **The `sched=`, `thp=` and `mglru=` keys need the [Lindos kernel](KERNEL.md)** (sched_ext /
> `CONFIG_SCHED_CLASS_EXT`). On the stock kernel — or when a scheduler binary is missing — the
> switch **degrades to a logged skip, never an error**: `lindos-tune sched set` prints *"running
> kernel has no sched_ext — install lindos-kernel"* and the mode still applies everything else.
> `lindos-tune apply --mode` applies `sched`/`thp`/`mglru`, and `lindos-tune status` reports the
> active scheduler and THP mode.

Pins are `.desktop` ids (`lindos-files.desktop`, `firefox.desktop`, `lindos-store.desktop`,
`lindos-settings.desktop`, `lindos-terminal.desktop`, `steam.desktop`, `net.lutris.Lutris.desktop`,
`heroic.desktop`, `lindos-minecraft.desktop` (Prism Launcher shim), `lindos-roblox.desktop`,
`libreoffice-writer.desktop`, `libreoffice-calc.desktop`, `thunderbird.desktop`,
`com.usebottles.bottles.desktop`, `gimp.desktop`, `org.kde.krita.desktop`,
`org.kde.kdenlive.desktop`). The Whisker menu favourites are the same list, and
`lindos-desktop`'s per-mode `docklike-2.rc` / `whiskermenu-1.rc` carry the identical ids
(`tests/test_integration.py` enforces it). Pins whose application is not installed are simply not
shown by docklike until you install it.

Honesty notes carried in the mode files themselves:
* Gaming: *Valorant (Vanguard) and Fortnite (EAC-Linux disabled by Epic) do not run on any
  Linux, Lindos included. Roblox runs through Sober, not the Windows client. Check protondb.com
  and areweanticheatyet.com for Steam titles.*
* Creator: *Photoshop/Illustrator CC 2019–2021 work through the shipped Wine recipes; newer
  Creative Cloud releases are unreliable (partial). Premiere and AutoCAD do not work — Kdenlive,
  DaVinci Resolve and FreeCAD are the native alternatives.*

## 2. What happens on `lindos-mode set <id>`

```
lindos-mode list [--json]                 all modes with a one-liner
lindos-mode get [--json]                  the effective mode id (user config → /etc/lindos/system.json → everyday)
lindos-mode show <id> [--json]            the definition + the exact helper plan
lindos-mode set <id> [--system] [--no-install] [--dry-run] [--json] [--quiet]
```

1. `~/.config/lindos/config.json` gets `"mode": "<id>"` (`--system` also writes
   `/etc/lindos/system.json` through the helper action `write-system-config`).
2. **User part** (no password): panel profile — `xfce4-panel-profiles load
   /usr/share/lindos/modes/<id>/panel.tar.bz2` when the tool and tarball exist, else the mode's
   `panel/xfce4-panel.xml` is copied into `~/.config/xfce4/xfconf/xfce-perchannel-xml/` and the
   `*.rc` files into `~/.config/xfce4/panel/`; xfconf tweaks; `lindos-compositor start|stop`
   according to `compositor`; the mode's `apply-user.sh` (guarded xfconf / animations /
   night-light / `lindos-mangohud sync`).
3. **Privileged part** — one `pkexec` prompt for the helper action `apply-mode` with the plan
   from `lindos.modes.build_system_plan()`: apt packages and Flatpaks that are still missing (only when
   online — offline they are skipped and logged; skipped altogether with `--no-install` / `install: false`,
   which is what Lindos Setup sends), then `lindos-tune apply --mode <id> --system` (services,
   `/etc/sysctl.d/90-lindos-mode.conf`, governor + EPP, zram, earlyoom, ananicy, presets), then
   the mode's `apply-system.sh` if present, then `system.json`.
4. Every step is reported as ok / failed / skipped; `--dry-run` prints them without touching
   anything. The whole thing is idempotent — running it twice is harmless.

Steps that depend on another package (panel profiles from lindos-desktop, `lindos-compositor`,
`lindos-tune`, `lindos-mangohud`) are recorded as *skipped* when that package is not installed.

## 3. Files

| Path | Owner | Purpose |
|---|---|---|
| `/usr/share/lindos/modes/<id>/mode.json` | lindos-core | definition (SPEC keys `id name description icon packages flatpaks services_disable services_enable sysctl governor pins compositor zram_percent` + informational extras `tagline ram_hint gamemode_auto mangohud animations ananicy notes gaming_items compat_items suggested_packages suggested_flatpaks power_profile night_light earlyoom_min_percent minimal_tray colour_profile_hint`) |
| `/usr/share/lindos/modes/<id>/apply-user.sh` | lindos-core | runs as the user after the switch (env `LINDOS_MODE`, `HOME`) |
| `/usr/share/lindos/modes/<id>/panel/{xfce4-panel.xml,whiskermenu-1.rc,docklike-2.rc}` | lindos-desktop | panel layout + pins; `panel.tar.bz2` is generated next to it at build time / postinst by `/usr/libexec/lindos/build-panel-profiles.sh` |
| `/etc/lindos/tune.d/<id>.conf` | lindos-tune (conffile) | `ZRAM_PERCENT GOVERNOR EARLYOOM_MIN COMPOSITOR ANANICY` (+ optional `EARLYOOM_SWAP_MIN SWAPPINESS SERVICES_DISABLE SERVICES_ENABLE` and the [kernel](KERNEL.md) keys `sched= thp= mglru=`); precedence: defaults < mode.json < tune.d — edit this to tweak a mode without touching the package data |
| `/etc/sysctl.d/90-lindos-mode.conf` | written by lindos-tune / helper | the mode's sysctl block |
| `/etc/lindos/system.json` | lindos-core | system default mode (used for new users) |
| `~/.config/lindos/config.json` | user | your mode; overrides the system default |
| `~/.config/lindos/panel-init.done` | user | stamp of `first-login-panel.sh` (seeds the mode's panel files once at first login) |

## 4. Choosing

* **4 GB of RAM or less, or a pre-2012 machine →** Lite (the wizard suggests it automatically).
* **You play games →** Gaming (performance governor, ananicy, zram 75 %; the launchers are pinned —
  the installer already installed them).
* **Office/mail all day →** Work (`power-profiles-daemon` balanced, night light).
* **Photoshop CS6/2021, Illustrator 2021, drawing/video →** Creator (Bottles + recipes; note the
  Adobe caveats above).
* **Everything else →** Everyday.

Switching back and forth is free: nothing is uninstalled when you leave a mode; only pins,
services, sysctl, governor, zram and compositor change.
