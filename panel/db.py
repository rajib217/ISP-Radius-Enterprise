"""Connection and settings helpers (MariaDB via PyMySQL)."""
import os
import sys

import pymysql
from pymysql.cursors import DictCursor

DEFAULT_SETTINGS = {
    # tickets: time limit per priority (hours), alert when nobody takes a new one (minutes), area technicians,
    # customer messages, the customer's code on Done, portal tickets (tickets_lib.py)
    "sla_on": "1", "sla_urgent": "2", "sla_high": "4", "sla_normal": "24", "sla_low": "72", "sla_unassigned_min": "60",
    "sla_cat_connection": "", "sla_cat_slow": "", "sla_cat_billing": "", "sla_cat_new": "", "sla_cat_shift": "",
    "sla_cat_hardware": "", "sla_cat_other": "",
    "ticket_auto_assign": "1", "ticket_sms_on": "1", "ticket_app_on": "1", "ticket_otp_on": "0",
    "ticket_portal_on": "1", "ticket_portal_route": "office", "ticket_low_rating": "2",
    "ticket_purge_on": "1", "ticket_purge_month": "",
    # expired customers: kept online in the "expired-notice" RADIUS group (MikroTik address list "expired") and shown
    # the unpaid-bill page (mod_expired.py) instead of being refused
    "expired_notice_on": "0", "expired_notice_phone": "", "expired_notice_msg": "", "expired_notice_rate": "256k/256k",
    "expired_notice_bkash": "1", "expired_notice_server": "", "expired_notice_hours": "0",
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
    "doc_upload": "1", "serial_prefix": "", "sub_id_prefix": "RN", "portal_on": "0",
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
    "smtp_pass", "noc_snmp_community", "gmap_key", "sms_url", "push_vapid_private", "bksms_token",
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


# ------------------------------------------------------------------ shared SQL (alias s = subscribers; no Flask, so the
# pages, the daily report and the Telegram message all use the same rules)
SELLERS_SQL = "SELECT username FROM panel_admins WHERE role IN ('reseller', 'subreseller', 'retailer')"
_PLAN_TIME = "NULLIF((SELECT px.fixed_expire_time FROM plans px WHERE px.id = s.plan_id), '')"
# when the package ends: the customer's own time, else the package's, else 23:59:59 (as RADIUS cuts them)
PKG_END = "TIMESTAMP(s.expiry_date, COALESCE(NULLIF(s.expiry_time, ''), " + _PLAN_TIME + ", '23:59:59'))"
# a grace period still running: RADIUS lets them in until the grace day at the package's time
GRACE_ON = ("(s.grace_until IS NOT NULL AND s.grace_until > s.expiry_date AND "
            "TIMESTAMP(s.grace_until, COALESCE(" + _PLAN_TIME + ", '23:59:59')) > NOW())")
# an active customer whose package has ended (the Dues list, the Expired counts everywhere)
ENDED = "s.status = 'active' AND s.expiry_date IS NOT NULL AND " + PKG_END + " <= NOW()"
# an active customer whose package is still running (the Active counts); ENDED + RUNNING + suspended = every customer
RUNNING = "s.status = 'active' AND (s.expiry_date IS NULL OR " + PKG_END + " > NOW())"


def expiring_sql(days):
    """Still running and ends within `days` days (days x 24 hours)."""
    return "(s.status = 'active' AND s.expiry_date IS NOT NULL AND " + PKG_END + " > NOW() AND " + PKG_END + \
        " <= NOW() + INTERVAL %d DAY)" % int(days)
# what the customer pays for their package now: their seller's price (+ subscriber profit), else the package price,
# plus the package's active taxes / fees (mod_subs.customer_price)
_BASE = ("COALESCE(NULLIF((SELECT up.price + COALESCE(up.sub_profit, 0) FROM user_plans up JOIN panel_admins ua ON ua.id = up.user_id "
         "WHERE ua.username = s.created_by AND up.plan_id = s.plan_id LIMIT 1), 0), "
         "(SELECT pz.price FROM plans pz WHERE pz.id = s.plan_id), 0)")
CUSTOMER_PRICE = ("ROUND(" + _BASE + " + COALESCE((SELECT SUM(CASE WHEN f.mode = 'percent' THEN " + _BASE + " * f.value / 100 "
                  "ELSE f.value END) FROM fees f JOIN plan_fees pf ON pf.fee_id = f.id WHERE f.active = 1 AND pf.plan_id = s.plan_id), 0), 2)")
_CHARGES = "COALESCE((SELECT SUM(c.amount) FROM sub_charges c WHERE c.subscriber_id = s.id AND c.paid = 0), 0)"
_BAL = "GREATEST(COALESCE(s.balance, 0), 0)"
# what an ended customer owes now (as on their page and for the bill collector): the package less what is already in the
# balance, plus unpaid charges less any balance left over
DUE_SQL = ("(GREATEST(" + CUSTOMER_PRICE + " - " + _BAL + ", 0) + GREATEST(" + _CHARGES + " - GREATEST(" + _BAL + " - "
           + CUSTOMER_PRICE + ", 0), 0))")
# what any customer owes now (the bill collector's lists, the map): an ended package as above, otherwise only unpaid
# charges less the balance; and the package part alone
OWED_SQL = "(CASE WHEN " + ENDED + " THEN " + DUE_SQL + " ELSE GREATEST(" + _CHARGES + " - " + _BAL + ", 0) END)"
PKG_OWED_SQL = "(CASE WHEN " + ENDED + " THEN GREATEST(" + CUSTOMER_PRICE + " - " + _BAL + ", 0) ELSE 0 END)"
# a payment that is the company's own income: from the office's own customers (a reseller's customer pays the reseller;
# the company's income from them is the balance the reseller buys, counted from user_ledger)
OWN_PAYMENT = ("(p.subscriber_id IS NULL OR p.subscriber_id NOT IN (SELECT id FROM subscribers WHERE created_by IN ("
               + SELLERS_SQL + ")))")
# money a reseller / sub-reseller / retailer paid the company for balance (bKash, or balance added by staff), after its
# corrections; the same rule as Accounting > Cashflow "User Payment"
TOPUP_FROM = ("FROM user_ledger ul JOIN panel_admins a ON a.id = ul.user_id LEFT JOIN (SELECT ref_id, SUM(amount) AS adj "
              "FROM user_ledger WHERE method = 'correction' AND ref_id IS NOT NULL GROUP BY ref_id) cr ON cr.ref_id = ul.id "
              "WHERE ul.amount > 0 AND ul.method NOT IN ('activation', 'correction') AND a.role IN ('reseller', 'subreseller', "
              "'retailer') AND (ul.method = 'bkash' OR ul.created_by NOT IN (" + SELLERS_SQL + "))")
TOPUP_AMOUNT = "(ul.amount + COALESCE(cr.adj, 0))"


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


