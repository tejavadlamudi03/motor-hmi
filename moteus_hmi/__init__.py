"""Motor HMI - secure operator / engineering HMI for mjbots moteus controllers.

The package is split into three layers:

* ``moteus_hmi.core``      - hardware abstraction, control loop, safety
                             supervision, signal filtering, calibration.
                             No widget code lives here.
* ``moteus_hmi.security``  - users / roles, password hashing, tamper-evident
                             audit trail, file signing.
* ``moteus_hmi.ui``        - PySide6 widgets.
"""

__all__ = ["__version__", "APP_NAME", "APP_ID"]

__version__ = "1.0.0"
APP_NAME = "Motor HMI"
APP_ID = "moteus-hmi"
