"""Accounting: payments, receipts, cashflow (income and expenses), income vs expense, dues, receive payment."""
import csv
import io
from datetime import date, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

import core
from core import admin_required, audit, commit, insert, pager, parse_date, q, route, x

METHODS = ["cash", "bkash", "nagad", "rocket", "bank", "other"]


def _company_only():
    """Expenses and the income summary are the company's own books: a reseller / sub-reseller / retailer never sees them."""
    if core.my_downline() is not None:
        abort(403)


def _in(names):
    return "(" + ",".join(["%s"] * len(names)) + ")"


PROFILE_TYPES = [("own", "Own Reports"), ("reseller", "Resellers"), ("subreseller", "Subresellers"), ("retailer", "Retailers")]


def _profiles():
    """Payments filter > Profile Type: the resellers / sub-resellers / retailers the viewer may pick, by type.
    A seller sees only the people below them."""
    names = core.my_downline()
    rows = q("SELECT id, username, full_name, role FROM panel_admins WHERE role IN ('reseller', 'subreseller', 'retailer') "
             "ORDER BY username")
    out = {k: [] for k, _ in PROFILE_TYPES if k != "own"}
    for r in rows:
        if names is not None and (r["username"] not in names or r["username"] == g.me["username"]):
            continue
        out[r["role"]].append(r)
    return out


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
    # Profile Type: whose customers paid. Own = the company's own customers (a seller: the ones they made themselves);
    # a type alone = customers of anyone of that type; a type and a person = that person's customers and their downline's
    ptype, owner = args.get("ptype", ""), (args.get("owner") or "").strip()
    sellers = "SELECT username FROM panel_admins WHERE role IN ('reseller', 'subreseller', 'retailer')"
    if ptype == "own":
        if names is None:
            where.append("(p.subscriber_id IS NULL OR p.subscriber_id NOT IN (SELECT id FROM subscribers WHERE created_by IN ("
                         + sellers + ")))")
        else:
            where.append("p.subscriber_id IN (SELECT id FROM subscribers WHERE created_by = %s)")
            params.append(g.me["username"])
    elif ptype in ("reseller", "subreseller", "retailer"):
        person = owner and q("SELECT id, username FROM panel_admins WHERE username = %s AND role = %s", (owner, ptype), one=True)
        if person and (names is None or person["username"] in names):
            under = core.downline_of(person["id"], person["username"])
            where.append("p.subscriber_id IN (SELECT id FROM subscribers WHERE created_by IN " + _in(under) + ")")
            params += under
        elif owner:
            where.append("1 = 0")              # a name that is not of that type (or not below this seller): nothing
        else:
            where.append("p.subscriber_id IN (SELECT id FROM subscribers WHERE created_by IN (SELECT username FROM "
                         "panel_admins WHERE role = %s))")
            params.append(ptype)
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
    keep = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("payments.html", rows=rows, tot=tot, pg=pg, methods=methods, staff=staff, keep=keep,
                           method_list=METHODS, profiles=_profiles(), profile_types=PROFILE_TYPES,
                           active=[k for k in ("q", "from", "to", "ptype", "owner", "method", "by") if request.args.get(k)])


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


# ===================================================================== cashflow
# The company's own book: income and expenses written by hand (table expenses, column kind), plus the money resellers /
# sub-resellers / retailers pay in for balance (their balance topups: bKash, or balance added by staff), which shows on
# its own as Income > User Payment. Expenses written before Cashflow existed are its Expense rows.
KINDS = [("income", "Income"), ("expense", "Expense")]
INCOME_CATEGORIES = ["User Payment", "Installation fee", "Device sale", "Other income"]
TOPUP_CATEGORY = "User Payment"
SELLERS_SQL = "SELECT username FROM panel_admins WHERE role IN ('reseller', 'subreseller', 'retailer')"


def _categories(kind=None):
    rows = q("SELECT id, kind, name, note, created_by, created_at FROM cashflow_categories" +
             (" WHERE kind = %s" if kind else "") + " ORDER BY kind DESC, name", (kind,) if kind else ())
    return rows


def _cf_filters(args):
    today = date.today()
    try:
        start, end = parse_date(args.get("from")), parse_date(args.get("to"))
    except ValueError:
        start = end = None
    kind = args.get("type") if args.get("type") in ("income", "expense") else ""
    src = args.get("src") if args.get("src") in ("manual", "auto") else ""
    return {"from": start, "to": end, "type": kind, "src": src, "category": (args.get("category") or "").strip()[:64],
            "q": (args.get("q") or "").strip()[:64], "today": today}


