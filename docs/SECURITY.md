# Security design

## Roles

| Role | Can |
|---|---|
| Viewer | View telemetry and alarms, E-stop |
| Operator | + connect, enable/disable, command motion, jog, reset trips and E-stop, record data |
| Engineer | + calibration, tuning, parameters (write / flash / import / export), safety limits, filters, console, firmware upgrade (approved images only) |
| Admin | + user management, firmware approval, audit verification and export, session timeout |

E-stop is never permission-gated. It also works from the lock screen.

## Authentication

* No default accounts. The first start forces creation of an administrator and shows a
  one-time **recovery code**.
* Passwords: at least 10 characters with upper case, lower case and a digit, and no part
  matching the user name. Hashed with scrypt (N=2^15, r=8, p=1, 16-byte random salt).
* Lockout: 5 failures lock the account for 5 min, doubling on each repeat. Unknown user
  names cost the same hashing time, which prevents user enumeration.
* New or reset accounts must change their password at first login.
* The session auto-locks after inactivity (default 10 min). Another user may take over;
  the switch is audited.

## Integrity

* A 32-byte per-installation key (`security/install.key`) is stored with mode 0600 on
  Linux. On Windows it is wrapped with DPAPI for the current user. Separate
  domain-specific subkeys MAC the following:
  * the user database (role edits outside the HMI → refused; deletion → recovery code required)
  * settings, including the safety limits (tampered → safe defaults and a warning)
  * the audit trail (HMAC hash chain plus a separately MAC'd anchor, which detects edits,
    re-ordering, deletion and tail truncation)
  * exported parameter files (an import of a modified file needs a typed override)
  * the one-minute token that allows the packaged EXE to run `moteus_tool`, so the
    executable cannot be used to bypass login
* Data recordings get a SHA-256 sidecar. Firmware images must have an admin-approved
  SHA-256.
* Diagnostic commands with CR, LF or control characters are rejected. Parameter names and
  values must be plain tokens. This prevents command injection over the line-based
  diagnostic channel. The console is allow-listed and refuses motion commands.

## Network

The HMI opens **no network ports** and makes no outbound connections. All traffic is
local USB/CAN.

## Threat model and limits

Protects against operators exceeding their role, casual or accidental file tampering,
replay of files between installations, and silent loss of audit history.

Does **not** protect against an attacker who controls the same OS account or has
admin/root on the laptop. Such an attacker can read the install key. Use OS-level
controls as the trust anchor:

* a separate, non-admin OS account per operator station
* full-disk encryption (BitLocker / LUKS)
* the read-only install directory (Program Files / /opt, as done by the installers)
* code-signed builds and verification of the published SHA-256 checksums
* forwarding the audit trail to central storage (SIEM / syslog) if your QA system
  requires it; the export function gives JSONL plus a verification report
