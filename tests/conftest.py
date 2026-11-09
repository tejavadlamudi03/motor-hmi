import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTEUS_HMI_HOME", str(tmp_path))
    from moteus_hmi.paths import AppPaths
    return AppPaths()


@pytest.fixture
def keystore(home):
    from moteus_hmi.security.keystore import KeyStore
    return KeyStore(home.key_file)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
