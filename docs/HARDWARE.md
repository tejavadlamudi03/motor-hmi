# Hardware integration

## mjbots moteus r4.11 (and n1 / c1 / x1)

**Adapter:** CAN-FD is mandatory: 1 Mbit/s nominal, 5 Mbit/s data. Classic CAN 2.0
adapters and slcan / canable-derived adapters do **not** work.

| Adapter | Windows | Linux | Interface in HMI |
|---|---|---|---|
| mjbots fdcanusb / mjcanfd-usb-1x | COM port (driver built in) | `/dev/fdcanusb` (udev rule) or SocketCAN (gs_usb firmware) | *fdcanusb* or *Auto* |
| PEAK PCAN-USB FD | PCAN driver | `peak_usb` SocketCAN | *python-can → pcan* / *SocketCAN* |
| Kvaser, Vector, IXXAT (FD models) | vendor driver | SocketCAN | *python-can* |

SocketCAN bring-up (Linux):

```bash
sudo ip link set can0 up type can bitrate 1000000 dbitrate 5000000 \
     sjw 10 dsjw 5 sample-point 0.666 dsample-point 0.666 restart-ms 1000 fd on
```

**Wiring:** 120 Ω termination at both bus ends. Every controller needs a unique `id.id`
(change it with *Test & Control → Change ID…*). The r4.11 accepts 10-44 V (≤ 10S),
100 A peak.

**First power-up checklist**

1. Connect, then **Bus & Devices**: every motor must appear with *Comm OK*.
2. **Safety & Alarms**: set conservative limits, then *Write soft limits + watchdog to
   controller*.
3. **Calibration**: complete the checklist, run calibration with the motor unloaded, and
   check the report (R, L, Kv).
4. **Test & Control**: Enable, move slowly in Position mode, and watch the following error.
5. **Tuning**: run a step test and adjust Kp/Kd in small steps, then save.

## ST X-NUCLEO-IHM08M1 + STM32 Nucleo

The IHM08M1 is a 10-48 V / 15 A rms three-phase power stage (STL220N6F7 MOSFETs, L6398
drivers, 10 mΩ shunts). Its control comes from the Nucleo board below it running **ST
Motor Control SDK 5.x** firmware generated in MC Workbench:

* In MC Workbench, enable *Serial communication → Motor Control Protocol* on the UART
  routed to the ST-LINK virtual COM port, at 115200 baud by default.
* Connect the Nucleo USB. In the HMI choose *Controller → STMicroelectronics
  X-NUCLEO-IHM08M1*, *Interface → USB serial / ST-LINK*, select the port, then Connect.
* Set **A / digit** (current scaling of your power board) and **Kt** (motor torque
  constant) so that torque reads in N·m. The defaults are for IHM08M1 (10 mΩ, gain 5.18).
* Available: Velocity (speed ramp, using the acceleration limit as ramp time), Torque (Iq
  reference), start/stop, fault acknowledge, encoder alignment, tuning of speed / Iq / Id
  PI gains (RAM only), and live bus voltage / heatsink temperature / power / faults.
* **Not** available through the protocol: absolute position, a command watchdog,
  persistent parameter storage, or motor profiling (use ST Motor Profiler).
* MCSDK **6.x** uses the newer ASPEP/MCP framing and is not supported yet (see
  ADDING_A_CONTROLLER.md).

The ST protocol driver is unit-tested against a protocol-level fake of the MCSDK
firmware. Validate it on your Nucleo / IHM08M1 bench before production. Start with the
motor unloaded and low speed limits, and confirm that the speed, voltage and fault
read-outs match MC Workbench / Motor Pilot.
