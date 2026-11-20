"""TOTP two-factor authentication for panel admins (RFC 6238, stdlib only).

Secrets are base32 strings; the caller stores them encrypted (db.encrypt_value).
Backup codes are shown once and stored only as sha256 hashes.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def new_secret():
    """A fresh 160-bit base32 TOTP secret (32 characters, no padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32, counter):
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return "%06d" % code


def match_step(secret_b32, code, window=1):
    """The 30-second time step whose code is `code` (allowing +/- `window` steps), or None.
    The caller stores the last step used so the same code cannot be used twice."""
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not secret_b32 or not code.isdigit() or len(code) != 6:
        return None
    counter = int(time.time()) // 30
    for d in range(-window, window + 1):
        try:
            if hmac.compare_digest(_hotp(secret_b32, counter + d), code):
                return counter + d
        except (ValueError, TypeError):      # not a valid base32 secret
            return None
    return None


def verify(secret_b32, code, window=1):
    """True if `code` is the current TOTP code (allowing +/- `window` steps)."""
    return match_step(secret_b32, code, window) is not None


def otpauth_uri(secret_b32, username, issuer):
    """The otpauth:// URI an authenticator app scans or accepts by hand."""
    return "otpauth://totp/%s:%s?secret=%s&issuer=%s&digits=6&period=30" % (
        quote(issuer), quote(username), secret_b32, quote(issuer))


def new_backup_codes(n=8):
    """One-time backup codes like 1234-5678 (shown once, stored hashed)."""
    return ["%04d-%04d" % (secrets.randbelow(10000), secrets.randbelow(10000))
            for _ in range(n)]


def hash_backup_code(code):
    return hashlib.sha256((code or "").replace("-", "").replace(" ", "").encode()).hexdigest()
