"""Users and roles, own profile, My Packages, departments / zones, areas."""
from datetime import date, timedelta

from flask import send_file, session, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import json
import os
from functools import wraps
import re
import secrets
import core
import db
import permcatalog
import totp
from core import (EMAIL_RE, LEVELS, MODULES, PHONE_RE, ROLES, USERNAME_RE, admin_required, audit, commit,
                  get_db, insert, parse_date, q, route, x)


# ===================================================================== My Profile
@route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        need = {"password": "prof.password", "details": "prof.edit"}.get(request.form.get("section") or "details")
        if need and not core.can_fn(need):
            abort(403)
        if request.form.get("section") == "2fa_start":
            session["totp_pending_secret"] = totp.new_secret()
            flash("Add the key below to your authenticator app, then enter a code to switch two-factor on.", "success")
        elif request.form.get("section") == "2fa_confirm":
            secret = session.get("totp_pending_secret", "")
            if not secret:
                flash("Start again: press Set up two-factor first.", "error")
            elif totp.match_step(secret, request.form.get("code", "")) is None:
                flash("That code did not match. Check the app and try again.", "error")
            else:
                codes = totp.new_backup_codes()
                step = totp.match_step(secret, request.form.get("code", ""))
                x("UPDATE panel_admins SET totp_secret = %s, totp_enabled = 1, totp_backup_codes = %s, totp_last_step = %s "
                  "WHERE id = %s",
                  (db.encrypt_value(secret), json.dumps([totp.hash_backup_code(c) for c in codes]), step, g.me["id"]))
                audit("2fa_enabled", g.me["username"])
                commit()
                session.pop("totp_pending_secret", None)
                session["totp_new_backup"] = codes
                flash("Two-factor is on. Save your backup codes below — they are shown only once.", "success")
        elif request.form.get("section") == "2fa_disable":
            row = q("SELECT password_hash, totp_secret FROM panel_admins WHERE id = %s", (g.me["id"],), one=True)
            if not check_password_hash(row["password_hash"], request.form.get("current", "")):
                flash("Current password is wrong.", "error")
            elif not totp.verify(db.decrypt_value(row["totp_secret"] or ""), request.form.get("code", "")):
                flash("The code from your authenticator app is wrong. Lost the app? Ask an administrator to reset two-factor.",
                      "error")
            else:
                x("UPDATE panel_admins SET totp_secret = '', totp_enabled = 0, totp_backup_codes = NULL, totp_last_step = 0 "
                  "WHERE id = %s",
                  (g.me["id"],))
                audit("2fa_disabled", g.me["username"])
                commit()
                flash("Two-factor is off.", "success")
        elif request.form.get("section") == "password":
            row = q("SELECT password_hash FROM panel_admins WHERE id = %s", (g.me["id"],), one=True)
            new, again = request.form.get("new", ""), request.form.get("again", "")
            if not check_password_hash(row["password_hash"], request.form.get("current", "")):
                flash("Current password is wrong.", "error")
            elif len(new) < 8:
                flash("New password must be at least 8 characters.", "error")
            elif new != again:
                flash("The two new passwords do not match.", "error")
            else:
                x("UPDATE panel_admins SET password_hash = %s WHERE id = %s",
                  (generate_password_hash(new), g.me["id"]))
                audit("password_changed", g.me["username"])
                commit()
                flash("Password changed.", "success")
        else:
            name = request.form.get("full_name", "").strip()[:128]
            phone, email = request.form.get("phone", "").strip(), request.form.get("email", "").strip()
            if not PHONE_RE.match(phone):
                flash("Phone: digits, + - ( ) and spaces only.", "error")
            elif email and (not EMAIL_RE.match(email) or len(email) > 128):
                flash("Enter a valid email address.", "error")
            elif phone and core.cfg().get("user_dup_phone") != "1" and q(
                    "SELECT 1 AS x FROM panel_admins WHERE phone = %s AND id <> %s LIMIT 1", (phone, g.me["id"]), one=True):
                flash("Another user already has this phone number.", "error")
            else:
                x("UPDATE panel_admins SET full_name = %s, phone = %s, email = %s WHERE id = %s",
                  (name, phone, email, g.me["id"]))
                audit("profile_updated", g.me["username"])
                commit()
                flash("Profile saved.", "success")
        return redirect(url_for("profile"))
    me = q("SELECT a.*, d.name AS dept, z.name AS zone FROM panel_admins a "
           "LEFT JOIN departments d ON d.id = a.department_id LEFT JOIN zones z ON z.id = a.zone_id "
           "WHERE a.id = %s", (g.me["id"],), one=True)
    today = date.today()
    mine = q("SELECT COUNT(*) AS subs FROM subscribers WHERE created_by = %s", (me["username"],), one=True)
    pay = q("SELECT COALESCE(SUM(CASE WHEN paid_at >= %s THEN amount ELSE 0 END), 0) AS today_total, "
            "COALESCE(SUM(amount), 0) AS month_total, COUNT(*) AS payments FROM payments "
            "WHERE received_by = %s AND paid_at >= %s",
            (today, me["username"], today.replace(day=1)), one=True)
    activity = q("SELECT action, target, detail, created_at FROM audit_log WHERE admin = %s "
                 "ORDER BY id DESC LIMIT 12", (me["username"],))
    pending_secret = session.get("totp_pending_secret", "")
    pending_uri = totp.otpauth_uri(pending_secret, me["username"],
                                   core.cfg().get("isp_name") or "RADIUS Panel") if pending_secret else ""
    return render_template("profile.html", u=me, mine=mine, pay=pay, activity=activity,
                           totp_pending_secret=pending_secret, totp_pending_uri=pending_uri,
                           totp_new_backup=session.pop("totp_new_backup", []))


@route("/account")
def account():
    return redirect(url_for("profile"))


# ===================================================================== My Packages
@route("/my-packages")
def my_packages():
    if (g.me["role"] in core.SELLER_ROLES or g.me["role"] in core.ROLE_LOCKS) and not core.can_fn("pkg.module"):
        abort(403)                        # technicians never (core.ROLE_LOCKS / permcatalog.ROLE_NEVER)
    allowed = core.allowed_plan_ids()
    me = g.me["username"]
    names = list(core.my_downline() or [me])     # a seller's own customers and their downline's
    rows = q("SELECT p.*, (SELECT COUNT(*) FROM subscribers s WHERE s.plan_id = p.id) AS total, "
             "(SELECT COUNT(*) FROM subscribers s WHERE s.plan_id = p.id AND s.created_by IN (" + ",".join(["%s"] * len(names)) +
             ")) AS mine FROM plans p ORDER BY p.download_mbps, p.label", names)
    if allowed is not None:
        rows = [r for r in rows if r["id"] in allowed]
    month = {r["plan_id"]: r for r in q(
        "SELECT s.plan_id, COUNT(*) AS payments, COALESCE(SUM(pay.amount), 0) AS amount "
        "FROM payments pay JOIN subscribers s ON s.id = pay.subscriber_id "
        "WHERE pay.received_by = %s AND pay.paid_at >= %s GROUP BY s.plan_id",
        (me, date.today().replace(day=1)))}
    seller = g.me["role"] in core.SELLER_ROLES
    prices = {r["plan_id"]: r for r in q("SELECT plan_id, price, sub_profit FROM user_plans WHERE user_id = %s", (g.me["id"],))} if seller else {}
    for r in rows:
        r["month"] = month.get(r["id"]) or {"payments": 0, "amount": 0}
        r["rate"] = core.bw_text(r)
        if seller:
            # a seller sees what they pay and what their customers pay, never the ISP-wide customer count
            up = prices.get(r["id"]) or {}
            import mod_policy
            # no price set for them: they pay the package's own price (mod_subs.seller_cost)
            r["my_cost"] = float(up["price"]) if up.get("price") is not None else float(r["price"] or 0)
            # what their customer pays: cost + subscriber profit, plus the package's taxes / fees (as charged at renewal)
            r["customer_price"] = mod_policy.fee_total(r["my_cost"] + float(up.get("sub_profit") or 0), r["id"])
            r.pop("total", None)
    return render_template("my_packages.html", rows=rows, restricted=allowed is not None, seller=seller)


# ===================================================================== My Users (a seller's own downline)
@route("/my-users")
def my_users():
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    labels = dict(core.role_list())
    rows = q("SELECT a.id, a.username, a.full_name, a.role, a.balance, a.is_active, a.phone, a.email, a.identity, a.address, a.max_children, "
             "(SELECT COUNT(*) FROM panel_admins k WHERE k.parent_id = a.id) AS kids, "
             "(SELECT COUNT(*) FROM subscribers s WHERE s.created_by = a.username) AS subs "
             "FROM panel_admins a WHERE a.parent_id = %s ORDER BY a.username", (g.me["id"],))
    for r in rows:
        r["role_label"] = labels.get(r["role"], r["role"])
    child = CHILD_ROLE.get(g.me["role"])
    if child in core.tiers_off():
        child = None
    for r in rows:
        r.update({k: r.get(k, "") or "" for k in ("phone", "email", "identity", "address")})
    return render_template("my_users.html", rows=rows, methods=BALANCE_METHODS, child_label=labels.get(child, "") if child else "")


