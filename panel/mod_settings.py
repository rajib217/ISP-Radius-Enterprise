"""Settings pages for email, the SMS gateway and automatic messages (General settings stay in app.py)."""
import json
import os
import re
import zoneinfo

from flask import abort, flash, redirect, render_template, request, send_file, url_for

import db
import sendlib
from core import EMAIL_RE, admin_required, audit, cfg, commit, get_db, q, route
import core
import msglib


def _save(values):
    with get_db().cursor() as cur:
        for k, v in values.items():
            db.save_setting(cur, k, v)


def _int(f, key, low, high):
    value = int(f.get(key, ""))
    if not low <= value <= high:
        raise ValueError(key)
    return str(value)


@route("/settings/email", methods=["GET", "POST"])
@admin_required
def settings_email():
    s = cfg()
    if request.method == "POST":
        f = request.form
        try:
            security = f.get("smtp_security", "starttls")
            assert security in ("starttls", "ssl", "none")
            sender = f.get("smtp_from", "").strip()
            assert not sender or EMAIL_RE.match(sender)
            new = {"smtp_host": f.get("smtp_host", "").strip()[:255], "smtp_port": _int(f, "smtp_port", 1, 65535),
                   "smtp_security": security, "smtp_user": f.get("smtp_user", "").strip()[:255],
                   "smtp_from": sender[:255], "smtp_from_name": f.get("smtp_from_name", "").strip()[:64],
                   "email_rate": _int(f, "email_rate", 1, 600)}
            if f.get("clear_pass") == "1":
                new["smtp_pass"] = ""
            elif f.get("smtp_pass"):
                new["smtp_pass"] = f["smtp_pass"][:255]
        except (ValueError, AssertionError):
            flash("Check the port, the From address and the sending speed. Nothing was saved.", "error")
            return redirect(url_for("settings_email"))
        _save(new)
        audit("settings_email_saved")
        commit()
        flash("Email settings saved. Send a test message to make sure they work.", "success")
        return redirect(url_for("settings_email"))
    return render_template("settings_email.html", s=s, tab="email")


@route("/settings/email/test", methods=["POST"])
@admin_required
def settings_email_test():
    to = request.form.get("to", "").strip()
    if not EMAIL_RE.match(to):
        flash("Enter the address to send the test to.", "error")
        return redirect(url_for("settings_email"))
    mailer = sendlib.Mailer(cfg())
    try:
        mailer.send(to, "Test message from your ISP panel", "If you can read this, email sending works.")
        flash("Test email sent to %s." % to, "success")
    except sendlib.SendError as exc:
        flash("Test failed: %s" % exc, "error")
    finally:
        mailer.close()
    return redirect(url_for("settings_email"))


@route("/settings/sms", methods=["GET", "POST"])
@admin_required
def settings_sms():
    s = cfg()
    if request.method == "POST":
        f = request.form
        try:
            url = f.get("sms_url", "").strip()
            assert not url or url.lower().startswith(("http://", "https://"))
            method = f.get("sms_method", "GET")
            assert method in ("GET", "POST")
            fmt = f.get("sms_number_format", "intl")
            assert fmt in ("intl", "local", "plus")
            cc = "".join(ch for ch in f.get("sms_country_code", "") if ch.isdigit())
            assert len(cc) <= 4
            new = {"sms_url": url[:1000], "sms_method": method, "sms_body": f.get("sms_body", "").strip()[:1000],
                   "sms_success": f.get("sms_success", "").strip()[:200], "sms_sender": f.get("sms_sender", "").strip()[:64],
                   "sms_rate": _int(f, "sms_rate", 1, 3000), "sms_country_code": cc, "sms_number_format": fmt}
        except (ValueError, AssertionError):
            flash("Check the gateway address (it must start with http:// or https://), the country code and the "
                  "sending speed. Nothing was saved.", "error")
            return redirect(url_for("settings_sms"))
        _save(new)
        audit("settings_sms_saved")
        commit()
        flash("SMS gateway saved. Send a test message to make sure it works.", "success")
        return redirect(url_for("settings_sms"))
    return render_template("settings_sms.html", s=s, tab="sms")


