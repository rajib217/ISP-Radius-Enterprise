"""Customer self-service portal: /portal.

Customers sign in with their PPPoE username and password and see their expiry, usage, sessions and invoices.
They can renew with bKash when the customer belongs to the ISP itself (created by an administrator or staff).
Customers of a reseller / sub-reseller / retailer can pay with bKash too: the money is credited to that seller's
balance and the seller's own package price is then taken from it, exactly as if the seller had activated the customer
(the seller keeps the difference as balance). See bkash.settle_portal.

The portal has its own session key ("cid"); it never gives access to anything in the admin panel."""
import secrets
from datetime import date, timedelta

from flask import abort, flash, g, redirect, render_template, request, session, url_for

import core
import bkash
import mod_subs
import mod_topup
import radius
from core import audit, commit, insert, q, route, x

LOGIN_TRIES = 8


def _on():
    """The portal is switched off (Settings > Subscriber > Customer Portal): a clear page instead of a bare 404."""
    if core.cfg().get("portal_on") != "1":
        from flask import Response
        s = core.cfg()
        staff = bool(session.get("uid"))
        abort(Response(render_template("portal_off.html", isp=s.get("isp_name", ""), staff=staff,
                                       phone=s.get("support_phone", "")), 404))


def _sub():
    """The signed-in customer (row with plan columns) or None."""
    sid = session.get("cid")
    if not sid:
        return None
    return sub_row(sid)


