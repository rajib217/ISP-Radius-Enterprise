"""Reports (traffic, collection, customers), plan report and the RADIUS sessions log."""
import csv
import io
from datetime import timedelta

from flask import Response, abort, g, render_template, request

import core
from core import fmt_money, n, pager, q, route

SPAN_SQL = "acctstarttime >= %s AND acctstarttime < %s + INTERVAL 1 DAY"


@route("/reports", module="reports")
def reports():
    # Users > Roles > Reports: Bandwidth Usage = Traffic, Payment Reports = Collection, Sales Reports = Customers and Seller profit
    tab_fn = {"traffic": "rep.bandwidth", "collection": "rep.payment", "customers": "rep.sales", "profit": "rep.sales"}
    tabs = tuple(k for k in ("traffic", "collection", "customers", "profit") if core.can_fn(tab_fn[k])) + \
        (("server",) if g.me["role"] == "admin" else ())
    if not tabs:
        abort(403)
    tab = request.args.get("tab") if request.args.get("tab") in tabs else tabs[0]
    start, end = core.date_range(29)
    span = (start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    labels = [d.strftime("%d %b") for d in days]
    ctx = {"tab": tab, "start": start, "end": end}
    if tab == "traffic":
        ctx["tot"] = q("SELECT COUNT(*) AS sessions, COUNT(DISTINCT username) AS users, "
                       "COALESCE(SUM(acctinputoctets), 0) AS up, COALESCE(SUM(acctoutputoctets), 0) AS down "
                       "FROM radacct WHERE " + SPAN_SQL, span, one=True)
        daily = {r["d"]: r for r in q(
            "SELECT DATE(acctstarttime) AS d, SUM(acctinputoctets) AS up, SUM(acctoutputoctets) AS down FROM radacct "
            "WHERE " + SPAN_SQL + " GROUP BY DATE(acctstarttime)", span)}
        ctx["chart"] = {"labels": labels,
                        "series": [{"name": "Download", "values": [n(daily.get(d, {}).get("down")) for d in days]},
                                   {"name": "Upload", "values": [n(daily.get(d, {}).get("up")) for d in days]}]}
        ctx["top"] = q("SELECT ra.username, SUM(ra.acctinputoctets) AS up, SUM(ra.acctoutputoctets) AS down, "
                       "COUNT(*) AS sessions, s.id AS sid, s.full_name FROM radacct ra "
                       "LEFT JOIN subscribers s ON s.username = ra.username WHERE ra.acctstarttime >= %s "
                       "AND ra.acctstarttime < %s + INTERVAL 1 DAY GROUP BY ra.username, s.id, s.full_name "
                       "ORDER BY SUM(ra.acctinputoctets + ra.acctoutputoctets) DESC LIMIT 20", span)
    elif tab == "collection":
        names = core.my_downline()
        sc, sa = ("", []) if names is None else (" AND received_by IN (" + ",".join(["%s"] * len(names)) + ")", names)
        ctx["tot"] = q("SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS total FROM payments "
                       "WHERE paid_at >= %s AND paid_at < %s + INTERVAL 1 DAY" + sc, list(span) + sa, one=True)
        daily = {r["d"]: r for r in q(
            "SELECT DATE(paid_at) AS d, SUM(amount) AS total FROM payments WHERE paid_at >= %s "
            "AND paid_at < %s + INTERVAL 1 DAY" + sc + " GROUP BY DATE(paid_at)", list(span) + sa)}
        ctx["chart"] = {"labels": labels, "unit": "money",
                        "series": [{"name": "Collected", "values": [float(daily.get(d, {}).get("total") or 0) for d in days]}]}
        ctx["by_user"] = q("SELECT received_by, COUNT(*) AS c, SUM(amount) AS total FROM payments "
                           "WHERE paid_at >= %s AND paid_at < %s + INTERVAL 1 DAY" + sc + " GROUP BY received_by "
                           "ORDER BY total DESC", list(span) + sa)
        ctx["by_method"] = q("SELECT method, COUNT(*) AS c, SUM(amount) AS total FROM payments "
                             "WHERE paid_at >= %s AND paid_at < %s + INTERVAL 1 DAY" + sc + " GROUP BY method "
                             "ORDER BY total DESC", list(span) + sa)
    elif tab == "profit":
        ctx.update(_profit_report(span, days, labels))
    elif tab == "server":
        ctx.update(_server_report(start, end, labels_days=(end - start).days + 1))
    else:
        ctx["joined"] = q("SELECT COUNT(*) AS c FROM subscribers WHERE created_at >= %s "
                          "AND created_at < %s + INTERVAL 1 DAY", span, one=True)["c"]
        daily = {r["d"]: r["c"] for r in q(
            "SELECT DATE(created_at) AS d, COUNT(*) AS c FROM subscribers WHERE created_at >= %s "
            "AND created_at < %s + INTERVAL 1 DAY GROUP BY DATE(created_at)", span)}
        ctx["chart"] = {"labels": labels, "unit": "number", "series": [{"name": "New customers",
                                                      "values": [n(daily.get(d)) for d in days]}]}
        ctx["totals"] = q("SELECT COUNT(*) AS total, COALESCE(SUM(status = 'active' AND (expiry_date IS NULL "
                          "OR expiry_date >= CURDATE())), 0) AS active, "
                          "COALESCE(SUM(status = 'active' AND expiry_date < CURDATE()), 0) AS expired, "
                          "COALESCE(SUM(status = 'suspended'), 0) AS suspended FROM subscribers", one=True)
        ctx["by_area"] = q("SELECT COALESCE(NULLIF(area, ''), '(no area)') AS area, COUNT(*) AS c "
                           "FROM subscribers GROUP BY 1 ORDER BY c DESC LIMIT 12")
    return render_template("reports.html", **ctx)


def _uptime():
    try:
        with open("/proc/uptime") as fh:
            secs = int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        return ""
    d, rest = divmod(secs, 86400)
    h, rest = divmod(rest, 3600)
    return "%dd %dh" % (d, h) if d else "%dh %dm" % (h, rest // 60)


def _server_report(start, end, labels_days):
    """CPU / RAM / disk history for the chosen dates from the samples taken every 5 minutes."""
    if labels_days <= 2:
        bucket, fmt = "ts", "%d %b %H:%M"             # every sample
    elif labels_days <= 31:
        bucket, fmt = "DATE_FORMAT(ts, '%%Y-%%m-%%d %%H:00:00')", "%d %b %H:00"   # hourly average
    else:
        bucket, fmt = "DATE(ts)", "%d %b"             # daily average
    rows = q("SELECT " + bucket + " AS b, AVG(cpu_pct) AS cpu, AVG(100 * mem_used_mb / NULLIF(mem_total_mb, 0)) AS mem, "
             "AVG(100 * disk_used_gb / NULLIF(disk_total_gb, 0)) AS disk, AVG(load1) AS load1 FROM server_stats "
             "WHERE ts >= %s AND ts < %s + INTERVAL 1 DAY GROUP BY b ORDER BY b", (start, end))
    peak = q("SELECT MAX(cpu_pct) AS cpu, AVG(cpu_pct) AS cpu_avg, MAX(100 * mem_used_mb / NULLIF(mem_total_mb, 0)) AS mem, "
             "MAX(load1) AS load1, COUNT(*) AS samples FROM server_stats WHERE ts >= %s AND ts < %s + INTERVAL 1 DAY",
             (start, end), one=True)
    now = q("SELECT * FROM server_stats ORDER BY ts DESC LIMIT 1", one=True)
    labels = [r["b"].strftime(fmt) if hasattr(r["b"], "strftime") else str(r["b"]) for r in rows]

    def chart(key, suffix, unit, color):
        return {"labels": labels, "values": [round(float(r[key] or 0), 2) for r in rows], "unit": unit,
                "suffix": suffix, "color": color, "height": 170}
    return {"srv": {"now": now, "peak": peak, "uptime": _uptime(), "keep": core.cfg().get("server_stats_days") or "180",
                    "charts": {"cpu": chart("cpu", " CPU", "percent", "--c1"), "mem": chart("mem", " RAM used", "percent", "--c2"),
                               "disk": chart("disk", " disk used", "percent", "--c3"),
                               "load": chart("load1", " load", "load", "--c4")}}}


def _sellers():
    """The resellers / sub-resellers / retailers this person may look at (themselves and everyone below for a seller)."""
    names = core.my_downline()
    rows = q("SELECT id, username, full_name, role, parent_id FROM panel_admins WHERE role IN ('reseller','subreseller','retailer') "
             "ORDER BY username")
    return [r for r in rows if names is None or r["username"] in names]


def _pick(sellers):
    """The seller chosen in the drop-down: (row or None, ids to include or None for everyone, include_below)."""
    sid = request.args.get("seller", type=int)
    chosen = next((r for r in sellers if r["id"] == sid), None)
    below = bool(request.args.get("below"))
    if not chosen:
        return None, None, False
    ids, todo = {chosen["id"]}, [chosen["id"]]
    while below and todo:
        cur_id = todo.pop()
        for r in sellers:
            if r["parent_id"] == cur_id and r["id"] not in ids:
                ids.add(r["id"])
                todo.append(r["id"])
    return chosen, sorted(ids), below


def _scope(ids):
    """(' AND ...', params) limiting payments p / sellers a to the chosen seller(s), or to the signed-in seller's downline."""
    if ids is not None:
        return " AND p.payer_id IN (" + ",".join(["%s"] * len(ids)) + ")", list(ids)
    names = core.my_downline()
    if names is not None:
        return " AND a.username IN (" + ",".join(["%s"] * len(names)) + ")", list(names)
    return "", []


def _profit_rows(span, ids=None):
    """One row per reseller / sub-reseller / retailer who sold in the period: sales, what customers paid, what the seller
    paid for the packages, and the difference. Only sales that recorded the package cost are counted."""
    where, extra = _scope(ids)
    return q("SELECT a.id, a.username, a.full_name, a.role, a.parent_id, COUNT(*) AS sales, SUM(p.amount) AS collected, "
             "SUM(p.cost) AS cost FROM payments p JOIN panel_admins a ON a.id = p.payer_id "
             "WHERE p.cost IS NOT NULL AND p.paid_at >= %s AND p.paid_at < %s + INTERVAL 1 DAY" + where +
             " GROUP BY a.id, a.username, a.full_name, a.role, a.parent_id ORDER BY SUM(p.amount - p.cost) DESC",
             list(span) + extra)


def _sales_rows(span, ids, limit=None):
    """Every sale of the chosen seller(s) with the customer, what they paid and the seller's cost."""
    where, extra = _scope(ids)
    return q("SELECT p.paid_at, p.username, p.amount, p.cost, p.days_added, p.method, p.note, a.username AS seller "
             "FROM payments p JOIN panel_admins a ON a.id = p.payer_id "
             "WHERE p.cost IS NOT NULL AND p.paid_at >= %s AND p.paid_at < %s + INTERVAL 1 DAY" + where +
             " ORDER BY p.paid_at DESC" + (" LIMIT %d" % limit if limit else ""), list(span) + extra)


def _profit_report(span, days, labels):
    sellers = _sellers()
    chosen, ids, below = _pick(sellers)
    rows = _profit_rows(span, ids)
    out, tot = [], {"sales": 0, "collected": 0.0, "cost": 0.0}
    for r in rows:
        col, cost = float(r["collected"] or 0), float(r["cost"] or 0)
        out.append({"id": r["id"], "username": r["username"], "full_name": r["full_name"], "role": r["role"],
                    "sales": r["sales"], "collected": col, "cost": cost, "profit": round(col - cost, 2),
                    "margin": round((col - cost) * 100 / col, 1) if col else 0})
        tot["sales"] += r["sales"]
        tot["collected"] += col
        tot["cost"] += cost
    tot["profit"] = round(tot["collected"] - tot["cost"], 2)
    where, extra = _scope(ids)
    daily = {r["d"]: float(r["profit"] or 0) for r in q(
        "SELECT DATE(p.paid_at) AS d, SUM(p.amount - p.cost) AS profit FROM payments p JOIN panel_admins a ON a.id = p.payer_id "
        "WHERE p.cost IS NOT NULL AND p.paid_at >= %s AND p.paid_at < %s + INTERVAL 1 DAY" + where + " GROUP BY DATE(p.paid_at)",
        list(span) + extra)}
    sales = []
    if chosen:
        for r in _sales_rows(span, ids, 500):
            sales.append(dict(r, profit=round(float(r["amount"] or 0) - float(r["cost"] or 0), 2)))
    return {"prof": out, "ptot": tot, "unit_note": core.my_downline() is not None, "sellers": sellers, "chosen": chosen,
            "below": below, "sales": sales, "sales_cut": len(sales) >= 500,
            "chart": {"labels": labels, "unit": "money",
                      "series": [{"name": "Profit", "values": [round(daily.get(d, 0), 2) for d in days]}]}}


@route("/my-profit")
def my_profit():
    """The same profit table for a reseller / sub-reseller / retailer: themselves and everyone below them."""
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    start, end = core.date_range(29)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    ctx = _profit_report((start, end), days, [d.strftime("%d %b") for d in days])
    return render_template("my_profit.html", start=start, end=end, tab="profit", **ctx)


@route("/my-profit.csv")
def my_profit_csv():
    if g.me["role"] not in core.SELLER_ROLES:
        abort(403)
    return profit_csv()


@route("/reports/profit.csv", module="reports")
def profit_csv():
    """Summary per seller, or, when one seller is chosen, every sale of that seller."""
    start, end = core.date_range(29)
    chosen, ids, below = _pick(_sellers())
    buf = io.StringIO()
    w = csv.writer(buf)
    clean = lambda v: str(v or "").lstrip("=+-@")
    if chosen:
        w.writerow(["date", "seller", "customer", "days", "method", "customer_paid", "seller_paid", "profit", "note"])
        for r in _sales_rows((start, end), ids):
            w.writerow([r["paid_at"], clean(r["seller"]), clean(r["username"]), r["days_added"], r["method"],
                        "%.2f" % float(r["amount"] or 0), "%.2f" % float(r["cost"] or 0),
                        "%.2f" % (float(r["amount"] or 0) - float(r["cost"] or 0)), clean(r["note"])])
        name = "seller-sales-%s" % chosen["username"]
    else:
        w.writerow(["username", "name", "role", "sales", "customers_paid", "seller_paid", "profit"])
        for r in _profit_rows((start, end)):
            col, cost = float(r["collected"] or 0), float(r["cost"] or 0)
            w.writerow([clean(r["username"]), clean(r["full_name"]), r["role"], r["sales"],
                        "%.2f" % col, "%.2f" % cost, "%.2f" % (col - cost)])
        name = "seller-profit"
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=%s-%s-to-%s.csv" % (name, start, end)})


@route("/reports/collection.csv", module="reports")
def collection_csv():
    start, end = core.date_range(29)
    names = core.my_downline()
    sc, sa = ("", []) if names is None else (" AND received_by IN (" + ",".join(["%s"] * len(names)) + ")", names)
    rows = q("SELECT paid_at, username, amount, method, days_added, period_to, received_by, note FROM payments "
             "WHERE paid_at >= %s AND paid_at < %s + INTERVAL 1 DAY" + sc + " ORDER BY paid_at", [start, end] + sa)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paid_at", "username", "amount", "method", "days_added", "valid_until", "received_by", "note"])
    for r in rows:
        w.writerow([r["paid_at"], r["username"], r["amount"], r["method"], r["days_added"], r["period_to"] or "",
                    r["received_by"], str(r["note"] or "").lstrip("=+-@")])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=collection-%s-to-%s.csv" % (start, end)})


@route("/reports/plans", module="plans")
def plan_report():
    rows = q("SELECT p.id, p.label, p.download_mbps, p.upload_mbps, p.bandwidth_value, p.bandwidth_type, p.price, COUNT(s.id) AS total, "
             "COALESCE(SUM(s.status = 'active' AND (s.expiry_date IS NULL OR s.expiry_date >= CURDATE())), 0) AS active, "
             "COALESCE(SUM(s.status = 'active' AND s.expiry_date < CURDATE()), 0) AS expired, "
             "COALESCE(SUM(s.status = 'suspended'), 0) AS suspended "
             "FROM plans p LEFT JOIN subscribers s ON s.plan_id = p.id GROUP BY p.id, p.label, p.download_mbps, "
             "p.upload_mbps, p.bandwidth_value, p.bandwidth_type, p.price ORDER BY total DESC, p.label")
    for r in rows:
        r["monthly"] = float(r["price"] or 0) * n(r["active"])
    online = {r["plan_id"]: r["c"] for r in q(
        "SELECT s.plan_id, COUNT(*) AS c FROM radacct ra JOIN subscribers s ON s.username = ra.username "
        "WHERE ra.acctstoptime IS NULL GROUP BY s.plan_id")}
    for r in rows:
        r["online"] = n(online.get(r["id"]))
    tot = {"subs": sum(r["total"] for r in rows), "active": sum(n(r["active"]) for r in rows),
           "monthly": sum(r["monthly"] for r in rows)}
    chart = {"items": [{"name": r["label"], "value": n(r["total"])} for r in rows[:6]]}
    return render_template("plan_report.html", rows=rows, tot=tot, chart=chart)


@route("/logs/sessions", module="logs")
def logs_sessions():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("ra.username LIKE %s")
        params.append(text + "%")
    if request.args.get("state") == "online":
        where.append("ra.acctstoptime IS NULL")
    elif request.args.get("state") == "closed":
        where.append("ra.acctstoptime IS NOT NULL")
    start, end = core.date_range(6)
    if request.args.get("from") or request.args.get("to"):
        where.append("ra.acctstarttime >= %s AND ra.acctstarttime < %s + INTERVAL 1 DAY")
        params += [start, end]
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    # without a text filter an exact COUNT on millions of rows is slow, so the page just offers next/previous
    page, per = core.page_number(), 50
    rows = q("SELECT ra.username, ra.nasipaddress, ra.framedipaddress, ra.callingstationid, ra.acctstarttime, "
             "ra.acctstoptime, ra.acctsessiontime, ra.acctinputoctets, ra.acctoutputoctets, ra.acctterminatecause "
             "FROM radacct ra" + cond + " ORDER BY ra.radacctid DESC LIMIT %s OFFSET %s",
             params + [per + 1, (page - 1) * per])
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("logs_sessions.html", rows=rows[:per], page=page, has_next=len(rows) > per, keep=keep,
                           start=start, end=end)
