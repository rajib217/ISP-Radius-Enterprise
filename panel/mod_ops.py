"""Owner tools: the daily report page, the monthly statement of resellers / sub-resellers / retailers, and the
due reminder SMS from the Dues list.

  /reports/daily        Accounting > Daily report (the same numbers as the Telegram morning message)
  /statements           Users > Monthly statement (sellers: their own and their downline's rows), CSV export
  /dues/remind          Dues: send the "due" SMS to the ticked customers, or to everyone the filters show"""
import csv
import io
import re
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

import core
import dailyreport
import msglib
from core import audit, commit, get_db, q, route


# ===================================================================== daily report
@route("/reports/daily", module="accounting")
def daily_report():
    if g.me["role"] in core.SELLER_ROLES:
        abort(403)                    # the whole business's money: office only
    try:
        day = core.parse_date(request.args.get("day")) or date.today()
    except ValueError:
        day = date.today()
    day = min(day, date.today())
    with get_db().cursor() as cur:
        d = dailyreport.build(cur, day)
    return render_template("daily_report.html", d=d, prev=day - timedelta(days=1),
                           next=(day + timedelta(days=1)) if day < date.today() else None, is_today=day == date.today())


# ===================================================================== monthly statement of sellers
def _month():
    text = (request.args.get("month") or "").strip()
    try:
        first = datetime.strptime(text, "%Y-%m").date().replace(day=1)
    except ValueError:
        first = date.today().replace(day=1)
    if first > date.today():
        first = date.today().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt


def _statement_rows(first, nxt):
    """One row per reseller / sub-reseller / retailer the signed-in person may see, for the month [first, nxt)."""
    names = core.my_downline()
    if names is None:
        if not (g.me["role"] == "admin" or core.can_fn("usr.view")):
            abort(403)
        sellers = q("SELECT a.id, a.username, a.full_name, a.role, a.balance, par.username AS parent FROM panel_admins a "
                    "LEFT JOIN panel_admins par ON par.id = a.parent_id WHERE a.role IN ('reseller', 'subreseller', 'retailer') "
                    "ORDER BY FIELD(a.role, 'reseller', 'subreseller', 'retailer'), a.username")
    else:
        sellers = q("SELECT a.id, a.username, a.full_name, a.role, a.balance, par.username AS parent FROM panel_admins a "
                    "LEFT JOIN panel_admins par ON par.id = a.parent_id WHERE a.username IN (%s) "
                    "ORDER BY FIELD(a.role, 'reseller', 'subreseller', 'retailer'), a.username" % ",".join(["%s"] * len(names)),
                    list(names))
    if not sellers:
        return []
    ids = [s["id"] for s in sellers]
    users = [s["username"] for s in sellers]
    ph_i, ph_u = ",".join(["%s"] * len(ids)), ",".join(["%s"] * len(users))
    led = {r["user_id"]: r for r in q(
        "SELECT user_id, SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS added, "
        "SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS used, COUNT(*) AS n FROM user_ledger "
        "WHERE user_id IN (" + ph_i + ") AND created_at >= %s AND created_at < %s GROUP BY user_id", ids + [first, nxt])}
    # balance at the start / end of the month: the last ledger line before that moment
    def _bal_at(moment):
        rows = q("SELECT l.user_id, l.balance_after FROM user_ledger l JOIN (SELECT user_id, MAX(id) AS mid FROM user_ledger "
                 "WHERE user_id IN (" + ph_i + ") AND created_at < %s GROUP BY user_id) m ON m.mid = l.id", ids + [moment])
        return {r["user_id"]: float(r["balance_after"]) for r in rows}
    opening, closing = _bal_at(first), _bal_at(nxt)
    pays = {r["received_by"]: r for r in q(
        "SELECT received_by, COUNT(*) AS n, SUM(days_added > 0) AS renewals, COALESCE(SUM(amount), 0) AS total FROM payments "
        "WHERE received_by IN (" + ph_u + ") AND paid_at >= %s AND paid_at < %s GROUP BY received_by", users + [first, nxt])}
    subs = {r["created_by"]: r for r in q(
        "SELECT created_by, COUNT(*) AS total, SUM(created_at >= %s AND created_at < %s) AS new_c, "
        "SUM(status = 'active' AND (expiry_date IS NULL OR expiry_date >= CURDATE())) AS active FROM subscribers "
        "WHERE created_by IN (" + ph_u + ") GROUP BY created_by", [first, nxt] + users)}
    labels = dict(core.role_list())
    out = []
    for s in sellers:
        lg, p, c = led.get(s["id"], {}), pays.get(s["username"], {}), subs.get(s["username"], {})
        op = opening.get(s["id"], 0.0)
        cl = closing.get(s["id"], op) if nxt <= date.today() else float(s["balance"] or 0)
        out.append({"id": s["id"], "username": s["username"], "name": s["full_name"] or "", "role": labels.get(s["role"], s["role"]),
                    "parent": s["parent"] or "", "opening": op, "added": float(lg.get("added") or 0), "used": float(lg.get("used") or 0),
                    "closing": cl, "pay_n": int(p.get("n") or 0), "renewals": int(p.get("renewals") or 0),
                    "collected": float(p.get("total") or 0), "new_c": int(c.get("new_c") or 0), "subs": int(c.get("total") or 0),
                    "active": int(c.get("active") or 0)})
    return out


