"""Mobile view: HTTPS server, sign-in, live state, E-stop, and an end-to-end run
where a phone E-stop stops a simulated motor."""

import http.client
import json
import random
import ssl
import time

import pytest

from moteus_hmi.core.bus_worker import MotorStatus, TelemetryBatch
from moteus_hmi.core.safety import Alarm, Severity
from moteus_hmi.core.telemetry import Sample
from moteus_hmi.core.types import ControlMode, FirmwareMode, MotorCommand, Telemetry, TransportConfig
from moteus_hmi.remote.mobile_server import MobileServer, client_allowed
from moteus_hmi.security.audit import AuditLog
from moteus_hmi.security.auth import Role, UserStore

PW = "Str0ng-Admin-Pass"
OP_PW = "Operat0r-Pass"


def make_server(home, keystore, estop_fn):
    us = UserStore(home.users_file, home.recovery_file, keystore)
    us.setup_admin("chief", PW)
    us.add_user("op1", OP_PW, Role.OPERATOR, must_change=False)
    us.add_user("newbie", "Fresh-Viewer-9", Role.VIEWER)            # must change password first
    audit = AuditLog(home.audit_file, home.audit_anchor, keystore)
    srv = MobileServer(us, audit, keystore, home.security, estop_fn=estop_fn, idle_timeout_s=600)
    srv.start(port=0, host="127.0.0.1")
    return srv, us, audit


@pytest.fixture
def stack(home, keystore):
    stops = []
    srv, us, audit = make_server(home, keystore, stops.append)
    yield srv, us, audit, stops
    srv.stop()


class Phone:
    """Minimal HTTPS client that verifies the server against its own certificate."""

    def __init__(self, srv):
        self.ctx = ssl.create_default_context(cafile=str(srv.cert_path))
        self.port, self.cookie, self.csrf = srv.port, None, None

    def conn(self):
        return http.client.HTTPSConnection("127.0.0.1", self.port, context=self.ctx, timeout=5)

    def req(self, method, path, body=None, headers=None):
        c = self.conn()
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = None if "Max-Age=0" in sc else sc.split(";", 1)[0]
        c.close()
        try:
            js = json.loads(raw)
        except ValueError:
            js = None
        return r.status, js, r

    def login(self, user, pw):
        st, js, r = self.req("POST", "/api/login", {"username": user, "password": pw})
        if st == 200:
            self.csrf = js["csrf"]
        return st, js, r


def test_only_same_network_clients_are_served():
    for ok in ("127.0.0.1", "192.168.1.20", "10.1.2.3", "172.16.0.5", "fe80::1%wlan0", "::ffff:192.168.0.9"):
        assert client_allowed(ok), ok
    for bad in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "0.0.0.0", "not-an-ip"):
        assert not client_allowed(bad), bad


def test_page_and_api_need_sign_in(stack):
    srv, _, _, stops = stack
    p = Phone(srv)
    st, _, r = p.req("GET", "/")
    assert st == 200 and "text/html" in r.getheader("Content-Type")
    assert "script-src 'self'" in r.getheader("Content-Security-Policy")
    assert r.getheader("X-Frame-Options") == "DENY"
    for path in ("/api/state", "/api/me", "/api/stream"):
        assert p.req("GET", path)[0] == 401, path
    assert p.req("POST", "/api/estop")[0] == 401 and stops == []
    assert p.req("GET", "/etc/passwd")[0] == 404


def test_login_session_cookie_and_audit(stack):
    srv, _, audit, _ = stack
    p = Phone(srv)
    assert p.login("chief", "wrong-password")[0] == 401
    st, js, r = p.login("chief", PW)
    assert st == 200 and js["user"] == "chief" and js["role"] == "Admin" and js["can_estop"]
    cookie = r.getheader("Set-Cookie")
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Strict"))
    assert p.req("GET", "/api/me")[1]["user"] == "chief"
    outcomes = [(e["action"], e["outcome"]) for e in audit.entries()]
    assert ("mobile.login", "failed") in outcomes and ("mobile.login", "ok") in outcomes
    assert p.req("POST", "/api/logout")[0] == 200 and p.cookie is None
    assert p.req("GET", "/api/me")[0] == 401


def test_user_must_set_password_at_the_laptop_first(stack):
    srv, *_ = stack
    st, js, _ = Phone(srv).login("newbie", "Fresh-Viewer-9")
    assert st == 401 and "HMI laptop" in js["error"]


def test_state_snapshot(stack):
    srv, *_ = stack
    raw = Telemetry(motor_id=1, position=1.25, velocity=float("nan"), torque=0.1, voltage=24.0,
                    temperature=31.5, state_text="Position")
    batch = TelemetryBatch(status={1: MotorStatus(
        motor_id=1, armed=True, tripped=False, trip_reason="", comm_ok=True,
        command=MotorCommand(mode=ControlMode.POSITION, position=1.0),
        sample=Sample(raw=raw, filtered={"position": 1.2}))}, loop_hz=99.6, estop=False)
    srv.state.set_connection(True, "virtual CAN-FD")
    srv.state.update_batch(batch)
    srv.state.add_alarm(Alarm(code="FW_FAULT", severity=Severity.TRIP, motor_id=1, message="Driver fault 33"))
    p = Phone(srv)
    p.login("op1", OP_PW)
    st, js, _ = p.req("GET", "/api/state")
    m = js["motors"][0]
    assert st == 200 and js["connected"] and not js["estop"]
    assert m["id"] == 1 and m["armed"] and m["position"] == 1.2      # filtered value preferred
    assert m["velocity"] is None and m["voltage"] == 24.0              # NaN becomes null
    assert m["command"] == "Position" and m["state"] == "Position"
    assert js["alarms"][0]["severity"] == "TRIP" and js["alarms"][0]["active"]