def _cf_parts(f):
    """(sql, params) of the whole book, newest first, already filtered. The same columns from both sources."""
    like = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if f["q"] else None
    parts = []
    if f["src"] in ("", "manual"):
        sql = ("SELECT 'M' AS src, e.id AS rid, e.kind AS kind, e.amount AS amount, e.category AS category, e.paid_to AS paid_to, "
               "e.note AS note, e.method AS method, e.profile AS profile, pa.role AS prole, e.created_by AS created_by, "
               "TIMESTAMP(e.spent_on, TIME(e.created_at)) AS at, e.spent_on AS day "
               "FROM expenses e LEFT JOIN panel_admins pa ON pa.username = e.profile WHERE 1 = 1")
        args = []
        if f["type"]:
            sql += " AND e.kind = %s"
            args.append(f["type"])
        if f["category"]:
            sql += " AND e.category = %s"
            args.append(f["category"])
        if f["from"]:
            sql += " AND e.spent_on >= %s"
            args.append(f["from"])
        if f["to"]:
            sql += " AND e.spent_on <= %s"
            args.append(f["to"])
        if like:
            sql += " AND (e.category LIKE %s OR e.paid_to LIKE %s OR e.note LIKE %s OR e.profile LIKE %s OR e.created_by LIKE %s)"
            args += [like] * 5
        parts.append((sql, args))
    if f["src"] in ("", "auto") and f["type"] in ("", "income") and f["category"] in ("", TOPUP_CATEGORY):
        sql = ("SELECT 'T' AS src, ul.id AS rid, 'income' AS kind, ul.amount AS amount, '" + TOPUP_CATEGORY + "' AS category, "
               "'' AS paid_to, IF(ul.method = 'bkash', 'User Balance Topup (bKash)', 'User Balance Topup') AS note, "
               "ul.method AS method, a.username AS profile, a.role AS prole, ul.created_by AS created_by, ul.created_at AS at, "
               "DATE(ul.created_at) AS day FROM user_ledger ul JOIN panel_admins a ON a.id = ul.user_id "
               "WHERE ul.amount > 0 AND ul.method <> 'activation' AND a.role IN ('reseller', 'subreseller', 'retailer') "
               "AND (ul.method = 'bkash' OR ul.created_by NOT IN (" + SELLERS_SQL + "))")
        args = []
        if f["from"]:
            sql += " AND ul.created_at >= %s"
            args.append(f["from"])
        if f["to"]:
            sql += " AND ul.created_at < %s + INTERVAL 1 DAY"
            args.append(f["to"])
        if like:
            sql += " AND (a.username LIKE %s OR ul.created_by LIKE %s OR ul.note LIKE %s)"
            args += [like] * 3
        parts.append((sql, args))
    if not parts:
        return None, []
    return " UNION ALL ".join("(%s)" % s for s, _ in parts), [a for _, args in parts for a in args]


def _cf_keep():
    return {k: v for k, v in request.args.items() if k not in ("page",) and v}


