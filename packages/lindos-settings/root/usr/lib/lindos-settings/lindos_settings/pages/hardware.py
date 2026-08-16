"""Hardware page: CPU (governor, EPP), GPU (vendor, driver status, NVIDIA Settings / CoreCtrl /
Install drivers), fans & sensors (3 s refresh while visible), power profile, RGB (OpenRGB),
storage TRIM, battery."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .. import model
from ..widgets import HAVE_GTK, ButtonCard, Card, ComboCard, OutputDialog, PageBase, add_timeout_seconds, box, button, confirm, label, remove_source, run_async

if HAVE_GTK:  # pragma: no cover
    from gi.repository import Gtk  # type: ignore
else:  # pragma: no cover
    Gtk = None  # type: ignore

log = logging.getLogger("lindos.settings.hardware")

SENSOR_REFRESH_SECONDS = 3


class HardwarePage(PageBase):
    PAGE_ID = "hardware"

    def build(self) -> None:
        b = self.backend
        self._timer: Optional[int] = None

        # -- CPU
        csec = self.add_section("Processor")
        cpu = b.cpu_info()
        cores = f"{cpu['cores']} cores / {cpu['threads']} threads" if cpu.get("cores") and cpu.get("threads") and cpu["cores"] != cpu["threads"] else (f"{cpu['threads']} threads" if cpu.get("threads") else "")
        self.cpu_card = Card(cpu["model"], cores, ("cpu", "computer"), ("cpu", "processor", "cores"))
        csec.add(self.cpu_card)
        govs = b.available_governors()
        cur = b.current_governor()
        self.gov_card = ComboCard("CPU governor", "schedutil = balanced (Everyday); performance = max clocks (Gaming); powersave = battery", ("cpu", "computer"), ("governor", "schedutil", "performance", "powersave", "frequency"), options=[(g, g) for g in govs] or [(cur or "unknown", cur or "unknown")], on_change=self._set_governor, active_id=cur or None)
        if not govs:
            self.gov_card.combo.set_sensitive(False)
            self.gov_card.set_subtitle("cpufreq is not available on this machine (virtual machine or unsupported driver)")
        csec.add(self.gov_card)
        epp_opts = b.epp_available()
        if epp_opts:
            # Read-only: SPEC §4.6 has no privileged EPP action; the helper's set-governor writes
            # the matching EPP (performance / balance_performance / power) — see backend.EPP_BY_GOVERNOR.
            self.epp_card = Card("Energy/performance preference (EPP)", "", ("battery",), ("epp", "energy", "pstate", "intel_pstate", "amd-pstate"))
            self.epp_value = label("", ("kv-value",))
            self.epp_card.set_control(self.epp_value)
            csec.add(self.epp_card)
            self._update_epp()
        else:
            self.epp_card = None
            self.epp_value = None

        # -- GPU
        gsec = self.add_section("Graphics")
        self.gpus = b.gpu_info()
        self.gpu_cards: list[Card] = []
        if not self.gpus:
            gsec.add(Card("Graphics", "No GPU detected (lspci unavailable?)", ("video-display",), ("gpu",)))
        for g in self.gpus:
            card = Card(g["model"], f"Vendor: {g['vendor']}" + (f" · driver: {g['driver']}" if g.get("driver") else ""), ("video-display",), ("gpu", "graphics", g["vendor"]))
            gsec.add(card)
            self.gpu_cards.append(card)
        self.driver_card = Card("Driver status", "Checking with lindos-drivers…", ("jockey", "preferences-desktop-peripherals"), ("driver", "nvidia", "amd", "intel", "mesa", "vulkan"))
        vendors = {g["vendor"] for g in self.gpus}
        if "nvidia" in vendors:
            self.driver_card.add_control(button("NVIDIA Settings", on_click=lambda: self._open_or_install(["nvidia-settings"], "nvidia-settings")))
        if vendors & {"amd", "intel"} or not vendors:
            self.driver_card.add_control(button("CoreCtrl", on_click=lambda: self._open_or_install(["corectrl"], "corectrl"), tooltip="AMD/Intel GPU clocks, fan curves and power profiles"))
        self.driver_card.add_control(button("Driver Manager", on_click=lambda: self._open_or_install(["mintdrivers"], "mintdrivers")))
        self.install_drivers_btn = button("Install drivers", classes=("suggested-action",), on_click=self._install_drivers)
        self.driver_card.set_control(self.install_drivers_btn)
        gsec.add(self.driver_card)
        self._refresh_driver_status()

        # -- fans / sensors
        fsec = self.add_section("Fans and sensors")
        self.sensor_card = Card("Sensors", "Reading lm-sensors…", ("temperature", "sensors-applet"), ("temperature", "fan", "rpm", "sensors"))
        self.sensor_grid = Gtk.Grid()
        self.sensor_grid.set_column_spacing(24)
        self.sensor_grid.set_row_spacing(4)
        self.sensor_card.add_body(self.sensor_grid)
        fsec.add(self.sensor_card)
        self.fan_card = ComboCard("Fan profile", "nbfc / fancontrol profile (needs a supported laptop or configured fancontrol) — loading list…", ("weather-windy",), ("fan", "profile", "nbfc", "quiet", "silent"), options=[("auto", "auto")], on_change=self._set_fan_profile, with_entry=True)
        fsec.add(self.fan_card)
        self._load_fan_profiles()

        # -- power profile
        psec = self.add_section("Power")
        pp = b.power_profiles()
        self.power_card = ComboCard("Power profile", "power-profiles-daemon: performance / balanced / power-saver", ("battery",), ("power", "profile", "balanced", "performance", "saver"), options=[(p, p) for p in pp] or [("balanced", "balanced")], on_change=self._set_power_profile, active_id=b.power_profile() or None)
        psec.add(self.power_card)
        self.battery_card = Card("Battery", "Battery present — power-saver profile and Lite mode extend runtime" if b.battery_present() else "No battery detected (desktop PC)", ("battery",), ("battery",))
        psec.add(self.battery_card)

        # -- RGB / storage
        osec = self.add_section("Devices and storage")
        osec.add(ButtonCard("RGB lighting", "Control keyboard, mouse, RAM and case lighting with OpenRGB", ("preferences-color",), ("rgb", "openrgb", "lighting", "led"), "Open OpenRGB", lambda: self._open_or_install(["openrgb"], "openrgb")))
        osec.add(ButtonCard("Mouse and peripherals", "Configure gaming mice (Piper / libratbag)", ("input-mouse",), ("mouse", "piper", "dpi"), "Open Piper", lambda: self._open_or_install(["piper"], "piper")))
        self.trim_card = Card("SSD TRIM", "Checking fstrim.timer…", ("drive-harddisk",), ("trim", "ssd", "fstrim", "storage"))
        self.trim_btn = button("Enable weekly TRIM", on_click=self._enable_trim)
        self.trim_card.set_control(self.trim_btn)
        osec.add(self.trim_card)
        self._refresh_trim()

    # ------------------------------------------------------------------ helpers
    def _open_or_install(self, argv: list[str], package: str) -> None:
        kind, value = model.which_or_install(argv, package, self.backend.which)
        if kind == "run":
            self.backend.spawn(value)
            return
        if confirm(self.app.window, f"Install {package}?", f"'{argv[0]}' is not installed. Install package '{package}' now?", "Install"):
            run_async(lambda: self.backend.install_packages([str(value)]), lambda res, exc: self.toast(f"{package} installed" if res is not None and getattr(res, "ok", False) and not exc else f"Install of {package} failed"), name="install-" + package)

    def _helper_done(self, what: str, refresh: Optional[Any] = None) -> Any:
        def _done(res: Any, exc: Optional[BaseException]) -> None:
            ok = res is not None and getattr(res, "ok", False) and not exc
            if ok:
                self.toast(f"{what} applied")
            else:
                err = (getattr(res, "err", "") or getattr(res, "out", "") or str(exc or "")).strip()
                self.toast(f"{what} failed: {err[:140] or 'helper error'}")
            if refresh:
                refresh()

        return _done

    # ------------------------------------------------------------------ CPU
    def _set_governor(self, gov: str) -> None:
        def _after() -> None:
            self.gov_card.set_active_id_silent(self.backend.current_governor() or gov)
            self._update_epp()

        run_async(lambda: self.backend.set_governor(gov), self._helper_done(f"Governor {gov}", _after), name="governor")

    def _update_epp(self) -> None:
        if not self.epp_card or self.epp_value is None:
            return
        cur = self.backend.epp_current() or "unknown"
        self.epp_value.set_text(cur)
        opts = ", ".join(self.backend.epp_available())
        self.epp_card.set_subtitle(f"Follows the governor (performance → performance, schedutil → balance_performance, powersave → power). Available: {opts}")

    # ------------------------------------------------------------------ GPU
    def _refresh_driver_status(self) -> None:
        def _done(res: Any, exc: Optional[BaseException]) -> None:
            if exc or res is None:
                self.driver_card.set_subtitle("Driver status unavailable")
                return
            if res.code == 127:
                self.driver_card.set_subtitle("lindos-drivers not installed (package lindos-gaming) — use Driver Manager")
                return
            text = res.out.strip() or res.err.strip()
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if data is not None:
                summary = model.summarize_driver_status(data)
                if not summary and isinstance(data, list):
                    summary = "; ".join(str(d.get("driver") or d.get("name") or d) if isinstance(d, dict) else str(d) for d in data)[:300]
                text = summary or json.dumps(data)[:240]
            else:
                text = " ".join(text.split())[:300]
            self.driver_card.set_subtitle(text or "No output from lindos-drivers")

        run_async(self.backend.drivers_status, _done, name="drivers-status")

    def _install_drivers(self) -> None:
        vendors = [g["vendor"] for g in self.gpus if g["vendor"] in ("nvidia", "amd", "intel")]
        vendor = vendors[0] if vendors else "auto"
        variant = ""
        if vendor == "nvidia":
            dlg = Gtk.MessageDialog(transient_for=self.app.window, modal=True, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE, text="Install NVIDIA driver")
            dlg.format_secondary_text("Choose the driver flavour. 'Proprietary' is recommended for GeForce GTX 900–RTX 40 gaming; 'Open kernel modules' for RTX 20+ / newest cards. Secure Boot may need MOK enrolment.")
            dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dlg.add_button("Open kernel modules", 2)
            dlg.add_button("Proprietary", 1)
            resp = dlg.run()
            dlg.destroy()
            if resp not in (1, 2):
                return
            variant = "proprietary" if resp == 1 else "open"
        elif not confirm(self.app.window, "Install recommended drivers?", f"Runs 'lindos-drivers install' for {vendor} through the Lindos helper (administrator password, internet needed).", "Install"):
            return
        self.install_drivers_btn.set_sensitive(False)
        out = OutputDialog(self.app.window, "Installing drivers", self.backend)
        out.set_status("Running lindos-drivers through the helper… (this can take several minutes)", True)

        def _done(res: Any, exc: Optional[BaseException]) -> None:
            self.install_drivers_btn.set_sensitive(True)
            ok = res is not None and getattr(res, "ok", False) and not exc
            out.append((getattr(res, "out", "") or "") + ("\n" + getattr(res, "err", "") if getattr(res, "err", "") else ""))
            out.set_status("Done — reboot to use the new driver" if ok else "Failed — try Driver Manager (mintdrivers) or `pkexec lindos-drivers install`", False)
            self._refresh_driver_status()

        run_async(lambda: self.backend.install_drivers(vendor, variant), _done, name="install-drivers")

    # ------------------------------------------------------------------ sensors
    def _refresh_sensors(self) -> bool:
        def _done(readings: Any, exc: Optional[BaseException]) -> None:
            for child in self.sensor_grid.get_children():
                self.sensor_grid.remove(child)
            readings = readings or []
            if not readings:
                self.sensor_card.set_subtitle("No sensor data (install lm-sensors and run `sudo sensors-detect`, or the machine exposes none)")
                return
            fans = [r for r in readings if r["kind"] == "fan"]
            temps = [r for r in readings if r["kind"] == "temp"]
            self.sensor_card.set_subtitle(f"{len(temps)} temperature and {len(fans)} fan reading(s) — refreshes every {SENSOR_REFRESH_SECONDS} s")
            row = 0
            for r in (temps + fans + [x for x in readings if x["kind"] not in ("fan", "temp")])[:24]:
                self.sensor_grid.attach(label(r["name"], ("kv-key", "dim-label"), ellipsize=True), 0, row, 1, 1)
                val = f"{r['value']:.0f} {r['unit']}" if r["unit"] == "RPM" else f"{r['value']:.1f} {r['unit']}".strip()
                self.sensor_grid.attach(label(val, ("kv-value",)), 1, row, 1, 1)
                row += 1
            self.sensor_grid.show_all()

        run_async(self.backend.fan_sensors, _done, name="sensors")
        return True  # keep the timer; on_hide() removes it

    def _load_fan_profiles(self) -> None:
        def _done(profiles: Any, exc: Optional[BaseException]) -> None:
            if exc or not profiles:
                if not self.backend.which("lindos-tune"):
                    self.fan_card.set_subtitle("lindos-tune is not installed — fan profiles unavailable (type a nbfc profile name to try anyway)")
                else:
                    self.fan_card.set_subtitle("No fan profiles reported by `lindos-tune fan list` (nbfc not configured?) — type a profile name to try anyway")
                return
            self.fan_card.set_options([(p, p) for p in profiles], None)
            self.fan_card.set_subtitle("nbfc / fancontrol profile from `lindos-tune fan list`; applied through the Lindos helper (set-fan-profile)")

        run_async(self.backend.fan_profiles, _done, name="fan-profiles")

    def _set_fan_profile(self, profile: str) -> None:
        run_async(lambda: self.backend.set_fan_profile(profile), self._helper_done(f"Fan profile {profile}"), name="fan-profile")

    # ------------------------------------------------------------------ power
    def _set_power_profile(self, profile: str) -> None:
        run_async(lambda: self.backend.set_power_profile(profile), self._helper_done(f"Power profile {profile}", lambda: self.power_card.set_active_id_silent(self.backend.power_profile() or profile)), name="power-profile")

    # ------------------------------------------------------------------ trim
    def _refresh_trim(self) -> None:
        def _done(st: Any, exc: Optional[BaseException]) -> None:
            if exc or not st:
                self.trim_card.set_subtitle("systemctl unavailable")
                return
            enabled = st.get("enabled") == "enabled"
            self.trim_card.set_subtitle(f"fstrim.timer: {st.get('enabled')} / {st.get('active')} — weekly TRIM keeps SSDs fast")
            self.trim_btn.set_sensitive(not enabled)
            self.trim_btn.set_label("Enabled" if enabled else "Enable weekly TRIM")

        run_async(self.backend.trim_status, _done, name="trim-status")

    def _enable_trim(self) -> None:
        run_async(self.backend.enable_trim, self._helper_done("TRIM timer", self._refresh_trim), name="enable-trim")

    # ------------------------------------------------------------------ visibility
    def on_show(self) -> None:
        self.gov_card.set_active_id_silent(self.backend.current_governor() or None)
        self._update_epp()
        self.power_card.set_active_id_silent(self.backend.power_profile() or None)
        self._refresh_sensors()
        remove_source(self._timer)
        self._timer = add_timeout_seconds(SENSOR_REFRESH_SECONDS, self._refresh_sensors)

    def on_hide(self) -> None:
        remove_source(self._timer)
        self._timer = None


__all__ = ["HardwarePage"]
