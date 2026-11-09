"""``python -m moteus_hmi`` / packaged executable entry point."""

import sys


def run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--simulator":
        # Motor Simulator (virtual controllers on a virtual CAN-FD bus).
        from moteus_hmi.sim.__main__ import main as sim_main
        return sim_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--moteus-tool":
        # Frozen builds re-launch themselves to run moteus_tool; only an
        # authenticated HMI session can issue the required token.
        from moteus_hmi.core.tools import run_passthrough
        return run_passthrough(sys.argv[2:])
    from moteus_hmi.app import main
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(run())
