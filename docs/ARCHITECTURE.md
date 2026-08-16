# Lindos architecture

Lindos is eleven Debian packages on top of Linux Mint 22.x XFCE, plus a build pipeline. The
authoritative definitions of every name, path, format and API live in [`SPEC.md`](../SPEC.md),
[`SPEC-KERNEL.md`](../SPEC-KERNEL.md) (Addendum K — the kernel and compat-performance layer) and
[`SPEC-VM.md`](../SPEC-VM.md) (Addendum V — virtualization, WinApps, system Proton, drivers, CI);
this document explains how the pieces fit and where to look.

## 1. Packages

```
lindos-meta ─┬─ lindos-core      python3 module `lindos` + polkit helper + CLIs (no GTK)
             ├─ lindos-desktop   theme/panel/xfconf/shortcuts/picom/branding/greeter/plymouth
             ├─ lindos-tune      RAM/perf tune, zram, services, governor, fans, power, sched_ext, report
             ├─ lindos-compat    lindos-run / lindos-compat, recipes, DXVK/VKD3D, MIME + Thunar integration
             ├─ lindos-gaming    lindos-proton / -drivers / -game / -mangohud / -gamescope, profiles, compat matrix
             ├─ lindos-kernel    tuned kernel recipe + config fragment + boot integration + lindos-kernel CLI  (Recommends)
             ├─ lindos-setup     first-boot OOBE (GTK 3)
             └─ lindos-settings  settings centre (GTK 3)

lindos-vm        honest KVM/VFIO Windows VM (no spoofing) + optional GPU passthrough — lindos-vm CLI
lindos-winapps   seamless Windows apps (Adobe/Office) over RDP against a user-supplied Windows
```

`lindos-vm` and `lindos-winapps` (Addendum V) are **standalone** packages, not pulled in by
`lindos-meta`: they are opt-in, need a user-supplied licensed Windows, and are documented in
[VM.md](VM.md) and [WINAPPS.md](WINAPPS.md).

