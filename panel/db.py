"""Connection and settings helpers (MariaDB via PyMySQL)."""
import os
import sys

import pymysql
from pymysql.cursors import DictCursor

DEFAULT_SETTINGS = {
    # expired customers: kept online in the "expired-notice" RADIUS group (MikroTik address list "expired") and shown
    # the unpaid-bill page (mod_expired.py) instead of being refused
    "expired_notice_on": "0", "expired_notice_phone": "", "expired_notice_msg": "", "expired_notice_rate": "256k/256k",
    "expired_notice_bkash": "1", "expired_notice_server": "",
    "isp_name": "My ISP",
    "currency": "BDT",
    "interim_interval": "600",   # seconds, sent to routers as Acct-Interim-Interval
    "stale_minutes": "45",       # close online sessions with no update for this long (0 = off)
    "authlog_days": "30",        # keep login attempts for this many days
    "server_stats_days": "180",  # keep CPU / RAM / disk samples for this many days
    "archive_months": "12",      # move accounting rows older than this to radacct_archive (0 = never)
    "coa_port": "3799",          # router port for disconnect requests
    # general
    "show_credit": "0", "company_slogan": "", "support_phone": "", "support_email": "", "company_address": "", "timezone": "Asia/Dhaka",
    "address": "", "city": "", "country": "", "zip_code": "", "latitude": "", "longitude": "",
    "meta_description": "", "meta_keyword": "", "meta_author": "", "copyright_text": "",
    "phone_format_on": "0", "phone_format": "[11-13]", "identity_format_on": "0", "identity_format": "[11-13]",
    "online_refresh": "30", "table_view": "standard", "gmap_on": "0", "gmap_key": "", "coa_log": "1",
    "renew_fail_log": "1", "mac_lock_mode": "off", "nas_mismatch_notify": "1", "nas_auto_update": "1",
    "nat_on": "0", "nat_push_on": "0", "nat_url": "http://127.0.0.1:8080", "nat_token": "",
    "nat_export_days": "7", "nat_export_rows": "100000", "nat_view": "details",
    "nat_last_session": "", "nat_last_stop": "", "nat_last_sub": "", "nat_last_nas": "",
    "sub_identity_verify": "0", "sub_phone_otp": "0", "sub_email_verify": "0", "sub_self_reg": "1",
    "sub_see_volume": "1", "sub_update_data": "1", "sub_reset_pw": "1", "sub_login_log": "1",
    "sub_session_log": "1", "sub_self_activation": "1", "sub_all_packages": "0", "sub_two_factor": "0",
    "grace_on": "0", "grace_days": "10", "dup_phone": "1", "dup_nid": "1", "identity_optional": "1",
    "doc_upload": "1", "serial_prefix": "", "portal_on": "0",
    "tg_on": "0", "tg_token": "", "tg_chat": "", "al_nas": "1", "al_drop": "1", "al_drop_pct": "25", "al_server": "1",
    "al_cpu": "90", "al_ram": "90", "al_disk": "85", "al_radius": "1", "al_daily": "1", "al_daily_hour": "8",
    "user_doc_verify": "0", "user_phone_otp": "0", "user_email_verify": "0", "user_dup_phone": "1", "user_dup_nid": "1",
    "tier_on_reseller": "1", "tier_on_subreseller": "1", "tier_on_retailer": "1",
    "tier_name_reseller": "", "tier_plural_reseller": "", "tier_name_subreseller": "", "tier_plural_subreseller": "",
    "tier_name_retailer": "", "tier_plural_retailer": "", "general_name": "", "general_plural": "",
    "bkash_on": "0", "bkash_mode": "live", "bkash_key": "", "bkash_secret": "", "bkash_user": "", "bkash_pass": "",
    "bkash_fee": "0", "bkash_callback_base": "", "bkash_fee_type": "percent", "bkash_merchant": "", "bkash_topics": "", "bkash_ref": "auto",
    "backup_on": "1", "backup_hour": "4", "backup_keep": "7", "backup_last_day": "", "backup_last_msg": "",
    "backup_last_ok": "", "backup_last_time": "",
    "gdrive_on": "0", "gdrive_client_id": "", "gdrive_client_secret": "", "gdrive_refresh_token": "",
    "gdrive_folder_id": "", "gdrive_keep": "14", "gdrive_device": "",
    "modules_off": "notices",           # comma list of modules switched off for everyone
    # NOC service (noc.py): router health every 30 s, Telegram on router down / high CPU, auto-disconnect of expired
    "noc_snmp_community": "", "noc_cpu_alert": "85", "al_noc": "1", "auto_kick_expired": "1",
    "btrc_division": "", "btrc_district": "", "btrc_thana": "", "btrc_client_type": "Home", "btrc_connection_type": "Wired",
    "btrc_distribution_point": "NOC", "btrc_connectivity_type": "Shared",
    # email
    "smtp_host": "", "smtp_port": "587", "smtp_security": "starttls", "smtp_user": "", "smtp_pass": "",
    "smtp_from": "", "smtp_from_name": "", "email_rate": "120",
    # sms gateway
    "sms_url": "", "sms_method": "GET", "sms_body": "", "sms_success": "", "sms_sender": "",
    "sms_rate": "600", "sms_country_code": "880", "sms_number_format": "intl",
    # automatic messages
    "auto_sms_welcome": "0", "auto_sms_payment": "0", "auto_email_welcome": "0", "auto_email_payment": "0",
    "auto_sms_expiry_days": "", "auto_email_expiry_days": "", "reminder_hour": "10",
}


