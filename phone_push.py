"""Web Push for the phone page, done in the app: a local certificate authority and an HTTPS certificate for the phone
listener (the phone trusts the CA once), VAPID keys, RFC 8291 payload encryption and delivery to the browser's own
push service (Apple's for Safari, Google's for Chrome). That service is the only relay a phone notification can take
and it carries ciphertext only: the keys never leave this computer and the phone.

Files live in one directory (the workspace, never the repo): ca.key / ca.crt, server.key / server.crt, vapid.json,
subscriptions.json."""
from __future__ import annotations

import base64
import datetime as _dt
import ipaddress
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LEAF_DAYS = 820              # Apple trusts TLS server certificates of 825 days or fewer
CA_DAYS = 3650
RENEW_WITHIN_DAYS = 30
RECORD_SIZE = 4096
VAPID_HOURS = 12
UA_PUBLIC_LEN = 65           # an uncompressed P-256 point


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def local_hosts(ip: str) -> list[str]:
    """What the phone may call this computer: its Bonjour name (stable across DHCP leases) and the LAN address."""
    name = socket.gethostname().lower().rstrip(".")
    if not name.endswith(".local"):
        name += ".local"
    out = [name]
    for h in (ip, "127.0.0.1"):
        if h and h not in out:
            out.append(h)
    return out


