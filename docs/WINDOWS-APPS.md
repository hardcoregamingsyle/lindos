# Windows programs on Lindos

> **What "runs Windows programs" means here.** Lindos runs `.exe` / `.msi` programs through
> **Wine** and **Proton** — translation layers that implement the Windows API on Linux, without a
> virtual machine, at near-native speed. It is *not* Windows: programs that install Windows kernel
> drivers (anti-cheat, some DRM, hardware tools), programs that need Windows-only services
> (Microsoft 365 Click-to-Run, Creative Cloud) and recent Adobe releases do not work. The recipe
> table below says which is which; when a program is *broken* the recipe names native
> alternatives.

## 1. Double-click and go: `lindos-run`

Any `.exe`, `.msi`, `.bat`/`.cmd` or `.lnk` opens with **Windows App Runner (Lindos)**
(`/usr/share/applications/lindos-run.desktop`, MIME `application/x-ms-dos-executable
application/x-msdownload application/x-msi application/x-ms-shortcut application/x-bat` merged
into `/etc/xdg/mimeapps.list` at install). Thunar's right-click menu also gets **Run with Lindos
(Windows app)**, **Run with Proton (game)** and **Open C:\ drive**.

```
lindos-run <file.exe|.msi|.bat|.lnk> [args…] [--runner umu|wine|bottles] [--prefix NAME]
           [--new-prefix] [--shared] [--gamemode] [--mangohud] [--gamescope [WxH]] [--hdr] [--fsr]
           [--dxvk-async|--no-dxvk-async] [--info] [--dry-run] [-v]
```

| Flag | Meaning |
|---|---|
| `--runner umu\|wine\|bottles` | force a runner (only honoured when that runner is installed) |
| `--prefix NAME` | use this C:\ drive (`~/.local/share/lindos/prefixes/NAME`) |
| `--new-prefix` | start from a fresh C:\ drive |
| `--shared` | use the shared `default` drive (small utilities) |
| `--gamemode` | wrap in `gamemoderun` (Feral GameMode) — automatic for games when the config key `gamemode_auto` is on |
| `--mangohud` | show the MangoHud overlay |
| `--gamescope [WxH]` | run inside `lindos-gamescope` (gamescope) at an optional resolution; degrades with a message if gamescope is absent |
| `--hdr` | ask gamescope for HDR output (needs a supported display + compositor) |
| `--fsr` | enable gamescope AMD FSR upscaling |
| `--dxvk-async` / `--no-dxvk-async` | force `DXVK_ASYNC` on/off (default respects an existing env value or the per-title profile) |
| `--info` | print what `lindos.compat.analyze_exe` sees (JSON: kind, arch, installer type, product, company, SHA-256 prefix, suggested prefix/runner, log path) **and the resolved performance env** and exit |
| `--dry-run` | print the launch plan (argv, env, cwd, log) as JSON without running |

What happens:

1. **Analyse** — minimal PE parse: 32/64-bit, installer type (NSIS, Inno, InstallShield, MSI, WiX,
   Squirrel), product/company from version info, kind `installer` / `app` / `game` / `msi`.
2. **Choose the runner** (`lindos_compat.runner.choose_runner`): `--runner` wins if available →
   a recipe asking for Bottles wins if Bottles is installed → a prefix already created by a
   runner keeps it → **games and unknown → Proton-GE via `umu-run`** (DXVK/VKD3D built in) if
   umu is installed, else Wine → **installers, apps, `.msi` → Wine** (falls back to umu when Wine
   is missing).
3. **Prefix** ("C:\ drive"): per program `~/.local/share/lindos/prefixes/<slug>` (64-bit
   default; recipes may ask for 32-bit); `WINEDLLOVERRIDES=winemenubuilder.exe=d` (no `.desktop`
   spam), `WINEDEBUG=-all`, DXVK/`PROTON_*` env for umu, `gamemoderun` / `mangohud` wrappers.
   `~/Windows Apps/<Name>/C:` is a symlink to the prefix's `drive_c` so you can find your files
   from File Explorer.
4. **Installer flow** — after an installer exits, the prefix is scanned (`drive_c/Program
   Files*/`, Start Menu, `~/.local/share/applications/wine/`) for new `.lnk`/`.exe`; each new
   program gets a Start-menu entry `~/.local/share/applications/lindos-<slug>.desktop`
   (`Exec=lindos-run --prefix <prefix> "<exe>"`, icon extracted with `wrestool`/`icotool` when
   `icoutils` is present, else the generic `lindos-exe` icon) and a record in
   `~/.local/share/lindos/apps.json` (`{slug: {name, exe, prefix, runner, installed_at, kind,
   …}}`). Lindos Settings → Windows apps lists exactly that database.
