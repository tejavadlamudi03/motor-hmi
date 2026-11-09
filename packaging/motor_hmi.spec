# PyInstaller spec - builds a one-folder application for the current OS.
#   Linux:   packaging/build_linux.sh
#   Windows: packaging\build_windows.ps1
# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

block_cipher = None
hidden = (collect_submodules("moteus") + collect_submodules("can.interfaces")
          + ["serial.tools.list_ports", "pyqtgraph.graphicsItems", "importlib_metadata"])
if sys.platform == "win32":
    hidden += ["win32crypt", "win32api", "win32file", "pywintypes"]

datas = copy_metadata("moteus") + copy_metadata("python-can")

a = Analysis(
    ["../moteus_hmi/__main__.py"],
    pathex=[".."],
    hiddenimports=hidden,
    datas=datas,
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml", "tkinter",
              "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MotorHMI",
    console=False,             # GUI app; the --moteus-tool passthrough logs to the HMI
    disable_windowed_traceback=True,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="MotorHMI")
