"""Phone notifications done in the app: the local CA and HTTPS certificate, VAPID, RFC 8291 encryption (checked
against the RFC's own test vector), the subscription store and delivery bookkeeping."""
import datetime as dt
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand

import phone_push as pp


def test_rfc8291_test_vector_encrypts_byte_for_byte():
    # RFC 8291 Appendix A: the user agent's keys, the application server's key and salt, the expected body
    ua_priv = int.from_bytes(pp.unb64u("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big")
    ua_pub = pp.unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    auth = pp.unb64u("BTBZMqHH6r4Tts7J_aSIgg")
    salt = pp.unb64u("DGv6ra1nlYgDCS1FRnbzlw")
    as_key = ec.derive_private_key(int.from_bytes(pp.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    expect = pp.unb64u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
                       "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    body = pp.PushStore.encrypt(b"When I grow up, I want to be a watermelon", ua_pub, auth, salt=salt, as_key=as_key)
    assert body == expect
    # and the receiver's side (RFC 8291 §3.4) gets the text back from a message made with fresh keys
    ua_key = ec.derive_private_key(ua_priv, ec.SECP256R1())
    body = pp.PushStore.encrypt(b"fit T3", ua_pub, auth)
    salt2, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub = body[21:21 + idlen]
    ecdh = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua_pub + as_pub).derive(ecdh)
    prk = pp._hkdf_extract(salt2, ikm)
    cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\x00").derive(prk)
    assert rs == 4096 and AESGCM(cek).decrypt(nonce, body[21 + idlen:], None) == b"fit T3\x02"


def test_vapid_authorization_is_a_valid_es256_jwt_for_the_push_service(tmp_path):
    st = pp.PushStore(tmp_path)
    key, pub = st.vapid()
    assert len(pp.unb64u(pub)) == 65 and pp.unb64u(pub)[0] == 4
    assert st.vapid()[1] == pub                                       # kept, not regenerated
    auth = st.vapid_authorization("https://web.push.apple.com/QAbc123", "https://agenticcad.github.io/agenticcad/", now=1_700_000_000)
    assert auth.startswith("vapid t=") and auth.endswith(f", k={pub}")
    jwt = auth[len("vapid t="):].split(",")[0]
    h, c, sig = jwt.split(".")
    assert json.loads(pp.unb64u(h)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(pp.unb64u(c))
    assert claims["aud"] == "https://web.push.apple.com" and claims["exp"] == 1_700_000_000 + 12 * 3600 and claims["sub"].startswith("https://")
    raw = pp.unb64u(sig)
    assert len(raw) == 64
    key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))


def test_local_ca_and_server_certificate_fit_apple_rules_and_reissue_only_the_leaf(tmp_path):
    st = pp.PushStore(tmp_path)
    info = st.ensure_certs(["studio-mac.local", "192.168.0.20", "127.0.0.1"])
    ca = x509.load_pem_x509_certificate(st.ca_crt.read_bytes())
    leaf = x509.load_pem_x509_certificate(st.srv_crt.read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    leaf.verify_directly_issued_by(ca)                               # signed by our CA
    assert info["hosts"] == ["studio-mac.local", "192.168.0.20", "127.0.0.1"]
    assert ExtendedKeyUsage_has_server_auth(leaf)
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc <= dt.timedelta(days=825)   # Apple's limit
    assert (st.dir / "ca.key").stat().st_mode & 0o077 == 0
    ca_pem, leaf_pem = st.ca_crt.read_bytes(), st.srv_crt.read_bytes()
    st.ensure_certs(["studio-mac.local", "192.168.0.20", "127.0.0.1"])
    assert st.srv_crt.read_bytes() == leaf_pem                        # nothing to do
    st.ensure_certs(["studio-mac.local", "10.0.0.5", "127.0.0.1"])     # the LAN address changed
    assert st.srv_crt.read_bytes() != leaf_pem and st.ca_crt.read_bytes() == ca_pem
    assert "10.0.0.5" in st.leaf_hosts()
    assert pp.b64u(st.ca_der()[:4]) and x509.load_der_x509_certificate(st.ca_der()).subject == ca.subject
    st.new_ca()
    st.ensure_certs(["studio-mac.local"])
    assert st.ca_crt.read_bytes() != ca_pem                           # start over: a new CA


def ExtendedKeyUsage_has_server_auth(cert):
    from cryptography.x509.oid import ExtendedKeyUsageOID
    return ExtendedKeyUsageOID.SERVER_AUTH in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value


def _subscription(n=1):
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://web.push.apple.com/sub{n}", "keys": {"p256dh": pp.b64u(pub), "auth": pp.b64u(b"\x01" * 16)}}


def test_subscription_store_validates_and_replaces_by_endpoint(tmp_path):
    st = pp.PushStore(tmp_path)
    with pytest.raises(ValueError):
        st.add({"endpoint": "http://x", "keys": {}})
    with pytest.raises(ValueError):
        st.add({"endpoint": "https://x", "keys": {"p256dh": "AA", "auth": "AA"}})
    a = st.add(_subscription(1), label="iPhone · Safari")
    st.add(_subscription(2), label="Pixel")
    st.add({**_subscription(1), "keys": a["keys"]}, label="iPhone again")      # same endpoint: replaced, not doubled
    subs = st.subscriptions()
    assert [s["label"] for s in subs] == ["Pixel", "iPhone again"]
    st.remove(subs[0]["endpoint"])
    assert len(st.subscriptions()) == 1
    st.forget_all()
    assert st.subscriptions() == []


def test_send_builds_an_aes128gcm_post_with_vapid(tmp_path, monkeypatch):
    st = pp.PushStore(tmp_path)
    sub = st.add(_subscription(3))
    seen = {}

    class R:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(req, timeout, context):
        seen["url"], seen["headers"], seen["body"] = req.full_url, dict(req.header_items()), req.data
        return R()
    monkeypatch.setattr(pp.urllib.request, "urlopen", fake_open)
    assert st.send(sub, {"title": "Tool change"}, "https://agenticcad.github.io/agenticcad/", ttl=600, topic="toolchange") == 201
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert seen["url"] == sub["endpoint"] and h["content-encoding"] == "aes128gcm" and h["ttl"] == "600" and h["urgency"] == "high"
    assert h["topic"] == "toolchange" and h["authorization"].startswith("vapid t=")
    assert seen["body"][16:20] == (4096).to_bytes(4, "big") and seen["body"][20] == 65