| Package | Depends (essentials) | Ships (highlights) |
|---|---|---|
| **lindos-core** | `python3 (>= 3.10)`, `policykit-1 \| polkitd`, `pkexec`, `xdg-utils`, `ca-certificates`, `curl \| wget`, `gpg` (Recommends `xfconf flatpak procps systemd lindos-tune`) | `/usr/lib/python3/dist-packages/lindos/{paths,config,modes,browsers,hardware,helper,theme,compat,ram}.py`; `/usr/libexec/lindos/lindos-helper`, `install-browser.sh`; `/usr/share/polkit-1/actions/org.lindos.helper.policy`; `/usr/share/lindos/modes/<id>/{mode.json,apply-user.sh}`; `/etc/lindos/system.json` (conffile); `/usr/bin/lindos-mode lindos-browser lindos-config lindos-ram` |
| **lindos-desktop** | `xfce4-panel xfce4-whiskermenu-plugin xfce4-docklike-plugin xfwm4 xfconf xfce4-settings xfce4-notifyd picom xfce4-panel-profiles xfce4-clipman-plugin xfce4-screenshooter xfce4-taskmanager lightdm slick-greeter plymouth fontconfig lindos-core python3` | `/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/*.xml` (panel, xfwm4, xsettings, shortcuts, desktop, thunar, notifyd, power-manager, session, keyboards), `/etc/xdg/xfce4/panel/{whiskermenu-1,docklike-2}.rc`, `/etc/xdg/picom-lindos.conf`, `/etc/xdg/gtk-3.0/settings.ini`, `/etc/xdg/autostart/{lindos-setup,lindos-picom,lindos-mode-apply-user}.desktop`, `/etc/fonts/conf.d/60-lindos-ui.conf`, `/etc/lightdm/slick-greeter.conf` + `lightdm.conf.d/50-lindos.conf`, `/etc/lindos-release`, `/usr/bin/lindos-compositor`, `/usr/libexec/lindos/{apply-branding,build-panel-profiles,first-login-panel}.sh`, `panel-profile-pack.py`, `plymouth-gen-assets.py`, branded `.desktop` shims (`lindos-files`, `lindos-settings`, `lindos-store`, `lindos-terminal`), wallpapers, `lindos-logo.svg`, `lindos-start` / `lindos-settings` icons, `/usr/share/lindos/gtk-3.0/lindos.css`, `/usr/share/lindos/os-release.d/lindos.conf`, per-mode `/usr/share/lindos/modes/<id>/panel/`, plymouth theme `lindos`, xfce4-notifyd theme `Lindos` |
| **lindos-setup** | `python3 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 lindos-core xdg-utils` | `/usr/bin/lindos-setup` → `/usr/lib/lindos-setup/main.py`, `lindos_setup/{plan,core,pages,app,widgets,i18n}.py`, `ui/oobe.css`, `/usr/share/lindos/setup/{apps.json,accents.json}`, menu entry `lindos-setup.desktop` (`--reconfigure`) |
| **lindos-settings** | `python3 python3-gi gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 lindos-core xfce4-settings xfconf xdg-utils` | `/usr/bin/lindos-settings` → `/usr/lib/lindos-settings/main.py`, `lindos_settings/{model,backend,widgets,sidebar,app,power_menu}.py` + `pages/*.py`, `ui/settings.css`, `/usr/share/lindos/settings/pages.json`, `lindos-power-menu.desktop` (NoDisplay) |
| **lindos-compat** | `python3 (>= 3.10) lindos-core cabextract winbind xdg-utils desktop-file-utils shared-mime-info` (Recommends `winehq-staging \| wine-staging \| wine`, `winetricks`, `umu-launcher`, `icoutils`, `zenity`, `gamemode`, `mangohud`, Vulkan libs, `fonts-liberation`, `libnotify-bin`) | `/usr/bin/lindos-run`, `/usr/bin/lindos-compat`, `/usr/lib/lindos-compat/lindos_compat/*.py`, `/usr/libexec/lindos/install-compat.sh`, `/usr/share/lindos/recipes/*.json` (15), `lindos-run.desktop`, `lindos-exe.svg`, `mimeapps-lindos.list`, `thunar-uca-lindos.xml`, `mime/packages/lindos-windows.xml` |
| **lindos-gaming** | `lindos-core gamemode mangohud steam-devices python3 curl \| wget flatpak udev procps` (Recommends launchers, `antimicrox goverlay piper corectrl openrgb`, Vulkan, `ubuntu-drivers-common`, …) | `/usr/bin/lindos-proton lindos-drivers lindos-game lindos-mangohud`, `/usr/libexec/lindos/{install-gaming,gamemode-start,gamemode-end,install-xpadneo,install-xone}.sh`, `/etc/gamemode.ini`, `/etc/xdg/MangoHud/MangoHud.conf`, `/etc/udev/rules.d/60-lindos-controllers.rules`, `/etc/sysctl.d/80-lindos-gaming.conf`, `lindos-roblox.desktop`, `lindos-roblox-studio.desktop`, `lindos-minecraft.desktop`, `/usr/share/lindos/compat-matrix.json`, `/usr/share/lindos/gaming/launchers.json` |
| **lindos-tune** | `python3 (>= 3.10) lindos-core systemd procps util-linux zram-tools \| systemd-zram-generator earlyoom lm-sensors` (Recommends `power-profiles-daemon ananicy-cpp fancontrol hdparm pciutils kmod scx-scheds`) | `/usr/bin/lindos-tune` → `/usr/lib/lindos-tune/lindos_tune/*.py` (incl. `sched.py`), `/usr/lib/systemd/system-preset/90-lindos.preset`, `lindos-sensors-detect.service`, `/usr/libexec/lindos/{install-nbfc,sensors-detect-once}.sh`, `/usr/share/lindos/tune/{services-whitelist.txt,autostart-hide.list,ram-budget.json,earlyoom.default}`, `/etc/sysctl.d/70-lindos-base.conf`, `/etc/systemd/journald.conf.d/lindos.conf`, `/etc/tmpfiles.d/lindos.conf`, `/etc/lindos/tune.d/<mode>.conf` (incl. `sched= thp= mglru=`), `/etc/ananicy.d/lindos/*` |
| **lindos-kernel** | `python3, lindos-core` (Recommends `scx-scheds`; Suggests `lindos-tune`) | `/usr/bin/lindos-kernel` → `/usr/lib/lindos-kernel/lindos_kernel/{kconfig,features,grub,manifest,build}.py`, `/etc/default/grub.d/50-lindos.cfg` (conservative cmdline drop-in), `/usr/share/lindos/kernel/{manifest.json,lindos.config}`. The compiled kernel `.deb`s are build artifacts from `build/kernel/build-kernel.sh` (Linux host only) — **not** shipped in the package. See [KERNEL.md](KERNEL.md). |
| **lindos-meta** | the seven core packages at `= 1.0.0`; **Recommends** `lindos-kernel` (stock kernel stays as fallback) | nothing else |
| **lindos-vm** | `python3, lindos-core` (Recommends `qemu-system-x86 \| qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`, `ovmf`, `virt-manager`, `virtiofsd`; Suggests `looking-glass-client`) | `/usr/bin/lindos-vm` → `/usr/lib/lindos-vm/lindos_vm/{caps,plan,domain,passthrough}.py`, `/usr/share/lindos/vm/{win.xml.template,vfio.conf.template}` (honest, no spoof knobs), `/etc/libvirt/hooks/qemu` (single-GPU passthrough, conffile). `postinst` adds the user to `libvirt`/`kvm` **if present** (`getent`), never fails |
| **lindos-winapps** | `python3, lindos-core, freerdp3-x11 \| freerdp2-x11` (Recommends `lindos-vm \| libvirt-daemon-system`; Suggests `podman`) | `/usr/bin/lindos-winapps` → `lindos_winapps/{backend,apps,rdp}.py`, `/usr/share/lindos/winapps/apps.json` (catalog), `/usr/share/applications/lindos-winapps.desktop`. Config `~/.config/lindos/winapps/winapps.conf`; **never** stores the RDP password |

