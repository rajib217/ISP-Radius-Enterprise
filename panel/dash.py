"""Data for the tabs of the home page (only the tab that is open is calculated)."""
import threading
import time
from datetime import date, timedelta

import core
import db
from core import n, q

TABS = [("subscriber", "Subscriber", "subscribers"), ("accounting", "Accounting", "accounting"),
        ("user", "User", "reports"), ("predictions", "Predictions", "subscribers"),
        ("usage", "Usage", "reports"), ("sales", "Sales & Payment Graphs", "accounting"),
        ("cashflow", "Cashflow Reports", "accounting")]

ONLINE = "(SELECT username FROM radacct WHERE acctstoptime IS NULL)"


def _days(back):
    today = date.today()
    return [today - timedelta(days=i) for i in range(back - 1, -1, -1)]


def _series(rows, days, key, field):
    by = {r[key]: r for r in rows}
    return [float((by.get(d) or {}).get(field) or 0) for d in days]


def subscriber():
    sc, sp = core.scope_sql("s")
    st = q("SELECT COUNT(*) AS total, "
           "COALESCE(SUM(" + db.RUNNING + "), 0) AS active, "
           "COALESCE(SUM(" + db.ENDED + "), 0) AS expired, "
           "COALESCE(SUM(s.status = 'suspended'), 0) AS disabled, "
           "COALESCE(SUM(s.username IN " + ONLINE + "), 0) AS online, "
           "COALESCE(SUM(" + db.ENDED + " AND NOT " + db.GRACE_ON + " AND s.username IN " + ONLINE + "), 0) "
           "AS expired_online, "
           "COALESCE(SUM(s.status = 'suspended' AND s.username IN " + ONLINE + "), 0) AS disabled_online, "
           "COALESCE(SUM(" + db.expiring_sql(1) + "), 0) AS exp1, "
           "COALESCE(SUM(" + db.expiring_sql(3) + "), 0) AS exp3, "
           "COALESCE(SUM(" + db.expiring_sql(7) + "), 0) AS exp7 "
           "FROM subscribers s WHERE 1 = 1" + sc, sp, one=True)
    v = {k: n(st[k]) for k in ("total", "active", "expired", "disabled", "online", "expired_online", "disabled_online",
                                   "exp1", "exp3", "exp7")}
    if core.my_downline() is None:
        v["unknown_online"] = n(q("SELECT COUNT(DISTINCT username) AS c FROM radacct WHERE acctstoptime IS NULL "
                                  "AND username NOT IN (SELECT username FROM subscribers)", one=True)["c"])
    else:
        v["unknown_online"] = None
    v["offline"] = max(v["total"] - v["online"], 0)
    t = v["total"] or 1

    def pct(x):
        return "%.2f%%" % (100.0 * x / t)

    cards = [
        ("Total", v["total"], "All subscribers", "100.00%" if v["total"] else "0.00%", "violet", {}),
        ("Active", v["active"], "Active accounts", pct(v["active"]), "green", {"status": "active"}),
        ("Online", v["online"], "Currently connected", pct(v["online"]), "green", {"status": "online"}),
        ("Offline", v["offline"], "Not connected", pct(v["offline"]), "red", {"status": "offline"}),
        ("Expired", v["expired"], "Expired accounts", pct(v["expired"]), "red", {"status": "expired"}),
        ("Disabled", v["disabled"], "Suspended accounts", pct(v["disabled"]), "amber", {"status": "suspended"}),
        ("Expired online", v["expired_online"], "Expired but still connected", "", "amber", {"status": "expired_online"}),
        ("Disabled online", v["disabled_online"], "Suspended but still connected", "", "amber",
         {"status": "profile_disabled_online"}),
        ("Expiring in 7 days", v["exp7"], "Active, expire within a week", "", "amber", {"status": "exp7"}),
        ("Expiring in 3 days", v["exp3"], "Active, expire within 3 days", "", "amber", {"status": "exp3"}),
        ("Expiring in 1 day", v["exp1"], "Active, expire today or tomorrow", "", "red", {"status": "exp1"}),
    ]
    if v["unknown_online"] is not None:
        cards.append(("Unknown online", v["unknown_online"], "Connected, not in the panel", "", "rose", {}))
    pie = {"items": [{"name": "Active, online", "value": max(v["online"] - v["expired_online"] - v["disabled_online"], 0)},
                     {"name": "Active, offline", "value": max(v["active"] - (v["online"] - v["expired_online"] - v["disabled_online"]), 0)},
                     {"name": "Expired", "value": v["expired"]}, {"name": "Disabled", "value": v["disabled"]}],
           "center": "subscribers"}
    return {"cards": cards, "pie": pie}