def test_live_stream_pushes_events(stack):
    srv, *_ = stack
    p = Phone(srv)
    p.login("op1", OP_PW)
    srv.state.set_connection(True, "virtual CAN-FD")
    c = p.conn()
    c.request("GET", "/api/stream", headers={"Cookie": p.cookie})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    assert srv.viewers == 1
    end = time.time() + 3
    event = None
    while time.time() < end:
        line = r.readline().decode()
        if line.startswith("data: "):
            event = json.loads(line[6:])
            break
    c.close()
    assert event is not None and event["connected"] and event["link"] == "virtual CAN-FD"


def test_estop_needs_csrf_token_and_is_audited(stack):
    srv, _, audit, stops = stack
    p = Phone(srv)
    p.login("op1", OP_PW)
    assert p.req("POST", "/api/estop")[0] == 403 and stops == []
    assert p.req("POST", "/api/estop", headers={"X-CSRF-Token": "guess"})[0] == 403 and stops == []
    assert p.req("POST", "/api/estop", headers={"X-CSRF-Token": p.csrf})[0] == 200
    assert stops == ["mobile (op1 @ 127.0.0.1)"]
    assert any(e["action"] == "mobile.estop" and e["user"] == "op1" and e["outcome"] == "ok"
               for e in audit.entries())


def test_disabling_a_user_ends_their_phone_session(stack):
    srv, us, _, _ = stack
    p = Phone(srv)
    p.login("op1", OP_PW)
    assert p.req("GET", "/api/me")[0] == 200
    us.set_disabled("op1", True)
    assert p.req("GET", "/api/me")[0] == 401


def test_login_attempts_are_rate_limited(stack):
    srv, *_ = stack
    p = Phone(srv)
    codes = [p.login("nobody", "x" * 12)[0] for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_certificate_is_reused_and_key_is_encrypted(home, keystore):
    srv, *_ = make_server(home, keystore, lambda r: None)
    fp = srv.fingerprint
    srv.stop()
    assert b"ENCRYPTED" in srv.key_path.read_bytes()
    srv2 = MobileServer(srv.users, srv.audit_log, keystore, home.security, estop_fn=lambda r: None)
    srv2.start(port=0, host="127.0.0.1")
    try:
        assert srv2.fingerprint == fp and len(fp) == 95
    finally:
        srv2.stop()


# ---- end to end: phone E-stop stops a simulated motor -------------------------------
def test_phone_estop_stops_simulated_motor(qapp, tmp_path, home, keystore):
    from moteus_hmi.core.bus_worker import BusWorker
    from moteus_hmi.sim.canfd_sim import CanFdSimulator

    chan = (f"239.74.{random.randint(1, 250)}.{random.randint(1, 250)}", random.randint(20000, 40000))
    sim = CanFdSimulator(ids=(1,), channel=chan[0], port=chan[1])
    sim.start()
    w = BusWorker(tmp_path)
    res = {}
    w.request_done.connect(lambda rid, ok, r: res.__setitem__(rid, (ok, r)))
    w.start()

    def spin(t):
        end = time.time() + t
        while time.time() < end:
            qapp.processEvents()
            time.sleep(0.005)

    def wait(rid, t=10):
        end = time.time() + t
        while rid not in res and time.time() < end:
            spin(0.01)
        return res[rid]

    srv, *_ = make_server(home, keystore, w.estop)
    w.telemetry.connect(srv.state.update_batch)
    w.connection_changed.connect(srv.state.set_connection)
    try:
        ok, r = wait(w.connect_bus("moteus_r4_11", TransportConfig(
            profile="moteus_r4_11", kind="virtual_canfd", virtual_channel=chan[0], virtual_port=chan[1]),
            100, 0.25))
        assert ok, r
        assert wait(w.arm(1, True))[0]
        wait(w.set_command(1, MotorCommand(mode=ControlMode.VELOCITY, velocity=0.5)))
        spin(1.0)
        assert sim.nodes[1].motor.vel == pytest.approx(0.5, abs=0.1)

        phone = Phone(srv)
        assert phone.login("op1", OP_PW)[0] == 200
        js = phone.req("GET", "/api/state")[1]
        assert js["connected"] and js["motors"][0]["armed"]
        assert js["motors"][0]["velocity"] == pytest.approx(0.5, abs=0.15)

        assert phone.req("POST", "/api/estop", headers={"X-CSRF-Token": phone.csrf})[0] == 200
        spin(0.5)
        assert sim.nodes[1].motor.mode == FirmwareMode.STOPPED            # the real plant stopped
        js = phone.req("GET", "/api/state")[1]
        assert js["estop"] and not js["motors"][0]["armed"]
    finally:
        srv.stop()
        w.shutdown()
        sim.stop()