Every package: `Version: 1.0.0`, `Architecture: all`, `Maintainer: Lindos Team <team@lindos.dev>`,
`#!/bin/sh` + `set -e` idempotent maintainer scripts, `root/` copied verbatim by `build/mkdeb.sh`.

## 2. Paths (`lindos.paths`)

| Constant | Path | Notes |
|---|---|---|
| `SYSTEM_CONF_DIR` / `SYSTEM_CONF` | `/etc/lindos` / `/etc/lindos/system.json` | `{"mode":"everyday","browser":"firefox","oem":false}` (conffile of lindos-core) |
| `SHARE_DIR` / `MODES_DIR` / `RECIPES_DIR` | `/usr/share/lindos` / `…/modes` / `…/recipes` | |
| `LIBEXEC_DIR` / `HELPER` | `/usr/libexec/lindos` / `…/lindos-helper` | |
| `USER_CONF_DIR` / `USER_CONF` / `SETUP_DONE` | `~/.config/lindos` / `…/config.json` / `…/setup-done` | |
| `STATE_DIR` / `PREFIXES_DIR` / `APPS_DB` | `~/.local/share/lindos` / `…/prefixes` / `…/apps.json` | Wine prefixes ("C:\ drives") and the Windows-apps database |
| `LOG_DIR` | `~/.local/state/lindos` | `setup.log`, `settings.log`, `run-<slug>.log`, `gamemode.log`, `first-login-panel.log` |
| helper log | `/var/log/lindos/helper.log` (+ `install-compat.log`, `install-gaming.log`, `tune.log`) | |

`LINDOS_ROOT` prefixes every system path and `LINDOS_HOME` replaces `~` — this is how the whole
test suite runs on Windows/macOS against sandboxes. `lindos-config paths` prints the effective
values.

## 3. Configuration (`lindos.config`)

User config `~/.config/lindos/config.json`, atomic writes, defaults:

```json
{"mode": "everyday", "browser": "firefox", "theme": "dark", "accent": "#60CDFF",
 "wallpaper": "/usr/share/backgrounds/lindos/aurora-dark.svg", "setup_done": false,
 "gamemode_auto": true, "mangohud": false, "telemetry": false, "schema": 1}
```

