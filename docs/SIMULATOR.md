# Testing without hardware: Motor Simulator walkthrough

The **Motor Simulator** app runs virtual moteus controllers. The HMI talks to them over a
virtual CAN-FD bus, through the same driver code (moteus library, then python-can) as a
real r4.11 on a USB-CAN-FD adapter. You change the simulated motor in the simulator window
and watch the HMI react.

Every step and expected result below was checked with an automated run of both apps.

## A. Start both apps (once)

1. **Simulator.** Run `python -m moteus_hmi.sim`, or click *▶ Launch Motor Simulator* in the
   HMI. You get two motor cards (CAN ID 1 and 2) and an amber bar: *Waiting for the HMI*.
2. **HMI.** Run `python -m moteus_hmi` and sign in.
3. In the HMI's left panel, set:
   * **Controller:** *mjbots moteus r4.11*
   * **Interface:** *Motor Simulator app (virtual CAN-FD bus)*
   * **Bus group** `239.74.163.2` and **Bus port** `43113`, the same values as the
     simulator's top bar.

   Then click **Connect**.
   * The HMI header shows **VIRTUAL CAN-FD · MOTOR SIMULATOR APP**, and *Motors* lists ID 1
     and ID 2.
   * In the simulator, the amber bar disappears and RX/TX count up (about 100 frames/s per
     motor).
4. Put the two windows side by side.

## B. HMI → simulator (input)

| Do in the HMI | See in the simulator (card *CAN ID 1*, column *From HMI (input)*) |
|---|---|
| *Test & Control* → select ID 1 → **Enable** | Requested mode **Position** (holding the current position) |
| *Position* tab → Pos `1.0` → **Send** | Position `+1.0000 rev`, Vel limit, Acc limit, Watchdog `0.25 s`, Command age of a few ms. The dial turns to 1 turn. |
| *Velocity* tab → `0.5` → **Send** | Requested mode Position with Position `default` (NaN = velocity mode), Velocity `+0.5000` |
| Hold **Jog +** | Velocity = jog speed while held, back to 0 on release |
| **E-STOP** (top right) | Requested mode **Stopped**, firmware mode Stopped |

## C. Simulator → HMI (output): change the simulation, watch the HMI

Start each experiment with motor 1 enabled and holding position 1.0 (step B).

| # | Change in the simulator (card ID 1) | Where to look in the HMI | What you should see | To recover |
|---|---|---|---|---|
| 1 | **External load** `0.3 N·m` | Test & Control: Torque plot, Position plot | Torque ≈ **−0.31 N·m** (controller pushing back); position settles ≈ **0.05 rev** past target | Load `0` |
| 1b | (keep load) In the HMI: *Safety & Alarms* → tick **Max following error** `0.02` → *Apply to motor* | Status, alarm banner | **TRIPPED - Following error … > ±0.0200**. The axis **holds position** and does not run away. | Load `0`, untick, Apply, **Reset fault** (stays enabled) |
| 2 | **Sensor noise** `5×` | Test & Control (tick *Show raw*); *Feedback & Filters* → Velocity | Grey raw trace much noisier, blue filtered stays smooth; noise table e.g. raw σ ≈ 0.33 → filtered ≈ 0.017 (**≈ 26 dB**). Change the filter (e.g. cut-off 5 Hz → Apply) and watch the dB change. | Noise `1×` |
| 3 | **Supply voltage** `9 V` | Status, alarm banner | **TRIPPED - Bus voltage 9.8 V < 10.00 V** (UNDERVOLTAGE), motor torque off | `24 V` → **Reset fault** → **Enable** |
| 3b | **Supply voltage** `45.5 V` | Status | **TRIPPED - Bus voltage … > 44.00 V** (OVERVOLTAGE, r4.11 rating) | same |
| 4 | **Overheat (85 °C)** | Status, Board T | **Controller fault 38: Over temperature**, Board T ≈ 85 °C | **Clear injections** → **Reset fault** → **Enable** |
| 5 | **Driver fault 33** | Status; *Console* → Read channel `drv8323` | **33: Motor driver fault**; drv8323 shows `fault: true` | same |
| 6 | **Encoder fault 35** | Status | **35: Encoder fault** | same |
| 7 | Tick **Cable cut** | Status, Motors list, *Safety & Alarms* journal | **TRIPPED - No reply for 10 cycles**, motor list *NO COMM*. **Motor 2 keeps working.** In the simulator: Command age turns red, firmware mode **Timeout** (the controller stopped itself). | Untick → **Reset fault** → **Enable** |
| 8 | Tick **Uncalibrated** | Status after Enable / Send | **36: Motor not configured** | Untick → **Reset fault** → **Enable** |
| 9 | (any time) | *Firmware* → **Read version** | Firmware ABI 1.1.0 (0x010100), model 0xa, serial, git hash, build date | none |
| 10 | (any time) | *Parameters* → **Read all** → change `servo.pid_position.kp` 4 → 8 → **Apply**; then *Tuning* → **Run step test** | Faster rise / more overshoot in the step metrics than with kp 4 | set kp back to 4 |

All alarms are listed with time, motor, severity and message in **Safety & Alarms →
Alarm journal**, and every action is recorded in **Users & Audit**.

Command line options:

```
python -m moteus_hmi.sim --ids 1,2,3            # node IDs
                         --channel 239.74.163.2  # virtual bus group  } must match the HMI's
                         --port 43113            # virtual bus port   } "Bus group / Bus port"
                         --headless              # no window (CI, servers)
                         --interface socketcan --channel vcan0   # Linux vcan instead of UDP
```

Linux `vcan` alternative (root needed once):

```bash
sudo modprobe vcan && sudo ip link add vcan0 type vcan && sudo ip link set vcan0 mtu 72 up
python -m moteus_hmi.sim --interface socketcan --channel vcan0
# HMI: Interface → SocketCAN, channel vcan0
```

A virtual bus is identified by its **UDP port** (plus group). Two simulators or HMIs on
the same port form one bus. That is how to test several HMIs against one machine, and it
is also why the HMI reports "Duplicate CAN ID" if two simulators with the same IDs share a
port. Use different ports for independent benches on one PC.

## Hardware-in-the-loop bench

With two CAN-FD adapters cabled together (for example a PCAN-USB FD running the
simulator and an fdcanusb running the HMI), the simulator can act as fake controllers on
a physical bus:

```
python -m moteus_hmi.sim --interface pcan --channel PCAN_USBBUS1 --allow-hardware-bus
```

The `--allow-hardware-bus` flag is required on purpose. A simulated node on a real
machine's bus could spoof feedback, so never use it on a production machine.

## What is not simulated

Calibration routines (`d pwm`, `d vdq` …), firmware flashing, and flow-controlled
diagnostic polling. (Firmware version read-out, `servo_stats` and `drv8323` **are**
simulated, so *Firmware → Read version* and `moteus_tool --info` work.) The client library falls back
to plain polling automatically (≈0.5 s once per connection). Validate those on the real
r4.11.