def accounting():
    today, first = date.today(), date.today().replace(day=1)
    inc = q("SELECT COALESCE(SUM(CASE WHEN paid_at >= %s THEN amount ELSE 0 END), 0) AS today_total, "
            "COALESCE(SUM(amount), 0) AS month_total, COUNT(*) AS c FROM payments WHERE paid_at >= %s",
            (today, first), one=True)
    exp = q("SELECT COALESCE(SUM(IF(kind = 'expense', amount, 0)), 0) AS month_total, "
            "COALESCE(SUM(IF(kind = 'income', amount, 0)), 0) AS other_income FROM expenses WHERE spent_on >= %s", (first,), one=True)
    due = q("SELECT COUNT(*) AS c, COALESCE(SUM(" + db.DUE_SQL + "), 0) AS amount FROM subscribers s WHERE " + db.ENDED, one=True)
    by_method = q("SELECT method, COUNT(*) AS c, SUM(amount) AS total FROM payments WHERE paid_at >= %s "
                  "GROUP BY method ORDER BY total DESC", (first,))
    cards = [("Collected today", inc["today_total"], "payments received", "", "green", {}),
             ("Collected this month", inc["month_total"], "%d payments" % n(inc["c"]), "", "violet", {}),
             ("Expenses this month", exp["month_total"], "money spent", "", "red", {}),
             ("Net this month", float(inc["month_total"] or 0) + float(exp["other_income"] or 0) - float(exp["month_total"] or 0), "income minus expenses", "", "amber", {}),
             ("Dues", due["amount"], "%d expired customers" % n(due["c"]), "", "rose", {})]
    return {"cards": cards, "by_method": by_method, "money": True}


def user():
    first = date.today().replace(day=1)
    rows = q("SELECT a.username, a.full_name, a.role, "
             "(SELECT COUNT(*) FROM subscribers s WHERE s.created_by = a.username) AS subs, "
             "(SELECT COUNT(*) FROM payments p WHERE p.received_by = a.username AND p.paid_at >= %s) AS payments, "
             "(SELECT COALESCE(SUM(p.amount), 0) FROM payments p WHERE p.received_by = a.username "
             "AND p.paid_at >= %s) AS amount FROM panel_admins a WHERE a.is_active = 1 ORDER BY amount DESC, a.username",
             (first, first))
    roles = {}
    for r in q("SELECT role, COUNT(*) AS c FROM panel_admins WHERE is_active = 1 GROUP BY role"):
        roles[r["role"]] = r["c"]
    cards = [(core.role_labels().get(k, k), c, "active", "", "violet", {}) for k, c in roles.items()]
    return {"cards": cards, "staff": rows}


