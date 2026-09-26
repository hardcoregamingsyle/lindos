# Lindos — Engineering Specification (v1.0 "Aurora")

> **This file is the contract.** Every package, script and document in this repository must
> follow the names, paths, formats and APIs defined here. If you need something that is not
> here, add it here first, then implement it.

## 0. What Lindos is (one paragraph)

Lindos is a remaster of **Linux Mint XFCE** (Ubuntu LTS base, currently Mint 22.x / Ubuntu 24.04
"noble") that gives Windows 11 users a familiar, beautiful, dark-by-default desktop; runs Windows
programs (`.exe`/`.msi`) through an integrated Wine/Proton compatibility layer; ships gaming
launchers and drivers pre-wired (Steam/Proton, Lutris, Heroic, Prism/Minecraft, Sober/Roblox);
and is tuned to idle at **~350–500 MB RAM** with no bloatware. At first login a Windows-style
OOBE ("Out-Of-Box Experience") lets the user pick a **Mode** (Everyday / Gaming / Work / Creator /
Lite) and a **browser** (Microsoft Edge / Google Chrome / Mozilla Firefox). Both can be changed
later in **Lindos Settings**.

### 0.1 Honesty rules (must be reflected everywhere, incl. UI and docs)
- "Runs Windows apps natively" means *without a VM*, through Wine / Proton (translation layer,
  near-native speed). Do not claim it is Windows.
- Anti-cheat reality: **Valorant (Vanguard) and Fortnite (EAC-Linux disabled by Epic) do NOT run
  on any Linux, including Lindos.** Say so plainly. Roblox runs through **Sober** (not the Windows
  client). Minecraft Java runs natively; Bedrock via `mcpelauncher` (unofficial). Steam games
  depend on the developer enabling anti-cheat for Proton — link to areweanticheatyet.com and
  protondb.com. Adobe: CC 2019–2021 era Photoshop/Illustrator work through Wine recipes; newer
  releases are unreliable — mark **partial**.
- Edge and Chrome licences forbid redistribution inside the ISO. Firefox is on the ISO;
  Edge/Chrome are downloaded from Microsoft/Google's official apt repositories during OOBE (with
  a clean offline fallback to Firefox + "install later" notice).
- RAM: idle target is measured as `free -m` "used" after login into XFCE with no apps open. State
  the target as **350–500 MB** and ship the measurement tool (`lindos-tune status`).

## 1. Repository layout

```
Lindos/
├── SPEC.md                     ← this contract
├── README.md                   ← product README (vision, features, honesty section, quickstart)
├── LICENSE                     ← GPL-3.0-or-later for Lindos code; THIRD_PARTY.md for others
├── THIRD_PARTY.md              ← licences of fetched assets (Fluent theme, icons, fonts, …)
├── Makefile                    ← `make debs`, `make iso`, `make test`, `make qemu`, `make clean`
├── build/                      ← ISO remaster + .deb builder (runs on Ubuntu/Debian host or Docker)
│   ├── config.env              ← BASE_ISO_URL, BASE_ISO_SHA256, LINDOS_VERSION, CODENAME, …
│   ├── build-iso.sh            ← main entry: fetch → unpack → chroot hooks → repack (EFI+BIOS)
│   ├── mkdeb.sh                ← builds packages/<name> into out/debs/<name>_<ver>_all.deb
│   ├── chroot/NN-*.sh          ← ordered hooks executed INSIDE the chroot (see §8)
│   ├── overlay/                ← files copied verbatim onto ISO root (grub.cfg, isolinux, …)
│   ├── Dockerfile              ← ubuntu:24.04 + xorriso squashfs-tools … ; `make docker-iso`
│   ├── test-qemu.sh            ← boot out/lindos-*.iso in QEMU (OVMF UEFI + legacy)
│   └── fetch-assets.sh         ← pinned git checkouts of Fluent theme/icons/cursors + fonts
├── packages/                   ← one dir per .deb; each has DEBIAN/ + root/ (filesystem tree)
│   ├── lindos-core/            ← python3 module `lindos` + polkit helper + CLIs (§4)
│   ├── lindos-desktop/         ← theme, panel, shortcuts, wallpapers, branding, greeter (§5)
│   ├── lindos-setup/           ← first-boot OOBE wizard (§6)
│   ├── lindos-settings/        ← Windows-11-style settings centre (§7)
│   ├── lindos-compat/          ← .exe/.msi runner, Wine/Proton/umu, Adobe recipes (§9)
│   ├── lindos-gaming/          ← launchers, drivers, gamemode, MangoHud, controllers (§10)
│   ├── lindos-tune/            ← RAM/perf tuning, zram, services, hardware control (§11)
│   ├── lindos-transfer/        ← Windows Easy Transfer-style migration + GUI (Addendum W §29)
│   └── lindos-meta/            ← depends on all of the above (incl. lindos-transfer)
├── docs/                       ← BUILDING, ARCHITECTURE, COMPATIBILITY, MODES, RAM-BUDGET,
│                                  KEYBOARD-SHORTCUTS, FAQ, HARDWARE-CONTROL, WINDOWS-APPS,
│                                  WINDOWS-FORMATS, WINGET, TRANSFER, DUALBOOT (Addendum W)
├── tests/                      ← run.sh (lint everything), pytest suites, conftest.py (gi stub)
├── .github/workflows/ci.yml    ← lint + pytest + build debs; ISO build = workflow_dispatch
└── out/                        ← build products (git-ignored)
```

Addendum K (kernel + compat-performance, §14), Addendum V (VM/WinApps/Proton-system/drivers/CI,
§15) and Addendum W (every Windows format/Transfer/Play-anywhere, §16) each add their own packages
(`lindos-kernel`; `lindos-vm`, `lindos-winapps`; `lindos-transfer`) and docs on top of this base
layout — see their own specs for the full file lists.

### 1.1 Package format
Each `packages/<name>/` contains:
- `DEBIAN/control` (Package, Version `1.0.0`, Section, Priority optional, Architecture `all`,
  Maintainer `Lindos Team <team@lindos.dev>`, Depends, Recommends, Description).
- optional `DEBIAN/postinst`, `DEBIAN/prerm`, `DEBIAN/postrm`, `DEBIAN/conffiles` (all `#!/bin/sh`,
  `set -e`, idempotent).
