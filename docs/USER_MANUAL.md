# Motor HMI User Manual

Nov 9, 2026 · Meena Salika

Motor HMI 1.0.0 is a secure desktop HMI for commanding, calibrating and monitoring brushless motor controllers on Windows and Linux. It ships with a Motor Simulator, so the whole HMI can be tested without hardware.

This manual is for operators and commissioning engineers. Architecture, protocols, the safety and security design, tests and release are in the [Project Documentation](PROJECT_DOCUMENTATION.md).

| Item | Value |
| --- | --- |
| Version | 1.0.0 |
| Source code | `~/moteus_hmi` (Python 3.12, PySide6 6.12, moteus library 1.1.3) |
| Verification | 69 automated tests, all passing; end-to-end runs against the Motor Simulator |
| Not yet validated | real moteus r4.11 and X-NUCLEO-IHM08M1 hardware; a Windows build |

> The software E-stop and trips in this HMI are not a safety-rated function. Every machine needs a hard-wired emergency stop that removes motor power independently of the PC.

### 1. Overview

Motor HMI lets an operator connect to one or more motor controllers, enable them, command motion and watch filtered feedback in real time. Every action passes through safety limits and is recorded in a tamper-evident audit trail.

**Two applications**

| App | Start command | Purpose |
| --- | --- | --- |
| Motor HMI | `python -m moteus_hmi` | Operator and engineering interface for real or simulated controllers |
| Motor Simulator | `python -m moteus_hmi.sim` | Virtual moteus controllers on a virtual CAN-FD bus, with input/output view and fault injection |

**Supported controllers** (the *Controller* drop-down)

| Controller | Link | Supply | Peak current | Control modes |
| --- | --- | --- | --- | --- |
| mjbots moteus r4.11 | USB-CAN-FD (fdcanusb, PCAN, Kvaser, SocketCAN) or Motor Simulator | 10-44 V | 100 A | Position, Velocity, Torque, Impedance, Hold, Brake, Stay-within |
| mjbots moteus n1 / x1 | as above | 10-54 V | 100 / 120 A | as above |
| mjbots moteus c1 | as above | 10-51 V | 20 A | as above |
| ST X-NUCLEO-IHM08M1 + Nucleo (MCSDK 5.x) | USB serial (ST-LINK virtual COM) | 10-48 V | 15 A | Velocity, Torque |
| Any STM32 MCSDK 5.x board | USB serial | 8-48 V | per board | Velocity, Torque |

**Main features**

- Live position, velocity and torque plots with raw, filtered and setpoint traces, and a rotating motor dial.
- Control with sliders bounded by safety limits, Send or Live send, and hold-to-run jog.
- Noise reduction per signal: spike rejection, notch filter, then low-pass or Kalman smoothing, with measured dB reduction.
- Calibration through the official `moteus_tool`, zero offset, and controller-side encoder filter settings.
- Parameter read/write/save with signed export and import, PID tuning with a step-response test, and firmware version read-out and upgrade.
- Safety supervision: limits, trips, latched E-stop and per-node fault isolation.
- Security: four user roles, scrypt passwords, account lockout, auto-lock and an HMAC-chained audit trail.

### 2. Installation and first start

Run from source on a development PC, or install a packaged build on an operator laptop. Both need Python 3.12 only when running from source.

To install the HMI on another laptop, follow [Motor HMI Installation Guide](INSTALLATION.md).

**Run from source (Linux)**