@route("/my-users/balance", methods=["POST"])
def user_transfer():
    """A seller gives balance to one of the people directly below them: it is taken from the seller's own balance."""
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    uid = request.form.get("user_id", "")
    row = q("SELECT id, username, balance FROM panel_admins WHERE id = %s AND parent_id = %s AND role NOT IN ('technician', 'collector')",
            (int(uid), g.me["id"]), one=True) if uid.isdigit() else None
    if not row:
        abort(404)
    if request.form.get("method", "") not in dict(BALANCE_METHODS):
        flash("Choose the payment method.", "error")
        return redirect(url_for("my_users"))
    try:
        amount = round(float(request.form.get("amount", "0")), 2)
        assert amount != 0 and abs(amount) < 10 ** 8
    except (ValueError, AssertionError):
        flash("Enter an amount (use a minus sign to take balance back).", "error")
        return redirect(url_for("my_users"))
    new = round(float(row["balance"] or 0) + amount, 2)
    if new < 0:
        flash("%s only has %.2f, so you cannot take back %.2f." % (row["username"], float(row["balance"] or 0), -amount), "error")
        return redirect(url_for("my_users"))
    ok, have = core.seller_pays(amount, "Balance %s %s" % ("to" if amount > 0 else "back from", row["username"]),
                                request.form.get("method"))
    if not ok:
        flash("Your balance (%.2f) is lower than %.2f. Add balance to yourself first. Nothing was changed." % (have, amount), "error")
        return redirect(url_for("my_users"))
    x("UPDATE panel_admins SET balance = %s WHERE id = %s", (new, row["id"]))
    insert("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) VALUES (%s, %s, %s, %s, %s, %s)",
           (row["id"], amount, new, request.form.get("method"),
            ("From %s. %s" % (g.me["username"], request.form.get("note", "").strip()))[:255], g.me["username"]))
    audit("user_balance", row["username"], "%+.2f -> %.2f (from %s)" % (amount, new, g.me["username"]))
    commit()
    flash("%s now has %.2f. %.2f was taken from your balance." % (row["username"], new, amount), "success")
    return redirect(url_for("my_users"))


def _limit_in(text):
    """Blank = no limit. Otherwise a whole number from 0 to 100000."""
    text = str(text or "").strip()
    if not text:
        return None, True
    if text.isdigit() and int(text) <= 100000:
        return int(text), True
    return None, False


def _room_under(parent_id, skip_id=0):
    """None when the person may take another user directly below them, else the message to show."""
    p = q("SELECT username, max_children FROM panel_admins WHERE id = %s", (parent_id,), one=True)
    if not p or p.get("max_children") in (None, ""):
        return None
    n = q("SELECT COUNT(*) AS n FROM panel_admins WHERE parent_id = %s AND id <> %s", (parent_id, skip_id), one=True)
    if int(list(n.values())[0] if n else 0) >= int(p["max_children"]):
        return "%s is limited to %d user%s directly below them and already has that many." % (
            p["username"], p["max_children"], "" if p["max_children"] == 1 else "s")
    return None


CHILD_ROLE = {"reseller": "subreseller", "subreseller": "retailer"}


def _my_child(aid):
    """A person directly under the signed-in seller, or 404."""
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    return q("SELECT * FROM panel_admins WHERE id = %s AND parent_id = %s", (aid, g.me["id"]), one=True) or abort(404)


# own people a reseller tier adds under My Users: role -> (tick needed, label)
OWN_STAFF = {"technician": ("usr.tech", "technician"), "collector": ("usr.collector", "bill collector")}


def own_key(role):
    """The tick a reseller tier needs to manage this person below them (their own technician / bill collector)."""
    return OWN_STAFF[role][0] if role in OWN_STAFF else None


def _my_tech_create(f, name, phone, email, identity, address, username, password, role="technician"):
    """A reseller / sub-reseller / retailer adds their own technician or bill collector (My Users): an account under
    them that sees and works on only their customers (core.seller_boss / my_downline)."""
    back = url_for("my_users")
    key, label = OWN_STAFF[role]
    if not core.can_fn(key):
        abort(403)
    if not name or not phone or not PHONE_RE.match(phone):
        flash("Full name and a phone number are required for a %s." % label, "error")
        return redirect(back)
    if email and (not EMAIL_RE.match(email) or len(email) > 128):
        flash("Check the email address.", "error")
        return redirect(back)
    if not USERNAME_RE.match(username) or len(password) < 8:
        flash("Username (3-64 characters) and a password of at least 8 characters are required.", "error")
        return redirect(back)
    if q("SELECT 1 AS x FROM panel_admins WHERE username = %s", (username,), one=True):
        flash("That username is taken.", "error")
        return redirect(back)
    insert("INSERT INTO panel_admins (username, full_name, password_hash, role, phone, email, parent_id, created_by, "
           "is_active, sms_on, email_on, identity, address) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 1, 1, %s, %s)",
           (username, name, generate_password_hash(password), role, phone, email, g.me["id"], g.me["username"], identity, address))
    audit(role + "_created", username, "by %s" % g.me["username"])
    commit()
    flash("%s %s added. They sign in with this username and see only your customers." % (label.capitalize(), username), "success")
    return redirect(back)


@route("/my-users/save", methods=["POST"])
def my_user_save():
    """A seller creates or edits someone directly below them (the next tier only). The admin must tick Add / Edit."""
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    f = request.form
    back = url_for("my_users")
    aid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, phone, email = f.get("full_name", "").strip()[:128], f.get("phone", "").strip(), f.get("email", "").strip()
    identity, address = f.get("identity", "").strip()[:64], f.get("address", "").strip()[:255]
    password, username = f.get("password", ""), f.get("username", "").strip()
    kind = f.get("kind")
    if kind in OWN_STAFF and not aid:
        return _my_tech_create(f, name, phone, email, identity, address, username, password, role=kind)
    if not PHONE_RE.match(phone) or (email and (not EMAIL_RE.match(email) or len(email) > 128)):
        flash("Check the phone number and email address.", "error")
        return redirect(back)
    if phone and core.cfg().get("user_dup_phone") != "1" and q(
            "SELECT 1 AS x FROM panel_admins WHERE phone = %s AND id <> %s LIMIT 1", (phone, aid), one=True):
        flash("Another user already has this phone number.", "error")
        return redirect(back)
    if aid:
        row = _my_child(aid)
        if not core.can_fn(own_key(row["role"]) or "usr.edit"):
            abort(403)
        if password and len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(back)
        x("UPDATE panel_admins SET full_name = %s, phone = %s, email = %s, identity = %s, address = %s WHERE id = %s",
          (name or row["username"], phone, email, identity, address, aid))
        audit("admin_updated", row["username"], "by %s" % g.me["username"])
    else:
        if not core.can_fn("usr.add"):
            abort(403)
        role = CHILD_ROLE.get(g.me["role"])
        if not role:
            flash("Your account type cannot create other users.", "error")
            return redirect(back)
        if role in core.tiers_off():
            flash("%s is switched off in Settings > Partner Management." % core.tier_name(role), "error")
            return redirect(back)
        if not name or not phone or not identity or not email:
            flash("Full name, identity, phone and email are required.", "error")
            return redirect(back)
        full = _room_under(g.me["id"])
        if full:
            flash("Not created. " + full.replace(g.me["username"] + " is", "You are").replace("them and already", "you and already"), "error")
            return redirect(back)
        if not USERNAME_RE.match(username) or len(password) < 8:
            flash("Username (3-64 characters) and a password of at least 8 characters are required.", "error")
            return redirect(back)
        if q("SELECT 1 AS x FROM panel_admins WHERE username = %s", (username,), one=True):
            flash("That username is taken.", "error")
            return redirect(back)
        insert("INSERT INTO panel_admins (username, full_name, password_hash, role, phone, email, parent_id, created_by, "
               "is_active, sms_on, email_on, identity, address) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 1, 1, %s, %s)",
               (username, name, generate_password_hash(password), role, phone, email, g.me["id"], g.me["username"],
                identity, address))
        audit("admin_created", username, "by %s" % g.me["username"])
    if aid and password:
        x("UPDATE panel_admins SET password_hash = %s WHERE id = %s", (generate_password_hash(password), aid))
    if "max_children" in f and core.can_fn("usr.retlimit" if g.me["role"] == "reseller" else "usr.limit_none"):
        cap, ok = _limit_in(f.get("max_children"))
        own = q("SELECT max_children FROM panel_admins WHERE id = %s", (g.me["id"],), one=True) or {}
        target = aid or None
        if not ok or not target:
            if not ok:
                flash("Saved, but the limit must be a whole number.", "error")
        elif own.get("max_children") not in (None, "") and (cap is None or cap > int(own["max_children"])):
            flash("Saved, but the limit cannot be more than your own (%d)." % int(own["max_children"]), "error")
        else:
            x("UPDATE panel_admins SET max_children = %s WHERE id = %s", (cap, target))
            audit("user_limit", "id %s" % target, "max %s" % ("no limit" if cap is None else cap))
    commit()
    flash("Saved.", "success")
    return redirect(back)


