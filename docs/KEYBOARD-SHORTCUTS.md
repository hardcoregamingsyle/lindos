# Keyboard shortcuts

Windows-11-style bindings on top of the standard XFCE set. Everything below is generated from
the shipped xfconf channel
`packages/lindos-desktop/root/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-keyboard-shortcuts.xml`
(`commands/custom` = application launchers handled by xfsettingsd, `xfwm4/custom` = window-manager
actions; the `default` branches are identical copies so *Reset to defaults* in Keyboard settings
restores this layout). Change them in **Settings → Keyboard** (`xfce4-keyboard-settings`) or with
`xfconf-query -c xfce4-keyboard-shortcuts`.

*Super* = the Windows key. Numpad keys are listed as `Numpad …`.

## Windows-style essentials

| Shortcut | Does | Command / action |
|---|---|---|
| `Super` (left Windows key, alone) | open the Start menu | `xfce4-popup-whiskermenu` |
| `Super+E` | File Explorer | `thunar` |
| `Super+I` | Lindos Settings | `lindos-settings` |
| `Super+X` | power / quick-links menu (Win+X) | `lindos-settings --power-menu` |
| `Super+L` | lock the screen | `xflock4` |
| `Super+D` / `Super+M` | show desktop | `show_desktop_key` |
| `Super+A` | notifications (notification log / do-not-disturb) — xfce4-notifyd has no "toggle centre" event, so its settings dialog opens | `xfce4-notifyd-config` |
| `Super+V` | clipboard history | `xfce4-popup-clipman` |
| `Super+R` | run dialog | `xfce4-appfinder --collapsed` |
| `Super+S` | search / application finder | `xfce4-appfinder` |
| `Super+G` | Lindos Settings → Gaming | `lindos-settings gaming` |
| `Super+P` | display / projector switch | `xfce4-display-settings --minimal` |
| `Super+Pause` | About this PC | `lindos-settings about` |
| `Super+.` | emoji picker (if `emote` is installed) | `emote` |
| `Super+Shift+S` | screenshot a region | `xfce4-screenshooter -r` |
| `PrtSc` / `Alt+PrtSc` / `Shift+PrtSc` | full screen / active window / region | `xfce4-screenshooter -f` / `-w` / `-r` |
| `Ctrl+Shift+Esc` | Task Manager | `xfce4-taskmanager` |
| `Ctrl+Alt+T` | Terminal | `exo-open --launch TerminalEmulator` |
| `Ctrl+Alt+Del` | lock the screen | `xflock4` |
| `Super+Tab` / `Super+Shift+Tab` | switch windows (also `Alt+Tab` / `Alt+Shift+Tab`) | `cycle_windows_key` / `cycle_reverse_windows_key` |
| `Super+←` / `Super+→` | tile left / right | `tile_left_key` / `tile_right_key` |
| `Super+↑` | maximise | `maximize_window_key` |
| `Super+↓` | minimise (xfwm4 has no restore-only action) | `hide_window_key` |
| `Ctrl+Super+←` / `Ctrl+Super+→` | previous / next workspace | `prev_workspace_key` / `next_workspace_key` |
| `Ctrl+Super+D` / `Ctrl+Super+F4` | add / remove workspace | `add_workspace_key` / `del_workspace_key` |
| `Super+1` … `Super+9` | activate the N-th taskbar item — **not** in this XML: `xfce4-docklike-plugin` grabs these itself (`keyComboActive=true` in `docklike-2.rc`) | docklike |

## All command shortcuts (`commands/custom`, 33 entries)

| Shortcut | Command |
|---|---|
| `Super` (left Windows key) | `xfce4-popup-whiskermenu` |
| `Super+E` | `thunar` |
| `Super+I` | `lindos-settings` |
| `Super+L` | `xflock4` |
| `Super+A` | `xfce4-notifyd-config` |
| `Super+X` | `lindos-settings --power-menu` |
| `Super+V` | `xfce4-popup-clipman` |
| `Super+R` | `xfce4-appfinder --collapsed` |
| `Super+S` | `xfce4-appfinder` |
| `Super+Shift+S` | `xfce4-screenshooter -r` |
| `PrtSc` | `xfce4-screenshooter -f` |
| `Alt+PrtSc` | `xfce4-screenshooter -w` |
| `Shift+PrtSc` | `xfce4-screenshooter -r` |
| `Ctrl+Shift+Esc` | `xfce4-taskmanager` |
| `Super+.` | `emote` |
| `Ctrl+Alt+T` | `exo-open --launch TerminalEmulator` |
| `Super+G` | `lindos-settings gaming` |
| `Super+P` | `xfce4-display-settings --minimal` |
| `Super+Pause` | `lindos-settings about` |
| `Alt+F1` | `xfce4-popup-whiskermenu` |
| `Alt+F2` | `xfce4-appfinder --collapsed` |
| `Alt+F3` | `xfce4-appfinder` |
| `Ctrl+Alt+Del` | `xflock4` |
| `Ctrl+Alt+L` | `xflock4` |
| `Ctrl+Esc` | `xfdesktop --menu` |
| `XF86Display` | `xfce4-display-settings --minimal` |
| `XF86WWW` | `exo-open --launch WebBrowser` |
| `HomePage` | `exo-open --launch WebBrowser` |
| `XF86Mail` | `exo-open --launch MailReader` |
| `XF86Explorer` | `exo-open --launch FileManager` |
| `XF86Terminal` | `exo-open --launch TerminalEmulator` |
| `XF86Calculator` | `gnome-calculator` |
| `XF86Tools` | `lindos-settings` |