@route("/settings/sms/test", methods=["POST"])
@admin_required
def settings_sms_test():
    s = cfg()
    number = msglib.normalize_phone(request.form.get("to", ""), s["sms_country_code"], s["sms_number_format"])
    if not number:
        flash("Enter a valid phone number to send the test to.", "error")
        return redirect(url_for("settings_sms"))
    try:
        reply = sendlib.send_sms(s, number, "Test message from your ISP panel.")
        flash("Gateway accepted the message for %s. Its answer: %s" % (number, reply or "(empty)"), "success")
    except sendlib.SendError as exc:
        flash("Test failed: %s" % exc, "error")
    return redirect(url_for("settings_sms"))


@route("/settings/automation", methods=["GET", "POST"])
@admin_required
def settings_automation():
    s = cfg()
    if request.method == "POST":
        f = request.form
        try:
            new = {k: "1" if f.get(k) == "1" else "0" for k in
                   ("auto_sms_welcome", "auto_sms_payment", "auto_email_welcome", "auto_email_payment")}
            new["auto_sms_expiry_days"] = ",".join(str(d) for d in msglib.parse_days(f.get("auto_sms_expiry_days")))
            new["auto_email_expiry_days"] = ",".join(str(d) for d in msglib.parse_days(f.get("auto_email_expiry_days")))
            new["reminder_hour"] = _int(f, "reminder_hour", 0, 23)
        except ValueError:
            flash("The reminder hour must be between 0 and 23. Nothing was saved.", "error")
            return redirect(url_for("settings_automation"))
        _save(new)
        audit("settings_automation_saved")
        commit()
        flash("Automatic messages saved.", "success")
        return redirect(url_for("settings_automation"))
    return render_template("settings_automation.html", s=s, tab="automation")


# ===================================================================== alerts (Telegram)
@route("/settings/alerts", methods=["GET", "POST"])
@admin_required
def settings_alerts():
    import alertlib
    s = cfg()
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        token = f.get("tg_token", "").strip() or s.get("tg_token", "")
        chat = f.get("tg_chat", "").strip()
        if action == "find":             # the person wrote to the bot: read the chat id from Telegram
            if not alertlib.TOKEN_RE.match(token):
                flash("Paste the bot token first (it looks like 123456789:AA...).", "error")
            else:
                ok, res = alertlib.tg_call(token, "getUpdates", {"limit": 20})
                chats = [(u.get("message") or u.get("channel_post") or {}).get("chat", {}) for u in (res if ok else [])]
                chats = [c for c in chats if c.get("id")]
                if not ok:
                    flash("Telegram said: %s" % res, "error")
                elif not chats:
                    flash("Telegram has no message for this bot yet. Open your bot in Telegram, press Start, send it any word, then press this button again.", "error")
                else:
                    _save({"tg_token": token, "tg_chat": str(chats[-1]["id"])})
                    audit("settings_saved", "alerts")
                    commit()
                    flash("Found your chat (%s) and saved it. Now press Send test message." % (chats[-1].get("first_name") or chats[-1].get("title") or chats[-1]["id"]), "success")
            return redirect(url_for("settings_alerts"))
        if token and not alertlib.TOKEN_RE.match(token):
            flash("That does not look like a bot token. It looks like 123456789:AAH... (from @BotFather). Nothing was saved.", "error")
            return redirect(url_for("settings_alerts"))
        if chat and not alertlib.CHAT_RE.match(chat):
            flash("The chat ID is a number such as 123456789 (or -100... for a group). Nothing was saved.", "error")
            return redirect(url_for("settings_alerts"))
        try:
            new = {k: "1" if f.get(k) == "1" else "0" for k in ("tg_on", "al_nas", "al_drop", "al_server", "al_radius", "al_daily")}
            new["al_drop_pct"] = _int(f, "al_drop_pct", 5, 90)
            new["al_cpu"] = _int(f, "al_cpu", 50, 100)
            new["al_ram"] = _int(f, "al_ram", 50, 100)
            new["al_disk"] = _int(f, "al_disk", 50, 99)
            new["al_daily_hour"] = _int(f, "al_daily_hour", 0, 23)
        except ValueError:
            flash("Check the limits: drop 5-90%, CPU/RAM 50-100%, disk 50-99%, hour 0-23. Nothing was saved.", "error")
            return redirect(url_for("settings_alerts"))
        new["tg_token"], new["tg_chat"] = token, chat
        _save(new)
        audit("settings_saved", "alerts")
        commit()
        if action == "test":
            merged = dict(s, **new)
            ok, err = alertlib.send(merged, "%s\n✅ Test message. Alerts will arrive here." % (merged.get("isp_name") or "RADIUS panel"))
            flash("Test message sent. Check Telegram on your phone." if ok else "Saved, but Telegram did not accept the message: %s" % err,
                  "success" if ok else "error")
        else:
            flash("Alert settings saved.", "success")
        return redirect(url_for("settings_alerts"))
    return render_template("settings_alerts.html", s=s, tab="alerts", has_token=bool(s.get("tg_token")))


