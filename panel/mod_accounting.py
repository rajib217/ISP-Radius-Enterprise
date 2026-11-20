"""Accounting: payments, receipts, expenses, income vs expense, dues, receive payment."""
import csv
import io
from datetime import date, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

import core
from core import admin_required, audit, commit, insert, pager, parse_date, q, route, x

EXPENSE_CATEGORIES = ["Bandwidth / upstream", "Salary", "Electricity", "Rent", "Equipment", "Maintenance",
                      "Transport", "Marketing", "Tax / license", "Other"]
METHODS = ["cash", "bkash", "nagad", "rocket", "bank", "other"]


def _company_only():
    """Expenses and the income summary are the company's own books: a reseller / sub-reseller / retailer never sees them."""
    if core.my_downline() is not None:
        abort(403)


def _in(names):
    return "(" + ",".join(["%s"] * len(names)) + ")"


def _pay_filters(args):
    where, params = [], []
    names = core.my_downline()
    if names is not None:
        # a seller sees the payments they took, the ones taken by people below them, and any payment of their customers
        where.append("(p.received_by IN " + _in(names) + " OR p.subscriber_id IN (SELECT id FROM subscribers "
                     "WHERE created_by IN " + _in(names) + "))")
        params += list(names) * 2
    text = args.get("q", "").strip()
    if text:
        where.append("(p.username LIKE %s OR p.note LIKE %s)")
        params += ["%" + text + "%"] * 2
    if args.get("method"):
        where.append("p.method = %s")
        params.append(args["method"])
    if args.get("by"):
        where.append("p.received_by = %s")
        params.append(args["by"])
    try:
        start, end = parse_date(args.get("from")), parse_date(args.get("to"))
    except ValueError:
        start = end = None
    if start:
        where.append("p.paid_at >= %s")
        params.append(start)
    if end:
        where.append("p.paid_at < %s + INTERVAL 1 DAY")
        params.append(end)
    return (" WHERE " + " AND ".join(where)) if where else "", params


# ===================================================================== payments
@route("/payments", module="accounting")
def payments():
    where, params = _pay_filters(request.args)
    tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(p.amount), 0) AS total FROM payments p" + where, params, one=True)
    pg = pager(tot["c"])
    rows = q("SELECT p.*, s.full_name, s.id AS sid FROM payments p LEFT JOIN subscribers s "
             "ON s.id = p.subscriber_id" + where + " ORDER BY p.id DESC LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    methods = METHODS if core.my_downline() is not None else sorted(
        set(METHODS) | {r["method"] for r in q("SELECT DISTINCT method FROM payments")})
    names = core.my_downline()
    staff = [r["received_by"] for r in q("SELECT DISTINCT received_by FROM payments WHERE received_by <> ''" +
                                         ("" if names is None else " AND received_by IN " + _in(names)) +
                                         " ORDER BY received_by", [] if names is None else list(names))]
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("payments.html", rows=rows, tot=tot, pg=pg, methods=methods, staff=staff, keep=keep,
                           method_list=METHODS)


@route("/payments/export.csv", module="accounting")
def payments_export():
    where, params = _pay_filters(request.args)
    rows = q("SELECT p.id, p.paid_at, p.username, s.full_name, p.amount, p.method, p.days_added, p.period_to, "
             "p.received_by, p.note FROM payments p LEFT JOIN subscribers s ON s.id = p.subscriber_id" + where +
             " ORDER BY p.id DESC LIMIT 100000", params)
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(["receipt", "paid_at", "username", "name", "amount", "method", "days_added", "valid_until",
                "received_by", "note"])
    for r in rows:
        w.writerow([r["id"], r["paid_at"], r["username"], r["full_name"] or "", r["amount"], r["method"],
                    r["days_added"], r["period_to"] or "", r["received_by"], r["note"]])
    audit("export", "payments", "%d rows" % len(rows))
    commit()
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=payments-%s.csv" % date.today().isoformat()})


@route("/payments/add", module="accounting", methods=["POST"])
def payment_add():
    """Record money that does not renew the connection (installation fee, device sale, old dues)."""
    f = request.form
    sub = q("SELECT id, username, created_by FROM subscribers WHERE username = %s", (f.get("username", "").strip(),), one=True)
    if sub and core.my_downline() is not None and sub["created_by"] not in core.my_downline():
        abort(403)
    try:
        amount = round(float(f.get("amount", "0")), 2)
        assert 0 < amount < 10 ** 8
    except (ValueError, AssertionError):
        flash("Enter an amount greater than zero.", "error")
        return redirect(url_for("payments"))
    if not sub:
        flash("No subscriber has that username.", "error")
        return redirect(url_for("payments"))
    pid = insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, received_by) "
                 "VALUES (%s, %s, %s, 0, %s, %s, %s)",
                 (sub["id"], sub["username"], amount, f.get("method", "cash")[:32], f.get("note", "")[:255],
                  g.me["username"]))
    audit("payment_added", sub["username"], "%s" % amount)
    commit()
    flash("Payment recorded (receipt #%d)." % pid, "success")
    return redirect(url_for("receipt", pid=pid))


