"""Web Push for the customer app: a notification on the phone's lock screen / notification bar even when the app is
closed, with the phone's own notification sound and vibration.

Standards, done with python3-cryptography only (no extra package):
  RFC 8292  VAPID: the server proves who it is with an ES256-signed JWT
  RFC 8291  the message is encrypted for that one browser (ECDH P-256 + HKDF + AES-128-GCM, "aes128gcm")

The VAPID key pair is made once and kept in the settings table (the private half encrypted like the other secrets).
The isp-panel-sender service sends the pushes (sender.push_round); the browser shows them from the service worker.
Browsers do not let a website choose a custom notification sound: the phone plays its normal notification tone."""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

# a subscription endpoint must belong to a real browser push service: the sender POSTs to it, so anything else
# (an address inside the network) is refused when the customer's browser registers it
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com", "push.apple.com",
              "notify.windows.com", "push.microsoft.com", "web.push.apple.com")


def available():
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
        return True
    except Exception:
        return False


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ub64(text):
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def endpoint_ok(url):
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and len(url) <= 1000 and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


# ===================================================================== keys
def new_keys():
    """(private, public) VAPID keys, base64url: 32-byte private value, 65-byte uncompressed public point."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u(priv), b64u(pub)


def keys(cur):
    """The panel's VAPID keys, made on first use. Two processes starting at once keep the first pair (INSERT IGNORE)."""
    import db
    cur.execute("SELECT skey, svalue FROM settings WHERE skey IN ('push_vapid_private', 'push_vapid_public')")
    have = {r["skey"]: r["svalue"] for r in cur.fetchall()}
    if not (have.get("push_vapid_private") and have.get("push_vapid_public")):
        priv, pub = new_keys()
        cur.execute("INSERT IGNORE INTO settings (skey, svalue) VALUES ('push_vapid_private', %s)", (db.encrypt_value(priv),))
        cur.execute("INSERT IGNORE INTO settings (skey, svalue) VALUES ('push_vapid_public', %s)", (pub,))
        cur.execute("SELECT skey, svalue FROM settings WHERE skey IN ('push_vapid_private', 'push_vapid_public')")
        have = {r["skey"]: r["svalue"] for r in cur.fetchall()}
    return db.decrypt_value(have.get("push_vapid_private", "")), have.get("push_vapid_public", "")


def _private(priv_b64):
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.derive_private_key(int.from_bytes(ub64(priv_b64), "big"), ec.SECP256R1())


def vapid_header(endpoint, priv_b64, pub_b64, subject):
    """The Authorization header: 'vapid t=<ES256 JWT>, k=<public key>' (RFC 8292)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    u = urllib.parse.urlsplit(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": "%s://%s" % (u.scheme, u.netloc), "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}, separators=(",", ":")).encode())
    signing = (head + "." + claims).encode()
    r, s = decode_dss_signature(_private(priv_b64).sign(signing, ec.ECDSA(hashes.SHA256())))
    return "vapid t=%s.%s, k=%s" % (signing.decode(), b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big")), pub_b64)


# ===================================================================== encryption (RFC 8291, aes128gcm)
def encrypt(payload, p256dh_b64, auth_b64, salt=None, server_key=None):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_pub = ub64(p256dh_b64)
    auth = ub64(auth_b64)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # one record, padding delimiter 0x02
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def send(sub, data, priv_b64, pub_b64, subject, ttl=86400):
    """POST one push. Returns (status, detail): 201 sent; 404 / 410 the subscription is gone; 0 network error."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "TTL": str(ttl), "Urgency": "high", "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "Authorization": vapid_header(sub["endpoint"], priv_b64, pub_b64, subject)})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        return e.code, detail
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)[:200]


def subject_of(s):
    mail = (s.get("support_email") or "").strip()
    if "@" in mail:
        return "mailto:" + mail
    base = (s.get("bkash_callback_base") or "").strip().rstrip("/")
    return base if base.startswith("https://") else "mailto:noreply@example.com"