class PushStore:
    def __init__(self, folder: Path):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ca_key, self.ca_crt = self.dir / "ca.key", self.dir / "ca.crt"
        self.srv_key, self.srv_crt = self.dir / "server.key", self.dir / "server.crt"
        self.vapid_file, self.subs_file = self.dir / "vapid.json", self.dir / "subscriptions.json"

    # ------------------------------------------------------------ certificates
    def _write_key(self, path: Path, key) -> None:
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _ca(self):
        if self.ca_key.exists() and self.ca_crt.exists():
            key = serialization.load_pem_private_key(self.ca_key.read_bytes(), None)
            return key, x509.load_pem_x509_certificate(self.ca_crt.read_bytes())
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"AgenticCAD Phone CA · {socket.gethostname()}"[:64]),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AgenticCAD (local)")])
        now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
        crt = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + _dt.timedelta(days=CA_DAYS))
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .sign(key, hashes.SHA256()))
        self._write_key(self.ca_key, key)
        self.ca_crt.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
        return key, crt

    def ca_der(self) -> bytes:
        return self._ca()[1].public_bytes(serialization.Encoding.DER)

    def leaf(self):
        return x509.load_pem_x509_certificate(self.srv_crt.read_bytes()) if self.srv_crt.exists() else None

    def leaf_hosts(self) -> list[str]:
        c = self.leaf()
        if c is None:
            return []
        try:
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return []
        return [str(v) for v in san.get_values_for_type(x509.DNSName)] + [str(v) for v in san.get_values_for_type(x509.IPAddress)]

    def ensure_certs(self, hosts: list[str]) -> dict:
        """The CA (made once; the phone trusts it) and a server certificate naming `hosts` (remade when a host is
        missing or it is near its end: the CA stays, so the phone needs nothing new)."""
        ca_key, ca_crt = self._ca()
        have = self.leaf()
        need = have is None or any(h not in self.leaf_hosts() for h in hosts)
        if have is not None and not need:
            left = have.not_valid_after_utc - _dt.datetime.now(_dt.timezone.utc)
            need = left < _dt.timedelta(days=RENEW_WITHIN_DAYS)
        if need:
            key = ec.generate_private_key(ec.SECP256R1())
            names = []
            for h in hosts:
                try:
                    names.append(x509.IPAddress(ipaddress.ip_address(h)))
                except ValueError:
                    names.append(x509.DNSName(h))
            now = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
            crt = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0][:64])]))
                   .issuer_name(ca_crt.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now).not_valid_after(now + _dt.timedelta(days=LEAF_DAYS))
                   .add_extension(x509.SubjectAlternativeName(names), critical=False)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                                key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .sign(ca_key, hashes.SHA256()))
            self._write_key(self.srv_key, key)
            self.srv_crt.write_bytes(crt.public_bytes(serialization.Encoding.PEM))
        return self.cert_info()

    def cert_info(self) -> dict:
        ca = self._ca()[1]
        leaf = self.leaf()
        fp = ca.fingerprint(hashes.SHA256()).hex().upper()
        return {"ca_fingerprint": ":".join(fp[i:i + 2] for i in range(0, 16, 2)) + "…", "ca_not_after": ca.not_valid_after_utc.isoformat(),
                "hosts": self.leaf_hosts(), "not_after": leaf.not_valid_after_utc.isoformat() if leaf else None}

    def new_ca(self) -> None:
        """Start over (the phones have to trust the new certificate): the CA, the server certificate and the
        subscriptions, which belong to the old origin's trust."""
        for p in (self.ca_key, self.ca_crt, self.srv_key, self.srv_crt):
            if p.exists():
                p.unlink()

    # ------------------------------------------------------------ VAPID
    def vapid(self) -> tuple[ec.EllipticCurvePrivateKey, str]:
        """(private key, public key as the browser wants it: base64url of the uncompressed point)."""
        if self.vapid_file.exists():
            d = json.loads(self.vapid_file.read_text())
            key = serialization.load_pem_private_key(d["private_pem"].encode(), None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            self.vapid_file.write_text(json.dumps({"private_pem": key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}))
            try:
                os.chmod(self.vapid_file, 0o600)
            except OSError:
                pass
        pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return key, b64u(pub)

    def vapid_authorization(self, endpoint: str, subject: str, now: float | None = None) -> str:
        """RFC 8292: `vapid t=<JWT signed with our key>, k=<our public key>` for the push service at `endpoint`."""
        from urllib.parse import urlsplit
        key, pub = self.vapid()
        u = urlsplit(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + VAPID_HOURS * 3600), "sub": subject}
        seg = lambda o: b64u(json.dumps(o, separators=(",", ":")).encode())
        signing = f"{seg({'typ': 'JWT', 'alg': 'ES256'})}.{seg(claims)}"
        r, s = decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        jwt = f"{signing}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={jwt}, k={pub}"

    # ------------------------------------------------------------ subscriptions
    def subscriptions(self) -> list[dict]:
        try:
            return json.loads(self.subs_file.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, subs: list[dict]) -> None:
        self.subs_file.write_text(json.dumps(subs, indent=1))

    def add(self, sub: dict, label: str = "") -> dict:
        """Remember a browser's PushSubscription (toJSON()): {endpoint, keys: {p256dh, auth}}."""
        if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://"):
            raise ValueError("a push subscription has an https endpoint")
        keys = sub.get("keys") or {}
        if len(unb64u(str(keys.get("p256dh", "")))) != UA_PUBLIC_LEN or len(unb64u(str(keys.get("auth", "")))) != 16:
            raise ValueError("a push subscription has p256dh (65 bytes) and auth (16 bytes) keys")
        subs = [s for s in self.subscriptions() if s["endpoint"] != sub["endpoint"]]
        rec = {"endpoint": sub["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "label": str(label)[:120],
               "added": time.time(), "last": None}
        subs.append(rec)
        self._save(subs)
        return rec

    def remove(self, endpoint: str) -> None:
        self._save([s for s in self.subscriptions() if s["endpoint"] != endpoint])

    def forget_all(self) -> None:
        self._save([])

    # ------------------------------------------------------------ RFC 8291 encryption
    @staticmethod
    def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, salt: bytes | None = None,
                as_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
        """aes128gcm body for one push message (RFC 8291 §3 with the RFC 8188 header). `salt` and `as_key` are
        chosen fresh per message; the parameters exist for the RFC's test vector."""
        if len(ua_public) != UA_PUBLIC_LEN or len(auth_secret) != 16:
            raise ValueError("bad subscription keys")
        salt = salt or os.urandom(16)
        as_key = as_key or ec.generate_private_key(ec.SECP256R1())
        as_public = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
        ecdh = as_key.exchange(ec.ECDH(), ua_key)
        ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_public + as_public).derive(ecdh)
        prk = _hkdf_extract(salt, ikm)
        cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\x00").derive(prk)
        nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\x00").derive(prk)
        if len(plaintext) + 17 > RECORD_SIZE:
            raise ValueError("push payload too long")
        body = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)        # 0x02: the last (only) record
        header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
        return header + body

    # ------------------------------------------------------------ delivery
    def send(self, sub: dict, payload: dict, subject: str, ttl: int = 3600, urgency: str = "high", topic: str | None = None) -> int:
        """Deliver one message to one subscription (blocking: run in a thread). Returns the HTTP status; 404/410 mean
        the subscription is gone (the caller forgets it)."""
        body = self.encrypt(json.dumps(payload).encode(), unb64u(sub["keys"]["p256dh"]), unb64u(sub["keys"]["auth"]))
        headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(int(ttl)),
                   "Urgency": urgency, "Authorization": self.vapid_authorization(sub["endpoint"], subject)}
        if topic:
            headers["Topic"] = topic[:32]
        req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10, context=_tls_context()) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    import hmac
    import hashlib
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _tls_context() -> ssl.SSLContext:
    try:
        import certifi                                      # python.org / packaged Pythons have no system CA bundle
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
