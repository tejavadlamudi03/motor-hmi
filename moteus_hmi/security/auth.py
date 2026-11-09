"""Users, roles, permissions, password hashing and lockout.

* Passwords are hashed with scrypt (N=2^15, r=8, p=1, 16 byte salt).
* No default accounts: the first start forces creation of an admin and
  prints a one-time recovery code.
* 5 failed logins lock an account for 5 minutes (doubling on repeat).
* Unknown user names cost the same hashing time as known ones (no user
  enumeration by timing).
* The user database is MAC'd with the install key; a modified or
  deleted database is detected and requires the recovery code.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..paths import restrict_permissions
from .keystore import KeyStore

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
MAX_FAILED = 5
LOCKOUT_S = 300
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


class Role(enum.IntEnum):
    VIEWER = 0
    OPERATOR = 1
    ENGINEER = 2
    ADMIN = 3

    @property
    def label(self) -> str:
        return self.name.title()


class Perm(str, enum.Enum):
    VIEW = "view"                      # see telemetry, alarms
    ESTOP = "estop"                    # everyone, always
    CONNECT = "connect"                # open / close the bus
    OPERATE = "operate"                # arm, command motion, jog
    RESET = "reset"                    # reset trips and E-stop
    RECORD = "record"                  # data recording
    TUNE = "tune"                      # PID gains, step tests
    CALIBRATE = "calibrate"            # moteus calibration, zero offset
    CONFIGURE = "configure"            # conf set / write, backup / restore
    EDIT_LIMITS = "edit_limits"        # safety limits, filters
    CONSOLE = "console"                # diagnostic console
    MANAGE_USERS = "manage_users"
    AUDIT = "audit"                    # view / verify / export audit trail


ROLE_PERMS: dict[Role, frozenset[Perm]] = {
    Role.VIEWER: frozenset({Perm.VIEW, Perm.ESTOP}),
    Role.OPERATOR: frozenset({Perm.VIEW, Perm.ESTOP, Perm.CONNECT, Perm.OPERATE,
                              Perm.RESET, Perm.RECORD}),
    Role.ENGINEER: frozenset({Perm.VIEW, Perm.ESTOP, Perm.CONNECT, Perm.OPERATE, Perm.RESET,
                              Perm.RECORD, Perm.TUNE, Perm.CALIBRATE, Perm.CONFIGURE,
                              Perm.EDIT_LIMITS, Perm.CONSOLE}),
    Role.ADMIN: frozenset(Perm),
}


def has_perm(role: Role, perm: Perm) -> bool:
    return perm in ROLE_PERMS[Role(role)]


class AuthError(Exception):
    pass


class IntegrityError(Exception):
    pass


def password_problems(pw: str, username: str = "") -> list[str]:
    p = []
    if len(pw) < 10:
        p.append("at least 10 characters")
    if not re.search(r"[a-z]", pw):
        p.append("a lower-case letter")
    if not re.search(r"[A-Z]", pw):
        p.append("an upper-case letter")
    if not re.search(r"\d", pw):
        p.append("a digit")
    if username and username.lower() in pw.lower():
        p.append("no part matching the user name")
    return p


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(pw: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                       maxmem=SCRYPT_MAXMEM, dklen=32)
    return _b64(h), _b64(salt)


def verify_password(pw: str, pw_hash: str, salt: str) -> bool:
    h, _ = hash_password(pw, base64.b64decode(salt))
    return hmac.compare_digest(h, pw_hash)


@dataclass
class User:
    username: str
    role: int
    pw_hash: str
    salt: str
    created: float = field(default_factory=time.time)
    last_login: float = 0.0
    failed: int = 0
    lockouts: int = 0
    locked_until: float = 0.0
    must_change: bool = False
    disabled: bool = False

    @property
    def role_enum(self) -> Role:
        return Role(self.role)


class UserStore:
    def __init__(self, path: Path, recovery_path: Path, keystore: KeyStore):
        self.path = Path(path)
        self.recovery_path = Path(recovery_path)
        self.ks = keystore
        self.users: dict[str, User] = {}
        self._lock = threading.Lock()
        _dummy = hash_password("dummy-password-for-timing")
        self._dummy_hash, self._dummy_salt = _dummy

    # ---- persistence --------------------------------------------------
    def _canonical(self, payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def load(self) -> None:
        """Raises IntegrityError if the database was tampered with or
        removed after initial setup."""
        if not self.path.exists():
            if self.recovery_path.exists():
                raise IntegrityError("User database is missing but this installation was "
                                     "already set up. Recovery code required.")
            self.users = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            payload, mac = data["payload"], data["mac"]
        except (ValueError, KeyError) as e:
            raise IntegrityError(f"User database unreadable: {e}") from e
        if not self.ks.verify(self._canonical(payload), mac, "users"):
            raise IntegrityError("User database signature mismatch (file modified outside the HMI).")
        self.users = {u["username"]: User(**u) for u in payload["users"]}

    def save(self) -> None:
        payload = {"version": 1, "users": [asdict(u) for u in self.users.values()]}
        data = {"payload": payload, "mac": self.ks.mac(self._canonical(payload), "users")}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        restrict_permissions(tmp)
        os.replace(tmp, self.path)

    @property
    def needs_setup(self) -> bool:
        return not any(u.role == Role.ADMIN and not u.disabled for u in self.users.values())

    # ---- recovery code ------------------------------------------------
    def _write_recovery(self, code: str) -> None:
        h, s = hash_password(code.replace("-", "").upper())
        payload = {"hash": h, "salt": s, "created": time.time()}
        data = {"payload": payload, "mac": self.ks.mac(self._canonical(payload), "recovery")}
        self.recovery_path.write_text(json.dumps(data), encoding="utf-8")
        restrict_permissions(self.recovery_path)

    def check_recovery_code(self, code: str) -> bool:
        if not self.recovery_path.exists():
            return False
        try:
            data = json.loads(self.recovery_path.read_text(encoding="utf-8"))
            payload = data["payload"]
            if not self.ks.verify(self._canonical(payload), data["mac"], "recovery"):
                return False
            return verify_password(code.replace("-", "").replace(" ", "").upper(),
                                   payload["hash"], payload["salt"])
        except (ValueError, KeyError):
            return False

    @staticmethod
    def _new_recovery_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(20))
        return "-".join(raw[i:i + 5] for i in range(0, 20, 5))

    def setup_admin(self, username: str, password: str) -> str:
        """First run (or recovery): create an admin.  Returns a new
        one-time recovery code that must be shown to the user."""
        with self._lock:
            self._validate_new(username, password)
            h, s = hash_password(password)
            self.users[username] = User(username=username, role=int(Role.ADMIN), pw_hash=h, salt=s)
            code = self._new_recovery_code()
            self._write_recovery(code)
            self.save()
            return code

    def recover(self, code: str, username: str, password: str) -> str:
        if not self.check_recovery_code(code):
            raise AuthError("Invalid recovery code")
        self.users = {}
        return self.setup_admin(username, password)

    # ---- user management ---------------------------------------------
    def _validate_new(self, username: str, password: str) -> None:
        if not USERNAME_RE.match(username or ""):
            raise AuthError("User name must be 3-32 characters: letters, digits, . _ -")
        probs = password_problems(password, username)
        if probs:
            raise AuthError("Password needs " + ", ".join(probs))

    def add_user(self, username: str, password: str, role: Role, must_change: bool = True) -> None:
        with self._lock:
            if username in self.users:
                raise AuthError("User already exists")
            self._validate_new(username, password)
            h, s = hash_password(password)
            self.users[username] = User(username=username, role=int(role), pw_hash=h, salt=s,
                                        must_change=must_change)
            self.save()

    def _active_admins(self) -> int:
        return sum(1 for u in self.users.values() if u.role == Role.ADMIN and not u.disabled)

    def set_role(self, username: str, role: Role) -> None:
        with self._lock:
            u = self.users[username]
            if u.role == Role.ADMIN and role != Role.ADMIN and self._active_admins() <= 1:
                raise AuthError("Cannot demote the last administrator")
            u.role = int(role)
            self.save()

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._lock:
            u = self.users[username]
            if disabled and u.role == Role.ADMIN and self._active_admins() <= 1:
                raise AuthError("Cannot disable the last administrator")
            u.disabled = disabled
            self.save()

    def delete_user(self, username: str) -> None:
        with self._lock:
            u = self.users[username]
            if u.role == Role.ADMIN and self._active_admins() <= 1:
                raise AuthError("Cannot delete the last administrator")
            del self.users[username]
            self.save()

    def set_password(self, username: str, password: str, must_change: bool = False) -> None:
        with self._lock:
            u = self.users[username]
            probs = password_problems(password, username)
            if probs:
                raise AuthError("Password needs " + ", ".join(probs))
            if verify_password(password, u.pw_hash, u.salt):
                raise AuthError("New password must differ from the current one")
            u.pw_hash, u.salt = hash_password(password)
            u.must_change = must_change
            u.failed = 0
            u.locked_until = 0.0
            self.save()

    def unlock(self, username: str) -> None:
        with self._lock:
            u = self.users[username]
            u.failed, u.locked_until = 0, 0.0
            self.save()

    # ---- authentication ----------------------------------------------
    def authenticate(self, username: str, password: str) -> User:
        with self._lock:
            u = self.users.get(username)
            if u is None:
                verify_password(password, self._dummy_hash, self._dummy_salt)
                raise AuthError("Invalid user name or password")
            now = time.time()
            if u.locked_until > now:
                raise AuthError(f"Account locked. Try again in {int(u.locked_until - now) + 1} s "
                                "or ask an administrator to unlock it.")
            ok = verify_password(password, u.pw_hash, u.salt)
            if not ok or u.disabled:
                if not ok:
                    u.failed += 1
                    if u.failed >= MAX_FAILED:
                        u.lockouts += 1
                        u.locked_until = now + LOCKOUT_S * (2 ** min(u.lockouts - 1, 5))
                        u.failed = 0
                    self.save()
                raise AuthError("Invalid user name or password" if not u.disabled or not ok
                                else "Account disabled")
            u.failed = 0
            u.lockouts = 0
            u.last_login = now
            self.save()
            return u


@dataclass
class Session:
    user: User
    started: float = field(default_factory=time.time)

    @property
    def username(self) -> str:
        return self.user.username

    @property
    def role(self) -> Role:
        return self.user.role_enum

    def can(self, perm: Perm) -> bool:
        return has_perm(self.role, perm)
