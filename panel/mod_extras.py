"""Features behind permission ticks that used to be saved but did nothing (Users > Roles > Set Permission).

  Transfer Subscribers   (usr.transfer)    Subscribers list > tick > "Transfer to" another reseller / the office
  Login Link             (sub.login_link)  subscriber page > a one-time link that signs the customer in to the portal
  Revert Last Invoice    (sub.revert)      subscriber page > undo the last renewal (expiry, package, balances)
  Exports                (log.*_x, doc.export, itm.export, cfc.export, log.pgw_x)   CSV of those pages
  Invoice mass actions   (inv.mark_paid, inv.mass_status, inv.mass_delete)          Extra Service list > tick
  User Packages > Delete (usr.pkg_delete)  remove one package from a user
  Subscriber Limitation  (usr.sublimit)    the most customers a reseller may have (panel_admins.max_subs)
  PGW Transactions       (log.pgw)         the payment gateway (bKash) call log, Logs > Payment gateway
"""
import csv
import hashlib
import io
import secrets
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, session, url_for

import core
import radius
from core import audit, commit, get_db, insert, q, route, x

REVERT_HOURS = 48           # a renewal can be reverted for this long after it was made
LINK_HOURS = 24             # a login link works this long (and only once)


def _seller():
    return g.me["role"] in core.SELLER_ROLES


