"""About page: Win11-style 'Device specifications' and 'Lindos specifications' cards
(Lindos version, Mint base, kernel, XFCE, CPU, GPU, RAM, disk, mode, browser) + Copy button."""

from __future__ import annotations

import logging
import platform
from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, ButtonCard, Card, KeyValueGrid, PageBase, box, button, clipboard_set, label, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.about")


def collect_specs(backend: Any, modes: Optional[dict[str, Any]] = None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Gather (device_rows, lindos_rows) — runs in a worker thread (subprocess calls)."""
    b = backend
    cpu = b.cpu_info()
    gpus = b.gpu_info()
    ram = b.ram_info()
    osr = b.os_release()
    du = b.disk_usage("/")

    cores = ""
    if cpu.get("cores") and cpu.get("threads") and cpu["cores"] != cpu["threads"]:
        cores = f" ({cpu['cores']} cores / {cpu['threads']} threads)"
    elif cpu.get("threads"):
        cores = f" ({cpu['threads']} threads)"
    gpu_text = "; ".join(f"{g['model']}" + (f" [{g['driver']}]" if g.get("driver") else "") for g in gpus) or "Not detected"
    ram_text = f"{model.format_mb(ram.get('total'))} total · {model.format_mb(ram.get('used'))} used · {model.format_mb(ram.get('available'))} available"
    disk_text = f"{model.format_bytes(du[1])} used of {model.format_bytes(du[0])} ({model.format_bytes(du[2])} free)" if du else "—"

    device_rows = [
        ("Device name", b.hostname()),
        ("Processor", f"{cpu['model']}{cores}"),
        ("Graphics", gpu_text),
        ("Installed RAM", ram_text),
        ("Storage (/)", disk_text),
        ("System type", f"{platform.machine() or 'x86_64'} operating system, {cpu.get('arch') or platform.machine() or 'x64'} processor"),
        ("Battery", "Present" if b.battery_present() else "None (desktop)"),
        ("Uptime", model.format_uptime(b.uptime_seconds())),
    ]

    mint = osr.get("PRETTY_NAME") or osr.get("NAME") or "Linux Mint"
    base_bits = []
    if osr.get("VERSION_CODENAME"):
        base_bits.append(osr["VERSION_CODENAME"])
    if osr.get("UBUNTU_CODENAME"):
        base_bits.append("Ubuntu " + osr["UBUNTU_CODENAME"])
    base = mint + (f" ({', '.join(base_bits)})" if base_bits else "")
    mode_id = b.effective_mode()
    lindos_rows = [
        ("Edition", b.lindos_release()),
        ("Codename", osr.get("LINDOS_CODENAME") or "Aurora"),
        ("Based on", base),
        ("Kernel", b.kernel()),
        ("Desktop", b.xfce_version()),
        ("Lindos Mode", model.mode_display_name(mode_id, modes) + f" ({mode_id})"),
        ("Default browser", str(b.effective_browser())),
        ("Theme", "Lindos-Dark" if b.is_dark() else "Lindos-Light"),
        ("Installed on", b.install_date()),
        ("Windows apps", "Wine / Proton compatibility layer (no virtual machine — not Windows)"),
    ]
    return device_rows, lindos_rows


class AboutPage(PageBase):
    PAGE_ID = "about"

    def build(self) -> None:
        # -- hero
        hero = box("h", 18)
        hero.set_margin_top(6)
        hero.set_margin_bottom(6)
        logo = Gtk.Image.new_from_icon_name("lindos-start", Gtk.IconSize.DIALOG)
        logo.set_pixel_size(72)
        hero.pack_start(logo, False, False, 0)
        hb = box("v", 4)
        hb.set_valign(Gtk.Align.CENTER)
        self.hero_title = label("Lindos", ("about-title",))
        self.hero_sub = label("Windows-11-style Linux Mint XFCE remaster · idle target 350–500 MB RAM", ("dim-label",), wrap=True)
        hb.pack_start(self.hero_title, False, False, 0)
        hb.pack_start(self.hero_sub, False, False, 0)
        hero.pack_start(hb, True, True, 0)
        self.copy_btn = button("Copy", "edit-copy", (), self._copy, "Copy all specifications to the clipboard")
        self.copy_btn.set_valign(Gtk.Align.START)
        hero.pack_end(self.copy_btn, False, False, 0)
        self.add_widget(hero, 0)

        # -- device specifications
        dsec = self.add_section("Device specifications")
        self.device_card = Card("", "", None, ("device", "specifications", "cpu", "gpu", "ram", "disk", "hostname"))
        self.device_card.header.set_no_show_all(True)
        self.device_card.header.hide()
        self.device_grid = KeyValueGrid([("Device name", "…"), ("Processor", "…"), ("Graphics", "…"), ("Installed RAM", "…"), ("Storage (/)", "…"), ("System type", "…"), ("Battery", "…"), ("Uptime", "…")])
        self.device_card.add_body(self.device_grid.widget)
        dsec.add(self.device_card)

        # -- lindos specifications
        lsec = self.add_section("Lindos specifications")
        self.lindos_card = Card("", "", None, ("lindos", "version", "kernel", "xfce", "mode", "browser", "theme"))
        self.lindos_card.header.set_no_show_all(True)
        self.lindos_card.header.hide()
        self.lindos_grid = KeyValueGrid([("Edition", "…"), ("Codename", "…"), ("Based on", "…"), ("Kernel", "…"), ("Desktop", "…"), ("Lindos Mode", "…"), ("Default browser", "…"), ("Theme", "…"), ("Installed on", "…"), ("Windows apps", "…")])
        self.lindos_card.add_body(self.lindos_grid.widget)
        lsec.add(self.lindos_card)

        # -- related
        rsec = self.add_section("Related")
        rsec.add(ButtonCard("Memory report", "lindos-tune status: RAM used vs. the 350–500 MB idle target, zram, top processes", ("utilities-system-monitor",), ("ram", "memory", "report", "tune"), "Open", self._ram_report))
        rsec.add(ButtonCard("Bug report", "Generate a Markdown hardware/tuning report (lindos-tune report)", ("dialog-information",), ("bug", "report", "support"), "Generate", self._bug_report))
        rsec.add(ButtonCard("Website", "lindos.dev — documentation, compatibility list and FAQ", ("web-browser",), ("website", "docs", "help"), "Open", lambda: self.backend.open_url("https://lindos.dev")))
        rsec.add(ButtonCard("Licence", "Lindos code is GPL-3.0-or-later; third-party themes, fonts and icons keep their own licences (THIRD_PARTY.md)", ("text-x-generic",), ("licence", "license", "gpl"), "Open", lambda: self.backend.open_url("https://www.gnu.org/licenses/gpl-3.0.html")))
        self.add_widget(label("Windows programs run through Wine / Proton — a compatibility layer, not Windows and not a virtual machine.", ("dim-label",), wrap=True), 10)
        self._loaded = False

    # ------------------------------------------------------------------ data
    def refresh(self) -> None:
        def _work() -> Any:
            return collect_specs(self.backend, self.app.modes)

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            if exc or not res:
                self.toast(f"Could not read system information: {exc}")
                return
            device_rows, lindos_rows = res
            for k, v in device_rows:
                self.device_grid.set(k, v)
            for k, v in lindos_rows:
                self.lindos_grid.set(k, v)
            edition = dict(lindos_rows).get("Edition", "Lindos")
            self.hero_title.set_text(edition)
            self._loaded = True

        run_async(_work, _done, name="about-specs")

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ actions
    def _copy(self) -> None:
        text = "Device specifications\n" + self.device_grid.as_text() + "\n\nLindos specifications\n" + self.lindos_grid.as_text() + "\n"
        clipboard_set(text)
        self.toast("Specifications copied to the clipboard")

    def _ram_report(self) -> None:
        if self.backend.which("lindos-tune"):
            self.app.show_output("Memory report (lindos-tune status)", ["lindos-tune", "status"])
        elif self.backend.which("lindos-ram"):
            self.app.show_output("Memory report (lindos-ram)", ["lindos-ram"])
        else:
            self.app.show_output("Memory report (free -m)", ["free", "-m"])

    def _bug_report(self) -> None:
        if self.backend.which("lindos-tune"):
            self.app.show_output("Bug report (lindos-tune report)", ["lindos-tune", "report"])
        else:
            self.toast("lindos-tune is not installed — copy the specifications above instead")


__all__ = ["AboutPage", "collect_specs"]