# ===================================================================== general, software, modules, log engine
CURRENCIES = ["BDT", "USD", "EUR", "GBP", "INR", "PKR", "NPR", "LKR", "AED", "SAR", "MYR", "IDR", "PHP", "NGN",
              "KES", "ZAR", "EGP", "TRY"]
GATEWAYS = [("bkash", "bKash", "phone")]
BRAND_TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg"),
               b"GIF8": ("gif", "image/gif"), b"RIFF": ("webp", "image/webp"), b"\x00\x00\x01\x00": ("ico", "image/x-icon")}


def _brand_dir():
    path = os.path.join(core.DATA_DIR, "branding")
    os.makedirs(path, exist_ok=True)
    return path


def _brand_file(kind):
    path = os.path.join(core.DATA_DIR, "branding")
    if os.path.isdir(path):
        for name in os.listdir(path):
            if name.startswith(kind + "."):
                return os.path.join(path, name)
    return None


def brand_urls():
    out = {}
    for kind in ("logo", "favicon"):
        f = _brand_file(kind)
        out[kind] = url_for("branding", kind=kind, v=int(os.path.getmtime(f))) if f else ""
    return out


@route("/branding/<kind>")
def branding(kind):
    f = _brand_file(kind) if kind in ("logo", "favicon") else None
    if not f:
        abort(404)
    mime = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
            "ico": "image/x-icon"}[f.rsplit(".", 1)[1]]
    resp = send_file(f, mimetype=mime, max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@route("/settings/brand/<kind>", methods=["POST"])
@admin_required
def settings_brand(kind):
    if kind not in ("logo", "favicon"):
        abort(404)
    if request.form.get("remove"):
        f = _brand_file(kind)
        if f:
            os.remove(f)
        audit("brand_removed", kind)
        flash("%s removed." % kind.capitalize(), "success")
        return redirect(request.referrer or url_for("settings"))
    up = request.files.get("file")
    data = up.read(1024 * 1024 + 1) if up else b""
    if not data or len(data) > 1024 * 1024:
        flash("Choose an image file up to 1 MB.", "error")
        return redirect(url_for("settings"))
    ext = next((v for k, v in BRAND_TYPES.items() if data.startswith(k)), None)
    if not ext or (ext[0] == "webp" and data[8:12] != b"WEBP"):
        flash("Use a PNG, JPG, GIF, WebP or ICO image.", "error")
        return redirect(url_for("settings"))
    old = _brand_file(kind)
    if old:
        os.remove(old)
    with open(os.path.join(_brand_dir(), "%s.%s" % (kind, ext[0])), "wb") as fh:
        fh.write(data)
    audit("brand_saved", kind)
    flash("%s saved." % kind.capitalize(), "success")
    return redirect(request.referrer or url_for("settings"))


@route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    s = cfg()
    if request.method == "POST":
        f = request.form
        need = {"isp_name": 64, "company_slogan": 120, "support_phone": 32, "support_email": 120, "address": 200,
                "city": 80, "country": 80, "zip_code": 16}
        new = {k: f.get(k, "").strip()[:n] for k, n in need.items()}
        missing = [k for k, v in new.items() if not v]
        if missing:
            flash("Please fill in all the fields marked with a star.", "error")
            return redirect(url_for("settings"))
        if not EMAIL_RE.match(new["support_email"]):
            flash("The support email does not look right.", "error")
            return redirect(url_for("settings"))
        zone = f.get("timezone", "").strip()
        if zone not in zoneinfo.available_timezones():
            flash("Choose a timezone from the list.", "error")
            return redirect(url_for("settings"))
        new["timezone"] = zone
        new["currency"] = f.get("currency", "").strip().upper()[:8] or "BDT"
        for k, n in (("meta_description", 200), ("meta_keyword", 200), ("meta_author", 80), ("copyright_text", 120)):
            new[k] = f.get(k, "").strip()[:n]
        for k, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
            v = f.get(k, "").strip()
            try:
                if v:
                    assert lo <= float(v) <= hi
            except (ValueError, AssertionError):
                flash("Latitude and longitude must be numbers (latitude -90 to 90, longitude -180 to 180).", "error")
                return redirect(url_for("settings"))
            new[k] = v
        new["show_credit"] = "1" if f.get("show_credit") else "0"
        _save(new)
        audit("settings_saved", "general")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    zones = sorted(z for z in zoneinfo.available_timezones() if "/" in z and not z.startswith(("Etc/", "posix", "right")))
    currencies = CURRENCIES if s["currency"] in CURRENCIES else CURRENCIES + [s["currency"]]
    return render_template("settings.html", s=s, zones=zones, currencies=currencies, tab="general")


@route("/settings/software", methods=["GET", "POST"])
@admin_required
def settings_software():
    if request.method == "POST":
        f = request.form
        try:
            new = {"interim_interval": _int(f, "interim_interval", 0, 86400), "stale_minutes": _int(f, "stale_minutes", 0, 10080),
                   "coa_port": _int(f, "coa_port", 1, 65535)}
        except ValueError:
            flash("One of the numbers is out of range. Nothing was saved.", "error")
            return redirect(url_for("settings_software"))
        for key in ("phone_format", "identity_format"):
            spec = f.get(key, "").strip()
            if not core.format_range(spec):
                flash("Formats look like [11-13] (the allowed number of digits) or [11].", "error")
                return redirect(url_for("settings_software"))
            new[key] = spec
        for key in ("phone_format_on", "identity_format_on", "gmap_on", "coa_log", "renew_fail_log",
                    "nas_mismatch_notify", "nas_auto_update"):
            new[key] = "1" if f.get(key) else "0"
        new["online_refresh"] = f.get("online_refresh") if f.get("online_refresh") in ("0", "10", "15", "30", "60", "120", "300") else "30"
        new["table_view"] = "compact" if f.get("table_view") == "compact" else "standard"
        new["mac_lock_mode"] = f.get("mac_lock_mode") if f.get("mac_lock_mode") in ("off", "lock", "remove") else "off"
        new["gmap_key"] = f.get("gmap_key", "").strip()[:128]
        if new["gmap_key"] and not re.match(r"^[A-Za-z0-9_\-]+$", new["gmap_key"]):
            flash("The Google Maps API key has odd characters. Copy it again from the Google console.", "error")
            return redirect(url_for("settings_software"))
        import radius
        old = cfg()["interim_interval"]
        with get_db().cursor() as cur:
            for k, v in new.items():
                db.save_setting(cur, k, v)
            if new["interim_interval"] != old:
                cur.execute("SELECT * FROM plans")
                for plan in cur.fetchall():
                    radius.sync_plan(cur, plan, new["interim_interval"])
        audit("settings_saved", "software")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_software"))
    return render_template("settings_software.html", s=cfg(), tab="software")


@route("/settings/log-engine", methods=["GET", "POST"])
@admin_required
def settings_logs():
    if request.method == "POST":
        f = request.form
        try:
            new = {"authlog_days": _int(f, "authlog_days", 1, 3650),
                   "archive_months": _int(f, "archive_months", 0, 120),
                   "nat_export_days": _int(f, "nat_export_days", 1, 31),
                   "nat_export_rows": _int(f, "nat_export_rows", 1000, 500000)}
        except ValueError:
            flash("One of the numbers is out of range. Nothing was saved.", "error")
            return redirect(url_for("settings_logs"))
        url = f.get("nat_url", "").strip()[:200]
        if url and not re.match(r"^https?://[^\s]+$", url):
            flash("Engine URL must start with http:// or https://. Nothing was saved.", "error")
            return redirect(url_for("settings_logs"))
        new["nat_url"] = url
        new["nat_token"] = f.get("nat_token", "").strip()[:200]
        new["nat_on"] = "1" if f.get("nat_on") else "0"
        new["nat_push_on"] = "1" if f.get("nat_push_on") else "0"
        new["nat_view"] = "raw" if f.get("nat_view") == "raw" else "details"
        _save(new)
        audit("settings_saved", "log engine")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_logs"))
    return render_template("settings_logs.html", s=cfg(), tab="logs")


@route("/settings/log-engine/test", methods=["POST"])
@admin_required
def settings_logs_test():
    import urllib.request
    s = cfg()
    url = (s.get("nat_url") or "").strip()
    if not re.match(r"^https?://[^\s]+$", url):
        flash("Save a valid Engine URL first.", "error")
        return redirect(url_for("settings_logs"))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + s.get("nat_token", "")} if s.get("nat_token") else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            flash("The engine answered (HTTP %s)." % r.status, "success")
    except urllib.error.HTTPError as e:
        flash("The engine answered with HTTP %s. It is reachable; check the token if you expected success." % e.code, "error")
    except Exception as e:
        flash("Could not reach the engine: %s" % str(e)[:160], "error")
    return redirect(url_for("settings_logs"))


