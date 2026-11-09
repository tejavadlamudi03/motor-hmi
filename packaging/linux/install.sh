#!/usr/bin/env bash
# Installs Motor HMI system-wide to /opt/motor-hmi (root-owned, read-only for
# operators, so the binaries cannot be modified by the user account).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" -ne 0 ]; then echo "run with sudo"; exit 1; fi

( cd "$HERE/app" && sha256sum -c --quiet ../SHA256SUMS ) || { echo "checksum mismatch - aborting"; exit 2; }

# Qt >= 6.5 needs libxcb-cursor0 for X11 windows (not bundled in the wheels).
if ! ldconfig -p | grep libxcb-cursor.so.0 >/dev/null; then   # no -q: avoids SIGPIPE under pipefail
  if command -v apt-get >/dev/null; then apt-get install -y libxcb-cursor0
  elif command -v dnf >/dev/null; then dnf install -y xcb-util-cursor
  else echo "Install libxcb-cursor (xcb-util-cursor) with your package manager"; fi
fi

rm -rf /opt/motor-hmi
cp -r "$HERE/app" /opt/motor-hmi
chown -R root:root /opt/motor-hmi
chmod -R go-w /opt/motor-hmi
ln -sf /opt/motor-hmi/MotorHMI /usr/local/bin/motor-hmi
install -m 0644 "$HERE/motor-hmi.desktop" /usr/share/applications/motor-hmi.desktop

# USB-CAN access without root: udev rule + dialout group.
install -m 0644 "$HERE/99-mjbots-fdcanusb.rules" /etc/udev/rules.d/99-mjbots-fdcanusb.rules
udevadm control --reload-rules && udevadm trigger || true
if [ -n "${SUDO_USER:-}" ]; then
  usermod -aG dialout "$SUDO_USER" && echo "Added $SUDO_USER to 'dialout' (log out/in once)."
fi
echo "Installed. Start with: motor-hmi"