def predictions():
    days = [date.today() + timedelta(days=i) for i in range(14)]
    rows = q("SELECT s.expiry_date AS d, COUNT(*) AS c, COALESCE(SUM(p.price), 0) AS amount FROM subscribers s "
             "LEFT JOIN plans p ON p.id = s.plan_id WHERE " + db.RUNNING + " AND s.expiry_date >= CURDATE() "
             "AND s.expiry_date < CURDATE() + INTERVAL 14 DAY GROUP BY s.expiry_date")
    tot = q("SELECT COALESCE(SUM(s.expiry_date <= CURDATE() + INTERVAL 7 DAY), 0) AS c7, "
            "COALESCE(SUM(CASE WHEN s.expiry_date <= CURDATE() + INTERVAL 7 DAY THEN p.price ELSE 0 END), 0) AS a7, "
            "COUNT(*) AS c30, COALESCE(SUM(p.price), 0) AS a30 FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
            "WHERE " + db.RUNNING + " AND s.expiry_date >= CURDATE() AND s.expiry_date <= CURDATE() + INTERVAL 30 DAY",
            one=True)
    cards = [("Expiring in 7 days", tot["c7"], "customers to renew", "", "amber", {"status": "expiring"}),
             ("Expected in 7 days", tot["a7"], "if all renew", "", "green", {}),
             ("Expiring in 30 days", tot["c30"], "customers to renew", "", "violet", {}),
             ("Expected in 30 days", tot["a30"], "if all renew", "", "green", {})]
    chart = {"labels": [d.strftime("%d %b") for d in days], "unit": "number",
             "series": [{"name": "Accounts expiring", "values": _series(rows, days, "d", "c")}]}
    return {"cards": cards, "charts": [("Accounts expiring per day, next 14 days", "pred", chart)]}


def usage():
    days = _days(14)
    rows = q("SELECT DATE(acctstarttime) AS d, SUM(acctinputoctets) AS up, SUM(acctoutputoctets) AS down FROM radacct "
             "WHERE acctstarttime >= CURDATE() - INTERVAL 13 DAY GROUP BY DATE(acctstarttime)")
    tot = q("SELECT COALESCE(SUM(acctinputoctets), 0) AS up, COALESCE(SUM(acctoutputoctets), 0) AS down, "
            "COUNT(*) AS sessions FROM radacct WHERE acctstarttime >= CURDATE()", one=True)
    chart = {"labels": [d.strftime("%d %b") for d in days],
             "series": [{"name": "Download", "values": _series(rows, days, "d", "down")},
                        {"name": "Upload", "values": _series(rows, days, "d", "up")}]}
    top = q("SELECT username, SUM(acctinputoctets + acctoutputoctets) AS total FROM radacct "
            "WHERE acctstarttime >= CURDATE() GROUP BY username ORDER BY total DESC LIMIT 10")
    return {"cards": [("Downloaded today", tot["down"], "bytes", "", "green", {}),
                      ("Uploaded today", tot["up"], "bytes", "", "violet", {}),
                      ("Sessions today", tot["sessions"], "started today", "", "amber", {})],
            "bytes": True, "charts": [("Traffic, last 14 days", "usage", chart)], "top": top}


def sales():
    days = _days(30)
    pay = q("SELECT DATE(paid_at) AS d, SUM(amount) AS total FROM payments WHERE paid_at >= CURDATE() - INTERVAL 29 DAY "
            "GROUP BY DATE(paid_at)")
    new = q("SELECT DATE(created_at) AS d, COUNT(*) AS c FROM subscribers WHERE created_at >= CURDATE() - INTERVAL 29 DAY "
            "GROUP BY DATE(created_at)")
    labels = [d.strftime("%d %b") for d in days]
    return {"cards": [], "charts": [
        ("Collection per day, last 30 days", "sales1", {"labels": labels, "unit": "money",
         "series": [{"name": "Collected", "values": _series(pay, days, "d", "total")}]}),
        ("New customers per day, last 30 days", "sales2", {"labels": labels, "unit": "number", "colors": ["--c2"],
         "series": [{"name": "New customers", "values": _series(new, days, "d", "c")}]})]}


