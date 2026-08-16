"""Windows apps page: installed Windows programs (APPS_DB) with Run / Open C:\\ / winecfg /
Uninstall, plus toolbar actions 'Install a Windows program…' (→ lindos-run), 'Recipes…'
(→ lindos-compat recipes apply) and 'Doctor' (→ lindos-compat doctor)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, Card, InfoCard, OutputDialog, PageBase, add_class, badge, box, button, choose_file, confirm, label, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.windows_apps")


class WindowsAppsPage(PageBase):
    PAGE_ID = "windows-apps"

    def build(self) -> None:
        # -- toolbar
        bar = box("h", 8)
        bar.set_margin_top(4)
        self.install_btn = button("Install a Windows program…", "document-open", ("suggested-action",), self._install_program, "Choose an .exe or .msi — it runs through lindos-run")
        self.recipes_btn = button("Recipes…", "x-office-address-book", (), self._recipes, "Ready-made setups for Photoshop, Office, Notepad++ …")
        self.doctor_btn = button("Doctor", "dialog-information", (), self._doctor, "Check Wine, umu, Vulkan and 32-bit libraries")
        self.refresh_btn = button("", "view-refresh", (), self.refresh, "Refresh list")
        bar.pack_start(self.install_btn, False, False, 0)
        bar.pack_start(self.recipes_btn, False, False, 0)
        bar.pack_start(self.doctor_btn, False, False, 0)
        bar.pack_end(self.refresh_btn, False, False, 0)
        self.add_widget(bar, 0)

        # -- honesty note + Windows app support setup (helper install-compat → install-compat.sh)
        note = self.add_section("")
        note.add(InfoCard("How Windows programs run on Lindos", model.WINE_HONESTY_TEXT + " Games and unknown programs use Proton-GE (umu); installers and desktop apps use Wine; creative recipes use Bottles.", ("dialog-information",), ("wine", "proton", "honest", "not windows")))
        self.support_card = Card("Windows app support (Wine + Proton)", "Checking…", ("wine", "system-software-install"), ("wine", "proton", "umu", "install", "setup", "compat"))
        self.support_btn = button("Install / repair", "system-software-install", ("suggested-action",), self._install_support, "Runs /usr/libexec/lindos/install-compat.sh through the Lindos helper (administrator password; downloads Wine, winetricks, 32-bit libraries and umu-launcher)")
        self.support_card.set_control(self.support_btn)
        note.add(self.support_card)
        self._refresh_support()

        # -- app list
        self.list = self.add_section("Installed Windows programs")
        self.empty_card = Card("No Windows programs yet", "Use 'Install a Windows program…' or double-click any .exe/.msi in File Explorer.", ("wine", "application-x-executable"), ("empty",))
        self.list.add(self.empty_card)
        self.app_cards: list[Card] = []
        self.refresh()

    # ------------------------------------------------------------------ list
    def refresh(self) -> None:
        def _done(entries: Any, exc: Optional[BaseException]) -> None:
            for c in self.app_cards:
                self.list.widget.remove(c.row)
                if c in self.list.cards:
                    self.list.cards.remove(c)
            self.app_cards.clear()
            entries = entries or []
            self.empty_card.set_visible(not entries)
            for e in entries:
                card = self._app_card(e)
                self.list.add(card)
                self.app_cards.append(card)
            self.list.widget.show_all()
            if exc:
                self.toast(f"Could not read the Windows apps list: {exc}")

        run_async(self.backend.apps_db_load, _done, name="apps-db")

    def _app_card(self, e: dict[str, Any]) -> Card:
        sub_parts = [f"Runner: {e['runner']}", f"Prefix: {e['prefix']}"]
        if e.get("last_used"):
            sub_parts.append(f"Last used: {e['last_used'][:19].replace('T', ' ')}")
        elif e.get("installed_at"):
            sub_parts.append(f"Installed: {e['installed_at'][:19].replace('T', ' ')}")
        icon = ("lindos-exe", "wine", "application-x-ms-dos-executable", "application-x-executable")
        card = Card(e["name"], " · ".join(sub_parts), icon, ("windows", e["slug"], e["runner"], e.get("kind", "")))
        if e.get("kind") in ("game", "installer", "msi"):
            card.add_control(label(str(e["kind"]), ("badge", "badge-neutral")))
        run_b = button("Run", "media-playback-start", (), lambda ent=e: self._run(ent), "Launch with lindos-run")
        c_b = button("Open C:\\", "folder", (), lambda ent=e: self._open_c(ent), "Open the prefix drive_c folder")
        cfg_b = button("winecfg", "preferences-system", (), lambda ent=e: self._winecfg(ent), "Wine configuration for this prefix")
        del_b = button("Uninstall", "user-trash", ("destructive-action",), lambda ent=e: self._uninstall(ent), "Remove the prefix and shortcuts")
        for b in (run_b, c_b, cfg_b, del_b):
            card.add_control(b)
        card.on_activate(lambda ent=e: self._run(ent))
        return card

    # ------------------------------------------------------------------ actions
    def _run(self, e: dict[str, Any]) -> None:
        if not self.backend.which("lindos-run"):
            self.toast("lindos-run (lindos-compat) is not installed")
            return
        exe = e.get("exe") or ""
        if not exe:
            self.toast("No executable recorded for this app")
            return
        ok = self.backend.run_windows_app(exe, e.get("slug", ""))
        self.toast(f"Starting {e['name']}…" if ok else f"Could not start {e['name']}")

    def _open_c(self, e: dict[str, Any]) -> None:
        if not self.backend.open_c_drive(e.get("prefix") or e.get("slug", "")):
            self.toast("C:\\ drive folder not found for this prefix")

    def _winecfg(self, e: dict[str, Any]) -> None:
        if not self.backend.winecfg(e.get("slug", ""), e.get("prefix") or e.get("slug", "")):
            self.toast("winecfg not available (Wine not installed?)")

    def _uninstall(self, e: dict[str, Any]) -> None:
        slug = e.get("slug", "")
        if not confirm(self.app.window, f"Uninstall {e['name']}?", f"This removes the Wine prefix '{e['prefix']}' (all its files under drive_c) and the app's shortcuts. This cannot be undone.", "Uninstall", destructive=True):
            return
        if not self.backend.which("lindos-compat"):
            self.toast("lindos-compat is not installed — cannot remove the prefix safely")
            return
        dlg = OutputDialog(self.app.window, f"Uninstalling {e['name']}", self.backend)
        dlg.run_argv(["lindos-compat", "prefixes", "remove", slug], on_finished=lambda code: (self.refresh(), self.toast("Removed" if code == 0 else "Removal reported an error")))

    def _install_program(self) -> None:
        path = choose_file(self.app.window, "Install a Windows program", [("Windows programs (*.exe, *.msi)", ["*.exe", "*.EXE", "*.msi", "*.MSI"]), ("Batch / shortcuts", ["*.bat", "*.lnk"]), ("All files", ["*"])], os.path.expanduser("~/Downloads") if os.path.isdir(os.path.expanduser("~/Downloads")) else None)
        if not path:
            return
        if not self.backend.which("lindos-run"):
            self.toast("lindos-run (lindos-compat) is not installed — install 'lindos-compat' first")
            return
        if self.backend.spawn(["lindos-run", path]):
            self.toast(f"Starting {os.path.basename(path)} through Wine/Proton… refresh the list when the installer finishes")
        else:
            self.toast("Could not start lindos-run")

    # ------------------------------------------------------------------ Windows app support
    def _refresh_support(self) -> None:
        def _done(st: Any, exc: Optional[BaseException]) -> None:
            st = st or {}
            if exc or not isinstance(st, dict):
                self.support_card.set_subtitle("Status unknown — use Doctor for details")
                return
            parts = [f"Wine: {'installed' if st.get('wine') else 'missing'}",
                     f"Proton (umu-run): {'installed' if st.get('umu') else 'missing'}",
                     f"winetricks: {'installed' if st.get('winetricks') else 'missing'}"]
            if not st.get("lindos_run"):
                parts.append("lindos-run: missing (package lindos-compat)")
            self.support_card.set_subtitle(" · ".join(parts))

        run_async(self.backend.compat_status, _done, name="compat-status")

    def _install_support(self) -> None:
        if not confirm(self.app.window, "Install Windows app support?",
                       "Installs Wine (staging), winetricks, 32-bit Vulkan/GL libraries and umu-launcher (Proton-GE runner) "
                       "through the Lindos helper. Needs an internet connection and the administrator password.", "Install"):
            return
        self.support_btn.set_sensitive(False)
        self.support_card.set_subtitle("Installing… this downloads a few hundred MB")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.support_btn.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.toast("Windows app support installed")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Install failed: {err[:140] or 'see /var/log/lindos/helper.log'}")
                if err:
                    dlg = OutputDialog(self.app.window, "Windows app support — output", self.backend)
                    dlg.set_text((getattr(res, "out", "") or "") + "\n" + (getattr(res, "err", "") or ""))
                    dlg.set_status("Failed", False)
            self._refresh_support()

        run_async(lambda: self.backend.install_compat(["wine", "umu"]), _done, name="install-compat")

    def _doctor(self) -> None:
        if not self.backend.which("lindos-compat"):
            self.toast("lindos-compat is not installed")
            return
        dlg = OutputDialog(self.app.window, "Windows compatibility doctor", self.backend)
        dlg.run_argv(["lindos-compat", "doctor"])

    def _recipes(self) -> None:
        RecipesDialog(self.app, self.backend)


class RecipesDialog:
    """Lists /usr/share/lindos/recipes/*.json with status badges; Apply → lindos-compat recipes apply."""

    def __init__(self, app: Any, backend: Any):
        self.app = app
        self.backend = backend
        self.dialog = Gtk.Dialog(title="Recipes — ready-made Windows app setups", transient_for=app.window, modal=False)
        self.dialog.set_default_size(760, 560)
        add_class(self.dialog, "recipes-dialog")
        area = self.dialog.get_content_area()
        area.set_spacing(6)
        area.set_margin_top(10)
        area.set_margin_start(12)
        area.set_margin_end(12)
        area.pack_start(label("Status is honest: 'Works' runs well, 'Partial' has known issues, 'Broken' does not work — the notes suggest native alternatives.", ("dim-label",), wrap=True), False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        add_class(self.listbox, "settings-cards")
        sw.add(self.listbox)
        area.pack_start(sw, True, True, 0)
        self.dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        self.dialog.connect("response", lambda d, *_: d.destroy())
        recipes = backend.recipes()
        if not recipes:
            self.listbox.add(Card("No recipes found", f"Expected *.json in {backend.recipes_dir()} (shipped by lindos-compat)", ("dialog-warning",)).row)
        for rec in recipes:
            self.listbox.add(self._row(rec))
        self.dialog.show_all()

    def _row(self, rec: dict[str, Any]) -> Any:
        sub = " · ".join(p for p in (rec.get("vendor"), rec.get("category"), ("runner: " + rec["runner"]) if rec.get("runner") else "") if p)
        card = Card(rec["name"], sub, ("wine", "application-x-executable"), (rec["id"], rec["status"]))
        card.add_control(badge(rec["status"]))
        if rec.get("notes"):
            card.add_body(label(rec["notes"], ("dim-label",), wrap=True))
        apply_btn = button("Apply", on_click=lambda r=rec: self._apply(r))
        if rec["status"] in ("broken", "not-possible"):
            apply_btn.set_sensitive(False)
            apply_btn.set_tooltip_text("This recipe is marked broken — see the notes for alternatives")
        card.set_control(apply_btn)
        return card.row

    def _apply(self, rec: dict[str, Any]) -> None:
        if not self.backend.which("lindos-compat"):
            self.app.toast("lindos-compat is not installed")
            return
        if not confirm(self.dialog, f"Apply recipe '{rec['name']}'?", "This creates/configures a Wine prefix (winetricks components may be downloaded). You will then be asked for the installer file by the recipe if needed.", "Apply"):
            return
        out = OutputDialog(self.dialog, f"Applying recipe: {rec['name']}", self.backend)
        out.run_argv(["lindos-compat", "recipes", "apply", rec["id"]])


__all__ = ["WindowsAppsPage", "RecipesDialog"]
