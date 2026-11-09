"""Mobile view server: phones on the same Wi-Fi see live motor status and can
trigger the E-stop.

Design rules
------------
* **Monitoring plus E-stop only.**  The phone cannot enable motors, command
  motion or reset anything; those stay at the HMI laptop, in sight of the
  machine.  An E-stop from a phone goes straight to ``BusWorker.estop``, the
  same thread-safe call the E-STOP button uses.
* **Same network only.**  Requests from non-private addresses are dropped
  before TLS, and the server is off until an administrator enables it.
* **HTTPS only**, with a per-installation self-signed certificate
  (``certs.py``).  Sign-in uses the HMI's own user accounts, so lockout,
  disabled accounts and role changes apply immediately.
* Session cookie: random 256-bit token, ``HttpOnly; Secure; SameSite=Strict``.
  The E-stop additionally needs the session's CSRF token in a header.
* Every sign-in (good or bad), sign-out and E-stop is written to the audit
  trail with the phone's IP address.

Only the Python standard library is used for HTTP; live data is pushed with
Server-Sent Events, which every mobile browser supports.
"""

from __future__ import annotations

import collections
import http.cookies
import ipaddress
import json
import logging
import math
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..security.auth import AuthError, Perm, Session
from . import certs

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
COOKIE = "mhmi"
PUSH_HZ = 10.0
MAX_STREAMS = 8
MAX_BODY = 4096
LOGIN_ATTEMPTS_PER_MIN = 10
SIGNALS = ("position", "velocity", "torque", "voltage", "temperature", "motor_temperature",
           "power", "q_current", "position_error")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
                                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def client_allowed(addr: str) -> bool:
    """Same-network check: loopback, RFC 1918 / ULA and link-local only."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or (ip.is_private and not ip.is_unspecified)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 5) if math.isfinite(f) else None


# ---------------------------------------------------------------------------
class MobileState:
    """Latest HMI state as plain JSON data.  Written by the UI thread (from
    the bus worker's signals), read by the HTTP threads."""

    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0
        self.connected = False
        self.link = ""
        self._motors: list[dict] = []
        self._estop = False
        self._loop_hz = 0.0
        self._alarms: collections.deque = collections.deque(maxlen=50)
        self._t = 0.0

    def _bump(self) -> None:
        self.version += 1
        self._t = time.time()
        self._cond.notify_all()

    def set_connection(self, connected: bool, link: str) -> None:
        with self._cond:
            self.connected, self.link = bool(connected), str(link)
            if not connected:
                self._motors = []
            self._bump()

    def update_batch(self, batch) -> None:
        motors = []
        for mid, st in sorted(batch.status.items()):
            s = st.sample
            raw = s.raw if s else None
            filt = s.filtered if s else {}
            vals = {}
            for name in SIGNALS:
                v = filt.get(name) if name in filt else (raw.value(name) if raw else None)
                vals[name] = _num(v)
            motors.append({
                "id": mid, "comm_ok": bool(st.comm_ok), "armed": bool(st.armed),
                "tripped": bool(st.tripped), "trip_reason": st.trip_reason or "",
                "state": (raw.state_text if raw else "") or "", "fault": int(raw.fault) if raw else 0,
                "fault_text": (raw.fault_text if raw else "") or "",
                "command": st.command.mode.label if st.command else "", **vals})
        with self._cond:
            self._motors, self._estop, self._loop_hz = motors, bool(batch.estop), float(batch.loop_hz)
            self._bump()

    def add_alarm(self, alarm) -> None:
        sev = getattr(alarm.severity, "name", str(alarm.severity))
        with self._cond:
            self._alarms.append({"code": alarm.code, "severity": sev, "motor": alarm.motor_id,
                                 "message": alarm.message, "raised_at": alarm.raised_at,
                                 "cleared_at": alarm.cleared_at, "active": alarm.active})
            self._bump()

    def snapshot(self) -> tuple[int, dict]:
        with self._cond:
            return self.version, {
                "t": self._t, "connected": self.connected, "link": self.link, "estop": self._estop,
                "loop_hz": round(self._loop_hz, 1), "motors": list(self._motors),
                "alarms": list(reversed(self._alarms))[:20]}

    def wait_newer(self, version: int, timeout: float) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.version != version, timeout=timeout)
            return self.version

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()