@route("/cashflow", module="accounting")
def cashflow():
    _company_only()
    f = _cf_filters(request.args)
    sql, params = _cf_parts(f)
    if sql:
        tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(IF(kind = 'income', amount, 0)), 0) AS income, "
                "COALESCE(SUM(IF(kind = 'expense', amount, 0)), 0) AS expense FROM (" + sql + ") t", params, one=True)
    else:
        tot = {"c": 0, "income": 0, "expense": 0}
    pg = pager(tot["c"])
    rows = q("SELECT * FROM (" + sql + ") t ORDER BY at DESC, rid DESC LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]]) if sql and tot["c"] else []
    cats = _categories()
    used = {(r["kind"], r["category"]) for r in q("SELECT DISTINCT kind, category FROM expenses")}
    names = {(c["kind"], c["name"]) for c in cats}
    extra = sorted(u for u in used if u not in names)          # in use but not on the category list (kept as they are)
    people = q("SELECT username, role FROM panel_admins WHERE role IN ('reseller', 'subreseller', 'retailer') ORDER BY username")
    return render_template("cashflow.html", rows=rows, tot=tot, pg=pg, f=f, keep=_cf_keep(), cats=cats, extra=extra,
                           kinds=KINDS, methods=METHODS, people=people, topup_category=TOPUP_CATEGORY,
                           can_export=g.me["role"] == "admin" or core.can_fn("cf.export"),
                           active=[k for k in ("q", "from", "to", "type", "category", "src") if request.args.get(k)])


def _back():
    return redirect(core.safe_next(request.form.get("next")) or url_for("cashflow"))


@route("/cashflow/save", module="accounting", methods=["POST"])
def cashflow_save():
    _company_only()
    f = request.form
    eid = int(f["id"]) if f.get("id", "").isdigit() else 0
    try:
        day = parse_date(f.get("spent_on")) or date.today()
        amount = round(float(f.get("amount", "0")), 2)
        kind = f.get("kind", "expense")
        category = f.get("category", "").strip()
        assert kind in ("income", "expense") and 0 < amount < 10 ** 8 and 1 <= len(category) <= 64
    except (ValueError, AssertionError):
        flash("Enter the type, a date, a category and an amount greater than zero.", "error")
        return _back()
    profile = f.get("profile", "").strip()[:64]
    if profile and not q("SELECT 1 FROM panel_admins WHERE username = %s", (profile,), one=True):
        flash("No user is called %s. Nothing was saved." % profile, "error")
        return _back()
    vals = (kind, day, category, amount, f.get("paid_to", "").strip()[:128], f.get("method", "cash")[:32],
            f.get("note", "").strip()[:255], profile)
    if eid:
        if not x("UPDATE expenses SET kind = %s, spent_on = %s, category = %s, amount = %s, paid_to = %s, method = %s, "
                 "note = %s, profile = %s WHERE id = %s", vals + (eid,)) and not q("SELECT 1 FROM expenses WHERE id = %s", (eid,), one=True):
            abort(404)
    else:
        eid = insert("INSERT INTO expenses (kind, spent_on, category, amount, paid_to, method, note, profile, created_by) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", vals + (g.me["username"],))
    x("INSERT IGNORE INTO cashflow_categories (kind, name, created_by) VALUES (%s, %s, %s)", (kind, category, g.me["username"]))
    audit("cashflow_saved", "%s #%d" % (kind, eid), "%s %s" % (category, amount))
    commit()
    flash("%s saved." % ("Income" if kind == "income" else "Expense"), "success")
    return _back()


@route("/cashflow/<int:eid>/delete", module="accounting", methods=["POST"])
def cashflow_delete(eid):
    _company_only()
    row = q("SELECT kind, category, amount FROM expenses WHERE id = %s", (eid,), one=True) or abort(404)
    x("DELETE FROM expenses WHERE id = %s", (eid,))
    audit("cashflow_deleted", "%s #%d" % (row["kind"], eid), "%s %s" % (row["category"], row["amount"]))
    commit()
    flash("Cashflow entry deleted.", "success")
    return _back()


def _cf_export():
    _company_only()
    f = _cf_filters(request.args)
    sql, params = _cf_parts(f)
    rows = q("SELECT * FROM (" + sql + ") t ORDER BY at DESC, rid DESC LIMIT 100000", params) if sql else []
    head = ["ID", "Date", "Type", "Category", "Amount", "Profile", "Paid to / from", "Method", "Description", "Created by", "Source"]
    data = [["%s%d" % ("CF" if r["src"] == "M" else "TOP", r["rid"]), r["at"].strftime("%Y-%m-%d %H:%M") if r["at"] else "",
             r["kind"].capitalize(), r["category"], float(r["amount"]), r["profile"] or "", r["paid_to"] or "", r["method"] or "",
             r["note"] or "", r["created_by"] or "", "Written" if r["src"] == "M" else "Balance topup"] for r in rows]
    audit("export", "cashflow", "%d rows" % len(rows))
    commit()
    return head, data


@route("/cashflow.csv", module="accounting")
def cashflow_csv():
    head, data = _cf_export()
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(head)
    for r in data:
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in r])
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=cashflow-%s.csv" % date.today().isoformat()})


