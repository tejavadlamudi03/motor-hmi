"""Application bootstrap."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import traceback

from . import APP_NAME, __version__
from .paths import AppPaths


def _setup_logging(paths: AppPaths, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.app_log, maxBytes=5_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="motor-hmi", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--home", help="data folder (portable mode)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{APP_NAME} {__version__}")
        return 0

    import os
    if args.home:
        os.environ["MOTEUS_HMI_HOME"] = args.home

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    from .core.bus_worker import BusWorker
    from .security.audit import AuditLog
    from .security.auth import IntegrityError, UserStore
    from .security.keystore import KeyStore
    from .settings import SettingsStore
    from .ui import theme
    from .ui.context import AppContext
    from .ui.login import LoginDialog, SetupDialog
    from .ui.main_window import MainWindow

    paths = AppPaths()
    _setup_logging(paths, args.verbose)
    log = logging.getLogger("app")
    log.info("starting %s %s, data=%s", APP_NAME, __version__, paths.base)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.QSS)

    # One HMI per data folder: two instances would interleave the audit chain
    # and fight over the CAN adapter.
    from PySide6.QtCore import QLockFile
    lock = QLockFile(str(paths.base / "hmi.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.critical(None, APP_NAME, "Motor HMI is already running for this user / data folder.")
        return 4
    app._instance_lock = lock      # keep the lock for the process lifetime

    ks = KeyStore(paths.key_file)
    audit = AuditLog(paths.audit_file, paths.audit_anchor, ks)
    users = UserStore(paths.users_file, paths.recovery_file, ks)
    audit.record("-", "app.start", {"version": __version__})

    v = audit.verify()
    if not v.ok:
        QMessageBox.critical(None, "Audit trail integrity", f"The audit trail failed verification:\n\n"
                             f"{v.message}\n\nThe HMI will continue, and this event is recorded. "
                             "Inform your administrator.")
        audit.record("-", "audit.integrity_failure", {"message": v.message}, "failed")

    try:
        users.load()
    except IntegrityError as e:
        audit.record("-", "auth.integrity_failure", {"message": str(e)}, "failed")
        QMessageBox.critical(None, "User database", str(e))
        dlg = SetupDialog(users, recovery=True)
        if dlg.exec() != QDialog.Accepted:
            return 3
        audit.record(dlg.name.text(), "auth.recovered", None, "ok")
    if users.needs_setup:
        dlg = SetupDialog(users)
        if dlg.exec() != QDialog.Accepted:
            return 0
        audit.record(dlg.name.text(), "auth.setup_admin", None, "ok")

    store = SettingsStore(paths.settings_file, ks)
    settings = store.load()
    if store.tampered:
        audit.record("-", "settings.integrity_failure", None, "failed")
        QMessageBox.warning(None, "Settings", "The settings file was modified outside the HMI. Safe "
                            "defaults were loaded; review the safety limits.")

    login = LoginDialog(users, audit)
    if login.exec() != QDialog.Accepted or not login.session:
        return 0

    worker = BusWorker(paths.recordings)
    worker.start()
    ctx = AppContext(paths, ks, users, audit, store, settings, worker)
    ctx.session = login.session

    win = MainWindow(ctx)

    def excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        log.error("unhandled exception:\n%s", text)
        try:
            ctx.audit("app.exception", {"error": str(value)[:500]}, "error")
            # Fail safe: an unexpected UI error stops every motor.
            worker.estop(f"software exception: {etype.__name__}")
        except Exception:
            pass
        QMessageBox.critical(win, "Unexpected error", f"{etype.__name__}: {value}\n\nAll motors were "
                             "stopped as a precaution. Details are in the log file.")

    sys.excepthook = excepthook
    win.show()
    code = app.exec()
    worker.shutdown()
    audit.record(ctx.username, "app.stop", None, "ok")
    return code