@dataclass
class WebSession:
    token: str
    username: str
    csrf: str
    ip: str
    created: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, addr, app: "MobileServer", ssl_ctx: ssl.SSLContext):
        self.app, self.ssl_ctx = app, ssl_ctx
        super().__init__(addr, _Handler)

    def verify_request(self, request, client_address) -> bool:
        return client_allowed(client_address[0])

    def finish_request(self, request, client_address) -> None:
        # TLS handshake here, on the per-connection thread, so a slow client
        # can never block the accept loop.
        request.settimeout(15)
        try:
            tls = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            self.RequestHandlerClass(tls, client_address, self)
        except (ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            try:
                tls.close()
            except OSError:
                pass


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    server_version = "MotorHMI"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args) -> None:
        log.debug("mobile %s %s", self.client_address[0], fmt % args)

    # ---- helpers ---------------------------------------------------------
    @property
    def app(self) -> "MobileServer":
        return self.server.app

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              headers: Optional[dict] = None) -> None:
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any, headers: Optional[dict] = None) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":")).encode(), headers=headers)

    def _body(self) -> Optional[dict]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _session(self) -> Optional[WebSession]:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return self.app.session_for(morsel.value) if morsel else None

    # ---- routes ------------------------------------------------------------
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            body, ctype = self.app.static(path)
            self._send(HTTPStatus.OK, body, ctype)
        elif path == "/api/me":
            s = self._session()
            if not s:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            else:
                self._json(HTTPStatus.OK, self.app.describe(s))
        elif path == "/api/state":
            if not self._session():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            else:
                self._json(HTTPStatus.OK, self.app.state.snapshot()[1])
        elif path == "/api/stream":
            self._stream()
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            self._login()
        elif path == "/api/logout":
            s = self._session()
            if s:
                self.app.end_session(s, "logout")
            self._json(HTTPStatus.OK, {"ok": True},
                       {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"})
        elif path == "/api/estop":
            s = self._session()
            if not s:
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            elif not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), s.csrf):
                self.app.audit(s.username, "mobile.estop", {"ip": s.ip}, "denied_csrf")
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing or wrong CSRF token"})
            elif not self.app.can(s, Perm.ESTOP):
                self._json(HTTPStatus.FORBIDDEN, {"error": "not permitted"})
            else:
                self.app.trigger_estop(s)
                self._json(HTTPStatus.OK, {"ok": True})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _login(self) -> None:
        ip = self.client_address[0]
        if not self.app.login_allowed(ip):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many attempts. Wait a minute."})
            return
        data = self._body()
        user = data.get("username") if data else None
        pw = data.get("password") if data else None
        if not isinstance(user, str) or not isinstance(pw, str) or not (0 < len(user) <= 64) \
                or not (0 < len(pw) <= 256):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "user name and password required"})
            return
        try:
            s = self.app.login(user.strip(), pw, ip)
        except AuthError as e:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(e)})
            return
        self._json(HTTPStatus.OK, self.app.describe(s), {
            "Set-Cookie": (f"{COOKIE}={s.token}; Path=/; HttpOnly; Secure; SameSite=Strict; "
                           f"Max-Age={int(self.app.max_age_s)}")})

    def _stream(self) -> None:
        s = self._session()
        if not s:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "not signed in"})
            return
        if not self.app.stream_opened():
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "too many viewers"})
            return
        try:
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(10)
            version, last = -1, 0.0
            while not self.app.stopping:
                if self.app.session_for(s.token) is None:
                    self.wfile.write(b"event: auth\ndata: {}\n\n")
                    break
                if self.app.state.version != version:
                    version, snap = self.app.state.snapshot()
                    self.wfile.write(b"data: " + json.dumps(snap, separators=(",", ":")).encode() + b"\n\n")
                    last = time.monotonic()
                elif time.monotonic() - last > 5.0:
                    self.wfile.write(b": keep-alive\n\n")
                    last = time.monotonic()
                self.wfile.flush()
                time.sleep(1.0 / PUSH_HZ)
                self.app.state.wait_newer(version, timeout=1.0)
        except (ConnectionError, ssl.SSLError, socket.timeout, OSError):
            pass
        finally:
            self.app.stream_closed()