@route("/statements")
def statements():
    first, nxt = _month()
    rows = _statement_rows(first, nxt)
    if request.args.get("csv") == "1":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Username", "Name", "Type", "Parent", "Opening balance", "Balance added", "Balance used", "Closing balance",
                    "Payments", "Renewals", "Collected", "New customers", "Active customers", "All customers"])
        for r in rows:
            w.writerow([r["username"], r["name"], r["role"], r["parent"], "%.2f" % r["opening"], "%.2f" % r["added"],
                        "%.2f" % r["used"], "%.2f" % r["closing"], r["pay_n"], r["renewals"], "%.2f" % r["collected"],
                        r["new_c"], r["active"], r["subs"]])
        audit("statement_export", first.strftime("%Y-%m"))
        commit()
        return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=statement-%s.csv" % first.strftime("%Y-%m")})
    tot = {k: sum(r[k] for r in rows) for k in ("opening", "added", "used", "closing", "pay_n", "renewals", "collected", "new_c", "active", "subs")}
    months = []
    m = date.today().replace(day=1)
    for _ in range(12):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    return render_template("statements.html", rows=rows, tot=tot, first=first, months=months)


# ===================================================================== dues: reminder SMS
def pay_link(s, sid):
    """The bKash pay page for one customer (the expired-notice page by subscriber ID), or "" when bKash is not set up."""
    import mod_expired
    base = (s.get("bkash_callback_base") or "").strip().rstrip("/")
    if not mod_expired._pay_url(s) or not base:
        return ""
    return "%s/expired-notice?pay=1&sid=%s" % (base, core.sub_code(sid))


def dues_filter(args):
    """(where list, params) for the Dues list and its reminder: active customers whose package has ended."""
    where = ["s.status = 'active'", "s.expiry_date < CURDATE()"]
    params = []
    text = (args.get("q") or "").strip()
    if text:
        where.append("(s.username LIKE %s OR s.full_name LIKE %s OR s.phone LIKE %s)")
        params += ["%" + text + "%"] * 3
    if args.get("area"):
        where.append("s.area = %s")
        params.append(args["area"])
    if (args.get("plan") or "").isdigit():
        where.append("s.plan_id = %s")
        params.append(int(args["plan"]))
    over = args.get("over") or ""
    span = {"1-3": (1, 3), "4-7": (4, 7), "8-30": (8, 30), "31+": (31, 100000)}.get(over)
    if span:
        where.append("DATEDIFF(CURDATE(), s.expiry_date) BETWEEN %s AND %s")
        params += list(span)
    names = core.my_downline()
    if names is not None:
        where.append("s.created_by IN (%s)" % ",".join(["%s"] * len(names)))
        params += list(names)
    return where, params


@route("/dues/remind", module="accounting", methods=["POST"])
def dues_remind():
    if not core.can("sms", "edit"):
        abort(403)
    f = request.form
    back = redirect(core.safe_next(f.get("next")) or url_for("dues"))
    where, params = dues_filter(f)
    if f.get("all") != "1":
        ids = [int(v) for v in f.getlist("ids") if v.isdigit()][:5000]
        if not ids:
            flash("Tick the customers to remind, or press Remind everyone shown.", "error")
            return back
        where.append("s.id IN (%s)" % ",".join(["%s"] * len(ids)))
        params += ids
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.expiry_date, s.sms_on, s.balance, p.label AS plan_label, p.price "
             "FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE " + " AND ".join(where) + " LIMIT 5000", params)
    s = core.cfg()
    isp = s.get("isp_name") or ""
    cc, fmt = s.get("sms_country_code") or "880", s.get("sms_number_format") or "intl"
    today = date.today().isoformat()
    queued = skipped = 0
    with get_db().cursor() as cur:
        tpl = msglib.get_template(cur, "sms", "due")
        out = []
        for r in rows:
            phone = msglib.normalize_phone(r["phone"], cc, fmt) if r["phone"] else None
            if not phone or not r["sms_on"]:
                skipped += 1
                continue
            due = max(float(r["price"] or 0) - float(r["balance"] or 0), 0)
            ctx = msglib.sub_context(r, isp, {"amount": msglib.money(due), "id": core.sub_code(r["id"]),
                                              "paylink": pay_link(s, r["id"])})
            body = re.sub(r"[ \t]{2,}", " ", msglib.render(tpl["body"], ctx)).strip()   # an empty {paylink} leaves no gap
            out.append(("sms", phone, "", body, r["id"], "due:%d:%s" % (r["id"], today), None, g.me["username"]))
        queued = msglib.enqueue_many(cur, out) if out else 0
    dup = len(rows) - skipped - queued
    audit("dues_reminder", "", "%d queued, %d without phone / SMS off, %d already reminded today" % (queued, skipped, dup))
    commit()
    msg = "Reminder SMS queued for %d customer%s." % (queued, "" if queued == 1 else "s")
    if skipped:
        msg += " %d skipped (no phone or SMS switched off)." % skipped
    if dup > 0:
        msg += " %d already got one today." % dup
    flash(msg, "success" if queued else "warn")
    return back