@route("/my-users/<int:aid>/toggle", methods=["POST"])
def my_user_toggle(aid):
    row = _my_child(aid)
    if not core.can_fn(own_key(row["role"]) or "usr.edit"):   # own technicians / collectors: their Add tick
        abort(403)
    x("UPDATE panel_admins SET is_active = 1 - is_active WHERE id = %s", (aid,))
    audit("admin_disabled" if row["is_active"] else "admin_enabled", row["username"], "by %s" % g.me["username"])
    commit()
    flash("%s is now %s." % (row["username"], "disabled" if row["is_active"] else "active"), "success")
    return redirect(url_for("my_users"))


@route("/my-users/<int:aid>/password", methods=["POST"])
def my_user_password(aid):
    row = _my_child(aid)
    if not core.can_fn(own_key(row["role"]) or "usr.password"):   # own technicians / collectors: their Add tick
        abort(403)
    new = request.form.get("password", "")
    if len(new) < 8:
        flash("Password must be at least 8 characters.", "error")
    else:
        x("UPDATE panel_admins SET password_hash = %s WHERE id = %s", (generate_password_hash(new), aid))
        audit("admin_password_set", row["username"], "by %s" % g.me["username"])
        commit()
        flash("Password changed for %s." % row["username"], "success")
    return redirect(url_for("my_users"))


@route("/my-users/<int:aid>/delete", methods=["POST"])
def my_user_delete(aid):
    """Delete someone directly below the seller, only when nothing would be left behind."""
    row = _my_child(aid)
    kids = q("SELECT COUNT(*) AS n FROM panel_admins WHERE parent_id = %s", (aid,), one=True)
    subs = q("SELECT COUNT(*) AS n FROM subscribers WHERE created_by = %s", (row["username"],), one=True)
    n_kids, n_subs = (int(list(r.values())[0]) if r else 0 for r in (kids, subs))
    if n_kids or n_subs:
        flash("%s cannot be deleted: %s. Move or remove them first, or disable the account instead." % (
            row["username"], " and ".join(t for t in ("%d user%s below" % (n_kids, "" if n_kids == 1 else "s") if n_kids else "",
                                                      "%d subscriber%s" % (n_subs, "" if n_subs == 1 else "s") if n_subs else "") if t)), "error")
    elif float(row.get("balance") or 0) != 0:
        flash("%s still has a balance of %.2f. Take it back first, then delete." % (row["username"], float(row["balance"])), "error")
    else:
        x("DELETE FROM user_plans WHERE user_id = %s", (aid,))
        x("UPDATE tickets SET assigned_to = NULL WHERE assigned_to = %s", (aid,))
        x("DELETE FROM panel_admins WHERE id = %s", (aid,))
        audit("admin_deleted", row["username"], "by %s" % g.me["username"])
        commit()
        flash("%s was deleted." % row["username"], "success")
    return redirect(url_for("my_users"))


@route("/my-users/<int:aid>/packages", methods=["GET", "POST"])
def my_user_packages(aid):
    """What each person below pays for a package. Only the packages the seller may use, never below the seller's own price."""
    row = _my_child(aid)
    if row["role"] in OWN_STAFF:              # a technician / bill collector sells nothing
        abort(404)
    u = dict(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"], parent_id=g.me["id"])
    mine = core.allowed_plan_ids()
    return _packages_page(u, "my_user_packages", allowed=mine if mine is not None else None, back=url_for("my_users"),
                          profit_ok=core.can_fn("pkg.profit"))


# ===================================================================== Users
def _lookups():
    return (q("SELECT id, name FROM departments ORDER BY name"), q("SELECT id, name FROM zones ORDER BY name"),
            q("SELECT id, label FROM plans ORDER BY download_mbps, label"))


GROUPS = {"staff": ("Staffs", core.STAFF_ROLES, "Managers, support, accountants and technicians."),
          "reseller": ("Resellers", ("reseller",), "Partners who sell connections and see only their own customers."),
          "subreseller": ("Sub-resellers", ("subreseller",), "Work under a reseller and see only their own customers."),
          "retailer": ("Retailers", ("retailer",), "Small sellers or agents at the bottom of the chain.")}


def _ptype(role):
    return {"admin": "Admin", "reseller": "Reseller", "subreseller": "Subreseller", "retailer": "Retailer"}.get(role, "Staff")


# ---- Users and Roles for office staff: what they may open follows the User ticks (Users > Roles > User). Staff never
# touch an administrator or make anyone one, never change their own account here (role, status, deletion), and never
# hand out more than they have: a role whose ticks they lack, or a tick they lack on a role's permissions. Resellers /
# sub-resellers / retailers manage the people below them under My Users instead.
def _users_gate(*keys):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **k):
            role = g.me["role"]
            if role != "admin" and (role in core.SELLER_ROLES or not any(core.can_fn(key) for key in keys)):
                abort(403)
            return fn(*a, **k)
        return wrapper
    return deco


def _staff():
    return g.me["role"] != "admin"


def _role_funcs(role):
    cache = g.setdefault("_role_funcs", {})
    if role not in cache:
        cache[role] = core.load_funcs(role, core.load_perms(role))
    return cache[role]


def _within_mine(role):
    """May the signed-in user give this role to someone? Administrators: any. Staff: never Administrator; an office role
    only when it can do nothing they cannot; a reseller tier always (it sees only its own customers)."""
    if not _staff():
        return True
    if role == "admin":
        return False
    return role in core.SELLER_ROLES or _role_funcs(role) <= set(g.funcs or ())


def _reach(row, own_ok=False):
    """May the signed-in user change this account?"""
    if not _staff():
        return True
    if row["id"] == g.me["id"]:
        return own_ok
    return _within_mine(row["role"])


def _reach_or_403(row, own_ok=False):
    if not _reach(row, own_ok):
        abort(403)


@route("/admins")
@_users_gate("usr.module")
def admins():
    group = request.args.get("group") if request.args.get("group") in GROUPS else ""
    where, params = "", []
    if group:
        roles_in = core.staff_roles() if group == "staff" else GROUPS[group][1]
        where = " WHERE a.role IN (%s)" % ",".join(["%s"] * len(roles_in))
        params = list(roles_in)
    rows = q("SELECT a.*, d.name AS dept, z.name AS zone, par.username AS parent_name, "
             "(SELECT COUNT(*) FROM subscribers s WHERE s.created_by = a.username) AS subs "
             "FROM panel_admins a LEFT JOIN departments d ON d.id = a.department_id "
             "LEFT JOIN zones z ON z.id = a.zone_id LEFT JOIN panel_admins par ON par.id = a.parent_id" + where +
             " ORDER BY a.id", params)
    plan_map = {}
    for r in q("SELECT user_id, plan_id FROM user_plans"):
        plan_map.setdefault(r["user_id"], []).append(r["plan_id"])
    for r in rows:
        r["plan_ids"] = plan_map.get(r["id"], [])
    flt_role = request.args.get("role", "")
    flt_status = request.args.get("status", "")
    everyone = {r["id"]: r for r in q("SELECT id, username, role, parent_id FROM panel_admins")}
    kids = {}
    for r in everyone.values():
        if r["parent_id"]:
            kids[r["parent_id"]] = kids.get(r["parent_id"], 0) + 1
    labels = dict(core.role_list())
    admin_names = [r["username"] for r in everyone.values() if r["role"] == "admin"]
    only_admin = admin_names[0] if len(admin_names) == 1 else ""
    for r in rows:
        r["kids"] = kids.get(r["id"], 0)
        r["role_label"] = labels.get(r["role"], r["role"])
        r["ptype"] = _ptype(r["role"])
        r["role_full"] = "%s (%s)" % (r["role_label"], r["ptype"]) if r["role"] != "admin" else "%s (Admin)" % r["role_label"]
        up, seen, node = {}, set(), everyone.get(r["parent_id"])
        while node and node["id"] not in seen:
            seen.add(node["id"])
            up.setdefault(node["role"], node["username"])
            node = everyone.get(node["parent_id"])
        r["up_admin"], r["up_reseller"], r["up_sub"] = up.get("admin", ""), up.get("reseller", ""), up.get("subreseller", "")
        if r["role"] != "admin" and not r["up_admin"]:
            r["up_admin"] = r.get("created_by") or (only_admin if only_admin else "")
    if _staff():                                    # staff never see administrators here
        rows = [r for r in rows if r["role"] != "admin"]
    for r in rows:
        r["can_act"] = _reach(r)
    if flt_role in labels:
        rows = [r for r in rows if r["role"] == flt_role]
    if flt_status in ("active", "disabled"):
        rows = [r for r in rows if bool(r["is_active"]) == (flt_status == "active")]
    total_plans = q("SELECT COUNT(*) AS c FROM plans", one=True)["c"]
    depts, zones, plans = _lookups()
    sellers = q("SELECT id, username, role FROM panel_admins WHERE role IN ('reseller', 'subreseller') "
                "ORDER BY username")
    gtitle, _roles, ghelp = GROUPS.get(group, ("Users", None, "Everyone who can sign in to this panel."))
    if group in dict((t[0], 1) for t in core.TIERS):
        gtitle = core.tier_name(group, True)
    off = core.tiers_off()
    return render_template("admins.html", methods=BALANCE_METHODS, rows=rows, roles=core.role_list(),
                           role_opts=[r for r in core.role_list() if r[0] not in off and _within_mine(r[0])], depts=depts, zones=zones, plans=plans,
                           group=group, gtitle=gtitle, ghelp=ghelp, sellers=sellers, total_plans=total_plans,
                           flt_role=flt_role, flt_status=flt_status,
                           all_users=[u for u in everyone.values() if _reach(u)],
                           default_role={"staff": "support", "reseller": "reseller", "subreseller": "subreseller",
                                         "retailer": "retailer"}.get(group, "support"))