- `root/` — the filesystem tree copied as-is (`root/usr/bin/foo` → `/usr/bin/foo`).
- optional `tests/` (pytest).
`build/mkdeb.sh <name>` copies `root/` into a staging dir, fixes perms (dirs 755, files 644, anything
under `bin/`, `libexec/`, `sbin/`, `*.sh`, `DEBIAN/post*|pre*` → 755), then
`dpkg-deb --build --root-owner-group`.

## 2. Naming, branding, versions

| Item | Value |
|---|---|
| Product | **Lindos** |
| Version / codename | `1.0.0` / **Aurora** |
| Base | Linux Mint 22.x XFCE (default `22.2` "Zara", Ubuntu 24.04 `noble`) — set in `build/config.env` |
| `/etc/os-release` | keep `ID=linuxmint`, `ID_LIKE="ubuntu debian"`, `VERSION_CODENAME`/`UBUNTU_CODENAME` unchanged; set `NAME="Lindos"`, `PRETTY_NAME="Lindos 1.0 (Aurora)"`, `HOME_URL="https://lindos.dev"`, add `LINDOS_VERSION=1.0.0`, `LINDOS_CODENAME=Aurora`. Rationale: apt sources & Mint tooling keep working. |
| `/etc/lindos-release` | `Lindos 1.0.0 (Aurora)` |
| Logo | `/usr/share/pixmaps/lindos-logo.svg`; start-button icon name `lindos-start` (hicolor svg) |
| Accent colours | Dark accent `#60CDFF`, Light accent `#0067C0`, dark bg `#202020`, card `#2B2B2B`, light bg `#F3F3F3` |
| Default UI font | `Selawik 10` (Microsoft's OFL Segoe-metric font); fallbacks `Inter`, `Noto Sans`; mono `JetBrains Mono` if present else `DejaVu Sans Mono` |
| GTK theme names | `Lindos-Dark` (default), `Lindos-Light` (built from Fluent-gtk-theme with `--name Lindos --tweaks round`) |
| Icon theme | `Lindos` (Fluent-icon-theme installed with `-n Lindos`, dark panel variant `Lindos-dark`) |
| Cursor theme | `Fluent-dark-cursors` (dark) / `Fluent-cursors` (light) |
| xfwm4 theme | `Lindos-Dark` / `Lindos-Light` (Fluent xfwm themes renamed) |
| Wallpapers | `/usr/share/backgrounds/lindos/aurora-dark.svg` (default), `aurora-light.svg`, `bloom-blue.svg`, `mist-purple.svg`, `nightfall.svg` — original SVG art, ≥ 3840×2160 viewBox |
| Plymouth theme | `lindos` (`/usr/share/plymouth/themes/lindos/`) |
| Greeter | LightDM + slick-greeter, `/etc/lightdm/slick-greeter.conf` uses aurora-dark + Lindos theme |
| Maintainer | `Lindos Team <team@lindos.dev>` |

## 3. Modes

Five modes. Exactly these ids and display names:

| id | Name | Purpose | Key differences |
|---|---|---|---|
| `everyday` | Everyday | default; balanced | governor `schedutil`, picom on, all launchers available not pinned |
| `gaming` | Gaming | max FPS | governor `performance`, gamemode auto, MangoHud toggle, Steam/Lutris/Heroic/Prism/Sober pinned, zram 75 %, `vm.max_map_count=2147483642`, ananicy-cpp on |
| `work` | Work | productivity | LibreOffice/Thunderbird/OnlyOffice(optional) pinned, `power-profiles-daemon` balanced, night light on |
| `creator` | Creator | Adobe/creative via Wine | Bottles + creator recipes ready, GIMP/Krita/Kdenlive suggested, colour-managed profile hint |
| `lite` | Lite | ≤ 4 GB RAM / old PCs | no compositor, no picom, no animations, zram 100 %, minimal tray, `earlyoom` aggressive |

Storage:
- System default: `/etc/lindos/system.json` → `{"mode": "everyday", "browser": "firefox", "oem": false}`
- User: `~/.config/lindos/config.json` (see §4.2). User mode overrides system mode.
- Mode definitions: `/usr/share/lindos/modes/<id>/`
  - `mode.json` — `{ "id", "name", "description", "icon", "packages": [..apt..], "flatpaks": [..],
    "services_disable": [..], "services_enable": [..], "sysctl": {"key": "value"},
    "governor": "schedutil|performance|powersave", "pins": [".desktop ids in order"],
    "compositor": "picom|xfwm|none", "zram_percent": 50 }`
  - `panel.tar.bz2` — an **xfce4-panel-profiles** profile (created by `xfce4-panel-profiles save`)
    OR `panel/` dir with `xfce4-panel.xml` + `panel-*/…rc` that `lindos-mode` applies via
    `xfce4-panel-profiles load` (preferred format: the tarball; the desktop package must generate it
    at build time from `panel/` using `build/chroot/40-theme.sh` if xfce4-panel-profiles is present,
    else copy the xml into `~/.config/xfce4/xfconf/xfce-perchannel-xml/`).
  - optional `apply-user.sh` (runs as user after mode switch), `apply-system.sh` (runs as root).

CLI: `lindos-mode list | get | set <id> [--system]` (in lindos-core). `set` = write config →
`lindos.modes.apply_mode()` → user part (panel profile, xfconf, compositor, autostart of
mangohud/gamemode toggles) + privileged part via helper action `apply-mode` (packages install if
missing & online, services, sysctl drop-in `/etc/sysctl.d/90-lindos-mode.conf`, governor,
zram config, ananicy). Must be idempotent and safe offline (skip installs, log warning).

## 4. `lindos-core` (Python 3, no GTK dependency)

Installed at `/usr/lib/python3/dist-packages/lindos/`. Pure-Python, stdlib only (+ optional
`PyYAML` guarded). Every module must be importable on any OS (tests run on Windows/macOS): wrap
Linux-only calls, never execute at import time.

### 4.1 Paths (`lindos/paths.py`)
```python
SYSTEM_CONF_DIR = "/etc/lindos"
SYSTEM_CONF     = "/etc/lindos/system.json"
SHARE_DIR       = "/usr/share/lindos"
MODES_DIR       = "/usr/share/lindos/modes"
RECIPES_DIR     = "/usr/share/lindos/recipes"          # compat recipes (yaml/json)
LIBEXEC_DIR     = "/usr/libexec/lindos"
HELPER          = "/usr/libexec/lindos/lindos-helper"
USER_CONF_DIR   = "~/.config/lindos"                    # expanduser at call time
USER_CONF       = "~/.config/lindos/config.json"
SETUP_DONE      = "~/.config/lindos/setup-done"
STATE_DIR       = "~/.local/share/lindos"
PREFIXES_DIR    = "~/.local/share/lindos/prefixes"
APPS_DB         = "~/.local/share/lindos/apps.json"
LOG_DIR         = "~/.local/state/lindos"
```
All are overridable via env `LINDOS_ROOT` (prefix for system paths, used by tests) and
`LINDOS_HOME` (replaces `~`).

### 4.2 Config (`lindos/config.py`)
```python
DEFAULTS = {"mode":"everyday","browser":"firefox","theme":"dark","accent":"#60CDFF",
            "wallpaper":"/usr/share/backgrounds/lindos/aurora-dark.svg","setup_done":False,
            "gamemode_auto":True,"mangohud":False,"telemetry":False,"schema":1}
class Config:            # user config; dict-like .get/.set/.save()/.load(); atomic write
    def load(cls) -> "Config"
    def save(self) -> None
    def __getitem__/__setitem__/get/set/as_dict
def load_system() -> dict      # /etc/lindos/system.json merged with defaults
def effective_mode() -> str    # user override else system else "everyday"
def effective_browser() -> str
```

### 4.3 Modes (`lindos/modes.py`)
```python
MODE_IDS = ["everyday","gaming","work","creator","lite"]
@dataclass class Mode: id,name,description,icon,packages,flatpaks,services_disable,
                       services_enable,sysctl,governor,pins,compositor,zram_percent,path
def load_modes(modes_dir=None) -> dict[str, Mode]
def get_mode(mode_id) -> Mode
def current_mode() -> str
def apply_mode(mode_id, *, system=False, dry_run=False, log=print) -> ApplyResult
    # ApplyResult(ok: bool, steps: list[tuple[str,bool,str]])
def build_system_plan(mode: Mode) -> dict   # JSON-able plan sent to helper 'apply-mode'
```

### 4.4 Browsers (`lindos/browsers.py`)
```python
BROWSERS = {
 "edge":    {"name":"Microsoft Edge","package":"microsoft-edge-stable","desktop":"microsoft-edge.desktop",
             "repo":"deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main",
             "key_url":"https://packages.microsoft.com/keys/microsoft.asc","list":"/etc/apt/sources.list.d/microsoft-edge.list"},
 "chrome":  {"name":"Google Chrome","package":"google-chrome-stable","desktop":"google-chrome.desktop",
             "repo":"deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main",
             "key_url":"https://dl.google.com/linux/linux_signing_key.pub","list":"/etc/apt/sources.list.d/google-chrome.list"},
 "firefox": {"name":"Mozilla Firefox","package":"firefox","desktop":"firefox.desktop","repo":None,"key_url":None,"list":None},
}
def is_installed(bid) -> bool          # shutil.which / dpkg-query, guarded
def install(bid, log=print) -> bool     # via helper 'install-browser'
def set_default(bid) -> bool           # xdg-settings set default-web-browser + xfconf helper
def online() -> bool
```

### 4.5 Hardware (`lindos/hardware.py`)
`cpu_info()`, `gpu_info()` (parse `lspci -nn` / sysfs; vendor in {"nvidia","amd","intel","other"}),
`ram_info()` (`/proc/meminfo` → total/used/available MB), `battery_present()`,
`available_governors()`, `current_governor()`, `set_governor(g)` (helper `set-governor`),
`fan_sensors()` (parse `sensors -j` if present), `refresh_rates()` (parse `xrandr`).

### 4.6 Helper (`lindos/helper.py` + `/usr/libexec/lindos/lindos-helper`)
- `run_privileged(action: str, payload: dict, log=None) -> HelperResult(ok, out, err, code)` →
  `pkexec /usr/libexec/lindos/lindos-helper <action> <json>` (falls back to `sudo -n` if no
  pkexec; if already root, direct).
- Polkit policy id `org.lindos.helper` in `/usr/share/polkit-1/actions/org.lindos.helper.policy`
  with `auth_admin_keep`.
- Actions (helper validates every payload; never `shell=True`; whitelists only):
  `apply-mode`, `install-browser`, `install-packages`, `install-flatpaks`, `set-governor`,
  `set-services`, `apply-sysctl`, `apply-tune`, `set-zram`, `install-compat`, `install-gaming`,
  `install-drivers`, `set-fan-profile`, `write-system-config`, `enable-earlyoom`.
- Logs to `/var/log/lindos/helper.log`.

### 4.7 Theme (`lindos/theme.py`)
`set_dark(bool)` (xfconf: `/Net/ThemeName`, `/Net/IconThemeName`, xfwm `/general/theme`,
cursor; also writes `~/.config/gtk-4.0/settings.ini` `gtk-application-prefer-dark-theme` and
`gsettings set org.gnome.desktop.interface color-scheme` if available), `is_dark()`,
`set_accent(hex)` (writes `~/.config/gtk-3.0/lindos-accent.css` and `~/.config/gtk-3.0/gtk.css`
`@import`), `list_wallpapers()`, `set_wallpaper(path)` (xfconf all monitors/workspaces),
`set_font(name)`, `set_taskbar_alignment("center"|"left")`, `set_taskbar_position("bottom"|"top")`.

### 4.8 Compat helpers (`lindos/compat.py`)
```python
@dataclass class ExeInfo: path,name,kind ("installer"|"app"|"game"|"msi"|"unknown"),
                          arch ("x86"|"x64"|"unknown"), installer_type (nsis|inno|installshield|msi|wix|squirrel|None),
                          product, company, sha256_prefix
def analyze_exe(path) -> ExeInfo      # minimal PE parse: MZ→e_lfanew→PE→Machine; scan first 4 MB for markers
def slugify(name) -> str
def apps_db_load()/apps_db_save(dict)
def choose_runner(info, config) -> str  # "umu" | "wine" | "bottles"  (rules in §9)
```

### 4.9 RAM (`lindos/ram.py`)
`snapshot()` → dict(total, used, available, top=[(name,rss_mb),…]) ; `report(fmt="text|json")`.

### 4.10 CLIs shipped by lindos-core (all `/usr/bin/`, python3, argparse, `--json` where sensible)
`lindos-mode`, `lindos-browser`, `lindos-config` (`get/set/show`), `lindos-ram` (alias for
`lindos-tune status` output). Exit codes: 0 ok, 1 error, 2 usage.

## 5. `lindos-desktop` (theme, panel, shortcuts, branding)

Ships:
- `/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/` defaults: `xfce4-panel.xml`, `xfwm4.xml`,
  `xsettings.xml`, `xfce4-keyboard-shortcuts.xml`, `xfce4-desktop.xml`, `thunar.xml`,
  `xfce4-notifyd.xml`, `xfce4-power-manager.xml`, `xfce4-session.xml`, `keyboards.xml`.
- `/etc/xdg/xfce4/panel/` default plugin rcs (`whiskermenu-1.rc`, `docklike-2.rc`, `clock-*.rc`…).
- Panel spec: **bottom**, 48 px, spans monitor, dark translucent (`background-style=2`, rgba
  `#202020` α 0.85), plugin order:
  `separator(expand,transparent) · whiskermenu(icon lindos-start, no title) · docklike(pinned per mode)
   · separator(expand,transparent) · systray · pulseaudio · power-manager · xfce4-notifyd panel · clock
   (two-line `%H:%M` / `%d/%m/%Y`, Selawik 9) · showdesktop`.
  Whisker: `view-mode=2` (icon grid), `favorites` = pins, `show-button-title=false`,
  `menu-width=680 menu-height=720`, category position left, search at top, `command-lockscreen`,
  `command-switchuser`, `command-logout` (xfce4-session-logout), settings → `lindos-settings`,
  `command-profile` → `lindos-settings accounts`.
- Keyboard shortcuts (`xfce4-keyboard-shortcuts.xml`, `commands/custom` + `xfwm4/custom`):
  `Super` (`Super_L`) → `xfce4-popup-whiskermenu`; `Super+E` → `thunar`; `Super+I` →
  `lindos-settings`; `Super+D` → show desktop; `Super+L` → `xflock4`; `Super+A` →
  `xfce4-notifyd` toggle (`xfce4-panel --plugin-event=notification-plugin-*:toggle` fallback to
  `xfce4-notifyd-config`); `Super+X` → `lindos-settings --power-menu`; `Super+V` →
  `xfce4-popup-clipman`; `Super+R` → `xfce4-appfinder --collapsed`; `Super+Shift+S` →
  `xfce4-screenshooter -r`; `Print` → `xfce4-screenshooter -f`; `Ctrl+Shift+Escape` →
  `xfce4-taskmanager`; `Super+Tab` → cycle windows; `Super+Left/Right/Up/Down` → tile
  left/right/maximize/restore; `Super+Ctrl+Left/Right` → prev/next workspace; `Super+1..9` →
  activate docklike item N (`xfce4-panel --plugin-event=docklike-*:…` if supported else omit);
  `Super+.` → emoji picker (`emote` if installed); `Ctrl+Alt+T` → terminal; `Super+G` →
  `lindos-settings gaming`.
- xfwm4: theme `Lindos-Dark`, `button_layout=O|HMC`, `title_font=Selawik Bold 9`,
  `placement_mode=center`, `use_compositing=true` (except lite), `frame_opacity=100`,
  `show_frame_shadow=true`, `snap_to_windows`, `tile_on_move`, `wrap_workspaces=false`,
  `focus_delay=100`, `easy_click=Super`.
- picom: `/etc/xdg/picom-lindos.conf` (`backend=glx`, `vsync=true`, `corner-radius=8`, shadows,
  no blur by default, fade 0.03), autostart `/etc/xdg/autostart/lindos-picom.desktop`
  (`OnlyShowIn=XFCE`, Exec `lindos-compositor start` which honours mode: skips in lite).
- GTK CSS overlays `/usr/share/lindos/gtk-3.0/lindos.css` (round 8 px, Fluent-ish spacing,
  accent variable `@define-color lindos_accent #60CDFF`) imported by user `gtk.css` via
  `lindos.theme.set_accent`.
- Wallpapers (§2), Plymouth theme (script-based, logo + progress bar on `#202020`), slick-greeter
  conf, lightdm conf (`greeter-hide-users=false`, `user-background=false`), `/etc/xdg/autostart/`
  entries: `lindos-setup.desktop` (first-run gate), `lindos-picom.desktop`, `lindos-mode-apply-user`.
- Branded `.desktop` shims in `/usr/share/applications/`: `lindos-files.desktop` (Name=File
  Explorer, Exec=thunar), `lindos-settings.desktop` (Name=Settings), `lindos-store.desktop`
  (Name=Store, Exec=mintinstall), `lindos-terminal.desktop` (Name=Terminal, Exec=xfce4-terminal).
- Fonts installed to `/usr/share/fonts/truetype/{selawik,inter}` by `build/fetch-assets.sh`
  (pinned URLs + SHA256) — the package ships `fonts.conf` snippet
  `/etc/fonts/conf.d/60-lindos-ui.conf` aliasing `Segoe UI` → Selawik.
- Branding files: `os-release` fragment applied by `build/chroot/40-theme.sh` (sed, not
  overwrite), `/etc/lindos-release`, `/usr/share/pixmaps/lindos-logo.svg`, `lindos-start` icon
  in `/usr/share/icons/hicolor/scalable/apps/`, `/etc/issue`, `/etc/motd`-less.
- `lindos-compositor` CLI (`start|stop|status|toggle`) in `/usr/bin` (sh).

## 6. `lindos-setup` (OOBE)

- Binary `/usr/bin/lindos-setup` → `python3 /usr/lib/lindos-setup/main.py "$@"`.
- Flags: `--first-run` (exit 0 silently if `SETUP_DONE` exists or not in XFCE), `--reconfigure`,
  `--dry-run` (no helper calls, print plan), `--page <id>`.
- GTK 3, one `Gtk.Window` fullscreen, undecorated, dark `#202020`, centred card 900×620,
  Windows-11-OOBE styling via `Gtk.CssProvider` (`ui/oobe.css`). Fonts follow xsettings.
- Pages (ids): `welcome` → `mode` → `browser` → `personalize` → `apps` → `privacy` → `summary`
  → `apply` → `done`.
  - `mode`: 5 cards (icon, name, one-line description, RAM hint), default Everyday.
  - `browser`: 3 cards (Edge, Chrome, Firefox) with note "Edge/Chrome download from vendor";
    disabled with explanation when offline (Firefox pre-selected).
  - `personalize`: Dark/Light toggle (default Dark), accent swatches (8), wallpaper thumbnails,
    taskbar alignment Center/Left.
  - `apps`: checkboxes — "Windows app support (Wine + Proton)" (on), "Steam" (on in gaming),
    "Roblox (Sober)", "Minecraft (Prism Launcher)", "Heroic (Epic/GOG)", "Lutris", "Bottles",
    "Office (LibreOffice already installed) — add OnlyOffice", "Creative (GIMP, Krita, Kdenlive)".
    Pre-checked set depends on chosen mode.
  - `privacy`: informational (no telemetry, no ads), toggles: location services off, crash
    reports off (both default off; nothing to send).
  - `summary`: review + Back.
  - `apply`: progress bar + log; builds `Plan` (JSON) → user-side steps directly + ONE helper call
    per privileged group; robust to offline (mark skipped, tell user how to finish later:
    `lindos-settings apps`).
  - `done`: "Welcome to Lindos" + Finish (writes `SETUP_DONE`, config `setup_done=true`).
- Pure logic in `lindos_setup/plan.py` (`Plan`, `build_plan(selections) -> Plan`, `Plan.to_json()`,
  `Plan.user_steps()`, `Plan.system_payloads()`), unit-tested without GTK.
- Autostart: `/etc/xdg/autostart/lindos-setup.desktop` (`Exec=lindos-setup --first-run`,
  `OnlyShowIn=XFCE`, `X-GNOME-Autostart-Delay=2`).

## 7. `lindos-settings`

- Binary `/usr/bin/lindos-settings [page] [--power-menu]`; `python3 /usr/lib/lindos-settings/main.py`.
- Window 1100×720 min 800×560, header search box, **left sidebar** (Win11 order):
  `home` Home · `system` System · `personalization` Personalization · `apps` Apps ·
  `windows-apps` Windows apps · `gaming` Gaming · `hardware` Hardware · `network` Network ·
  `accounts` Accounts · `mode` Lindos Mode · `update` Update & Recovery · `about` About.
- Native pages (implemented in GTK): `home` (mode card, RAM meter, quick toggles: Dark mode,
  Game Mode, MangoHud, Compositor), `personalization` (theme, accent, wallpaper, taskbar
  alignment/position, fonts, cursor), `mode` (5 cards + Apply, shows what changes),
  `windows-apps` (list from APPS_DB: run/uninstall/open prefix/`winecfg`, "Install a Windows
  program…" file chooser → `lindos-run`), `gaming` (Game Mode auto, MangoHud, Proton-GE
  update/list via `lindos-proton`, launchers install buttons, controller status, FPS/refresh),
  `hardware` (CPU governor + EPP, GPU vendor + driver status + buttons for `nvidia-settings` /
  `corectrl`, fans (`sensors` readout, `nbfc` profile), power profile, RGB → OpenRGB, refresh
  rate per monitor), `about` (Lindos version, kernel, CPU/GPU/RAM, "Windows-like specs" layout).
- Delegating pages (buttons that launch existing tools — keep RAM low): `system` → Display
  (`xfce4-display-settings`), Sound (`pavucontrol`), Notifications (`xfce4-notifyd-config`),
  Power (`xfce4-power-manager-settings`), Storage (`baobab` if present else `gnome-disks`),
  Default apps (`xfce4-mime-settings`), Bluetooth (`blueman-manager`), Printers
  (`system-config-printer`); `apps` → Store (`mintinstall`), Installed (`mintinstall`/`synaptic`),
  Startup (`xfce4-session-settings`), Default apps; `network` → `nm-connection-editor`, VPN,
  Firewall (`gufw`); `accounts` → `users-admin`/`mintusers` (`cinnamon-settings users` absent →
  `mugshot` for avatar), `update` → `mintupdate`, Drivers (`mintdrivers`), Timeshift, Recovery
  (boot repair link), Kernels (`mintupdate` kernels).
- `--power-menu`: small popup at bottom-left with Sleep / Restart / Shut down / Sign out / Lock /
  Settings / File Explorer / Terminal / Task Manager (Win+X clone).
- Pure logic module `lindos_settings/model.py` (page registry, quick-toggle actions) tested.

## 8. Build pipeline (`build/`)

`build/config.env` (sourced by scripts):
```
LINDOS_VERSION=1.0.0
LINDOS_CODENAME=Aurora
BASE_ISO_URL="https://mirrors.edge.kernel.org/linuxmint/stable/22.2/linuxmint-22.2-xfce-64bit.iso"
BASE_ISO_SHA256=""     # empty = skip verify (warn); fill for releases
BASE_UBUNTU_CODENAME=noble
OUT_DIR=out
WORK_DIR=out/work
ISO_NAME="lindos-${LINDOS_VERSION}-xfce-64bit.iso"
SQUASHFS_COMP=zstd
SQUASHFS_ARGS="-Xcompression-level 19 -b 1M"
INCLUDE_WINE=1 INCLUDE_STEAM=1 INCLUDE_FLATPAK_LAUNCHERS=0
```
`build-iso.sh` steps: check root+deps (`xorriso squashfs-tools rsync wget dosfstools mtools
7zip|p7zip-full dpkg-dev`) → download/verify ISO → extract with `xorriso -osirrox on` → `unsquashfs`
→ prepare chroot (bind `/dev /dev/pts /proc /sys /run`, copy `resolv.conf`, `DEBIAN_FRONTEND=
noninteractive`, `LC_ALL=C.UTF-8`, policy-rc.d to block service starts, `dpkg-divert` for
`initctl`/`ischroot` not needed on systemd but keep policy-rc.d) → copy `out/debs/*.deb` and
`build/chroot/` into `/tmp/lindos/` inside chroot → run hooks in order → cleanup → `mksquashfs`
(zstd) → write `casper/filesystem.size`, `filesystem.manifest` (`dpkg-query -W`), remove-list →
copy overlay (branded `boot/grub/grub.cfg`, `isolinux/*.cfg` if present, `.disk/info`
"Lindos 1.0 Aurora") → `md5sum.txt` regen → build ISO with
`xorriso -indev "$BASE_ISO" -outdev "$OUT" -boot_image any replay -map "$ISO_DIR" / -volid LINDOS`
(the *replay* trick keeps the original hybrid BIOS+UEFI boot records; document why) →
`sha256sum`. Every step logged to `out/build.log`; `set -Eeuo pipefail`; traps to unmount chroot
binds even on failure. Support `--skip-download`, `--no-cleanup`, `--only-debs`, `--iso PATH`.

Chroot hooks (`build/chroot/`), each `#!/bin/bash`, `set -Eeuo pipefail`, idempotent:
- `00-repos.sh` — add WineHQ (noble), Steam (Valve repo, i386 enabled), Mozilla apt repo (pin
  firefox .deb from Mozilla or keep Mint's), Flathub remote (flatpak already on Mint), Kisak Mesa
  optional (flag), `dpkg --add-architecture i386`, `apt-get update`.
- `10-debloat.sh` — purge list (keep tiny): `hexchat thunderbird? (keep) rhythmbox hypnotix
  warpinator? (keep, useful) mintwelcome? (replace by lindos-setup; keep package but hide autostart)
  onboard drawing simple-scan? (keep) gnome-calendar?`. Actual list documented in
  `docs/RAM-BUDGET.md`; never purge network/printing basics; disable (not purge) `bluetooth`,
  `cups` (socket-activated), `ModemManager`, `avahi-daemon` (keep for printers? → keep enabled but
  document), `mintreport`, `apport`, `whoopsie`, `kerneloops`, `ubuntu-report`, `brltty`,
  `speech-dispatcher`, `NetworkManager-wait-online`.
- `20-base.sh` — install: `xfce4-docklike-plugin xfce4-panel-profiles xfce4-clipman-plugin
  xfce4-notifyd picom zram-tools|systemd-zram-generator earlyoom gamemode mangohud
  power-profiles-daemon lm-sensors fancontrol python3-gi gir1.2-gtk-3.0 polkitd pkexec
  flatpak fonts-noto-color-emoji xdg-desktop-portal-gtk winbind cabextract`, plus mode packages.
  `xfce4-docklike-plugin` is not packaged for Ubuntu 24.04 "noble" as of this writing (only
  25.10+ / Debian trixie+ carry it): `20-base.sh` already skips any package `pkg_available`
  reports as absent with a loud warning rather than failing the build, and
  `packages/lindos-desktop/DEBIAN/control` lists it as a Recommends (not a Depends) for exactly
  this reason — see `docs/BUILDING.md` and `CI-LOGS.md`.
- `30-lindos-debs.sh` — `apt-get install ./tmp/lindos/debs/*.deb`.
- `40-theme.sh` — run `fetch-assets` outputs already staged in `/tmp/lindos/assets` (theme,
  icons, cursors, fonts) → install (`install.sh -n Lindos …`), os-release sed, plymouth default,
  `update-alternatives` for default wallpaper, `glib-compile-schemas`, `fc-cache`,
  `update-icon-caches`, generate `panel.tar.bz2` per mode, `update-initramfs -u` (plymouth).
- `50-tune.sh` — apply base tune (systemd presets, sysctl, zram, journald, tmpfiles) via
  `lindos-tune apply --system --mode everyday --offline`.
- `60-compat.sh` — wine-staging (WineHQ) + winetricks + `umu-launcher` (deb from openSUSE OBS
  or GitHub release pinned) + dxvk/vkd3d optional; MIME defaults; `lindos-compat doctor`.
- `70-gaming.sh` — steam-launcher (Valve deb), lutris (Ubuntu repo or GitHub deb pinned),
  heroic (GitHub deb pinned), prism launcher (PPA/Flatpak flag), `steam-devices`, `xpadneo`
  (dkms optional), `mesa-vulkan-drivers:i386`, `libgl1-mesa-dri:i386`, `nvidia` NOT preinstalled
  (mintdrivers/`lindos-drivers` at first boot; ISO ships `nvidia-driver-5xx` in pool? → no,
  keep ISO small; document), OpenRGB (repo/deb), `antimicrox`, `goverlay`, `piper`, `corectrl`.
- `80-cleanup.sh` — `apt-get autoremove --purge`, `apt-get clean`, rm `/tmp/lindos`, machine-id
  reset, `/var/lib/dbus/machine-id`, resolv.conf restore, logs truncated, `/root/.bash_history`.

## 9. `lindos-compat` (Windows apps)

- Depends: `lindos-core`, `wine-staging | wine`, `winetricks`, `cabextract`, `winbind`,
  `umu-launcher` (Recommends), `bottles` via Flatpak (Suggests, installed on demand).
- `/usr/bin/lindos-run <file.exe|.msi|.bat|.lnk> [--runner umu|wine|bottles] [--prefix NAME]
  [--new-prefix] [--gamemode] [--mangohud] [--info]`:
  1. `analyze_exe` → decide runner (`choose_runner`): games & unknown → **umu** (Proton-GE via
     umu-launcher, DXVK/VKD3D built-in) if available else wine; installers/apps (office, adobe,
     utilities) → **wine** (staging); creator recipes → **bottles** if installed.
  2. Prefix: default per-app `~/.local/share/lindos/prefixes/<slug>` (`WINEPREFIX`), shared
     `default` for small utilities (flag `--shared`); 64-bit default; `WINEDLLOVERRIDES=winemenubuilder.exe=d`
     to prevent .desktop spam; `WINEDEBUG=-all`; `DXVK_ASYNC`, `PROTON_*` env for umu; `gamemoderun`
     wrapper if `--gamemode` or config `gamemode_auto` & kind==game; `mangohud` if flag.
  3. Installer flow: run, then scan prefix `drive_c/Program Files*/`, `users/*/Start Menu` and
     `~/.local/share/applications/wine/` for new `.lnk`/`.exe` → create `.desktop`
     `~/.local/share/applications/lindos-<slug>.desktop` (Exec=`lindos-run --prefix <slug>
     "<exe>"`, Icon extracted via `wrestool`+`icotool` if `icoutils` present else `lindos-exe`
     generic icon) → register in `APPS_DB` `{slug:{name,exe,prefix,runner,installed_at,kind}}`.
  4. GUI feedback via `zenity`/`yad` when launched from file manager (no terminal): "Preparing
     Windows compatibility…", errors with copyable log path `~/.local/state/lindos/run-<slug>.log`.
- MIME: `/usr/share/applications/lindos-run.desktop` (`MimeType=application/x-ms-dos-executable;
  application/x-msdownload;application/x-msi;application/x-ms-shortcut;application/x-bat;`),
  `/usr/share/lindos/mimeapps-lindos.list` merged into `/etc/xdg/mimeapps.list` by postinst
  (`[Default Applications]`), `update-desktop-database`.
- Thunar custom action `/etc/xdg/Thunar/uca.xml` fragment merged in postinst: "Run with Lindos
  (Windows app)", "Run with Proton (game)", "Open C:\ drive".
- `lindos-compat` CLI: `doctor` (checks wine/umu/vulkan/32-bit libs/fonts, prints fix commands),
  `prefixes list|remove|open|winecfg <slug>`, `recipes list|apply <id> [--prefix]`, `install-umu`,
  `install-bottles`, `proton list|update` (delegates to `lindos-proton`).
- Recipes `/usr/share/lindos/recipes/*.json`: `{id,name,vendor,category,status
  ("works"|"partial"|"broken"),notes,runner,arch,winetricks:[...],dll_overrides:{},env:{},
  post_cmds:[],registry:[{key,name,type,value}]}` — ship at least:
  `photoshop-cc-2021` (partial/works w/ notes), `photoshop-cs6` (works), `illustrator-cc-2021`
  (partial), `premiere` (broken — say so, suggest Kdenlive/DaVinci Resolve native),
  `office-2016` (works), `office-365` (broken → LibreOffice/OnlyOffice/web), `notepad-plus-plus`
  (works), `7zip` (works), `winrar` (works), `paint-net` (partial), `foobar2000` (works),
  `autocad` (broken → suggest FreeCAD/web), `epic-games-launcher` (works via Heroic instead),
  `roblox-player` (broken → Sober), `riot-client` (broken — Vanguard).
- Windows-friendly touches: `~/.local/share/lindos/prefixes/<slug>/drive_c` symlinked as
  `~/Windows Apps/<Name>/C:` (created lazily); `lindos-run --info` prints PE details.

## 10. `lindos-gaming`

- Depends: `lindos-core`, `gamemode`, `mangohud`, `steam-devices`; Recommends `steam-launcher |
  steam-installer`, `lutris`, `heroic`, `prismlauncher`, `antimicrox`, `goverlay`, `piper`,
  `corectrl`, `openrgb`, `mesa-vulkan-drivers`, `libvulkan1`, `vulkan-tools`.
- CLIs: `lindos-proton list|update|remove <tag>` (Proton-GE from GitHub releases → `~/.steam/root/
  compatibilitytools.d/` and `~/.local/share/umu/compatibilitytools/` shared via symlink; also
  used by umu), `lindos-drivers detect|install [--nvidia-open|--nvidia-proprietary|--amd|--intel]
  --status` (wraps `ubuntu-drivers`, `mintdrivers`, sets `nvidia-drm.modeset=1`),
  `lindos-game install <steam|lutris|heroic|prism|sober|vinegar|mcpelauncher|bottles|all>`,
  `lindos-game status`.
- Roblox: `lindos-game install sober` → `flatpak install -y flathub org.vinegarhq.Sober`; desktop
  file `lindos-roblox.desktop` (Name=Roblox, Exec=`flatpak run org.vinegarhq.Sober`); doc note.
- Minecraft: Prism Launcher (Flatpak `org.prismlauncher.PrismLauncher` or PPA) + `openjdk-17/21`;
  Bedrock via `io.mrarm.mcpelauncher` Flatpak (unofficial; user must own Android version).
- Config: `/etc/gamemode.ini` (`renice=10`, `ioprio=0`, `inhibit_screensaver=1`, `softrealtime=auto`,
  `desiredgov=performance`, scripts to toggle picom off/on: `lindos-compositor stop|start`),
  `/etc/xdg/MangoHud/MangoHud.conf` (fps, frametime, gpu/cpu temp, ram, `position=top-left`,
  `toggle_hud=Shift_R+F12`, `font_size=20`, `no_display=1` unless mode gaming & config mangohud),
  udev: `60-lindos-controllers.rules` (Xbox/PS/8BitDo/Switch hidraw perms), `xpadneo`/`xone`
  optional installers, `/etc/sysctl.d/80-lindos-gaming.conf` (`vm.max_map_count=2147483642`).
- Compat matrix data: `/usr/share/lindos/compat-matrix.json` (games list with status + reason +
  link) — same data renders `docs/COMPATIBILITY.md` (docs must match). Minimum entries:
  Roblox (works via Sober), Minecraft Java (works), Minecraft Bedrock (partial), Valorant
  (**not possible**, Vanguard), Fortnite (**not possible**, Epic disabled EAC-Linux), Apex Legends
  (not possible since Nov 2024), CS2 (native), Dota 2 (native), GTA V (works; Online: BattlEye
  Linux enabled — verify), Elden Ring (works), Cyberpunk 2077 (works), Rocket League (works via
  Heroic/Steam), Genshin Impact (works via Anime Game Launcher — third-party), League of Legends
  (**not possible** since Vanguard 2024), Rainbow Six Siege (not possible, BattlEye disabled),
  Destiny 2 (not possible), PUBG (not possible), Overwatch 2 (works via Battle.net in Lutris),
  Sims 4 (works via EA app in Lutris/Heroic — partial), Forza Horizon 5 (works), Hogwarts Legacy
  (works), Halo Infinite (works, EAC enabled), Palworld (works), Helldivers 2 (works, nProtect
  enabled), Marvel Rivals (works), Warframe (works).

## 11. `lindos-tune` (RAM/perf/hardware)

- CLI `lindos-tune status [--json]` (RAM used/available, zram, top 10 RSS, services on, governor,
  compositor, score vs target 350–500 MB), `lindos-tune apply --mode <id> [--system] [--offline]`,
  `lindos-tune services list|disable|enable <unit>` (whitelist), `lindos-tune zram <percent>`,
  `lindos-tune governor <g>`, `lindos-tune fan list|set <profile>` (nbfc/fancontrol),
  `lindos-tune power <performance|balanced|power-saver>` (powerprofilesctl), `lindos-tune report`
  (markdown for bug reports).
- Ships: `/etc/sysctl.d/70-lindos-base.conf` (`vm.swappiness=180` when zram, `vm.page-cluster=0`,
  `vm.vfs_cache_pressure=50`, `vm.dirty_ratio`, `kernel.nmi_watchdog=0`, `net.core.default_qdisc=fq`
  `net.ipv4.tcp_congestion_control=bbr`), zram (`/etc/default/zramswap` or
  `/etc/systemd/zram-generator.conf` — support both, detect), earlyoom
  `/etc/default/earlyoom` (`-m 4 -s 100 --avoid '(^|/)(Xorg|xfwm4|xfce4-panel|lightdm)$'`),
  journald drop-in (`SystemMaxUse=64M`, `Storage=persistent`), tmpfiles (`/tmp` tmpfs via
  `tmp.mount` enable), systemd preset `/usr/lib/systemd/system-preset/90-lindos.preset`
  (disable list §8), NetworkManager `wifi.powersave` untouched, `ananicy-cpp` rules
  `/etc/ananicy.d/lindos/` (games → high prio, browsers → normal, compilers → idle) opt-in via
  gaming mode, `/etc/lindos/tune.d/*.conf` per-mode overrides, cpupower/`intel_pstate` EPP
  helper, `nbfc-linux` optional installer, `lm-sensors` autodetect at postinst (`sensors-detect
  --auto` guarded), `hdparm`/`fstrim.timer` on.
- RAM budget doc must list each measure and its estimated saving; measured baseline of stock
  Mint 22 XFCE ≈ 600–750 MB; Lindos target 350–500 MB; Lite ≈ 300–380 MB (no compositor).

## 12. Docs & tests
- `docs/COMPATIBILITY.md` is **generated** from `packages/lindos-gaming/root/usr/share/lindos/
  compat-matrix.json` by `tests/gen-compat-doc.py` (also verifies in CI that they match).
- `tests/run.sh`: `bash -n` all `*.sh`/bin scripts with bash shebang, `sh -n` for `#!/bin/sh`,
  `python3 -m py_compile` all `.py` + python bins, `shellcheck` if available, `pytest -q tests
  packages/*/tests`, JSON validity of every `*.json`, XML well-formedness of every `*.xml`,
  `.desktop` sanity (`[Desktop Entry]`, `Name=`, `Exec=`), `desktop-file-validate` if present.
- `tests/conftest.py`: adds `packages/lindos-core/root/usr/lib/python3/dist-packages` and
  `packages/lindos-setup/root/usr/lib/lindos-setup`, `packages/lindos-settings/root/usr/lib/
  lindos-settings`, `packages/lindos-compat/root/usr/lib/lindos-compat` to `sys.path`; installs
  a stub `gi` module (`gi.require_version`, `gi.repository.Gtk/Gdk/GLib/Gio/Pango/GdkPixbuf`
  as permissive dummies) **only if real gi is missing**.
- Shell style: `#!/bin/bash`, `set -Eeuo pipefail`, functions, `log()`/`die()`, no `sudo` inside
  scripts (callers use pkexec/sudo), quote everything, ShellCheck-clean.
- Python style: 3.10+ (base has 3.12), type hints, no third-party deps except `gi` in UI,
  `argparse` CLIs, `logging`, never `shell=True`.

## 13. Cross-component call map (must agree)
| Caller | Calls |
|---|---|
| lindos-setup apply page | `lindos.config`, `lindos.modes.apply_mode`, `lindos.browsers.install/set_default`, `lindos.theme.*`, helper `install-compat` / `install-gaming` / `install-flatpaks` / `install-packages` |
| lindos-settings | everything in lindos-core; `lindos-run`, `lindos-proton`, `lindos-drivers`, `lindos-game`, `lindos-tune`, `lindos-compat` via subprocess |
| lindos-mode set | `lindos.modes.apply_mode` → helper `apply-mode` (payload from `build_system_plan`) + `xfce4-panel-profiles load` + `lindos-compositor` |
| helper `apply-mode` | `lindos-tune apply --mode <id> --system` |
| helper `install-compat` | `/usr/libexec/lindos/install-compat.sh` (from lindos-compat) |
| helper `install-gaming` | `/usr/libexec/lindos/install-gaming.sh <items…>` (from lindos-gaming) |
| helper `install-browser` | `/usr/libexec/lindos/install-browser.sh <edge|chrome|firefox>` (from lindos-core) |
| helper `install-drivers` | `lindos-drivers install …` (from lindos-gaming) |
| build 50-tune.sh | `lindos-tune apply --mode everyday --system --offline` |
| build 40-theme.sh | `/usr/libexec/lindos/build-panel-profiles.sh` (from lindos-desktop) |

## 14. Addendum K — Kernel & Compat-Performance layer

The Lindos-tuned kernel (`lindos-kernel`: ntsync, sched_ext, 1000 Hz/preempt, MGLRU, tuned
config + build recipe), the sched_ext/THP additions to `lindos-tune`, and the Proton/DXVK/
VKD3D/gamescope/per-title-profile overhaul of `lindos-compat`/`lindos-gaming` are specified in
[`SPEC-KERNEL.md`](SPEC-KERNEL.md) (§14–§19). Its §14 honesty rules (no anti-cheat circumvention
of any kind — no "Windows attester", attestation/TPM forger, or HWID/VM-detection evader; such a
tool cannot produce hardware-rooted signatures and only gets the user hardware-banned) are binding
everywhere, extending §0.1.

## 15. Addendum V — Virtualization, WinApps, Proton-system, Drivers, CI

The honest KVM/VFIO Windows VM (`lindos-vm`, real Windows VM with optional GPU passthrough — **no**
hypervisor-hiding/SMBIOS/UUID/ACPI/CPUID spoofing, does not defeat VM-blocking anti-cheat), seamless
Windows apps over RDP (`lindos-winapps`, for Adobe/Office — user supplies licensed Windows),
system-wide Proton-GE + gamescope session, expanded driver automation (Broadcom/audio + first-boot),
and real GitHub-Actions Linux builds (kernel/deb/ISO) are specified in [`SPEC-VM.md`](SPEC-VM.md)
(§20–§26). Its §20 honesty rules (a VM is a truthful VM; no spoofing; no license bypass; user
supplies Windows) are binding, extending §0.1 and §14.

## 16. Addendum W — Every Windows format, Transfer, Play-anywhere

Opening every common Windows file type Wine/Proton alone does not cover (MSIX/APPX packages and
bundles, `.appinstaller`, `.msp`, `.reg`, `.ps1`, `.vbs`, `.url`, `.scr`, `.cpl`, `.inf`, `.cab`,
disk images, ClickOnce, DOS and 16-bit Windows programs, `./setup.exe` from a terminal via
`binfmt_misc`, and `winget install <id>`), a new `lindos-transfer` package (a read-only,
Windows-Easy-Transfer-style migration tool with a Windows-side kit and GUI), and honest
"play-anywhere" routes for games whose kernel-level anti-cheat cannot run on any Linux (official
cloud streaming where a provider offers it, or a one-shot restart into the PC's own Windows
installation via `lindos-dualboot` — never a VM route, an emulator or a spoofer) are specified in
[`SPEC-WINDOWS.md`](SPEC-WINDOWS.md) (§27–§34). Its §27 honesty rules (no anti-cheat/attestation
circumvention of any kind; every format handler states `works`/`partial`/`unsupported` and never
tries a file anyway; transfer is read-only and never touches secrets; no licence bypass; routes are
official and region-accurate) are binding, extending §0.1, §14 and §15.