5. **Feedback** — when started from the file manager (no terminal) `zenity`/`yad` show
   "Preparing Windows compatibility…" and errors with the copyable log path
   `~/.local/state/lindos/run-<slug>.log`. `LINDOS_NO_GUI=1` disables the dialogs.

Exit codes 0 / 1 / 2. If Wine or umu is missing, `lindos-run` says so and points to
`pkexec /usr/libexec/lindos/install-compat.sh` and to Settings → Windows apps → Doctor. The same
page has a **Windows app support (Wine + Proton) → Install / repair** button that runs the helper
action `install-compat` (`wine umu`) — use it after an offline first boot.

### 1.1 Performance environment (games and Proton)

When the runner is `umu`/Proton or the file's kind is `game`, `lindos-run` sets a small,
documented set of performance variables. Each is set **only if it is not already in the
environment**, so a user override or a per-title profile always wins. `--info` prints the resolved
set.

| Variable | Value | Condition |
|---|---|---|
| `PROTON_USE_NTSYNC` | `1` | **only if `/dev/ntsync` exists** (the [Lindos kernel](KERNEL.md)); otherwise unset, so Proton falls back to fsync silently |
| `DXVK_ASYNC` | `1` | on by default; `--no-dxvk-async` or an existing value turns it off; a per-title profile can force either way |
| `PROTON_HIDE_NVIDIA_GPU` | `0` | so Proton sees the NVIDIA GPU |
| `DXVK_HUD` / `MANGOHUD` | per flags | `--mangohud` / MangoHud config |

`WINEFSYNC` / `WINEESYNC` are left to umu, and `PROTON_ENABLE_WAYLAND` is left untouched. There is
**no** behavioural change for the `wine` (app/installer) runners — this env applies to games only.
A per-title **profile** (`/usr/share/lindos/gaming/profiles/*.json`, user overrides in
`~/.config/lindos/gaming/profiles/`, resolved by exe name / Steam AppID before launch) can supply
the runner, Proton build, extra env, gamescope size, FSR/HDR, DXVK-async and MangoHud. See
[GAMING.md](GAMING.md) §6.

## 2. `lindos-compat`

```
lindos-compat doctor [--json] [--ascii]
lindos-compat prefixes list [--json] [--size] | remove <slug> [-y] [--keep-apps] | open <slug|file> | winecfg <slug>
lindos-compat recipes list [--json] [--status works|partial|broken] | show <id> [--json]
                     | apply <id> [--prefix SLUG] [--fresh] [--dry-run] [--json]
lindos-compat install-umu [--system] [--version TAG] [--no-verify] [--force] [--dry-run] [--json]
lindos-compat install-bottles [--system|--user] [--dry-run] [--json]
lindos-compat install-dxvk [--prefix SLUG] [--tag TAG] [--dry-run] [--json]
lindos-compat install-vkd3d [--prefix SLUG] [--tag TAG] [--dry-run] [--json]
lindos-compat proton <args…>              (delegates to lindos-proton: list | update | remove <tag>)
```

* **doctor** checks, and prints the fix command for each failure: `wine`, `wine32`, `winetricks`,
  `cabextract`, `winbind`, `umu`, `proton-ge`, `vulkan64`, `vulkan32`, `vulkan-tools`, `fonts`,
  `corefonts`, `icoutils`, `zenity`, `gamemode`, `mangohud`, `bottles`, `max-map-count`,
  `prefixes-dir`, `display`, `core` (the `lindos` Python module) — plus the performance checks
  `ntsync` (`/dev/ntsync`, the [Lindos kernel](KERNEL.md)), `sched_ext`, `gamescope` and
  `dxvk`/`vkd3d` presence. Exit 0 when every *required* check passes, 1 otherwise.
* **prefixes** = your C:\ drives with the programs registered in each; `remove` deletes the
  drive and its Start-menu entries (`--keep-apps` keeps the records); `open` opens `drive_c` in
  Thunar; `winecfg` opens Wine's configuration for that drive.
* **recipes** — see §3; `apply` prepares a drive (winetricks verbs, DLL overrides, registry,
  env) and refuses `broken` recipes (it prints the alternatives instead).
* **install-umu** downloads the pinned umu-launcher release (1.4.4, SHA-256 verified) — per user
  by default, `--system` to `/usr/local/bin` through the helper. **install-bottles** installs
  the Flatpak `com.usebottles.bottles` from Flathub.