@route("/cashflow.xlsx", module="accounting")
def cashflow_xlsx():
    import mod_ledger
    head, data = _cf_export()
    return Response(mod_ledger._xlsx(head, data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=cashflow-%s.xlsx" % date.today().isoformat()})


@route("/expenses", module="accounting")
def expenses():
    """The old Expenses page is Cashflow's Expense list now."""
    return redirect(url_for("cashflow", type="expense"))


# ----------------------------------------------------------------------------- cashflow categories
@route("/cashflow/categories", module="accounting")
def cashflow_categories():
    _company_only()
    cats = _categories()
    use = {(r["kind"], r["category"]): r for r in q(
        "SELECT kind, category, COUNT(*) AS c, SUM(amount) AS total FROM expenses GROUP BY kind, category")}
    for c in cats:
        u = use.get((c["kind"], c["name"]))
        c["uses"], c["total"] = (u["c"], u["total"]) if u else (0, 0)
    return render_template("cashflow_categories.html", cats=cats, kinds=KINDS, topup_category=TOPUP_CATEGORY)


@route("/cashflow/categories/save", module="accounting", methods=["POST"])
def cashflow_category_save():
    _company_only()
    f = request.form
    cid = int(f["id"]) if f.get("id", "").isdigit() else 0
    kind, name = f.get("kind", ""), f.get("name", "").strip()
    if kind not in ("income", "expense") or not 1 <= len(name) <= 64:
        flash("Choose Income or Expense and give the category a name (up to 64 characters).", "error")
        return _back()
    clash = q("SELECT id FROM cashflow_categories WHERE kind = %s AND name = %s", (kind, name), one=True)
    if clash and clash["id"] != cid:
        flash("There is already a %s category called %s." % (kind, name), "error")
        return _back()
    if cid:
        old = q("SELECT kind, name FROM cashflow_categories WHERE id = %s", (cid,), one=True) or abort(404)
        x("UPDATE cashflow_categories SET kind = %s, name = %s, note = %s WHERE id = %s", (kind, name, f.get("note", "").strip()[:255], cid))
        # entries keep up with a renamed category
        x("UPDATE expenses SET kind = %s, category = %s WHERE kind = %s AND category = %s", (kind, name, old["kind"], old["name"]))
    else:
        insert("INSERT INTO cashflow_categories (kind, name, note, created_by) VALUES (%s, %s, %s, %s)",
               (kind, name, f.get("note", "").strip()[:255], g.me["username"]))
    audit("cashflow_category_saved", name, kind)
    commit()
    flash("Category saved.", "success")
    return _back()


@route("/cashflow/categories/<int:cid>/delete", module="accounting", methods=["POST"])
def cashflow_category_delete(cid):
    _company_only()
    row = q("SELECT kind, name FROM cashflow_categories WHERE id = %s", (cid,), one=True) or abort(404)
    used = q("SELECT COUNT(*) AS c FROM expenses WHERE kind = %s AND category = %s", (row["kind"], row["name"]), one=True)["c"]
    if used:
        flash("%s is used by %d entr%s. Move them to another category first." % (row["name"], used, "y" if used == 1 else "ies"), "error")
        return _back()
    x("DELETE FROM cashflow_categories WHERE id = %s", (cid,))
    audit("cashflow_category_deleted", row["name"], row["kind"])
    commit()
    flash("Category deleted.", "success")
    return _back()


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
        "SELECT DATE_FORMAT(spent_on, '%%Y-%%m') AS ym, SUM(amount) AS t FROM expenses WHERE kind = 'expense' AND spent_on >= %s "
        "GROUP BY ym", (first,))}
    for r in q("SELECT DATE_FORMAT(spent_on, '%%Y-%%m') AS ym, SUM(amount) AS t FROM expenses WHERE kind = 'income' "
               "AND spent_on >= %s GROUP BY ym", (first,)):        # income written in Cashflow counts as income too
        inc[r["ym"]] = inc.get(r["ym"], 0) + float(r["t"] or 0)
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
    other = q("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS c FROM expenses WHERE kind = 'income' AND spent_on >= %s "
              "AND spent_on <= %s", span, one=True)
    income = {"total": float(income["total"] or 0) + float(other["total"] or 0), "c": income["c"] + other["c"]}
    spent = q("SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS c FROM expenses WHERE kind = 'expense' AND spent_on >= %s "
              "AND spent_on <= %s", span, one=True)
    spent = {"total": float(spent["total"] or 0), "c": spent["c"]}
    by_cat = q("SELECT category, SUM(amount) AS total FROM expenses WHERE kind = 'expense' AND spent_on >= %s AND spent_on <= %s "
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