1. One-time system library for Qt windows: `sudo apt install libxcb-cursor0`.
2. Create the environment once:

   ```
   cd ~/moteus_hmi
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Start the HMI: `env -u PYTHONPATH .venv/bin/python -m moteus_hmi`.
4. Start the simulator, if needed: `env -u PYTHONPATH .venv/bin/python -m moteus_hmi.sim`.

`env -u PYTHONPATH` keeps a sourced ROS environment from injecting its own packages. On Windows use `.venv\Scripts\python -m moteus_hmi`.

**Packaged builds**

| OS | Install | Start |
| --- | --- | --- |
| Linux | unpack `MotorHMI-<ver>-linux-x86_64.tar.gz`, run `sudo ./install.sh` | `motor-hmi`, or the desktop entry |
| Windows | run `MotorHMI-<ver>-setup.exe` (admin) | Start menu → Motor HMI |

The Linux installer verifies SHA-256 checksums and installs to `/opt/motor-hmi`, read-only for operators. It also installs `libxcb-cursor0` and a udev rule for fdcanusb and ST-LINK ports, and adds the user to `dialout`; log out and in once afterwards. Packaged builds start the simulator with `MotorHMI --simulator`.

**First start**

1. The HMI asks you to create the administrator account. There are no default passwords.
2. Passwords need at least 10 characters with upper case, lower case and a digit, and no part of the user name.
3. A one-time **recovery code** is shown (format `XXXXX-XXXXX-XXXXX-XXXXX`). Store it offline; it is the only way back in if the user database is lost or tampered with.
4. Sign in with the new account.

**Where data is stored**

| OS | Folder |
| --- | --- |
| Linux | `~/.local/share/moteus-hmi` |
| Windows | `%LOCALAPPDATA%\MotorHMI` |
| Portable / test | any folder via `--home <folder>` or `MOTEUS_HMI_HOME` |

The folder holds settings, users, the audit trail, logs, recordings, calibration reports and configuration backups. Only one HMI can run per data folder.

### 3. Users, roles and sessions

Each person signs in with their own account. Their role decides which tabs and buttons are available. The E-STOP works for every role, even on the lock screen.

| Capability | Viewer | Operator | Engineer | Admin |
| --- | --- | --- | --- | --- |
| See telemetry and alarms, E-STOP | yes | yes | yes | yes |
| Connect, enable/disable, command motion, jog | no | yes | yes | yes |
| Reset trips and E-stop, record data | no | yes | yes | yes |
| Calibration, tuning, parameters, safety limits, filters, console | no | no | yes | yes |
| Firmware upgrade (approved images only) | no | no | yes | yes |
| Manage users, approve firmware, verify/export audit trail | no | no | no | yes |

**Sign-in rules**

- Five wrong passwords lock the account for 5 minutes, doubling on each repeat. An admin can unlock it in *Users & Audit*.
- New or reset accounts must change their password at first sign-in.
- After 10 minutes without input, the screen locks; the timeout is set in *Users & Audit*. The same or another user can sign in on the lock screen, and a user switch is audited.
- **Log out** disables any enabled motors first, after confirmation.

**Managing users (Admin):** *Users & Audit* → **Add…**, **Reset password…**, **Change role…**, **Unlock**, **Enable / disable**, **Delete**. The last administrator cannot be demoted, disabled or deleted.

### 4. Main window tour

The window has four areas: the header with the E-STOP, the connection panel on the left, the tabs in the centre and the status bar at the bottom. Colour is used only for states that need attention: blue = enabled, amber = warning or simulation, red = alarm or trip.

**Header**

| Element | Meaning |
| --- | --- |
| Orange banner *VIRTUAL CAN-FD · MOTOR SIMULATOR APP* | Connected to simulated controllers, not hardware |
| Amber banner *NO CONTROLLER WATCHDOG* | ST MCSDK board: it keeps its last setpoint if the PC stops |
| Red banner *n ALARMS* | Unacknowledged active alarms (see *Safety & Alarms*) |
| Lock / Log out | Lock the screen, or end the session |
| Reset E-stop | Releases a latched E-stop after confirmation (Operator and above) |
| **■ E-STOP** (also F12 or Pause) | Stops every motor and latches |

**Connection panel (left)**

- **Controller:** controller family; the line under it shows its supply range, peak current and modes (hover ⓘ for notes).
- **Interface:** USB-CAN-FD auto-detect, fdcanusb, SocketCAN, python-can, *Motor Simulator app (virtual CAN-FD bus)*, or USB serial for ST boards.
- **Link settings:** serial port and baud rate, CAN channel, or bus group and port, depending on the interface.
- **CAN prefix (adv.):** normally 0; it is not the motor ID.
- **Node IDs:** blank means auto-discover.
- **Refresh / Connect / Disconnect:** the motors list shows each ID as *disabled*, *ENABLED*, *TRIP*, *NO COMM* or *E-STOP*.
- **Quick actions:** Export / Import params, Calibrate, Firmware.

**Tabs:** Test & Control · Bus & Devices · Parameters · Calibration · Tuning · Feedback & Filters · Safety & Alarms · Parse · Firmware · Console · Users & Audit · About. Tabs a role may not use are hidden.

**Status bar:** Save data (record) · Clear data (plots) · EN/DIS for the selected motor · RX and TX bytes/s · approximate bus load · RX/TX frame counts · control-loop rate and overruns · Clear count.

### 5. Connecting to hardware

moteus controllers need a CAN-FD adapter at 1 Mbit/s nominal and 5 Mbit/s data. Classic CAN 2.0 and slcan/canable-type adapters do not work.

**moteus r4.11 (and n1, c1, x1)**

| Adapter | Windows | Linux | Interface to choose |
| --- | --- | --- | --- |
| mjbots fdcanusb / mjcanfd-usb-1x | COM port, built-in driver | `/dev/fdcanusb` (udev rule) | *fdcanusb* or *USB-CAN-FD auto-detect* |
| PEAK PCAN-USB FD | PCAN driver | `peak_usb` SocketCAN | *python-can* → `pcan`, or *SocketCAN* |
| Kvaser, Vector, IXXAT (FD models) | vendor driver | SocketCAN | *python-can* or *SocketCAN* |

SocketCAN needs the bit timing set once per boot:

```
sudo ip link set can0 up type can bitrate 1000000 dbitrate 5000000 \
     sjw 10 dsjw 5 sample-point 0.666 dsample-point 0.666 restart-ms 1000 fd on
