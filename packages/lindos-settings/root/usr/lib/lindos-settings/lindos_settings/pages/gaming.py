"""Gaming page: Game Mode auto, MangoHud, Proton-GE (lindos-proton), launcher grid
(helper install-gaming), controller status, refresh rate per monitor, anti-cheat reality, the
compatibility list (compat-matrix.json) and honest play-anywhere routes for titles that need
Windows (cloud streaming or a one-shot restart into Windows; never a VM or a spoof)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .. import model
from ..widgets import HAVE_GTK, Card, ComboCard, InfoCard, OutputDialog, PageBase, SwitchCard, add_class, badge, box, button, confirm, icon_image, label, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.gaming")


def _not_supported_badge(disclaimer: Dict[str, Any]) -> Any:
    """The "Not supported yet" badge; its tooltip is the publisher-decides sentence, so it is never shown bare."""
    return badge(model.BADGE_NOT_SUPPORTED_YET, disclaimer["badge"], disclaimer.get("short") or None)


class GamingPage(PageBase):
    PAGE_ID = "gaming"

    def build(self) -> None:
        b = self.backend
        # -- toggles
        sec = self.add_section("Performance")
        self.gamemode_card = SwitchCard("Game Mode", "Automatically wrap games with Feral GameMode (performance governor, renice, screensaver off)", ("applications-games",), ("gamemode", "performance"), on_toggle=lambda v: self._set_cfg("gamemode_auto", v, "Game Mode"), active=bool(b.config_get("gamemode_auto", True)))
        sec.add(self.gamemode_card)
        self.mango_card = SwitchCard("MangoHud overlay", "FPS, frametime, CPU/GPU temperature and RAM in games (Shift_R+F12 toggles the HUD)", ("utilities-system-monitor",), ("mangohud", "fps", "overlay"), on_toggle=lambda v: self._set_cfg("mangohud", v, "MangoHud"), active=bool(b.config_get("mangohud", False)))
        sec.add(self.mango_card)
        self.goverlay_card = Card("MangoHud / vkBasalt configuration", "Fine-tune the overlay with GOverlay", ("preferences-desktop",), ("goverlay", "vkbasalt"))
        self.goverlay_card.set_control(button("Open GOverlay", on_click=lambda: self._open_or_install(["goverlay"], "goverlay")))
        sec.add(self.goverlay_card)

        # -- Proton-GE
        psec = self.add_section("Proton-GE")
        self.proton_card = Card("Proton-GE (GloriousEggroll)", "Community Proton build used by Steam (compatibility tools) and by lindos-run/umu", ("steam", "applications-games"), ("proton", "ge", "steam", "umu", "update"))
        self.proton_card.add_control(button("List installed", on_click=lambda: self._proton(["list"])))
        self.proton_card.set_control(button("Update Proton-GE", classes=("suggested-action",), on_click=lambda: self._proton(["update"])))
        psec.add(self.proton_card)

        # -- launchers grid
        self.add_section("Launchers")
        self.grid = Gtk.FlowBox()
        self.grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.grid.set_max_children_per_line(4)
        self.grid.set_min_children_per_line(2)
        self.grid.set_row_spacing(8)
        self.grid.set_column_spacing(8)
        self.grid.set_homogeneous(True)
        add_class(self.grid, "launcher-grid")
        self.add_widget(self.grid, 4)
        self.tiles: dict[str, dict[str, Any]] = {}
        for launcher in model.LAUNCHERS:
            self.grid.add(self._tile(launcher))
        self.add_filterable(self._filter_tiles)
        self.add_widget(label("Install runs /usr/libexec/lindos/install-gaming.sh through the Lindos helper (administrator password). Flatpak launchers come from Flathub.", ("dim-label",), wrap=True), 4)

        # -- controllers
        csec = self.add_section("Controllers")
        self.ctrl_card = Card("Connected controllers", "Scanning…", ("input-gaming",), ("controller", "gamepad", "joystick", "xbox", "playstation", "8bitdo"))
        self.ctrl_card.set_control(button("Refresh", on_click=self._refresh_controllers))
        self.ctrl_card.add_control(button("AntiMicroX", on_click=lambda: self._open_or_install(["antimicrox"], "antimicrox"), tooltip="Map controller buttons to keyboard/mouse"))
        csec.add(self.ctrl_card)

        # -- refresh rate
        self.rate_section = self.add_section("Display refresh rate")
        self.rate_cards: dict[str, ComboCard] = {}
        self._build_rate_cards()

        # -- honesty
        hsec = self.add_section("Compatibility")
        info = InfoCard("Anti-cheat reality", model.ANTICHEAT_TEXT, ("dialog-warning",), ("anti-cheat", "valorant", "fortnite", "roblox", "eac", "battleye", "vanguard"))
        links = box("h", 10)
        for text, url in model.ANTICHEAT_LINKS:
            lb = Gtk.LinkButton.new_with_label(url, text)
            add_class(lb, "inline-link")
            links.pack_start(lb, False, False, 0)
        info.add_body(links)
        hsec.add(info)
        compat = Card("Compatibility list", "Which popular games work on Lindos, which are partial and which are not supported yet", ("view-list-details",), ("compatibility", "matrix", "games", "list"))
        compat.set_control(button("Open compatibility list", on_click=self._open_compat))
        hsec.add(compat)

        # -- games that need Windows (SPEC-WINDOWS §30, §32)
        nwsec = self.add_section("Games that need Windows")
        self.region_card = ComboCard(
            "Region for cloud gaming", "Which cloud-gaming providers and Game Pass tiers are offered where you are — taken from the system locale/timezone, or set it here",
            ("preferences-desktop-locale", "network-workgroup"), ("region", "geforce now", "xbox cloud", "cloud gaming", "boosteroid", "luna"),
            options=list(model.REGION_CHOICES), on_change=self._set_region, active_id=model.region_combo_id(b.region()))
        nwsec.add(self.region_card)
        self.needs_windows_section = self.add_section("")
        self.needs_windows_section.add(Card("Checking…", "", ("dialog-information",), ()))
        self._refresh_needs_windows()

    # ------------------------------------------------------------------ toggles
    def _set_cfg(self, key: str, value: bool, title: str) -> None:
        # "mangohud" goes through backend.set_mangohud so `lindos-mangohud sync` updates the
        # per-user MangoHud.conf (no_display) as well as the config key.
        if key == "mangohud":
            work = lambda: self.backend.set_mangohud(bool(value))  # noqa: E731
        else:
            work = lambda: self.backend.config_set(key, bool(value))  # noqa: E731
        run_async(work, lambda ok, exc: self.toast(f"{title} {'on' if value else 'off'}" if ok and not exc else f"Could not save {title}"), name="cfg-" + key)

    def _open_or_install(self, argv: list[str], package: str) -> None:
        kind, value = model.which_or_install(argv, package, self.backend.which)
        if kind == "run":
            self.backend.spawn(value)
            return
        if confirm(self.app.window, f"Install {package}?", f"'{argv[0]}' is not installed. Install package '{package}' now?", "Install"):
            run_async(lambda: self.backend.install_packages([str(value)]), lambda res, exc: self.toast(f"{package} installed" if res is not None and getattr(res, "ok", False) and not exc else f"Install of {package} failed"), name="install-" + package)

    # ------------------------------------------------------------------ proton
    def _proton(self, args: list[str]) -> None:
        if not self.backend.which("lindos-proton"):
            self.toast("lindos-proton (lindos-gaming) is not installed")
            return
        dlg = OutputDialog(self.app.window, "Proton-GE — " + " ".join(args), self.backend)
        dlg.run_argv(["lindos-proton", *args])

    # ------------------------------------------------------------------ launchers
    def _tile(self, launcher: model.Launcher) -> Any:
        frame = Gtk.Frame()
        add_class(frame, "settings-card", "launcher-tile")
        vb = box("v", 6)
        vb.set_margin_top(12)
        vb.set_margin_bottom(12)
        vb.set_margin_start(12)
        vb.set_margin_end(12)
        vb.pack_start(icon_image(launcher.icon, 40, "applications-games"), False, False, 0)
        vb.pack_start(label(launcher.name, ("launcher-name",), xalign=0.5), False, False, 0)
        desc = label(launcher.description, ("dim-label",), xalign=0.5, wrap=True)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_max_width_chars(26)
        vb.pack_start(desc, True, True, 0)
        status = label("", ("dim-label", "launcher-status"), xalign=0.5)
        vb.pack_start(status, False, False, 0)
        btn = button("Install", on_click=lambda: self._launcher_action(launcher.id))
        btn.set_halign(Gtk.Align.CENTER)
        vb.pack_start(btn, False, False, 0)
        if launcher.note:
            frame.set_tooltip_text(launcher.note)
        frame.add(vb)
        self.tiles[launcher.id] = {"frame": frame, "button": btn, "status": status, "installed": False, "run": None, "launcher": launcher}
        return frame

    def _filter_tiles(self, query: str) -> int:
        n = 0
        for lid, t in self.tiles.items():
            ok = model.text_matches(t["launcher"].name + " " + t["launcher"].description + " " + lid, query)
            parent = t["frame"].get_parent()
            if parent is not None:
                parent.set_no_show_all(not ok)
                parent.set_visible(ok)
            n += 1 if ok else 0
        return n

    def _refresh_launchers(self) -> None:
        def _done(states: Any, exc: Optional[BaseException]) -> None:
            if exc or not states:
                return
            for st in states:
                t = self.tiles.get(st["launcher"].id)
                if not t:
                    continue
                t["installed"] = st["installed"]
                t["run"] = st["run"]
                t["button"].set_label("Open" if st["installed"] else "Install")
                t["status"].set_text("Installed" if st["installed"] else "Not installed")
                if st["installed"]:
                    add_class(t["button"], "suggested-action")
                else:
                    t["button"].get_style_context().remove_class("suggested-action")

        run_async(self.backend.launcher_states, _done, name="launcher-states")

    def _launcher_action(self, lid: str) -> None:
        t = self.tiles[lid]
        launcher: model.Launcher = t["launcher"]
        if t["installed"]:
            argv = t["run"] or list(launcher.run)
            if argv and self.backend.spawn(argv):
                self.toast(f"Starting {launcher.name}…")
            else:
                self.toast(f"Could not start {launcher.name}")
            return
        extra = ("\n\n" + launcher.note) if launcher.note else ""
        if not confirm(self.app.window, f"Install {launcher.name}?", f"Runs the Lindos gaming installer for '{lid}' (administrator password required, internet needed).{extra}", "Install"):
            return
        t["button"].set_sensitive(False)
        t["status"].set_text("Installing…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            t["button"].set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if not ok:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Install of {launcher.name} failed: {err[:140] or 'see helper log'}")
                if err:
                    dlg = OutputDialog(self.app.window, f"Install {launcher.name} — output", self.backend)
                    dlg.set_text((getattr(res, "out", "") or "") + "\n" + (getattr(res, "err", "") or ""))
                    dlg.set_status("Failed", False)
            else:
                self.toast(f"{launcher.name} installed")
            self._refresh_launchers()

        run_async(lambda: self.backend.install_gaming([lid]), _done, name="install-gaming-" + lid)

    # ------------------------------------------------------------------ controllers
    def _refresh_controllers(self) -> None:
        def _done(found: Any, exc: Optional[BaseException]) -> None:
            if exc or not found:
                self.ctrl_card.set_subtitle("No controller detected — plug in or pair (Bluetooth) a controller and press Refresh. Xbox/PS/8BitDo/Switch pads work out of the box (udev rules from lindos-gaming).")
                return
            names = []
            for c in found:
                if c["device"].startswith("/dev/input/js"):
                    names.append(f"{c['name']} ({c['device']})")
            if not names:
                names = [c["name"] for c in found]
            self.ctrl_card.set_subtitle("; ".join(dict.fromkeys(names)))

        run_async(self.backend.controllers, _done, name="controllers")

    # ------------------------------------------------------------------ refresh rate
    def _build_rate_cards(self) -> None:
        def _done(outputs: Any, exc: Optional[BaseException]) -> None:
            outputs = outputs or []
            if not outputs:
                if not self.rate_cards:
                    self.rate_section.add(Card("Refresh rate", "No display information (xrandr unavailable — Wayland session or headless?)", ("video-display",), ("refresh", "hz")))
                return
            for o in outputs:
                name = o["output"]
                rates = o.get("rates") or []
                opts = [(r, f"{float(r):g} Hz") for r in rates]
                if name in self.rate_cards:
                    self.rate_cards[name].set_options(opts, o.get("current"))
                    continue
                card = ComboCard(f"Monitor {name}", (o.get("resolution") + " · " if o.get("resolution") else "") + "Higher refresh rate = smoother motion; the panel must support it", ("video-display",), ("refresh", "hz", "monitor", name.lower()), options=opts, on_change=lambda r, n=name: self._set_rate(n, r), active_id=o.get("current"))
                self.rate_cards[name] = card
                self.rate_section.add(card)
            self.rate_section.show_all()

        run_async(self.backend.refresh_rates, _done, name="refresh-rates")

    def _set_rate(self, output: str, rate: str) -> None:
        def _done(res: Any, exc: Optional[BaseException]) -> None:
            if exc or res is None or not res.ok:
                self.toast(f"xrandr could not set {rate} Hz on {output}: {(getattr(res, 'err', '') or str(exc)).strip()[:120]}")
            else:
                self.toast(f"{output}: {float(rate):g} Hz applied (use Display settings to make it permanent)")

        run_async(lambda: self.backend.set_refresh_rate(output, rate), _done, name="xrandr-rate")

    # ------------------------------------------------------------------ compat list
    def _open_compat(self) -> None:
        CompatDialog(self.app, self.backend)

    # ------------------------------------------------------------------ games that need Windows
    def _set_region(self, region_id: str) -> None:
        value = model.region_config_value(region_id)
        run_async(lambda: self.backend.set_region(value),
                 lambda ok, exc: None if ok and not exc else self.toast("Could not save the region"),
                 name="set-region")

    def _needs_windows_data(self) -> tuple[list, dict]:
        return self.backend.not_possible_games(), self.backend.compat_disclaimer()

    def _refresh_needs_windows(self) -> None:
        def _done(data: Any, exc: Optional[BaseException]) -> None:
            self.needs_windows_section.clear()
            games, disclaimer = data if data else ([], {})
            games = games or []
            if disclaimer.get("long"):
                # SPEC 0.1: "Not supported yet" - publishers decide, no date is promised
                self.needs_windows_section.add(InfoCard(disclaimer.get("badge") or "Not supported yet", disclaimer["long"], ("dialog-information",), ("not supported yet", "anti-cheat", "publisher")))
            if exc:
                self.needs_windows_section.add(Card("Could not read the compatibility list", str(exc), ("dialog-error",), ()))
            elif not games:
                self.needs_windows_section.add(Card("Nothing needs Windows right now", "Every title Lindos knows about runs here.", ("emblem-ok-symbolic", "applications-games"), ()))
            else:
                for g in games:
                    name = str(g.get("name") or "")
                    if not name:
                        continue
                    card = Card(name, g.get("reason") or "", ("dialog-warning", "applications-games"), (name.lower(), "windows", "restart into windows", "cloud"))
                    card.set_control(button("Routes…", on_click=lambda n=name: self._open_routes(n)))
                    if g.get("kind") and disclaimer.get("badge"):
                        card.add_control(_not_supported_badge(disclaimer))
                    self.needs_windows_section.add(card)
            self.needs_windows_section.show_all()

        run_async(self._needs_windows_data, _done, name="not-possible-games")

    def _open_routes(self, title: str) -> None:
        RoutesDialog(self.app, self.backend, title)

    def on_show(self) -> None:
        b = self.backend
        self.gamemode_card.set_active_silent(bool(b.config_get("gamemode_auto", True)))
        self.mango_card.set_active_silent(bool(b.config_get("mangohud", False)))
        self.region_card.set_active_id_silent(model.region_combo_id(b.region()))
        self._refresh_launchers()
        self._refresh_controllers()
        self._build_rate_cards()
        self._refresh_needs_windows()


class CompatDialog:
    """Renders /usr/share/lindos/compat-matrix.json with status badges."""

    def __init__(self, app: Any, backend: Any):
        self.dialog = Gtk.Dialog(title="Game compatibility on Lindos", transient_for=app.window, modal=False)
        self.dialog.set_default_size(820, 600)
        add_class(self.dialog, "compat-dialog")
        area = self.dialog.get_content_area()
        area.set_spacing(6)
        area.set_margin_top(10)
        area.set_margin_start(12)
        area.set_margin_end(12)
        area.pack_start(label("Status: Native / Works (Proton) / Partial / Not supported yet (kernel anti-cheat: Valorant, Fortnite, LoL … — up to their publishers, no date) / Not possible (Windows-only store apps). Same data as docs/COMPATIBILITY.md.", ("dim-label",), wrap=True), False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Filter games…")
        area.pack_start(self.search, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        add_class(self.listbox, "settings-cards")
        sw.add(self.listbox)
        area.pack_start(sw, True, True, 0)
        self.dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self.dialog.connect("response", lambda d, *_: d.destroy())
        self.rows: list[tuple[Any, str]] = []
        entries = backend.compat_matrix()
        self.disclaimer = backend.compat_disclaimer()
        if not entries:
            self.listbox.add(Card("Compatibility data not found", f"Expected {model.COMPAT_MATRIX_JSON} (shipped by lindos-gaming)", ("dialog-warning",)).row)
        for e in entries:
            row = self._row(e, backend)
            self.listbox.add(row)
            self.rows.append((row, " ".join([e["name"], e["status"], e["reason"], e["how"], self._badge_text(e)]).lower()))
        self.search.connect("search-changed", self._filter)
        self.dialog.show_all()

    def _badge_text(self, e: dict[str, str]) -> str:
        return str(self.disclaimer.get("badge") or "") if e.get("kind") else ""

    def _row(self, e: dict[str, str], backend: Any) -> Any:
        sub = e["reason"] + ((" — via " + e["how"]) if e["how"] and e["how"].lower() not in e["reason"].lower() else "")
        card = Card(e["name"], sub, ("applications-games",), (e["status"],))
        if e.get("kind") and self.disclaimer.get("badge"):
            card.add_control(_not_supported_badge(self.disclaimer))
        else:
            card.add_control(badge(e["status"]))
        if e["link"]:
            lb = Gtk.LinkButton.new_with_label(e["link"], "Details")
            card.set_control(lb)
        return card.row

    def _filter(self, entry: Any) -> None:
        q = model.normalize_query(entry.get_text())
        for row, text in self.rows:
            ok = model.text_matches(text, q)
            row.set_no_show_all(not ok)
            row.set_visible(ok)


_ROUTE_ICONS: dict[str, tuple] = {
    "cloud": ("network-server", "applications-internet"),
    "windows": ("computer", "system-reboot"),
    "vm": ("computer-symbolic",),
    "proton": ("wine",),
    "native": ("applications-games",),
}


class RoutesDialog:
    """Per-title honest routes (SPEC-WINDOWS §30.2): official cloud streaming
    (``lindos-game cloud install geforce-now``) or a one-shot restart into Windows
    (``lindos-dualboot reboot-to-windows``, re-validated as root before it does anything). The
    VM route is shown as information only — Lindos never offers it for a blocked title."""

    def __init__(self, app: Any, backend: Any, title: str):
        self.app = app
        self.backend = backend
        self.title = title
        self.dialog = Gtk.Dialog(title=f"{title} — how to play it", transient_for=app.window, modal=False)
        self.dialog.set_default_size(680, 500)
        add_class(self.dialog, "routes-dialog")
        area = self.dialog.get_content_area()
        area.set_spacing(8)
        area.set_margin_top(10)
        area.set_margin_start(12)
        area.set_margin_end(12)
        self.status_label = label("Looking up routes…", ("dim-label",), wrap=True)
        area.pack_start(self.status_label, False, False, 0)
        self.boot_label = label("", ("dim-label",), wrap=True)
        area.pack_start(self.boot_label, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        add_class(self.listbox, "settings-cards")
        sw.add(self.listbox)
        area.pack_start(sw, True, True, 0)
        self.dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self.dialog.connect("response", lambda d, *_: d.destroy())
        self.dialog.show_all()
        self._route: Optional[Dict[str, Any]] = None
        self._boot: Optional[Dict[str, Any]] = None
        run_async(lambda: backend.game_route(title), self._route_loaded, name="game-route")
        run_async(backend.dualboot_status, self._boot_loaded, name="dualboot-status")

    def _route_loaded(self, data: Any, exc: Optional[BaseException]) -> None:
        self._route = data if isinstance(data, dict) else {}
        if exc:
            self.status_label.set_text(f"Could not look up routes: {exc}")
        self._render()

    def _boot_loaded(self, data: Any, exc: Optional[BaseException]) -> None:
        self._boot = data if isinstance(data, dict) else {}
        self._render()

    def _render(self) -> None:
        if self._route is None:
            return
        for child in list(self.listbox.get_children()):
            self.listbox.remove(child)
        route = self._route
        boot = self._boot or {}
        routes = route.get("routes") or []
        anticheat = route.get("anticheat") or "—"
        region = route.get("region") or "unknown"
        self.status_label.set_text(f"Anti-cheat: {anticheat}   ·   Region used: {region}")
        self.boot_label.set_text("Dual boot: " + (model.dualboot_summary(boot) if self._boot is not None else "checking…"))
        dis = route.get("disclaimer") or {}
        if dis.get("short"):
            why = (" " + dis["cause"]) if dis.get("cause") else ""
            self.listbox.add(InfoCard(dis.get("badge") or "Not supported yet", dis["short"] + why, ("dialog-information",)).row)
        if not routes:
            notes = [n for n in (route.get("notes") or []) if n]
            self.listbox.add(Card("No routes available", "; ".join(notes) or "Lindos has nothing more to suggest for this title.", ("dialog-warning",)).row)
        else:
            for note in route.get("notes") or []:
                if note:
                    self.listbox.add(InfoCard("Why", note, ("dialog-information",)).row)
            for r in routes:
                self.listbox.add(self._route_row(r, boot))
        self.listbox.show_all()

    def _route_row(self, route: Dict[str, Any], boot: Dict[str, Any]) -> Any:
        rtype = str(route.get("type") or "")
        provider = route.get("provider")
        title_text = str(route.get("label") or rtype) + (f" ({provider})" if provider else "")
        card = Card(title_text, str(route.get("why") or ""), _ROUTE_ICONS.get(rtype, ("applications-games",)), (rtype, provider or ""))
        card.add_control(badge("works", "Available") if route.get("available") else badge("unknown", "Not available"))
        requires = route.get("requires") or []
        if requires:
            card.add_body(label("Needs: " + ", ".join(requires), ("dim-label",), wrap=True))
        if rtype == "cloud" and provider == "geforce-now" and not route.get("available"):
            card.set_control(button("Install GeForce NOW", classes=("suggested-action",), on_click=self._install_geforce_now))
        elif rtype == "windows":
            boot_ready = self._boot is not None
            can_reboot = bool(route.get("available")) and boot_ready and bool(boot.get("can_reboot_to_windows"))
            btn = button("Restart into Windows", classes=("suggested-action",) if can_reboot else (), on_click=self._restart_to_windows)
            btn.set_sensitive(can_reboot)
            if not can_reboot:
                reason = ("Checking whether Lindos can restart into Windows…" if not boot_ready else
                         str(route.get("why") or boot.get("why") or "Not available on this PC"))
                btn.set_tooltip_text(reason)
            card.set_control(btn)
        return card.row

    def _install_geforce_now(self) -> None:
        if not self.backend.which("lindos-game"):
            self.app.toast("lindos-game (lindos-gaming) is not installed")
            return
        if not confirm(self.dialog, "Install GeForce NOW?",
                       "Installs NVIDIA's official Flatpak (Flathub remote GeForceNOW) through the Lindos helper. "
                       "Needs an internet connection and the administrator password.", "Install"):
            return
        self.app.toast("Installing GeForce NOW…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.app.toast("GeForce NOW installed")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.app.toast(f"Install failed: {err[:140] or 'see helper log'}")
                if err:
                    dlg = OutputDialog(self.dialog, "Install GeForce NOW — output", self.backend)
                    dlg.set_text((getattr(res, "out", "") or "") + "\n" + (getattr(res, "err", "") or ""))
                    dlg.set_status("Failed", False)

        run_async(self.backend.install_geforce_now, _done, name="install-geforce-now")

    def _restart_to_windows(self) -> None:
        if not self.backend.which("lindos-dualboot"):
            self.app.toast("lindos-dualboot (lindos-core) is not installed")
            return
        if not confirm(self.dialog, "Restart into Windows?",
                       f"This restarts the PC once into Windows so you can play {self.title}. Lindos boots "
                       "normally again next time — nothing about your boot order is changed permanently. "
                       "Save your work first.", "Restart", destructive=True):
            return
        self.app.toast("Restarting into Windows…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            ok = res is not None and getattr(res, "ok", False) and not exc
            if not ok:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.app.toast(f"Could not restart into Windows: {err[:160] or 'see log'}")

        run_async(self.backend.reboot_to_windows, _done, name="reboot-to-windows")


__all__ = ["GamingPage", "CompatDialog", "RoutesDialog"]