def sub_row(sid):
    """One customer with the plan columns pay_terms needs (also used by the expired notice page's bKash button)."""
    return q("SELECT s.*, p.label AS plan_label, p.name AS plan_name, p.price AS plan_price, p.validity_days, "
             "p.download_mbps, p.upload_mbps, p.quota_total_gb FROM subscribers s "
             "LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (sid,), one=True)


def pw_mark(username):
    """A short fingerprint of the customer's current password. The portal keeps a customer signed in until Sign out
    (app.PanelSessions); when the password is changed (by the office, or on another phone) every old sign-in ends."""
    import hashlib
    from flask import current_app
    pw = q("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password' LIMIT 1", (username,), one=True)
    if not pw:
        return ""
    return hashlib.sha256(("%s|%s" % (current_app.secret_key, pw["value"])).encode()).hexdigest()[:16]


def _sign_out():
    session.pop("cid", None)
    session.pop("cpw", None)


def _need():
    _on()
    sub = _sub()
    if not sub:
        _sign_out()
        return None
    mark = pw_mark(sub["username"])
    if "cpw" not in session:            # signed in before this check existed: start it now
        session["cpw"] = mark
    elif not mark or not secrets.compare_digest(session["cpw"], mark):
        _sign_out()
        return None
    return sub


def _owner(sub):
    return q("SELECT username, full_name, phone, role FROM panel_admins WHERE username = %s", (sub.get("created_by") or "",), one=True)


def pay_terms(sub):
    """What the customer may pay online: {'ok', 'amount', 'fee', 'days', 'why', 'seller'}."""
    s = core.cfg()
    out = {"ok": False, "amount": 0.0, "fee": 0.0, "days": int(sub.get("validity_days") or 0), "why": "", "seller": None}
    owner = _owner(sub)
    if owner and owner["role"] in core.SELLER_ROLES:
        out["seller"] = owner
    if s.get("sub_self_activation") != "1" or not bkash.configured():
        out["why"] = "Online payment is not available. Please contact us to renew."
        return out
    if not sub.get("plan_id") or not sub.get("plan_price") or not out["days"]:
        out["why"] = "Your package has no price set. Please contact us to renew."
        return out
    if sub["status"] == "suspended":
        out["why"] = "Your account is suspended. Please contact us."
        return out
    plan = mod_subs._plan(sub["plan_id"])
    out["amount"] = mod_subs.customer_price(sub, plan) if plan else round(float(sub["plan_price"]), 2)
    if out["amount"] <= 0:
        out["why"] = "Your package has no price set. Please contact us to renew."
        return out
    out["fee"] = bkash.fee_for(out["amount"])
    out["ok"] = True
    return out


# ===================================================================== sign in
@route("/portal/login", methods=["GET", "POST"])
def portal_login():
    _on()
    error = None
    if request.method == "POST":
        ip = request.remote_addr or ""
        username = request.form.get("username", "").strip()[:64]
        recent = q("SELECT COUNT(*) AS c FROM audit_log WHERE action = 'portal_login_failed' AND created_at > NOW() - INTERVAL 10 MINUTE "
                   "AND (ip = %s OR target = %s)", (ip, username), one=True)["c"]
        if recent >= LOGIN_TRIES:
            error = "Too many failed attempts. Try again in 10 minutes."
        else:
            sub = q("SELECT id, username FROM subscribers WHERE username = %s", (username,), one=True) if username else None
            pw = q("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password' LIMIT 1",
                   (username,), one=True) if sub else None
            given = request.form.get("password", "")
            if sub and pw and given and secrets.compare_digest(str(pw["value"]).encode(), given.encode()):
                session.pop("cid", None)
                session["cid"] = sub["id"]
                session["csrf"] = secrets.token_hex(16)
                session["cpw"] = pw_mark(sub["username"])
                session.permanent = True
                audit("portal_login", sub["username"], who=sub["username"])
                commit()
                return redirect(url_for("portal"))
            audit("portal_login_failed", username, who=username)
            commit()
            error = "Wrong username or password."
    return render_template("portal_login.html", error=error, isp=core.cfg().get("isp_name", ""))


@route("/portal/")
def portal_slash():
    """/portal/ (with the slash people often type) is the same page as /portal."""
    return redirect(url_for("portal"))


@route("/portal/logout", methods=["POST"])
def portal_logout():
    _sign_out()
    return redirect(url_for("portal_login"))


# ===================================================================== notifications (bell, list, live check)
@route("/portal/notifications")
def portal_notifications():
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    rows = q("SELECT id, kind, title, body, link, created_at, read_at FROM sub_notifications WHERE subscriber_id = %s "
             "ORDER BY id DESC LIMIT 100", (sub["id"],))
    # opening the list reads them all (the page still shows which were new this time)
    x("UPDATE sub_notifications SET read_at = NOW() WHERE subscriber_id = %s AND read_at IS NULL", (sub["id"],))
    commit()
    return render_template("portal_notifications.html", sub=sub, rows=rows, isp=core.cfg().get("isp_name", ""),
                           portal_unread=0)


@route("/portal/notifications/check")
def portal_notifications_check():
    """For the app: unread count and the newest notification (a toast pops up when a new one arrives)."""
    from flask import jsonify
    sid = session.get("cid")
    if not sid or core.cfg().get("portal_on") != "1":
        return jsonify({"unread": 0, "latest": None})
    n = q("SELECT COUNT(*) AS c FROM sub_notifications WHERE subscriber_id = %s AND read_at IS NULL", (sid,), one=True)["c"]
    latest = q("SELECT id, title, body FROM sub_notifications WHERE subscriber_id = %s AND read_at IS NULL "
               "ORDER BY id DESC LIMIT 1", (sid,), one=True)
    resp = jsonify({"unread": int(n), "latest": latest})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ===================================================================== Web Push (notifications with the app closed)
@route("/portal/push/key")
def portal_push_key():
    from flask import jsonify
    import webpush
    if not session.get("cid") or not webpush.available():
        return jsonify({"key": None})
    with core.get_db().cursor() as cur:
        _priv, pub = webpush.keys(cur)
    commit()
    return jsonify({"key": pub})


@route("/portal/push/subscribe", methods=["POST"])
def portal_push_subscribe():
    """The customer's browser allowed notifications: keep its push subscription (one row per browser / phone)."""
    from flask import jsonify
    import hashlib
    import webpush
    sub = _need()
    if not sub:
        return jsonify({"ok": False}), 401
    d = request.get_json(silent=True) or {}
    endpoint = (d.get("endpoint") or "").strip()
    keys = d.get("keys") or {}
    p256dh, auth = (keys.get("p256dh") or "").strip(), (keys.get("auth") or "").strip()
    if not webpush.endpoint_ok(endpoint) or not (40 <= len(p256dh) <= 255) or not (8 <= len(auth) <= 64):
        return jsonify({"ok": False, "error": "This browser's push address is not accepted."}), 400
    h = hashlib.sha1(endpoint.encode()).hexdigest()
    x("INSERT INTO push_subscriptions (subscriber_id, endpoint_hash, endpoint, p256dh, auth, user_agent) "
      "VALUES (%s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE subscriber_id = VALUES(subscriber_id), "
      "p256dh = VALUES(p256dh), auth = VALUES(auth), user_agent = VALUES(user_agent), fails = 0",
      (sub["id"], h, endpoint, p256dh, auth, (request.headers.get("User-Agent") or "")[:255]))
    audit("push_enabled", sub["username"], who=sub["username"])
    commit()
    return jsonify({"ok": True})


@route("/portal/push/unsubscribe", methods=["POST"])
def portal_push_unsubscribe():
    from flask import jsonify
    import hashlib
    sub = _need()
    if not sub:
        return jsonify({"ok": False}), 401
    endpoint = ((request.get_json(silent=True) or {}).get("endpoint") or "").strip()
    x("DELETE FROM push_subscriptions WHERE subscriber_id = %s AND endpoint_hash = %s",
      (sub["id"], hashlib.sha1(endpoint.encode()).hexdigest()))
    commit()
    return jsonify({"ok": True})


# ===================================================================== the customer's page
@route("/portal")
def portal():
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login")) if core.cfg().get("portal_on") == "1" else abort(404)
    s = core.cfg()
    today = date.today()
    left = (sub["expiry_date"] - today).days if sub["expiry_date"] else None
    state = ("suspended" if sub["status"] == "suspended" else
             "expired" if left is not None and left < 0 else
             "active")
    user = sub["username"]
    usage = sessions = None
    if s.get("sub_see_volume") == "1":
        month = q("SELECT COALESCE(SUM(acctinputoctets), 0) AS up, COALESCE(SUM(acctoutputoctets), 0) AS down, COUNT(*) AS n "
                  "FROM radacct WHERE username = %s AND acctstarttime >= %s", (user, today.replace(day=1)), one=True)
        start = today - timedelta(days=13)
        daily = {r["d"]: r for r in q("SELECT DATE(acctstarttime) AS d, SUM(acctinputoctets) AS up, SUM(acctoutputoctets) AS down "
                                      "FROM radacct WHERE username = %s AND acctstarttime >= %s GROUP BY DATE(acctstarttime)",
                                      (user, start))}
        days = [start + timedelta(days=i) for i in range(14)]
        vals = [core.n(daily.get(d, {}).get("down")) + core.n(daily.get(d, {}).get("up")) for d in days]
        top = max(vals) or 1
        used = core.n(month["up"]) + core.n(month["down"])
        quota = float(sub["quota_total_gb"] or 0)
        usage = {"up": core.n(month["up"]), "down": core.n(month["down"]), "n": core.n(month["n"]),
                 "bars": [{"day": d.strftime("%d %b"), "v": v, "h": max(2, int(v * 100 / top)) if v else 0} for d, v in zip(days, vals)],
                 "quota": quota, "pct": min(100, int(used * 100 / (quota * 1024 ** 3))) if quota else 0}
    online = q("SELECT acctstarttime, framedipaddress FROM radacct WHERE username = %s AND acctstoptime IS NULL "
               "ORDER BY acctstarttime DESC LIMIT 1", (user,), one=True)
    if s.get("sub_session_log") == "1":
        sessions = q("SELECT acctstarttime, acctstoptime, acctsessiontime, acctinputoctets, acctoutputoctets, framedipaddress "
                     "FROM radacct WHERE username = %s ORDER BY radacctid DESC LIMIT 10", (user,))
    payments = q("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 300", (sub["id"],))
    charges = q("SELECT * FROM sub_charges WHERE subscriber_id = %s ORDER BY id DESC LIMIT 300", (sub["id"],))
    balance_rows = q("SELECT id, amount, balance_after, note, created_by, created_at FROM sub_ledger WHERE subscriber_id = %s "
                     "ORDER BY id DESC LIMIT 300", (sub["id"],))
    pending = q("SELECT id, token, amount, fee, status, message, created_at FROM portal_payments WHERE subscriber_id = %s "
                "ORDER BY id DESC LIMIT 20", (sub["id"],))
    due = sum(float(c["amount"] or 0) for c in charges if not c["paid"])
    used_gb = ((usage["up"] + usage["down"]) / 1024 ** 3) if usage else None
    if used_gb is None:
        m = q("SELECT COALESCE(SUM(acctinputoctets + acctoutputoctets), 0) AS t FROM radacct WHERE username = %s "
              "AND acctstarttime >= %s", (user, today.replace(day=1)), one=True)
        used_gb = core.n(m["t"]) / 1024 ** 3
    uptime = None
    if online and online.get("acctstarttime"):
        from datetime import datetime
        uptime = max(0, int((datetime.now() - online["acctstarttime"]).total_seconds()))
    import mod_subview as sv
    # everything else the customer may see about their own connection
    last = q("SELECT acctstarttime, acctstoptime, framedipaddress, callingstationid FROM radacct WHERE username = %s "
             "ORDER BY radacctid DESC LIMIT 1", (user,), one=True)
    try:
        onu = q("SELECT rx_dbm, status, last_seen FROM onus WHERE username = %s ORDER BY last_seen DESC LIMIT 1",
                (user,), one=True)
    except Exception:                  # the OLT tables are optional
        onu = None
    plan = mod_subs._plan(sub["plan_id"]) if sub.get("plan_id") else None
    ends = mod_subs.end_moment(sub, plan)
    total_days = int(sub.get("validity_days") or 30)
    ring = 0 if state != "active" else (100 if left is None else max(0, min(100, int(round((left + 1) * 100 / total_days)))))
    nid = (sub.get("identity") or "").strip()
    terms = pay_terms(sub)
    import ticketlib
    with core.get_db().cursor() as _cur:
        outages = ticketlib.active_outages(_cur, sub.get("area") or "")
    info = {"ends": ends, "ring": ring, "last": last, "onu": onu, "plan": plan,
            "ip": (online or {}).get("framedipaddress") or sub.get("static_ip") or (last or {}).get("framedipaddress") or "",
            "mac": (last or {}).get("callingstationid") or "",
            "nid": ("•••• " + nid[-4:]) if len(nid) > 4 else nid,
            "next_bill": terms["amount"] or (mod_subs.customer_price(sub, plan) if plan else 0.0),
            "last_pay": payments[0] if payments else None,
            "id_ref": s.get("bksms_on") == "1"}
    return render_template("portal.html", sub=sub, state=state, left=left, usage=usage, online=online, sessions=sessions, info=info,
                           invoices=sv.invoices(payments, charges), ledger=sv.ledger(sub, payments, charges, balance_rows),
                           payments=payments, tries=pending, terms=terms, isp=s.get("isp_name", ""),
                           cur=s.get("currency", ""), can_pw=s.get("sub_reset_pw") == "1", due=due, used_gb=used_gb,
                           uptime=uptime, photo=_photo_v(sv._photo_path(sub["id"])), owner=_owner(sub),
                           phone=s.get("support_phone", ""), outages=outages)


@route("/portal/rate")
def portal_rate():
    """Live download / upload speed of the signed-in customer (JSON for the chart on the portal)."""
    import mod_subview
    sub = _need() or abort(404)
    return mod_subview.rate_response(sub["username"])


@route("/portal/photo/save", methods=["POST"])
def portal_photo_save():
    """The customer sets or removes their own profile photo."""
    from flask import jsonify
    import mod_subview
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    ajax = request.headers.get("X-Requested-With") == "fetch"
    if request.form.get("remove"):
        old = mod_subview._photo_path(sub["id"])
        if old:
            import os
            os.remove(old)
        x("UPDATE subscribers SET photo = '' WHERE id = %s", (sub["id"],))
        why = None
    else:
        up = request.files.get("file")
        why = mod_subview.save_photo(sub["id"], up.read(2 * 1024 * 1024 + 1) if up else b"")
    if not why:
        audit("portal_photo", sub["username"], who=sub["username"])
        commit()
    if ajax:
        return jsonify({"ok": not why, "error": why}), (400 if why else 200)
    flash(why or "Photo saved.", "error" if why else "success")
    return redirect(url_for("portal"))


def _photo_v(path):
    """Version number for the photo URL (its file time), so a new photo shows at once; 0 = no photo."""
    import os
    return int(os.path.getmtime(path)) if path else 0


@route("/portal/photo")
def portal_photo():
    """The signed-in customer's own profile photo (set by the office)."""
    from flask import send_file
    import mod_subview
    sub = _need() or abort(404)
    f = mod_subview._photo_path(sub["id"]) or abort(404)
    resp = send_file(f, mimetype="image/png" if f.endswith(".png") else "image/jpeg", max_age=3600)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@route("/portal/invoice/<int:pid>")
def portal_invoice(pid):
    sub = _need() or abort(404)
    p = q("SELECT p.*, pl.label AS plan_label FROM payments p LEFT JOIN subscribers s ON s.id = p.subscriber_id "
          "LEFT JOIN plans pl ON pl.id = s.plan_id WHERE p.id = %s AND p.subscriber_id = %s", (pid, sub["id"]), one=True) or abort(404)
    inv = q("SELECT inv_name, inv_address, inv_phone, inv_footer FROM panel_admins WHERE username = %s",
            (sub.get("created_by") or "",), one=True) or {}
    s = core.cfg()
    return render_template("portal_invoice.html", p=p, sub=sub, inv=inv, isp=s.get("isp_name", ""), cur=s.get("currency", ""))


@route("/portal/password", methods=["POST"])
def portal_password():
    sub = _need() or abort(404)
    if core.cfg().get("sub_reset_pw") != "1":
        abort(404)
    pw = q("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password' LIMIT 1", (sub["username"],), one=True)
    cur_pw, new, again = (request.form.get(k, "") for k in ("current", "new", "again"))
    if not pw or not secrets.compare_digest(str(pw["value"]).encode(), cur_pw.encode()):
        flash("Your current password is wrong.", "error")
    elif len(new) < 6 or len(new) > 64:
        flash("The new password must be 6 to 64 characters.", "error")
    elif new != again:
        flash("The two new passwords do not match.", "error")
    else:
        with core.get_db().cursor() as c:
            radius.sync_subscriber(c, sub["username"], mod_subs._plan(sub["plan_id"]), sub["status"], sub["expiry_date"],
                                   sub["static_ip"], password=new)
        audit("portal_password", sub["username"], who=sub["username"])
        session["cpw"] = pw_mark(sub["username"])      # this phone stays signed in; other sign-ins end
        commit()
        flash("Password changed. Use the new one on your router too.", "success")
    return redirect(url_for("portal") + "#account")


# ===================================================================== pay with bKash
def _preview_expiry(sub, days):
    """The expiry the customer gets if bKash confirms now (same rules as mod_subs.do_renew)."""
    base = max(sub["expiry_date"] or date.today(), date.today())
    plan = mod_subs._plan(sub["plan_id"])
    new = mod_subs.plan_expiry(plan, base, days, monthly=mod_subs.monthly_for(sub))
    if plan and plan.get("fixed_expire_day"):
        new = mod_subs.fixed_expiry(new, base, plan["fixed_expire_day"])
    return new


@route("/portal/pay", methods=["GET", "POST"])
def portal_pay():
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    t = pay_terms(sub)
    if not t["ok"]:
        flash(t["why"], "error")
        return redirect(url_for("portal"))
    s = core.cfg()
    if request.method == "GET":                     # the checkout page: what they pay, and until when
        return render_template("portal_pay.html", sub=sub, t=t, new_expiry=_preview_expiry(sub, t["days"]),
                               isp=s.get("isp_name", ""), cur=s.get("currency", ""), sandbox=s.get("bkash_mode") == "sandbox")
    token = secrets.token_hex(16)
    tid = insert("INSERT INTO portal_payments (subscriber_id, amount, fee, days, token) VALUES (%s, %s, %s, %s, %s)",
                 (sub["id"], t["amount"], t["fee"], t["days"], token))
    commit()
    try:
        data = bkash.create("portal", tid, t["amount"] + t["fee"], core.sub_code(sub["id"]),   # reference: the subscriber ID (RN05)
                            mod_topup.callback_url("portal_bkash", tid=tid, key=token), "PRT%d" % tid)
    except bkash.BkashError as e:
        x("UPDATE portal_payments SET status = 'failed', message = %s WHERE id = %s", (str(e)[:255], tid))
        commit()
        flash("The payment could not be started. Please try again later or contact us.", "error")
        return redirect(url_for("portal_pay"))
    x("UPDATE portal_payments SET payment_id = %s WHERE id = %s", (data["paymentID"], tid))
    commit()
    return redirect(data["bkashURL"])


def _payment(tid, key):
    """A portal payment found by its id and secret key (no portal sign-in needed: the customer's session may
    have ended while they were on the bKash page)."""
    _on()
    row = q("SELECT * FROM portal_payments WHERE id = %s", (tid,), one=True)
    if not row or not secrets.compare_digest(str(row["token"]), str(key)):
        abort(404)
    return row


@route("/portal/bkash/<int:tid>/<key>")
def portal_bkash(tid, key):
    row = _payment(tid, key)
    if row["status"] == "pending":
        status = request.args.get("status", "")
        if status != "success" or request.args.get("paymentID", "") != row["payment_id"]:
            bkash.mark("portal", row["id"], "cancelled" if status == "cancel" else "failed",
                       "bKash returned: %s" % (status or "no status"))
        else:
            bkash.check("portal", row)
    return redirect(url_for("portal_receipt", tid=tid, key=key))


@route("/portal/payment/<int:tid>/<key>")
def portal_receipt(tid, key):
    """Result of one bKash payment: receipt when paid, a 'confirming' page while bKash has not answered."""
    row = _payment(tid, key)
    if row["status"] == "pending" and row["payment_id"]:
        due = q("SELECT id FROM portal_payments WHERE id = %s AND status = 'pending' "
                "AND (checked_at IS NULL OR checked_at < NOW() - INTERVAL 15 SECOND)", (tid,), one=True)
        if due:                                      # at most one bKash check per 15 seconds per payment
            try:
                bkash.check("portal", row)
            except Exception:
                core.get_db().rollback()
            row = q("SELECT * FROM portal_payments WHERE id = %s", (tid,), one=True)
    sub = q("SELECT s.id, s.username, s.full_name, s.expiry_date, s.created_by, p.label AS plan_label, p.name AS plan_name "
            "FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (row["subscriber_id"],), one=True) or {}
    pay = q("SELECT id, period_to, paid_at FROM payments WHERE id = %s", (row["payment_ref"],), one=True) if row.get("payment_ref") else None
    inv = q("SELECT inv_name, inv_address, inv_phone, inv_footer FROM panel_admins WHERE username = %s",
            (sub.get("created_by") or "",), one=True) or {}
    s = core.cfg()
    return render_template("portal_receipt.html", row=row, sub=sub, pay=pay, inv=inv, signed_in=session.get("cid") == row["subscriber_id"],
                           isp=s.get("isp_name", ""), cur=s.get("currency", ""))
