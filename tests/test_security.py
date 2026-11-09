import json
import os
import time

import pytest

from moteus_hmi.core.config_io import SignatureStatus, diff_configs, read_backup, validate_kv, write_backup
from moteus_hmi.core.tools import issue_token, verify_token
from moteus_hmi.security.audit import AuditLog
from moteus_hmi.security.auth import (AuthError, IntegrityError, Perm, Role, UserStore, has_perm,
                                      password_problems)
from moteus_hmi.settings import AppSettings, SettingsStore

PW = "Str0ng-Passw0rd"


def store(home, ks):
    return UserStore(home.users_file, home.recovery_file, ks)


def test_first_run_and_login(home, keystore):
    us = store(home, keystore)
    us.load()
    assert us.needs_setup
    code = us.setup_admin("chief", PW)
    assert len(code.replace("-", "")) == 20
    us2 = store(home, keystore)
    us2.load()
    assert us2.authenticate("chief", PW).role == Role.ADMIN
    with pytest.raises(AuthError):
        us2.authenticate("chief", "wrong")
    with pytest.raises(AuthError):
        us2.authenticate("nobody", PW)


def test_password_policy():
    assert password_problems("short")
    assert password_problems("alllowercase123")
    assert password_problems("Operator1234", "operator")
    assert not password_problems("Good-Passw0rd")


def test_lockout_after_failures(home, keystore):
    us = store(home, keystore)
    us.setup_admin("chief", PW)
    for _ in range(5):
        with pytest.raises(AuthError):
            us.authenticate("chief", "bad")
    with pytest.raises(AuthError, match="locked"):
        us.authenticate("chief", PW)
    us.unlock("chief")
    assert us.authenticate("chief", PW)


def test_user_db_tamper_detected_and_recovery(home, keystore):
    us = store(home, keystore)
    code = us.setup_admin("chief", PW)
    us.add_user("op1", "Operat0r-Pass", Role.OPERATOR)
    data = json.loads(home.users_file.read_text())
    for u in data["payload"]["users"]:
        if u["username"] == "op1":
            u["role"] = int(Role.ADMIN)          # privilege escalation attempt
    home.users_file.write_text(json.dumps(data))
    with pytest.raises(IntegrityError):
        store(home, keystore).load()
    home.users_file.unlink()                     # deleting the DB is not a reset either
    with pytest.raises(IntegrityError):
        store(home, keystore).load()
    us3 = store(home, keystore)
    with pytest.raises(AuthError):
        us3.recover("WRONG-CODE", "chief2", PW)
    us3.recover(code, "chief2", PW)
    assert us3.authenticate("chief2", PW)


def test_last_admin_protected(home, keystore):
    us = store(home, keystore)
    us.setup_admin("chief", PW)
    with pytest.raises(AuthError):
        us.set_role("chief", Role.OPERATOR)
    with pytest.raises(AuthError):
        us.delete_user("chief")


def test_role_permissions():
    assert has_perm(Role.VIEWER, Perm.ESTOP) and not has_perm(Role.VIEWER, Perm.OPERATE)
    assert has_perm(Role.OPERATOR, Perm.OPERATE) and not has_perm(Role.OPERATOR, Perm.CALIBRATE)
    assert has_perm(Role.ENGINEER, Perm.CALIBRATE) and not has_perm(Role.ENGINEER, Perm.MANAGE_USERS)
    assert all(has_perm(Role.ADMIN, p) for p in Perm)


def test_audit_chain_detects_edit_and_truncation(home, keystore):
    log = AuditLog(home.audit_file, home.audit_anchor, keystore)
    for i in range(5):
        log.record("u", "act", {"i": i})
    assert log.verify().ok
    lines = home.audit_file.read_text().splitlines()
    e = json.loads(lines[2]); e["details"] = {"i": 99}
    home.audit_file.write_text("\n".join(lines[:2] + [json.dumps(e)] + lines[3:]) + "\n")
    r = log.verify()
    assert not r.ok and r.first_bad_seq == 3
    home.audit_file.write_text("\n".join(lines[:3]) + "\n")   # truncated tail
    r = AuditLog(home.audit_file, home.audit_anchor, keystore).verify()
    assert not r.ok and "truncated" in r.message


def test_settings_signature(home, keystore):
    st = SettingsStore(home.settings_file, keystore)
    s = AppSettings()
    s.default_limits["max_torque"] = 1.5
    st.save(s)
    assert st.load().default_limits["max_torque"] == 1.5 and not st.tampered
    data = json.loads(home.settings_file.read_text())
    data["payload"]["default_limits"]["max_torque"] = 25.0
    home.settings_file.write_text(json.dumps(data))
    loaded = st.load()
    assert st.tampered and loaded.default_limits["max_torque"] != 25.0


def test_config_backup_signature(home, keystore, tmp_path):
    cfg = {"servo.pid_position.kp": "4.0", "id.id": "1"}
    p = tmp_path / "b.cfg"
    write_backup(p, cfg, {"device": "id 1"}, keystore)
    b = read_backup(p, keystore)
    assert b.status == SignatureStatus.VALID and dict(b.config) == cfg
    p.write_text(p.read_text().replace("4.0", "40.0"))
    assert read_backup(p, keystore).status == SignatureStatus.INVALID
    q = tmp_path / "plain.cfg"
    q.write_text("servo.pid_position.kp 3.0\n")
    assert read_backup(q, keystore).status == SignatureStatus.UNSIGNED
    assert diff_configs(cfg, {"servo.pid_position.kp": "4.000000", "id.id": "2"}) == [("id.id", "1", "2")]


def test_value_validation_blocks_command_injection():
    assert validate_kv("servo.pid_position.kp", "4.5") is None
    assert validate_kv("servo.pid_position.kp", "1\nd pos 5 0 1")
    assert validate_kv("servo.pid_position.kp", "1 d")
    assert validate_kv("bad key", "1")


def test_tool_passthrough_token(keystore):
    env = issue_token(keystore)
    assert verify_token(keystore, env)
    assert not verify_token(keystore, {})
    stale = dict(env)
    nonce = f"{int(time.time()) - 3600}:{env['MOTEUS_HMI_TOOL_NONCE'].split(':')[1]}"
    stale["MOTEUS_HMI_TOOL_NONCE"] = nonce
    assert not verify_token(keystore, stale)