def cashflow():
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
        "SELECT DATE_FORMAT(paid_at, '%%Y-%%m') AS ym, SUM(amount) AS t FROM payments WHERE paid_at >= %s GROUP BY ym",
        (first,))}
    exp = {r["ym"]: float(r["t"] or 0) for r in q(
        "SELECT DATE_FORMAT(spent_on, '%%Y-%%m') AS ym, SUM(amount) AS t FROM expenses WHERE kind = 'expense' AND spent_on >= %s "
        "GROUP BY ym", (first,))}
    for r in q("SELECT DATE_FORMAT(spent_on, '%%Y-%%m') AS ym, SUM(amount) AS t FROM expenses WHERE kind = 'income' "
               "AND spent_on >= %s GROUP BY ym", (first,)):
        inc[r["ym"]] = inc.get(r["ym"], 0) + float(r["t"] or 0)
    keys = ["%04d-%02d" % ym for ym in months]
    chart = {"labels": [date(y, m, 1).strftime("%b %y") for y, m in months], "unit": "money", "colors": ["--c1", "--c4"],
             "series": [{"name": "Income", "values": [inc.get(k, 0) for k in keys]},
                        {"name": "Expenses", "values": [exp.get(k, 0) for k in keys]}]}
    return {"cards": [], "charts": [("Income and expenses, last 12 months", "cash", chart)],
            "link": ("Full income and expense report", "acct_summary")}


LOAD = {"subscriber": subscriber, "accounting": accounting, "user": user, "predictions": predictions,
        "usage": usage, "sales": sales, "cashflow": cashflow}

# Seconds to reuse a tab's numbers. The home page is the most opened page, and "usage" sums the whole day of
# radacct (millions of rows after a year at 20,000 customers), so recalculating it on every load adds up.
# Money tabs are not cached, so a payment shows up at once.
CACHE_SECONDS = {"subscriber": 30, "usage": 120, "predictions": 300, "user": 120}
_cache = {}
_cache_lock = threading.Lock()


def load(tab):
    """LOAD[tab](), reused for CACHE_SECONDS[tab] per worker. Keyed by the viewer's scope,
    so a reseller never sees numbers calculated for another reseller or for an admin."""
    ttl = CACHE_SECONDS.get(tab)
    if not ttl:
        return LOAD[tab]()
    sc, sp = core.scope_sql("s")
    key = (tab, sc, tuple(sp), date.today())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    data = LOAD[tab]()
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (now, data)
    return data


# ===================================================================== v3 dashboard (staff view)
TRAFFIC_RANGES = {            # key: (look-back, bucket seconds, label format)
    "1h": ("1 HOUR", 300, "%H:%M"),
    "24h": ("24 HOUR", 300, "%H:%M"),
    "7d": ("7 DAY", 3600, "%a %d"),
    "30d": ("30 DAY", 6 * 3600, "%d %b"),
}


def traffic_series(rng):
    """Download / upload throughput (bits per second) for the bandwidth chart. [] while nothing is recorded yet."""
    back, bucket, fmt = TRAFFIC_RANGES.get(rng, TRAFFIC_RANGES["24h"])
    try:
        rows = q("SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(ts) / %s) * %s) AS b, AVG(down_bps) AS down, AVG(up_bps) AS up "
                 "FROM traffic_snapshots WHERE ts >= NOW() - INTERVAL " + back + " GROUP BY b ORDER BY b",
                 (bucket, bucket))
    except Exception:          # table not created yet: install.sh has not been re-run after the upgrade
        rows = []
    return {"unit": "bps", "labels": [r["b"].strftime(fmt) for r in rows],
            "stamps": [r["b"].strftime("%d %b %Y, %H:%M") for r in rows],
            "series": [{"name": "Download", "color": "--accent", "values": [float(r["down"] or 0) for r in rows]},
                       {"name": "Upload", "color": "--online", "values": [float(r["up"] or 0) for r in rows]}]}


def throughput_now():
    """Latest total throughput, or None when no sample is younger than 15 minutes."""
    try:
        r = q("SELECT ts, down_bps, up_bps, sessions FROM traffic_snapshots WHERE ts >= NOW() - INTERVAL 15 MINUTE "
              "ORDER BY ts DESC LIMIT 1", one=True)
    except Exception:
        return None
    return r


