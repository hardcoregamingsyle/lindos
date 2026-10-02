# lindos-compat — technical notes

Windows-program support on Lindos (SPEC §9). Wine/Proton are *translation layers*: no
virtual machine, near-native speed — but it is not Windows. Anti-cheat titles (Valorant,
League of Legends, Fortnite) do not run on any Linux today, so they are not supported on
Lindos yet: that is up to their publishers, and no date is given. The Windows Roblox client
is blocked on Wine as well; Roblox itself works through Sober.

## Pieces

| Path | What |
|---|---|
| `/usr/bin/lindos-run` | double-click runner for `.exe` `.msi` `.bat` `.lnk` (thin; logic in `lindos_compat.cli_run`) |
| `/usr/bin/lindos-compat` | `doctor`, `prefixes`, `recipes`, `install-umu`, `install-bottles`, `proton` |
| `/usr/lib/lindos-compat/lindos_compat/` | importable package (stdlib only, importable on any OS) |
| `/usr/libexec/lindos/install-compat.sh` | root installer (WineHQ staging, winetricks, 32-bit libs, umu) — via helper action `install-compat` |
| `/usr/share/lindos/recipes/*.json` | honest recipes (`works` / `partial` / `broken` + alternatives) |
| `/usr/share/applications/lindos-run.desktop` | MIME handler (`NoDisplay=true`) |
| `/usr/share/lindos/mimeapps-lindos.list` | merged into `/etc/xdg/mimeapps.list` by postinst |
| `/usr/share/lindos/thunar-uca-lindos.xml` | Thunar actions merged into `/etc/xdg/Thunar/uca.xml` by postinst |
| `/usr/share/mime/packages/lindos-windows.xml` | "Windows program" MIME comments + globs |
| `/usr/share/icons/hicolor/scalable/apps/lindos-exe.svg` | generic icon for Windows programs |

Shared code lives in **lindos-core** (`lindos.compat`: `analyze_exe`, `ExeInfo`, `slugify`,
`apps_db_load/save`, `choose_runner`) — this package imports it, never duplicates it.

## `lindos-run` flow

1. `.lnk` → parsed with `lindos_compat.lnk` (LinkInfo `LocalBasePath` + `CommonPathSuffix`,
   `EnvironmentVariableDataBlock`, relative-path StringData) and mapped onto the C:\ drive
   case-insensitively; arguments/working dir come from the shortcut.
2. `lindos.compat.analyze_exe` → `ExeInfo` (`kind`: installer/app/game/msi/unknown, arch,
   installer type, product/company).
3. Runner (`lindos_compat.runner.choose_runner`): `--runner` wins if available → prefix
   marker (`.lindos.json`) keeps the runner it was created with → recipe `runner` (bottles
   only if the Flatpak is installed) → game/unknown → **umu** when `umu-run` is on PATH,
   installers/apps/msi → **wine**; each falls back to whatever is installed.
4. Prefix: `~/.local/share/lindos/prefixes/<slug>` (`--prefix NAME`, `--shared` = `default`,
   `--new-prefix` moves the old one aside). Wine: `WINEARCH=win64`,
   `WINEDLLOVERRIDES=winemenubuilder.exe=d`, `WINEDEBUG=-all`, `wineboot -u` on first use.
   umu: `GAMEID=umu-<slug>` (`umu-default` for the shared prefix), `PROTONPATH=<GE-Proton dir>`
   from `~/.local/share/umu/compatibilitytools` or `~/.steam/root/compatibilitytools.d`, else the
   literal `GE-Proton` (umu downloads it), `STORE=none`, `DXVK_ASYNC=1`,
   `PROTON_ENABLE_NVAPI=1` on NVIDIA. `gamemoderun` wrapper when `--gamemode` or
   (config `gamemode_auto` and kind == game); `mangohud` when `--mangohud` or config `mangohud`.
   `.msi` → `msiexec /i`, `.bat`/`.cmd` → `cmd /c`.
5. Run with `subprocess` (never a shell); output → `~/.local/state/lindos/run-<slug>.log`.
6. Post-run scan (`lindos_compat.scan`): snapshot before/after of `drive_c/Program Files*`,
   `users/*/AppData/Roaming/Microsoft/Windows/Start Menu`, `users/*/Start Menu`,
   `users/*/Desktop`, `ProgramData/.../Start Menu`, `~/.local/share/applications/wine/Programs`.
   New `.lnk` (preferred) / Wine `.desktop` / `.exe` → `~/.local/share/applications/lindos-<slug>.desktop`
   (`Exec=lindos-run --prefix <prefix> "<exe>"`, `Icon=lindos-<slug>` extracted with
   `wrestool`+`icotool` into `~/.local/share/icons/hicolor/<size>x<size>/apps/`, else
   `lindos-exe`, `Categories=Wine;X-Lindos;`, `StartupWMClass=<exe name>`) + record in
   `APPS_DB` `{slug:{name,exe,prefix,runner,installed_at,kind,...}}` via
   `lindos.compat.apps_db_save`, then `notify-send`.
   MSIX hand-off (`lindos_compat.handoff`, SPEC-WINDOWS §28.4a): before an installer runs, a Wine
   C:\ drive gets (once, marker `handoff`: 1) file associations for `.msix/.appx/...` and
   `ms-appinstaller:` that only record the path (or, for a link, just that one arrived) in `drive_c/ProgramData/Lindos/handoff.log`; when the
   installer exits, the queue and the new packages in Temp/Downloads go through the MSIX handler
   (the usual question) or ONE explanation. It never opens a file manager.