def _name_ok(v):
    return len(v) <= 30 and all(ch.isalnum() or ch in " -." for ch in v)


@route("/settings/partners", methods=["GET", "POST"])
@admin_required
def settings_partners():
    if request.method == "POST":
        f = request.form
        new = {}
        for key, _, _ in core.TIERS:
            new["tier_on_" + key] = "1" if f.get("tier_on_" + key) else "0"
            for field in ("tier_name_", "tier_plural_"):
                v = " ".join(f.get(field + key, "").split())
                if not _name_ok(v):
                    flash("Names: up to 30 letters, digits, spaces, \"-\" or \".\". Nothing was saved.", "error")
                    return redirect(url_for("settings_partners"))
                new[field + key] = v
        for field in ("general_name", "general_plural"):
            v = " ".join(f.get(field, "").split())
            if not _name_ok(v):
                flash("Names: up to 30 letters, digits, spaces, \"-\" or \".\". Nothing was saved.", "error")
                return redirect(url_for("settings_partners"))
            new[field] = v
        _save(new)
        audit("settings_saved", "partner management")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_partners"))
    counts = {r["role"]: r["c"] for r in core.q("SELECT role, COUNT(*) AS c FROM panel_admins WHERE is_active = 1 GROUP BY role")}
    return render_template("settings_partners.html", s=cfg(), tiers=core.TIERS, counts=counts,
                           off=core.tiers_off(), tab="partners")