* **install-dxvk** / **install-vkd3d** install or refresh DXVK and VKD3D-Proton into a Wine
  prefix from pinned GitHub release tarballs (version pins in
  `/usr/share/lindos/compat/components.json`). Proton already bundles both, so these are for
  **plain Wine** prefixes; the default `--prefix` is the shared drive.

## 3. Recipes (`/usr/share/lindos/recipes/<id>.json`)

Recipes are ready-made setups for well-known programs. Status is honest by contract:
**works** · **partial** (runs with limits) · **broken** (does not work — alternatives named,
`apply` refuses). Run `lindos-compat recipes show <id>` for the full notes, installer hint and
homepage.

| id | Program | Category | Status | Runner / arch | Summary | Native / other alternatives |
|---|---|---|---|---|---|---|
| `7zip` | 7-Zip | utility | **works** | wine / win64 | The 7-Zip File Manager runs through Wine; Lindos already extracts 7z/zip/rar natively. | Archive Manager, 7-Zip for Linux (7zz), PeaZip |
| `autocad` | Autodesk AutoCAD (2016–2025) | engineering | **broken** | wine / win64 | Does NOT work: installer, licensing/sign-in service and the DirectX 11 canvas all fail. | AutoCAD Web, FreeCAD, LibreCAD / QCAD, BricsCAD (Linux build) |
| `epic-games-launcher` | Epic Games Launcher (Windows client) | gaming | **broken** | umu / win64 | The Windows Epic launcher is not supported; your Epic games work through Heroic (native launcher + Proton). Fortnite stays impossible (EAC-Linux disabled by Epic). | Heroic Games Launcher, Legendary (CLI), Lutris |
| `foobar2000` | foobar2000 | media | **works** | wine / win64 | Playback, library, tagging, converter and most components work through Wine. | DeaDBeeF, Strawberry, Quod Libet |
| `illustrator-cc-2021` | Adobe Illustrator CC 2021 (v25) | creator | **partial** | wine / win64 | Vector editing works with GPU preview off; sign-in, cloud documents and some panels do not; 2022+ fail. | Inkscape, Krita, Figma / Photopea (web) |
| `notepad-plus-plus` | Notepad++ | utility | **works** | wine / win64 | Installs and runs perfectly, incl. plugins, themes and "Open with". | Mousepad, Geany, VS Code / VSCodium |
| `office-2016` | Microsoft Office 2016 (Word, Excel, PowerPoint) | office | **works** | wine / **win32** | 32-bit offline installer works; activate with a product key, not a Microsoft account. | LibreOffice, OnlyOffice Desktop Editors, Office for the web |
| `office-365` | Microsoft 365 / Office 365 (Click-to-Run) | office | **broken** | wine / win64 | Does NOT install: Click-to-Run and Microsoft-account sign-in need Windows components Wine lacks. | LibreOffice, OnlyOffice, Office for the web, Office 2016 offline installer |
| `paint-net` | Paint.NET (4.2.16 classic) | creator | **partial** | wine / win64 | 4.2.16 works with .NET 4.8 from winetricks (long install); Paint.NET 5.x does not. | Pinta, GIMP, Krita |
| `photoshop-cc-2021` | Adobe Photoshop CC 2021 (v22) | creator | **partial** | wine / win64 | Everyday editing works; GPU features, Neural Filters and Creative Cloud sign-in do not; 2022+ crash. | GIMP, Krita, Photopea |
| `photoshop-cs6` | Adobe Photoshop CS6 (13.x) | creator | **works** | wine / win64 | Runs well for photo editing; keep GPU acceleration off; install with a serial, not an Adobe ID trial. | GIMP, Krita, Photopea |
| `premiere` | Adobe Premiere Pro (CC 2019–2024) | creator | **broken** | wine / win64 | Does NOT work: installer fails and the editor needs GPU/media features Wine cannot provide. | Kdenlive, DaVinci Resolve, Shotcut |
| `riot-client` | Riot Client (Valorant, League of Legends, TFT) | gaming | **broken** | wine / win64 | **Not possible on any Linux**: Riot Vanguard is a Windows-only kernel anti-cheat. | (none) — Counter-Strike 2 / Dota 2 native, GeForce NOW (cloud) |
| `roblox-player` | Roblox Player (Windows client) | gaming | **broken** | wine / win64 | The Windows client is blocked on Wine by Roblox's Hyperion anti-cheat; **play Roblox through Sober instead (works)** — `lindos-game install sober`. | Sober (Roblox for Linux), Roblox in Lindos Settings |
| `winrar` | WinRAR | utility | **works** | wine / win64 | Runs through Wine (trial nag included); Lindos also opens RAR archives natively. | Archive Manager, rar for Linux, PeaZip |