def revenue_6m():
    """Collected vs. not collected for the last 6 months.

    'Expected' is what all active customers' packages cost, counting customers who existed by the end of that month.
    Older months are an estimate: the panel does not keep who was active in the past, only who is active now."""
    today = date.today()
    months = []
    for back in range(5, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))
    first = date(months[0][0], months[0][1], 1)
    paid = {r["ym"]: float(r["t"] or 0) for r in q(
        "SELECT DATE_FORMAT(paid_at, '%%Y-%%m') AS ym, SUM(amount) AS t FROM payments WHERE paid_at >= %s GROUP BY ym",
        (first,))}
    expected = {}
    for y, m in months:
        end = date(y + (m == 12), m % 12 + 1, 1)
        r = q("SELECT COALESCE(SUM(p.price), 0) AS t FROM subscribers s JOIN plans p ON p.id = s.plan_id "
              "WHERE s.status = 'active' AND s.created_at < %s", (end,), one=True)
        expected["%04d-%02d" % (y, m)] = float(r["t"] or 0)
    keys = ["%04d-%02d" % ym for ym in months]
    collected = [paid.get(k, 0.0) for k in keys]
    unpaid = [max(expected[k] - paid.get(k, 0.0), 0.0) for k in keys]
    return {"unit": "money", "colors": ["--online", "--offline"],
            "labels": [date(y, m, 1).strftime("%b %y") for y, m in months],
            "series": [{"name": "Collected", "values": collected}, {"name": "Not collected (est.)", "values": unpaid}]}


def status_mix():
    """Non-overlapping split for the donut: every subscriber is in exactly one slice."""
    r = q("SELECT COUNT(*) AS total, "
          "COALESCE(SUM(" + db.RUNNING + "), 0) AS active, "
          "COALESCE(SUM(" + db.RUNNING + " AND s.username IN " + ONLINE + "), 0) AS active_online, "
          "COALESCE(SUM(" + db.ENDED + "), 0) AS expired, "
          "COALESCE(SUM(s.status = 'suspended'), 0) AS disabled FROM subscribers s", one=True)
    v = {k: n(r[k]) for k in ("total", "active", "active_online", "expired", "disabled")}
    return {"center": "subscribers", "items": [
        {"name": "Active, online", "value": v["active_online"], "color": "--online", "status": "online"},
        {"name": "Active, offline", "value": max(v["active"] - v["active_online"], 0), "color": "--accent", "status": "offline"},
        {"name": "Expired", "value": v["expired"], "color": "--offline", "status": "expired"},
        {"name": "Disabled", "value": v["disabled"], "color": "--neutral", "status": "suspended"}]}


def growth_pct(total):
    """Subscriber growth over 30 days: customers added in that time against those who were there before."""
    before = n(q("SELECT COUNT(*) AS c FROM subscribers WHERE created_at < NOW() - INTERVAL 30 DAY", one=True)["c"])
    if not before:
        return None
    return round(100.0 * (total - before) / before, 1)


def dues_total():
    """What customers owe now (each one's own price and charges, less their balance: db.OWED_SQL, as on their page and
    Dues), and how many have an ended package."""
    a = q("SELECT COALESCE(SUM(" + db.OWED_SQL + "), 0) AS t, COALESCE(SUM(" + db.ENDED + "), 0) AS c FROM subscribers s", one=True)
    return {"amount": float(a["t"] or 0), "customers": n(a["c"])}


TAB_FN = {"subscriber": "home.subs", "accounting": "home.acc", "user": "home.users", "predictions": "home.pred",
          "usage": "home.usage", "sales": "home.g_sales", "cashflow": "home.g_pay"}     # Users > Roles > Home


def allowed_tabs():
    return [(k, label) for k, label, mod in TABS if core.can(mod) and core.can_fn(TAB_FN[k])
            and not (core.my_downline() is not None and k != "subscriber")]