7. GUI feedback: when stdout is not a TTY and `DISPLAY` is set → `zenity --progress` /
   `--error` (or `yad`). Exit codes 0 / 1 / 2. `--info` prints the `ExeInfo` as JSON, `--dry-run`
   prints the launch plan.

`~/Windows Apps/<Name>/C:` → symlink to the prefix's `drive_c` (created lazily after a run).

## Recipes

`{id,name,vendor,category,status,notes,runner,arch,winetricks,dll_overrides,env,post_cmds,
registry,alternatives,summary,homepage,installer_hint,winget_id?}` — `id` == file name; `status ∈
works|partial|broken`; broken recipes **must** carry `alternatives` and `apply` refuses them.
`apply` = create prefix → `winetricks -q <verbs>` (with `WINEPREFIX`) → `wine reg add` for
`dll_overrides` (HKCU\Software\Wine\DllOverrides) and `registry` → `post_cmds` → env/overrides
stored in `APPS_DB` under `recipe-<id>` so `lindos-run --prefix <id>` picks them up.

## Doctor

Checks: lindos-core, wine (+version), 32-bit wine, winetricks, umu-run, Proton-GE, vulkan
tools, 64-bit/32-bit Vulkan (`dpkg-query -W libvulkan1:i386 mesa-vulkan-drivers:i386`), fonts
(fonts-liberation, corefonts), cabextract, winbind, gamemode, mangohud, flatpak+Bottles,
icoutils, zenity/yad, `PREFIXES_DIR` writable, `DISPLAY`, `vm.max_map_count`. Each line
prints ✓/✗ plus the fix command; `--json` for Lindos Settings.

## Installing the layer

`install-compat.sh [--minimal] [--from-chroot] [--dry-run] [--no-update] [items…]`:
`dpkg --add-architecture i386` → WineHQ key `/etc/apt/keyrings/winehq-archive.key` + `winehq-noble.sources`
→ `winehq-staging` (fallback `wine-staging`, then Ubuntu `wine`) → support packages
(winetricks cabextract winbind libvulkan1(:i386) mesa-vulkan-drivers(:i386) libgl1-mesa-dri:i386
fonts-liberation icoutils zenity) → `lindos-compat install-umu --system`. Exit 3 when offline.
`--minimal` = Ubuntu's `wine` only (ISO build with `INCLUDE_WINE=1`).

`lindos-compat install-umu` downloads the pinned umu-launcher release (deb pair on root+apt,
zipapp otherwise) to `/usr/local/bin/umu-run` (root) or `~/.local/bin/umu-run`, verifying
SHA-256; `install-bottles` = `flatpak install -y flathub com.usebottles.bottles`.

## Performance layer (SPEC-KERNEL §17)

`lindos_compat.perf` adds the documented Proton performance environment to game / umu
launches, **only when the value is not already set** (a user override always wins):
`PROTON_USE_NTSYNC=1` iff `/dev/ntsync` exists (`LINDOS_ROOT`-aware; otherwise Proton
falls back to fsync silently), `DXVK_ASYNC=1` (respect override; forced by
`--dxvk-async`/`--no-dxvk-async`), `PROTON_HIDE_NVIDIA_GPU=0`, and `DXVK_HDR=1` with
`--hdr`. `PROTON_ENABLE_WAYLAND` is left untouched; `WINEFSYNC`/`WINEESYNC` are handled
by umu. wine/app launches are unchanged. `lindos-run --info` prints the resolved perf env.

New `lindos-run` flags: `--gamescope [WxH]`, `--hdr`, `--fsr`, `--dxvk-async` /
`--no-dxvk-async`, `--appid <id>`. gamescope wrapping prefers `/usr/bin/lindos-gamescope`
(lindos-gaming), then the raw `gamescope` binary, else degrades with a warning.

**Per-title profiles** (`lindos_compat.profiles`, schema 1):
`/usr/share/lindos/gaming/profiles/*.json` overridden per-id by
`~/.config/lindos/gaming/profiles/*.json`, resolved by exe name / Steam appid. A profile
supplies runner / Proton / env / gamescope / dxvk_async / mangohud. A `not_possible`
profile (e.g. Valorant/Vanguard) only documents *why* and fakes nothing — `lindos-run`
refuses to launch it and never spoofs anti-cheat/attestation (SPEC-KERNEL §14).

**DXVK / VKD3D-Proton** into a plain-Wine C:\ drive:
`lindos-compat install-dxvk <slug>` / `install-vkd3d <slug>` read the pins in
`/usr/share/lindos/compat/components.json` (`{dxvk,vkd3d_proton,gamescope}`) and run
`/usr/libexec/lindos/install-into-prefix.sh` (download release tarball → copy DLLs into
system32/syswow64 → mark them native). Proton (umu) already ships both. `doctor` also
checks Vulkan, `/dev/ntsync`, sched_ext, gamescope and DXVK/VKD3D with exact fixes.

## Testing

`pytest packages/lindos-compat/tests` (works on Windows/macOS: `lindos.compat` is stubbed,
`which`/`run` are injected). Env knobs: `LINDOS_HOME`, `LINDOS_ROOT`, `LINDOS_RECIPES_DIR`,
`LINDOS_NO_GUI=1`, `LINDOS_DEBUG=1`, `LINDOS_COMPAT_LIB`.