def _csv(name, header, rows):
    """A CSV download (Excel-friendly BOM); cells that would start a formula are defused."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else ("" if v is None else v) for v in r])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s-%s.csv" % (name, date.today().isoformat())})


# ===================================================================== Subscriber Limitation
def room_for(username, adding=1):
    """'' when this owner may take `adding` more customers, otherwise why not (their Subscriber limit)."""
    r = q("SELECT username, role, max_subs FROM panel_admins WHERE username = %s", (username,), one=True)
    if not r or r["role"] not in core.SELLER_ROLES or r.get("max_subs") is None:
        return ""
    have = q("SELECT COUNT(*) AS c FROM subscribers WHERE created_by = %s", (username,), one=True)["c"]
    if have + adding > int(r["max_subs"]):
        return "%s may have at most %d customer%s (has %d)." % (username, int(r["max_subs"]), "" if r["max_subs"] == 1 else "s", have)
    return ""


# ===================================================================== Transfer Subscribers
def transfer_targets():
    """Who customers can be moved to: the office (administrator) and every reseller; a seller, their own downline."""
    if not core.can_fn("usr.transfer"):
        return []
    names = core.my_downline()
    if names is None:
        return q("SELECT username, full_name, role FROM panel_admins WHERE is_active = 1 AND role IN "
                 "('admin', 'reseller', 'subreseller', 'retailer') ORDER BY role = 'admin' DESC, role, username")
    return q("SELECT username, full_name, role FROM panel_admins WHERE is_active = 1 AND username IN (%s) ORDER BY role, username"
             % ",".join(["%s"] * len(names)), list(names))


def transfer(subs, to_user):
    """Move these customers (already limited to the signed-in person's own) to another owner. Returns (moved, error)."""
    if not core.can_fn("usr.transfer"):
        abort(403)
    target = next((t for t in transfer_targets() if t["username"] == to_user), None)
    if not target:
        return 0, "Choose who gets the customers."
    moving = [s for s in subs if s["created_by"] != to_user]
    if not moving:
        return 0, "The selected customers already belong to %s." % to_user
    full = room_for(to_user, len(moving))
    if full:
        return 0, "Not moved. " + full
    seller = to_user if target["role"] in core.SELLER_ROLES else ""
    for s in moving:
        x("UPDATE subscribers SET created_by = %s WHERE id = %s", (to_user, s["id"]))
        # open tickets follow the customer to the new reseller
        x("UPDATE tickets SET seller = %s WHERE subscriber_id = %s AND status IN ('open', 'pending')", (seller, s["id"]))
    audit("subscribers_transferred", to_user, ", ".join(s["username"] for s in moving)[:250])
    return len(moving), ""


# ===================================================================== Login Link
@route("/subscribers/<int:sid>/login-link", module="subscribers", methods=["POST"])
def subscriber_login_link(sid):
    sub = q("SELECT id, username, full_name, phone, sms_on, expiry_date, created_by FROM subscribers WHERE id = %s", (sid,),
            one=True) or abort(404)
    s = core.cfg()
    if s.get("portal_on") != "1":
        flash("The customer portal is switched off (Settings > Subscriber > Customer Portal), so a login link would not work.", "error")
        return redirect(url_for("subscriber_view", sid=sid))
    token = secrets.token_urlsafe(24)
    x("INSERT INTO portal_login_links (subscriber_id, token_hash, expires_at, created_by) VALUES (%s, %s, %s, %s)",
      (sid, hashlib.sha256(token.encode()).hexdigest(), datetime.now() + timedelta(hours=LINK_HOURS), g.me["username"]))
    link = url_for("portal_link_login", token=token, _external=True)
    sent = 0
    if request.form.get("sms") == "1" and core.can("sms", "edit"):
        import msglib
        addr = msglib.normalize_phone(sub["phone"], s.get("sms_country_code", "880"), s.get("sms_number_format", "intl"))
        if addr:
            with get_db().cursor() as cur:
                sent = msglib.enqueue(cur, "sms", addr, "%s: open your account (works once, for %d hours): %s" % (
                    s.get("isp_name") or "Your ISP", LINK_HOURS, link), subscriber_id=sid)
    audit("portal_link_created", sub["username"], "sms" if sent else "")
    commit()
    return render_template("login_link.html", sub=sub, link=link, hours=LINK_HOURS, sent=sent)


@route("/portal/link/<token>")
def portal_link_login(token):
    """The customer opens the link: signed in to the portal once, without the password."""
    import mod_portal
    mod_portal._on()
    h = hashlib.sha256((token or "").encode()).hexdigest()
    row = q("SELECT * FROM portal_login_links WHERE token_hash = %s", (h,), one=True)
    if not row or row["used_at"] or row["expires_at"] < datetime.now():
        flash("এই লিংকটি আর কাজ করে না (একবারই ব্যবহার করা যায়, সময়ও শেষ হতে পারে)। Username আর password দিয়ে লগইন করুন।", "error")
        return redirect(url_for("portal_login"))
    with get_db().cursor() as cur:            # used once: two clicks at the same moment cannot both sign in
        cur.execute("UPDATE portal_login_links SET used_at = NOW() WHERE id = %s AND used_at IS NULL", (row["id"],))
        first = cur.rowcount == 1
    sub = q("SELECT id, username FROM subscribers WHERE id = %s", (row["subscriber_id"],), one=True)
    if not sub or not first:
        commit()
        return redirect(url_for("portal_login"))
    session["cid"] = sub["id"]
    session["cpw"] = mod_portal.pw_mark(sub["username"])
    audit("portal_link_used", sub["username"], who=sub["username"])
    commit()
    return redirect(url_for("portal"))


# ===================================================================== Revert Last Invoice
def revertable(sid):
    """The customer's last renewal when it can still be undone, else None (with the reason in ['why'])."""
    p = q("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (sid,), one=True)
    if not p or not p["days_added"]:
        return None
    sub = q("SELECT expiry_date FROM subscribers WHERE id = %s", (sid,), one=True)
    if p["prev_status"] is None:
        return dict(p, why="made before this feature existed")
    if p["paid_at"] < datetime.now() - timedelta(hours=REVERT_HOURS):
        return dict(p, why="older than %d hours" % REVERT_HOURS)
    if not sub or sub["expiry_date"] != p["period_to"]:
        return dict(p, why="the expiry was changed after it")
    return dict(p, why="")


def undo_renewal(sub, p):
    """Undo renewal payment p (from revertable()): expiry, status and package back, money it took from the balance back,
    the reseller's cost refunded, the payment row removed, RADIUS rewritten. The caller commits.
    Returns (back to the balance, refunded to the reseller); ValueError when the balance it added was already spent."""
    fb = float(p["from_balance"] or 0)
    new_bal = round(float(sub["balance"] or 0) + fb, 2)
    if new_bal < 0:
        raise ValueError("Not reverted: the money this renewal added to the customer's balance (%.2f) has already been spent." % -fb)
    plan_id = p["prev_plan_id"] or sub["plan_id"]
    x("UPDATE subscribers SET expiry_date = %s, expiry_time = %s, status = %s, plan_id = %s, balance = %s WHERE id = %s",
      (p["prev_expiry"], p["prev_expiry_time"], p["prev_status"] or "active", plan_id, new_bal, sub["id"]))
    if fb:
        insert("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
               (sub["id"], fb, new_bal, "Reverted renewal #%d" % p["id"], g.me["username"]))
    refunded = 0.0
    if p["payer_id"] and p["cost"]:
        refunded = float(p["cost"])
        with get_db().cursor() as cur:
            cur.execute("UPDATE panel_admins SET balance = balance + %s WHERE id = %s", (refunded, p["payer_id"]))
            cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (p["payer_id"],))
            after = cur.fetchone()["balance"]
            cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                        "VALUES (%s, %s, %s, 'activation', %s, %s)",
                        (p["payer_id"], refunded, after, ("Reverted renewal of %s" % sub["username"])[:255], g.me["username"]))
    x("DELETE FROM payments WHERE id = %s", (p["id"],))
    plan = q("SELECT * FROM plans WHERE id = %s", (plan_id,), one=True)
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], plan, p["prev_status"] or "active", p["prev_expiry"], sub["static_ip"])
        if p["prev_expiry"] and p["prev_expiry"] < date.today():
            try:                     # back to expired: the live session drops to the expired policy / is refused
                cur.execute("INSERT INTO coa_queue (username, reason, created_by) VALUES (%s, 'reverted', %s)",
                            (sub["username"], g.me["username"][:64]))
            except Exception:
                pass
    audit("renewal_reverted", sub["username"], "payment #%d %.2f, expiry back to %s%s" % (
        p["id"], float(p["amount"] or 0), p["prev_expiry"], ", refunded %.2f to the reseller" % refunded if refunded else ""))
    return fb, refunded