@route("/payments/<int:pid>/receipt", module="accounting")
def receipt(pid):
    p = q("SELECT p.*, s.full_name, s.phone, s.address, s.area, s.created_by AS owner, pl.label AS plan_label FROM payments p "
          "LEFT JOIN subscribers s ON s.id = p.subscriber_id LEFT JOIN plans pl ON pl.id = s.plan_id "
          "WHERE p.id = %s", (pid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and p["received_by"] not in names and p.get("owner") not in names:
        abort(403)
    inv = q("SELECT inv_name, inv_address, inv_phone, inv_footer FROM panel_admins WHERE username = %s", (p.get("owner") or "",),
            one=True) or {}
    return render_template("receipt.html", p=p, inv=inv)


@route("/payments/<int:pid>/delete", module="accounting", methods=["POST"])
@admin_required
def payment_delete(pid):
    p = q("SELECT username, amount FROM payments WHERE id = %s", (pid,), one=True) or abort(404)
    x("DELETE FROM payments WHERE id = %s", (pid,))
    audit("payment_deleted", p["username"], "%s" % p["amount"])
    commit()
    flash("Payment record deleted. The customer's expiry date was not changed.", "success")
    return redirect(url_for("payments"))


@route("/accounting/receive", module="accounting")
def receive():
    text = request.args.get("q", "").strip()
    rows = []
    if text:
        like = "%" + text + "%"
        names = core.my_downline()
        rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.area, s.status, s.expiry_date, p.label AS plan_label, "
                 "p.price FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE (s.username LIKE %s "
                 "OR s.full_name LIKE %s OR s.phone LIKE %s)" + ("" if names is None else " AND s.created_by IN " + _in(names)) +
                 " ORDER BY s.username LIMIT 20", [like, like, like] + ([] if names is None else list(names)))
        today = date.today()
        for r in rows:
            r["expired"] = bool(r["expiry_date"] and r["expiry_date"] < today)
    return render_template("receive.html", rows=rows, text=text)


# ===================================================================== expenses
def _exp_filters(args):
    where, params = [], []
    text = args.get("q", "").strip()
    if text:
        where.append("(e.paid_to LIKE %s OR e.note LIKE %s)")
        params += ["%" + text + "%"] * 2
    if args.get("category"):
        where.append("e.category = %s")
        params.append(args["category"])
    try:
        start, end = parse_date(args.get("from")), parse_date(args.get("to"))
    except ValueError:
        start = end = None
    if start:
        where.append("e.spent_on >= %s")
        params.append(start)
    if end:
        where.append("e.spent_on <= %s")
        params.append(end)
    return (" WHERE " + " AND ".join(where)) if where else "", params


@route("/expenses", module="accounting")
def expenses():
    _company_only()
    where, params = _exp_filters(request.args)
    tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(e.amount), 0) AS total FROM expenses e" + where, params, one=True)
    pg = pager(tot["c"])
    rows = q("SELECT e.* FROM expenses e" + where + " ORDER BY e.spent_on DESC, e.id DESC LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    by_cat = q("SELECT e.category, SUM(e.amount) AS total FROM expenses e" + where +
               " GROUP BY e.category ORDER BY total DESC", params)
    cats = sorted(set(EXPENSE_CATEGORIES) | {r["category"] for r in q("SELECT DISTINCT category FROM expenses")})
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("expenses.html", rows=rows, tot=tot, pg=pg, by_cat=by_cat, cats=cats, keep=keep,
                           methods=METHODS)


@route("/expenses/save", module="accounting", methods=["POST"])
def expense_save():
    _company_only()
    f = request.form
    eid = int(f["id"]) if f.get("id", "").isdigit() else 0
    try:
        spent = parse_date(f.get("spent_on")) or date.today()
        amount = round(float(f.get("amount", "0")), 2)
        category = f.get("category", "").strip()
        assert 0 < amount < 10 ** 8 and 1 <= len(category) <= 64
    except (ValueError, AssertionError):
        flash("Enter a date, a category and an amount greater than zero.", "error")
        return redirect(url_for("expenses"))
    vals = (spent, category, amount, f.get("paid_to", "").strip()[:128], f.get("method", "cash")[:32],
            f.get("note", "").strip()[:255])
    if eid:
        x("UPDATE expenses SET spent_on = %s, category = %s, amount = %s, paid_to = %s, method = %s, note = %s "
          "WHERE id = %s", vals + (eid,))
    else:
        insert("INSERT INTO expenses (spent_on, category, amount, paid_to, method, note, created_by) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s)", vals + (g.me["username"],))
    audit("expense_saved", category, "%s" % amount)
    commit()
    flash("Expense saved.", "success")
    return redirect(url_for("expenses"))


