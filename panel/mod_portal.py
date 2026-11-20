"""Customer self-service portal: /portal.

Customers sign in with their PPPoE username and password and see their expiry, usage, sessions and invoices.
They can renew with bKash when the customer belongs to the ISP itself (created by an administrator or staff).
Customers of a reseller / sub-reseller / retailer are asked to pay that seller, because the seller's own price and
balance rules decide that money; the portal never changes a seller's balance.

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
    if core.cfg().get("portal_on") != "1":
        abort(404)


def _sub():
    """The signed-in customer (row with plan columns) or None."""
    sid = session.get("cid")
    if not sid:
        return None
    return q("SELECT s.*, p.label AS plan_label, p.name AS plan_name, p.price AS plan_price, p.validity_days, "
             "p.download_mbps, p.upload_mbps, p.quota_total_gb FROM subscribers s "
             "LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (sid,), one=True)


def _need():
    _on()
    sub = _sub()
    if not sub:
        session.pop("cid", None)
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
        out["why"] = "Please pay your provider %s%s." % (owner["full_name"] or owner["username"],
                                                       (" (" + owner["phone"] + ")") if owner["phone"] else "")
        return out
    if s.get("sub_self_activation") != "1" or not bkash.configured():
        out["why"] = "Online payment is not available. Please contact us to renew."
        return out
    if not sub.get("plan_id") or not sub.get("plan_price") or not out["days"]:
        out["why"] = "Your package has no price set. Please contact us to renew."
        return out
    if sub["status"] == "suspended":
        out["why"] = "Your account is suspended. Please contact us."
        return out
    out["amount"] = round(float(sub["plan_price"]), 2)
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
                session.permanent = True
                audit("portal_login", sub["username"], who=sub["username"])
                commit()
                return redirect(url_for("portal"))
            audit("portal_login_failed", username, who=username)
            commit()
            error = "Wrong username or password."
    return render_template("portal_login.html", error=error, isp=core.cfg().get("isp_name", ""))


@route("/portal/logout", methods=["POST"])
def portal_logout():
    session.pop("cid", None)
    return redirect(url_for("portal_login"))


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
    invoices = q("SELECT id, amount, days_added, method, period_to, paid_at FROM payments WHERE subscriber_id = %s "
                 "ORDER BY id DESC LIMIT 50", (sub["id"],))
    pending = q("SELECT id, token, amount, fee, status, message, created_at FROM portal_payments WHERE subscriber_id = %s "
                "ORDER BY id DESC LIMIT 5", (sub["id"],))
    return render_template("portal.html", sub=sub, state=state, left=left, usage=usage, online=online, sessions=sessions,
                           invoices=invoices, tries=pending, terms=pay_terms(sub), isp=s.get("isp_name", ""),
                           cur=s.get("currency", ""), can_pw=s.get("sub_reset_pw") == "1")


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
        commit()
        flash("Password changed. Use the new one on your router too.", "success")
    return redirect(url_for("portal") + "#account")


# ===================================================================== pay with bKash
def _preview_expiry(sub, days):
    """The expiry the customer gets if bKash confirms now (same rules as mod_subs.do_renew)."""
    base = max(sub["expiry_date"] or date.today(), date.today())
    plan = mod_subs._plan(sub["plan_id"])
    new = mod_subs.plan_expiry(plan, base, days)
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
        data = bkash.create("portal", tid, t["amount"] + t["fee"], sub["username"],
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