@route("/admins/<int:aid>")
@_users_gate("usr.view")
def admin_view(aid):
    u = q("SELECT a.*, d.name AS dept, z.name AS zone, par.username AS parent_name FROM panel_admins a "
          "LEFT JOIN departments d ON d.id = a.department_id LEFT JOIN zones z ON z.id = a.zone_id "
          "LEFT JOIN panel_admins par ON par.id = a.parent_id WHERE a.id = %s", (aid,), one=True) or abort(404)
    if _staff() and u["role"] == "admin":
        abort(403)
    labels = dict(core.role_list())
    u["role_label"] = labels.get(u["role"], u["role"])
    u["can_act"] = _reach(u)
    allowed = [r["plan_id"] for r in q("SELECT plan_id FROM user_plans WHERE user_id = %s", (aid,))]
    all_plans = q("SELECT id, label, name, price, download_mbps, upload_mbps, bandwidth_value, bandwidth_type, "
                  "validity_days, duration, duration_type FROM plans ORDER BY download_mbps, label")
    shown = [p for p in all_plans if (not allowed or p["id"] in allowed)] if u["role"] != "admin" else all_plans
    for p in shown:
        p["rate"] = core.bw_text(p)
    kids = {r["role"]: r["c"] for r in q("SELECT role, COUNT(*) AS c FROM panel_admins WHERE parent_id = %s GROUP BY role", (aid,))}
    subs_n = q("SELECT COUNT(*) AS c FROM subscribers WHERE created_by = %s", (u["username"],), one=True)["c"]
    subs = q("SELECT s.id, s.username, s.full_name, s.phone, s.status, s.expiry_date, p.label AS plan_label "
             "FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.created_by = %s "
             "ORDER BY s.id DESC LIMIT 100", (u["username"],))
    first = date.today().replace(day=1)
    rep = q("SELECT COALESCE(SUM(CASE WHEN paid_at >= CURDATE() THEN amount ELSE 0 END), 0) AS today_total, "
            "COALESCE(SUM(CASE WHEN paid_at >= %s THEN amount ELSE 0 END), 0) AS month_total, "
            "COALESCE(SUM(amount), 0) AS all_total, COUNT(*) AS payments FROM payments WHERE received_by = %s",
            (first, u["username"]), one=True)
    # Ledgers tab: optional ?from= / ?to= dates for both tables; without them, the latest 50 of each
    try:
        dfrom, dto = core.parse_date(request.args.get("from")), core.parse_date(request.args.get("to"))
    except ValueError:
        dfrom = dto = None
    if dfrom and dto and dfrom > dto:
        dfrom, dto = dto, dfrom

    def _span(col):
        cond, args = "", []
        if dfrom:
            cond, args = cond + " AND %s >= %%s" % col, args + [dfrom]
        if dto:
            cond, args = cond + " AND %s < %%s" % col, args + [dto + timedelta(days=1)]
        return cond, args
    limit = 2000 if (dfrom or dto) else 50
    pc, pa = _span("paid_at")
    ledger = q("SELECT username, amount, days_added, method, note, paid_at FROM payments WHERE received_by = %s" + pc +
               " ORDER BY id DESC LIMIT %s", [u["username"]] + pa + [limit])
    wc, wa = _span("created_at")
    wallet = q("SELECT amount, balance_after, method, note, created_by, created_at FROM user_ledger WHERE user_id = %s" + wc +
               " ORDER BY id DESC LIMIT %s", [aid] + wa + [limit])
    sums = None
    if dfrom or dto:
        sums = {"pay": q("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM payments WHERE received_by = %s" + pc,
                         [u["username"]] + pa, one=True),
                "wallet": q("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS added, "
                            "COALESCE(SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END), 0) AS taken FROM user_ledger "
                            "WHERE user_id = %s" + wc, [aid] + wa, one=True)}
    u["ptype"] = _ptype(u["role"])
    off = core.tiers_off()
    depts, zones, plans = _lookups()
    sellers = q("SELECT id, username, role FROM panel_admins WHERE role IN ('reseller', 'subreseller') ORDER BY username")
    return render_template("admin_view.html", methods=BALANCE_METHODS, wallet=wallet, open_balance=bool(request.args.get("balance")), u=u, kids=kids, subs_n=subs_n, subs=subs, rep=rep, ledger=ledger,
                           dfrom=dfrom, dto=dto, sums=sums, ledger_limit=limit,
                           shown=shown, restricted=bool(allowed) and u["role"] != "admin", all_count=len(all_plans),
                           allowed=allowed, roles=core.role_list(), role_opts=[r for r in core.role_list() if r[0] not in off and _within_mine(r[0])],
                           depts=depts, zones=zones, plans=plans, sellers=sellers, group="", default_role="support",
                           gtitle="Users", isp=core.cfg().get("isp_name", ""))


BALANCE_METHODS = [("cash", "Cash"), ("bkash", "bKash"), ("nagad", "Nagad"), ("rocket", "Rocket"), ("bank", "Bank"),
                   ("other", "Other")]


def _money_in(text):
    try:
        v = round(float(str(text).strip()), 2)
    except ValueError:
        return None
    return v if 0 <= v < 10 ** 8 else None