def connect():
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "radius_panel"),
        password=os.environ.get("DB_PASS", ""),
        database=os.environ.get("DB_NAME", "radius"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=5,
    )


# ---------------------------------------------------------------------------
# Secret settings are encrypted at rest with Fernet when PANEL_SETTINGS_KEY is
# set in the environment (panel.env, generated by install.sh / update.sh; the
# key never lives in the database). Encrypted values carry the "enc:v1:"
# prefix. Without a key (older installs) values stay plaintext so nothing
# breaks; plaintext values are read as-is and re-encrypted on the next save
# or by the daily maintenance pass (encrypt_existing_secrets).
# Keep a copy of panel.env with your backups: without the key, encrypted
# secrets cannot be read back.
SECRET_KEYS = {
    "bkash_key", "bkash_secret", "bkash_pass", "bkash_token_cache",
    "tg_token", "nat_token", "gdrive_client_secret", "gdrive_refresh_token",
    "smtp_pass", "noc_snmp_community", "gmap_key", "sms_url",
}
_ENC_PREFIX = "enc:v1:"
_fernet = None
_fernet_tried = False


def _get_fernet():
    global _fernet, _fernet_tried
    if _fernet_tried:
        return _fernet
    _fernet_tried = True
    key = os.environ.get("PANEL_SETTINGS_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode())
    except Exception:
        _fernet = None
    return _fernet


def encrypt_value(value):
    """Encrypt one value for storage; returns it unchanged without a key."""
    f = _get_fernet()
    if f is None or value == "":
        return value
    return _ENC_PREFIX + f.encrypt(str(value).encode()).decode()


_warned = False


def _try_decrypt(value):
    """Plaintext of an "enc:v1:" value, or None when it cannot be read (no key, wrong key, no cryptography)."""
    f = _get_fernet()
    if f is None:
        return None
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        return None


def decrypt_value(value):
    """Decrypt a stored value; plaintext (no prefix) passes through unchanged.
    An encrypted value that cannot be read comes back as "" (never the ciphertext, which would otherwise be
    used as a password), with one warning in the log; the dashboard lists it (unreadable_secrets)."""
    global _warned
    if not isinstance(value, str) or not value.startswith(_ENC_PREFIX):
        return value
    out = _try_decrypt(value)
    if out is None:
        if not _warned:
            _warned = True
            print("WARNING: an encrypted setting could not be read. PANEL_SETTINGS_KEY in panel.env is missing or "
                  "not the key it was saved with (or python3-cryptography is not installed).", file=sys.stderr)
        return ""
    return out


def unreadable_secrets(rows):
    """Keys among (skey, svalue) rows that are encrypted but cannot be decrypted with the current key."""
    return [k for k, v in rows if isinstance(v, str) and v.startswith(_ENC_PREFIX) and _try_decrypt(v) is None]


def encrypt_existing_secrets(cur):
    """Re-encrypt any secret settings still stored as plaintext. Returns count."""
    if _get_fernet() is None:
        return 0
    cur.execute("SELECT skey, svalue FROM settings WHERE skey IN (%s)"
                % ",".join(["%s"] * len(SECRET_KEYS)), tuple(SECRET_KEYS))
    n = 0
    for row in cur.fetchall():
        if row["svalue"] and not row["svalue"].startswith(_ENC_PREFIX):
            cur.execute("UPDATE settings SET svalue = %s WHERE skey = %s",
                        (encrypt_value(row["svalue"]), row["skey"]))
            n += 1
    return n


def load_settings(cur):
    cur.execute("SELECT skey, svalue FROM settings")
    out = dict(DEFAULT_SETTINGS)
    for row in cur.fetchall():
        out[row["skey"]] = decrypt_value(row["svalue"])
    return out


def save_setting(cur, key, value):
    value = str(value)
    if key in SECRET_KEYS:
        value = encrypt_value(value)
    cur.execute(
        "INSERT INTO settings (skey, svalue) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)",
        (key, value),
    )


# partner tiers: pure helpers shared by the panel and the maintenance job
TIERS = [("reseller", "Reseller", "Resellers"), ("subreseller", "Sub-reseller", "Sub-resellers"),
         ("retailer", "Retailer", "Retailers")]


def tiers_off(s):
    """Roles that are switched off. A tier is also off while the tier above it is off."""
    off, above_off = set(), False
    for key, _, _ in TIERS:
        if above_off or s.get("tier_on_" + key, "1") != "1":
            above_off = True
            off.add(key)
    return off


def blocked_usernames(admins, s):
    """Usernames that may not sign in because their tier, or the tier of someone above them, is off."""
    off = tiers_off(s)
    if not off:
        return set()
    by_id = {a["id"]: a for a in admins}
    out = set()
    for a in admins:
        node, hops = a, 0
        while node and hops < 8:
            if node["role"] in off:
                out.add(a["username"])
                break
            node, hops = by_id.get(node.get("parent_id")), hops + 1
    return out