@route("/subscribers/<int:sid>/revert", module="subscribers", methods=["POST"])
def subscriber_revert(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    p = revertable(sid)
    back = redirect(url_for("subscriber_view", sid=sid))
    if not p or p["why"]:
        flash("Nothing to revert%s." % ((": the last renewal was " + p["why"]) if p else ""), "error")
        return back
    if str(p["id"]) != request.form.get("payment_id", ""):
        flash("The last renewal changed meanwhile. Reload the page and try again.", "error")
        return back
    try:
        fb, refunded = undo_renewal(sub, p)
    except ValueError as e:
        flash(str(e), "error")
        return back
    commit()
    flash("Renewal reverted: expiry back to %s%s%s." % (
        p["prev_expiry"].strftime("%d %b %Y") if p["prev_expiry"] else "none",
        ", %.2f back to the customer's balance" % fb if fb > 0 else "", ", %.2f back to the reseller" % refunded if refunded else ""),
        "success")
    return back


# ===================================================================== exports
@route("/logs/export.csv", module="logs")
def logs_export():
    if _seller():
        abort(403)
    tab = "activity" if request.args.get("tab") == "activity" else "auth"
    if not core.can_fn("log.act_x" if tab == "activity" else "log.login_x"):
        abort(403)
    if tab == "activity":
        rows = q("SELECT created_at, admin, action, target, detail, ip FROM audit_log ORDER BY id DESC LIMIT 20000")
        return _csv("panel-activity", ["time", "who", "action", "target", "detail", "ip"],
                    [(r["created_at"], r["admin"], r["action"], r["target"], r["detail"], r["ip"]) for r in rows])
    rows = q("SELECT authdate, username, reply FROM radpostauth ORDER BY id DESC LIMIT 20000")
    return _csv("login-attempts", ["time", "username", "result"], [(r["authdate"], r["username"], r["reply"]) for r in rows])


@route("/logs/sessions.csv", module="logs")
def logs_sessions_export():
    if _seller() or not core.can_fn("log.sess_x"):
        abort(403)
    text = request.args.get("q", "").strip()
    rows = q("SELECT username, acctstarttime, acctstoptime, acctsessiontime, acctinputoctets, acctoutputoctets, framedipaddress, "
             "callingstationid, nasipaddress, acctterminatecause FROM radacct" + (" WHERE username LIKE %s" if text else "") +
             " ORDER BY radacctid DESC LIMIT 20000", (text + "%",) if text else ())
    return _csv("sessions", ["username", "start", "stop", "seconds", "upload_bytes", "download_bytes", "ip", "mac", "nas", "end_cause"],
                [(r["username"], r["acctstarttime"], r["acctstoptime"], r["acctsessiontime"], r["acctinputoctets"],
                  r["acctoutputoctets"], r["framedipaddress"], r["callingstationid"], r["nasipaddress"], r["acctterminatecause"])
                 for r in rows])


@route("/documents/export.csv", module="documents")
def documents_export():
    if not core.can_fn("doc.export"):
        abort(403)
    names = core.my_downline()
    rows = q("SELECT d.*, s.username FROM documents d LEFT JOIN subscribers s ON s.id = d.subscriber_id" +
             ("" if names is None else " WHERE s.created_by IN (%s)" % ",".join(["%s"] * len(names))) +
             " ORDER BY d.id DESC LIMIT 20000", [] if names is None else list(names))
    return _csv("documents", ["id", "title", "category", "customer", "file", "uploaded_by", "uploaded_at"],
                [(r.get("id"), r.get("title"), r.get("category"), r.get("username"), r.get("filename"),
                  r.get("uploaded_by") or r.get("created_by"), r.get("created_at")) for r in rows])


@route("/inventory/export.csv", module="inventory")
def inventory_export():
    if not core.can_fn("itm.export"):
        abort(403)
    rows = q("SELECT * FROM inventory_items ORDER BY category, name")
    return _csv("inventory", ["name", "category", "sku", "unit", "qty", "min_qty", "unit_cost", "location", "note"],
                [(r["name"], r["category"], r["sku"], r["unit"], r["qty"], r["min_qty"], r["unit_cost"], r["location"], r["note"])
                 for r in rows])


@route("/cashflow/categories.csv", module="accounting")
def cashflow_categories_export():
    if _seller() or not core.can_fn("cfc.export"):
        abort(403)
    rows = q("SELECT kind, name, note, created_by, created_at FROM cashflow_categories ORDER BY kind DESC, name")
    return _csv("cashflow-categories", ["kind", "name", "note", "created_by", "created_at"],
                [(r["kind"], r["name"], r["note"], r["created_by"], r["created_at"]) for r in rows])


# ===================================================================== PGW Transactions
@route("/logs/payments", module="logs")
def pgw_log():
    """Every call the panel made to the payment gateway (bKash): flow, step, answer and time."""
    if _seller() or not core.can_fn("log.pgw"):
        abort(403)
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(payment_id LIKE %s OR CAST(ref_id AS CHAR) = %s OR step LIKE %s)")
        params += [text + "%", text, text + "%"]
    if request.args.get("ok") in ("0", "1"):
        where.append("ok = %s")
        params.append(int(request.args["ok"]))
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        rows = q("SELECT created_at, flow, ref_id, payment_id, step, http_status, ms, ok, error FROM gateway_logs" + cond +
                 " ORDER BY id DESC LIMIT %s", params + [5000 if request.args.get("csv") == "1" else 300])
    except Exception:
        rows = []
    if request.args.get("csv") == "1":
        if not core.can_fn("log.pgw_x"):
            abort(403)
        return _csv("payment-gateway", ["time", "flow", "ref", "payment_id", "step", "http", "ms", "ok", "error"],
                    [(r["created_at"], r["flow"], r["ref_id"], r["payment_id"], r["step"], r["http_status"], r["ms"],
                      r["ok"], r["error"]) for r in rows])
    return render_template("pgw_log.html", rows=rows)


# ===================================================================== invoice (extra charge) mass actions
@route("/subscribers/charges/bulk", module="subscribers", methods=["POST"])
def charges_bulk():
    act = request.form.get("action", "")
    need = {"paid": "inv.mark_paid", "unpaid": "inv.mass_status", "delete": "inv.mass_delete"}.get(act)
    back = redirect(core.safe_next(request.form.get("next")) or url_for("charges"))
    if not need:
        flash("Pick what to do with the ticked charges.", "warn")
        return back
    if not core.can_fn(need):
        abort(403)
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()][:1000]
    if not ids:
        flash("Tick one or more charges first.", "warn")
        return back
    names = core.my_downline()
    rows = q("SELECT c.*, s.username, s.created_by FROM sub_charges c JOIN subscribers s ON s.id = c.subscriber_id WHERE c.id IN (%s)"
             % ",".join(["%s"] * len(ids)), ids)
    if names is not None:
        rows = [r for r in rows if r["created_by"] in names]
    n = 0
    for r in rows:
        if act == "paid" and not r["paid"]:
            x("UPDATE sub_charges SET paid = 1, paid_at = NOW() WHERE id = %s", (r["id"],))
            insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, received_by) "
                   "VALUES (%s, %s, %s, 0, 'cash', %s, %s)", (r["subscriber_id"], r["username"], r["amount"],
                                                            "Extra fee: " + r["name"], g.me["username"]))
            n += 1
        elif act == "unpaid" and r["paid"]:
            x("UPDATE sub_charges SET paid = 0, paid_at = NULL WHERE id = %s", (r["id"],))
            # the payment that marking it paid wrote (same customer, amount and note) goes too
            pay = q("SELECT id FROM payments WHERE subscriber_id = %s AND amount = %s AND note = %s AND days_added = 0 "
                    "ORDER BY id DESC LIMIT 1", (r["subscriber_id"], r["amount"], "Extra fee: " + r["name"]), one=True)
            if pay:
                x("DELETE FROM payments WHERE id = %s", (pay["id"],))
            n += 1
        elif act == "delete":
            x("DELETE FROM sub_charges WHERE id = %s", (r["id"],))
            n += 1
    audit("charges_bulk", act, "%d charge(s)" % n)
    commit()
    flash("%d charge%s %s." % (n, "" if n == 1 else "s", {"paid": "marked paid and recorded as payments",
                                                          "unpaid": "marked unpaid", "delete": "deleted"}[act]), "success")
    return back