# ---------------------------------------------------------------------------
class MobileServer(QObject):
    """Owned by the main window (UI thread).  ``estop_fn`` must be thread-safe
    (``BusWorker.estop`` is)."""

    estop_requested = Signal(str)     # reason, for UI feedback (the stop itself is already done)
    viewers_changed = Signal(int)

    def __init__(self, users, audit_log, keystore, security_dir: Path, estop_fn: Callable[[str], None],
                 idle_timeout_s: float = 600.0, max_age_s: float = 12 * 3600.0):
        super().__init__()
        self.users, self.audit_log, self.keystore = users, audit_log, keystore
        self.cert_path = Path(security_dir) / "mobile_cert.pem"
        self.key_path = Path(security_dir) / "mobile_key.pem"
        self.estop_fn = estop_fn
        self.idle_timeout_s, self.max_age_s = float(idle_timeout_s), float(max_age_s)
        self.state = MobileState()
        self.stopping = False
        self.port = 0
        self.addresses: list[str] = []
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._sessions: dict[str, WebSession] = {}
        self._attempts: dict[str, collections.deque] = {}
        self._streams = 0
        self._static: dict[str, tuple[bytes, str]] = {}

    # ---- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def urls(self) -> list[str]:
        return [f"https://{a}:{self.port}/" for a in self.addresses]

    @property
    def fingerprint(self) -> str:
        return certs.fingerprint(self.cert_path) if self.cert_path.exists() else ""

    @property
    def viewers(self) -> int:
        return self._streams

    def start(self, port: int = 8443, host: str = "0.0.0.0") -> list[str]:
        if self.running:
            return self.urls
        self._static = {p: ((WEB_DIR / f).read_bytes(), t) for p, (f, t) in STATIC.items()}
        self.addresses = certs.lan_addresses() if host in ("0.0.0.0", "") else [host]
        cert, key, pw = certs.ensure_certificate(self.cert_path, self.key_path, self.keystore,
                                                 [a for a in self.addresses if a != "127.0.0.1"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key), password=pw)
        self.stopping = False
        self._httpd = _Server((host, int(port)), self, ctx)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="mobile-http", daemon=True)
        self._thread.start()
        self.audit("-", "mobile.server_start", {"port": self.port, "addresses": self.addresses})
        log.info("mobile view on %s", ", ".join(self.urls) or f"port {self.port}")
        return self.urls

    def stop(self) -> None:
        if not self._httpd:
            return
        self.stopping = True
        self.state.wake_all()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        with self._lock:
            self._sessions.clear()
        self.audit("-", "mobile.server_stop", None)

    # ---- used by the handler (HTTP threads) ---------------------------------
    def static(self, path: str) -> tuple[bytes, str]:
        return self._static[path]

    def audit(self, user: str, action: str, details: Any, outcome: str = "ok") -> None:
        try:
            self.audit_log.record(user, action, details, outcome, role="mobile")
        except OSError as e:
            log.error("audit write failed: %s", e)

    def login_allowed(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._attempts.setdefault(ip, collections.deque())
            while q and now - q[0] > 60.0:
                q.popleft()
            if len(q) >= LOGIN_ATTEMPTS_PER_MIN:
                return False
            q.append(now)
            return True

    def _user(self, username: str):
        u = self.users.users.get(username)
        return None if (u is None or u.disabled) else u

    def can(self, s: WebSession, perm: Perm) -> bool:
        u = self._user(s.username)
        return bool(u and Session(u).can(perm))

    def login(self, username: str, password: str, ip: str) -> WebSession:
        try:
            u = self.users.authenticate(username, password)
        except AuthError:
            self.audit(username, "mobile.login", {"ip": ip}, "failed")
            raise
        if u.must_change:
            self.audit(username, "mobile.login", {"ip": ip}, "password_change_required")
            raise AuthError("Set your own password at the HMI laptop first, then sign in here.")
        if not Session(u).can(Perm.VIEW):
            self.audit(username, "mobile.login", {"ip": ip}, "denied")
            raise AuthError("Your role cannot use the mobile view.")
        s = WebSession(token=secrets.token_urlsafe(32), username=u.username,
                       csrf=secrets.token_urlsafe(24), ip=ip)
        with self._lock:
            self._sessions[s.token] = s
        self.audit(u.username, "mobile.login", {"ip": ip}, "ok")
        return s

    def session_for(self, token: str) -> Optional[WebSession]:
        now = time.monotonic()
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if now - s.last_seen > self.idle_timeout_s or now - s.created > self.max_age_s \
                    or self._user(s.username) is None:
                del self._sessions[token]
                return None
            s.last_seen = now
            return s

    def end_session(self, s: WebSession, why: str) -> None:
        with self._lock:
            self._sessions.pop(s.token, None)
        self.audit(s.username, "mobile.logout", {"ip": s.ip, "why": why})

    def describe(self, s: WebSession) -> dict:
        u = self._user(s.username)
        role = Session(u).role.label if u else ""
        return {"user": s.username, "role": role, "csrf": s.csrf,
                "can_estop": self.can(s, Perm.ESTOP), "idle_timeout_s": self.idle_timeout_s}

    def trigger_estop(self, s: WebSession) -> None:
        reason = f"mobile ({s.username} @ {s.ip})"
        try:
            self.estop_fn(reason)           # thread-safe, acts immediately
        finally:
            self.audit(s.username, "mobile.estop", {"ip": s.ip})
            self.estop_requested.emit(reason)

    def stream_opened(self) -> bool:
        with self._lock:
            if self._streams >= MAX_STREAMS:
                return False
            self._streams += 1
            n = self._streams
        self.viewers_changed.emit(n)
        return True

    def stream_closed(self) -> None:
        with self._lock:
            self._streams = max(0, self._streams - 1)
            n = self._streams
        self.viewers_changed.emit(n)