@route("/expenses/<int:eid>/delete", module="accounting", methods=["POST"])
def expense_delete(eid):
    _company_only()
    row = q("SELECT category, amount FROM expenses WHERE id = %s", (eid,), one=True) or abort(404)
    x("DELETE FROM expenses WHERE id = %s", (eid,))
    audit("expense_deleted", row["category"], "%s" % row["amount"])
    commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses"))


# ===================================================================== income vs expense
@route("/accounting/summary", module="accounting")
def acct_summary():
    _company_only()
    today = date.today()
    months = []
    for back in range(11, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))
    first = date(months[0][0], months[0][1], 1)
    inc = {r["ym"]: float(r["t"] or 0) for r in q(
        "SELECT DATE_FORMAT(paid_at, '%%Y-%%m') AS ym, SUM(amount) AS t FROM payments WHERE paid_at >= %s "
        "GROUP BY ym", (first,))}
    exp = {r["ym"]: float(r["t"] or 0) for r in q(
        "SELECT DATE_FORMAT(spent_on, '%%Y-%%m') AS ym, SUM(amount) AS t FROM expenses WHERE spent_on >= %s "
        "GROUP BY ym", (first,))}
    keys = ["%04d-%02d" % ym for ym in months]
    chart = {"labels": [date(y, m, 1).strftime("%b %y") for y, m in months],
             "series": [{"name": "Income", "values": [inc.get(k, 0) for k in keys]},
                        {"name": "Expenses", "values": [exp.get(k, 0) for k in keys]}],
             "unit": "money", "colors": ["--c1", "--c4"]}
    table = [{"label": chart["labels"][i], "income": inc.get(k, 0), "expense": exp.get(k, 0),
              "net": inc.get(k, 0) - exp.get(k, 0)} for i, k in enumerate(keys)]
    start, end = core.date_range(default_days=today.day - 1)
    span = (start, end)
    income = q("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS c FROM payments WHERE paid_at >= %s "
               "AND paid_at < %s + INTERVAL 1 DAY", span, one=True)
    spent = q("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS c FROM expenses WHERE spent_on >= %s "
              "AND spent_on <= %s", span, one=True)
    by_cat = q("SELECT category, SUM(amount) AS total FROM expenses WHERE spent_on >= %s AND spent_on <= %s "
               "GROUP BY category ORDER BY total DESC", span)
    by_method = q("SELECT method, SUM(amount) AS total FROM payments WHERE paid_at >= %s "
                  "AND paid_at < %s + INTERVAL 1 DAY GROUP BY method ORDER BY total DESC", span)
    return render_template("acct_summary.html", chart=chart, table=table, income=income, spent=spent,
                           by_cat=by_cat, by_method=by_method, start=start, end=end)


# ===================================================================== dues
@route("/dues", module="accounting")
def dues():
    where = ["s.status = 'active'", "s.expiry_date < CURDATE()"]
    params = []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(s.username LIKE %s OR s.full_name LIKE %s OR s.phone LIKE %s)")
        params += ["%" + text + "%"] * 3
    if request.args.get("area"):
        where.append("s.area = %s")
        params.append(request.args["area"])
    if request.args.get("plan", "").isdigit():
        where.append("s.plan_id = %s")
        params.append(int(request.args["plan"]))
    names = core.my_downline()
    if names is not None:
        where.append("s.created_by IN " + _in(names))
        params += list(names)
    cond = " WHERE " + " AND ".join(where)
    tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(p.price), 0) AS amount FROM subscribers s "
            "LEFT JOIN plans p ON p.id = s.plan_id" + cond, params, one=True)
    pg = pager(tot["c"])
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.area, s.expiry_date, p.label AS plan_label, p.price, "
             "DATEDIFF(CURDATE(), s.expiry_date) AS days_over FROM subscribers s LEFT JOIN plans p "
             "ON p.id = s.plan_id" + cond + " ORDER BY s.expiry_date ASC, s.username LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    plans = core.filter_plans(q("SELECT id, label FROM plans ORDER BY download_mbps, label"))
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("dues.html", rows=rows, tot=tot, pg=pg, plans=plans, areas=core.area_names(), keep=keep)
