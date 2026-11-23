"""The owner's daily report: one day's money, customers, dues, tickets and follow-ups.

No Flask here: the page (Accounting > Daily report, mod_ops.py) and the Telegram morning message (alertlib.py) both
call build() with an open cursor, so the two always show the same numbers."""
from datetime import date, timedelta


def _one(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone() or {}


def _all(cur, sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


def build(cur, day):
    """Everything for `day` (a date). The "now" figures (online, dues, expiring, open tickets) are as of now."""
    start, end = day, day + timedelta(days=1)
    d = {"day": day}
    p = _one(cur, "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total, SUM(days_added > 0) AS renewals "
                  "FROM payments WHERE paid_at >= %s AND paid_at < %s", (start, end))
    d["pay"] = {"n": int(p.get("n") or 0), "total": float(p.get("total") or 0), "renewals": int(p.get("renewals") or 0)}
    d["by_method"] = [{"method": r["method"] or "other", "n": int(r["n"]), "total": float(r["total"])} for r in _all(
        cur, "SELECT method, COUNT(*) AS n, SUM(amount) AS total FROM payments WHERE paid_at >= %s AND paid_at < %s "
             "GROUP BY method ORDER BY total DESC", (start, end))]
    d["by_person"] = [{"who": r["received_by"] or "—", "n": int(r["n"]), "total": float(r["total"])} for r in _all(
        cur, "SELECT received_by, COUNT(*) AS n, SUM(amount) AS total FROM payments WHERE paid_at >= %s AND paid_at < %s "
             "GROUP BY received_by ORDER BY total DESC LIMIT 15", (start, end))]
    try:
        e = _one(cur, "SELECT COALESCE(SUM(CASE WHEN kind = 'expense' THEN amount END), 0) AS spent, "
                      "COALESCE(SUM(CASE WHEN kind = 'income' THEN amount END), 0) AS other_in FROM expenses WHERE spent_on = %s",
                 (day,))
        d["spent"], d["other_in"] = float(e.get("spent") or 0), float(e.get("other_in") or 0)
    except Exception:                 # an older database without the Cashflow columns
        d["spent"], d["other_in"] = 0.0, 0.0
    d["net"] = round(d["pay"]["total"] + d["other_in"] - d["spent"], 2)
    c = _one(cur, "SELECT SUM(created_at >= %s AND created_at < %s) AS new_c, COUNT(*) AS total, "
                  "SUM(status = 'active' AND expiry_date < CURDATE()) AS dues_n, "
                  "SUM(status = 'active' AND expiry_date = CURDATE()) AS exp_today, "
                  "SUM(status = 'active' AND expiry_date = CURDATE() + INTERVAL 1 DAY) AS exp_tomorrow, "
                  "SUM(status = 'active' AND expiry_date BETWEEN CURDATE() + INTERVAL 2 DAY AND CURDATE() + INTERVAL 3 DAY) AS exp_3 "
                  "FROM subscribers", (start, end))
    d["subs"] = {k: int(c.get(k) or 0) for k in ("new_c", "total", "dues_n", "exp_today", "exp_tomorrow", "exp_3")}
    due = _one(cur, "SELECT COALESCE(SUM(p.price), 0) AS amount FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
                    "WHERE s.status = 'active' AND s.expiry_date < CURDATE()")
    d["subs"]["dues_amount"] = float(due.get("amount") or 0)
    d["online"] = int(_one(cur, "SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL").get("c") or 0)
    t = _one(cur, "SELECT SUM(created_at >= %s AND created_at < %s) AS opened, SUM(closed_at >= %s AND closed_at < %s) AS closed, "
                  "SUM(status IN ('open', 'pending')) AS open_now FROM tickets", (start, end, start, end))
    d["tickets"] = {k: int(t.get(k) or 0) for k in ("opened", "closed", "open_now")}
    try:
        f = _one(cur, "SELECT SUM(done = 0 AND follow_up <= CURDATE()) AS due, SUM(created_at >= %s AND created_at < %s) AS logged "
                      "FROM sub_calls WHERE done = 0 OR (created_at >= %s AND created_at < %s)", (start, end, start, end))
        d["calls"] = {"due": int(f.get("due") or 0), "logged": int(f.get("logged") or 0)}
    except Exception:                 # sub_calls appears after update.sh has applied the schema
        d["calls"] = {"due": 0, "logged": 0}
    return d


def text(d, isp, currency):
    """The Telegram message: yesterday's numbers and what today holds."""
    m = lambda v: "{:,.2f}".format(v)
    lines = ["📊 %s daily report, %s" % (isp, d["day"].strftime("%d %b %Y")),
             "💰 Collected: %s %s (%d payments, %d renewals)" % (currency, m(d["pay"]["total"]), d["pay"]["n"], d["pay"]["renewals"])]
    if d["by_method"]:
        lines.append("   " + " · ".join("%s %s" % (r["method"].capitalize(), m(r["total"])) for r in d["by_method"][:5]))
    if d["spent"] or d["other_in"]:
        lines.append("🧾 Expenses: %s %s · Other income: %s · Net: %s" % (currency, m(d["spent"]), m(d["other_in"]), m(d["net"])))
    if d["by_person"]:
        lines.append("👤 Top collectors: " + ", ".join("%s %s" % (r["who"], m(r["total"])) for r in d["by_person"][:3]))
    s = d["subs"]
    lines += ["👥 New customers: %d · Online now: %d of %d" % (s["new_c"], d["online"], s["total"]),
              "⏳ Expiring today: %d · tomorrow: %d · in 2-3 days: %d" % (s["exp_today"], s["exp_tomorrow"], s["exp_3"]),
              "🔴 Unpaid (expired): %d customers, %s %s to collect" % (s["dues_n"], currency, m(s["dues_amount"])),
              "🎫 Tickets: %d opened, %d closed, %d still open" % (d["tickets"]["opened"], d["tickets"]["closed"], d["tickets"]["open_now"])]
    if d["calls"]["due"]:
        lines.append("📞 Follow-up calls due: %d" % d["calls"]["due"])
    return "\n".join(lines)


def yesterday():
    return date.today() - timedelta(days=1)
