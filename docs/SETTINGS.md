# Lindos Settings

`lindos-settings` is the Windows-11-style settings centre (`Super+I`, the gear in the taskbar,
or *Settings* in the Start menu). It is a small GTK 3 application (`/usr/lib/lindos-settings/`)
with a left sidebar in the Windows 11 order, a header search box that filters the sidebar and the
cards on the current page, and a Win+X power menu. Native pages do the Lindos-specific things;
everything the Mint/XFCE tools already do well is *delegated* to them (one click opens the
existing dialog) to keep the RAM footprint low.

```
lindos-settings [PAGE] [--power-menu] [--list-pages] [--debug] [--version]
```

* `PAGE` — one of the ids below; aliases `wine`→`windows-apps`, `games`→`gaming`,
  `modes`→`mode`, `updates`→`update`, `personalisation`→`personalization`; unknown → `home`.
* A second `lindos-settings <page>` while the window is open just switches the running instance
  to that page (single instance, `org.lindos.Settings`).
* `--power-menu` shows the Win+X popup instead of the window (bound to `Super+X`).
* Log: `~/.local/state/lindos/settings.log`. Exit 0 ok / 1 error / 2 usage.

## Pages

The registry is `/usr/share/lindos/settings/pages.json` (mirrored by `model.BUILTIN_PAGES`; a
test keeps them identical). Kind **native** = built in GTK here; **delegate** = cards that launch
the listed program (with the alternatives tried in order and an "Install" offer naming the
package when none is present).