Volume, mute and brightness media keys are handled by the pulseaudio and power-manager panel
plugins and are deliberately not bound here.

## All window-manager shortcuts (`xfwm4/custom`, 59 entries)

| Shortcut | xfwm4 action |
|---|---|
| `Super+Tab` | `cycle_windows_key` |
| `Super+Shift+Tab` | `cycle_reverse_windows_key` |
| `Alt+Tab` | `cycle_windows_key` |
| `Alt+Shift+Tab` | `cycle_reverse_windows_key` |
| `Super+D` | `show_desktop_key` |
| `Super+M` | `show_desktop_key` |
| `Super+←` | `tile_left_key` |
| `Super+→` | `tile_right_key` |
| `Super+↑` | `maximize_window_key` |
| `Super+↓` | `hide_window_key` |
| `Ctrl+Super+←` | `prev_workspace_key` |
| `Ctrl+Super+→` | `next_workspace_key` |
| `Ctrl+Super+D` | `add_workspace_key` |
| `Ctrl+Super+F4` | `del_workspace_key` |
| `Super+Numpad ←` | `tile_left_key` |
| `Super+Numpad →` | `tile_right_key` |
| `Super+Numpad ↑` | `tile_up_key` |
| `Super+Numpad ↓` | `tile_down_key` |
| `Super+Numpad Home` | `tile_up_left_key` |
| `Super+Numpad PgUp` | `tile_up_right_key` |
| `Super+Numpad End` | `tile_down_left_key` |
| `Super+Numpad PgDn` | `tile_down_right_key` |
| `Alt+F4` | `close_window_key` |
| `Alt+F6` | `stick_window_key` |
| `Alt+F7` | `move_window_key` |
| `Alt+F8` | `resize_window_key` |
| `Alt+F9` | `hide_window_key` |
| `Alt+F10` | `maximize_window_key` |
| `Alt+F11` | `fullscreen_key` |
| `Alt+F12` | `above_key` |
| `Alt+Space` | `popup_menu_key` |
| `Alt+Ins` | `add_workspace_key` |
| `Alt+Del` | `del_workspace_key` |
| `Shift+Alt+PgDn` | `lower_window_key` |
| `Shift+Alt+PgUp` | `raise_window_key` |
| `Ctrl+Alt+D` | `show_desktop_key` |
| `Ctrl+Alt+←` | `left_workspace_key` |
| `Ctrl+Alt+→` | `right_workspace_key` |
| `Ctrl+Alt+↑` | `up_workspace_key` |
| `Ctrl+Alt+↓` | `down_workspace_key` |
| `Ctrl+Alt+Home` | `prev_workspace_key` |
| `Ctrl+Alt+End` | `next_workspace_key` |
| `Ctrl+Shift+Alt+←` | `move_window_left_key` |
| `Ctrl+Shift+Alt+→` | `move_window_right_key` |
| `Ctrl+Shift+Alt+↑` | `move_window_up_key` |
| `Ctrl+Shift+Alt+↓` | `move_window_down_key` |
| `Ctrl+Alt+Numpad 1` | `move_window_workspace_1_key` |
| `Ctrl+Alt+Numpad 2` | `move_window_workspace_2_key` |
| `Ctrl+Alt+Numpad 3` | `move_window_workspace_3_key` |
| `Ctrl+Alt+Numpad 4` | `move_window_workspace_4_key` |
| `Ctrl+F1` | `workspace_1_key` |
| `Ctrl+F2` | `workspace_2_key` |
| `Ctrl+F3` | `workspace_3_key` |
| `Ctrl+F4` | `workspace_4_key` |
| `Esc` | `cancel_key` |
| `←` | `left_key` |
| `→` | `right_key` |
| `↑` | `up_key` |
| `↓` | `down_key` |

(`Esc`/arrow entries are xfwm4's move/resize-mode keys, not global bindings.) Mouse: `Super`
+ drag moves windows (`easy_click=Super`); windows snap to borders/other windows and tile when
dragged to a screen edge (`tile_on_move`).

## In games

`Shift_R+F12` toggles the MangoHud overlay (`/etc/xdg/MangoHud/MangoHud.conf`), `Shift_L+F2`
toggles MangoHud logging.

## Regenerating this file

The two long tables are produced from the XML (`custom` branch, `<Primary>` → `Ctrl`, `<Super>`
→ `Super`, `KP_*` → `Numpad …`). After editing the XML, regenerate them and keep the `default`
branch identical to `custom`; `packages/lindos-desktop/tests/test_xml.py` checks the XML itself.