USER_SWITCHES = [("user_doc_verify", "Document Verification"), ("user_phone_otp", "Phone Verification (OTP)"),
                 ("user_email_verify", "Email Verification"), ("user_dup_phone", "Allow Duplicate Phone"),
                 ("user_dup_nid", "Allow Duplicate NID/NIC")]


@route("/settings/user", methods=["GET", "POST"])
@admin_required
def settings_user():
    if request.method == "POST":
        _save({k: "1" if request.form.get(k) else "0" for k, _ in USER_SWITCHES})
        audit("settings_saved", "user")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_user"))
    return render_template("settings_user.html", s=cfg(), switches=USER_SWITCHES, tab="user")


SUB_SWITCHES = [   # customer portal switches (see mod_portal.py)
    ("sub_identity_verify", "Identity File Verification"), ("sub_phone_otp", "Phone Verification (OTP)"),
    ("sub_email_verify", "Email Verification"), ("sub_self_reg", "Self Registation"),
    ("sub_see_volume", "See Volume Info"), ("sub_update_data", "Allow To Update Data"),
    ("sub_reset_pw", "Reset Password"), ("sub_login_log", "Login Log"), ("sub_session_log", "Session Log"),
    ("sub_self_activation", "Self Activation"), ("sub_all_packages", "Allow All Packages"),
    ("sub_two_factor", "Two Factor Authentication"),
]
SUB_RULES = [("dup_phone", "Allow Duplicate Phone"), ("dup_nid", "Allow Duplicate NID/NIC"),
             ("identity_optional", "Identity Optional"), ("doc_upload", "Allow Document Upload")]


