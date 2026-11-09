# Adding a controller to the drop-down

1. **Profile:** add a `ControllerProfile` in `moteus_hmi/core/controllers.py`. It
   defines the vendor/model, the allowed links, the supported `ControlMode`s, feature
   flags, the voltage/current rating (used to cap safety limits), the default loop rate,
   and driver parameters.
2. **Driver:** if the controller speaks a new protocol, subclass
   `core/backend.MotorBackend` and implement these methods:
   * `open / discover / attach / close`
   * `cycle(commands, timeout)`: send one (already clamped) command per motor and return
     `Telemetry` (None = no reply, which counts toward comm loss)
   * `stop_all`, `diagnostic` (a `conf get/set/enumerate` style text API, so the
     Parameters and Tuning tabs work unchanged), `read_channel`, `set_output`
   * optionally `last_frames()` for the bus monitor
   * Fill `Telemetry.state_text` / `fault_text` for vendor-specific states.
3. Register it in `make_backend()`.
4. **Tests:** write a protocol-level fake (see `FakeMcsdkPort`) and add tests to
   `tests/test_protocols.py`. The control loop, safety supervisor, filters, UI, security
   then work automatically. To simulate the new
   controller, add its protocol to the Motor Simulator (`moteus_hmi/sim/`).

Good next candidates:

| Controller family | Protocol | Notes |
|---|---|---|
| Damiao DM-J series, CubeMars AK (MIT mode), Mini-Cheetah style actuators | Classic CAN, MIT frame (p 16 bit, v/kp/kd/τ 12 bit), enable `FF…FC`, disable `FF…FD`, zero `FF…FE` | Maps directly onto the *Impedance* mode; python-can transport |
| Industrial servo drives | CANopen CiA 402 (SDO/PDO, controlword/statusword) | `canopen` library; profile position/velocity/torque |
| STM32 MCSDK 6.x | ASPEP + MCP over UART | new framing; register IDs differ from 5.x |
| ODrive | CAN (CANSimple) or USB | |
