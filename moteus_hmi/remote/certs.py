"""Self-signed TLS certificate for the mobile view.

Each installation creates its own EC P-256 key and certificate on first use.
The private key is stored encrypted with a key derived from the
installation key, so copying the file to another PC is useless.  The
certificate's SAN covers ``localhost``, the host name and every LAN address
the PC has when the server starts; it is re-issued when the PC gets an
address the certificate does not cover yet (for example on another Wi-Fi).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ..paths import restrict_permissions

VALID_DAYS = 825          # the longest lifetime Apple / Chrome accept for TLS server certificates


def lan_addresses() -> list[str]:
    """IPv4 addresses of this PC that a phone on the same network can reach.
    The primary address (the one with the default route) comes first."""
    found: list[str] = []
    try:   # no packet is sent: connect() on UDP only selects the outgoing interface
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        ip = ipaddress.ip_address(a)
        if (ip.is_private or ip.is_link_local) and not ip.is_loopback and a not in out:
            out.append(a)
    return out


def _password(keystore) -> bytes:
    return keystore.subkey("mobile-tls-key").hex().encode()


def _covers(cert: x509.Certificate, ips: list[str]) -> bool:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    have = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    return all(i in have for i in ips)


def fingerprint(cert_path: Path) -> str:
    der = x509.load_pem_x509_certificate(Path(cert_path).read_bytes()).public_bytes(serialization.Encoding.DER)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def ensure_certificate(cert_path: Path, key_path: Path, keystore, ips: list[str]) -> tuple[Path, Path, bytes]:
    """Return (cert file, key file, key password), creating or re-issuing the
    certificate when it is missing, about to expire or does not cover ``ips``."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    password = _password(keystore)
    now = _dt.datetime.now(_dt.timezone.utc)
    want_ips = sorted(set(ips) | {"127.0.0.1"})
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            serialization.load_pem_private_key(key_path.read_bytes(), password=password)
            if cert.not_valid_after_utc - now > _dt.timedelta(days=30) and _covers(cert, want_ips):
                return cert_path, key_path, password
        except (ValueError, TypeError):
            pass          # unreadable or from another installation: issue a new one

    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "motor-hmi"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Motor HMI on {host}"[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Motor HMI")])
    san = [x509.DNSName("localhost"), x509.DNSName(host)]
    san += [x509.IPAddress(ipaddress.ip_address(i)) for i in want_ips]
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.BestAvailableEncryption(password)))
    restrict_permissions(key_path)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, password
