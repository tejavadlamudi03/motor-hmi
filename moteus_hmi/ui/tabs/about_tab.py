"""About: versions, paths, controller profile, safety notice."""

from __future__ import annotations

import platform
import sys

from PySide6 import __version__ as pyside_version
from PySide6.QtWidgets import QLabel, QTextBrowser, QVBoxLayout

from ... import APP_NAME, __version__
from ...core.controllers import PROFILES
from .base import HmiTab


class AboutTab(HmiTab):
    title = "About"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        self.text = QTextBrowser()
        self.text.setOpenExternalLinks(True)
        lay.addWidget(self.text)
        self.refresh()

    def on_connection(self, connected: bool) -> None:
        self.refresh()

    def refresh(self) -> None:
        try:
            import moteus
            mv = moteus.VERSION
        except Exception:
            mv = "not installed"
        p = self.ctx.paths
        prof = self.ctx.profile
        rows = "".join(f"<tr><td>{x.vendor}</td><td>{x.model}</td><td>{x.min_voltage:g}–{x.max_voltage:g} V</td>"
                       f"<td>{x.peak_current_A:g} A</td><td>{', '.join(m.label for m in x.modes)}</td></tr>"
                       for x in PROFILES.values())
        self.text.setHtml(f"""
<h2>{APP_NAME} {__version__}</h2>
<p>Secure operator and engineering HMI for brushless motor controllers.</p>
<table cellpadding=3>
<tr><td><b>moteus library</b></td><td>{mv}</td></tr>
<tr><td><b>Qt / PySide6</b></td><td>{pyside_version}</td></tr>
<tr><td><b>Python</b></td><td>{sys.version.split()[0]} ({platform.system()} {platform.release()}, {platform.machine()})</td></tr>
<tr><td><b>Data folder</b></td><td>{p.base}</td></tr>
<tr><td><b>Active controller</b></td><td>{prof.title}</td></tr>
</table>
<h3>Supported controllers</h3>
<table border=1 cellspacing=0 cellpadding=4><tr><th>Vendor</th><th>Model</th><th>Supply</th><th>Peak</th><th>Modes</th></tr>{rows}</table>
<h3>Safety notice</h3>
<p>The software emergency stop, limits and trips in this HMI are <b>not</b> a safety-rated function
(no SIL / PL rating). Every installation needs a hard-wired emergency stop that removes motor power
through a safety relay or STO circuit independent of this computer. See docs/SAFETY.md.</p>
<h3>Licences</h3>
<p>moteus python library © mjbots Robotic Systems, Apache-2.0. Qt / PySide6 under LGPLv3.
pyqtgraph MIT. python-can LGPLv3. pyserial BSD.</p>
""")