@route("/settings/subscriber", methods=["GET", "POST"])
@admin_required
def settings_subscriber():
    if request.method == "POST":
        f = request.form
        try:
            new = {"grace_days": _int(f, "grace_days", 1, 90)}
        except ValueError:
            flash("Grace period must be 1 to 90 days. Nothing was saved.", "error")
            return redirect(url_for("settings_subscriber"))
        prefix = f.get("serial_prefix", "").strip()
        if not re.match(r"^[0-9]*$", prefix) or len(prefix) > 20:
            flash("Serial number must be digits only (for example 2001000).", "error")
            return redirect(url_for("settings_subscriber"))
        new["serial_prefix"] = prefix
        for key, _ in SUB_SWITCHES + SUB_RULES + [("grace_on", ""), ("portal_on", "")]:
            new[key] = "1" if f.get(key) else "0"
        _save(new)
        audit("settings_saved", "subscriber")
        commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_subscriber"))
    return render_template("settings_subscriber.html", s=cfg(), switches=SUB_SWITCHES, rules=SUB_RULES, tab="subscriber")


MODULE_SWITCHES = [   # key, label, module it controls (None = not built yet)
    ("voucher", "Voucher Module Status", None),
    ("prepaid", "Prepaid Card Module Status", None),
    ("inventory", "Inventory Module Status", "inventory"),
    ("tickets", "Tickets Module Status", "tickets"),
    ("notices", "Notices Module Status", "notices"),
    ("zones", "Department/Zone/POP Module Status", "zones"),
]


@route("/settings/modules", methods=["GET", "POST"])
@admin_required
def settings_modules():
    managed = [m for _, _, m in MODULE_SWITCHES if m]
    if request.method == "POST":
        on = set(request.form.getlist("on"))
        off = core.modules_off()
        for m in managed:
            (off.discard if m in on else off.add)(m)
        value = ",".join(sorted(off))
        _save({"modules_off": value})
        audit("settings_saved", "modules", value or "all on")
        commit()
        flash("Modules updated.", "success")
        return redirect(url_for("settings_modules"))
    off = core.modules_off()
    items = [(k, label, bool(m)) for k, label, m in MODULE_SWITCHES]
    return render_template("settings_modules.html", items=items,
                           off={k for k, _, m in MODULE_SWITCHES if m and m in off}, tab="modules")


BKASH_REFS = [("auto", "Automatic (current behaviour)", "11 or more digits = phone number, 6 or fewer digits = subscriber ID, letters = username. 7-10 digit references are not matched."),
              ("username", "Username", "The subscriber's username."),
              ("serial", "Serial Number (Unique ID)", "The subscriber's serial number."),
              ("phone", "Phone Number", "The subscriber's phone number (88 / +88 in front is accepted)."),
              ("id", "Subscriber ID", "The subscriber's ID in this panel.")]