System defaults `/etc/lindos/system.json`: `{"mode": "everyday", "browser": "firefox", "oem": false}`.
`effective_mode()` / `effective_browser()` = user override, else system, else default. CLI:
`lindos-config show [--system|--effective] | get <key> [--system] | set <key> <value> [--system]
| unset <key> | paths | reset --yes` (`--json` everywhere; `set --system` goes through the helper
action `write-system-config`).

## 4. The privileged helper (`lindos.helper` + `/usr/libexec/lindos/lindos-helper`)

Nothing in the desktop session runs as root directly. `lindos.helper.run_privileged(action,
payload, log=None) -> HelperResult(ok, out, err, code)` calls
`pkexec /usr/libexec/lindos/lindos-helper <action> '<json>'` (polkit action `org.lindos.helper`,
`auth_admin_keep` — one password prompt covers a burst of calls), falls back to `sudo -n`, and
runs directly when already root. The helper validates every payload against a whitelist
(`lindos.helper.validate_payload`) and never uses `shell=True`. Exit codes: 0 ok · 1 error /
not root · 2 usage or invalid payload; `run_privileged` adds 127 helper missing, 126
authentication failed, 124 timeout. `LINDOS_HELPER_DRYRUN=1` prints what would run without root.

| Action | Payload | What runs as root |
|---|---|---|
| `apply-mode` | `{mode, packages[], flatpaks[], services_disable[], services_enable[], sysctl{}, governor, zram_percent, compositor, apply_system, set_system_default, offline, schema}` (from `lindos.modes.build_system_plan`) | apt / flatpak installs (skipped offline) → `lindos-tune apply --mode <id> --system [--offline]` (fallback without lindos-tune: systemctl, sysctl drop-in, governor, zram) → mode `apply-system.sh` → `/etc/lindos/system.json` |
| `install-browser` | `{browser: edge\|chrome\|firefox}` | `/usr/libexec/lindos/install-browser.sh <id>` (vendor keyring + list + `apt-get install`; firefox = Mint's .deb) |
| `install-packages` | `{packages[]}` | `apt-get install -y -q` |
| `install-flatpaks` | `{flatpaks[]}` | `flatpak install -y flathub …` |
| `set-governor` | `{governor}` | writes `scaling_governor` (+ `lindos-tune governor <g>` for persistence when present) |
| `set-services` | `{enable[], disable[], mask[]}` (whitelisted units only) | `lindos-tune services enable|disable <unit>…` when lindos-tune is installed (so its conditional autostart entries such as blueman-applet follow the unit), else `systemctl enable/disable --now`; `mask` always via `systemctl mask --now` |
| `apply-sysctl` | `{sysctl{}}` | `/etc/sysctl.d/90-lindos-mode.conf` + `sysctl --system` |
| `apply-tune` | `{mode, offline}` | `lindos-tune apply --mode <id> --system [--offline]` |
| `set-zram` | `{percent}` | `lindos-tune zram N` |
| `install-compat` | `{items[]}` ⊂ `all wine wine-staging umu umu-launcher winetricks bottles dxvk vkd3d fonts dependencies proton` | `/usr/libexec/lindos/install-compat.sh <items…>` |
| `install-gaming` | `{items[]}` ⊂ `steam lutris heroic prism sober vinegar mcpelauncher bottles all` | `/usr/libexec/lindos/install-gaming.sh <items…>` |
| `install-drivers` | `{args[]}` ⊂ `--nvidia-open --nvidia-proprietary --amd --intel` (or `{driver}`) | `lindos-drivers install <args>` |
| `set-fan-profile` | `{profile}` | `lindos-tune fan set <profile>` |
| `set-sched` | `{profile}` ⊂ `scx_lavd scx_bpfland scx_flash scx_rustland none` | loads/stops the scx sched_ext scheduler via `scx_loader`/systemd (or the documented fallback) |
| `write-system-config` | `{mode?, browser?, oem?}` | atomic write of `/etc/lindos/system.json` |
| `enable-earlyoom` | `{enable}` | `systemctl enable/disable --now earlyoom` |

## 5. Modes (`lindos.modes`)

`/usr/share/lindos/modes/<id>/mode.json` (+ optional `apply-user.sh`, `apply-system.sh`,
`panel/` or `panel.tar.bz2` from lindos-desktop, `/etc/lindos/tune.d/<id>.conf` from
lindos-tune). `lindos-mode set <id> [--system]` = write config → `apply_mode()`: user part
(panel profile via `xfce4-panel-profiles load` or xml copy, xfconf, `lindos-compositor`,
`apply-user.sh`, `lindos-mangohud sync`) + one helper call `apply-mode` with the plan from
`build_system_plan()`. Idempotent, safe offline (installs skipped and logged), steps that need a
missing component are recorded as *skipped*. Details: [MODES.md](MODES.md).

## 6. Desktop session (lindos-desktop)

* Panel `panel-1`: bottom, 48 px, solid-colour background `#202020` @ 0.85 (`background-style=1`
  — xfce4-panel's colour style; `2` would be an image), plugin order separator(expand) ·
  whiskermenu (icon `lindos-start`, no title, `view-mode=0` = icon grid, 680×720, favourites =
  the mode's pins) · docklike (pins per mode, `Super+1..9` grabbed by the plugin itself) ·
  separator(expand) · systray · pulseaudio · power-manager · notification plugin · clock (two
  lines `%H:%M` / `%d/%m/%Y`) · showdesktop. `lindos.theme.set_taskbar_alignment` toggles the
  first separator's `expand`; `set_taskbar_position` moves the panel.
* xfwm4: theme `Lindos-Dark`, `button_layout=O|HMC`, `title_font=Selawik Bold 9`,
  `placement_mode=center`, `use_compositing=true` (Lite: off), `frame_opacity=100`,
  `show_frame_shadow`, `snap_to_windows`, `tile_on_move`, `wrap_workspaces=false`,
  `focus_delay=100`, `easy_click=Super`, 2 workspaces.
* picom: `/etc/xdg/picom-lindos.conf` (`backend=glx`, `vsync`, `corner-radius=8`, shadows, no
  blur, `unredir-if-possible` for fullscreen games), started by
  `/etc/xdg/autostart/lindos-picom.desktop` → `lindos-compositor start` (does nothing in Lite
  unless `--force`; turns xfwm4's own compositor off while picom runs).
  `lindos-compositor start [--force] | stop [--keep-xfwm|--no-xfwm] | status | toggle | restart`;
  `status` exits 0 running / 3 stopped. GameMode's start/end scripts call `stop`/`start`.
* Autostart: `lindos-setup.desktop` (`lindos-setup --first-run`, gate on `~/.config/lindos/
  setup-done`), `lindos-picom.desktop`, `lindos-mode-apply-user.desktop`
  (`/usr/libexec/lindos/first-login-panel.sh`, seeds the per-mode panel files once).
* Branding: `apply-branding.sh` seds `NAME`, `PRETTY_NAME`, `HOME_URL`, `LOGO`, `LINDOS_VERSION`,
  `LINDOS_CODENAME` into `/etc/os-release` and keeps `ID=linuxmint`, `ID_LIKE`,
  `VERSION_CODENAME`, `UBUNTU_CODENAME` so apt sources and Mint tooling keep working;
  `/etc/lindos-release` = `Lindos 1.0.0 (Aurora)`; `/etc/issue`; plymouth `lindos`; default
  wallpaper alternative → `aurora-dark.svg`.

## 7. Cross-component call map (SPEC §13)

| Caller | Calls |
|---|---|
| `lindos-setup` apply page | `lindos.config`, `lindos.modes.apply_mode`, `lindos.browsers.install/set_default`, `lindos.theme.*`; helper `write-system-config`, `apply-mode`, `install-browser`, `install-packages`, `install-flatpaks`, `install-compat`, `install-gaming` — one step each, at most once per plan |
| `lindos-settings` | everything in `lindos.*`; `lindos-run`, `lindos-proton`, `lindos-drivers`, `lindos-game`, `lindos-tune`, `lindos-compat`, `lindos-mode`, `lindos-compositor`, `lindos-mangohud`, `xfconf-query`, `xrandr` via subprocess; helper `install-packages`, `install-gaming`, `install-drivers`, `set-fan-profile`, `set-governor`, `set-services` |
| `lindos-mode set` | `lindos.modes.apply_mode` → helper `apply-mode` + `xfce4-panel-profiles load` (or xml copy) + `lindos-compositor` + `apply-user.sh` |
| helper `apply-mode` / `apply-tune` | `lindos-tune apply --mode <id> --system [--offline]` |
| helper `install-compat` | `/usr/libexec/lindos/install-compat.sh` (lindos-compat) |
| helper `install-gaming` | `/usr/libexec/lindos/install-gaming.sh <items…>` (lindos-gaming) |
| helper `install-browser` | `/usr/libexec/lindos/install-browser.sh <edge\|chrome\|firefox>` (lindos-core) |
| helper `install-drivers` | `lindos-drivers install …` (lindos-gaming) |
| helper `set-fan-profile` / `set-zram` / `set-sched` | `lindos-tune fan set <p>` / `lindos-tune zram N` / load-or-stop scx scheduler |
| `lindos-tune sched set` | helper `set-sched` → scx via `scx_loader`/systemd; needs `CONFIG_SCHED_CLASS_EXT` (else exit 3, "install lindos-kernel") |
| `lindos-tune apply --mode` | now also applies `sched` / `thp` / `mglru` from `tune.d/<id>.conf` |
| `lindos-kernel cmdline set\|reset\|--preset` | `grub.py` → `/etc/default/grub.d/50-lindos.cfg` (marker-fenced); prints `pkexec update-grub` |
| `lindos-kernel build` | `build/kernel/build-kernel.sh` (Linux host only; refuses on Windows) |
| `lindos-run` (game) | reads `/dev/ntsync`, resolves a per-title profile, sets the perf env, optional `lindos-gamescope` |
| build `35-kernel.sh` | `dpkg -i out/kernel/*.deb` if present + `update-grub`, else "no Lindos kernel built — using stock"; never fails the ISO build |
| `lindos-tune` (unprivileged) | never sudo: maps its op to the helper actions `apply-tune`, `set-zram`, `set-governor`, `set-services`, `set-fan-profile` (`LINDOS_TUNE_NO_ESCALATE=1` disables) |
| `lindos-ram` | `lindos-tune status [--json]` when installed, else `lindos.ram.report` |
| `lindos-run` | `lindos.compat.analyze_exe`, `lindos_compat.runner.choose_runner`, `umu-run` / `wine` / `bottles-cli`, `gamemoderun`, `mangohud`; `lindos-compat install-umu --system` → helper `install-compat {items:[umu]}` |
| `lindos-drivers install` (non-root) / `lindos-game install` (non-root) | helper `install-drivers` / `install-gaming` |
| gamemoded (`/etc/gamemode.ini` `[custom]`) | `/usr/libexec/lindos/gamemode-start.sh` → `lindos-compositor stop`; `gamemode-end.sh` → `lindos-compositor start` |
| build `40-theme.sh` | `out/assets/install-into-chroot.sh`, `apply-branding.sh`, `build-panel-profiles.sh` |
| build `50-tune.sh` | `lindos-tune apply --mode everyday --system --offline` |
| build `60-compat.sh` / `70-gaming.sh` | `install-compat.sh --minimal --from-chroot --no-update` / `install-gaming.sh --from-chroot steam lutris` |

## 8. Where things are logged

`~/.local/state/lindos/*.log` (per user), `/var/log/lindos/*.log` (helper, installers, tune),
`out/build.log` + `out/hooks/*.log` (build). `lindos-tune report` produces a Markdown bug report
(RAM, zram, top RSS, units, kernel, mode, GPU, config).

## 9. Testing

`tests/run.sh` (lint everything + pytest + generated-doc freshness); package tests under
`packages/*/tests` and `build/tests`, all runnable on Windows/macOS thanks to `LINDOS_ROOT` /
`LINDOS_HOME` sandboxes, `LINDOS_HELPER_DRYRUN=1`, `LINDOS_FORCE_OFFLINE=1`, injected runners and
the `gi` stub in `tests/lindos_testsupport.py`. See [CONTRIBUTING.md](../CONTRIBUTING.md).
