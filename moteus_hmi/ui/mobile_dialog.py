"""Mobile view dialog: switch the phone view on/off, show the link + QR code."""

from __future__ import annotations

import io
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QSpinBox, QVBoxLayout)

from ..remote.mobile_server import MobileServer
from ..security.auth import Perm
from .widgets import button, muted

log = logging.getLogger(__name__)

HELP = (
    "<b>On the phone</b><ol style='margin-left:-20px'>"
    "<li>Join the <b>same Wi-Fi</b> as this laptop.</li>"
    "<li>Scan the QR code, or type the link into the browser.</li>"
    "<li>The browser warns that the certificate is not trusted, because this HMI made its own. "
    "Check that the fingerprint below matches, then continue (Chrome: <i>Advanced → Proceed</i>; "
    "Safari: <i>Show Details → visit this website</i>).</li>"
    "<li>Sign in with your Motor HMI account.</li></ol>"
    "The phone shows live status, plots and alarms, and has an <b>E-STOP</b>. Enable, motion and "
    "reset stay at this laptop. Windows may ask to allow the HMI through the firewall: allow "
    "<i>Private networks</i> only."
)


def qr_pixmap(text: str, scale: int = 6) -> QPixmap:
    pm = QPixmap()
    try:
        import segno
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2)
        pm.loadFromData(buf.getvalue(), "PNG")
    except Exception as e:      # QR is a convenience; the link is shown as text anyway
        log.warning("QR code unavailable: %s", e)
    return pm


class MobileDialog(QDialog):
    def __init__(self, ctx, server: MobileServer, parent=None):
        super().__init__(parent)
        self.ctx, self.server = ctx, server
        self.setWindowTitle("Mobile view")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)

        self.status = QLabel()
        self.status.setTextFormat(Qt.RichText)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(ctx.settings.mobile_port))
        self.port.setToolTip("TCP port of the phone view (HTTPS)")
        self.toggle_btn = button("Turn on", "primary", self._toggle)
        row.addWidget(QLabel("Port"))
        row.addWidget(self.port)
        row.addStretch(1)
        row.addWidget(self.toggle_btn)
        lay.addLayout(row)
        self.admin_note = muted("Only an administrator can turn the phone view on or off.")
        lay.addWidget(self.admin_note)

        body = QHBoxLayout()
        self.qr = QLabel()
        self.qr.setFixedSize(220, 220)
        self.qr.setAlignment(Qt.AlignCenter)
        body.addWidget(self.qr)
        form = QFormLayout()
        self.links = QLabel()
        self.links.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.links.setWordWrap(True)
        self.fp = QLabel()
        self.fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.fp.setWordWrap(True)
        self.fp.setStyleSheet("font-family: monospace;")
        self.viewers = QLabel()
        form.addRow("Link", self.links)
        form.addRow("Certificate\nSHA-256", self.fp)
        form.addRow("Phones viewing", self.viewers)
        body.addLayout(form, 1)
        lay.addLayout(body)

        help_lbl = QLabel(HELP)
        help_lbl.setWordWrap(True)
        help_lbl.setTextFormat(Qt.RichText)
        lay.addWidget(help_lbl)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        server.viewers_changed.connect(lambda _n: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        s, admin = self.server, self.ctx.can(Perm.MANAGE_USERS)
        self.toggle_btn.setEnabled(admin)
        self.port.setEnabled(admin and not s.running)
        self.admin_note.setVisible(not admin)
        if s.running:
            self.status.setText("<b>Phone view is ON</b>, for phones on the same Wi-Fi as this laptop.")
            self.toggle_btn.setText("Turn off")
            self.toggle_btn.setProperty("kind", "danger")
            urls = s.urls
            self.links.setText("\n".join(urls) if urls else
                               f"No Wi-Fi / LAN address found. Port {s.port} is open on this PC only.")
            self.qr.setPixmap(qr_pixmap(urls[0]) if urls else QPixmap())
            self.fp.setText(s.fingerprint)
        else:
            self.status.setText("<b>Phone view is OFF.</b> Turn it on to watch the motors from a phone "
                                "on the same Wi-Fi.")
            self.toggle_btn.setText("Turn on")
            self.toggle_btn.setProperty("kind", "primary")
            self.links.setText("-")
            self.qr.setPixmap(QPixmap())
            self.qr.setText("QR code appears\nwhen turned on")
            self.fp.setText(s.fingerprint or "-")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)
        self.viewers.setText(str(s.viewers))

    def _toggle(self) -> None:
        if not self.ctx.require(Perm.MANAGE_USERS, self):
            return
        st = self.ctx.settings
        if self.server.running:
            self.server.stop()
            st.mobile_enabled = False
            self.ctx.audit("mobile.disabled")
        else:
            try:
                self.server.start(self.port.value())
            except OSError as e:
                self.status.setText(f"<b>Could not start:</b> {e}. Is port {self.port.value()} in use? "
                                    "Choose another port.")
                self.ctx.audit("mobile.enabled", {"port": self.port.value(), "error": str(e)}, "failed")
                return
            st.mobile_enabled, st.mobile_port = True, self.port.value()
            self.ctx.audit("mobile.enabled", {"port": self.server.port, "urls": self.server.urls})
        self.ctx.settings_store.save(st)
        self.refresh()
        if hasattr(self.parent(), "update_mobile_status"):
            self.parent().update_mobile_status()