| id | Title | Kind | What is on it |
|---|---|---|---|
| `home` | Home | native | current **Lindos Mode** card, **Memory** meter (used vs. the 350–500 MB idle target, from `lindos-tune status`), *Quick settings*: Dark mode · Game Mode (`gamemode_auto`) · MangoHud overlay (`mangohud`, Shift_R+F12 toggles in game) · Compositor (`lindos-compositor`), *Recommended*: Windows apps · Gaming · Personalization · Update & Recovery |
| `system` | System | delegate | Display → `xfce4-display-settings` · Sound → `pavucontrol` · Notifications → `xfce4-notifyd-config` · Power & battery → `xfce4-power-manager-settings` · Storage → `baobab` (alt `gnome-disks`) · Default apps → `xfce4-mime-settings` · Bluetooth & devices → `blueman-manager` · Printers & scanners → `system-config-printer` |
| `personalization` | Personalization | native | *Background*: Wallpaper (the five Lindos SVGs + your files) · *Colours*: Dark/Light theme, Accent colour (8 swatches from `/usr/share/lindos/setup/accents.json`: Aurora Blue `#60CDFF`, Classic Blue `#0067C0`, Mint Green, Violet, Rose, Sunset Orange, Teal, Gold) · *Taskbar*: alignment Center/Left, position Bottom/Top · *Text and cursor*: Text size, Mouse pointer (Fluent cursors), Fonts (→ `xfce4-appearance-settings`), Lock screen |
| `apps` | Apps | delegate | Store → `mintinstall` · Installed apps → `mintinstall` (alt `synaptic-pkexec`, `synaptic`) · Startup → `xfce4-session-settings` · Default apps → `xfce4-mime-settings`; *Web browsers*: Microsoft Edge / Google Chrome / Mozilla Firefox cards — **Install** (helper `install-browser` via `lindos.browsers.install`, i.e. the vendor's apt repository; the place to finish an offline first boot) or **Make default** (`xdg-settings`); plus shortcuts to *Windows apps* and *Gaming launchers* |
| `windows-apps` | Windows apps | native | honesty card ("Wine / Proton — a translation layer, not Windows"), **Windows app support (Wine + Proton)** card with status line and an **Install / repair** button (helper `install-compat` `{items: [wine, umu]}` → `/usr/libexec/lindos/install-compat.sh wine umu`, asks for the administrator password once — the place to finish an offline first boot), *Installed Windows programs* from `~/.local/share/lindos/apps.json` with Run / Open C:\ / winecfg / Uninstall, "Install a Windows program…" file chooser → `lindos-run`, **Recipes…** (list with honest status + Apply, `lindos-compat recipes`), **Doctor** (`lindos-compat doctor` — prints the fix commands, e.g. `pkexec /usr/libexec/lindos/install-compat.sh`, when Wine/umu/Vulkan/32-bit libs are missing) |
| `gaming` | Gaming | native | *Performance*: Game Mode switch, MangoHud switch, GOverlay · *Proton-GE*: installed builds, Update / Remove (`lindos-proton`) · *Launchers*: Steam, Lutris, Heroic, Prism Launcher, Roblox (Sober), Roblox Studio (Vinegar), Minecraft Bedrock (mcpelauncher), Bottles — Install / Launch (`lindos-game`) · *Controllers*: connected pads (`/dev/input`), xpadneo/xone hints · *Display refresh rate*: per monitor via `xrandr` (session-only; make it permanent in Display settings) · *Compatibility*: the anti-cheat reality card + the full compat matrix (`/usr/share/lindos/compat-matrix.json`, same data as [COMPATIBILITY.md](COMPATIBILITY.md)) |
| `hardware` | Hardware | native | *Processor*: model, CPU governor (`schedutil`/`performance`/`powersave`, via helper `set-governor`), EPP read-out · *Graphics*: GPUs, driver status (`lindos-drivers status --json`), buttons NVIDIA Settings / CoreCtrl / Driver Manager (`mintdrivers`) / Install drivers · *Fans and sensors*: `sensors` read-out, fan profile auto/quiet/balanced/max (`lindos-tune fan set`, nbfc/thinkpad) · *Power*: power profile (`lindos-tune power` / `powerprofilesctl`), battery · *Devices and storage*: OpenRGB, Piper, SSD TRIM (`fstrim.timer`) |
| `network` | Network | delegate | Network connections → `nm-connection-editor` · VPN → `nm-connection-editor --create --type=vpn` · Firewall → `gufw` |
| `accounts` | Accounts | delegate | Users and groups → `users-admin` (alt `mintusers`, `cinnamon-settings users`) · Your info (avatar) → `mugshot`; sidebar avatar = `~/.face` or initials |
| `mode` | Lindos Mode | native | the five mode cards + **Apply** (`lindos-mode set`), "What will change" preview from the mode's plan (`lindos-mode show`) — see [MODES.md](MODES.md) |
| `update` | Update & Recovery | delegate | Update Manager → `mintupdate` · Driver Manager → `mintdrivers` · System snapshots (Timeshift) → `timeshift-launcher` (alt `timeshift-gtk`) · Recovery → Boot-Repair guide (browser) · Kernels → `mintupdate` (View → Linux Kernels) |
| `about` | About | native | Windows-like specs layout: Lindos version/codename, kernel, CPU, GPU, RAM, disk, mode; *Related*: Memory report (`lindos-tune status`), Bug report (`lindos-tune report`), Licence, Website |

## Win+X power menu

`lindos-settings --power-menu` (bound to `Super+X`) pops a small menu at the bottom-left:
Sleep (`xfce4-session-logout --suspend`) · Restart (`--reboot`) · Shut down (`--halt`) · Sign out
(`--logout`) · Lock (`xflock4`) · Settings (`lindos-settings`) · File Explorer (`thunar`) ·
Terminal (`xfce4-terminal`) · Task Manager (`xfce4-taskmanager`). Escape or focus-out closes it.

## Privileges

Everything that needs root goes through the polkit helper (`org.lindos.helper`,
`auth_admin_keep`): install packages/launchers/drivers, set governor, fan profile, enable
`fstrim.timer`, switch mode. One password prompt covers a burst of actions. Reading (sensors,
governor, drivers status, RAM) never asks.

## Where the settings live

* Lindos-specific keys: `~/.config/lindos/config.json` (`lindos-config show`).
* Theme, accent, wallpaper, taskbar: xfconf channels `xsettings`, `xfwm4`, `xfce4-panel`,
  `xfce4-desktop` (through `lindos.theme`) plus `~/.config/gtk-3.0/lindos-accent.css` /
  `gtk.css` and `~/.config/gtk-4.0/settings.ini`.
* Mode: `~/.config/lindos/config.json` → `/etc/lindos/system.json`.
* MangoHud: `~/.config/MangoHud/MangoHud.conf` (seeded from `/etc/xdg/MangoHud/MangoHud.conf` by
  `lindos-mangohud`).