Recipe JSON schema: `{id, name, vendor, category, status, notes, runner (wine|umu|bottles),
arch (win32|win64), winetricks[], dll_overrides{}, env{}, post_cmds[], registry[{key,name,type,
value}], alternatives[{name,how}], summary, homepage, installer_hint}`; `id` must equal the file
name; `broken` recipes must carry `alternatives`. `LINDOS_RECIPES_DIR` overrides the directory.

Adobe, in one paragraph (SPEC §0.1): CS6 works; the 2019–2021 Photoshop / Illustrator releases
work for everyday editing but without Creative Cloud sign-in, cloud documents, Adobe Fonts or GPU
features (**partial**); 2022 and newer releases, Premiere Pro, and anything that needs the
Creative Cloud desktop app do not work (**broken**). You need standalone/offline installers and
a licence you already own.

## 4. Installing the compatibility layer

The OOBE ticks **Windows app support (Wine + Proton)** by default; it runs the helper action
`install-compat` with `{items: [wine, umu]}` → `/usr/libexec/lindos/install-compat.sh wine umu`.
On the ISO itself (`INCLUDE_WINE=1`) Ubuntu's `wine` + `wine32:i386`, winetricks and cabextract
are already present; WineHQ *staging* and umu-launcher come from the network at setup time
(the WineHQ apt source is pre-configured).

```
install-compat.sh [--minimal] [--from-chroot] [--dry-run] [--no-update] [--list] [items…]
  items: all wine wine-staging umu umu-launcher winetricks bottles dxvk vkd3d fonts dependencies proton
         (default all = wine winetricks dependencies fonts umu)
```

Root only (`pkexec`), exit 0 ok / 1 an item failed / 2 usage or not root / 3 offline; log
`/var/log/lindos/install-compat.log`. `ACCEPT_MSCOREFONTS_EULA=1` adds `ttf-mscorefonts-installer`
(Microsoft's EULA must be accepted); recipes otherwise use per-prefix `winetricks corefonts`.
`dxvk`/`vkd3d` are no-ops (built into Proton; `winetricks dxvk` for plain Wine).

## 5. Where things are

| Path | Contents |
|---|---|
| `~/.local/share/lindos/prefixes/<slug>/` | one C:\ drive per program (`drive_c/`, `.lindos.json` marker with slug/runner/arch/created_at) |
| `~/.local/share/lindos/prefixes/default/` | the shared drive (`--shared`) |
| `~/.local/share/lindos/apps.json` | the Windows-apps database (Start-menu entries + Settings list) |
| `~/.local/share/applications/lindos-<slug>.desktop` | generated Start-menu entries (`Categories=Wine;X-Lindos;`) |
| `~/.local/share/icons/hicolor/<size>x<size>/apps/lindos-<slug>.png` | extracted icons |
| `~/Windows Apps/<Name>/C:` | symlink to the program's `drive_c` |
| `~/.local/state/lindos/run-<slug>.log` | per-launch log |
| `~/.local/share/umu/compatibilitytools/`, `~/.steam/root/compatibilitytools.d/` | GE-Proton builds (`lindos-proton`) shared between umu and Steam |
| `/usr/share/lindos/recipes/*.json` | recipes |
| `/usr/share/lindos/compat/components.json` | pinned DXVK / VKD3D-Proton / gamescope versions (`install-dxvk`/`install-vkd3d`) |
| `/usr/share/lindos/gaming/profiles/*.json` | per-title profiles (`~/.config/lindos/gaming/profiles/` overrides win) |
| `/usr/share/lindos/compat/README.md` | the package's own reference (schema, env knobs) |

Env knobs: `LINDOS_HOME`, `LINDOS_ROOT`, `LINDOS_RECIPES_DIR`, `LINDOS_NO_GUI=1`, `LINDOS_DEBUG=1`,
`PROTONPATH` (override the Proton build for umu), `GITHUB_TOKEN` (release API rate limit).

## 6. Games

Games are the job of Proton, not Wine: Steam ships its own Proton (enable *Steam Play for all
titles*), `lindos-run` uses GE-Proton through umu-launcher, Heroic/Lutris/Bottles have their own
Proton/Wine pickers. Whether a **multiplayer** game runs is decided by its anti-cheat vendor and
publisher — see [COMPATIBILITY.md](COMPATIBILITY.md) and [GAMING.md](GAMING.md).
