"""Motor Simulator entry point.

    python -m moteus_hmi.sim                       # GUI, IDs 1,2 on the virtual CAN-FD bus
    python -m moteus_hmi.sim --ids 1,2,3 --headless
    python -m moteus_hmi.sim --interface socketcan --channel vcan0
    MotorHMI --simulator …                         # packaged build
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .canfd_sim import DEFAULT_CHANNEL, DEFAULT_INTERFACE, DEFAULT_PORT, CanFdSimulator


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="motor-simulator", description="Virtual moteus controllers on CAN-FD")
    ap.add_argument("--ids", default="1,2", help="comma separated CAN IDs (default 1,2)")
    ap.add_argument("--interface", default=DEFAULT_INTERFACE, help="python-can interface (default udp_multicast)")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL, help=f"python-can channel (default {DEFAULT_CHANNEL})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"UDP port of the virtual bus (default {DEFAULT_PORT}); one bus per port")
    ap.add_argument("--prefix", type=int, default=0, help="CAN prefix (can.prefix)")
    ap.add_argument("--headless", action="store_true", help="no window, print statistics")
    ap.add_argument("--allow-hardware-bus", action="store_true",
                    help="permit a physical CAN interface (hardware-in-the-loop benches only)")
    a = ap.parse_args(argv)
    ids = sorted({int(x) for x in a.ids.split(",") if x.strip().isdigit() and 1 <= int(x) <= 126}) or [1]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from .sim_window import is_virtual
    if not is_virtual(a.interface, a.channel) and not a.allow_hardware_bus:
        print(f"Refusing physical bus {a.interface}:{a.channel} without --allow-hardware-bus", file=sys.stderr)
        return 2

    if a.headless:
        extra = {"port": a.port} if a.interface == "udp_multicast" else {}
        sim = CanFdSimulator(ids=ids, interface=a.interface, channel=a.channel, can_prefix=a.prefix, **extra)
        sim.start()
        stop = []
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        print(f"Motor Simulator: ids {ids} on {a.interface}:{a.channel} port {a.port}  (Ctrl-C to stop)", flush=True)
        try:
            while not stop:
                time.sleep(0.5)
        finally:
            sim.stop()
            print("stats:", sim.stats)
        return 0

    from PySide6.QtWidgets import QApplication
    from ..ui import theme
    from .sim_window import SimWindow
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Motor Simulator")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)
    w = SimWindow(ids=ids, interface=a.interface, channel=a.channel, prefix=a.prefix,
                  allow_hardware=a.allow_hardware_bus, port=a.port)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
