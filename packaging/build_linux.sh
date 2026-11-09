#!/usr/bin/env bash
# Build the Linux release: dist/MotorHMI-<ver>-linux-x86_64.tar.gz
# Requires: Python 3.12 with venv (apt: python3.12-venv) or uv. Run from anywhere.
set -euo pipefail
unset PYTHONPATH   # keep ROS or other system site-packages out of the build
cd "$(dirname "$0")/.."
VER=$(python3 -c "import re;print(re.search(r'__version__ = \"(.+?)\"', open('moteus_hmi/__init__.py').read()).group(1))")

rm -rf .build-venv
if command -v uv >/dev/null; then
  uv venv --python 3.12 .build-venv
  . .build-venv/bin/activate
  uv pip install -r requirements.txt pyinstaller pytest
else
  python3 -m venv .build-venv || {
    echo "python venv is missing: sudo apt install python3.12-venv   (or install uv)"; exit 1; }
  . .build-venv/bin/activate
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt pyinstaller pytest
fi

# Never ship an untested build.
env -u PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen python -m pytest -q tests

cd packaging
pyinstaller --noconfirm --clean motor_hmi.spec
cd ..
OUT="dist/MotorHMI-${VER}-linux-$(uname -m)"
rm -rf "$OUT" && mkdir -p "$OUT"
cp -r packaging/dist/MotorHMI "$OUT/app"
cp packaging/linux/install.sh packaging/linux/99-mjbots-fdcanusb.rules packaging/linux/motor-hmi.desktop "$OUT/"
cp README.md docs/*.md docs/*.docx "$OUT/" 2>/dev/null || true
cp -r docs/images "$OUT/images" 2>/dev/null || true
( cd "$OUT/app" && find . -type f -exec sha256sum {} + | sort -k2 > ../SHA256SUMS )
tar -C dist -czf "$OUT.tar.gz" "$(basename "$OUT")"
( cd dist && sha256sum "$(basename "$OUT").tar.gz" > "$(basename "$OUT").tar.gz.sha256" )
echo "Built $OUT.tar.gz"