@route("/settings/gateway/<slug>", methods=["GET", "POST"])
@admin_required
def settings_gateway(slug):
    gw = next((g for g in GATEWAYS if g[0] == slug), None)
    if not gw:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            fee = float(f.get("bkash_fee", "0") or 0)
            if not 0 <= fee <= 100000:
                raise ValueError
        except ValueError:
            flash("Transaction fee must be a number of 0 or more. Nothing was saved.", "error")
            return redirect(url_for("settings_gateway", slug=slug))
        fee_type = "fixed" if f.get("bkash_fee_type") == "fixed" else "percent"
        if fee_type == "percent" and fee > 100:
            flash("A percentage fee cannot be above 100. Nothing was saved.", "error")
            return redirect(url_for("settings_gateway", slug=slug))
        new = {"bkash_on": "1" if f.get("bkash_on") else "0",
               "bkash_mode": "sandbox" if f.get("bkash_mode") == "sandbox" else "live",
               "bkash_fee": ("%g" % fee), "bkash_fee_type": fee_type,
               "bkash_ref": f.get("bkash_ref") if f.get("bkash_ref") in dict((k, 1) for k, _, _ in BKASH_REFS) else "auto"}
        for key, limit in (("bkash_key", 200), ("bkash_secret", 200), ("bkash_user", 64), ("bkash_pass", 200),
                           ("bkash_merchant", 200), ("bkash_topics", 1000), ("bkash_callback_base", 200)):
            new[key] = f.get(key, "").strip()[:limit]
        # Secret and Password are never sent back to the browser, so an empty box means "keep the saved one"
        for key in ("bkash_secret", "bkash_pass"):
            if not new[key]:
                new[key] = cfg().get(key, "")
        base =new["bkash_callback_base"].rstrip("/")
        if base and not (base.startswith("https://") or base.startswith("http://")):
            flash("Callback address must start with https:// (or http://). Nothing was saved.", "error")
            return redirect(url_for("settings_gateway", slug=slug))
        new["bkash_callback_base"] = base
        if new["bkash_on"] == "1" and not all(new[k] for k in ("bkash_key", "bkash_secret", "bkash_user", "bkash_pass")):
            flash("Key, Secret, Username and Password are required to switch bKash on. Nothing was saved.", "error")
            return redirect(url_for("settings_gateway", slug=slug))
        _save(new)
        audit("settings_saved", "bkash")
        commit()
        flash("bKash settings saved.", "success")
        return redirect(url_for("settings_gateway", slug=slug))
    logs, stuck = [], []
    try:                       # tables appear when install.sh is re-run
        logs = q("SELECT created_at, flow, ref_id, payment_id, step, http_status, ms, ok, error, response FROM gateway_logs "
                 "ORDER BY id DESC LIMIT 30")
        stuck = q("SELECT 'portal' AS flow, p.id, s.username AS who, p.amount + p.fee AS total, p.payment_id, p.message, p.created_at "
                  "FROM portal_payments p LEFT JOIN subscribers s ON s.id = p.subscriber_id WHERE p.status = 'pending' "
                  "UNION ALL SELECT 'topup', t.id, a.username, t.amount + t.fee, t.payment_id, t.message, t.created_at "
                  "FROM topups t LEFT JOIN panel_admins a ON a.id = t.user_id WHERE t.status = 'pending' "
                  "ORDER BY created_at DESC LIMIT 30")
    except Exception:
        get_db().rollback()
    for r in logs:
        try:
            ans = json.loads(r["response"] or "{}")
        except ValueError:
            ans = {}
        r["said"] = str(ans.get("statusMessage") or ans.get("errorMessage") or ans.get("transactionStatus") or r["error"] or "")[:120]
    return render_template("settings_gateway.html", gw=gw, s=cfg(), refs=BKASH_REFS, tab="gw-" + slug, logs=logs, stuck=stuck)