```

1. Wire the bus with 120 Ω termination at both ends, and give each controller a unique CAN ID.
2. Power the controllers (r4.11: 10-44 V).
3. Select the controller and interface, leave *CAN prefix* at 0 and *Node IDs* blank, then click **Connect**.
4. Check *Bus & Devices*: every controller should show *Comm OK*.
5. Before the first motion, set the limits in *Safety & Alarms* and click *Write soft limits + watchdog to controller*.

**ST X-NUCLEO-IHM08M1 on an STM32 Nucleo**

The IHM08M1 is a power stage only; motor control runs on the Nucleo with ST Motor Control SDK 5.x firmware.

1. In MC Workbench, enable *Motor Control Protocol* on the UART routed to the ST-LINK virtual COM port (115200 baud by default), and flash the firmware.
2. Plug in the Nucleo USB. In the HMI choose *STMicroelectronics X-NUCLEO-IHM08M1*, interface *USB serial / ST-LINK*, and select the port.
3. Check **A / digit** (current scaling; default 0.000972 A for IHM08M1) and **Kt** (your motor's torque constant, in N·m/A), then click **Connect**.

Through this protocol the board offers velocity (speed ramp) and torque (Iq) control, start/stop, fault acknowledge, encoder alignment and RAM-only gain tuning. It has no absolute position and no command watchdog. MCSDK 6.x (ASPEP/MCP) is not supported yet.

**If the connection fails**, the HMI names the reason: no adapter found, a node that did not answer (with a hint about CAN prefix, IDs and power), or a duplicate CAN ID. With no hardware attached, the error dialog offers **Use Motor Simulator**.

### 6. Testing without hardware: Motor Simulator

The Motor Simulator behaves like real moteus controllers on a virtual CAN-FD bus. The HMI talks to it through the same driver code as a real r4.11, so you can practise every operation and fault before touching hardware. Every result below was checked with an automated run of both apps.

**A. Start and connect**

1. Terminal 1: `cd ~/moteus_hmi && env -u PYTHONPATH .venv/bin/python -m moteus_hmi.sim`. Two motor cards appear (CAN ID 1 and 2), with an amber bar: *Waiting for the HMI*.
2. Terminal 2: `cd ~/moteus_hmi && env -u PYTHONPATH .venv/bin/python -m moteus_hmi`, then sign in.
3. In the HMI set *Controller* = mjbots moteus r4.11 and *Interface* = **Motor Simulator app (virtual CAN-FD bus)**. Keep Bus group `239.74.163.2`, Bus port `43113`, CAN prefix `0`, and Node IDs blank.
4. Click **Connect**. The HMI header turns orange, the motors list shows ID 1 and ID 2, the simulator's amber bar disappears, and its RX/TX counters run.

**B. HMI → simulator (what the controller receives)**

| Do in the HMI | See in the simulator card *From HMI (input)* |
| --- | --- |
| Test & Control → **Enable** | Requested mode Position (holding the current position) |
| Position tab → Pos `1.0` → **Send** | Position +1.0000 rev, Vel limit, Acc limit, Watchdog 0.25 s, Command age a few ms; the dial turns |
| Tick **Live send**, drag the Pos slider | Each new position arrives immediately (0.3 → +0.3000, 0.6 → +0.6000) |
| **E-STOP** | Requested mode Stopped |

**C. Simulator → HMI (change the motor, watch the HMI)**

Start each line with motor 1 enabled and holding 1.0 rev.

| Change in the simulator | HMI shows | Recover |
| --- | --- | --- |
| External load 0.3 N·m | Torque about −0.31 N·m; position about 0.05 rev past target | Load 0 |
| Same load, plus *Max following error* 0.02 rev in Safety & Alarms | TRIPPED - following error; the axis holds position, no runaway | Load 0, untick, Reset fault (stays enabled) |
| Sensor noise 5× | Noisier grey raw trace; Feedback & Filters shows about 26 dB reduction | Noise 1× |
| Supply 9 V | TRIPPED - bus voltage below 10.00 V | 24 V, Reset fault, Enable |
| Supply 45.5 V | TRIPPED - bus voltage above 44.00 V (r4.11 rating) | same |
| Overheat (85 °C) | Controller fault 38: Over temperature | Clear injections, Reset fault, Enable |
| Driver fault 33 / Encoder fault 35 | Controller fault 33 / 35 with its text | same |
| Cable cut | TRIPPED - no reply for 10 cycles; motor 2 keeps running; simulator shows mode Timeout | Untick, Reset fault, Enable |
| Uncalibrated | Fault 36: Motor not configured | Untick, Reset fault, Enable |

Any time: *Firmware → Read version* shows the simulated firmware (ABI 1.1.0, model 0xa, serial, git hash). *Parameters → Read all* reads the simulated configuration, and changes are applied to the simulated motor.

**Not simulated:** calibration routines and firmware flashing (those buttons are disabled on the virtual bus), and ST boards.

### 7. Operating motors (Test & Control)

A motor moves only after it is **enabled**, and every value you send is limited by its safety limits. The left side shows live Position, Velocity and Torque plots; the right side holds the controls.

**Standard sequence**

1. Select the motor in **Node ID** (or in the motors list).
2. Click **Enable**. The motor holds its current position, the button turns blue, and the status shows *ENABLED*.
3. Pick a mode tab, set its values, and click **Send**, or tick **Live send**.
4. Click **Disable** to switch torque off; the button turns red.

Enable and Disable are grey until pressed, then show the motor's actual state. A trip or E-stop turns Enable grey again.

**Control modes**

| Tab | Fields | What the motor does |
| --- | --- | --- |
| Impedance (MIT-style) | Pos, Vel, Kp scale, Kd scale, Tor | torque = Kp·Δposition + Kd·Δvelocity + Tor; the scales multiply the controller's configured gains |
| Position | Pos, Vel limit, Acc limit, Max tor | trapezoidal move to the target |
| Velocity | Vel, Acc limit, Max tor | constant speed (ST boards: a speed ramp) |
| Torque | Tor | feed-forward torque only; trips to hold above the over-speed limit |
| Stay within | Lower, Upper, Max tor | moves freely inside the bounds, pushes back outside |
| Hold / Brake | buttons | Hold = zero-velocity damping; Brake = phases shorted |

Each field is a number box plus a slider with MIN and MAX labels. The range comes from the motor's safety limits; anything above them is clamped, and the HMI says what was limited.

**Send controls**

- **Send:** sends the current mode's values once. The HMI keeps re-sending them at the loop rate with the watchdog, so the motor holds the command.
- **Live send + period (ms):** while ticked, every slider or value change is sent immediately, at most once per period. It needs an enabled motor and switches itself off on a trip or E-stop.
- **Update:** loads the measured position into the fields. **Clear:** zeroes position, velocity and torque fields, and clears the plots.
- **Jog ◀ / ▶ (hold to run):** moves at the jog speed only while the button is held; it stops on release or when the window loses focus.

If a command is refused, a red line under the buttons says why and what to press. For example: *Live send refused: Motor is not enabled. Press Enable first.*

**Other controls**

- **Save Zero:** makes the current output position zero and stores it in flash (Engineer; motor disabled).
- **Change ID…:** writes a new CAN ID, rescans the bus and saves.
- **Reset fault:** acknowledges a trip after the cause is fixed.
- **Display units:** rev, rad or deg, for plots and fields.
- **Plot options:** Show raw (grey), Show setpoint (dashed), Pause, window length 2-120 s.
- **Motor (live):** a dial with the rotor and magnet poles at the shaft angle, the target marker, a speed arc and a torque bar.
- **Status / CAN data:** state, fault text with the next step, temperatures, bus voltage, home state, and the last TX/RX frame in hex.

### 8. Feedback, noise reduction and data recording

Noise is reduced in two places. Inside the controller, the encoder and current-loop filters act at 15-60 kHz (set in *Calibration*). In the HMI, a per-signal filter chain cleans what you see, what is recorded, and what the alarm checks evaluate. The HMI filters do not change the motor's control loop.

**HMI filter chain (Feedback & Filters tab):** three stages per signal, applied in order:

| Stage | Options | Use it for |
| --- | --- | --- |
| 1 · Spike rejection | none, Median (window), Hampel (window, threshold σ) | single corrupt samples; Hampel passes normal samples untouched |
| 2 · Notch | on/off, centre Hz, Q | a fixed tone such as mains pickup or a resonance |
| 3 · Smoothing | none, EMA, Butterworth 2nd order (cut-off Hz), Kalman (q, r) | broadband noise; a lower cut-off is smoother but lags more |

1. Select a signal from the list: position, velocity, torque, Iq, Id, power, voltage, board or motor temperature, following error, or velocity error.
2. Set the stages, choose *All motors* or *Selected motor*, and click **Apply** (Engineer).
3. Compare the grey raw trace with the blue filtered one, and read the noise table: raw σ, filtered σ and reduction in dB.

Defaults filter velocity, torque and current with Hampel plus 15 Hz Butterworth, voltage with median plus EMA, and temperatures with a 0.5 Hz EMA. Position is left unfiltered. Cut-offs above 40 % of the loop rate are reduced automatically on slow links.

**Recording**

- **● Save data** (status bar) or **Start recording** writes a CSV of every sample: time, motor ID, mode, fault, raw signals and `_filt` columns.
- On stop, a `.sha256` file is written next to the CSV; the Parse tab uses it to show whether the file was modified.
- Files are stored in the data folder under `recordings/`.

**Parse tab**

- **Frame decoder:** paste hex bytes, or click *Use last TX frame*, then decode as a moteus CAN-FD register frame or an ST FCP frame.
- **Recording viewer:** open a CSV, plot any column against time, see min/max/mean/std per column, and see the checksum result.

### 9. Calibration

A bare moteus controller must be calibrated for its motor before any control mode works; until then it reports fault 36. Calibration spins the motor in both directions at speed, so it is an Engineer task behind a checklist.

**Motor and encoder calibration (moteus)**

1. Open *Calibration*, select the motor, and confirm all five checklist items: shaft free to spin, guards in place, supply current limit set, motor clamped, and authorised to commission.
2. Review the options:

| Option | Default | Notes |
| --- | --- | --- |
| Calibration power | 7.5 W | lower for small motors, higher for large ones |
| Torque bandwidth | 200 Hz | higher = faster torque, more audible noise |
| Encoder filter BW | auto | sets the encoder PLL filter |
| Motor poles | auto |  |
| Known Kv | measure | limits rotation to about one turn each way |
| Hall sensors / Invert direction | off |  |
| Dry run | off | measures without changing the configuration |

3. Click **Run calibration** and type `CALIBRATE`. The HMI releases the adapter and runs the official `moteus_tool --calibrate`, showing its output live.
4. When it finishes, the HMI reconnects and commands stop. The report shows winding resistance, inductance, Kv, bandwidths and the device serial, and is saved to *Calibration history*.

**Abort** stops the tool immediately; the HMI then reconnects and commands stop.

**Zero and output reference** (motor disabled)

- *Set output = value (temporary)*: the current position reads the given value until power-off.
- *Set output nearest*: same, but keeps the whole-turn count closest to the current reading.
- *Save as zero offset (flash)*: stores the offset permanently (`d cfg-set-output`, then `conf write`).

**Controller-side noise reduction**

| Parameter | Effect |
| --- | --- |
| `motor_position.sources.0.pll_filter_hz` | encoder low-pass: lower = less jitter and audible noise, higher = more bandwidth; 0 disables it and removes velocity |
| `servo.pid_dq_hz` | current-loop bandwidth, set by calibration |
| `servo.max_current_desired_rate` | limits current slew (A/s), smoothing torque steps |

**Read**, **Write** (RAM) and **Save to flash** apply to these three values.

**ST boards:** motor parameters are identified with ST Motor Profiler in MC Workbench. The HMI offers **Encoder alignment**, after typing `ALIGN`.

**On the Motor Simulator** calibration and flashing are disabled, because the simulator emulates the bus protocol but not the firmware routines.

### 10. Parameters, tuning, firmware and console

All controller configuration work is for Engineers and is audited. Changes go to RAM first, and become permanent only with **Save to flash**.

**Parameters tab**

1. **Read all** loads every parameter (`conf enumerate`). Type in the filter box to narrow the list, for example `pid_position`.
2. Double-click a value to edit it; edited values turn blue. Orange names are hardware or motor-model parameters.
3. **Apply changes** writes them to RAM. Changing hardware parameters needs typing `APPLY`.
4. **Save to flash** (`conf write`) needs typing `SAVE`.

**Export / Import parameters**

- **Export** writes a backup file signed with HMAC-SHA256. It is compatible with `moteus_tool --restore-config`.
- **Import** checks the signature and shows *valid*, *unsigned* (for example a `moteus_tool --dump-config` file) or *INVALID* (changed after export, or exported on another laptop, needs typing `IMPORT ANYWAY`).
- A diff dialog lists only the differing values. `id.id` and `can.prefix` are unchecked by default because they break communication.

**Tuning tab**

- **Gains:** moteus Kp, Ki, Kd, integral limits, default velocity and acceleration limits, and inertia feed-forward. ST boards show speed, Iq and Id PI gains, RAM only. Each has **Read**, **Write (RAM)** and **Save to flash**.
- **Step test** (moteus, motor enabled): set the amplitude, record time and limits, then **Run step test**. The motor moves by the amplitude and back. The plot shows measured position, trajectory and target, with rise time (10-90 %), overshoot, settling time (±2 %), steady-state error and peak torque.

**Firmware tab**

- **Read version** shows firmware ABI (for example 1.1.0, 0x010100), model, serial number, git hash and build date, plus the HMI and moteus library versions.
- **Upgrade (moteus):** *Open file…* an `.elf`; its SHA-256 is shown. An Admin must **Approve this hash** once. **Upgrade** (type `FLASH`) runs `moteus_tool --flash`, which preserves the configuration. ST boards are flashed with STM32CubeProgrammer instead.

**Console tab (Engineer)** accepts only `conf get/set/enumerate/write`, `tel list`, `d stop`, `d rezero`, `d cfg-set-output`, and ST `cmd`/`info`. Motion commands are refused here; use Test & Control. **Read channel** decodes telemetry such as `servo_stats`, `drv8323`, `firmware` and `git`. Every console command is audited.

### 11. Safety and alarms

The HMI checks every command against per-motor limits, and every feedback sample against trip conditions. A trip latches until an operator fixes the cause and presses **Reset fault**. This is a software layer; the machine's hard-wired E-stop is still required.

**Per-motor limits (Safety & Alarms tab, Engineer)**

| Limit | Default | Hard ceiling |
| --- | --- | --- |
| Max velocity | 2 rev/s | 50 rev/s |
| Max acceleration | 10 rev/s² | 500 rev/s² |
| Max torque | 0.5 N·m | 30 N·m |
| Soft position limits min / max | off | none |
| Bus voltage min / max | 10 V / 46 V, capped to the board rating (r4.11: 44 V) | 8 V / 54 V |
| Max board / motor temperature | 75 °C / 90 °C | 90 °C / 130 °C |
| Max following error | off | none |
| Over-speed trip factor | 1.25 × max velocity | 2.0 |
| Max Kp / Kd scale | 1.0 | 2.0 |

**Apply to motor** or **Apply to all motors** saves the limits in signed settings. **Write soft limits + watchdog to controller** also stores position limits and the command timeout in the moteus flash, so they hold even if the PC fails.

**Global settings:** E-stop action (torque off, or brake), control-loop rate (default 100 Hz), controller watchdog (default 0.25 s, at least three loop periods), and comm-loss trip (default 10 missed replies).

**Trips and reactions**

| Alarm | Cause | Reaction |
| --- | --- | --- |
| FW\_FAULT | controller reports a fault (e.g. 33 driver, 35 encoder, 36 not calibrated, 38 over-temperature) | torque off; fault kept visible until reset |
| FW\_TIMEOUT | controller watchdog expired while enabled | torque off |
| COMM\_LOSS | no reply for N cycles (other motors unaffected) | torque off; controller watchdog also stops it |
| UNDERVOLTAGE / OVERVOLTAGE | bus voltage outside min / max | torque off |
| BOARD\_TEMP / MOTOR\_TEMP | temperature above its limit (warning 10 °C before) | torque off |
| OVERSPEED | filtered speed above factor × max velocity | controlled stop, hold position |
| SOFT\_LIMIT | position beyond a soft limit while moving outward | controlled stop, hold position |
| FOLLOWING\_ERROR | position error above the set maximum | controlled stop, hold position |

"Hold position" keeps the servo on, torque-limited, at the point of the trip, so an external load cannot drive the axis away. Reset fault then leaves the axis enabled and held. All other trips leave the motor disabled after reset.

**Recovery checklist**

1. Read the red status line and the *Alarm journal* in Safety & Alarms.
2. Remove the cause: power, wiring, load, temperature, or the limit itself.
3. Press **Reset fault** (Test & Control), or **Reset E-stop** (header) after an E-stop.
4. Press **Enable**, then send a command.

**Acknowledge all** clears the alarm banner count; **Export CSV** saves the journal.

### 12. Troubleshooting and FAQ

Most problems so far came from interface or ID settings, not from the motor. Check this table first.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| HMI window does not open; error mentions `xcb` | Qt needs `libxcb-cursor0` | `sudo apt install libxcb-cursor0` |
| *No CAN-FD adapter found* | Interface is USB-CAN-FD auto-detect with no adapter plugged in | Plug in the adapter, or click **Use Motor Simulator** in the dialog |
| Simulator shows *Waiting for the HMI*, RX 0 | HMI interface is not *Motor Simulator app*, or the bus group/port differ | Disconnect, set Interface and port 43113 on both, Connect |
| *Node ID(s) 1 did not answer … CAN prefix* | CAN prefix is not 0, or the wrong Node IDs are listed | Set CAN prefix 0 (it is not the motor ID), clear Node IDs, Connect |
| *Duplicate CAN ID detected* | Two controllers, or two simulators on one port, share an ID | Give each controller a unique ID (Change ID…), or use separate ports |
| *TRIPPED - No reply for 10 cycles* | Cable, power or bus problem on that motor | Fix the link, then Reset fault and Enable |
| *Live send refused / Send refused* | Motor not enabled, tripped or E-stop latched | Follow the red line: Reset fault or Reset E-stop, then Enable |
| Enable refused: *Controller fault 36* | Motor not calibrated | Run calibration (Engineer) |
| *Motor HMI is already running* | Another HMI uses the same data folder | Close the other instance, or start with `--home <folder>` |
| Motor spins on its own in the simulator | External load applied while the motor is stopped (torque off) | Set load to 0, or Enable so the controller holds against it |
| Calibration or Upgrade buttons are disabled | On the Motor Simulator, or missing Engineer/Admin role | Use real hardware with an Engineer account |
| *User database signature mismatch* | users file edited or deleted outside the HMI | Enter the recovery code to create a new administrator |
| Many *overruns* in the status bar | PC busy; GUI and control loop share the CPU | Close other apps or lower the loop rate; the controller watchdog still protects the motor |

**Log files:** `logs/hmi.log` in the data folder (rotating, 5 × 5 MB). **Audit trail:** *Users & Audit* → **Verify integrity**.
