# Safety concept

> **The software emergency stop, limits and trips in Motor HMI are not a safety-rated
> function.** They have no SIL or PL rating. Every machine must have a hard-wired
> emergency stop that removes motor power through a safety relay or STO input,
> independent of the PC, USB and software. Follow ISO 13849 / IEC 62061 and your
> machinery risk assessment.

## Protection layers (outermost first)

| # | Layer | Where | What it does |
|---|---|---|---|
| 1 | Hard-wired E-stop / STO | your machine | Removes power. The only safety-rated function. |
| 2 | Controller watchdog | moteus firmware | Every motion command carries `watchdog_timeout` (default 0.25 s). If the PC, USB cable or HMI stops, moteus enters its timeout mode by itself. **ST MCSDK has no such watchdog**; the HMI shows a permanent "NO CONTROLLER WATCHDOG" banner for those boards. |
| 3 | Controller limits | moteus flash | *Safety & Alarms → Write soft limits + watchdog to controller* stores `servopos.position_min/max` and `servo.default_timeout_s` so the controller enforces them even without the HMI. |
| 4 | Command clamping | HMI | Every command is clamped to the per-motor limits. Trajectories are always sent with finite velocity, acceleration and torque limits. Limits can never exceed the hard ceilings in `core/safety.py` or the board rating (e.g. 44 V for r4.11). |
| 5 | Telemetry supervision | HMI, every cycle | The motor trips on controller fault, controller timeout, comm loss (N missed replies), over/under-voltage, temperature, over-speed (on spike-filtered velocity), soft-limit overrun and following error. Electrical, thermal and comm trips go to *torque off*. Motion trips (over-speed, soft limit, following error) perform a *controlled stop and position hold at the trip point*, torque limited, with the servo kept on. Plain zero-velocity damping would let an external or gravity load drive the axis away (found with the Motor Simulator). Resetting such a trip keeps the axis held and enabled. Trips latch until an operator resets them. |
| 6 | Software E-stop | HMI | E-STOP button, F12 or Pause key, also on the lock screen. Latches; configurable torque-off or brake. Needs a confirmed reset by an Operator or higher, and motors stay disabled until enabled again. Esc is deliberately not an E-stop key, because it closes dialogs and would cause nuisance stops. |
| 7 | Fail-safe software | HMI | An unexpected exception stops all motors. Closing the app or disconnecting stops all motors. Jog is hold-to-run and also stops on focus loss. A controller fault is not auto-cleared, so it stays visible until acknowledged. |

## Operating rules

* Enable is explicit, per motor, and holds the current position (or zero speed on ST boards).
* Calibration requires a five-item checklist and a typed confirmation. The HMI releases the
  adapter, runs the official `moteus_tool`, reconnects, and commands stop.
* Commissioning sequence: set limits, push the limits to the controller, test in
  the Motor Simulator, test on hardware at reduced torque and speed, then raise the limits.

## Residual risks to cover in your risk assessment

* ST MCSDK keeps its last setpoint if the link dies (layer 2 is missing).
* Python and Qt are not real-time. Loop jitter is absorbed by the controller watchdog
  (≥ 3 loop periods, enforced). Overruns are counted in the status bar.
* The HMI cannot detect mechanical failures (broken coupling, slipping encoder magnet)
  except through following-error monitoring. Set `max_following_error`.
