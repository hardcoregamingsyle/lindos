"""Updates page (SPEC-UPDATE §37): two honestly-separate channels.

"Operating system & apps" is a single button that opens the system's Update Manager
(``mintupdate``) -- unchanged, already works, never touched here. "Lindos components" lists
every ``lindos-*`` package's installed/available version from ``lindos-update check --json``,
with a "Check now" (privileged ``apt-get-update`` refresh + re-check) and an "Update now" button
that only appears once there is something to install; when no apt repo is configured yet, a
plain-language note plus a "Load updates from a folder…" picker calls ``lindos-update sideload``
instead. A Kernel card shows booted vs. installed vs. available kernel version, the
Secure-Boot-signed indicator (``lindos-kernel secureboot status``, already shipped by
Addendum W) and an "Apply now (needs a restart)" button when a newer kernel is available.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import model
from ..widgets import Card, InfoCard, OutputDialog, PageBase, button, choose_folder, confirm, run_async

log = logging.getLogger("lindos.settings.updates")


class UpdatesPage(PageBase):
    PAGE_ID = "updates"

    def build(self) -> None:
        # -- state (rendered by the *_render_* helpers below; kept as plain dicts so the
        # visibility/summary logic stays in lindos_settings.model, testable without GTK)
        self._status: dict[str, Any] = model.normalize_update_status({})
        self._kernel: dict[str, Any] = model.normalize_kernel_status({})
        self._secureboot: dict[str, Any] = model.normalize_secureboot_status({})

        # -- Operating system & apps (mintupdate -- unchanged, already works)
        osec = self.add_section("Operating system & apps")
        self.os_card = Card(
            "Operating system & apps",
            "System packages, Firefox, Wine and everything else update through the system Update Manager",
            ("system-software-update",), ("mintupdate", "apt", "system", "operating system"),
        )
        self.os_card.set_control(button("Open Update Manager", "system-software-update", ("suggested-action",), self._open_update_manager))
        osec.add(self.os_card)

        # -- Lindos components
        lsec = self.add_section("Lindos components")
        self.components_card = Card(
            "Lindos components", "Checking…",
            ("lindos-start", "package-x-generic"), ("lindos-update", "package", "version"),
        )
        self.check_btn = button("Check now", "view-refresh", (), self._check_now, "Refreshes the package cache (administrator password) and re-checks")
        self.update_btn = button("Update now", "system-software-update", ("suggested-action",), self._update_now, "Installs the exact versions below through the Lindos helper")
        self.update_btn.set_no_show_all(True)
        self.update_btn.set_visible(False)
        self.components_card.add_control(self.check_btn)
        self.components_card.set_control(self.update_btn)
        lsec.add(self.components_card)

        self.list_section = self.add_section("")
        self.no_updates_card = Card("Everything is up to date", "", ("emblem-ok-symbolic",), ())
        self.list_section.add(self.no_updates_card)
        self.package_cards: list[Card] = []

        # -- sideload note + folder picker (only shown when no apt repo is configured/reachable)
        self.sideload_section = self.add_section("")
        self.sideload_card = InfoCard(
            "No Lindos update channel is configured yet", "",
            ("dialog-information",), ("sideload", "repo", "folder", ".deb"),
        )
        self.sideload_btn = button("Load updates from a folder…", "folder-open", (), self._pick_sideload_folder, "Installs any lindos-*.deb files found in the chosen folder")
        self.sideload_card.set_control(self.sideload_btn)
        self.sideload_section.add(self.sideload_card)
        self.sideload_section.widget.set_no_show_all(True)
        self.sideload_section.widget.set_visible(False)

        # -- Kernel
        ksec = self.add_section("Kernel")
        self.kernel_card = Card(
            "Lindos kernel", "Checking…",
            ("lindos-start", "computer"), ("kernel", "linux", "secure boot", "restart"),
        )
        self.kernel_apply_btn = button("Apply now (needs a restart)", "system-reboot", ("suggested-action",), self._apply_kernel)
        self.kernel_apply_btn.set_no_show_all(True)
        self.kernel_apply_btn.set_visible(False)
        self.kernel_card.set_control(self.kernel_apply_btn)
        ksec.add(self.kernel_card)

        self.refresh()

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ operating system & apps
    def _open_update_manager(self) -> None:
        if not self.backend.which("mintupdate"):
            self.toast("mintupdate is not installed")
            return
        if self.backend.spawn(["mintupdate"]):
            self.toast("Opening Update Manager…")
        else:
            self.toast("Could not start Update Manager")

    # ------------------------------------------------------------------ Lindos components
    def refresh(self) -> None:
        self.components_card.set_subtitle("Checking…")
        self.kernel_card.set_subtitle("Checking…")
        run_async(self.backend.update_check, self._check_done, name="update-check")
        run_async(self.backend.update_kernel_status, self._kernel_done, name="update-kernel-status")
        run_async(self.backend.secureboot_status, self._secureboot_done, name="secureboot-status")

    def _check_done(self, status: Any, exc: Optional[BaseException]) -> None:
        self._status = status if isinstance(status, dict) else model.normalize_update_status({})
        if exc and not self._status.get("error"):
            self._status = dict(self._status, error=str(exc))
        self._render_components()

    def _kernel_done(self, kernel: Any, exc: Optional[BaseException]) -> None:
        self._kernel = kernel if isinstance(kernel, dict) else model.normalize_kernel_status({})
        if exc and not self._kernel.get("error"):
            self._kernel = dict(self._kernel, error=str(exc))
        self._render_kernel()

    def _secureboot_done(self, sb: Any, exc: Optional[BaseException]) -> None:
        self._secureboot = sb if isinstance(sb, dict) else model.normalize_secureboot_status({})
        if exc and not self._secureboot.get("error"):
            self._secureboot = dict(self._secureboot, error=str(exc))
        self._render_kernel()

    def _render_components(self) -> None:
        status = self._status
        self.components_card.set_subtitle(model.update_status_summary(status))

        for c in self.package_cards:
            self.list_section.widget.remove(c.row)
            if c in self.list_section.cards:
                self.list_section.cards.remove(c)
        self.package_cards.clear()
        updates = status.get("lindos_updates") or []
        self.no_updates_card.set_visible(not updates)
        for u in updates:
            installed = u.get("installed") or "(not installed)"
            card = Card(u["name"], f"{installed} → {u['candidate']}", ("package-x-generic",), (u["name"],))
            self.list_section.add(card)
            self.package_cards.append(card)
        self.list_section.show_all()

        has_updates = bool(model.lindos_update_payload(status))
        self.update_btn.set_no_show_all(not has_updates)
        self.update_btn.set_visible(has_updates)

        show_sideload = model.needs_sideload_note(status)
        self.sideload_section.widget.set_no_show_all(not show_sideload)
        self.sideload_section.widget.set_visible(show_sideload)
        if show_sideload:
            self.sideload_card.body_label.set_text(model.sideload_note_text(status))

    def _check_now(self) -> None:
        self.check_btn.set_sensitive(False)
        self.components_card.set_subtitle("Refreshing the package cache… this may ask for the administrator password")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.check_btn.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if not ok:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Could not refresh: {err[:140] or 'see helper log'}")
            self.refresh()

        run_async(self.backend.update_refresh, _done, name="update-refresh")

    def _update_now(self) -> None:
        packages = model.lindos_update_payload(self._status)
        if not packages:
            self.toast("Nothing to update")
            return
        names = ", ".join(p.split("=", 1)[0] for p in packages)
        if not confirm(self.app.window, "Update Lindos components?",
                       f"Installs these exact versions through the Lindos helper (administrator password required):\n\n{names}", "Update"):
            return
        self.update_btn.set_sensitive(False)
        self.components_card.set_subtitle("Updating…")

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.update_btn.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.toast("Lindos components updated")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Update failed: {err[:160] or 'see helper log'}")
            self.refresh()

        run_async(lambda: self.backend.update_apply(packages, allow_kernel=False), _done, name="update-apply")

    # ------------------------------------------------------------------ sideload (no repo yet)
    def _pick_sideload_folder(self) -> None:
        path = choose_folder(self.app.window, "Load Lindos updates from a folder")
        if not path:
            return
        if not self.backend.which("lindos-update"):
            self.toast("lindos-update (lindos-core) is not installed")
            return
        if not confirm(self.app.window, "Load updates from this folder?",
                       f"Installs every lindos-*.deb found in:\n{path}\n\nthrough the Lindos helper (administrator password required).", "Load"):
            return
        dlg = OutputDialog(self.app.window, "Loading updates from " + path, self.backend)
        dlg.run_argv(self.backend.sideload_argv(path), on_finished=lambda code: (self.refresh(), self.toast("Updates installed" if code == 0 else "Sideload reported an error — see the output above")))

    # ------------------------------------------------------------------ kernel
    def _render_kernel(self) -> None:
        text = model.kernel_status_summary(self._kernel) + "  ·  " + model.secureboot_summary(self._secureboot)
        self.kernel_card.set_subtitle(text)
        can_apply = bool(model.kernel_update_payload(self._kernel))
        self.kernel_apply_btn.set_no_show_all(not can_apply)
        self.kernel_apply_btn.set_visible(can_apply)

    def _apply_kernel(self) -> None:
        packages = model.kernel_update_payload(self._kernel)
        if not packages:
            self.toast("No kernel update available")
            return
        if not confirm(self.app.window, "Install the new Lindos kernel?",
                       f"Installs {packages[0]} through the Lindos helper (administrator password required). "
                       "The new kernel is used after your next restart; the current one stays available from the boot menu.", "Install"):
            return
        self.kernel_apply_btn.set_sensitive(False)

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.kernel_apply_btn.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.toast("Kernel installed — restart to use it")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"Kernel install failed: {err[:160] or 'see helper log'}")
            self.refresh()

        run_async(lambda: self.backend.update_apply(packages, allow_kernel=True), _done, name="update-apply-kernel")


__all__ = ["UpdatesPage"]