def _packages_page(u, endpoint, allowed=None, back=None, profit_ok=True):
    """Price list for one user (shared by the admin page and by a seller pricing their own downline)."""
    aid = u["id"]
    plans = q("SELECT id, label, name, price FROM plans ORDER BY download_mbps, label")
    if allowed is not None:
        plans = [p for p in plans if p["id"] in allowed]
    mine = {r["plan_id"]: r for r in q("SELECT plan_id, price, sub_profit, subreseller_profit FROM user_plans WHERE user_id = %s", (aid,))}
    parent = {}
    if u["parent_id"]:
        parent = {r["plan_id"]: r["price"] for r in q("SELECT plan_id, price FROM user_plans WHERE user_id = %s AND price IS NOT NULL", (u["parent_id"],))}
    for p in plans:
        p["title"] = p["label"] or p["name"]
        p["min"] = float(parent[p["id"]]) if p["id"] in parent else None
    if request.method == "POST":
        rows, bad = [], []
        for p in plans:
            raw = request.form.get("price_%d" % p["id"], "").strip()
            if raw == "":
                continue
            price = _money_in(raw)
            if profit_ok:
                sp = _money_in(request.form.get("sp_%d" % p["id"], "0") or "0")
                rp = _money_in(request.form.get("rp_%d" % p["id"], "0") or "0")
            else:                    # My Packages > Manage Profit not ticked: the profits stay as they are
                old = mine.get(p["id"]) or {}
                sp, rp = float(old.get("sub_profit") or 0), float(old.get("subreseller_profit") or 0)
            if price is None or sp is None or rp is None or (p["min"] is not None and price < p["min"]):
                bad.append(p["title"])
                continue
            rows.append((aid, p["id"], price, sp, rp))
        if bad:
            flash("Nothing saved. Check the price (it cannot be below the minimum) for: %s." % ", ".join(bad[:6]), "error")
            return redirect(url_for(endpoint, aid=aid))
        if allowed is None:
            x("DELETE FROM user_plans WHERE user_id = %s", (aid,))
        else:   # a seller only manages the packages they may use: leave any other row alone
            for pid in [p["id"] for p in plans]:
                x("DELETE FROM user_plans WHERE user_id = %s AND plan_id = %s", (aid, pid))
        if rows:
            with get_db().cursor() as cur:
                cur.executemany("INSERT INTO user_plans (user_id, plan_id, price, sub_profit, subreseller_profit) "
                                "VALUES (%s, %s, %s, %s, %s)", rows)
        audit("packages_assigned", u["username"], "%d packages" % len(rows))
        commit()
        nothing = "no package (resellers sell only what is given to them)" if u["role"] in core.SELLER_ROLES else "every package"
        flash("Saved. %s can now use %s." % (u["username"], "%d package%s" % (len(rows), "" if len(rows) == 1 else "s") if rows else nothing), "success")
        return redirect(back or (url_for("admin_view", aid=aid) + "#packages"))
    for p in plans:
        r = mine.get(p["id"])
        p["on"] = bool(r)
        p["v_price"] = "" if not r else ("%.2f" % float(r["price"] if r["price"] is not None else p["price"] or 0))
        p["v_sp"] = "0" if not r else "%g" % float(r["sub_profit"] or 0)
        p["v_rp"] = "0" if not r else "%g" % float(r["subreseller_profit"] or 0)
    return render_template("admin_packages.html", u=u, plans=plans, ptype=_ptype(u["role"]), back_url=back,
                           label=dict(core.role_list()).get(u["role"], u["role"]), profit_ok=profit_ok)


