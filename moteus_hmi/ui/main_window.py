"""Main window: connection panel, E-STOP header, tabs, status bar."""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QProgressBar, QSplitter, QStatusBar, QTabWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core.controllers import F_FW_WATCHDOG, get_profile
from ..core.safety import SafetyLimits, Severity
from ..remote.mobile_server import MobileServer
from ..security.auth import Perm
from . import theme
from .connection_panel import ConnectionPanel
from .context import AppContext
from .login import LoginDialog
from .mobile_dialog import MobileDialog
from .tabs.about_tab import AboutTab
from .tabs.admin_tab import AdminTab
from .tabs.bus_tab import BusTab
from .tabs.calibration_tab import CalibrationTab
from .tabs.console_tab import ConsoleTab
from .tabs.filters_tab import FiltersTab
from .tabs.firmware_tab import FirmwareTab
from .tabs.params_tab import ParamsTab
from .tabs.parse_tab import ParseTab
from .tabs.safety_tab import SafetyTab
from .tabs.test_tab import TestTab
from .tabs.tuning_tab import TuningTab
from .widgets import Led, button, confirm, muted

log = logging.getLogger(__name__)


class ActivityFilter(QObject):
    """Tracks user input for the inactivity lock."""

    def __init__(self):
        super().__init__()
        self.last = time.monotonic()

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel, QEvent.TouchBegin):
            self.last = time.monotonic()
        return False


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1500, 940)
        scr = QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            if avail.width() < 1500 or avail.height() < 940:
                # small or scaled screen (laptops, WSL): fit it so the status bar stays visible
                self.resize(min(1500, avail.width()), min(940, avail.height()))
                self.setWindowState(self.windowState() | Qt.WindowMaximized)
        self._locked = False

        # ---- header ----------------------------------------------------
        header = QWidget()
        header.setObjectName("header")
        header.setStyleSheet(f"#header {{ background: {theme.PANEL}; border-bottom: 1px solid {theme.BORDER}; }}")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 6, 10, 6)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 14pt; font-weight: 700;")
        hl.addWidget(title)
        self.sim_banner = QLabel("SIMULATION")
        self.sim_banner.setProperty("role", "banner")
        self.sim_banner.setVisible(False)
        hl.addWidget(self.sim_banner)
        self.wd_banner = QLabel("NO CONTROLLER WATCHDOG")
        self.wd_banner.setProperty("role", "banner")
        self.wd_banner.setToolTip("This controller keeps its last setpoint if the PC stops. Hard-wired E-stop required.")
        self.wd_banner.setVisible(False)
        hl.addWidget(self.wd_banner)
        self.alarm_banner = QLabel("")
        self.alarm_banner.setProperty("role", "alarmbanner")
        self.alarm_banner.setVisible(False)
        hl.addWidget(self.alarm_banner)
        hl.addStretch(1)
        self.user_lbl = QLabel("")
        hl.addWidget(self.user_lbl)
        self.mobile_hdr_btn = button("Mobile view", slot=self._mobile_dialog,
                                     tip="Phone view for the same Wi-Fi: live status, alarms and E-STOP")
        hl.addWidget(self.mobile_hdr_btn)
        self.lock_btn = button("Lock", slot=self.lock)
        self.logout_btn = button("Log out", slot=self.logout)
        hl.addWidget(self.lock_btn)
        hl.addWidget(self.logout_btn)
        self.reset_estop_btn = button("Reset E-stop", slot=self._reset_estop)
        hl.addWidget(self.reset_estop_btn)
        self.estop_btn = button("■  E-STOP", "estop", lambda: self.estop("operator button"))
        self.estop_btn.setMinimumWidth(150)
        self.estop_btn.setToolTip("Emergency stop all motors (F12 / Pause). Software stop – "
                                  "not a substitute for the hard-wired E-stop.")
        hl.addWidget(self.estop_btn)

        # ---- body ------------------------------------------------------
        self.panel = ConnectionPanel(ctx)
        self.tabs = QTabWidget()
        self.tab_list = [
            TestTab(ctx), BusTab(ctx), ParamsTab(ctx), CalibrationTab(ctx), TuningTab(ctx),
            FiltersTab(ctx), SafetyTab(ctx), ParseTab(ctx), FirmwareTab(ctx), ConsoleTab(ctx),
            AdminTab(ctx), AboutTab(ctx),
        ]
        self.tab_by_type = {type(t).__name__: t for t in self.tab_list}
        for t in self.tab_list:
            self.tabs.addTab(t, t.title)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.panel)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 1300])
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(header)
        cl.addWidget(split, 1)
        self.setCentralWidget(central)

        # ---- status bar --------------------------------------------------
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.rec_btn = button("● Save data", slot=self._toggle_record)
        self.clear_btn = button("Clear data", slot=self._clear_plots)
        self.en_led = Led(theme.MUTED, 12)
        self.en_lbl = QLabel("DIS")
        self.rx_rate = QLabel("RX 0 B/s")
        self.tx_rate = QLabel("TX 0 B/s")
        self.load = QProgressBar()
        self.load.setMaximumWidth(110)
        self.load.setFormat("bus %p%")
        self.load.setToolTip("Approximate CAN-FD bus load (5 Mbit/s data phase)")
        self.counts = QLabel("RX 0 · TX 0")
        self.loop_lbl = QLabel("— Hz")
        self.clear_count_btn = button("Clear count", slot=self._clear_counts)
        self.mobile_btn = button("Mobile: off", slot=self._mobile_dialog,
                                 tip="Phone view for the same Wi-Fi: live status, alarms and E-STOP")
        for w in (self.rec_btn, self.clear_btn, self.en_led, self.en_lbl, self.rx_rate, self.tx_rate,
                  self.load, self.counts, self.loop_lbl, self.clear_count_btn, self.mobile_btn):
            sb.addPermanentWidget(w)
        self._last_bus = None

        # ---- wiring --------------------------------------------------------
        w = ctx.worker
        # Phone view (off until an administrator turns it on). The E-stop it
        # sends goes straight to the worker's thread-safe estop().
        self.mobile = MobileServer(ctx.users, ctx.audit_log, ctx.keystore, ctx.paths.security,
                                   estop_fn=w.estop, idle_timeout_s=ctx.settings.session_timeout_min * 60)
        self.mobile.estop_requested.connect(self._on_mobile_estop)
        self.mobile.viewers_changed.connect(lambda _n: self.update_mobile_status())
        w.connection_changed.connect(self.mobile.state.set_connection)
        w.telemetry.connect(lambda b: self.mobile.running and self.mobile.state.update_batch(b))
        w.alarm.connect(lambda a: self.mobile.running and self.mobile.state.add_alarm(a))
        w.connection_changed.connect(self._on_connection)
        w.devices_changed.connect(self._on_devices)
        w.telemetry.connect(self._on_batch)
        w.alarm.connect(self._on_alarm)
        w.message.connect(lambda lvl, m: self.statusBar().showMessage(m, 8000))
        w.noise_report.connect(lambda mid, rep: [t.on_noise(mid, rep) for t in self.tab_list])
        ctx.motor_selected.connect(self._on_motor)
        ctx.units_changed.connect(lambda u: [t.on_units(u) for t in self.tab_list])
        ctx.status_message.connect(lambda m, ms: self.statusBar().showMessage(m, ms))
        self.panel.connect_requested.connect(self._connect)
        self.panel.disconnect_requested.connect(self._disconnect)
        self.panel.goto_tab.connect(self._goto)
        self.tab_by_type["SafetyTab"].active_alarms_changed.connect(self._alarm_summary)

        # Esc is deliberately NOT used: it closes dialogs and would cause
        # nuisance stops.
        for seq in ("F12", "Pause"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(lambda s=seq: self.estop(f"keyboard {s}"))

        self.activity = ActivityFilter()
        QApplication.instance().installEventFilter(self.activity)
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._check_idle)
        self._idle_timer.start(5000)
        self._estop_latched = False
        self.apply_session()
        self.update_mobile_status()
        if ctx.settings.mobile_enabled:
            QTimer.singleShot(0, self._autostart_mobile)

    # ------------------------------------------------------------------
    def apply_session(self) -> None:
        c = self.ctx
        s = c.session
        self.user_lbl.setText(f"{s.username} · {s.role.label}" if s else "")
        for i, t in enumerate(self.tab_list):
            self.tabs.setTabVisible(i, c.can(t.perm))
            t.on_session()
        self.panel.on_session()
        self.reset_estop_btn.setEnabled(c.can(Perm.RESET))
        self.rec_btn.setEnabled(c.connected and c.can(Perm.RECORD))

    def _goto(self, what: str) -> None:
        target = {"export": "ParamsTab", "import": "ParamsTab", "calibration": "CalibrationTab",
                  "firmware": "FirmwareTab"}[what]
        tab = self.tab_by_type[target]
        self.tabs.setCurrentWidget(tab)
        if what == "export":
            tab._export()
        elif what == "import":
            tab._import()

    # ---- connection ---------------------------------------------------
    def _connect(self, profile_key: str, cfg) -> None:
        if not self.ctx.require(Perm.CONNECT, self):
            return
        s = self.ctx.settings
        s.transport = cfg.to_dict()
        self.ctx.save_settings()
        sup = self.ctx.worker.supervisor
        sup.estop_action = s.estop_action
        sup.comm_loss_cycles = s.comm_loss_cycles
        prof = get_profile(profile_key)
        rate = s.rate_hz if prof.default_rate_hz >= 100 else min(s.rate_hz, prof.default_rate_hz)
        # Per-controller ceiling: never allow a limit above the board rating.
        dl = SafetyLimits.from_dict(s.default_limits)
        dl.max_voltage = min(dl.max_voltage, prof.max_voltage)
        dl.min_voltage = max(dl.min_voltage, prof.min_voltage)
        sup.default_limits = dl
        self.panel.set_state(False, f"Connecting to {prof.model}…", busy=True)
        self.ctx.run(self.ctx.worker.connect_bus(profile_key, cfg, rate, s.watchdog_s),
                     err=self._connect_failed)

    def _connect_failed(self, msg: str) -> None:
        self.panel.set_state(False, "Connection failed")
        self.ctx.audit("bus.connect", {"error": msg}, "failed")
        kind = self.panel.config().kind
        if kind == "virtual_canfd" or not self.panel.has_link("virtual_canfd"):
            if kind == "virtual_canfd":
                msg += ("\n\nIs the Motor Simulator running with the same Bus group and Bus port? "
                        "Start it with ▶ Launch Motor Simulator.")
            QMessageBox.critical(self, "Connection failed", msg)
            return
        alt = "virtual_canfd"
        box = QMessageBox(QMessageBox.Critical, "Connection failed", msg, parent=self)
        box.setInformativeText(
            "No hardware connected? Test the complete HMI with the Motor Simulator: virtual moteus "
            "controllers on a virtual CAN-FD bus (same driver path as the real adapter).")
        sim_btn = box.addButton("Use Motor Simulator", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is sim_btn:
            self._use_simulator(alt)

    def _use_simulator(self, kind: str) -> None:
        """Switch to the virtual CAN-FD bus; start the Motor Simulator if none
        is answering, then connect."""
        self.panel.select_link(kind)
        cfg = self.panel.config()
        profile = self.panel.profile.currentData()
        from ..core.moteus_backend import MoteusBackend
        from ..core.types import TransportConfig

        async def probe():
            b = MoteusBackend(TransportConfig(**{**cfg.to_dict(), "explicit_ids": []}))
            try:
                await b.open()
                return [d.motor_id for d in await b.discover()]
            except Exception:
                return []
            finally:
                await b.close()

        def probed(ids):
            if ids:
                self.statusBar().showMessage(f"Motor Simulator found (IDs {ids}) – connecting…", 4000)
                self._connect(profile, cfg)
            else:
                self.statusBar().showMessage("Starting the Motor Simulator…", 4000)
                self.panel._launch_sim()
                self.panel.set_state(False, "Starting Motor Simulator…", busy=True)
                QTimer.singleShot(3500, lambda: self._connect(profile, cfg))

        self.ctx.run(self.ctx.worker.run_coroutine(probe), ok=probed, err=lambda e: probed([]))

    def _disconnect(self) -> None:
        if any(st.armed for st in self.ctx.status.values()):
            if not confirm(self, "Disconnect", "Motors are enabled. Disconnecting stops them. Continue?"):
                return
        self.ctx.run(self.ctx.worker.disconnect_bus())

    def _on_connection(self, connected: bool, desc: str) -> None:
        c = self.ctx
        c.connected = connected
        if not connected:
            c.devices = []
            c.status = {}
        prof = c.profile
        virtual = connected and c.worker.config.kind == "virtual_canfd"
        self.sim_banner.setText("VIRTUAL CAN-FD · MOTOR SIMULATOR APP")
        self.sim_banner.setToolTip("Talking to simulated controllers in the Motor Simulator app")
        self.sim_banner.setVisible(virtual)
        self.wd_banner.setVisible(connected and not prof.has(F_FW_WATCHDOG))
        self.panel.set_state(connected, f"{prof.model} — {desc}" if connected else "Disconnected")
        for t in self.tab_list:
            t.on_connection(connected)
        self.apply_session()

    def _on_devices(self, devices) -> None:
        c = self.ctx
        c.devices = devices
        for d in devices:
            lim = c.settings.limits_for(d.motor_id)
            prof = c.profile
            lim.max_voltage = min(lim.max_voltage, prof.max_voltage)
            lim.min_voltage = max(lim.min_voltage, prof.min_voltage)
            c.run(c.worker.set_limits(d.motor_id, lim))
            c.run(c.worker.set_filter(d.motor_id, "position", c.settings.filters.get("position", [])))
        for sig, specs in c.settings.filters.items():
            c.run(c.worker.set_filter(None, sig, specs))
        self.panel.set_devices(devices)
        for t in self.tab_list:
            t.on_devices(devices)
        if devices:
            c.select_motor(devices[0].motor_id)

    def _on_motor(self, mid: int) -> None:
        self.panel.select(mid)
        for t in self.tab_list:
            t.on_motor(mid)

    # ---- telemetry ------------------------------------------------------
    def _on_batch(self, batch) -> None:
        self.ctx.status = batch.status
        parse = self.tab_by_type["ParseTab"]
        for t in self.tab_list:
            # Hidden tabs skip plotting; Parse always tracks the last frame.
            if t.isVisible() or t is parse:
                t.on_batch(batch)
        self.panel.update_status(batch)
        st = batch.status.get(self.ctx.selected_motor)
        armed = bool(st and st.armed)
        self.en_led.set_color(theme.ACCENT if armed else theme.MUTED)
        self.en_lbl.setText("EN" if armed else "DIS")
        self.loop_lbl.setText(f"{batch.loop_hz:.0f} Hz" + (f" · {batch.overruns} overruns" if batch.overruns else ""))
        now = time.monotonic()
        b = batch.bus
        if self._last_bus:
            t0, b0 = self._last_bus
            dt = max(1e-3, now - t0)
            rx = max(0, b["rx_bytes"] - b0["rx_bytes"]) / dt
            tx = max(0, b["tx_bytes"] - b0["tx_bytes"]) / dt
            self.rx_rate.setText(f"RX {rx:,.0f} B/s")
            self.tx_rate.setText(f"TX {tx:,.0f} B/s")
            # rough: data bytes at 5 Mbit/s plus ~30 % framing / arbitration overhead
            self.load.setValue(int(min(100, (rx + tx) * 8 * 1.3 / 5e6 * 100)))
        self._last_bus = (now, b)
        self.counts.setText(f"RX {b['rx']:,} · TX {b['tx']:,}")
        if batch.estop != self._estop_latched:
            self._estop_latched = batch.estop
            self.estop_btn.setText("■  E-STOP ACTIVE" if batch.estop else "■  E-STOP")

    def _on_alarm(self, alarm) -> None:
        for t in self.tab_list:
            t.on_alarm(alarm)
        if alarm.active and alarm.severity >= Severity.TRIP:
            self.statusBar().showMessage(f"ALARM motor {alarm.motor_id}: {alarm.message}", 15000)

    def _alarm_summary(self, count: int, severity: int) -> None:
        self.alarm_banner.setVisible(count > 0)
        self.alarm_banner.setText(f"{count} ALARM{'S' if count != 1 else ''}")
        self.alarm_banner.setProperty("role", "alarmbanner" if severity >= Severity.TRIP else "banner")
        self.alarm_banner.style().unpolish(self.alarm_banner)
        self.alarm_banner.style().polish(self.alarm_banner)

    def _clear_counts(self) -> None:
        self.ctx.worker.reset_bus_stats()

    def _clear_plots(self) -> None:
        test = self.tab_by_type["TestTab"]
        for p in (test.p_pos, test.p_vel, test.p_tor):
            p.clear()
        self.tab_by_type["FiltersTab"].plot.clear()

    def _toggle_record(self) -> None:
        self.tab_by_type["FiltersTab"]._toggle_record()
        QTimer.singleShot(300, lambda: self.rec_btn.setText(
            "■ Stop saving" if self.tab_by_type["FiltersTab"].recording else "● Save data"))

    # ---- safety -----------------------------------------------------------
    def estop(self, reason: str) -> None:
        # Never permission-gated, works while locked.
        self.ctx.worker.estop(reason)
        self.statusBar().showMessage(f"EMERGENCY STOP: {reason}", 0)
        self.tab_by_type["TestTab"].live.setChecked(False)

    def _reset_estop(self) -> None:
        if not self.ctx.require(Perm.RESET, self):
            return
        if not confirm(self, "Reset emergency stop", "Confirm the cause of the emergency stop has been "
                       "removed and the machine is safe. Motors stay disabled until enabled again."):
            return
        self.ctx.audit("safety.estop_reset")
        self.ctx.run(self.ctx.worker.reset_estop(),
                     ok=lambda _: (self.statusBar().showMessage("E-stop reset", 5000),
                                   self.tab_by_type["SafetyTab"].clear_motor_alarms(None)))

    # ---- session ----------------------------------------------------------
    def _check_idle(self) -> None:
        if self._locked or not self.ctx.session:
            return
        if time.monotonic() - self.activity.last > self.ctx.settings.session_timeout_min * 60:
            self.lock(reason="inactivity")

    def lock(self, reason: str = "operator") -> None:
        if self._locked:
            return
        self._locked = True
        self.ctx.audit("auth.lock", {"reason": reason})
        old = self.ctx.session
        dlg = LoginDialog(self.ctx.users, self.ctx.audit_log, locked_user=old.username if old else "",
                          estop=lambda: self.estop("lock screen"), parent=self)
        dlg.setWindowModality(Qt.ApplicationModal)
        while dlg.exec() != QDialog.Accepted:
            pass
        if dlg.session.username != (old.username if old else ""):
            self.ctx.audit("auth.user_switch", {"from": old.username if old else "-", "to": dlg.session.username})
        self.ctx.session = dlg.session
        self.activity.last = time.monotonic()
        self._locked = False
        self.apply_session()

    def logout(self) -> None:
        armed = [m for m, st in self.ctx.status.items() if st.armed]
        if armed and not confirm(self, "Log out", f"Motors {armed} are enabled and will be disabled. Continue?"):
            return
        for m in armed:
            self.ctx.worker.arm(m, False)
        self.ctx.audit("auth.logout")
        self.ctx.session = None
        self.apply_session()
        self.lock(reason="logout")

    # ---- mobile view -------------------------------------------------------
    def _autostart_mobile(self) -> None:
        try:
            self.mobile.start(int(self.ctx.settings.mobile_port))
        except OSError as e:
            self.ctx.audit("mobile.autostart", {"port": self.ctx.settings.mobile_port, "error": str(e)}, "failed")
            self.statusBar().showMessage(f"Mobile view could not start on port "
                                         f"{self.ctx.settings.mobile_port}: {e}", 15000)
        self.update_mobile_status()

    def update_mobile_status(self) -> None:
        m = self.mobile
        if not m.running:
            self.mobile_btn.setText("Mobile: off")
            self.mobile_hdr_btn.setText("Mobile view: off")
        else:
            n = m.viewers
            self.mobile_btn.setText(f"Mobile: on · {n} phone{'s' if n != 1 else ''}")
            self.mobile_hdr_btn.setText(f"Mobile view: on ({n})")

    def _mobile_dialog(self) -> None:
        MobileDialog(self.ctx, self.mobile, self).exec()
        self.update_mobile_status()

    def _on_mobile_estop(self, reason: str) -> None:
        # The stop itself already happened on the worker thread.
        self.statusBar().showMessage(f"EMERGENCY STOP: {reason}", 0)
        self.tab_by_type["TestTab"].live.setChecked(False)

    def closeEvent(self, ev) -> None:
        armed = [m for m, st in self.ctx.status.items() if st.armed]
        if armed and not confirm(self, "Exit", f"Motors {armed} are enabled. Exiting stops all motors."):
            ev.ignore()
            return
        self.ctx.audit("app.exit")
        self.mobile.stop()
        self.ctx.worker.shutdown()
        ev.accept()