# ===================================================================== User Packages > Delete
@route("/admins/<int:aid>/packages/<int:pid>/remove", methods=["POST"])
def admin_package_remove(aid, pid):
    if not core.can_fn("usr.pkg_delete"):
        abort(403)
    u = q("SELECT id, username, role, parent_id FROM panel_admins WHERE id = %s", (aid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and (u["username"] not in names or u["id"] == g.me["id"]):
        abort(403)
    used = q("SELECT COUNT(*) AS c FROM subscribers WHERE created_by = %s AND plan_id = %s", (u["username"], pid), one=True)["c"]
    plan = q("SELECT label, name FROM plans WHERE id = %s", (pid,), one=True) or abort(404)
    back = request.form.get("next") or url_for("admin_view", aid=aid)
    if used:
        flash("Not removed: %d of %s's customers use %s. Move them to another package first." % (used, u["username"], plan["label"] or plan["name"]), "error")
        return redirect(core.safe_next(back) or url_for("admin_view", aid=aid))
    x("DELETE FROM user_plans WHERE user_id = %s AND plan_id = %s", (aid, pid))
    audit("user_package_removed", u["username"], plan["name"])
    commit()
    flash("%s removed from %s's packages." % (plan["label"] or plan["name"], u["username"]), "success")
    return redirect(core.safe_next(back) or url_for("admin_view", aid=aid))