@route("/admins/<int:aid>/packages", methods=["GET", "POST"])
@_users_gate("usr.pkg_module", "usr.pkg_assign")
def admin_packages(aid):
    u = q("SELECT id, username, full_name, role, parent_id FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    if u["role"] == "admin":
        abort(404)
    if _staff():
        _reach_or_403(u)
        if request.method == "POST" and not core.can_fn("usr.pkg_assign"):
            abort(403)
    return _packages_page(u, "admin_packages")


@route("/admins/balance", methods=["POST"])
@_users_gate("usr.balance")
def admin_balance():
    uid = request.form.get("user_id", "")
    row = q("SELECT * FROM panel_admins WHERE id = %s", (int(uid),), one=True) if uid.isdigit() else None
    if not row:
        abort(404)
    _reach_or_403(row)
    aid = row["id"]
    method = request.form.get("method", "")
    if method not in dict(BALANCE_METHODS):
        flash("Choose the payment method.", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admin_view", aid=aid))
    try:
        amount = round(float(request.form.get("amount", "0")), 2)
        assert amount != 0 and abs(amount) < 10 ** 8
    except (ValueError, AssertionError):
        flash("Enter an amount (use a minus sign to take balance away).", "error")
        return redirect(url_for("admin_view", aid=aid))
    new = round(float(row.get("balance") or 0) + amount, 2)
    if new < 0:
        flash("The balance cannot go below zero (it is %.2f)." % float(row.get("balance") or 0), "error")
        return redirect(url_for("admin_view", aid=aid))
    x("UPDATE panel_admins SET balance = %s WHERE id = %s", (new, aid))
    insert("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) VALUES (%s, %s, %s, %s, %s, %s)",
           (aid, amount, new, method, request.form.get("note", "").strip()[:255], g.me["username"]))
    audit("user_balance", row["username"], "%+.2f -> %.2f (%s)" % (amount, new, method))
    commit()
    flash("Balance of %s is now %.2f." % (row["username"], new), "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("admin_view", aid=aid))


@route("/admins/<int:aid>/password", methods=["POST"])
@_users_gate("usr.password")
def admin_password(aid):
    row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    new = request.form.get("password", "")
    if len(new) < 8:
        flash("Password must be at least 8 characters.", "error")
    else:
        x("UPDATE panel_admins SET password_hash = %s WHERE id = %s", (generate_password_hash(new), aid))
        audit("admin_password_set", row["username"])
        commit()
        flash("Password changed for %s." % row["username"], "success")
    return redirect(url_for("admin_view", aid=aid))


@route("/admins/<int:aid>/reset-2fa", methods=["POST"])
@admin_required
def admin_reset_2fa(aid):
    row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    x("UPDATE panel_admins SET totp_secret = '', totp_enabled = 0, totp_backup_codes = NULL, totp_last_step = 0 WHERE id = %s",
      (aid,))
    audit("admin_2fa_reset", row["username"])
    commit()
    flash("Two-factor reset for %s. They sign in with password only until they set it up again." % row["username"], "success")
    return redirect(url_for("admin_view", aid=aid))


@route("/admins/save", methods=["POST"])
@_users_gate("usr.add", "usr.edit")
def admin_save():
    f = request.form
    aid = int(f["id"]) if f.get("id", "").isdigit() else 0
    row0 = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404) if aid else None
    if _staff():
        if not core.can_fn("usr.edit" if aid else "usr.add"):
            abort(403)
        if row0:
            _reach_or_403(row0)
    username, name = f.get("username", "").strip(), f.get("full_name", "").strip()[:128]
    role = f.get("role") if f.get("role") in dict(core.role_list()) else "support"
    if _staff():
        if row0 and role != row0["role"] and not (core.can_fn("usr.role") or core.can_fn("usr.ptype")):
            role = row0["role"]                   # Edit without Change Role keeps the role
        if not _within_mine(role):
            flash("Not saved. You cannot give the role %s: it can do things your own role cannot." % dict(core.role_list()).get(role, role), "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    if role in core.tiers_off():
        flash("%s is switched off in Settings > Partner Management." % core.tier_name(role), "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    phone, email, password = f.get("phone", "").strip(), f.get("email", "").strip(), f.get("password", "")
    if row0 and _staff() and not core.can_fn("usr.password"):
        password = ""                             # Edit without Change Password keeps the password
    identity = f.get("identity", "").strip()[:64]
    country, province, city = (f.get(k, "").strip()[:64] for k in ("country", "province", "city"))
    address = f.get("address", "").strip()[:255]
    is_active = 1 if f.get("is_active") else 0
    sms_on, email_on = (1 if f.get(k) else 0 for k in ("sms_on", "email_on"))
    if not aid and (not identity or not name or not phone or not email):
        flash("Full name, identity, phone and email are required.", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    if aid == g.me["id"] and not is_active:
        flash("You cannot disable your own profile.", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    if not PHONE_RE.match(phone) or (email and (not EMAIL_RE.match(email) or len(email) > 128)):
        flash("Check the phone number and email address.", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    if phone and core.cfg().get("user_dup_phone") != "1" and q(
            "SELECT 1 AS x FROM panel_admins WHERE phone = %s AND id <> %s LIMIT 1", (phone, aid), one=True):
        flash("Another user already has this phone number.", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))

    def ref(field, table):
        value = f.get(field, "")
        if value.isdigit() and q("SELECT 1 AS x FROM " + table + " WHERE id = %s", (int(value),), one=True):
            return int(value)
        return None
    dept, zone = ref("department_id", "departments"), ref("zone_id", "zones")
    parent = ref("parent_id", "panel_admins") if role in core.SELLER_ROLES else None
    if parent and parent == aid:
        parent = None
    cap, cap_ok = _limit_in(f.get("max_children"))
    if not cap_ok:
        flash("The user limit must be a whole number (leave it empty for no limit).", "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
    if role not in ("reseller", "subreseller"):
        cap = None
    elif _staff() and not core.can_fn({"reseller": "usr.subreslimit", "subreseller": "usr.retlimit"}[role]):
        cap = row0["max_children"] if row0 else None   # the limit needs its own tick
    if parent:
        old_parent = (q("SELECT parent_id FROM panel_admins WHERE id = %s", (aid,), one=True) or {}).get("parent_id") if aid else None
        full = _room_under(parent, skip_id=aid) if parent != old_parent else None
        if full:
            flash("Not saved. " + full, "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))

    if aid:
        row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
        if aid == g.me["id"] and role != "admin":
            flash("You cannot remove your own administrator role.", "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
        if password and len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
        x("UPDATE panel_admins SET full_name = %s, role = %s, phone = %s, email = %s, department_id = %s, "
          "zone_id = %s, parent_id = %s, is_active = %s, sms_on = %s, email_on = %s, identity = %s, country = %s, "
          "province = %s, city = %s, address = %s, max_children = %s WHERE id = %s",
          (name, role, phone, email, dept, zone, parent, is_active, sms_on, email_on, identity, country, province,
           city, address, cap, aid))
        if password:
            x("UPDATE panel_admins SET password_hash = %s WHERE id = %s", (generate_password_hash(password), aid))
        audit("admin_updated", row["username"])
    else:
        if not USERNAME_RE.match(username) or len(password) < 8:
            flash("Username (3-64 characters) and a password of at least 8 characters are required.", "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
        if q("SELECT 1 AS x FROM panel_admins WHERE username = %s", (username,), one=True):
            flash("That username is taken.", "error")
            return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))
        aid = insert("INSERT INTO panel_admins (username, full_name, password_hash, role, phone, email, "
                     "department_id, zone_id, parent_id, created_by, is_active, sms_on, email_on, identity, country, "
                     "province, city, address, max_children) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (username, name or username, generate_password_hash(password), role, phone, email, dept, zone,
                      parent, g.me["username"], is_active, sms_on, email_on, identity, country, province, city, address, cap))
        audit("admin_created", username)
    if "max_subs" in f and core.can_fn("usr.sublimit"):       # Subscriber Limitation
        subs_cap, subs_ok = _limit_in(f.get("max_subs"))
        if subs_ok:
            x("UPDATE panel_admins SET max_subs = %s WHERE id = %s", (subs_cap if role in core.SELLER_ROLES else None, aid))
    if role == "admin":
        x("DELETE FROM user_plans WHERE user_id = %s", (aid,))
    commit()
    flash("Saved.", "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))


@route("/admins/role", methods=["POST"])
@_users_gate("usr.role", "usr.ptype")
def admin_role():
    f = request.form
    row = q("SELECT * FROM panel_admins WHERE id = %s", (int(f["user_id"]),), one=True) if f.get("user_id", "").isdigit() else None
    role = f.get("role", "")
    if not row or role not in dict(core.role_list()):
        abort(404)
    _reach_or_403(row)
    if not _within_mine(role):
        flash("You cannot give the role %s: it can do things your own role cannot." % dict(core.role_list())[role], "error")
    elif role in core.tiers_off():
        flash("%s is switched off in Settings > Partner Management." % core.tier_name(role), "error")
    elif row["id"] == g.me["id"] and role != "admin":
        flash("You cannot remove your own administrator role.", "error")
    else:
        x("UPDATE panel_admins SET role = %s WHERE id = %s", (role, row["id"]))
        if role not in core.SELLER_ROLES:
            x("UPDATE panel_admins SET parent_id = NULL WHERE id = %s", (row["id"],))
        audit("admin_role_changed", row["username"], "%s -> %s" % (row["role"], role))
        commit()
        flash("%s is now %s." % (row["username"], dict(core.role_list())[role]), "success")
    return redirect(core.safe_next(f.get("next")) or url_for("admins"))


@route("/admins/<int:aid>/toggle", methods=["POST"])
@_users_gate("usr.edit")
def admin_toggle(aid):
    row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    if aid == g.me["id"]:
        flash("You cannot disable your own account.", "error")
    else:
        x("UPDATE panel_admins SET is_active = 1 - is_active WHERE id = %s", (aid,))
        audit("admin_disabled" if row["is_active"] else "admin_enabled", row["username"])
        commit()
        flash("%s is now %s." % (row["username"], "disabled" if row["is_active"] else "active"), "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))


# ---- profile settings, photo, invoice settings, switch profile (buttons on the user profile) ----
PHOTO_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}


def _admin_photo(aid):
    d = os.path.join(core.DATA_DIR, "photos")
    if os.path.isdir(d):
        for n in os.listdir(d):
            if n.startswith("admin-%d." % aid):
                return os.path.join(d, n)
    return None


@route("/admins/<int:aid>/settings", methods=["POST"])
@_users_gate("usr.settings")
def admin_profile_settings(aid):
    row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    f = request.form
    active = 1 if f.get("status") == "active" else 0
    if aid == g.me["id"] and not active:
        flash("You cannot disable your own account.", "error")
        return redirect(url_for("admin_view", aid=aid))
    x("UPDATE panel_admins SET is_active = %s, sms_on = %s, email_on = %s WHERE id = %s",
      (active, 1 if f.get("sms_on") else 0, 1 if f.get("email_on") else 0, aid))
    audit("admin_profile_settings", row["username"], "active=%d" % active)
    commit()
    flash("Profile settings saved.", "success")
    return redirect(url_for("admin_view", aid=aid))


@route("/admins/<int:aid>/photo")
@_users_gate("usr.module", "usr.view")
def admin_photo(aid):
    f = _admin_photo(aid)
    if not f:
        abort(404)
    resp = send_file(f, mimetype="image/png" if f.endswith(".png") else "image/jpeg", max_age=3600)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _save_photo(aid, username):
    """Save or remove (form field "remove") a user's profile picture from the posted form; flashes the outcome."""
    old = _admin_photo(aid)
    if request.form.get("remove"):
        if old:
            os.remove(old)
        x("UPDATE panel_admins SET photo = '' WHERE id = %s", (aid,))
        audit("admin_photo_removed", username)
        commit()
        flash("Photo removed.", "success")
        return
    up = request.files.get("file")
    data = up.read(2 * 1024 * 1024 + 1) if up else b""
    ext = next((v for k, v in PHOTO_TYPES.items() if data.startswith(k)), None)
    if not data or len(data) > 2 * 1024 * 1024 or not ext:
        flash("Choose a PNG or JPG photo up to 2 MB.", "error")
        return
    d = os.path.join(core.DATA_DIR, "photos")
    os.makedirs(d, exist_ok=True)
    if old:
        os.remove(old)
    with open(os.path.join(d, "admin-%d.%s" % (aid, ext)), "wb") as fh:
        fh.write(data)
    x("UPDATE panel_admins SET photo = %s WHERE id = %s", (ext, aid))
    audit("admin_photo_saved", username)
    commit()
    flash("Photo saved.", "success")


@route("/admins/<int:aid>/photo/save", methods=["POST"])
@_users_gate("usr.photo")
def admin_photo_save(aid):
    row = q("SELECT id, username, role FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    _save_photo(aid, row["username"])
    return redirect(url_for("admin_view", aid=aid))


@route("/profile/photo")
def profile_photo():
    """The signed-in person's own picture (any role, sellers too): sidebar, top bar and My Profile."""
    f = _admin_photo(g.me["id"])
    if not f:
        abort(404)
    resp = send_file(f, mimetype="image/png" if f.endswith(".png") else "image/jpeg", max_age=3600)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@route("/profile/photo/save", methods=["POST"])
def profile_photo_save():
    """Everyone (admin, staff, reseller, sub-reseller, retailer) sets their own picture on My Profile."""
    if not core.can_fn("prof.photo"):          # My Profile > Change Photo
        abort(403)
    _save_photo(g.me["id"], g.me["username"])
    return redirect(url_for("profile"))


@route("/admins/<int:aid>/invoice-settings", methods=["POST"])
@_users_gate("usr.inv_settings")
def admin_invoice_settings(aid):
    row = q("SELECT id, username, role FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    f = request.form
    x("UPDATE panel_admins SET inv_name = %s, inv_address = %s, inv_phone = %s, inv_footer = %s WHERE id = %s",
      (f.get("inv_name", "").strip()[:100], f.get("inv_address", "").strip()[:255],
       f.get("inv_phone", "").strip()[:40], f.get("inv_footer", "").strip()[:255], aid))
    audit("admin_invoice_settings", row["username"])
    commit()
    flash("Invoice settings saved. Receipts for this user's subscribers use them.", "success")
    return redirect(url_for("admin_view", aid=aid))


@route("/admins/<int:aid>/switch", methods=["POST"])
@_users_gate("usr.switch")
def admin_switch(aid):
    row = q("SELECT id, username, role, is_active FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    if row["role"] == "admin" or not row["is_active"]:
        flash("You can only switch to an active non-admin user.", "error")
        return redirect(url_for("admin_view", aid=aid))
    session["real_uid"] = session.get("real_uid") or g.me["id"]
    audit("switch_profile", row["username"], "by %s" % g.me["username"])
    commit()
    session["uid"] = aid
    session["csrf"] = secrets.token_hex(16)
    flash("You are now signed in as %s." % row["username"], "success")
    return redirect(url_for("dashboard"))


@route("/switch-back", methods=["POST"])
def switch_back():
    real = session.get("real_uid")
    back = q("SELECT id, role, is_active FROM panel_admins WHERE id = %s", (real,), one=True) if real else None
    if not back or back["role"] in core.SELLER_ROLES or not back["is_active"]:   # an administrator, or staff with Switch Profile
        abort(403)
    session.pop("real_uid", None)
    session["uid"] = back["id"]
    session["csrf"] = secrets.token_hex(16)
    flash("Switched back to your own account.", "success")
    return redirect(url_for("admins") if back["role"] == "admin" else url_for("dashboard"))



@route("/admins/<int:aid>/delete", methods=["POST"])
@_users_gate("usr.delete")
def admin_delete(aid):
    row = q("SELECT * FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    _reach_or_403(row)
    if aid == g.me["id"]:
        flash("You cannot delete your own account.", "error")
    else:
        x("DELETE FROM user_plans WHERE user_id = %s", (aid,))
        x("UPDATE tickets SET assigned_to = NULL WHERE assigned_to = %s", (aid,))
        x("DELETE FROM panel_admins WHERE id = %s", (aid,))
        audit("admin_deleted", row["username"])
        commit()
        flash("%s was deleted." % row["username"], "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("admins"))


# ===================================================================== Roles and permissions
PERM_GROUPS = [  # (tab, icon, modules, note shown when the tab has nothing to switch)
    ("Home", "home", ("notices",), "The home page is open to everyone who can sign in."),
    ("My Profile", "user", (), "Everyone can open and change their own profile and password."),
    ("User", "users", (), "Creating and managing users, roles and permissions is for administrators only."),
    ("Subscriber", "users", ("subscribers", "online"), ""),
    ("Accounting", "card", ("accounting",), ""),
    ("Network", "server", ("network", "area", "zones"), ""),
    ("Inventory", "package", ("inventory",), ""),
    ("Ticket", "ticket", ("tickets",), ""),
    ("Note", "note", ("notes",), ""),
    ("Logs", "file", ("logs",), ""),
    ("Reports", "chart", ("reports",), ""),
    ("Documents", "folder", ("documents",), ""),
    ("Package", "layers", ("plans",), ""),
    ("SMS & Email", "mail", ("sms", "email"), ""),
]
ROLE_PARENT = {"admin": "", "manager": "admin", "support": "admin", "accountant": "admin", "technician": "admin", "collector": "admin",
               "reseller": "admin", "subreseller": "reseller", "retailer": "subreseller"}


def _role_type(role):
    return "Administrator" if role == "admin" else ("Partner" if role in core.SELLER_ROLES else "Staff")


@route("/roles")
@_users_gate("usr.role_module")
def roles():
    labels = dict(core.role_list())
    counts = {r["role"]: r["c"] for r in q("SELECT role, COUNT(*) AS c FROM panel_admins GROUP BY role")}
    custom = {r["role_key"]: r for r in core.custom_roles()}
    rows = [dict(role=k, name=label, type=_role_type(k), parent=labels.get(ROLE_PARENT.get(k, ""), "") if k not in custom else "Administrator",
                 users=counts.get(k, 0), custom=k in custom, by=custom[k]["created_by"] if k in custom else "System",
                 at=str(custom[k]["created_at"])[:16] if k in custom else "Built in") for k, label in core.role_list()]
    starts = [(k, l) for k, l in core.role_list() if k in core.staff_roles()]
    return render_template("roles.html", rows=rows, starts=starts)


def _role_key(name):
    base = "c_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "role")
    base = base[:28]
    taken = {k for k, _ in core.role_list()}
    key, n = base, 2
    while key in taken:
        key = "%s_%d" % (base[:28 - len(str(n))], n)
        n += 1
    return key


@route("/roles/add", methods=["POST"])
@_users_gate("usr.role_add", "usr.role_copy")
def role_add():
    src = request.form.get("start", "")
    if _staff() and not core.can_fn("usr.role_copy" if src else "usr.role_add"):
        abort(403)
    name = request.form.get("name", "").strip()
    if not 2 <= len(name) <= 40:
        flash("Give the role a name of 2 to 40 characters.", "error")
        return redirect(url_for("roles"))
    if name.lower() in {l.lower() for _, l in core.role_list()}:
        flash("A role with that name already exists.", "error")
        return redirect(url_for("roles"))
    key = _role_key(name)
    ticked, levels = set(), {}
    if src and src in core.staff_roles():                 # start from a copy of an existing office-staff role
        levels = core._load_perms(src)
        saved = {r["fkey"]: r["allowed"] for r in q("SELECT fkey, allowed FROM role_funcs WHERE role = %s", (src,))}
        ticked = {k for k in permcatalog.KEYS if (bool(saved[k]) if k in saved else permcatalog.default_on(k, levels, False, src))}
        if _staff():                                      # staff copy only what they have themselves
            ticked &= set(g.funcs or ())
            levels = permcatalog.module_levels(ticked)
    elif src:
        abort(400)
    x("INSERT INTO custom_roles (role_key, name, created_by) VALUES (%s, %s, %s)", (key, name, g.me["username"]))
    with get_db().cursor() as cur:
        cur.executemany("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, %s)",
                        [(key, k, 1 if k in ticked else 0) for k in permcatalog.KEYS])
        cur.executemany("INSERT INTO role_perms (role, module, level) VALUES (%s, %s, %s)",
                        [(key, m, levels.get(m, "none")) for m, _n in MODULES])
    audit("role_created", name, "copy of %s" % src if src else "empty")
    commit()
    flash("Role created. Tick what it may do, then assign it to users.", "success")
    return redirect(url_for("role_permissions", role=key))


def _custom_or_404(role):
    return q("SELECT role_key, name FROM custom_roles WHERE role_key = %s", (role,), one=True) or abort(404)


@route("/roles/<role>/rename", methods=["POST"])
@_users_gate("usr.role_edit")
def role_rename(role):
    row = _custom_or_404(role)
    name = request.form.get("name", "").strip()
    if not 2 <= len(name) <= 40:
        flash("Give the role a name of 2 to 40 characters.", "error")
    elif name.lower() in {l.lower() for k, l in core.role_list() if k != role}:
        flash("A role with that name already exists.", "error")
    else:
        x("UPDATE custom_roles SET name = %s WHERE role_key = %s", (name, role))
        audit("role_renamed", row["name"], "-> " + name)
        commit()
        flash("Role renamed.", "success")
    return redirect(url_for("roles"))


@route("/roles/<role>/delete", methods=["POST"])
@_users_gate("usr.role_delete")
def role_delete(role):
    row = _custom_or_404(role)
    n = q("SELECT COUNT(*) AS n FROM panel_admins WHERE role = %s", (role,), one=True)
    if n and int(list(n.values())[0]):
        flash("%s is still given to %s user(s). Change their role first." % (row["name"], list(n.values())[0]), "error")
    else:
        x("DELETE FROM role_funcs WHERE role = %s", (role,))
        x("DELETE FROM role_perms WHERE role = %s", (role,))
        x("DELETE FROM custom_roles WHERE role_key = %s", (role,))
        audit("role_deleted", row["name"])
        commit()
        flash("Role deleted.", "success")
    return redirect(url_for("roles"))


@route("/roles/<role>/permissions", methods=["GET", "POST"])
@_users_gate("usr.role_module", "usr.role_edit")
def role_permissions(role):
    labels = dict(core.role_list())
    if role not in labels or role == "admin":
        abort(404)
    mine = set(g.funcs or ()) if _staff() else set(permcatalog.KEYS)
    own_role = _staff() and role == g.me["role"]
    if request.method == "POST":
        if _staff() and (not core.can_fn("usr.role_edit") or own_role):
            abort(403)
        ticked = {k for k in permcatalog.KEYS if request.form.get(k)}
        if _staff():                              # a tick the editor does not have stays as it was
            perms0 = core.load_perms(role)
            saved0 = {r["fkey"]: r["allowed"] for r in q("SELECT fkey, allowed FROM role_funcs WHERE role = %s", (role,))}
            before = {k for k in permcatalog.KEYS if (bool(saved0[k]) if k in saved0 else
                                                       permcatalog.default_on(k, perms0, role in core.SELLER_ROLES, role))}
            ticked = (ticked & mine) | (before - mine)
        levels = permcatalog.module_levels(ticked)
        with get_db().cursor() as cur:
            cur.executemany("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, %s) "
                            "ON DUPLICATE KEY UPDATE allowed = VALUES(allowed)",
                            [(role, k, 1 if k in ticked else 0) for k in permcatalog.KEYS])
            cur.executemany("INSERT INTO role_perms (role, module, level) VALUES (%s, %s, %s) "
                            "ON DUPLICATE KEY UPDATE level = VALUES(level)",
                            [(role, key, levels.get(key, "none")) for key, _name in MODULES])
        audit("roles_saved", role)
        commit()
        flash("Permissions saved. They apply on each person's next page load.", "success")
        return redirect(url_for("role_permissions", role=role, tab=request.form.get("tab", "")))
    ptype = _ptype(role)
    perms = core.load_perms(role)
    saved = {r["fkey"]: r["allowed"] for r in q("SELECT fkey, allowed FROM role_funcs WHERE role = %s", (role,))}
    seller = role in core.SELLER_ROLES
    ticked = {k for k in permcatalog.KEYS if (bool(saved[k]) if k in saved else permcatalog.default_on(k, perms, seller, role))}
    if role in core.ROLE_LOCKS:          # what this role can never have shows unticked (saving it changes nothing)
        ticked = permcatalog.effective(ticked, perms) - permcatalog.ROLE_NEVER.get(role, frozenset())
    tabs = []
    for t in permcatalog.TABS:
        groups = [dict(g, title=("My Profile (%s)" % ptype) if g["title"].startswith("My Profile (") else g["title"])
                  for g in t["groups"]]
        tabs.append(dict(t, title=("My Profile (%s)" % ptype) if t["title"] == "My Profile" else t["title"], groups=groups))
    return render_template("role_permissions.html", role=role, label=labels[role], tabs=tabs, ticked=ticked,
                           tab=request.args.get("tab", ""),
                           locked=(set(permcatalog.KEYS) - mine) | permcatalog.role_locked_keys(role, core.ROLE_LOCKS.get(role, ())),
                           can_save=not _staff() or (core.can_fn("usr.role_edit") and not own_role), own_role=own_role)


# ===================================================================== Department / Zone
@route("/org", module="zones")
def org():
    depts = q("SELECT d.*, (SELECT COUNT(*) FROM panel_admins u WHERE u.department_id = d.id) AS users, "
              "(SELECT COUNT(*) FROM tickets t WHERE t.department_id = d.id AND t.status IN ('open', 'pending')) "
              "AS open_tickets FROM departments d ORDER BY d.name")
    zones = q("SELECT z.*, (SELECT COUNT(*) FROM areas a WHERE a.zone_id = z.id) AS areas, "
              "(SELECT COUNT(*) FROM panel_admins u WHERE u.zone_id = z.id) AS users, "
              "(SELECT COUNT(*) FROM net_devices nd WHERE nd.zone_id = z.id) AS devices "
              "FROM zones z ORDER BY z.name")
    return render_template("org.html", depts=depts, zones=zones, tab="zones" if request.args.get("tab") == "zones"
                           else "departments")


def _name_desc(form):
    name, desc = form.get("name", "").strip(), form.get("description", "").strip()[:255]
    return (name, desc) if 1 <= len(name) <= 64 else (None, desc)


def _save_named(table, label, endpoint_tab):
    f = request.form
    rid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, desc = _name_desc(f)
    if not name:
        flash("Enter a name (up to 64 characters).", "error")
        return redirect(url_for("org", tab=endpoint_tab))
    clash = q("SELECT id FROM " + table + " WHERE name = %s AND id <> %s", (name, rid), one=True)
    if clash:
        flash("A %s named %s already exists." % (label, name), "error")
        return redirect(url_for("org", tab=endpoint_tab))
    if rid:
        x("UPDATE " + table + " SET name = %s, description = %s WHERE id = %s", (name, desc, rid))
    else:
        insert("INSERT INTO " + table + " (name, description) VALUES (%s, %s)", (name, desc))
    audit(label + "_saved", name)
    commit()
    flash("%s saved." % label.capitalize(), "success")
    return redirect(url_for("org", tab=endpoint_tab))


@route("/org/department/save", module="zones", methods=["POST"])
def dept_save():
    return _save_named("departments", "department", "departments")


@route("/org/zone/save", module="zones", methods=["POST"])
def zone_save():
    return _save_named("zones", "zone", "zones")


@route("/org/department/<int:rid>/delete", module="zones", methods=["POST"])
def dept_delete(rid):
    row = q("SELECT name FROM departments WHERE id = %s", (rid,), one=True) or abort(404)
    x("UPDATE panel_admins SET department_id = NULL WHERE department_id = %s", (rid,))
    x("UPDATE tickets SET department_id = NULL WHERE department_id = %s", (rid,))
    x("DELETE FROM departments WHERE id = %s", (rid,))
    audit("department_deleted", row["name"])
    commit()
    flash("Department %s deleted." % row["name"], "success")
    return redirect(url_for("org", tab="departments"))


@route("/org/zone/<int:rid>/delete", module="zones", methods=["POST"])
def zone_delete(rid):
    row = q("SELECT name FROM zones WHERE id = %s", (rid,), one=True) or abort(404)
    for table in ("panel_admins", "areas", "net_devices"):
        x("UPDATE " + table + " SET zone_id = NULL WHERE zone_id = %s", (rid,))
    x("DELETE FROM zones WHERE id = %s", (rid,))
    audit("zone_deleted", row["name"])
    commit()
    flash("Zone %s deleted." % row["name"], "success")
    return redirect(url_for("org", tab="zones"))


# ===================================================================== Areas
@route("/areas", module="area")
def areas():
    rows = q("SELECT a.id, a.name, a.note, a.zone_id, z.name AS zone_name, COUNT(s.id) AS total, "
             "COALESCE(SUM(s.status = 'suspended'), 0) AS suspended, "
             "COALESCE(SUM(s.status = 'active' AND s.expiry_date < CURDATE()), 0) AS expired "
             "FROM areas a LEFT JOIN zones z ON z.id = a.zone_id LEFT JOIN subscribers s ON s.area = a.name "
             "GROUP BY a.id, a.name, a.note, a.zone_id, z.name ORDER BY a.name")
    unassigned = q("SELECT COUNT(*) AS c FROM subscribers WHERE area = ''", one=True)["c"]
    zones = q("SELECT id, name FROM zones ORDER BY name")
    return render_template("areas.html", rows=rows, zones=zones, unassigned=unassigned)


@route("/areas/save", module="area", methods=["POST"])
def area_save():
    f = request.form
    rid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, note = f.get("name", "").strip(), f.get("note", "").strip()[:255]
    zone = int(f["zone_id"]) if f.get("zone_id", "").isdigit() else None
    if not 1 <= len(name) <= 64:
        flash("Enter an area name (up to 64 characters).", "error")
        return redirect(url_for("areas"))
    if q("SELECT id FROM areas WHERE name = %s AND id <> %s", (name, rid), one=True):
        flash("An area named %s already exists." % name, "error")
        return redirect(url_for("areas"))
    if rid:
        old = q("SELECT name FROM areas WHERE id = %s", (rid,), one=True) or abort(404)
        x("UPDATE areas SET name = %s, note = %s, zone_id = %s WHERE id = %s", (name, note, zone, rid))
        if old["name"] != name:
            x("UPDATE subscribers SET area = %s WHERE area = %s", (name, old["name"]))
    else:
        insert("INSERT INTO areas (name, note, zone_id) VALUES (%s, %s, %s)", (name, note, zone))
    audit("area_saved", name)
    commit()
    flash("Area saved.", "success")
    return redirect(url_for("areas"))


@route("/areas/<int:rid>/delete", module="area", methods=["POST"])
def area_delete(rid):
    row = q("SELECT name FROM areas WHERE id = %s", (rid,), one=True) or abort(404)
    x("DELETE FROM areas WHERE id = %s", (rid,))
    audit("area_deleted", row["name"])
    commit()
    flash("Area %s removed from the list. Customers keep their address text." % row["name"], "success")
    return redirect(url_for("areas"))


@route("/areas/report", module="area")
def area_report():
    start, end = core.date_range(default_days=(date.today().day - 1))
    span = (start, end)
    rows = q("SELECT s.area AS name, COUNT(*) AS total, "
             "COALESCE(SUM(s.status = 'active' AND (s.expiry_date IS NULL OR s.expiry_date >= CURDATE())), 0) AS active, "
             "COALESCE(SUM(s.status = 'active' AND s.expiry_date < CURDATE()), 0) AS expired, "
             "COALESCE(SUM(s.status = 'suspended'), 0) AS suspended FROM subscribers s "
             "GROUP BY s.area ORDER BY total DESC")
    online = {r["area"]: r["c"] for r in q(
        "SELECT s.area, COUNT(DISTINCT ra.username) AS c FROM radacct ra JOIN subscribers s "
        "ON s.username = ra.username WHERE ra.acctstoptime IS NULL GROUP BY s.area")}
    paid = {r["area"]: r for r in q(
        "SELECT s.area, COUNT(*) AS payments, COALESCE(SUM(p.amount), 0) AS amount FROM payments p "
        "JOIN subscribers s ON s.id = p.subscriber_id WHERE p.paid_at >= %s AND p.paid_at < %s + INTERVAL 1 DAY "
        "GROUP BY s.area", span)}
    for r in rows:
        r["label"] = r["name"] or "(no area)"
        r["online"] = online.get(r["name"], 0)
        r["paid"] = paid.get(r["name"]) or {"payments": 0, "amount": 0}
    return render_template("area_report.html", rows=rows, start=start, end=end)
