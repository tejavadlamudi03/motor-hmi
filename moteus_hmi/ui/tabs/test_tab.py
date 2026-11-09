"""Test & Control: live plots + manual commanding (operator workspace)."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ...core.controllers import F_CONF_PERSIST, F_SET_OUTPUT
from ...core.faults import fault_title
from ...core.types import (ControlMode, FirmwareMode, HOME_STATE_NAMES, MotorCommand, finite)
from ...security.auth import Perm
from .. import theme
from ..widgets import (HoldButton, Led, LivePlot, MotorDial, ParamSlider, button, confirm, fmt, from_display,
                       group, muted, to_display, unit_label)
from .base import HmiTab


class TestTab(HmiTab):
    title = "Test && Control"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        split = QSplitter(Qt.Horizontal)
        root.addWidget(split)

        # ---- plots -----------------------------------------------------
        plots = QWidget()
        pl = QVBoxLayout(plots)
        pl.setContentsMargins(0, 0, 0, 0)
        opts = QHBoxLayout()
        self.show_raw = QCheckBox("Show raw (unfiltered)")
        self.show_cmd = QCheckBox("Show setpoint")
        self.show_cmd.setChecked(True)
        self.pause = QCheckBox("Pause")
        self.window = QSpinBox()
        self.window.setRange(2, 120)
        self.window.setValue(int(ctx.settings.plot_window_s))
        self.window.setSuffix(" s window")
        for w in (self.show_raw, self.show_cmd, self.pause):
            opts.addWidget(w)
        opts.addStretch(1)
        opts.addWidget(self.window)
        pl.addLayout(opts)
        self.p_pos = LivePlot("Position", "rev", theme.TRACE["position"])
        self.p_vel = LivePlot("Velocity", "rev/s", theme.TRACE["velocity"])
        self.p_tor = LivePlot("Torque", "N·m", theme.TRACE["torque"])
        for p in (self.p_pos, self.p_vel, self.p_tor):
            p.add_series("raw", theme.TRACE["raw"], 1)
            p.add_series("cmd", theme.TRACE["command"], 1, dash=True)
            p.set_visible_series("raw", False)
            pl.addWidget(p, 1)
        split.addWidget(plots)

        # ---- control panel --------------------------------------------
        panel = QWidget()
        side = QVBoxLayout(panel)
        side.setContentsMargins(4, 0, 4, 0)

        node = QGridLayout()
        self.motor_combo = QComboBox()
        self.motor_combo.currentIndexChanged.connect(self._combo_motor)
        self.change_id_btn = button("Change ID…", slot=self._change_id,
                                    tip="Write a new id.id to the controller and save it")
        node.addWidget(QLabel("Node ID"), 0, 0)
        node.addWidget(self.motor_combo, 0, 1)
        node.addWidget(self.change_id_btn, 0, 2)
        side.addWidget(group("Node", node))

        cmds = QHBoxLayout()
        self.enable_btn = button("Enable", slot=self._enable, tip="Servo on: hold current position")
        self.zero_btn = button("Save Zero", slot=self._save_zero,
                               tip="Make the current output position 0 and save it to flash")
        self.disable_btn = button("Disable", slot=self._disable, tip="Servo off (torque off)")
        # Neutral until pressed; then coloured from the motor's ACTUAL state:
        # Enable = blue while enabled, Disable = red after a Disable press.
        self._last_pressed: dict[int, str] = {}
        self.reset_btn = button("Reset fault", slot=self._reset)
        for b in (self.enable_btn, self.zero_btn, self.disable_btn, self.reset_btn):
            cmds.addWidget(b)
        side.addWidget(group("Control commands", cmds))

        units = QHBoxLayout()
        self.unit_btns = QButtonGroup(self)
        for u in ("rev", "rad", "deg"):
            b = QPushButton(u)
            b.setCheckable(True)
            b.setChecked(u == ctx.units)
            self.unit_btns.addButton(b)
            units.addWidget(b)
            b.clicked.connect(lambda _=False, u=u: self.ctx.set_units(u))
        units.addStretch(1)
        side.addWidget(group("Display units", units))

        # mode tabs with slider parameters
        self.modes = QTabWidget()
        self.mode_pages: dict[str, tuple[ControlMode, dict[str, ParamSlider]]] = {}
        self._build_mode_pages()
        side.addWidget(group("Control parameters", self._wrap(self.modes)))

        # live motor picture
        self.dial = MotorDial()
        side.addWidget(group("Motor (live)", self._wrap(self.dial)))

        # status
        st = QGridLayout()
        self.state_led = Led()
        self.state_lbl = QLabel("Disconnected")
        self.state_lbl.setStyleSheet("font-weight: 600;")
        self.fault_lbl = muted("")
        self.mt_lbl = QLabel("—")
        self.bt_lbl = QLabel("—")
        self.v_lbl = QLabel("—")
        self.home_lbl = QLabel("—")
        self.tx_lbl = QLabel("—")
        self.rx_lbl = QLabel("—")
        for l in (self.tx_lbl, self.rx_lbl):
            l.setStyleSheet(f"font-family: monospace; color: {theme.ACCENT}; font-size: 8pt;")
            l.setWordWrap(True)
            l.setTextInteractionFlags(Qt.TextSelectableByMouse)
        st.addWidget(self.state_led, 0, 0)
        st.addWidget(self.state_lbl, 0, 1, 1, 3)
        st.addWidget(self.fault_lbl, 1, 0, 1, 4)
        st.addWidget(muted("Motor T"), 2, 0)
        st.addWidget(self.mt_lbl, 2, 1)
        st.addWidget(muted("Board T"), 2, 2)
        st.addWidget(self.bt_lbl, 2, 3)
        st.addWidget(muted("Bus V"), 3, 0)
        st.addWidget(self.v_lbl, 3, 1)
        st.addWidget(muted("Homed"), 3, 2)
        st.addWidget(self.home_lbl, 3, 3)
        st.addWidget(muted("TX"), 4, 0)
        st.addWidget(self.tx_lbl, 4, 1, 1, 3)
        st.addWidget(muted("RX"), 5, 0)
        st.addWidget(self.rx_lbl, 5, 1, 1, 3)
        side.addWidget(group("Status / CAN data", st))

        send = QGridLayout()
        self.live = QCheckBox("Live send")
        self.live.setToolTip("While ticked, every slider / value change is sent to the motor "
                             "immediately (at most once per period). The motor must be enabled.")
        self.period = QSpinBox()
        self.period.setRange(10, 1000)
        self.period.setValue(50)
        self.period.setSuffix(" ms")
        self.update_btn = button("Update", slot=self._update_from_feedback,
                                 tip="Load the measured state into the fields")
        self.clear_btn = button("Clear", slot=self._clear)
        self.send_btn = button("Send", "primary", self._send)
        send.addWidget(self.live, 0, 0)
        send.addWidget(self.period, 0, 1)
        send.addWidget(self.update_btn, 1, 0)
        send.addWidget(self.clear_btn, 1, 1)
        send.addWidget(self.send_btn, 1, 2)
        self.send_msg = QLabel("")
        self.send_msg.setWordWrap(True)
        self.send_msg.setVisible(False)
        send.addWidget(self.send_msg, 2, 0, 1, 3)
        side.addWidget(group("Send", send))

        jog = QHBoxLayout()
        self.jog_speed = QDoubleSpinBox()
        self.jog_speed.setRange(0.0, 1000.0)
        self.jog_speed.setValue(0.2)
        self.jog_speed.setDecimals(3)
        self.jog_neg = HoldButton("◀ Jog −")
        self.jog_pos = HoldButton("Jog + ▶")
        self.jog_neg.held.connect(lambda d: self._jog(-1, d))
        self.jog_pos.held.connect(lambda d: self._jog(+1, d))
        jog.addWidget(self.jog_neg)
        jog.addWidget(self.jog_speed)
        jog.addWidget(self.jog_pos)
        side.addWidget(group("Jog (hold to run)", jog))
        side.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(400)
        split.addWidget(scroll)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)

        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self.period.valueChanged.connect(lambda v: self._live_timer.setInterval(v))
        self.live.toggled.connect(self._live_toggled)
        self._dirty = False
        self.window.valueChanged.connect(self._set_window)
        self.show_raw.toggled.connect(lambda on: [p.set_visible_series("raw", on)
                                                  for p in (self.p_pos, self.p_vel, self.p_tor)])
        self.show_cmd.toggled.connect(lambda on: [p.set_visible_series("cmd", on)
                                                  for p in (self.p_pos, self.p_vel, self.p_tor)])
        self._set_window(self.window.value())
        self.on_units(ctx.units)
        self.on_session()

    # ------------------------------------------------------------------
    @staticmethod
    def _wrap(w):
        lay = QVBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(w)
        return lay

    def _page(self, key: str, title: str, mode: ControlMode, sliders: dict[str, ParamSlider],
              note: str = "", extra=None) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        for s in sliders.values():
            # Sliders are created in native moteus units; remember them so
            # display-unit changes convert exactly.
            s._base_unit, s._base_range, s._units = s.unit.text(), (s._lo, s._hi), "rev"
            lay.addWidget(s)
            s.valueChanged.connect(self._mark_dirty)
        if extra:
            lay.addWidget(extra)
        if note:
            lay.addWidget(muted(note))
        lay.addStretch(1)
        self.modes.addTab(w, title)
        self.mode_pages[key] = (mode, sliders)

    def _build_mode_pages(self) -> None:
        self.modes.clear()
        self.mode_pages.clear()
        prof = self.ctx.profile
        lim = self.ctx.settings.limits_for(self.ctx.selected_motor or 1)
        P = lambda n, u, lo, hi, v=0.0, d=4: ParamSlider(n, u, lo, hi, v, d)
        pmin = lim.position_min if finite(lim.position_min) else -10.0
        pmax = lim.position_max if finite(lim.position_max) else 10.0
        if prof.supports(ControlMode.POSITION):
            self._page("impedance", "Impedance", ControlMode.POSITION, {
                "position": P("Pos", "rev", pmin, pmax),
                "velocity": P("Vel", "rev/s", -lim.max_velocity, lim.max_velocity),
                "kp_scale": P("Kp scale", "×", 0.0, lim.kp_scale_max, 1.0, 3),
                "kd_scale": P("Kd scale", "×", 0.0, lim.kd_scale_max, 1.0, 3),
                "feedforward_torque": P("Tor", "N·m", -lim.max_torque, lim.max_torque),
            }, "MIT-style impedance control: torque = Kp·Δpos + Kd·Δvel + Tor. "
               "Scales multiply the controller's servo.pid_position kp / kd.")
            self._page("position", "Position", ControlMode.POSITION, {
                "position": P("Pos", "rev", pmin, pmax),
                "velocity_limit": P("Vel limit", "rev/s", 0.001, lim.max_velocity, lim.max_velocity / 2),
                "accel_limit": P("Acc limit", "rev/s²", 0.001, lim.max_accel, lim.max_accel / 2),
                "maximum_torque": P("Max tor", "N·m", 0.0, lim.max_torque, lim.max_torque),
            }, "Trapezoidal move to the target with velocity / acceleration limits.")
        if prof.supports(ControlMode.VELOCITY):
            self._page("velocity", "Velocity", ControlMode.VELOCITY, {
                "velocity": P("Vel", "rev/s", -lim.max_velocity, lim.max_velocity),
                "accel_limit": P("Acc limit", "rev/s²", 0.001, lim.max_accel, lim.max_accel / 2),
                "maximum_torque": P("Max tor", "N·m", 0.0, lim.max_torque, lim.max_torque),
            })
        if prof.supports(ControlMode.TORQUE):
            self._page("torque", "Torque", ControlMode.TORQUE, {
                "feedforward_torque": P("Tor", "N·m", -lim.max_torque, lim.max_torque),
            }, "Open torque control. The HMI trips to hold if speed exceeds the over-speed limit.")
        if prof.supports(ControlMode.STAY_WITHIN):
            self._page("stay_within", "Stay within", ControlMode.STAY_WITHIN, {
                "lower_bound": P("Lower", "rev", pmin, pmax, pmin / 2),
                "upper_bound": P("Upper", "rev", pmin, pmax, pmax / 2),
                "maximum_torque": P("Max tor", "N·m", 0.0, lim.max_torque, lim.max_torque),
            }, "Free motion inside the bounds; the controller pushes back outside them.")
        if prof.supports(ControlMode.HOLD) or prof.supports(ControlMode.BRAKE):
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            if prof.supports(ControlMode.HOLD):
                h.addWidget(button("Zero-velocity hold", slot=lambda: self._send_simple(ControlMode.HOLD)))
            if prof.supports(ControlMode.BRAKE):
                h.addWidget(button("Brake", slot=lambda: self._send_simple(ControlMode.BRAKE)))
            self._page("hold", "Hold / Brake", ControlMode.HOLD, {}, "Hold damps motion at zero "
                       "velocity; Brake shorts the phases (no power needed).", extra=w)

    def _rescale_sliders(self) -> None:
        units = self.ctx.units
        for _, (_, sliders) in self.mode_pages.items():
            for s in sliders.values():
                base = s._base_unit
                v_base = from_display(s.value(), base, s._units)
                lo, hi = s._base_range
                s.set_range(to_display(lo, base, units), to_display(hi, base, units),
                            unit_label(base, units))
                s.set_value(to_display(v_base, base, units))
                s._units = units

    def _set_window(self, v: int) -> None:
        for p in (self.p_pos, self.p_vel, self.p_tor):
            p.window_s = float(v)

    # ------------------------------------------------------------------
    def on_units(self, units: str) -> None:
        for b in self.unit_btns.buttons():
            b.setChecked(b.text() == units)
        self.p_pos.set_unit(units)
        self.p_vel.set_unit(f"{units}/s")
        self._rescale_sliders()

    def on_devices(self, devices) -> None:
        self.motor_combo.blockSignals(True)
        self.motor_combo.clear()
        for d in devices:
            self.motor_combo.addItem(d.label(), d.motor_id)
        self.motor_combo.blockSignals(False)
        self._build_mode_pages()
        self._rescale_sliders()
        if devices:
            self.ctx.select_motor(devices[0].motor_id)
            self.on_motor(devices[0].motor_id)
        self.on_session()

    def on_motor(self, motor_id: int) -> None:
        i = self.motor_combo.findData(motor_id)
        if i >= 0 and i != self.motor_combo.currentIndex():
            self.motor_combo.blockSignals(True)
            self.motor_combo.setCurrentIndex(i)
            self.motor_combo.blockSignals(False)
        for p in (self.p_pos, self.p_vel, self.p_tor):
            p.clear()

    def _combo_motor(self, _i: int) -> None:
        mid = self.motor_combo.currentData()
        if mid is not None:
            self.ctx.select_motor(int(mid))

    def on_connection(self, connected: bool) -> None:
        self._last_pressed.clear()
        self._update_enable_buttons(None, False)
        if not connected:
            self.live.setChecked(False)
            self.state_lbl.setText("Disconnected")
            self.state_led.set_color(theme.MUTED)
        self.on_session()

    def on_session(self) -> None:
        c = self.ctx
        conn = c.connected
        op = c.can(Perm.OPERATE) and conn
        prof = c.profile
        for w in (self.enable_btn, self.disable_btn, self.send_btn, self.live, self.update_btn,
                  self.jog_neg, self.jog_pos):
            w.setEnabled(op)
        self.reset_btn.setEnabled(conn and c.can(Perm.RESET))
        self.zero_btn.setEnabled(conn and c.can(Perm.CALIBRATE) and prof.has(F_SET_OUTPUT))
        self.change_id_btn.setEnabled(conn and c.can(Perm.CONFIGURE) and prof.has(F_CONF_PERSIST))
        jog_ok = prof.supports(ControlMode.VELOCITY)
        self.jog_neg.setVisible(jog_ok)
        self.jog_pos.setVisible(jog_ok)

    def on_batch(self, batch) -> None:
        mid = self.ctx.selected_motor
        if mid is None:
            return
        units = self.ctx.units
        k = {"rev": 1.0, "rad": 2 * math.pi, "deg": 360.0}[units]
        if not self.pause.isChecked():
            for s in batch.samples.get(mid, []):
                r, f = s.raw, s.filtered
                self.p_pos.append(r.timestamp, {"main": f["position"] * k, "raw": r.position * k,
                                                "cmd": r.control_position * k})
                self.p_vel.append(r.timestamp, {"main": f["velocity"] * k, "raw": r.velocity * k,
                                                "cmd": r.control_velocity * k})
                self.p_tor.append(r.timestamp, {"main": f["torque"], "raw": r.torque})
        st = batch.status.get(mid)
        s = st.sample if st else None
        self.p_pos.redraw(s.filtered["position"] * k if s else None)
        self.p_vel.redraw(s.filtered["velocity"] * k if s else None)
        self.p_tor.redraw(s.filtered["torque"] if s else None)
        self._update_status(st, batch)

    @staticmethod
    def _set_kind(btn, kind: str) -> None:
        if (btn.property("kind") or "") != kind:
            btn.setProperty("kind", kind)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _update_enable_buttons(self, st, estop: bool) -> None:
        if st is None:
            self._set_kind(self.enable_btn, "")
            self._set_kind(self.disable_btn, "")
            return
        armed = st.armed and not estop and not st.tripped
        pressed = self._last_pressed.get(st.motor_id)
        self._set_kind(self.enable_btn, "primary" if armed else "")
        self._set_kind(self.disable_btn, "danger" if (not armed and pressed == "disable") else "")

    def _update_status(self, st, batch) -> None:
        self._update_enable_buttons(st, batch.estop)
        if st is None:
            return
        if self.live.isChecked() and (batch.estop or st.tripped or not st.armed):
            self.live.setChecked(False)
            why = ("Emergency stop" if batch.estop else
                   f"Motor tripped: {st.trip_reason}" if st.tripped else "Motor is not enabled")
            self._refused("Live send stopped", why)
        s = st.sample
        if batch.estop:
            self.state_led.set_color(theme.ALARM)
            self.state_lbl.setText("EMERGENCY STOP")
        elif st.tripped:
            self.state_led.set_color(theme.ALARM)
            self.state_lbl.setText(f"TRIPPED — {st.trip_reason}")
        elif not st.comm_ok or s is None:
            self.state_led.set_color(theme.WARN)
            self.state_lbl.setText("No communication")
        else:
            r = s.raw
            text = r.state_text or FirmwareMode.name_of(r.mode)
            self.state_led.set_color(theme.ALARM if r.mode == FirmwareMode.FAULT else
                                     theme.ACCENT if st.armed else theme.OK)
            self.state_lbl.setText(("ENABLED · " if st.armed else "Disabled · ") + text)
        if s:
            lim = self.ctx.settings.limits_for(st.motor_id)
            target = s.raw.control_position
            if not finite(target) and st.command.mode == ControlMode.POSITION:
                target = st.command.position
            self.dial.set_state(s.filtered["position"], target, s.filtered["velocity"],
                                s.filtered["torque"], lim.max_velocity, lim.max_torque,
                                st.armed, st.tripped or s.raw.mode == FirmwareMode.FAULT)
        if s:
            r = s.raw
            ftxt = r.fault_text or (fault_title(r.fault) if r.fault else "")
            if batch.estop:
                self.fault_lbl.setText("→ Remove the cause, press Reset E-stop (top bar), then Enable.")
            elif st.tripped:
                self.fault_lbl.setText("→ Fix the cause, press Reset fault, then Enable.")
            else:
                self.fault_lbl.setText(f"Fault/limit: {ftxt}" if ftxt else "Normal operation")
            self.mt_lbl.setText(f"{s.filtered['motor_temperature']:.1f} °C" if finite(r.motor_temperature) else "n/a")
            self.bt_lbl.setText(f"{s.filtered['temperature']:.1f} °C" if finite(r.temperature) else "—")
            self.v_lbl.setText(f"{s.filtered['voltage']:.2f} V" if finite(r.voltage) else "—")
            self.home_lbl.setText(HOME_STATE_NAMES.get(r.home_state, str(r.home_state)))
        mid = st.motor_id
        self.tx_lbl.setText(batch.bus.get("last_tx", {}).get(mid, "—") or "—")
        self.rx_lbl.setText(batch.bus.get("last_rx", {}).get(mid, "—") or "—")

    # ------------------------------------------------------------------
    def _mid(self):
        mid = self.ctx.selected_motor
        if mid is None:
            self.ctx.status_message.emit("Select a motor first", 4000)
        return mid

    def _info(self, text: str, error: bool = False) -> None:
        """Feedback shown right under the Send buttons (and in the status bar)."""
        self.send_msg.setText(text)
        self.send_msg.setStyleSheet(f"color: {theme.ALARM if error else theme.OK}; font-weight: 600;")
        self.send_msg.setVisible(bool(text))
        if text:
            self.ctx.status_message.emit(text, 8000)

    def _refused(self, what: str, reason: str) -> None:
        r = reason.lower()
        if "emergency stop" in r:
            hint = "Remove the cause, press Reset E-stop (top bar), then Enable."
        elif "tripped" in r:
            hint = "Fix the cause, press Reset fault, then Enable."
        elif "not enabled" in r:
            hint = "Press Enable first."
        elif "not connected" in r:
            hint = "Connect first."
        else:
            hint = ""
        head = what if what.endswith("stopped") else f"{what} refused"
        self._info(f"{head}: {reason}. {hint}".strip(), error=True)

    def _enable(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.OPERATE, self):
            return
        self._last_pressed[mid] = "enable"
        self.ctx.audit("motor.enable", {"motor": mid})
        self.ctx.run(self.ctx.worker.arm(mid, True),
                     ok=lambda _: self._info(f"Motor {mid} enabled – holding position. Now Send or Live send."),
                     err=lambda e: self._refused("Enable", e))

    def _disable(self) -> None:
        mid = self._mid()
        if mid is None:
            return
        self.live.setChecked(False)
        self._last_pressed[mid] = "disable"
        self.ctx.audit("motor.disable", {"motor": mid})
        self.ctx.run(self.ctx.worker.arm(mid, False))

    def _reset(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.RESET, self):
            return
        self.ctx.audit("motor.reset", {"motor": mid})
        self.ctx.run(self.ctx.worker.reset_motor(mid),
                     ok=lambda how: self._info(
                         f"Motor {mid} reset – still enabled, holding position." if how == "holding"
                         else f"Motor {mid} reset – press Enable to continue."),
                     err=lambda e: self._refused("Reset", e))

    def _save_zero(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.CALIBRATE, self):
            return
        if not confirm(self, "Save zero", f"Make the current output position of motor {mid} the new "
                       "zero and store it in flash?\nThe motor must be disabled."):
            return

        def done(_):
            self.ctx.audit("calibration.save_zero", {"motor": mid})
            self.ctx.status_message.emit(f"Zero saved on motor {mid}", 5000)

        def write(_):
            self.ctx.run(self.ctx.worker.diagnostic(mid, "conf write"), ok=done, what="conf write")

        if self.ctx.status.get(mid) and self.ctx.status[mid].armed:
            self.ctx.status_message.emit("Disable the motor first", 5000)
            return
        self.ctx.run(self.ctx.worker.diagnostic(mid, "d cfg-set-output 0"), ok=write, what="Save zero")

    def _change_id(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        new, ok = QInputDialog.getInt(self, "Change CAN ID", f"New ID for controller {mid} (1-126):",
                                      mid, 1, 126)
        if not ok or new == mid:
            return
        if any(d.motor_id == new for d in self.ctx.devices):
            self.ctx.status_message.emit(f"ID {new} already in use on this bus", 6000)
            return
        if not confirm(self, "Change CAN ID", f"Change controller {mid} to ID {new} and save to flash?",
                       typed=f"ID {new}"):
            return
        self.ctx.audit("config.change_id", {"from": mid, "to": new})
        self.ctx.run(self.ctx.worker.change_id(mid, new), what="Change ID",
                     ok=lambda ids: self.ctx.status_message.emit(f"Controller now on ID {new} (saved)", 6000))

    def _active(self):
        key = list(self.mode_pages)[self.modes.currentIndex()] if self.mode_pages else None
        return (key, *self.mode_pages[key]) if key else (None, None, {})

    def _build_command(self) -> MotorCommand | None:
        key, mode, sliders = self._active()
        if key is None or key == "hold":
            return None
        vals = {k: from_display(s.value(), s._base_unit, s._units) for k, s in sliders.items()}
        cmd = MotorCommand(mode=mode, **vals)
        if key == "impedance":
            cmd.velocity_limit = math.nan
            cmd.accel_limit = math.nan
        return cmd

    def _send(self, quiet: bool = False) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.can(Perm.OPERATE):
            return
        cmd = self._build_command()
        if cmd is None:
            return
        if not quiet:
            self.ctx.audit("motion.command", {"motor": mid, "command": cmd.describe()})

        def ok(notes):
            if notes and not quiet:
                self.ctx.status_message.emit("Limited by safety: " + "; ".join(notes), 6000)

        def refused(e):
            self.live.setChecked(False)
            self._refused("Live send" if quiet else "Send", e)

        self.ctx.run(self.ctx.worker.set_command(mid, cmd), ok=ok, err=refused)
        self._dirty = False

    def _send_simple(self, mode: ControlMode) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.OPERATE, self):
            return
        self.ctx.audit("motion.command", {"motor": mid, "command": mode.label})
        self.ctx.run(self.ctx.worker.set_command(mid, MotorCommand(mode=mode)),
                     err=lambda e: self._refused(mode.label, e))

    def _mark_dirty(self, *_):
        self._dirty = True

    def _live_toggled(self, on: bool) -> None:
        if on:
            st = self.ctx.status.get(self.ctx.selected_motor)
            if st is None or not st.armed or st.tripped:
                reason = ("Motor is tripped" if st is not None and st.tripped else "Motor is not enabled")
                self.live.blockSignals(True)
                self.live.setChecked(False)
                self.live.blockSignals(False)
                self._refused("Live send", reason)
                return
            self.ctx.audit("motion.live_send", {"motor": self.ctx.selected_motor, "period_ms": self.period.value()})
            self._live_timer.start(self.period.value())
            self._info("Live send ON – move a slider or edit a value; changes go to the motor immediately.")
            self._send()
        else:
            self._live_timer.stop()
            if self.send_msg.text().startswith("Live send ON"):
                self._info("")

    def _live_tick(self) -> None:
        if self._dirty:
            self._send(quiet=True)

    def _update_from_feedback(self) -> None:
        s = self.ctx.current_sample()
        if not s:
            return
        units = self.ctx.units
        r = s.raw
        for _, sliders in self.mode_pages.values():
            if "position" in sliders and finite(r.position):
                sliders["position"].set_value(to_display(r.position, "rev", units))
            if "velocity" in sliders:
                sliders["velocity"].set_value(0.0)
            if "feedforward_torque" in sliders:
                sliders["feedforward_torque"].set_value(0.0)
        self._dirty = False

    def _clear(self) -> None:
        for _, sliders in self.mode_pages.values():
            for k, s in sliders.items():
                if k in ("position", "velocity", "feedforward_torque"):
                    s.set_value(0.0)
        for p in (self.p_pos, self.p_vel, self.p_tor):
            p.clear()

    def _jog(self, direction: int, down: bool) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.can(Perm.OPERATE):
            return
        units = self.ctx.units
        if down:
            v = direction * from_display(self.jog_speed.value(), "rev/s", units)
            lim = self.ctx.settings.limits_for(mid)
            cmd = MotorCommand(mode=ControlMode.VELOCITY, velocity=v, accel_limit=lim.max_accel / 2)
            self.ctx.audit("motion.jog", {"motor": mid, "velocity": v})
        else:
            cmd = MotorCommand(mode=ControlMode.VELOCITY, velocity=0.0,
                               accel_limit=self.ctx.settings.limits_for(mid).max_accel)
        self.ctx.run(self.ctx.worker.set_command(mid, cmd),
                     err=lambda e: self._refused("Jog", e))
