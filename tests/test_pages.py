"""Open every page of the panel against a real MariaDB with sample data, as each kind of user.

Used by GitHub Actions (.github/workflows/deploy.yml, job "pages") before any deploy, and runnable by hand:

    DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=radius_panel DB_PASS=... DB_NAME=radius python3 tests/test_pages.py

The database must already have the FreeRADIUS schema and sql/panel_schema.sql loaded (the workflow does that).
It is filled with sample data here, so never point this at a production database: the script refuses to run when
the database already has customers.

A page fails when it answers 500 or raises. 403 (no permission) and 404 (no such row) are fine. Nothing leaves the
machine: subprocess calls (ping, radclient, sudo) and outgoing HTTP are blocked for the run.
Exit code 0 = all pages fine, 1 = at least one page failed (each is printed, and annotated on GitHub).
"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
import types
import urllib.request
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANEL = os.path.join(ROOT, "panel")
sys.path.insert(0, PANEL)
os.chdir(PANEL)
os.environ.setdefault("PANEL_SECRET_KEY", "ci-pages")
os.environ.setdefault("PANEL_DATA_DIR", os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "panel-ci-data"))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
if os.name == "nt":                      # only for running it on a Windows workstation
    if not hasattr(os, "getloadavg"):
        os.getloadavg = lambda: (0.5, 0.4, 0.3)
    for _m in ("fcntl", "pwd", "grp", "resource"):
        sys.modules.setdefault(_m, types.ModuleType(_m))

import db          # noqa: E402
import radius      # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

TODAY = date.today()
NOW = datetime.now().replace(microsecond=0)
GH = os.environ.get("GITHUB_ACTIONS") == "true"


# ===================================================================== sample data
def align_collations(conn, cur):
    """Same step as install.sh / update.sh: panel tables must use radcheck's collation, or joins on username fail."""
    cur.execute("SELECT DATABASE() AS d"); dbname = cur.fetchone()["d"]
    cur.execute("SELECT table_collation AS c FROM information_schema.tables WHERE table_schema = %s AND table_name = 'radcheck'", (dbname,))
    want = cur.fetchone()["c"]
    # exactly the tables install.sh converts, so a table missing from its list shows up here as a failing page
    src = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
    tables = re.search(r"if \[\[ \"\$RC_COLL\" =~ .*?\n\s*for t in ([^;]+); do", src, re.S).group(1).split()
    for t in tables:
        cur.execute("SELECT table_collation AS c FROM information_schema.tables WHERE table_schema = %s AND table_name = %s", (dbname, t))
        r = cur.fetchone()
        if r and r["c"] != want:
            cur.execute("ALTER TABLE `%s` CONVERT TO CHARACTER SET utf8mb4 COLLATE %s" % (t, want))
    conn.commit()


def seed(conn):
    cur = conn.cursor()
    align_collations(conn, cur)
    cur.execute("SELECT COUNT(*) AS c FROM subscribers")
    if cur.fetchone()["c"]:
        sys.exit("The database already has customers: this test only runs on an empty test database.")

    def ins(sql, args=()):
        cur.execute(sql, args)
        return cur.lastrowid

    for k, v in {"isp_name": "Dhaka Fiber Net", "currency": "BDT", "portal_on": "1", "sub_self_activation": "1",
                 "support_phone": "01712345678", "company_address": "House 5, Uttara"}.items():
        db.save_setting(cur, k, v)

    pw = generate_password_hash("Ci-Pages-123")
    admin = ins("INSERT INTO panel_admins (username, full_name, password_hash, role, phone, email, inv_name) "
                "VALUES ('admin', 'Rafiqul Islam', %s, 'admin', '01700000001', 'admin@example.com', 'Dhaka Fiber Net')", (pw,))
    reseller = ins("INSERT INTO panel_admins (username, full_name, password_hash, role, phone, balance, parent_id, created_by) "
                   "VALUES ('karim', 'Karim Telecom', %s, 'reseller', '01700000002', 5000, %s, 'admin')", (pw, admin))
    subres = ins("INSERT INTO panel_admins (username, full_name, password_hash, role, balance, parent_id, created_by) "
                 "VALUES ('karim_sub', 'Karim Sub', %s, 'subreseller', 800, %s, 'karim')", (pw, reseller))
    staff = ins("INSERT INTO panel_admins (username, full_name, password_hash, role) VALUES ('nadia', 'Nadia Staff', %s, 'support')", (pw,))

    plans = []
    for name, label, mbps, price in (("home10", "10 Mbps Home", 10, 500), ("home20", "20 Mbps Home", 20, 800),
                                     ("pro50", "50 Mbps Pro", 50, 1500)):
        pid = ins("INSERT INTO plans (name, label, download_mbps, upload_mbps, price, validity_days, pool_name) "
                  "VALUES (%s, %s, %s, %s, %s, 30, 'pppoe-pool')", (name, label, mbps, mbps, price))
        cur.execute("SELECT * FROM plans WHERE id = %s", (pid,))
        plan = cur.fetchone()
        radius.sync_plan(cur, plan, "300")
        plans.append(plan)
        if len(plans) <= 2:          # the reseller is allocated 10 and 20 Mbps only; the sub-reseller nothing
            cur.execute("INSERT INTO user_plans (user_id, plan_id, price) VALUES (%s, %s, %s)", (reseller, pid, price - 100))

    group = ins("INSERT INTO nas_groups (name, note) VALUES ('Dhaka North', 'Mirpur and Uttara')")
    for ip, short in (("10.10.0.1", "MT-Mirpur"), ("10.20.0.1", "MT-Uttara")):
        ins("INSERT INTO nas (nasname, shortname, type, secret, description, group_id, coa_enabled) "
            "VALUES (%s, %s, 'mikrotik', 'ci-secret-1', 'CI router', %s, 1)", (ip, short, group))

    people = [  # username, name, area, plan index, state, created_by
        ("mirpur.rahim", "Rahim Uddin", "Mirpur-10", 0, "active", "admin"),
        ("uttara.salma", "Salma Akter", "Uttara-7", 1, "active", "admin"),
        ("gulshan.office", "Gulshan Trading Ltd", "Gulshan-1", 2, "expiring", "admin"),
        ("dhanmondi.tanvir", "Tanvir Hasan", "Dhanmondi-27", 0, "expired", "admin"),
        ("mirpur.nusrat", "Nusrat Jahan", "Mirpur-2", 1, "suspended", "admin"),
        ("banani.cafe", "Banani Cafe", "Banani-11", 2, "active", "karim"),
        ("badda.jewel", "Jewel Rana", "Badda", 0, "expired", "karim_sub"),
        ("uttara.shop", "Sector 14 Shop", "Uttara-14", 1, "active", "karim"),
    ]
    subs = []
    for k, (user, name, area, pi, state, by) in enumerate(people):
        exp = {"active": TODAY + timedelta(days=20), "expiring": TODAY + timedelta(days=2),
               "expired": TODAY - timedelta(days=4), "suspended": TODAY + timedelta(days=15)}[state]
        status = "suspended" if state == "suspended" else "active"
        sid = ins("INSERT INTO subscribers (username, full_name, phone, address, area, plan_id, status, expiry_date, created_by, "
                  "nas_ip, mac_bind) VALUES (%s, %s, %s, 'Road 7, House 12', %s, %s, %s, %s, %s, %s, %s)",
                  (user, name, "0171%07d" % (k + 1), area, plans[pi]["id"], status, exp, by, "10.10.0.1" if k % 2 == 0 else "10.20.0.1",
                   "A4:5E:60:1B:22:9C" if k == 2 else ""))
        radius.sync_subscriber(cur, user, plans[pi], status, exp, None, password="pw-%s" % user)
        subs.append((sid, user, plans[pi], exp))
        ins("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, period_to, received_by) "
            "VALUES (%s, %s, %s, 30, 'cash', 'CI payment', %s, %s)", (sid, user, plans[pi]["price"], exp, by))

    for k, (sid, user, plan, exp) in enumerate(subs):
        nas = "10.10.0.1" if k % 2 == 0 else "10.20.0.1"
        start = NOW - timedelta(hours=k + 1)
        cur.execute("INSERT INTO radacct (acctsessionid, acctuniqueid, username, nasipaddress, nasportid, acctstarttime, acctupdatetime, "
                    "acctstoptime, acctsessiontime, acctinputoctets, acctoutputoctets, callingstationid, framedipaddress, "
                    "acctterminatecause, servicetype, framedprotocol) VALUES (%s, %s, %s, %s, 'ether1', %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "'Framed-User', 'PPP')",
                    ("S%05d" % k, "U%05d" % k, user, nas, start, NOW, None if k < 5 else NOW - timedelta(minutes=5),
                     3600 * (k + 1), 2 ** 28 * (k + 1), 2 ** 30 * (k + 1), "A4:5E:60:00:00:%02X" % k, "100.64.0.%d" % (k + 2),
                     "" if k < 5 else "User-Request"))
        cur.execute("INSERT INTO radpostauth (username, pass, reply, authdate) VALUES (%s, '', %s, %s)",
                    (user, "Access-Accept" if k % 3 else "Access-Reject", NOW - timedelta(minutes=k)))
    try:
        cur.execute("UPDATE radpostauth SET reason = 'rlm_pap: Cleartext password does not match', nas_ip = '10.10.0.1', "
                    "mac = 'A4:5E:60:00:00:01' WHERE reply = 'Access-Reject'")
    except Exception:
        pass

    for i in range(48):
        ts = NOW - timedelta(minutes=5 * i)
        cur.execute("INSERT IGNORE INTO stats_snapshots (ts, online) VALUES (%s, %s)", (ts, 5 + i % 3))
        cur.execute("INSERT IGNORE INTO traffic_snapshots (ts, down_bps, up_bps, sessions) VALUES (%s, %s, %s, 5)",
                    (ts, 80000000 + i * 100000, 15000000))
    cur.execute("INSERT INTO nas_health (nas_id, checked_at, reachable, rtt_ms, snmp_ok, cpu_pct, ram_pct, uptime_s, board, pppoe) "
                "VALUES (1, NOW(), 1, 1.4, 1, 23, 41, 3456000, 'RouterOS CCR1036', 3), (2, NOW(), 0, NULL, 0, NULL, NULL, NULL, '', 0)")
    db.save_setting(cur, "noc_heartbeat", NOW.strftime("%Y-%m-%d %H:%M:%S"))
    ins("INSERT INTO portal_payments (subscriber_id, amount, fee, days, token, payment_id, trx_id, status) "
        "VALUES (1, 500, 7.5, 30, %s, 'TR0011CI', 'BKCI1', 'completed')", ("k" * 32,))
    ins("INSERT INTO topups (user_id, amount, fee, token, payment_id, status) VALUES (%s, 1000, 15, %s, 'TR0011CJ', 'pending')",
        (reseller, "t" * 32))
    ins("INSERT INTO gateway_logs (flow, ref_id, payment_id, step, http_status, ms, ok, request, response) "
        "VALUES ('portal', 1, 'TR0011CI', 'execute', 200, 640, 1, '{}', '{\"transactionStatus\": \"Completed\"}')")
    ins("INSERT INTO audit_log (admin, action, target, detail, ip) VALUES ('admin', 'renewed', 'mirpur.rahim', '+30 days', '127.0.0.1')")
    try:
        fee = ins("INSERT INTO fees (name, mode, value, active) VALUES ('VAT', 'percent', 5, 1)")
        ins("INSERT INTO plan_fees (plan_id, fee_id) VALUES (%s, %s)", (plans[1]["id"], fee))
    except Exception as exc:
        print("  (fees not seeded: %s)" % exc)
    conn.commit()
    autofill(conn, cur)
    conn.commit()
    return {"admin": admin, "reseller": reseller, "subreseller": subres, "staff": staff, "portal_sub": subs[0][0]}


def autofill(conn, cur):
    """Give every still-empty panel table two plain rows, so detail pages (/x/1) have something to show."""
    cur.execute("SELECT DATABASE() AS d")
    dbname = cur.fetchone()["d"]
    cur.execute("SELECT table_name AS t FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE'", (dbname,))
    for t in [r["t"] for r in cur.fetchall()]:
        cur.execute("SELECT COUNT(*) AS c FROM `%s`" % t)
        if cur.fetchone()["c"] or t.startswith("rad") or t in ("settings", "nas", "nasreload"):
            continue
        cur.execute("SELECT column_name AS c, data_type AS dt, column_type AS ct, is_nullable AS n, column_default AS d, extra AS x "
                    "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position", (dbname, t))
        cols = cur.fetchall()
        for row in (1, 2):
            names, vals = [], []
            for c in cols:
                if "auto_increment" in (c["x"] or ""):
                    continue
                name, dt = c["c"], c["dt"]
                if dt == "enum":
                    v = re.findall(r"'([^']*)'", c["ct"])[0]
                elif dt in ("int", "bigint", "smallint", "tinyint", "mediumint"):
                    v = 1 if dt == "tinyint" else row
                elif dt in ("decimal", "float", "double"):
                    v = 100 * row
                elif dt in ("date",):
                    v = TODAY
                elif dt in ("datetime", "timestamp"):
                    v = NOW - timedelta(hours=row)
                elif dt in ("text", "mediumtext", "longtext"):
                    v = "CI sample %d" % row
                else:  # varchar / char
                    size = int(re.search(r"\((\d+)\)", c["ct"]).group(1)) if "(" in c["ct"] else 16
                    v = ("%d-ci-%s" % (row, name))[:size]       # row number first: unique even when cut short
                names.append("`%s`" % name)
                vals.append(v)
            try:
                cur.execute("INSERT INTO `%s` (%s) VALUES (%s)" % (t, ", ".join(names), ", ".join(["%s"] * len(vals))), vals)
            except Exception as exc:
                print("  (autofill skipped %s: %s)" % (t, str(exc)[:80]))
                conn.rollback()
                break


# ===================================================================== business rules
def one(sql, args=()):
    conn = db.connect()        # a fresh connection sees what the app committed
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        conn.close()


def run_q(sql, args=()):
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def run(sql, args=()):
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def check_rules(flask_app, ids):
    """Package limits for sellers and the money rules of Activation. Returns a list of failure sentences."""
    bad = []

    def check(ok, what):
        print("  %s %s" % ("ok  " if ok else "FAIL", what))
        if not ok:
            bad.append(what)

    def client(uid):
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "ci"; s["uid"] = uid
        return c

    names = {r: one("SELECT label FROM plans WHERE id = %s", (r,))["label"] for r in (1, 2, 3)}
    adm_pkgs = client(ids["admin"]).get("/my-packages").get_data(as_text=True)
    adm_home = client(ids["admin"]).get("/").get_data(as_text=True)
    # ---- sellers: only allocated packages, no package pages
    res, sub = client(ids["reseller"]), client(ids["subreseller"])
    for url in ("/plans", "/plans/new", "/plans/1/edit", "/policy", "/allocation", "/fees", "/reports/plans"):
        check(res.get(url).status_code == 403, "reseller is refused %s" % url)
    check(res.post("/plans/save", data={"_csrf": "ci", "name": "x"}).status_code == 403, "reseller cannot save a package")
    page = res.get("/my-packages").get_data(as_text=True)
    check(names[1] in page and names[2] in page and names[3] not in page, "reseller's My Packages shows only the 2 allocated")
    cost1 = float(one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = 1", (ids["reseller"],))["price"])
    shown = ("BDT %s" % "{:,.0f}".format(cost1)) in page or ("%.2f" % cost1) in page   # money shows 400 or 400.00
    check("All customers" not in page and "My cost" in page and shown,
          "reseller's My Packages shows their own cost (%.2f), not the ISP-wide customer count" % cost1)
    check("All customers" in adm_pkgs, "admin's My Packages still shows the ISP-wide customer count")
    act = res.get("/subscribers/activation").get_data(as_text=True)
    check(names[1] in act and names[3] not in act, "reseller's Activation lists only allocated packages")
    check('href="/plans"' not in res.get("/").get_data(as_text=True), "reseller's menu has no Package group")
    page = sub.get("/my-packages").get_data(as_text=True)
    check(all(names[k] not in page for k in names), "sub-reseller with nothing allocated sees no package")
    info = res.get("/subscribers/charge-info?u=banani.cafe&plan=3").get_json()
    check(info.get("price") is None, "reseller gets no price for an unallocated package")

    # ---- sellers' dashboard: their own customers expiring within 7 days
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 3 DAY, status = 'active' WHERE username = 'uttara.shop'")
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 1 DAY, status = 'active' WHERE username = 'badda.jewel'")
    home = res.get("/").get_data(as_text=True)
    check("Expiring soon subscribers" in home and "uttara.shop" in home and "badda.jewel" in home and "by karim_sub" in home,
          "reseller dashboard lists their own and their sub-reseller's expiring customers")
    check("gulshan.office" not in home, "reseller dashboard does not show the admin's expiring customer")
    home = sub.get("/").get_data(as_text=True)
    check("badda.jewel" in home and "uttara.shop" not in home, "sub-reseller dashboard lists only their own expiring customers")
    check("Expiring soon subscribers" not in adm_home and "gulshan.office" in adm_home, "admin dashboard keeps its own Expiring soon list")
    home = res.get("/").get_data(as_text=True)
    check(home.find("Reports &amp; Statistics") < home.find("Expiring soon subscribers"),
          "the seller list comes after Reports & Statistics")
    conn = db.connect()
    with conn.cursor() as cur:
        for k in range(23):                    # 23 more of the reseller's customers expiring this week: 25 in all
            cur.execute("INSERT INTO subscribers (username, full_name, status, expiry_date, created_by, plan_id) "
                        "VALUES (%s, 'Paging test', 'active', CURDATE() + INTERVAL %s DAY, 'karim', 1)", ("pg.user%02d" % k, k % 7))
    conn.commit(); conn.close()
    def rows(html):
        part = html[html.find('id="my-expiring"'):]
        part = part[:part.find("</section>")]
        return part.count("<li>")
    p1, p2 = res.get("/").get_data(as_text=True), res.get("/?page=2").get_data(as_text=True)
    check(rows(p1) == 20 and rows(p2) == 5 and "21–25 of 25" in p2,
          "20 expiring customers a page (page 1: %d, page 2: %d)" % (rows(p1), rows(p2)))

    # ---- activation money rules
    adm = client(ids["admin"])

    def activate(c, user, plan_id, **form):
        data = {"_csrf": "ci", "username": user, "plan_id": str(plan_id)}
        data.update(form)
        return c.post("/subscribers/activate", data=data)

    def state(user):
        r = one("SELECT id, balance, expiry_date FROM subscribers WHERE username = %s", (user,))
        p = one("SELECT amount, method, note FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (r["id"],))
        n = one("SELECT COUNT(*) AS c FROM payments WHERE subscriber_id = %s", (r["id"],))["c"]
        return float(r["balance"]), r["expiry_date"], p, n

    u = "uttara.salma"                                   # admin's customer, package 2 (800 + its taxes / fees)
    price = float(adm.get("/subscribers/charge-info?u=%s&plan=2" % u).get_json()["price"])
    plain = float(one("SELECT price FROM plans WHERE id = 2")["price"])
    check(price >= plain, "customer price %.2f includes the package price %.2f and its fees" % (price, plain))
    run("UPDATE subscribers SET balance = 1000 WHERE username = %s", (u,))
    b0, e0, _, n0 = state(u)
    activate(adm, u, 2)                                   # Payment switch off
    b1, e1, p1, n1 = state(u)
    check(abs(b1 - (1000 - price)) < 0.01 and e1 > e0 and n1 == n0 + 1 and p1["method"] == "balance" and abs(float(p1["amount"]) - price) < 0.01,
          "Payment off: package price taken from the subscriber balance (1000 -> %.2f)" % b1)
    activate(adm, u, 2, payment="on", method="cash", amount=str(price + 200))
    b2, e2, p2, _ = state(u)
    check(abs(b2 - (b1 + 200)) < 0.01 and e2 > e1 and p2["method"] == "cash" and abs(float(p2["amount"]) - price) < 0.01,
          "cash above the price: the extra 200 goes to the balance (%.2f -> %.2f)" % (b1, b2))
    activate(adm, u, 2, payment="on", method="cash", amount=str(price - 100))
    b3, e3, p3, _ = state(u)
    check(abs(b3 - (b2 - 100)) < 0.01 and e3 > e2 and p3["method"] == "cash+balance",
          "cash below the price: the missing 100 comes from the balance (%.2f -> %.2f)" % (b2, b3))
    run("UPDATE subscribers SET balance = 50 WHERE username = %s", (u,))
    b4, e4, _, n4 = state(u)
    activate(adm, u, 2, payment="on", method="balance")
    b5, e5, _, n5 = state(u)
    check(b5 == b4 and e5 == e4 and n5 == n4, "balance too low: refused, nothing changed")
    activate(adm, u, 2, payment="on", method="cash", amount="100")
    b6, e6, _, n6 = state(u)
    check(b6 == b4 and e6 == e4 and n6 == n4, "cash plus balance still short: refused, nothing changed")

    # ---- secrets are never written into settings pages
    run("INSERT INTO settings (skey, svalue) VALUES ('nat_token', 'tok-SECRET-123') ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)")
    page = adm.get("/settings/log-engine").get_data(as_text=True)
    check("tok-SECRET-123" not in page and "Saved: leave empty" in page, "Log Engine API token is not written into the page")
    adm.post("/settings/log-engine", data={"_csrf": "ci", "nat_url": "http://127.0.0.1:8080", "nat_token": "", "nat_export_days": "7",
                                     "nat_export_rows": "100000", "authlog_days": "30", "archive_months": "12"})
    check(one("SELECT svalue FROM settings WHERE skey = 'nat_token'")["svalue"] == "tok-SECRET-123", "saving with an empty token box keeps the token")
    adm.post("/settings/log-engine", data={"_csrf": "ci", "nat_url": "http://127.0.0.1:8080", "nat_token": "", "clear_token": "1",
                                     "nat_export_days": "7", "nat_export_rows": "100000", "authlog_days": "30", "archive_months": "12"})
    check((one("SELECT svalue FROM settings WHERE skey = 'nat_token'") or {}).get("svalue", "") == "", "the tick box removes the saved token")
    run("INSERT INTO settings (skey, svalue) VALUES ('bkash_secret', 'bk-SECRET-9'), ('bkash_pass', 'bk-PASS-9') "
        "ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)")
    page = adm.get("/settings/gateway/bkash").get_data(as_text=True)
    check("bk-SECRET-9" not in page and "bk-PASS-9" not in page, "bKash secret and password are not written into the page")

    # ---- expiry time
    import noc
    def exp_row(user):
        r = one("SELECT expiry_date, expiry_time FROM subscribers WHERE username = %s", (user,))
        e = one("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Expiration'", (user,))
        return r["expiry_date"], r["expiry_time"], (e or {}).get("value", "")
    t = "dhanmondi.tanvir"                               # expired admin customer, package 1
    price1 = adm.get("/subscribers/charge-info?u=%s&plan=1" % t).get_json()["price"]
    before = datetime.now()
    activate(adm, t, 1, payment="on", method="cash", amount=str(price1))
    d, tm, exp = exp_row(t)
    now_ok = tm is not None and abs((datetime.combine(TODAY, datetime.strptime(tm, "%H:%M").time()) - before.replace(second=0, microsecond=0)).total_seconds()) <= 120
    check(now_ok and exp.endswith(tm + ":00"), "activation ends at the time of activation (%s, RADIUS: %s)" % (tm, exp))
    activate(adm, t, 1, payment="on", method="cash", amount=str(price1))
    d2, tm2, _ = exp_row(t)
    check(d2 > d and tm2 == tm, "renewing a running account keeps its time (%s -> %s, %s)" % (d, d2, tm2))
    target = (TODAY + timedelta(days=90)).isoformat()
    activate(adm, t, 1, payment="on", method="cash", amount="9999", custom="on", expiry=target, expiry_time="15:45")
    d3, tm3, exp3 = exp_row(t)
    check(d3.isoformat() == target and tm3 == "15:45" and exp3.endswith("15:45:00"),
          "custom expiry keeps the typed date and time (%s %s, RADIUS: %s)" % (d3, tm3, exp3))
    activate(adm, t, 1, payment="on", method="cash", amount="9999", custom="on", expiry=target, expiry_time="25:99")
    check(exp_row(t)[1] == "15:45", "an invalid time is refused")
    run("UPDATE subscribers SET expiry_date = CURDATE(), expiry_time = NULL, grace_until = NULL WHERE username = 'mirpur.rahim'")
    due = one("SELECT COUNT(*) AS c FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.username = 'mirpur.rahim' "
              "AND " + noc.END_MOMENT + " <= NOW()")["c"]
    check(due == 0 or datetime.now().strftime("%H:%M") >= "23:59", "auto-disconnect waits until the end of the last day (not midnight)")
    past = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M")
    run("UPDATE subscribers SET expiry_time = %s WHERE username = 'mirpur.rahim'", (past,))
    due = one("SELECT COUNT(*) AS c FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.username = 'mirpur.rahim' "
              "AND " + noc.END_MOMENT + " <= NOW()")["c"]
    check(due == 1 or past > datetime.now().strftime("%H:%M"), "auto-disconnect starts at the subscriber's own expiry time")

    r = "banani.cafe"                                    # the reseller's customer
    run("UPDATE subscribers SET plan_id = 1, balance = 0 WHERE username = %s", (r,))
    cost = float(one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = 1", (ids["reseller"],))["price"])
    rb0 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    info = res.get("/subscribers/charge-info?u=%s&plan=1" % r).get_json()
    activate(res, r, 1, payment="on", method="cash", amount=str(info["price"] + 50))
    sb, _, sp, _ = state(r)
    rb1 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    check(abs(rb1 - (rb0 - cost)) < 0.01 and abs(sb - 50) < 0.01,
          "reseller activation: reseller pays %.2f, the customer's extra 50 goes to their balance" % cost)
    activate(res, r, 3, payment="on", method="cash", amount="9999")
    check(abs(float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"]) - rb1) < 0.01,
          "reseller cannot activate an unallocated package")

    # ---- Ledger Reports
    led = adm.get("/ledger").get_data(as_text=True)
    check(all(t in led for t in ("led-type t-invoice", "led-type t-profit", "led-type t-package-cost", "led-type t-topup", "banani.cafe", "karim")),
          "admin ledger shows invoices, profit, package cost and balance top-ups")
    check(res.get("/ledger").status_code == 403 and 'href="/ledger"' not in res.get("/").get_data(as_text=True),
          "reseller without the Ledger permission is refused and has no menu link")
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'led.module', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    res2 = client(ids["reseller"])
    r = res2.get("/ledger")
    page = r.get_data(as_text=True)
    check(r.status_code == 200 and 'href="/ledger"' in res2.get("/").get_data(as_text=True), "with the permission the reseller opens the ledger")
    def side_menu(html):
        return html.split('<nav class="nav"', 1)[-1].split("</nav>", 1)[0]
    for who, cl in (("admin", adm), ("reseller", res2)):
        m = side_menu(cl.get("/").get_data(as_text=True))
        acct = m.split('data-key="acct"', 1)[-1].split("</details>", 1)[0] if 'data-key="acct"' in m else ""
        check(m.count('href="/ledger"') == 1 and 'href="/ledger"' in acct,
              "%s menu shows Ledger Reports once, under Accounting" % who)
    check("banani.cafe" in page and "led-type t-package-cost" in page and "uttara.salma" not in page,
          "reseller sees their own and their customers' entries, not the admin's customers")
    check(sub.get("/ledger").status_code == 403, "the sub-reseller role still needs its own permission")
    check(res2.get("/payments").status_code == 403 and res2.get("/dues").status_code == 403,
          "the Ledger tick does not open the rest of Accounting (Payments, Dues)")
    check(res2.get("/ledger.csv").status_code == 403, "export needs the Export tick")
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'led.export', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    res2 = client(ids["reseller"])
    prof = adm.get("/ledger?type=profit").get_data(as_text=True)
    check("led-type t-profit" in prof and "led-type t-package-cost" not in prof and "led-type t-invoice" not in prof,
          "type filter shows only that type")
    c = res2.get("/ledger.csv")
    x = res2.get("/ledger.xlsx")
    import zipfile as _zf, io as _io
    try:
        sheet = _zf.ZipFile(_io.BytesIO(x.data)).read("xl/worksheets/sheet1.xml").decode()
    except Exception:
        sheet = ""
    check(c.status_code == 200 and "banani.cafe" in c.get_data(as_text=True) and x.status_code == 200 and "banani.cafe" in sheet,
          "CSV and Excel exports work and hold the reseller's entries")
    cost_row = one("SELECT balance_after FROM user_ledger WHERE user_id = %s AND method = 'activation' ORDER BY id DESC LIMIT 1", (ids["reseller"],))
    check(cost_row is not None and ("{:,.2f}".format(float(cost_row["balance_after"]))) in page,
          "package cost row shows the reseller's balance after the charge")

    # ---- bKash Payments (seed: one completed portal renewal TR0011CI, one pending reseller top-up TR0011CJ)
    bk = adm.get("/bkash-payments").get_data(as_text=True)
    check("TR0011CI" in bk and "TR0011CJ" in bk and "BKCI1" in bk and "Completed" in bk and "Pending" in bk,
          "admin sees portal renewals and reseller top-ups with Trx ID and status")
    bk = adm.get("/bkash-payments?status=completed").get_data(as_text=True)
    check("TR0011CI" in bk and "TR0011CJ" not in bk, "status filter shows only completed payments")
    run("DELETE FROM role_funcs WHERE role = 'reseller' AND fkey LIKE 'bkp.%%'")
    res3 = client(ids["reseller"])
    check(res3.get("/bkash-payments").status_code == 403 and 'href="/bkash-payments"' not in res3.get("/").get_data(as_text=True),
          "reseller without the bKash Payments permission is refused and has no menu link")
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'bkp.module', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    res3 = client(ids["reseller"])
    bk = res3.get("/bkash-payments").get_data(as_text=True)
    check("TR0011CJ" in bk and "TR0011CI" not in bk, "with the permission the reseller sees their own top-ups, not the admin's customers")
    for who, cl in (("admin", adm), ("reseller", res3)):
        m = side_menu(cl.get("/").get_data(as_text=True))
        acct = m.split('data-key="acct"', 1)[-1].split("</details>", 1)[0] if 'data-key="acct"' in m else ""
        check(m.count('href="/bkash-payments"') == 1 and 'href="/bkash-payments"' in acct,
              "%s menu shows bKash Payments once, under Accounting" % who)
    check(res3.get("/payments").status_code == 403 and res3.get("/bkash-payments.csv").status_code == 403,
          "the bKash tick does not open Payments, and export needs its own tick")
    c, x = adm.get("/bkash-payments.csv"), adm.get("/bkash-payments.xlsx")
    check(c.status_code == 200 and "TR0011CI" in c.get_data(as_text=True) and x.status_code == 200 and x.data[:2] == b"PK",
          "admin CSV and Excel exports work")

    # ---- bKash amount check fails closed (no DB rows touched: mark/audit/commit are recorded, not run)
    import bkash as _bk
    _marks, _audits = [], []
    _orig = (_bk.mark, _bk.core.audit, _bk.core.commit, _bk.call, _bk.token)
    _bk.mark = lambda flow, rid, status, msg: _marks.append((flow, rid, status, msg))
    _bk.core.audit = lambda *a, **k: _audits.append(a)
    _bk.core.commit = lambda: None
    try:
        _row = {"id": 999001, "amount": 500, "fee": 7.5, "payment_id": "PAYX"}
        check(_bk._amount_ok("portal", _row, {"amount": "507.50", "trxID": "T1"}) and not _marks,
              "exact bKash amount (500 + 7.5 fee) is accepted")
        check(not _bk._amount_ok("portal", _row, {"trxID": "T2"}) and _marks[-1][2] == "failed"
              and "WITHOUT AN AMOUNT" in _marks[-1][3], "bKash answer without an amount is refused, not credited")
        check(not _bk._amount_ok("topup", _row, {"amount": 0, "trxID": "T3"}), "zero bKash amount is refused")
        check(not _bk._amount_ok("topup", _row, {"amount": "abc", "trxID": "T4"}), "unreadable bKash amount is refused")
        check(not _bk._amount_ok("portal", _row, {"amount": 100, "trxID": "T5"})
              and "BUT 507.50 WAS EXPECTED" in _marks[-1][3], "wrong bKash amount is refused")
        _bk.token = lambda: "t"
        _bk.call = lambda step, body, **k: ({"statusCode": "0000", "transactionStatus": "Completed", "trxID": "T6"}
                                            if step == "execute" else
                                            {"statusCode": "0000", "transactionStatus": "Completed",
                                             "trxID": "T6", "amount": "507.50"})
        _state, _data = _bk.confirm("PAYX", "portal", 999001)
        check(_state == "completed" and _bk._paid(_data) == 507.5,
              "execute without an amount falls back to the status answer, which carries it")
    finally:
        _bk.mark, _bk.core.audit, _bk.core.commit, _bk.call, _bk.token = _orig

    # ---- TOTP two-factor (RFC 6238 test vector) and settings encryption
    import totp as _totp
    check(_totp._hotp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1) == "287082",
          "TOTP matches the RFC 6238 test vector (t=59)")
    _sec = _totp.new_secret()
    check(_totp.verify(_sec, _totp._hotp(_sec, int(time.time()) // 30))
          and not _totp.verify(_sec, "000000" if _totp._hotp(_sec, int(time.time()) // 30) != "000000" else "111111"),
          "a fresh TOTP secret verifies its own code and refuses a wrong one")
    _codes = _totp.new_backup_codes()
    check(len(_codes) == 8 and _totp.hash_backup_code(_codes[0]) != _codes[0]
          and _totp.hash_backup_code(_codes[0].replace("-", "")) == _totp.hash_backup_code(_codes[0]),
          "backup codes are generated and hashed (dashes ignored)")
    try:
        from cryptography.fernet import Fernet as _Fernet
        os.environ["PANEL_SETTINGS_KEY"] = _Fernet.generate_key().decode()
    except Exception:
        os.environ["PANEL_SETTINGS_KEY"] = "not-a-real-key"
    db._fernet, db._fernet_tried = None, False
    if db._get_fernet() is not None:
        _enc = db.encrypt_value("s3cret")
        check(_enc.startswith("enc:v1:") and db.decrypt_value(_enc) == "s3cret"
              and db.decrypt_value("plain") == "plain",
              "secret settings encrypt/decrypt round-trip; plaintext passes through")
    else:
        check(db.encrypt_value("s3cret") == "s3cret",
              "without cryptography the settings encryption falls back to plaintext")
    os.environ.pop("PANEL_SETTINGS_KEY", None)
    db._fernet, db._fernet_tried = None, False
    check(adm.get("/login/2fa").status_code == 302,
          "/login/2fa without a pending sign-in redirects to /login")
    check("Two-factor authentication" in adm.get("/profile").get_data(as_text=True),
          "profile page offers two-factor setup")
    check(db.decrypt_value("enc:v1:gAAAAnot-readable") == "",
          "an encrypted setting that cannot be read comes back empty, never as the ciphertext")
    check(db.unreadable_secrets([("smtp_pass", "enc:v1:xyz"), ("isp_name", "Dhaka")]) == ["smtp_pass"],
          "unreadable encrypted settings are listed for the dashboard")

    # full sign-in with two-factor on: password -> code page -> dashboard; the same code cannot be used twice
    _s2 = _totp.new_secret()
    run("UPDATE panel_admins SET totp_secret = %s, totp_enabled = 1, totp_last_step = 0, totp_backup_codes = %s WHERE id = %s",
        (_s2, json.dumps([_totp.hash_backup_code("1111-2222")]), ids["staff"]))
    _uname = one("SELECT username FROM panel_admins WHERE id = %s", (ids["staff"],))["username"]

    def _tf_login():
        lc = flask_app.test_client()
        with lc.session_transaction() as s:
            s["csrf"] = "ci"
        r1 = lc.post("/login", data={"_csrf": "ci", "username": _uname, "password": "Ci-Pages-123"})
        with lc.session_transaction() as s:
            tok = s.get("csrf", "ci")
        return lc, r1, tok
    lc, r1, tok = _tf_login()
    page2 = lc.get("/login/2fa")
    check(r1.headers.get("Location", "").endswith("/login/2fa") and page2.status_code == 200
          and "Two-factor code" in page2.get_data(as_text=True),
          "with two-factor on, the password leads to the code page (reachable before sign-in)")
    wrong = lc.post("/login/2fa", data={"_csrf": tok, "code": "12345"})
    check(wrong.status_code == 200 and "Wrong code" in wrong.get_data(as_text=True), "a wrong code is refused")
    _code = _totp._hotp(_s2, int(time.time()) // 30)
    good = lc.post("/login/2fa", data={"_csrf": tok, "code": _code})
    check(good.status_code == 302 and "/login" not in good.headers.get("Location", "")
          and lc.get("/profile").status_code == 200, "the right code signs in")
    lc2, _, tok2 = _tf_login()
    again = lc2.post("/login/2fa", data={"_csrf": tok2, "code": _code})
    check(again.status_code == 200 and "Wrong code" in again.get_data(as_text=True), "the same code cannot be used twice")
    lc3, _, tok3 = _tf_login()
    bk1 = lc3.post("/login/2fa", data={"_csrf": tok3, "code": "1111-2222"})
    lc4, _, tok4 = _tf_login()
    bk2 = lc4.post("/login/2fa", data={"_csrf": tok4, "code": "1111-2222"})
    check(bk1.status_code == 302 and bk2.status_code == 200, "a backup code works once")
    st = client(ids["staff"])
    off = st.post("/profile", data={"_csrf": "ci", "section": "2fa_disable", "current": "Ci-Pages-123", "code": "000000"})
    check(one("SELECT totp_enabled FROM panel_admins WHERE id = %s", (ids["staff"],))["totp_enabled"] == 1,
          "switching two-factor off needs a valid code, not only the password")
    run("UPDATE panel_admins SET totp_secret = '', totp_enabled = 0, totp_backup_codes = NULL, totp_last_step = 0 WHERE id = %s",
        (ids["staff"],))
    run("DELETE FROM audit_log WHERE action = '2fa_failed'")

    # ---- customer portal pages (signed in as a customer) and the portal bottom nav
    pc = flask_app.test_client()
    with pc.session_transaction() as s:
        s["csrf"] = "ci"; s["cid"] = one("SELECT id FROM subscribers WHERE username = 'uttara.shop'")["id"]
    p_home, p_pay = pc.get("/portal"), pc.get("/portal/pay")
    p_html = p_home.get_data(as_text=True)
    check(p_home.status_code == 200 and p_pay.status_code in (200, 302) and 'class="pt-bottom-nav"' in p_html
          and 'href="/portal#invoices"' in p_html and 'id="invoices"' in p_html and 'id="usage"' in p_html,
          "customer portal and pay page open, with the bottom nav and its #usage / #invoices targets")
    _psid = one("SELECT id FROM subscribers WHERE username = 'uttara.shop'")["id"]
    run("INSERT INTO sub_charges (subscriber_id, name, amount, created_by) VALUES (%s, 'CI portal router fee', 250, 'admin')", (_psid,))
    pc.post("/portal/photo/save", data={"_csrf": "ci", "remove": "1"})      # no photo left from an earlier run
    _ph = pc.get("/portal").get_data(as_text=True)
    _photo_anon = flask_app.test_client().get("/portal/photo")
    check(all('id="%s"' % k in _ph for k in ("overview", "invoices", "ledger", "payments", "account"))
          and 'class="pp-logo"' in _ph and 'class="ph-stats"' in _ph and "CI portal router fee" in _ph
          and "Unpaid" in _ph and 'data-l="Debit"' in _ph and 'data-cards="off"' in _ph and 'href="/portal#ledger"' in _ph
          and pc.get("/portal/photo").status_code == 404 and _photo_anon.status_code in (302, 404)
          and 'class="pp-logo"' in flask_app.test_client().get("/portal/login").get_data(as_text=True),
          "portal redesign: logo, profile tiles, Overview / Invoices / Ledger / Payments / My details tabs, extra charges show as unpaid")
    run("DELETE FROM sub_charges WHERE name = 'CI portal router fee'")
    # ---- portal sign-in stays until Sign out (customers only); staff keep the 12-hour limit
    _ss = flask_app.session_interface.get_signing_serializer(flask_app)
    _cookie_name = flask_app.config.get("SESSION_COOKIE_NAME", "session")

    def _old_cookie(data, hours):
        _real, _then = time.time, time.time() - hours * 3600
        time.time = lambda: _then
        try:
            return _ss.dumps(dict(data, _permanent=True))
        finally:
            time.time = _real

    def _client_with(val):
        c = flask_app.test_client()
        c.set_cookie(_cookie_name, val)
        return c

    _upw = one("SELECT value FROM radcheck WHERE username = 'uttara.shop' AND attribute = 'Cleartext-Password'")["value"]

    def _portal_login(c):
        with c.session_transaction() as _s:
            _s["csrf"] = "ci"
        return c.post("/portal/login", data={"_csrf": "ci", "username": "uttara.shop", "password": _upw})

    _lc = flask_app.test_client()
    _lr = _portal_login(_lc)
    _sc = _lr.headers.get("Set-Cookie", "")
    _exp = re.search(r"Expires=([^;]+)", _sc)
    _exp_days = int(((datetime.strptime(_exp.group(1).strip(), "%a, %d %b %Y %H:%M:%S GMT") - datetime(1970, 1, 1)).total_seconds()
                     - time.time()) // 86400) if _exp else 0
    _adm_id = one("SELECT id FROM panel_admins WHERE username = 'admin'")["id"]
    _cust_old = _client_with(_old_cookie({"cid": _psid, "csrf": "ci"}, 24 * 30)).get("/portal")
    _staff_old = _client_with(_old_cookie({"uid": _adm_id, "csrf": "ci"}, 13)).get("/")
    _staff_new = _client_with(_old_cookie({"uid": _adm_id, "csrf": "ci"}, 1)).get("/")
    _both = _client_with(_old_cookie({"uid": _adm_id, "cid": _psid, "csrf": "ci"}, 13))
    _both_portal, _both_staff = _both.get("/portal"), _both.get("/")
    check(_lr.status_code == 302 and _exp_days >= 390 and _cust_old.status_code == 200
          and _staff_old.status_code == 302 and "/login" in _staff_old.headers.get("Location", "")
          and _staff_new.status_code == 200 and _both_portal.status_code == 200
          and _both_staff.status_code == 302 and "/login" in _both_staff.headers.get("Location", ""),
          "a customer stays signed in (400-day cookie, still in after 30 days away); staff are still signed out after 12 hours")
    _lc.get("/portal")
    run("UPDATE radcheck SET value = 'ci-new-pass' WHERE username = 'uttara.shop' AND attribute = 'Cleartext-Password'")
    _after_pw = _lc.get("/portal")
    run("UPDATE radcheck SET value = %s WHERE username = 'uttara.shop' AND attribute = 'Cleartext-Password'", (_upw,))
    _lc2 = flask_app.test_client()
    _portal_login(_lc2)
    with _lc2.session_transaction() as _s2:
        _csrf2 = _s2.get("csrf", "")
    _in = _lc2.get("/portal").status_code
    _lc2.post("/portal/logout", data={"_csrf": _csrf2})
    _out = _lc2.get("/portal")
    check(_after_pw.status_code == 302 and _in == 200 and _out.status_code == 302,
          "a portal sign-in ends when the password is changed, and when the customer presses Sign out")
    run("DELETE FROM audit_log WHERE action IN ('portal_login', 'portal_login_failed')")
    # ---- portal: live speed JSON, own photo upload / remove
    _rate = pc.get("/portal/rate")
    _png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    import io as _io
    _up = pc.post("/portal/photo/save", data={"_csrf": "ci", "file": (_io.BytesIO(_png), "me.png")},
                  content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    _got = pc.get("/portal/photo")
    _got_ok = _got.status_code == 200 and _got.data[1:4] == b"PNG"
    _got.close()
    _bad = pc.post("/portal/photo/save", data={"_csrf": "ci", "file": (_io.BytesIO(b"GIF89a....."), "x.gif")},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    _home_ph = pc.get("/portal").get_data(as_text=True)
    _rm = pc.post("/portal/photo/save", data={"_csrf": "ci", "remove": "1"})
    check(_rate.status_code == 200 and set(_rate.get_json()) >= {"online", "known", "down", "up"}
          and flask_app.test_client().get("/portal/rate").status_code in (302, 404)
          and _up.get_json() == {"ok": True, "error": None} and _got.status_code == 200 and _got.data.startswith(b"\x89PNG")
          and _bad.status_code == 400 and 'data-rate-src="/portal/rate"' in _home_ph and 'src="/portal/photo?v=' in _home_ph and _rm.status_code == 302 and pc.get("/portal/photo").status_code == 404,
          "portal: live speed chart data, and the customer can upload (PNG/JPG only) and remove their own photo")

    # ---- a reseller's customer pays in the portal with bKash: money to the reseller's balance, reseller cost taken, renewed
    import bkash as _bk
    _sh = one("SELECT * FROM subscribers WHERE username = 'uttara.shop'")
    _res_before = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _cost = float(one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _sh["plan_id"]))["price"])
    run("REPLACE INTO settings (skey, svalue) VALUES ('bkash_on', '1'), ('bkash_key', 'k'), ('bkash_secret', 's'), "
        "('bkash_user', 'u'), ('bkash_pass', 'p'), ('sub_self_activation', '1')")
    _terms_html = pc.get("/portal").get_data(as_text=True)
    with flask_app.test_request_context("/portal"):
        import mod_portal as _mp
        _t = _mp.pay_terms(dict(_sh, plan_price=one("SELECT price FROM plans WHERE id = %s", (_sh["plan_id"],))["price"],
                                validity_days=30))
    run("INSERT INTO portal_payments (subscriber_id, amount, fee, days, token, payment_id) VALUES (%s, %s, 0, 30, 'citok', 'CIPAY1')",
        (_sh["id"], _t["amount"]))
    _row = one("SELECT * FROM portal_payments WHERE token = 'citok'")
    with flask_app.test_request_context("/portal"):
        _done = _bk.settle_portal(_row, {"trxID": "CITRX1", "amount": "%.2f" % _t["amount"]})
        _again = _bk.settle_portal(_row, {"trxID": "CITRX1", "amount": "%.2f" % _t["amount"]})
    _res_after = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _sh2 = one("SELECT expiry_date, status FROM subscribers WHERE id = %s", (_sh["id"],))
    _led = run_q("SELECT amount, method FROM user_ledger WHERE user_id = %s ORDER BY id DESC LIMIT 2", (ids["reseller"],))
    check(_t["ok"] and _t["seller"] and "btn-bkash" in _terms_html and _done and not _again
          and abs(_res_after - (_res_before + _t["amount"] - _cost)) < 0.01
          and sorted((float(r["amount"]), r["method"]) for r in _led) == sorted([(_t["amount"], "bkash"), (-_cost, "activation")])
          and _sh2["status"] == "active" and _sh2["expiry_date"] > (_sh["expiry_date"] or date.today()),
          "a reseller's customer pays by bKash in the portal: reseller balance +%.2f -%.2f (cost), customer renewed, paid only once" % (_t["amount"], _cost))
    run("DELETE FROM settings WHERE skey IN ('bkash_on', 'bkash_key', 'bkash_secret', 'bkash_user', 'bkash_pass')")
    _slash = flask_app.test_client().get("/portal/")
    _junk = flask_app.test_client().get("/portal/nothing-here")
    check(_slash.status_code == 302 and _slash.headers.get("Location", "").endswith("/portal")
          and "/login" not in _junk.headers.get("Location", ""),
          "/portal/ (with the slash) goes to the portal, and unknown portal addresses never land on the staff sign-in")
    run("REPLACE INTO settings (skey, svalue) VALUES ('portal_on', '0')")
    _off_anon = flask_app.test_client().get("/portal")
    _off_staff = adm.get("/portal")
    run("REPLACE INTO settings (skey, svalue) VALUES ('portal_on', '1')")
    check(_off_anon.status_code == 404 and "গ্রাহক পোর্টাল এখন বন্ধ" in _off_anon.get_data(as_text=True)
          and "Customer portal is switched off" in _off_staff.get_data(as_text=True)
          and "/settings/subscriber" in _off_staff.get_data(as_text=True),
          "a switched-off portal explains itself (customers: not available; staff: where to turn it on)")
    # the Customer Portal switch saves even when another field on the form is bad (old serial prefix with letters)
    run("REPLACE INTO settings (skey, svalue) VALUES ('portal_on', '0'), ('serial_prefix', 'RN-01')")
    _sv = adm.post("/settings/subscriber", data={"_csrf": "ci", "portal_on": "on", "grace_days": "500", "serial_prefix": "RN-01"})
    _sv_page = adm.get("/settings/subscriber").get_data(as_text=True)
    check(_sv.status_code == 302 and one("SELECT svalue FROM settings WHERE skey = 'portal_on'")["svalue"] == "1"
          and one("SELECT svalue FROM settings WHERE skey = 'serial_prefix'")["svalue"] == "RN-01"
          and "Serial Number Start" in _sv_page and "novalidate" in _sv_page
          and flask_app.test_client().get("/portal").status_code == 302,
          "Settings > Subscriber: Customer Portal turns on even with a bad serial number / grace days (those keep their old value and are named)")
    run("REPLACE INTO settings (skey, svalue) VALUES ('serial_prefix', '')")
    # the switch alone saves on flip (fetch), only for admins
    _st = flask_app.test_client()
    with _st.session_transaction() as _s:
        _s["csrf"] = "ci"; _s["uid"] = ids["staff"]
    _f_off = adm.post("/settings/subscriber/portal", data={"_csrf": "ci", "on": "0"}, headers={"X-Requested-With": "fetch"})
    _off_db = one("SELECT svalue FROM settings WHERE skey = 'portal_on'")["svalue"]
    _f_staff = _st.post("/settings/subscriber/portal", data={"_csrf": "ci", "on": "1"}, headers={"X-Requested-With": "fetch"})
    _f_on = adm.post("/settings/subscriber/portal", data={"_csrf": "ci", "on": "1"}, headers={"X-Requested-With": "fetch"})
    check(_f_off.get_json() == {"ok": True, "on": False} and _off_db == "0" and _f_staff.status_code == 403
          and _f_on.get_json() == {"ok": True, "on": True}
          and one("SELECT svalue FROM settings WHERE skey = 'portal_on'")["svalue"] == "1"
          and 'data-save="/settings/subscriber/portal"' in adm.get("/settings/subscriber").get_data(as_text=True),
          "Settings > Subscriber: the Customer Portal switch saves the moment it is flipped (admins only)")
    _css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel", "static", "css", "app.css"), encoding="utf-8").read()
    _sw = re.search(r'\.switch input\[type="checkbox"\] \{([^}]*)\}', _css)
    check(bool(_sw) and "width: 100%" in _sw.group(1) and "height: 100%" in _sw.group(1)
          and _css.rindex('.switch input[type="checkbox"] {') > _css.index('input[type="checkbox"], input[type="radio"] {'),
          "every on/off switch answers a click anywhere on it (the 16px checkbox default does not shrink it)")
    _js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel", "static", "js", "app.js"), encoding="utf-8").read()
    _base = adm.get("/subscribers").get_data(as_text=True)
    check("var MENU_WAIT = 5000;" in _js and '"mouseenter"' in _js and "navIdle" in _js and "hover-menu" in _base
          and 'id="i-users3"' in _base and re.search(r'data-key="subs"[^>]*>\s*<summary[^>]*><svg class="i"[^>]*><use href="#i-users3"', _base),
          "menu: submenus open on hover and close 5 s later (desktop), the phone menu hides 5 s after the last touch; Subscribers has the three-person icon")
    # ---- Settings > General: Logo & Favicon card (upload shows "Custom", remove goes back to built-in)
    import io as _bio
    _set0 = adm.get("/settings").get_data(as_text=True)
    _png1 = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(64)
    _upl = adm.post("/settings/brand/favicon", data={"_csrf": "ci", "file": (_bio.BytesIO(_png1), "f.png")},
                    content_type="multipart/form-data")
    _set1 = adm.get("/settings").get_data(as_text=True)
    _rmv = adm.post("/settings/brand/favicon", data={"_csrf": "ci", "remove": "1"})
    _set2 = adm.get("/settings").get_data(as_text=True)
    check('id="branding"' in _set0 and _set0.count('enctype="multipart/form-data"') >= 2 and "brand-dlg" not in _set0
          and _upl.status_code == 302 and 'src="/branding/favicon?v=' in _set1 and _rmv.status_code == 302
          and 'src="/branding/favicon?v=' not in _set2,
          "Settings > General has a Logo & Favicon card: upload shows the new icon, Remove goes back to the built-in one")

    # ---- dashboard: Offline ONUs (Offline + LOS), sellers only see the ONUs on their own OLTs
    run("INSERT INTO net_devices (name, kind, ip, created_by) VALUES ('CI-OLT-Admin', 'olt', '10.9.0.1', 'admin')")
    _olt_a = one("SELECT id FROM net_devices WHERE name = 'CI-OLT-Admin'")["id"]
    run("INSERT INTO net_devices (name, kind, ip, created_by) VALUES ('CI-OLT-Karim', 'olt', '10.9.0.2', 'karim')")
    _olt_k = one("SELECT id FROM net_devices WHERE name = 'CI-OLT-Karim'")["id"]
    _shop_u = one("SELECT username FROM subscribers WHERE created_by = 'karim' LIMIT 1")["username"]
    run("INSERT INTO onus (olt_id, name, serial, username, pon_port, rx_dbm, status, last_seen) VALUES "
        "(%s, 'ONU-CI-LOS', 'ZTEG0001', '', '0/1', NULL, 'los', NOW() - INTERVAL 5 MINUTE), "
        "(%s, 'ONU-CI-OFF', 'ZTEG0002', '', '0/2', -27.10, 'offline', NOW() - INTERVAL 1 HOUR), "
        "(%s, 'ONU-CI-UP', 'ZTEG0003', '', '0/3', -20.00, 'online', NOW()), "
        "(%s, 'ONU-CI-KARIM', 'ZTEG0004', %s, '0/1', -29.00, 'offline', NOW())",
        (_olt_a, _olt_a, _olt_a, _olt_k, _shop_u))
    _adm_home = adm.get("/").get_data(as_text=True)
    _sec = _adm_home.split('id="offline-onus"', 1)[-1].split("</section>", 1)[0] if 'id="offline-onus"' in _adm_home else ""
    check(_sec and "ONU-CI-LOS" in _sec and "ONU-CI-OFF" in _sec and "ONU-CI-KARIM" in _sec and "ONU-CI-UP" not in _sec
          and _sec.index("ONU-CI-KARIM") < _sec.index("ONU-CI-LOS") < _sec.index("ONU-CI-OFF"),
          "admin dashboard lists Offline and LOS ONUs (newest drop first), not the online ones")
    # 10 a page, and the LOS / Offline filter
    for _n in range(12):
        run("INSERT INTO onus (olt_id, name, serial, status, last_seen) VALUES (%s, %s, '', 'offline', NOW() - INTERVAL %s DAY)",
            (_olt_a, "ONU-CI-PG%02d" % _n, 2 + _n))

    def _onu_items(html):
        sec = html.split('id="offline-onus"', 1)[-1].split("</section>", 1)[0]
        return re.findall(r'class="onu-off-name"[^>]*>([^<]+)<', sec), sec
    _p1, _s1 = _onu_items(adm.get("/").get_data(as_text=True))
    _p2, _s2 = _onu_items(adm.get("/?onu_page=2").get_data(as_text=True))
    _pl, _sl = _onu_items(adm.get("/?onu_st=los").get_data(as_text=True))
    check(len(_p1) == 10 and len(_p2) == 5 and not set(_p1) & set(_p2) and "1–10 of 15" in _s1 and "onu_page=2" in _s1
          and "#offline-onus" in _s1, "Offline ONUs shows 10 a page with page links that come back to the section")
    check(_pl == ["ONU-CI-LOS"] and 'aria-current="true"' in _sl, "the LOS filter shows only LOS ONUs")
    _h = adm.get("/").get_data(as_text=True)
    _at = _h.index('id="offline-onus"')
    check(_h.count('id="offline-onus"') == 1 and _h.index("Customers by plan") < _at
          and ("<h2>This server</h2>" not in _h or _at < _h.index("<h2>This server</h2>")),
          "on the admin dashboard Offline ONUs sits low, just above This server (once)")
    # which customer uses an unlinked ONU: ONU name = PPPoE username, or ONU MAC = the customer's bound MAC
    _a_sub = one("SELECT id, username FROM subscribers WHERE created_by NOT IN ('karim', 'karim_sub') ORDER BY id LIMIT 1")
    _b_sub = one("SELECT id, username FROM subscribers WHERE created_by NOT IN ('karim', 'karim_sub') ORDER BY id DESC LIMIT 1")
    run("UPDATE subscribers SET mac_bind = 'AA:BB:CC:00:11:22' WHERE id = %s", (_b_sub["id"],))
    run("DELETE FROM onus WHERE name LIKE 'ONU-CI-PG%%'")
    run("INSERT INTO onus (olt_id, name, serial, mac, status, last_seen) VALUES "
        "(%s, %s, 'ZTEGNAME01', '', 'los', NOW()), (%s, 'ONU-CI-BYMAC', 'ZTEGMAC01', 'aa-bb-cc-00-11-22', 'offline', NOW())",
        (_olt_a, _a_sub["username"].upper(), _olt_a))
    _h = adm.get("/").get_data(as_text=True)
    _sec = _h.split('id="offline-onus"', 1)[-1].split("</section>", 1)[0]
    check("auto by ONU name" in _sec and 'value="%s"' % _a_sub["username"] in _sec
          and "auto by MAC" in _sec and 'value="%s"' % _b_sub["username"] in _sec,
          "unlinked offline ONUs show the customer found by ONU name or MAC, with a Link button")
    _oid = one("SELECT id FROM onus WHERE name = 'ONU-CI-BYMAC'")["id"]
    _lk = adm.post("/onus/%d/map" % _oid, data={"_csrf": "ci", "username": _b_sub["username"], "next": "/#offline-onus"})
    check(one("SELECT username FROM onus WHERE id = %s", (_oid,))["username"] == _b_sub["username"]
          and _lk.headers.get("Location", "").endswith("/#offline-onus"), "Link saves the match and comes back to the section")
    run("UPDATE subscribers SET mac_bind = '' WHERE id = %s", (_b_sub["id"],))
    # Network > ONUs: the same customer column, also for online ONUs (live PPPoE session MAC)
    _live = one("SELECT r.username, s.id FROM radacct r JOIN subscribers s ON s.username = r.username "
                "WHERE r.acctstoptime IS NULL AND s.created_by NOT IN ('karim', 'karim_sub') LIMIT 1")
    if _live:
        run("UPDATE radacct SET callingstationid = 'DE:AD:BE:EF:00:42' WHERE username = %s AND acctstoptime IS NULL",
            (_live["username"],))
        run("INSERT INTO onus (olt_id, name, serial, mac, status, last_seen) VALUES (%s, 'ONU-CI-LIVE', 'ZTEGLIVE01', "
            "'de:ad:be:ef:00:42', 'online', NOW())", (_olt_a,))
        _onu_page = adm.get("/onus?olt=%d&per=100" % _olt_a).get_data(as_text=True)    # the list shows MAC / serial
        _row = _onu_page.split("de:ad:be:ef:00:42", 1)[-1].split("</tr>", 1)[0]
        _row_name = _onu_page.split("ZTEGNAME01", 1)[-1].split("</tr>", 1)[0]
        check('value="%s"' % _live["username"] in _row and "auto by live session" in _row
              and 'value="%s"' % _a_sub["username"] in _row_name and "auto by ONU name" in _row_name,
              "Network > ONUs shows the customer of unlinked ONUs too (ONU name, and online ONUs by live session MAC)")
        run("DELETE FROM onus WHERE name = 'ONU-CI-LIVE'")
    _res_home = client(ids["reseller"]).get("/").get_data(as_text=True)
    check('id="offline-onus"' not in _res_home and "ONU-CI-LOS" not in _res_home,
          "a reseller without ONU access gets no Offline ONUs section")
    run("REPLACE INTO role_perms (role, module, level) VALUES ('reseller', 'network', 'view')")
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'onu.view', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    _res_home = client(ids["reseller"]).get("/").get_data(as_text=True)
    _rsec = _res_home.split('id="offline-onus"', 1)[-1].split("</section>", 1)[0] if 'id="offline-onus"' in _res_home else ""
    check(_rsec and "ONU-CI-KARIM" in _rsec and "ONU-CI-LOS" not in _rsec and "ONU-CI-OFF" not in _rsec,
          "with ONU access a reseller's dashboard shows only the offline ONUs on their own OLTs")
    run("DELETE FROM role_perms WHERE role = 'reseller' AND module = 'network'")
    run("DELETE FROM role_funcs WHERE role = 'reseller' AND fkey = 'onu.view'")
    run("DELETE FROM onus WHERE name LIKE 'ONU-CI-%%'")
    run("DELETE FROM net_devices WHERE name LIKE 'CI-OLT-%%'")

    # ---- in-app notifications for customers: payment, balance, expiring, custom; unread mark, list, live check
    import appnotify as _an
    _nsid = one("SELECT id FROM subscribers WHERE username = 'uttara.shop'")["id"]
    check(one("SELECT COUNT(*) AS c FROM sub_notifications WHERE kind = 'payment'")["c"] > 0,
          "every activation / renewal creates a payment notification in the customer's app")
    run("DELETE FROM sub_notifications WHERE subscriber_id = %s", (_nsid,))
    adm.post("/subscribers/%d/balance" % _nsid, data={"_csrf": "ci", "amount": "150", "note": "ci"})
    adm.post("/subscribers/%d/notify" % _nsid, data={"_csrf": "ci", "title": "CI লাইনের কাজ", "body": "আজ রাতে লাইনের কাজ চলবে।"})
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 3 DAY, status = 'active', grace_until = NULL WHERE id = %s", (_nsid,))
    _c2 = db.connect()
    try:
        with _c2.cursor() as cur:
            _made1 = _an.run_expiry(_c2, cur)
            _made2 = _an.run_expiry(_c2, cur)
    finally:
        _c2.close()
    _kinds = sorted(r["kind"] for r in run_q("SELECT kind FROM sub_notifications WHERE subscriber_id = %s", (_nsid,)))
    check(_kinds == ["balance", "custom", "expiring"] and _made1 >= 1 and _made2 == 0,
          "balance top-up, the admin's custom message and 'expires in 3 days' arrive once each (no duplicates)")
    _home_p = pc.get("/portal").get_data(as_text=True)
    _chk = pc.get("/portal/notifications/check").get_json() or {}
    check('class="notif-dot" data-notif-count>3<' in _home_p and _chk.get("unread") == 3 and _chk.get("latest", {}).get("title")
          and "portal-notify.js" in _home_p, "the portal shows the red unread number (3) and the app can check for new ones")
    _list = pc.get("/portal/notifications").get_data(as_text=True)
    _after = pc.get("/portal/notifications/check").get_json() or {}
    check("CI লাইনের কাজ" in _list and "মেয়াদ শেষ হতে চলেছে" in _list and "ব্যালেন্সে টাকা যোগ হয়েছে" in _list
          and _list.count('class="notif-new"') == 3 and _after.get("unread") == 0,
          "opening the list shows the new ones marked and then clears the unread number")
    _sv = adm.get("/subscribers/%d" % _nsid).get_data(as_text=True)
    check('id="tab-notifications"' in _sv and "CI লাইনের কাজ" in _sv and ">Read<" in _sv and 'data-open="notify-dlg"' in _sv,
          "the customer page lists sent app notifications with read / unread status and a Send button")
    check(pc.get("/portal/notifications/check").status_code == 200
          and (flask_app.test_client().get("/portal/notifications/check").get_json() or {}).get("unread") == 0,
          "the live check gives nothing to someone who is not signed in")

    # ---- Web Push: opt-in, subscription storage (only real push services), the sender, the service worker
    import webpush as _wp
    import sender as _snd
    _key = (pc.get("/portal/push/key").get_json() or {}).get("key")
    check((len(_key or "") > 80) if _wp.available() else _key is None,
          "the push key is made once and given to the signed-in customer's app (none without python3-cryptography)")
    _hdr = {"X-CSRF-Token": "ci"}
    _good = {"endpoint": "https://fcm.googleapis.com/fcm/send/ci-test-endpoint-123",
             "keys": {"p256dh": "B" + "A" * 86, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}
    _evil = dict(_good, endpoint="https://10.0.0.5/admin/reboot")
    _r_evil = pc.post("/portal/push/subscribe", json=_evil, headers=_hdr)
    _r_good = pc.post("/portal/push/subscribe", json=_good, headers=_hdr)
    _r_again = pc.post("/portal/push/subscribe", json=_good, headers=_hdr)
    check(_r_evil.status_code == 400 and _r_good.status_code == 200 and _r_again.status_code == 200
          and one("SELECT COUNT(*) AS c FROM push_subscriptions WHERE subscriber_id = %s", (_nsid,))["c"] == 1,
          "a browser's push subscription is stored once; an address that is not a real push service is refused")
    _calls = []
    _keep_av, _keep_keys = _wp.available, _wp.keys
    try:
        _wp.available = lambda: True
        _wp.keys = lambda cur: ("priv", "pub")
        _c3 = db.connect()
        with _c3.cursor() as cur:
            cur.execute("UPDATE sub_notifications SET pushed_at = NOW() WHERE pushed_at IS NULL")
            _an.add(cur, _nsid, "custom", "CI push", "push body")
            _c3.commit()
            _sent = _snd.push_round(_c3, cur, {"portal_on": "1", "support_email": "noc@example.net"},
                                    send=lambda sub, data, priv, pub, subj: (_calls.append((sub["endpoint"], data, subj)) or (201, "")))
            _again = _snd.push_round(_c3, cur, {"portal_on": "1"}, send=lambda *a: (_calls.append("dup") or (201, "")))
            _an.add(cur, _nsid, "custom", "CI push 2", "gone")
            _c3.commit()
            _snd.push_round(_c3, cur, {"portal_on": "1"}, send=lambda *a: (410, "gone"))
        _c3.close()
    finally:
        _wp.available, _wp.keys = _keep_av, _keep_keys
    check(_sent == 1 and _again == 0 and len(_calls) == 1 and _calls[0][1]["title"] == "CI push"
          and _calls[0][1]["url"] == "/portal/notifications" and _calls[0][2] == "mailto:noc@example.net"
          and one("SELECT COUNT(*) AS c FROM push_subscriptions WHERE subscriber_id = %s", (_nsid,))["c"] == 0,
          "the sender pushes each new notification once to the customer's phones and drops a subscription the service reports gone")
    _sw2 = flask_app.test_client().get("/sw.js").get_data(as_text=True)
    _ph = pc.get("/portal/notifications").get_data(as_text=True)
    check('addEventListener("push"' in _sw2 and "showNotification" in _sw2 and "notificationclick" in _sw2 and "silent: false" in _sw2
          and 'data-push-card="list"' in _ph and "data-push-sub=" in _ph and "push_vapid_private" in db.SECRET_KEYS,
          "the service worker shows pushes with sound and opens the app on tap; the portal offers the opt-in")
    _r_un = pc.post("/portal/push/unsubscribe", json={"endpoint": _good["endpoint"]}, headers=_hdr)
    check(_r_un.status_code == 200, "a customer can switch push off again")
    # ---- bulk app notification: a group of customers at once, with history and take-back
    _all_n = one("SELECT COUNT(*) AS c FROM subscribers")["c"]
    _np = adm.get("/notifications/send")
    _cnt = adm.get("/notifications/recipients").get_json()["count"]
    _rb = adm.post("/notifications/send", data={"_csrf": "ci", "audience": "filter", "title": "CI bulk {name}",
                                                "body": "Hello {username}"})
    _bulk = run_q("SELECT n.title, n.body, s.full_name, s.username, n.batch FROM sub_notifications n "
              "JOIN subscribers s ON s.id = n.subscriber_id WHERE n.title LIKE 'CI bulk%%'")
    _batch = _bulk[0]["batch"] if _bulk else ""
    _hist = adm.get("/notifications/send").get_data(as_text=True)
    _resc = client(ids["reseller"])
    _rcnt = _resc.get("/notifications/recipients").get_json()["count"]
    _resc.post("/notifications/send", data={"_csrf": "ci", "audience": "filter", "title": "CI seller", "body": "x"})
    _rs_rows = one("SELECT COUNT(*) AS c FROM sub_notifications WHERE title = 'CI seller'")["c"]
    _rs_take = _resc.post("/notifications/batch/%s/delete" % _batch, data={"_csrf": "ci"}).status_code
    _empty = adm.post("/notifications/send", data={"_csrf": "ci", "audience": "filter", "title": "", "body": "x"})
    _del = adm.post("/notifications/batch/%s/delete" % _batch, data={"_csrf": "ci"})
    check(_np.status_code == 200 and "notify-form" in _np.get_data(as_text=True) and _cnt == _all_n
          and _rb.status_code == 302 and len(_bulk) == _all_n and len({r["batch"] for r in _bulk}) == 1
          and all(r["title"] == "CI bulk " + (r["full_name"] or r["username"]) and r["body"] == "Hello " + r["username"] for r in _bulk)
          and "CI bulk" in _hist and "Take back" in _hist
          and 0 < _rcnt < _all_n and _rs_rows == _rcnt and _rs_take == 404
          and one("SELECT COUNT(*) AS c FROM sub_notifications WHERE title LIKE 'CI bulk%%'")["c"] == 0 and _del.status_code == 302
          and one("SELECT COUNT(*) AS c FROM sub_notifications WHERE title = ''")["c"] == 0 and _empty.status_code == 200
          and 'href="/notifications/send"' in adm.get("/").get_data(as_text=True),
          "App Notification sends to a whole group with each customer's own details, sellers reach only their customers, "
          "and a send can be taken back")
    # ---- Network > CoA Request Log: disconnect log, and new PPPoE requests that open a filled-in New subscriber form
    _known = one("SELECT username FROM subscribers ORDER BY id LIMIT 1")["username"]
    run("INSERT INTO radpostauth (username, pass, reply, authdate, nas_ip, mac) VALUES "
        "('new.pppoe', 'S3cret!x', 'Access-Reject', NOW(), '10.9.9.1', 'AA:BB:CC:00:11:22'), "
        "('new.pppoe', '', 'Access-Reject', NOW() - INTERVAL 1 HOUR, '10.9.9.1', ''), "
        "('chap.only', '', 'Access-Reject', NOW(), '10.9.9.2', ''), "
        "(%s, 'typo-of-real-pw', 'Access-Reject', NOW(), '10.9.9.1', ''), "
        "(%s, 'real-pw', 'Access-Accept', NOW(), '10.9.9.1', '')", (_known, _known))
    run("INSERT INTO coa_log (username, nas_ip, ok, message, created_by) VALUES (%s, '10.9.9.1', 1, 'CI disconnect', 'admin')", (_known,))
    _kept = one("SELECT COUNT(*) AS c FROM radpostauth WHERE username = %s AND pass <> ''", (_known,))["c"]
    _pw = one("SELECT pass FROM radpostauth WHERE username = 'new.pppoe' AND pass <> ''")
    _coa = adm.get("/network/coa").get_data(as_text=True)
    _newp = adm.get("/network/coa?tab=new").get_data(as_text=True)
    _rid = one("SELECT id FROM radpostauth WHERE username = 'new.pppoe' AND pass <> ''")["id"]
    _form = adm.get("/subscribers/new?req=%d" % _rid).get_data(as_text=True)
    _rid_known = one("SELECT MAX(id) AS id FROM radpostauth WHERE username = %s", (_known,))["id"]
    _form_known = adm.get("/subscribers/new?req=%d" % _rid_known).get_data(as_text=True)
    _sel = client(ids["reseller"])
    _sel_new = _sel.get("/network/coa?tab=new")
    _sel_form = _sel.get("/subscribers/new?req=%d" % _rid).get_data(as_text=True)
    check(_kept == 0 and _pw and _pw["pass"] == "S3cret!x"
          and "CI disconnect" in _coa and "New PPPoE requests" in _coa
          and "new.pppoe" in _newp and "chap.only" in _newp and _known not in _newp and "Received" in _newp and "Not sent" in _newp
          and ('href="/subscribers/new?req=%d"' % _rid) in _newp and "S3cret" not in _newp
          and 'value="new.pppoe"' in _form and 'value="S3cret!x"' in _form and "10.9.9.1" in _form and "AA:BB:CC:00:11:22" in _form
          and "typo-of-real-pw" not in _form_known and "real-pw" not in _form_known
          and "S3cret" not in _sel_form and (_sel_new.status_code == 403 or "new.pppoe" not in _sel_new.get_data(as_text=True))
          and 'href="/network/coa"' in adm.get("/").get_data(as_text=True),
          "CoA Request Log sits under Network; a refused unknown PPPoE username opens New subscriber with its username and "
          "password filled in, existing customers' passwords are never kept, and sellers do not see others' requests")
    # ---- resellers see the New PPPoE requests of the routers assigned to them (NAS > Reseller) and add those customers
    _res_name = one("SELECT username FROM panel_admins WHERE id = %s", (ids["reseller"],))["username"]
    run("DELETE FROM nas WHERE nasname IN ('10.9.9.1', '10.9.9.2')")
    run("INSERT INTO nas (nasname, shortname, type, secret, description) VALUES ('10.9.9.2', 'ci-other', 'other', 'ci-secret-2', '')")
    _ns = adm.post("/nas/save", data={"_csrf": "ci", "nasname": "10.9.9.1", "shortname": "ci-res", "type": "mikrotik",
                                      "secret": "ci-secret-1", "seller": _res_name})
    _nas_seller = one("SELECT seller FROM nas WHERE nasname = '10.9.9.1'")
    _nas_html = adm.get("/nas").get_data(as_text=True)
    _rc = client(ids["reseller"])
    _rc_menu = _rc.get("/").get_data(as_text=True)
    _rc_new = _rc.get("/network/coa?tab=new")
    _rc_new_html = _rc_new.get_data(as_text=True)
    _rc_form = _rc.get("/subscribers/new?req=%d" % _rid).get_data(as_text=True)
    _chap_rid = one("SELECT MAX(id) AS id FROM radpostauth WHERE username = 'chap.only'")["id"]
    _rc_chap = _rc.get("/subscribers/new?req=%d" % _chap_rid).get_data(as_text=True)
    _sub_new = client(ids["subreseller"]).get("/network/coa?tab=new").get_data(as_text=True)
    _nid = one("SELECT id FROM nas WHERE nasname = '10.9.9.1'")["id"]
    run("REPLACE INTO role_perms (role, module, level) VALUES ('reseller', 'network', 'edit')")
    _rc.post("/nas/save", data={"_csrf": "ci", "id": str(_nid), "nasname": "10.9.9.1", "shortname": "ci-res", "type": "mikrotik",
                                "secret": "ci-secret-1", "seller": ""})
    _kept_seller = one("SELECT seller FROM nas WHERE nasname = '10.9.9.1'")["seller"]
    run("REPLACE INTO role_perms (role, module, level) VALUES ('reseller', 'network', 'view')")
    check(_ns.status_code == 302 and _nas_seller and _nas_seller["seller"] == _res_name and 'name="seller"' in _nas_html
          and 'href="/network/coa"' in _rc_menu
          and _rc_new.status_code == 200 and "new.pppoe" in _rc_new_html and "chap.only" not in _rc_new_html
          and "Add New Subscriber" in _rc_new_html and 'value="new.pppoe"' in _rc_form and 'value="S3cret!x"' in _rc_form
          and 'value="chap.only"' not in _rc_chap and "new.pppoe" not in _sub_new and _kept_seller == _res_name,
          "a reseller sees and adds the New PPPoE requests of routers assigned to them, not other routers'; only the office assigns routers")

    # ---- installable app (PWA): manifests, icons, service worker, offline page, tags on every layout
    import struct as _st
    import mod_pwa as _pwa
    _anon2 = flask_app.test_client()

    def _png_size(data):
        return _st.unpack(">II", data[16:24]) if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    _mf = _anon2.get("/manifest.webmanifest")
    _mfj = json.loads(_mf.get_data(as_text=True))
    _pmf = json.loads(_anon2.get("/portal/manifest.webmanifest").get_data(as_text=True))
    check(_mf.status_code == 200 and "manifest+json" in _mf.headers.get("Content-Type", "") and _mfj["display"] == "standalone"
          and _mfj["start_url"] == "/?source=pwa" and _mfj["name"].endswith("Panel")
          and sorted(i["purpose"] for i in _mfj["icons"]) == ["any", "any", "maskable"]
          and _pmf["start_url"] == "/portal?source=pwa" and _pmf["scope"] == "/portal",
          "panel and customer-portal app manifests open without sign-in, standalone, with any + maskable icons")
    _i512 = _anon2.get("/pwa/icon-512.png")
    _i180 = _anon2.get("/pwa/icon-180.png")
    _im = _anon2.get("/pwa/icon-512-maskable.png")
    check(_i512.mimetype == "image/png" and _png_size(_i512.data) == (512, 512) and _png_size(_i180.data) == (180, 180)
          and _png_size(_im.data) == (512, 512) and _anon2.get("/pwa/icon-999.png").status_code == 404,
          "app icons are real PNGs of the declared sizes (180, 512, maskable 512)")
    check(_png_size(_pwa._ring_icon(48, False)) == (48, 48) and _png_size(_pwa._ring_icon(48, True)) == (48, 48),
          "without a logo or Pillow the panel's mark is drawn as the icon")
    # a logo uploaded in Settings becomes the app icon at once: new icon URLs in the manifests, Apple icon and service worker
    try:
        from PIL import Image as _Img
        import io as _pio

        def _logo_png(rgb):
            _b = _pio.BytesIO()
            _Img.new("RGB", (64, 64), rgb).save(_b, "PNG")
            return _b.getvalue()

        def _app_state():
            _m = json.loads(_anon2.get("/manifest.webmanifest").get_data(as_text=True))
            _pm = json.loads(_anon2.get("/portal/manifest.webmanifest").get_data(as_text=True))
            _src = _m["icons"][0]["src"]
            _ic = _anon2.get(_src)
            _px = _Img.open(_pio.BytesIO(_ic.data)).convert("RGB").getpixel((96, 96))
            _ic.close()
            return (_src.split("v=")[1], _pm["icons"][0]["src"].split("v=")[1], _px,
                    _anon2.get("/sw.js").get_data(as_text=True), adm.get("/").get_data(as_text=True))
        _k0 = _app_state()
        adm.post("/settings/brand/logo", data={"_csrf": "ci", "file": (_pio.BytesIO(_logo_png((220, 20, 20))), "l.png")},
                 content_type="multipart/form-data")
        _k1 = _app_state()
        adm.post("/settings/brand/logo", data={"_csrf": "ci", "file": (_pio.BytesIO(_logo_png((20, 20, 220))), "l.png")},
                 content_type="multipart/form-data")
        _k2 = _app_state()
        adm.post("/settings/brand/logo", data={"_csrf": "ci", "remove": "1"})
        _k3 = _app_state()
        check(_k0[0] == "mark" and _k1[0].startswith("logo-") and _k1[0] == _k1[1] and _k1[2][0] > 180 and _k1[2][2] < 60
              and _k2[0] not in (_k1[0], "mark") and _k2[2][2] > 180 and _k3[0] == "mark"
              and ("v=" + _k1[0]) in _k1[3] and ("icon-180.png?v=" + _k1[0]) in _k1[4] and ("v=" + _k2[0]) in _k2[3],
              "app icon follows the logo: upload / replace / remove changes the icon URL in both manifests, the Apple icon "
              "and the service worker, and the icon shows the new logo")
    except ImportError:
        print("  skip  app icon from the logo (Pillow is not installed here)")
    _sw = _anon2.get("/sw.js")
    _sw_js = _sw.get_data(as_text=True)
    check(_sw.status_code == 200 and "javascript" in _sw.mimetype and _sw.headers.get("Service-Worker-Allowed") == "/"
          and '"/offline"' in _sw_js and "app.css" in _sw_js and 'req.mode === "navigate"' in _sw_js,
          "the service worker is served at /sw.js and only precaches static files and the offline page")
    check("ইন্টারনেট সংযোগ নেই" in _anon2.get("/offline").get_data(as_text=True), "the offline page opens without sign-in")
    _home = adm.get("/").get_data(as_text=True)
    _login = _anon2.get("/login").get_data(as_text=True)
    _portal_html = pc.get("/portal").get_data(as_text=True)
    check('rel="manifest" href="/manifest.webmanifest"' in _home and "apple-touch-icon" in _home and "pwa.js" in _home
          and "data-pwa-install" in _home and "pwa-launch" in _home
          and 'rel="manifest" href="/manifest.webmanifest"' in _login and "pwa.js" in _login
          and 'rel="manifest" href="/portal/manifest.webmanifest"' in _portal_html and "pwa.js" in _portal_html,
          "every layout (panel, login, customer portal) links its app manifest, splash and loading script")
    _pjs = open(os.path.join(ROOT, "panel", "static", "js", "pwa.js"), encoding="utf-8").read()
    check("beforeinstallprompt" in _pjs and "Add to Home Screen" in _pjs and "pwa-install-later" in _pjs
          and ".pwa-sheet-card" in open(os.path.join(ROOT, "panel", "static", "css", "app.css"), encoding="utf-8").read(),
          "phones get an install sheet (Android one-tap install, iPhone steps) that 'Later' hides for a week")
    check(_pmf["id"] != _mfj["id"] and _pmf["scope"] == "/portal" and _pmf["name"] != _mfj["name"]
          and '"pwa-install-later-" + KIND' in _pjs
          and '<meta name="pwa-kind" content="panel">' in _home and '<meta name="pwa-kind" content="panel">' in _login
          and '<meta name="pwa-kind" content="portal">' in _portal_html
          and 'apple-mobile-web-app-title" content="%s"' % _pmf["name"] in _portal_html
          and 'apple-mobile-web-app-title" content="%s"' % _mfj["name"] in _home
          and "list.filter(isPortal)" in _sw_js and "(staff ? !isPortal(c) : isPortal(c))" in _sw_js,
          "panel and customer apps install separately: own id, name and 'Later' memory; a notification never takes over a panel window")

    # ---- expired notice: expired customers stay online in the notice group and see the unpaid-bill page
    import radius as _rad
    _ex = one("SELECT s.id, s.username FROM subscribers s JOIN radacct r ON r.username = s.username AND r.acctstoptime IS NULL "
              "WHERE s.plan_id IS NOT NULL AND s.status = 'active' AND s.created_by NOT IN ('karim', 'karim_sub') LIMIT 1")
    run("UPDATE subscribers SET expiry_date = CURDATE() - INTERVAL 1 DAY, expiry_time = '10:00', grace_until = NULL, "
        "net_disabled = 0, quota_hit = '' WHERE id = %s", (_ex["id"],))
    _set = client(ids["admin"])
    _set.post("/settings/expired-notice", data={"_csrf": "ci", "expired_notice_on": "1", "expired_notice_bkash": "1",
                                                "expired_notice_phone": "01700000999", "expired_notice_rate": "256k/256k",
                                                "expired_notice_msg": "", "expired_notice_server": "10.20.30.40"})

    def _rad_rows(u):
        _c = db.connect()
        try:
            with _c.cursor() as cur:
                _rad.resync_username(cur, u)
                _c.commit()
                cur.execute("SELECT groupname FROM radusergroup WHERE username = %s", (u,))
                groups = [r["groupname"] for r in cur.fetchall()]
                cur.execute("SELECT attribute, value FROM radcheck WHERE username = %s", (u,))
                checks = {r["attribute"]: r["value"] for r in cur.fetchall()}
            return groups, checks
        finally:
            _c.close()
    _g_on, _c_on = _rad_rows(_ex["username"])
    _reply = {r["attribute"]: r["value"] for r in run_q("SELECT attribute, value FROM radgroupreply WHERE groupname = 'expired-notice'")}
    check(_g_on == ["expired-notice"] and "Expiration" not in _c_on and "Auth-Type" not in _c_on
          and _reply.get("Mikrotik-Address-List") == "expired" and _reply.get("Mikrotik-Rate-Limit") == "256k/256k",
          "expired notice on: an expired customer is kept online in the notice group (address list 'expired', low speed)")
    _ip = one("SELECT framedipaddress FROM radacct WHERE username = %s AND acctstoptime IS NULL LIMIT 1", (_ex["username"],))["framedipaddress"]
    _anon = flask_app.test_client()
    _pg = _anon.get("/expired-notice", environ_base={"REMOTE_ADDR": _ip})
    _pg_html = _pg.get_data(as_text=True)
    _gen = _anon.get("/expired-notice", environ_base={"REMOTE_ADDR": "192.0.2.77"}).get_data(as_text=True)
    check(_pg.status_code == 200 and "বিল অপরিশোধিত" in _pg_html and _ex["username"] in _pg_html and "01700000999" in _pg_html
          and "পরিশোধ করতে হবে" in _pg_html and "no-store" in _pg.headers.get("Cache-Control", ""),
          "the unpaid-bill page opens without sign-in and shows the customer found by their session IP, with the phone")
    check('class="hero"' in _pg_html and 'class="due"' in _pg_html and "সাবস্ক্রাইবার আইডি" in _pg_html
          and str(_ex["id"]) in _pg_html and "location.reload()" in _pg_html and "<script src" not in _pg_html,
          "notice redesign: hero, amount due, subscriber ID, try-again button, nothing loaded from outside")
    check("বিল অপরিশোধিত" in _gen and _ex["username"] not in _gen and "ইউজার আইডি" not in _gen,
          "an unknown address gets the general message only (no customer details)")
    _sp = _set.get("/settings/expired-notice").get_data(as_text=True)
    check("to-addresses=10.20.30.40 to-ports=8099" in _sp and "src-address-list=expired" in _sp and "pay.bka.sh" in _sp,
          "Settings > Expired notice shows the MikroTik commands for this server")
    # ---- notice page bKash button: checkout opens straight away for the customer on this IP (no portal sign-in)
    import bkash as _bk2
    run("REPLACE INTO settings (skey, svalue) VALUES ('bkash_on', '1'), ('bkash_key', 'k'), ('bkash_secret', 's'), "
        "('bkash_user', 'u'), ('bkash_pass', 'p'), ('sub_self_activation', '1'), ('portal_on', '1'), "
        "('bkash_callback_base', 'https://panel.ci.test')")
    _made = []
    _real_create = _bk2.create

    def _fake_create(flow, ref, amount, payer, callback, invoice):
        _made.append({"payer": payer, "callback": callback, "amount": amount})
        return {"paymentID": "CINOTICE%d" % ref, "bkashURL": "https://sandbox.payment.bkash.com/redirect/ci"}
    _bk2.create = _fake_create
    try:
        _np = _anon.get("/expired-notice", environ_base={"REMOTE_ADDR": _ip}).get_data(as_text=True)
        _go = _anon.get("/expired-notice?pay=1", environ_base={"REMOTE_ADDR": _ip})
        _stranger = _anon.get("/expired-notice?pay=1", environ_base={"REMOTE_ADDR": "192.0.2.77"})
        _made_by_ip = len(_made)
        _far = {"REMOTE_ADDR": "198.51.100.9"}           # the router masquerades: the page cannot tell who this is
        _gen_btn = _anon.get("/expired-notice", environ_base=_far).get_data(as_text=True)
        _ask = _anon.get("/expired-notice?pay=1", environ_base=_far).get_data(as_text=True)
        _who = _anon.get("/expired-notice?pay=1&sid=%d" % _ex["id"], environ_base=_far).get_data(as_text=True)
        _byname = _anon.get("/expired-notice?pay=1&sid=%s" % _ex["username"], environ_base=_far).get_data(as_text=True)
        _nobody = _anon.get("/expired-notice?pay=1&sid=999999999", environ_base=_far).get_data(as_text=True)
        _go2 = _anon.get("/expired-notice?pay=1&sid=%d&go=1" % _ex["id"], environ_base=_far)
    finally:
        _bk2.create = _real_create
    _exname = one("SELECT full_name FROM subscribers WHERE id = %s", (_ex["id"],))["full_name"] or _ex["username"]
    check('href="?pay=1"' in _gen_btn and "/portal/login" not in _gen_btn.split('class="btn pay"')[1][:80]
          and 'name="sid"' in _ask and "go=1" in _who and "পরিশোধ করুন" in _who and _ex["username"] not in _who
          and _exname not in _who and _exname[:2] + "***" in _who and "go=1" in _byname
          and "কোনো গ্রাহক পাওয়া যায়নি" in _nobody and _go2.status_code == 302 and len(_made) == _made_by_ip + 1
          and str(_made[-1]["payer"]) == "RN%02d" % _ex["id"],
          "customer not found by IP: the bKash button asks for the subscriber ID, shows a masked name and the amount, then opens bKash")
    run("DELETE FROM audit_log WHERE action = 'notice_lookup'")
    _prow = one("SELECT * FROM portal_payments WHERE subscriber_id = %s AND payment_id LIKE 'CINOTICE%%' ORDER BY id LIMIT 1", (_ex["id"],))
    check('href="?pay=1"' in _np and _go.status_code == 302
          and _go.headers.get("Location") == "https://sandbox.payment.bkash.com/redirect/ci"
          and _made_by_ip == 1 and str(_made[0]["payer"]) == "RN%02d" % _ex["id"]
          and _made[0]["callback"].startswith("https://panel.ci.test/portal/bkash/")
          and _prow and _prow["payment_id"] == "CINOTICE%d" % _prow["id"] and _prow["status"] == "pending"
          and _stranger.status_code == 200,
          "notice page bKash button: checkout starts for the customer found by IP, subscriber ID as the reference; unknown IP starts nothing")
    with flask_app.test_request_context("/portal"):
        _paid = _bk2.settle_portal(_prow, {"trxID": "CINTRX1", "amount": "%.2f" % (float(_prow["amount"]) + float(_prow["fee"]))})
    _after = one("SELECT expiry_date, status FROM subscribers WHERE id = %s", (_ex["id"],))
    _queued = one("SELECT COUNT(*) AS c FROM coa_queue WHERE username = %s AND reason = 'renewed'", (_ex["username"],))["c"]
    check(_paid and _after["expiry_date"] > date.today() and _after["status"] == "active" and _queued >= 1,
          "a bKash payment from the notice page renews the package at once and reconnects the customer")
    run("DELETE FROM settings WHERE skey IN ('bkash_on', 'bkash_key', 'bkash_secret', 'bkash_user', 'bkash_pass', 'bkash_callback_base')")
    run("DELETE FROM coa_queue WHERE username = %s", (_ex["username"],))

    # ---- how long the expired speed lasts: after it the customer is refused and their slow session dropped
    run("UPDATE subscribers SET expiry_date = CURDATE() - INTERVAL 3 DAY, expiry_time = '10:00' WHERE id = %s", (_ex["id"],))
    _hours_form = {"_csrf": "ci", "expired_notice_on": "1", "expired_notice_bkash": "1", "expired_notice_phone": "01700000999",
                   "expired_notice_rate": "256k/256k", "expired_notice_msg": "", "expired_notice_server": "10.20.30.40"}
    _set.post("/settings/expired-notice", data=dict(_hours_form, expired_notice_hours="900"))
    _bad_hours = one("SELECT svalue FROM settings WHERE skey = 'expired_notice_hours'")
    _set.post("/settings/expired-notice", data=dict(_hours_form, expired_notice_hours="168"))
    _g_in, _c_in = _rad_rows(_ex["username"])
    _np2 = _anon.get("/expired-notice", environ_base={"REMOTE_ADDR": _ip}).get_data(as_text=True)
    _sp2 = _set.get("/settings/expired-notice").get_data(as_text=True)
    _set.post("/settings/expired-notice", data=dict(_hours_form, expired_notice_hours="24"))
    import maintenance as _mt
    _mc = db.connect()
    try:
        with _mc.cursor() as _cur:
            _cur.execute("SELECT skey, svalue FROM settings")
            _sett = {r["skey"]: r["svalue"] for r in _cur.fetchall()}
            _mt.apply_plan_states(_mc, _cur, _sett)
    finally:
        _mc.close()
    _g_out = [r["groupname"] for r in run_q("SELECT groupname FROM radusergroup WHERE username = %s", (_ex["username"],))]
    _c_out = {r["attribute"]: r["value"] for r in run_q("SELECT attribute, value FROM radcheck WHERE username = %s", (_ex["username"],))}
    _kick = one("SELECT COUNT(*) AS c FROM coa_queue WHERE username = %s AND reason = 'notice time over'", (_ex["username"],))["c"]
    check((_bad_hours or {}).get("svalue", "0") in ("0", None) and _g_in == ["expired-notice"] and "Expiration" not in _c_in
          and "পর্যন্ত এই কম স্পীডে" in _np2 and 'name="expired_notice_hours"' in _sp2 and 'value="168"' in _sp2
          and "expired-notice" not in _g_out and "Expiration" in _c_out and _kick == 1,
          "expired speed time limit: inside it the customer sees the page and its deadline; after it they are refused and dropped")
    run("DELETE FROM coa_queue WHERE username = %s", (_ex["username"],))
    run("UPDATE subscribers SET expiry_date = CURDATE() - INTERVAL 1 DAY, expiry_time = '10:00' WHERE id = %s", (_ex["id"],))
    _set.post("/settings/expired-notice", data=dict(_hours_form, expired_notice_hours="0"))
    _rad_rows(_ex["username"])
    # the exact moment: an account ending later today keeps its Expiration; one that ended a minute ago is expired
    run("UPDATE subscribers SET expiry_date = CURDATE(), expiry_time = %s WHERE id = %s",
        ((datetime.now() + timedelta(hours=2)).strftime("%H:%M") if datetime.now().hour < 21 else "23:59", _ex["id"]))
    _g_later, _c_later = _rad_rows(_ex["username"])
    check("expired-notice" not in _g_later and "Expiration" in _c_later, "before the end moment the account is normal")
    _set.post("/settings/expired-notice", data={"_csrf": "ci", "expired_notice_server": "", "expired_notice_rate": "256k/256k"})
    run("UPDATE subscribers SET expiry_date = CURDATE() - INTERVAL 1 DAY WHERE id = %s", (_ex["id"],))
    _g_off, _c_off = _rad_rows(_ex["username"])
    check("expired-notice" not in _g_off and "Expiration" in _c_off,
          "expired notice off: expired customers are refused again (Expiration back, no notice group)")
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 20 DAY, expiry_time = NULL WHERE id = %s", (_ex["id"],))
    _rad_rows(_ex["username"])

    # ---- timezone: Settings warns when the server clock is on another zone; noc.py follows the panel setting
    import tempfile as _tf2
    _core = sys.modules["core"]
    check(_core.same_timezone("Asia/Dhaka", "Asia/Dhaka") and _core.same_timezone("Asia/Dhaka", "Asia/Dacca")
          and not _core.same_timezone("Asia/Dhaka", "UTC") and _core.same_timezone("Asia/Dhaka", ""),
          "timezone comparison: same clock is a match (aliases too), a different offset is not, unknown never alarms")
    _tzf = os.path.join(_tf2.mkdtemp(), "timezone")
    _keep_tz, _keep_lt = _core.TIMEZONE_FILE, _core.LOCALTIME
    try:
        _zi = os.path.join(_tf2.mkdtemp(), "zoneinfo")
        for _z in ("UTC", os.path.join("Asia", "Dhaka")):
            os.makedirs(os.path.dirname(os.path.join(_zi, _z)), exist_ok=True)
            open(os.path.join(_zi, _z), "w").close()
        _core.TIMEZONE_FILE = _tzf
        run("REPLACE INTO settings (skey, svalue) VALUES ('timezone', 'Asia/Dhaka')")
        # /etc/localtime decides; a stale /etc/timezone (timedatectl does not update it) must not raise a false alarm
        with open(_tzf, "w") as fh:
            fh.write("Etc/UTC\n")
        _core.LOCALTIME = os.path.join(_zi, "Asia", "Dhaka")
        stale_page = adm.get("/settings").get_data(as_text=True)
        _core.LOCALTIME = os.path.join(_zi, "UTC")
        warn_page = adm.get("/settings").get_data(as_text=True)
        _core.LOCALTIME = os.path.join(_zi, "missing")       # no /etc/localtime: fall back to /etc/timezone
        with open(_tzf, "w") as fh:
            fh.write("Asia/Dhaka\n")
        ok_page = adm.get("/settings").get_data(as_text=True)
        check("The server clock is on" not in stale_page and "The server clock is on UTC" in warn_page
              and "sudo timedatectl set-timezone Asia/Dhaka" in warn_page and "freeradius" in warn_page
              and _core.server_timezone() == "Asia/Dhaka" and "The server clock is on" not in ok_page,
              "Settings warns (with the fix command) only when the server clock (/etc/localtime) differs from Local "
              "Timezone; a stale /etc/timezone is ignored")
    finally:
        _core.TIMEZONE_FILE, _core.LOCALTIME = _keep_tz, _keep_lt
    _noc_src = open(os.path.join(ROOT, "panel", "noc.py"), encoding="utf-8").read()
    check('core.apply_timezone(s.get("timezone"))' in _noc_src, "noc.py follows the panel's Local Timezone like the other services")

    # ---- documents uploaded on a customer's page show there and in the main Documents menu; sellers see only their own
    import io as _io3
    run("REPLACE INTO settings (skey, svalue) VALUES ('doc_upload', '1')")
    _own = one("SELECT id, username FROM subscribers WHERE created_by = 'karim' LIMIT 1")
    _theirs = one("SELECT id, username FROM subscribers WHERE created_by NOT IN ('karim', 'karim_sub') LIMIT 1")
    up = adm.post("/documents/upload", data={"_csrf": "ci", "username": _theirs["username"], "title": "CI NID copy",
                                             "category": "NID / ID copy", "next": "/subscribers/%d#documents" % _theirs["id"],
                                             "file": (_io3.BytesIO(b"%PDF-1.4 ci test"), "nid.pdf")},
                  content_type="multipart/form-data")
    sv_page = adm.get("/subscribers/%d" % _theirs["id"]).get_data(as_text=True)
    menu_page = adm.get("/documents").get_data(as_text=True)
    only_page = adm.get("/documents?sub=%d" % _theirs["id"]).get_data(as_text=True)
    check(up.status_code == 302 and up.headers.get("Location", "").endswith("/subscribers/%d#documents" % _theirs["id"])
          and "CI NID copy" in sv_page and 'class="sv-doc-up"' in sv_page and "CI NID copy" in menu_page
          and "CI NID copy" in only_page and "Showing documents of" in only_page,
          "a document uploaded on the customer's page shows there, in Documents, and in the customer filter")
    _doc_id = one("SELECT id FROM documents WHERE title = 'CI NID copy'")["id"]
    res_c = client(ids["reseller"])
    bad_up = res_c.post("/documents/upload", data={"_csrf": "ci", "username": _theirs["username"], "next": "/documents",
                                                   "file": (_io3.BytesIO(b"%PDF-1.4 x"), "x.pdf")}, content_type="multipart/form-data")
    rlist = res_c.get("/documents")
    check(one("SELECT COUNT(*) AS c FROM documents WHERE filename = 'x.pdf'")["c"] == 0
          and res_c.get("/documents/%d/download" % _doc_id).status_code == 403
          and (rlist.status_code != 200 or "CI NID copy" not in rlist.get_data(as_text=True)),
          "a reseller cannot upload to, list or open another seller's customer's documents")

    # ---- subscriber Ledger (debit / credit / running balance) and Invoices (every activation + extra charges)
    import mod_subview as _sv
    from datetime import datetime as _dtm, timedelta as _td
    _t0 = _dtm(2026, 11, 1, 10, 0)
    _pays = [  # newest first, as the page loads them
        {"id": 3, "paid_at": _t0 + _td(days=2), "amount": 250, "days_added": 30, "method": "cash", "period_to": None,
         "note": "Received 300.00, 50.00 added to the balance", "received_by": "admin"},
        {"id": 2, "paid_at": _t0 + _td(days=1), "amount": 250, "days_added": 30, "method": "balance", "period_to": None,
         "note": "Paid from the customer's balance", "received_by": "admin"}]
    _bal = [
        {"id": 3, "amount": 50, "balance_after": 100, "note": "Extra from activation payment (received 300.00, package 250.00)",
         "created_by": "admin", "created_at": _t0 + _td(days=2)},
        {"id": 2, "amount": -250, "balance_after": 50, "note": "Activation +30 days (Home 20)", "created_by": "admin",
         "created_at": _t0 + _td(days=1)},
        {"id": 1, "amount": 300, "balance_after": 300, "note": "", "created_by": "admin", "created_at": _t0}]
    _chg = [{"id": 9, "created_at": _t0 + _td(days=3), "amount": 40, "paid": 0, "name": "Installation", "note": "",
             "created_by": "admin"}]
    _led = _sv.ledger({"balance": 100}, _pays, _chg, _bal)
    check([r["balance"] for r in _led] == [60.0, 100.0, -200.0, 50.0, 300.0]
          and [(r["debit"], r["credit"]) for r in _led] == [(40.0, 0.0), (0.0, 300.0), (250.0, 0.0), (250.0, 0.0), (0.0, 300.0)],
          "ledger: invoices are debits, money received is credit, package halves of balance lines are not repeated, "
          "and the running balance ends at balance minus dues")
    _inv = _sv.invoices(_pays, _chg)
    check([v["no"] for v in _inv] == ["CHG-9", "INV-3", "INV-2"] and _inv[1]["receipt"] == 3 and not _inv[0]["paid"]
          and _inv[2]["via"] == "customer balance", "invoices list every activation (with a receipt) and every extra charge")
    _sid = one("SELECT subscriber_id FROM payments WHERE subscriber_id IS NOT NULL AND days_added > 0 ORDER BY id DESC LIMIT 1")
    if _sid:
        _page = adm.get("/subscribers/%d" % _sid["subscriber_id"]).get_data(as_text=True)
        check('class="tbl sv-ledger"' in _page and ">INV-" in _page and "/receipt" in _page,
              "the customer's page shows the debit/credit ledger and printable invoices")

    # ---- Server tools: the web server is Caddy (install.sh), and logins are listed even when radius.log is empty
    import tempfile as _tf
    panel_app = sys.modules["app"]
    tools_html = adm.get("/tools").get_data(as_text=True)
    check(">caddy<" in tools_html and ">nginx<" not in tools_html, "Server tools checks Caddy, the web server install.sh sets up")
    check("Recent login attempts" in tools_html and "Cleartext password does not match" in tools_html,
          "Server tools lists recent RADIUS login attempts with the refusal reason")
    _d = _tf.mkdtemp()
    _empty, _old = os.path.join(_d, "radius.log"), os.path.join(_d, "radius.log.1")
    open(_empty, "w").close()
    with open(_old, "w") as fh:
        fh.write("Info: Ready to process requests\n")
    _keep = panel_app.RADIUS_LOGS
    try:
        panel_app.RADIUS_LOGS = (_empty, _old)
        text, used = panel_app.radius_log()
        check("Ready to process requests" in text and used == _old, "an empty radius.log falls back to the rotated file")
        panel_app.RADIUS_LOGS = (_empty,)
        text, _ = panel_app.radius_log()
        check(text == "", "no log text at all shows the empty-log message instead of a blank box")
    finally:
        panel_app.RADIUS_LOGS = _keep

    # ---- Mass Activation / Mass Payment / Mass Delete open as popups and work
    lst = adm.get("/subscribers").get_data(as_text=True)
    check(all(t in lst for t in ('id="mass-act-dlg"', 'id="mass-pay-dlg"', 'id="mass-del-dlg"', 'data-mass-open="mass-act-dlg"',
                                 'data-mass-open="mass-pay-dlg"', 'data-mass-open="mass-del-dlg"', 'data-u="'))
          and 'value="extend">Mass Activation' not in lst,
          "admin's subscriber list has the three mass popups (and the old bar options are gone)")
    pair = [r["id"] for r in run_q("SELECT id FROM subscribers WHERE plan_id IS NOT NULL AND status = 'active' "
                                   "AND (created_by = 'admin' OR created_by IS NULL OR created_by = '') ORDER BY id LIMIT 2")]
    if len(pair) == 2:
        run("UPDATE subscribers SET balance = 0 WHERE id IN (%s, %s)", tuple(pair))
        before = {r["id"]: r["expiry_date"] for r in run_q("SELECT id, expiry_date FROM subscribers WHERE id IN (%s, %s)", tuple(pair))}
        pays0 = one("SELECT COUNT(*) AS c FROM payments WHERE subscriber_id IN (%s, %s)", tuple(pair))["c"]
        r = adm.post("/subscribers/mass-activate", data={"_csrf": "ci", "ids": [str(i) for i in pair], "plan_id": "current",
                                                          "method": "balance", "next": "/subscribers"})
        same = {r2["id"]: r2["expiry_date"] for r2 in run_q("SELECT id, expiry_date FROM subscribers WHERE id IN (%s, %s)", tuple(pair))}
        check(r.status_code == 302 and same == before, "Subscriber Balance method skips customers whose balance is too low")
        r = adm.post("/subscribers/mass-activate", data={"_csrf": "ci", "ids": [str(i) for i in pair], "plan_id": "current",
                                                          "method": "cash", "next": "/subscribers"})
        after = {r2["id"]: r2["expiry_date"] for r2 in run_q("SELECT id, expiry_date FROM subscribers WHERE id IN (%s, %s)", tuple(pair))}
        pays1 = one("SELECT COUNT(*) AS c FROM payments WHERE subscriber_id IN (%s, %s)", tuple(pair))["c"]
        check(all(after[i] and (before[i] is None or after[i] > before[i]) for i in pair) and pays1 == pays0 + 2,
              "Cash method activates every ticked customer and records a payment for each")
        r = adm.post("/subscribers/mass-payment", data={"_csrf": "ci", "ids": [str(i) for i in pair], "method": "bKash",
                                                         "amount": "250", "next": "/subscribers"})
        bals = [float(r2["balance"]) for r2 in run_q("SELECT balance FROM subscribers WHERE id IN (%s, %s)", tuple(pair))]
        led = one("SELECT COUNT(*) AS c FROM sub_ledger WHERE subscriber_id IN (%s, %s) AND note = 'Mass payment (bKash)'", tuple(pair))["c"]
        check(bals == [250.0, 250.0] and led == 2, "Mass Payment adds the amount to each balance and writes the ledger")
        exp2 = {r2["id"]: r2["expiry_date"] for r2 in run_q("SELECT id, expiry_date FROM subscribers WHERE id IN (%s, %s)", tuple(pair))}
        r = adm.post("/subscribers/mass-activate", data={"_csrf": "ci", "ids": [str(i) for i in pair], "plan_id": "current",
                                                          "method": "smart", "next": "/subscribers"})
        exp3 = {r2["id"]: r2["expiry_date"] for r2 in run_q("SELECT id, expiry_date FROM subscribers WHERE id IN (%s, %s)", tuple(pair))}
        check(all(exp3[i] > exp2[i] for i in pair), "Smart Payment activates (from balance when it covers the price, else cash)")
    res_list = client(ids["reseller"]).get("/subscribers").get_data(as_text=True)
    check('id="mass-del-dlg"' not in res_list, "only the admin gets Mass Delete")

    # ---- RX / TX: not in the subscriber list any more; a live chart on each customer's page, for every panel
    other = one("SELECT id FROM subscribers WHERE created_by NOT IN ('karim', 'karim_sub') OR created_by IS NULL LIMIT 1")
    shop = one("SELECT s.id, s.username, r.radacctid FROM subscribers s JOIN radacct r ON r.username = s.username "
               "AND r.acctstoptime IS NULL WHERE s.created_by = 'karim' LIMIT 1")
    if not shop:        # give one of karim's customers an open session
        u = one("SELECT id, username FROM subscribers WHERE created_by = 'karim' LIMIT 1")
        run("INSERT INTO radacct (acctsessionid, acctuniqueid, username, nasipaddress, acctstarttime, acctupdatetime) "
            "VALUES ('ci-rate', 'ci-rate-u', %s, '10.0.0.1', NOW() - INTERVAL 1 HOUR, NOW())", (u["username"],))
        shop = one("SELECT s.id, s.username, r.radacctid FROM subscribers s JOIN radacct r ON r.username = s.username "
                   "AND r.acctstoptime IS NULL WHERE s.id = %s LIMIT 1", (u["id"],))
    shop_id = shop["id"]
    run("REPLACE INTO traffic_last (radacctid, username, down_bps, up_bps, upd) VALUES (%s, %s, 4640000, 186000, NOW())",
        (shop["radacctid"], shop["username"]))
    lst = adm.get("/subscribers").get_data(as_text=True)
    check("Rx / Tx" not in lst and 'data-c="rate"' not in lst, "the subscriber list has no Rx / Tx column")
    for who, cl in (("admin", adm), ("reseller", client(ids["reseller"]))):
        page = cl.get("/subscribers/%d" % shop_id).get_data(as_text=True)
        rj = cl.get("/subscribers/%d/rate" % shop_id)
        d = rj.get_json(silent=True) or {}
        check('data-rate-src="/subscribers/%d/rate"' % shop_id in page and "live-rate.js" in page
              and rj.status_code == 200 and d.get("online") is True and d.get("down") == 4640000 and d.get("up") == 186000,
              "%s sees the live RX / TX chart and its speed on the customer's page" % who)
    if other:
        check(client(ids["reseller"]).get("/subscribers/%d/rate" % other["id"]).status_code in (302, 403, 404),
              "a reseller cannot read the speed of a customer who is not theirs")

    # ---- phone menu: the empty toast box once covered the whole phone screen (top AND bottom set, z-index 100)
    # and swallowed every tap on the menu and the page
    _css = open(os.path.join(ROOT, "panel", "static", "css", "app.css"), encoding="utf-8").read()
    check(".toasts { pointer-events: none; }" in _css and ".toast { pointer-events: auto; }" in _css
          and ".toasts { top: auto; bottom:" in _css,
          "toast box never catches taps, and on phones it sits only at the bottom")
    check("body.nav-open::after" in _css and "100dvh" in _css,
          "phone menu has a real tap-catching backdrop and fits the visible screen")

    # ---- Phase 2 shell: the topbar "online" pill is ISP-wide, so never for sellers
    import re as _re3
    a_html = adm.get("/subscribers").get_data(as_text=True)
    r_cl = client(ids["reseller"])
    r_html = r_cl.get("/subscribers").get_data(as_text=True)
    check(_re3.search(r'class="live-pill".*?data-live="online">[\d,]+<', a_html, _re3.S) is not None,
          "admin topbar shows the online count straight away")
    check("live-pill" not in r_html and r_cl.get("/api/live").status_code == 403,
          "resellers get no ISP-wide online pill and /api/live refuses them")
    check("bottom-nav" in r_html and 'href="/subscribers"' in r_html, "reseller gets the phone bottom navigation")

    # ---- BTRC Report (admin only)
    import zipfile as _z2, io as _io2, re as _re2
    adm.post("/reports/btrc/settings", data={"_csrf": "ci", "btrc_division": "Chattogram", "btrc_district": "Brahmanbaria",
                                             "btrc_thana": "Nabinagar", "btrc_client_type": "", "btrc_connection_type": "",
                                             "btrc_distribution_point": "", "btrc_connectivity_type": ""})
    page = adm.get("/reports/btrc")
    check(page.status_code == 200 and "BTRC Report" in page.get_data(as_text=True), "admin opens the BTRC Report tab")
    check(res.get("/reports/btrc").status_code == 403 and res.get("/reports/btrc.xlsx").status_code == 403
          and client(ids["staff"]).get("/reports/btrc").status_code == 403, "resellers and staff cannot open or download it")
    x = adm.get("/reports/btrc.xlsx")
    sheet = _z2.ZipFile(_io2.BytesIO(x.data)).read("xl/worksheets/sheet1.xml").decode()
    def cells(row_xml):
        out = []
        for attrs, inner in _re2.findall(r"<c ([^>]*?)>(.*?)</c>", row_xml, _re2.S):
            ref = _re2.search(r'r="([A-Z]+)', attrs).group(1)
            t = _re2.findall(r"<t[^>]*>(.*?)</t>", inner) or _re2.findall(r"<v>(.*?)</v>", inner)
            out.append((ref, t[0] if t else "", 's="1"' in attrs))
        return out
    rows_x = _re2.findall(r"<row[^>]*>(.*?)</row>", sheet, _re2.S)
    head = [c[1] for c in cells(rows_x[0])]
    check(head == ["client_type", "connection_type", "client_username", "bandwidth_distribution_point", "connectivity_type",
                   "activation_date", "bandwidth_allocation", "allocated_ip", "division", "district", "thana", "address",
                   "client_mobile", "client_email", "selling_price_bdt_excluding_vat"], "Excel header matches the BTRC sample")
    by_user = {}
    for rx in rows_x[1:]:
        c = {ref: (v, date_style) for ref, v, date_style in cells(rx)}
        by_user[c["C"][0]] = c
    shop = by_user.get("uttara.shop", {})
    shop_price = float(one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = 2", (ids["reseller"],))["price"])
    check(shop and shop["A"][0] == "Home" and shop["B"][0] == "Wired" and shop["D"][0] == "NOC" and shop["E"][0] == "Shared"
          and shop["I"][0] == "Chattogram" and shop["K"][0] == "Nabinagar" and shop["G"][0] == "20"
          and abs(float(shop["O"][0]) - shop_price) < 0.01, "row values: defaults, location, Mbps and the reseller's customer price")
    shop_g = _re2.search(r'<c r="G\d+"[^>]*>.*?</c>', next((rx for rx in rows_x if ">uttara.shop<" in rx), ""), _re2.S)
    check(shop_g and "inlineStr" not in shop_g.group(0), "bandwidth_allocation is a number cell, not text")
    import mod_btrc as _mb
    check([_mb.package_mbps(r) for r in ({"plan_name": "15mb", "download_mbps": 1}, {"plan_name": "Home 10 MB"},
                                          {"plan_name": "gold", "plan_label": "512 Kbps"}, {"bandwidth_value": 1, "bandwidth_type": "Gbps"},
                                          {"plan_name": "gold", "download_mbps": 8})] == [15, 10, 0.512, 1000, 8],
          "bandwidth_allocation is the plain package speed (15mb -> 15)")
    check(shop and shop["F"][1] and 40000 < int(shop["F"][0]) < 60000, "activation_date is a real Excel date")
    check(shop and shop["H"][0].startswith("100.64."), "allocated_ip comes from the customer's latest session")
    check(len(by_user) >= 8, "everyone activated or renewed this month is in the sheet (%d)" % len(by_user))
    old = adm.get("/reports/btrc.xlsx?month=2001-01")
    old_rows = _re2.findall(r"<row ", _z2.ZipFile(_io2.BytesIO(old.data)).read("xl/worksheets/sheet1.xml").decode())
    check(len(old_rows) == 1, "a month with no activations gives only the header row")
    # ---- bKash SMS auto-renew: a forwarded "You have received" SMS renews the customer once, from a phone with a token
    import mod_bksms as _bks
    _p = _bks.parse("You have received payment Tk 500.00 from 0171XXXX583. Ref Jony Tnt. Fee Tk 0.00. Balance Tk 51,370.00. "
                   "TrxID DKM2S0CT80 at 22/11/2026 11:07")
    _p2 = _bks.parse("You have received Tk 1,250.50 from 01712345678. Fee Tk 0.00. Balance Tk 1,500.00. TrxID ABC12XYZ9 at 01/12/2026 09:05")
    check(_p and _p["amount"] == 500.0 and _p["payer"] == "0171XXXX583" and _p["ref"] == "Jony Tnt" and _p["trx"] == "DKM2S0CT80"
          and _p["paid_at"].strftime("%Y-%m-%d %H:%M") == "2026-11-22 11:07"
          and _p2 and _p2["amount"] == 1250.5 and _p2["ref"] == "" and _p2["payer"] == "01712345678"
          and _bks.parse("Your OTP is 1234") is None and _bks.parse("Cash Out Tk 500.00 to 0171. TrxID AB12CD34EF") is None,
          "bKash SMS parser reads amount, payer, reference, TrxID and time")
    _bs = adm.post("/settings/bkash-sms", data={"_csrf": "ci", "bksms_on": "1", "bksms_senders": "bKash"})
    _bs_html = adm.get("/settings/bkash-sms").get_data(as_text=True)
    _tok = _re2.search(r"/sms-in/([A-Za-z0-9_-]{20,})", _bs_html)
    _tok = _tok.group(1) if _tok else "missing"
    _anon = flask_app.test_client()
    _adm_name = one("SELECT username FROM panel_admins WHERE id = %s", (ids["admin"],))["username"]
    _a = one("SELECT * FROM subscribers WHERE created_by = %s AND status = 'active' AND plan_id IS NOT NULL ORDER BY id LIMIT 1",
             (_adm_name,))
    run("UPDATE subscribers SET balance = 0 WHERE id = %s", (_a["id"],))

    def _sms(text, sender="bKash", token=None, form=False):
        url = "/sms-in/" + (_tok if token is None else token)
        body = {"from": sender, "text": text}
        return _anon.post(url, data=body) if form else _anon.post(url, json=body)

    _t1 = "You have received payment Tk 5,000.00 from 01999000111. Ref %s. Fee Tk 0.00. Balance Tk 9,000.00. TrxID CIBK000001 at 22/11/2026 11:07"
    _r1 = _sms(_t1 % ("RN%02d" % _a["id"]))           # Reference = the subscriber ID with the prefix
    _a2 = one("SELECT * FROM subscribers WHERE id = %s", (_a["id"],))
    _pay = one("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (_a["id"],))
    _r1b = _sms(_t1 % ("rn %02d" % _a["id"]))
    _pays = one("SELECT COUNT(*) AS c FROM payments WHERE note LIKE '%%CIBK000001%%'")["c"]
    _bad = [_sms(_t1 % _a["username"], token="wrong-token").status_code, _anon.post("/sms-in", json={"text": "x"}).status_code]
    _other = _sms(_t1.replace("CIBK000001", "CIBK000002") % _a["username"], sender="Grameenphone").get_json()
    # too little to renew (sent as a form, "Rn 05" written): it goes to the balance
    _b = one("SELECT * FROM subscribers WHERE created_by = %s AND status = 'active' AND plan_id IS NOT NULL AND id <> %s "
             "ORDER BY id LIMIT 1", (_adm_name, _a["id"]))
    run("UPDATE subscribers SET balance = 0 WHERE id = %s", (_b["id"],))
    _r3 = _sms("You have received Tk 10.00 from %s. Ref Rn %02d. Fee Tk 0.00. Balance Tk 10.00. TrxID CIBK000003 at 22/11/2026 12:00"
               % (_b["phone"], _b["id"]), form=True).get_json()
    # anything but an RN-style ID in the Reference is ignored and kept nowhere: only digits, a username, the payer's own
    # registered mobile with no Reference, a masked number with other words
    _ign = [_sms(("You have received payment Tk 30.00 from %s. Ref %s. Fee Tk 0.00. Balance Tk 1.00. TrxID CIIGN0000%d at "
                  "22/11/2026 12:01") % (_b["phone"], ref, n)).get_json().get("status")
            for n, ref in enumerate((str(_b["id"]), _b["username"], "Someone Else", "RN-%02d" % _b["id"]))]
    _ign.append(_sms("You have received Tk 30.00 from %s. Fee Tk 0.00. Balance Tk 1.00. TrxID CIIGN00009 at 22/11/2026 12:02"
                     % _b["phone"]).get_json().get("status"))
    _ign_kept = one("SELECT COUNT(*) AS c FROM sms_payments WHERE trx_id LIKE 'CIIGN%%'")["c"]
    _b2 = one("SELECT balance, expiry_date FROM subscribers WHERE id = %s", (_b["id"],))
    # an RN-style ID nobody has (a typing mistake) waits under Needs a match; the admin books it with one click
    _masked = _b["phone"][:4] + "XXXX" + _b["phone"][-3:]
    _r4 = _sms("You have received payment Tk 20.00 from %s. Ref RN999999. Fee Tk 0.00. Balance Tk 1.00. TrxID CIBK000004 "
               "at 22/11/2026 12:05" % _masked).get_json()
    _row4 = one("SELECT * FROM sms_payments WHERE trx_id = 'CIBK000004'")
    _old_link = adm.get("/settings/bkash-sms?show=unmatched")
    _un_html = adm.get("/bkash-sms-payments?status=unmatched").get_data(as_text=True)
    _ap = adm.post("/settings/bkash-sms/%d/apply" % _row4["id"], data={"_csrf": "ci", "username": _b["username"]})
    _ap2 = adm.post("/settings/bkash-sms/%d/apply" % _row4["id"], data={"_csrf": "ci", "username": _b["username"]})
    _b3 = one("SELECT balance FROM subscribers WHERE id = %s", (_b["id"],))
    # a reseller's customer: the reseller is credited with the money and pays their own package cost from it
    _rsub = one("SELECT s.* FROM subscribers s JOIN user_plans up ON up.plan_id = s.plan_id AND up.user_id = %s "
                "WHERE s.created_by = %s AND s.status = 'active' ORDER BY s.id LIMIT 1", (ids["reseller"], _res_name))
    _rbal0 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _cost = float(one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _rsub["plan_id"]))["price"])
    _r5 = _sms(_t1.replace("CIBK000001", "CIBK000005") % ("Rn%02d bill" % _rsub["id"])).get_json()   # words around it are fine
    _rbal1 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _res_page = client(ids["reseller"]).get("/settings/bkash-sms").status_code
    _pc = flask_app.test_client()
    with _pc.session_transaction() as _ps:
        _ps["cid"] = ids["portal_sub"]
    _portal_html = _pc.get("/portal").get_data(as_text=True)
    check(_bs.status_code == 302 and _tok != "missing" and _r1.get_json().get("status") == "renewed"
          and _a2["expiry_date"] > (_a["expiry_date"] or date.today()) and _pay and _pay["method"] == "bKash"
          and abs(float(_a2["balance"]) - (5000 - float(_pay["amount"]))) < 0.01
          and _r1b.get_json().get("status") == "duplicate" and _pays == 1 and _bad == [403, 403]
          and _other.get("status") == "ignored" and not one("SELECT id FROM sms_payments WHERE trx_id = 'CIBK000002'")
          and _r3.get("status") == "balance" and abs(float(_b2["balance"]) - 10) < 0.01 and _b2["expiry_date"] == _b["expiry_date"]
          and _ign == ["ignored"] * 5 and _ign_kept == 0
          and _r4.get("status") == "unmatched" and _row4["subscriber_id"] is None and "Book for this customer" in _un_html
          and "RN999999" in _row4["message"]
          and _ap.status_code == 302 and abs(float(_b3["balance"]) - 30) < 0.01
          and one("SELECT status FROM sms_payments WHERE id = %s", (_row4["id"],))["status"] == "balance"
          and _r5.get("status") == "renewed" and abs((_rbal1 - _rbal0) - (5000 - _cost)) < 0.01
          and _res_page == 403 and 'href="/settings/bkash-sms"' in _bs_html
          and one("SELECT username FROM sms_payments WHERE trx_id = 'CIBK000001'")["username"] == _a["username"]
          and ('Subscriber ID</dt><dd class="mono">RN%02d<' % ids["portal_sub"]) in _portal_html
          and all(k in _portal_html for k in ('class="ph-hero', 'class="ph-ring', 'data-copy-text="RN%02d"' % ids["portal_sub"],
                                               ">Connection<", ">My package<", ">Billing<", ">My account<", "Next bill",
                                               "Customer since", 'data-tab="account"', "data-lr-down", "data-pp-photo")),
          "bKash SMS auto-renew: renews only from an RN01-style ID in the reference (any case, one space), each TrxID once, "
          "only with the token and from bKash; small payments go to the balance; a wrong RN ID waits for one click; "
          "anything else is ignored and not kept; a reseller's customer credits the reseller "
          "(%s %s %s %s %s %s)" % (_r1.get_json(), _r3, _ign, _r4, _r5, (_rbal1 - _rbal0, 5000 - _cost)))
    # ---- Accounting > bKash SMS Payments: the list moved out of Settings; resellers see only their own customers' payments
    _sp_adm = adm.get("/bkash-sms-payments?from=2026-01-01&to=2027-12-31").get_data(as_text=True)
    _sp_set = adm.get("/settings/bkash-sms").get_data(as_text=True)
    _rc2 = client(ids["reseller"])
    _sp_res = _rc2.get("/bkash-sms-payments?from=2026-01-01&to=2027-12-31")
    _sp_res_html = _sp_res.get_data(as_text=True)
    _sp_menu = side_menu(_rc2.get("/").get_data(as_text=True))
    _sp_adm_menu = side_menu(adm.get("/").get_data(as_text=True))
    _sp_csv = adm.get("/bkash-sms-payments.csv?from=2026-01-01&to=2027-12-31").get_data(as_text=True)
    _sp_res_csv = _rc2.get("/bkash-sms-payments.csv?from=2026-01-01&to=2027-12-31").status_code
    _rs_book = _rc2.post("/settings/bkash-sms/%d/ignore" % _row4["id"], data={"_csrf": "ci"}).status_code
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'bks.module', 0) ON DUPLICATE KEY UPDATE allowed = 0")
    _sp_off = client(ids["reseller"]).get("/bkash-sms-payments").status_code
    run("DELETE FROM role_funcs WHERE role = 'reseller' AND fkey = 'bks.module'")
    _acct = _sp_adm_menu.split('data-key="acct"', 1)[-1].split("</details>", 1)[0]
    check(_old_link.status_code == 302 and "/bkash-sms-payments?status=unmatched" in _old_link.headers.get("Location", "")
          and "CIBK000001" in _sp_adm and "CIBK000005" in _sp_adm and "Received payments</h2>" not in _sp_set
          and 'href="/bkash-sms-payments"' in _sp_set and "CIBK000001" in _sp_csv
          and _acct.find('href="/bkash-payments"') < _acct.find('href="/bkash-sms-payments"') and _acct.find('href="/bkash-payments"') >= 0
          and _sp_res.status_code == 200 and "CIBK000005" in _sp_res_html and "CIBK000001" not in _sp_res_html
          and "CIBK000004" not in _sp_res_html and "CIBK000003" not in _sp_res_html and "Book for this customer" not in _sp_res_html
          and 'href="/bkash-sms-payments"' in _sp_menu and _sp_res_csv == 403 and _rs_book == 403 and _sp_off == 403,
          "bKash SMS Payments under Accounting (after bKash Payments): staff see all and book the unmatched; a reseller sees "
          "only their own customers' payments, cannot book or export without the ticks, and loses it when unticked")
    # ---- USB GSM modem (SIM800C): PDU decoding, long SMS in parts, the service books bKash SMS, the page shows its state
    import gsmlib as _G
    import gsmd as _gd

    def _pack7(septs, fill=0):
        bits = 0
        for k, sv in enumerate(septs):
            bits |= sv << (fill + 7 * k)
        return bits.to_bytes((fill + 7 * len(septs) + 7) // 8, "little")

    def _pdu(text, sender="bKash", udh=b"", ucs2=False):
        sa = _pack7([_G.GSM7.index(ch) for ch in sender])
        head = "00" + "%02X" % (0x04 | (0x40 if udh else 0)) + "%02X" % ((len(sender) * 7 + 3) // 4) + "D0" + sa.hex()
        head += "00" + ("08" if ucs2 else "00") + "62112211700000"
        if ucs2:
            ud = udh + text.encode("utf-16-be")
            return (head + "%02X" % len(ud) + ud.hex()).upper()
        fill = (7 - (len(udh) * 8) % 7) % 7 if udh else 0
        body = _pack7([_G.GSM7.index(ch) for ch in text], len(udh) * 8 + fill)
        ud = bytes(a | b for a, b in zip(udh + bytes(len(body) - len(udh)), body))
        return (head + "%02X" % ((len(udh) * 8 + fill) // 7 + len(text)) + ud.hex()).upper()

    _classic = _G.decode_pdu("07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07")
    _gb = one("SELECT * FROM subscribers WHERE created_by = %s AND status = 'active' AND plan_id IS NOT NULL ORDER BY id DESC LIMIT 1",
              (_adm_name,))
    run("UPDATE subscribers SET balance = 0 WHERE id = %s", (_gb["id"],))
    _long = ("You have received payment Tk 5,000.00 from 01999000222. Ref RN%02d. Fee Tk 0.00. Balance Tk 9,000.00. "
             "TrxID CIGSM00001 at 22/11/2026 11:07" % _gb["id"])
    _half = len(_long) // 2
    _pa = _pdu(_long[:_half], udh=bytes([5, 0, 3, 77, 2, 1]))
    _pb = _pdu(_long[_half:], udh=bytes([5, 0, 3, 77, 2, 2]))
    _bn = _pdu("আপনার বিকাশ কোড 123456", ucs2=True)
    _lone = _pdu("part one of three that never completes", udh=bytes([5, 0, 3, 9, 3, 1]))
    _dp = [_G.decode_pdu(x) for x in (_pa, _pb, _bn)]
    _cm = _G.parse_cmgl(["+CMGL: 3,0,,40", _pa, "+CMGL: 4,0,,41", _pb, "", "OK"])

    class _FakeModem:
        def __init__(self, port, baud):
            self.port, self.deleted, self.closed = port, [], False
            self.box = {3: _pa, 4: _pb, 5: _bn, 6: _lone, 7: "0001000B911346610089F60000"}   # 7 = a sent copy
        def setup(self): pass
        def close(self): self.closed = True
        def status(self):
            return {"model": "SIM800C", "sim": "READY", "operator": "CI Mobile", "signal": 20, "registered": "home"}
        def messages(self): return sorted(self.box.items())
        def delete(self, idx):
            self.deleted.append(idx); self.box.pop(idx, None); return True

    _made = []
    _gs = adm.post("/settings/bkash-sms", data={"_csrf": "ci", "part": "gsm", "gsm_on": "1", "gsm_port": "auto", "gsm_baud": "115200"})
    _gbad = adm.post("/settings/bkash-sms", data={"_csrf": "ci", "part": "gsm", "gsm_on": "1", "gsm_port": "/dev/../etc/passwd"})
    _port_kept = one("SELECT svalue FROM settings WHERE skey = 'gsm_port'")["svalue"]
    _svc = _gd.Service(flask_app, opener=lambda p, b: _made.append(_FakeModem(p, b)) or _made[-1], finder=lambda b: "/dev/ci-modem")
    _wait = _svc.tick()
    _fm = _made[0] if _made else None
    _gpay = one("SELECT * FROM sms_payments WHERE trx_id = 'CIGSM00001'")
    _gb2 = one("SELECT expiry_date FROM subscribers WHERE id = %s", (_gb["id"],))
    _inbox = run_q("SELECT sender, text, result FROM gsm_inbox WHERE text NOT LIKE 'CI sample%%' ORDER BY id")
    _st1 = dict(_svc.state)
    _G.write_state(_st1)
    _gpage = adm.get("/settings/bkash-sms").get_data(as_text=True)
    _rc_file = adm.post("/settings/bkash-sms/modem/reconnect", data={"_csrf": "ci"})
    _reconn = os.path.exists(_G.RECONNECT_FILE)
    _svc.tick()
    _reopened = len(_made) == 2 and _made[0].closed and not os.path.exists(_G.RECONNECT_FILE)
    _rs_rc = client(ids["reseller"]).post("/settings/bkash-sms/modem/reconnect", data={"_csrf": "ci"}).status_code
    run("UPDATE settings SET svalue = '0' WHERE skey = 'gsm_on'")
    _off_wait = _svc.tick()
    check(len(_G.GSM7) == 128 and _classic and _classic["sender"] == "+31641600986" and _classic["text"] == "How are you?"
          and _dp[0]["sender"] == "bKash" and _dp[0]["part"] == (77, 2, 1) and _dp[0]["text"] + _dp[1]["text"] == _long
          and _dp[2]["text"] == "আপনার বিকাশ কোড 123456" and _dp[0]["time"].strftime("%Y-%m-%d %H:%M") == "2026-11-22 11:07"
          and _cm == [(3, _pa), (4, _pb)],
          "GSM modem: SMS PDUs decode (GSM alphabet, Bangla, long SMS parts, alphanumeric sender)")
    check(_gs.status_code == 302 and _gbad.status_code == 302 and _port_kept == "auto" and _wait == _gd.POLL and _fm
          and _fm.port == "/dev/ci-modem" and sorted(_fm.deleted) == [3, 4, 5, 7] and 6 in _fm.box
          and _gpay and _gpay["source"] == "modem" and _gpay["status"] == "renewed" and _gpay["username"] == _gb["username"]
          and _gb2["expiry_date"] > (_gb["expiry_date"] or date.today())
          and len(_inbox) == 2 and {r["text"]: r["result"] for r in _inbox}.get(_long) == "renewed"
          and {r["text"]: r["result"] for r in _inbox}.get("আপনার বিকাশ কোড 123456", "").startswith("ignored")
          and _st1["connected"] and _st1["sms_read"] == 2 and _st1["operator"] == "CI Mobile"
          and "Connected" in _gpage and "CI Mobile" in _gpage and "SIM800C" in _gpage and "SMS on the modem" in _gpage
          and "আপনার বিকাশ কোড" in _gpage and "Modem" in _gpage
          and _rc_file.status_code == 302 and _reconn and _reopened and _rs_rc == 403
          and _off_wait == _gd.IDLE and not _svc.state["connected"],
          "GSM modem service: books a bKash SMS that came in two parts, keeps every SMS for the admin, deletes them from the "
          "SIM, waits for missing parts, reconnects on request, and the page shows Connected, network and signal (%s %s)"
          % (_fm.deleted if _fm else None, _svc.state.get("error")))
    # ---- the USB GSM modem as the SMS gateway: encoding, the service sends the outbox, the HTTP sender leaves it alone
    def _as_deliver(pdu):
        b = bytes.fromhex(pdu)
        tp = b[1:]
        n = (tp[2] + 1) // 2
        rest = tp[4 + n:]
        return (bytes([0, 0x04 | (tp[0] & 0x40)]) + tp[2:4 + n] + rest[:2] + bytes.fromhex("62112211700000") + rest[2:]).hex()

    _known = _G.encode_sms("+46708251358", "hellohello")
    _lt = "Dear customer, " * 25 + "{ok}"
    _lp = [_G.decode_pdu(_as_deliver(x)) for x, _ in _G.encode_sms("8801711000000", _lt, 5)]
    _bt = "প্রিয় গ্রাহক, আপনার বিল ৫০০ টাকা। " * 6
    _bp = [_G.decode_pdu(_as_deliver(x)) for x, _ in _G.encode_sms("01711000000", _bt, 6)]
    check(_known == [("0001000B916407281553F800000AE8329BFD4697D9EC37", 22)]
          and "".join(p["text"] for p in _lp) == _lt and all(p["part"][1] == len(_lp) > 1 for p in _lp)
          and "".join(p["text"] for p in _bp) == _bt and len(_bp) > 1 and _bp[0]["sender"] == "01711000000"
          and _lp[0]["sender"] == "+8801711000000",
          "GSM modem: outgoing SMS encode (GSM alphabet, Bangla, long SMS in joined parts, local and international numbers)")

    class _SendModem(_FakeModem):
        def __init__(self, port, baud):
            _FakeModem.__init__(self, port, baud)
            self.box, self.sent = {}, []
        def send_sms(self, to, text, cc="880"):
            if to.endswith("000000000"):
                raise _G.ModemError("the network refused the SMS: 500")
            self.sent.append((to, text, cc))

    run("UPDATE settings SET svalue = '1' WHERE skey = 'gsm_on'")
    _sv = adm.post("/settings/sms", data={"_csrf": "ci", "sms_via": "modem", "sms_url": "", "sms_method": "GET",
                                          "sms_number_format": "intl", "sms_country_code": "880", "sms_rate": "60"})
    run("UPDATE outbox SET status = 'sent' WHERE channel = 'sms' AND status = 'pending'")
    for _to in ("8801711000001", "8801000000000", "8801711000003"):
        run("INSERT INTO outbox (channel, to_addr, body, created_by) VALUES ('sms', %s, %s, 'ci')", (_to, "CI modem " + _to))
    import sender as _sender
    _sc = db.connect()
    with _sc.cursor() as _cur:
        _http_sent = _sender.work(_sc, _cur, db.load_settings(_cur))
    _sc.close()
    _made2 = []
    _svc2 = _gd.Service(flask_app, opener=lambda p, b: _made2.append(_SendModem(p, b)) or _made2[-1], finder=lambda b: "/dev/ci-modem")
    _svc2.tick()
    _ob = {r["to_addr"]: r for r in run_q("SELECT to_addr, status, tries, error FROM outbox WHERE body LIKE 'CI modem%%'")}
    _svc2.tick()
    _ob2 = {r["to_addr"]: r for r in run_q("SELECT to_addr, status, tries FROM outbox WHERE body LIKE 'CI modem%%'")}
    _G.write_state(dict(_svc2.state))
    _sms_page = adm.get("/settings/sms").get_data(as_text=True)
    _tq = adm.post("/settings/sms/test", data={"_csrf": "ci", "to": "01711000009"})
    _queued = one("SELECT COUNT(*) AS c FROM outbox WHERE channel = 'sms' AND status = 'pending' AND body = 'Test message from your ISP panel.'")["c"]
    _compose = adm.get("/messages/sms").get_data(as_text=True) if any(r.rule == "/messages/sms" for r in flask_app.url_map.iter_rules()) else ""
    run("UPDATE settings SET svalue = 'http' WHERE skey = 'sms_via'")
    run("UPDATE settings SET svalue = '0' WHERE skey = 'gsm_on'")
    _sm = _made2[0] if _made2 else None
    check(_sv.status_code == 302 and _http_sent == 0 and _sm and len(_sm.sent) == 2
          and _ob["8801711000001"]["status"] == "sent" and _ob["8801000000000"]["status"] == "pending"
          and _ob["8801000000000"]["tries"] == 1 and _ob["8801000000000"]["error"].startswith("Modem:")
          and _ob["8801711000003"]["status"] == "pending" and _ob2["8801711000003"]["status"] == "sent"
          and _ob2["8801000000000"]["tries"] == 2
          and _svc2.state.get("sms_sent") == 2
          and "Send SMS through" in _sms_page and "Modem connected" in _sms_page and _tq.status_code == 302 and _queued == 1,
          "USB GSM modem as the SMS gateway: the service sends the waiting customer SMS (2 a round), retries a refused one, "
          "the HTTP sender leaves them alone, and Settings > SMS shows the modem (%s %s)"
          % (_sm.sent if _sm else None, {k: (v["status"], v["tries"]) for k, v in _ob.items()}))

    # ---- Payments > Filter: whose customers paid (Own / a type / one person of that type)
    _adm = client(ids["admin"])
    _rs_name = one("SELECT username FROM panel_admins WHERE id = %s", (ids["reseller"],))["username"]
    _rs_cust = one("SELECT id, username FROM subscribers WHERE created_by = %s ORDER BY id LIMIT 1", (_rs_name,))
    _own_cust = one("SELECT id, username FROM subscribers WHERE created_by NOT IN (SELECT username FROM panel_admins "
                    "WHERE role IN ('reseller', 'subreseller', 'retailer')) ORDER BY id LIMIT 1")
    for _c in (_rs_cust, _own_cust):
        run("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, received_by) "
            "VALUES (%s, %s, 11, 0, 'cash', 'ci-pf', 'admin')", (_c["id"], _c["username"]))
    _pf_res = _adm.get("/payments?q=ci-pf&ptype=reseller&owner=%s" % _rs_name).get_data(as_text=True)
    _pf_type = _adm.get("/payments?q=ci-pf&ptype=reseller").get_data(as_text=True)
    _pf_own = _adm.get("/payments?q=ci-pf&ptype=own").get_data(as_text=True)
    _pf_wrong = _adm.get("/payments?q=ci-pf&ptype=retailer&owner=%s" % _rs_name).get_data(as_text=True)
    _pf_all = _adm.get("/payments").get_data(as_text=True)
    _ub = lambda u: '<b class="mono">%s</b>' % u
    check(_ub(_rs_cust["username"]) in _pf_res and _ub(_own_cust["username"]) not in _pf_res
          and _ub(_rs_cust["username"]) in _pf_type and _ub(_own_cust["username"]) not in _pf_type
          and _ub(_own_cust["username"]) in _pf_own and _ub(_rs_cust["username"]) not in _pf_own
          and "No payments found" in _pf_wrong
          and 'id="payf-dlg"' in _pf_all and 'data-follows="ptype"' in _pf_all and 'data-tier="reseller"' in _pf_all
          and "data-copy-url" in _pf_all and 'class="f-count"' in _pf_res,
          "Payments filter: Profile Type (own / reseller / one reseller) shows only those customers' payments")
    run("DELETE FROM payments WHERE note = 'ci-pf'")

    # ---- Accounting > Cashflow: income and expenses in one book, reseller balance topups as User Payment
    _menu = side_menu(_adm.get("/").get_data(as_text=True))
    _r1 = _adm.post("/cashflow/save", data={"_csrf": "ci", "kind": "income", "spent_on": date.today().isoformat(),
                                            "category": "Installation fee", "amount": "1234.50", "profile": _rs_name,
                                            "method": "cash", "note": "ci-cf income"})
    _adm.post("/cashflow/save", data={"_csrf": "ci", "kind": "expense", "spent_on": date.today().isoformat(),
                                      "category": "CI Generator fuel", "amount": "300", "method": "cash", "note": "ci-cf expense"})
    _bad = _adm.post("/cashflow/save", data={"_csrf": "ci", "kind": "income", "category": "X", "amount": "5", "profile": "nobody-here"})
    _inc = one("SELECT * FROM expenses WHERE note = 'ci-cf income'")
    _exp = one("SELECT * FROM expenses WHERE note = 'ci-cf expense'")
    _newcat = one("SELECT id FROM cashflow_categories WHERE kind = 'expense' AND name = 'CI Generator fuel'")
    run("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) VALUES (%s, 777, 777, 'cash', 'ci-cf top', 'admin')",
        (ids["reseller"],))
    _top = one("SELECT id FROM user_ledger WHERE note = 'ci-cf top'")["id"]
    run("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) VALUES (%s, 55, 55, 'transfer', 'ci-cf move', %s)",
        (ids["subreseller"], _rs_name))
    _move = one("SELECT id FROM user_ledger WHERE note = 'ci-cf move'")["id"]
    _cf = _adm.get("/cashflow").get_data(as_text=True)
    _cf_exp = _adm.get("/cashflow?type=expense").get_data(as_text=True)
    _cf_cat = _adm.get("/cashflow?type=income&category=User+Payment").get_data(as_text=True)
    _csv = _adm.get("/cashflow.csv").get_data(as_text=True)
    _xl = _adm.get("/cashflow.xlsx")
    _old = _adm.get("/expenses")
    _cats = _adm.get("/cashflow/categories").get_data(as_text=True)
    _del_used = _adm.post("/cashflow/categories/%d/delete" % _newcat["id"], data={"_csrf": "ci"}) if _newcat else None
    _still = one("SELECT id FROM cashflow_categories WHERE id = %s", (_newcat["id"],)) if _newcat else None
    _adm.post("/cashflow/categories/save", data={"_csrf": "ci", "id": str(_newcat["id"]) if _newcat else "", "kind": "expense",
                                                 "name": "CI Diesel"})
    _renamed = one("SELECT category FROM expenses WHERE id = %s", (_exp["id"],))["category"] if _exp else ""
    _sum = _adm.get("/accounting/summary")
    _up = one("SELECT id FROM cashflow_categories WHERE kind = 'income' AND name = 'User Payment'")
    _up_ren = _adm.post("/cashflow/categories/save", data={"_csrf": "ci", "id": str(_up["id"]), "kind": "income", "name": "CI Topups"}) if _up else None
    _up_del = _adm.post("/cashflow/categories/%d/delete" % _up["id"], data={"_csrf": "ci"}) if _up else None
    _up_kept = one("SELECT name FROM cashflow_categories WHERE id = %s", (_up["id"],)) if _up else None
    check(_r1.status_code == 302 and _inc and _inc["kind"] == "income" and _inc["profile"] == _rs_name
          and _exp and _exp["kind"] == "expense" and _newcat and not one("SELECT id FROM expenses WHERE category = 'X'")
          and _bad.status_code == 302
          and 'href="/cashflow"' in _menu and 'href="/expenses"' not in _menu
          and "CF%d" % _inc["id"] in _cf and "CF%d" % _exp["id"] in _cf and "TOP%d" % _top in _cf and "TOP%d" % _move not in _cf
          and "User Balance Topup" in _cf and 'id="cff-dlg"' in _cf and 'id="cf-dlg"' in _cf and "data-compact-for" in _cf
          and "CF%d" % _exp["id"] in _cf_exp and "CF%d" % _inc["id"] not in _cf_exp and "TOP%d" % _top not in _cf_exp
          and "TOP%d" % _top in _cf_cat and "CF%d" % _inc["id"] not in _cf_cat
          and "ci-cf income" in _csv and "User Balance Topup" in _csv and _xl.status_code == 200 and _xl.data[:2] == b"PK"
          and _old.status_code == 302 and "/cashflow?type=expense" in _old.headers.get("Location", "")
          and "CI Generator fuel" in _cats and _still and _renamed == "CI Diesel" and _sum.status_code == 200
          and _up_ren.status_code == 302 and _up_del.status_code == 302 and _up_kept and _up_kept["name"] == "User Payment",
          "Cashflow: income and expense entries, reseller balance topups as User Payment (not seller-to-seller moves), "
          "filters, export, categories (in use cannot be deleted; a rename follows on the entries)")
    _rc = client(ids["reseller"])
    check(_rc.get("/cashflow").status_code == 403 and _rc.post("/cashflow/save", data={"_csrf": "ci", "kind": "income",
          "category": "X", "amount": "5"}).status_code == 403, "resellers never open the company's Cashflow")
    # ---- Cashflow > Profile filter (who was recharged how much, balance now), name -> their ledger, fixing a wrong recharge
    _bal0 = one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"]
    run("UPDATE panel_admins SET balance = 100 WHERE id = %s", (ids["reseller"],))
    _pp = _adm.get("/cashflow?ptype=reseller&owner=%s" % _rs_name).get_data(as_text=True)
    _pp_type = _adm.get("/cashflow?ptype=reseller").get_data(as_text=True)
    _pp_wrong = _adm.get("/cashflow?ptype=retailer&owner=%s" % _rs_name).get_data(as_text=True)
    _fix_url = "/cashflow/topup/%d/correct" % _top
    _no_reason = _adm.post(_fix_url, data={"_csrf": "ci", "amount": "700", "reason": ""})
    _n_after_bad = one("SELECT COUNT(*) AS c FROM user_ledger WHERE ref_id = %s", (_top,))["c"]
    _adm.post(_fix_url, data={"_csrf": "ci", "amount": "700", "reason": "typed 777 instead of 700"})
    _adm.post(_fix_url, data={"_csrf": "ci", "amount": "700", "reason": "typed 777 instead of 700"})     # pressed twice
    _n_dup = one("SELECT COUNT(*) AS c FROM user_ledger WHERE ref_id = %s", (_top,))["c"]
    _c1 = one("SELECT amount, balance_after, method FROM user_ledger WHERE ref_id = %s ORDER BY id DESC LIMIT 1", (_top,))
    _bal1 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _too_far = _adm.post(_fix_url, data={"_csrf": "ci", "amount": "0", "reason": "given by mistake"})
    _n_far = one("SELECT COUNT(*) AS c FROM user_ledger WHERE ref_id = %s", (_top,))["c"]
    _cf_fixed = _adm.get("/cashflow?ptype=reseller&owner=%s" % _rs_name).get_data(as_text=True)
    _adm.post(_fix_url, data={"_csrf": "ci", "amount": "0", "reason": "given by mistake", "allow_negative": "1"})
    _bal2 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _move_fix = _adm.post("/cashflow/topup/%d/correct" % _move, data={"_csrf": "ci", "amount": "1", "reason": "not a company recharge"})
    _rc_fix = client(ids["reseller"]).post(_fix_url, data={"_csrf": "ci", "amount": "1", "reason": "reseller tries"})
    _led = _adm.get("/ledger?acct=%s&from=2000-01-01" % _rs_name).get_data(as_text=True)
    _led_corr = _adm.get("/ledger?acct=%s&from=2000-01-01&type=correct" % _rs_name).get_data(as_text=True)
    check("TOP%d" % _top in _pp and "TOP%d" % _move not in _pp and "CF%d" % _inc["id"] in _pp and "CF%d" % _exp["id"] not in _pp
          and "Recharged to %s" % _rs_name in _pp and "777.00" in _pp and ("acct=%s" % _rs_name) in _pp
          and 'data-follows="ptype"' in _cf and "TOP%d" % _top in _pp_type and "TOP%d" % _top not in _pp_wrong,
          "Cashflow profile filter: one reseller's recharges, the total and balance table, and links to their ledger")
    check(_no_reason.status_code == 302 and _n_after_bad == 0 and _n_dup == 1
          and _c1 and _c1["method"] == "correction" and float(_c1["amount"]) == -77 and abs(_bal1 - 23) < 0.001
          and _n_far == 1 and abs(_bal2 - (-677)) < 0.001
          and "700.00" in _cf_fixed and "cf-was" in _cf_fixed and "777.00" in _cf_fixed
          and _move_fix.status_code == 404 and _rc_fix.status_code == 403,
          "a wrong recharge is fixed with a linked Correction (balance moves by the difference; a reason is needed; it will not "
          "go below zero unless allowed; seller-to-seller moves and resellers cannot use it)")
    check("Ledger of" in _led and "acct-kpis" in _led and "led-type t-correction" in _led and "led-type t-correction" in _led_corr
          and "ci-cf top" in _led and "ci-cf move" not in _led_corr,
          "a reseller's name opens their whole ledger: balance, what was recharged, every recharge and correction")
    run("DELETE FROM user_ledger WHERE ref_id = %s", (_top,))
    run("UPDATE panel_admins SET balance = %s WHERE id = %s", (_bal0, ids["reseller"]))
    run("DELETE FROM expenses WHERE note IN ('ci-cf income', 'ci-cf expense')")
    run("DELETE FROM cashflow_categories WHERE name IN ('CI Generator fuel', 'CI Diesel')")
    run("DELETE FROM user_ledger WHERE note IN ('ci-cf top', 'ci-cf move')")

    # ---- Users > Roles ticks really open what they say (office staff, one tick at a time)
    import permcatalog as _pc
    _snap_f = run_q("SELECT role, fkey, allowed FROM role_funcs WHERE role IN ('support', 'technician')")
    _snap_p = run_q("SELECT role, module, level FROM role_perms WHERE role IN ('support', 'technician')")

    def ticks(role, keys):
        data = {"_csrf": "ci"}
        data.update({k: "1" for k in keys})
        return adm.post("/roles/%s/permissions" % role, data=data).status_code

    _gate = {}
    for _mod, _grp, _it in _pc.ALL_ITEMS:
        _g = [x["key"] for c in _grp["cols"] for x in c if "g" in x["flags"]]
        _gate[_it["key"]] = _g[0] if _g else None
    _deps = {"net.live": ["sub.module"], "cfc.module": ["cf.module"], "cfc.add": ["cf.module"], "cfc.edit": ["cf.module"], "cfc.delete": ["cf.module"]}
    _seller_only = {"my_users", "my_user_packages", "add_balance"}
    st = client(ids["staff"])
    _get_rules = {r.endpoint: r for r in flask_app.url_map.iter_rules() if "GET" in r.methods}
    _closed, _shut = [], []
    for _ep, _keys in sorted(_pc.ENDPOINT_FN.items()):
        if _ep not in _get_rules or _ep in _seller_only or SKIP.search(_ep):
            continue
        _k = (_keys,) if isinstance(_keys, str) else _keys
        _k = _k[0]
        _want = {_k} | ({_gate[_k]} if _gate.get(_k) else set()) | set(_deps.get(_k, []))
        _url = re.sub(r"<(?:[a-z]+:)?\w+>", "1", _get_rules[_ep].rule)
        ticks("support", _want)
        if st.get(_url).status_code == 403:
            _closed.append("%s (%s)" % (_ep, _k))
        ticks("support", _want - {_k})
        if st.get(_url).status_code != 403:
            _shut.append("%s (%s)" % (_ep, _k))
    check(not _closed, "every page wired to a tick opens for staff when that tick is given%s" % (": " + ", ".join(_closed) if _closed else ""))
    check(not _shut, "and stays shut without it%s" % (": " + ", ".join(_shut) if _shut else ""))

    # ---- Users / Roles for office staff: by tick, never an administrator, never more than they have
    _base = ["sub.module", "sub.view"]
    _adm_name = one("SELECT username FROM panel_admins WHERE id = %s", (ids["admin"],))["username"]
    ticks("support", _base)
    _u0 = st.get("/admins").status_code
    _r0 = st.get("/roles").status_code
    _menu0 = st.get("/").get_data(as_text=True)
    ticks("support", _base + ["usr.module"])
    _list = st.get("/admins").get_data(as_text=True)
    _menu1 = st.get("/").get_data(as_text=True)
    _v0 = st.get("/admins/%d" % ids["reseller"]).status_code
    _pw0 = st.post("/admins/%d/password" % ids["reseller"], data={"_csrf": "ci", "password": "x"}).status_code
    check(_u0 == 403 and _r0 == 403 and 'href="/admins"' not in _menu0 and 'href="/admins"' in _menu1 and 'href="/roles"' not in _menu1
          and ('href="/admins/%d"' % ids["admin"]) not in _list and ('href="/admins/%d"' % ids["reseller"]) not in _list and _v0 == 403 and _pw0 == 403,
          "staff: User Profile > Module opens Users (menu and list, no administrators in it), nothing more")
    ticks("support", _base + ["usr.module", "usr.view", "usr.password", "usr.balance", "usr.edit", "usr.role", "usr.add", "usr.delete"])
    _vr = st.get("/admins/%d" % ids["reseller"])
    _va = st.get("/admins/%d" % ids["admin"]).status_code
    _pw1 = st.post("/admins/%d/password" % ids["reseller"], data={"_csrf": "ci", "password": "x"}).status_code
    _pwa = st.post("/admins/%d/password" % ids["admin"], data={"_csrf": "ci", "password": "longenough1"}).status_code
    _pws = st.post("/admins/%d/password" % ids["staff"], data={"_csrf": "ci", "password": "longenough1"}).status_code
    _b0 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    st.post("/admins/balance", data={"_csrf": "ci", "user_id": str(ids["reseller"]), "method": "cash", "amount": "5", "note": "ci staff"})
    _b1 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    st.post("/admins/role", data={"_csrf": "ci", "user_id": str(ids["reseller"]), "role": "admin"})
    _still = one("SELECT role FROM panel_admins WHERE id = %s", (ids["reseller"],))["role"]
    _new = {"_csrf": "ci", "full_name": "CI Staff Made", "phone": "01711000111", "email": "ci-made@example.com",
            "identity": "998877", "password": "secret1234", "is_active": "1"}
    st.post("/admins/save", data=dict(_new, username="ci_made_admin", role="admin"))
    st.post("/admins/save", data=dict(_new, username="ci_made_mgr", role="manager", phone="01711000112"))
    st.post("/admins/save", data=dict(_new, username="ci_made_res", role="reseller", phone="01711000113"))
    _made = {r["username"]: r for r in run_q("SELECT id, username, role, created_by FROM panel_admins WHERE username LIKE 'ci_made_%%'")}
    run("INSERT INTO panel_admins (username, full_name, password_hash, role) VALUES ('ci_big_mgr', 'CI Manager', 'x', 'manager')")
    _mgr = one("SELECT id FROM panel_admins WHERE username = 'ci_big_mgr'")["id"]
    _tm = st.post("/admins/%d/toggle" % _mgr, data={"_csrf": "ci"}).status_code
    _mk = st.post("/admins/role", data={"_csrf": "ci", "user_id": str(_made["ci_made_res"]["id"]) if "ci_made_res" in _made else "0", "role": "manager"})
    _mk_role = one("SELECT role FROM panel_admins WHERE username = 'ci_made_res'")
    _del = st.post("/admins/%d/delete" % _made["ci_made_res"]["id"], data={"_csrf": "ci"}).status_code if "ci_made_res" in _made else 0
    check(_vr.status_code == 200 and "Delete Profile" in _vr.get_data(as_text=True) and _va == 403 and _pw1 == 302
          and _pwa == 403 and _pws == 403 and abs(_b1 - _b0 - 5) < 0.001 and _still == "reseller",
          "staff with the User ticks: view, password, balance on a reseller; never on an administrator or themselves; "
          "never make someone an administrator")
    check("ci_made_admin" not in _made and "ci_made_mgr" not in _made and "ci_made_res" in _made
          and _made["ci_made_res"]["created_by"] == one("SELECT username FROM panel_admins WHERE id = %s", (ids["staff"],))["username"]
          and _tm == 403 and _mk_role and _mk_role["role"] == "reseller" and _del == 302
          and not one("SELECT id FROM panel_admins WHERE username = 'ci_made_res'"),
          "staff add / delete users, but never with (or on someone with) a role that can do more than their own")
    run("UPDATE panel_admins SET balance = %s WHERE id = %s", (_b0, ids["reseller"]))
    run("DELETE FROM user_ledger WHERE note = 'ci staff'")
    run("DELETE FROM panel_admins WHERE username IN ('ci_big_mgr', 'ci_made_res')")
    # roles
    ticks("support", _base + ["usr.role_module"])
    _rl = st.get("/roles")
    _rp = st.get("/roles/technician/permissions").get_data(as_text=True)
    _rp_post = st.post("/roles/technician/permissions", data={"_csrf": "ci", "sub.module": "1"}).status_code
    ticks("support", _base + ["usr.role_module", "usr.role_edit"])
    _menu2 = st.get("/").get_data(as_text=True)
    _own = st.post("/roles/support/permissions", data={"_csrf": "ci", "nas.module": "1"}).status_code
    _tech_nas0 = one("SELECT allowed FROM role_funcs WHERE role = 'technician' AND fkey = 'nas.module'")
    _ed = st.post("/roles/technician/permissions", data={"_csrf": "ci", "sub.module": "1", "usr.role_module": "1"}).status_code
    _tech = {r["fkey"]: r["allowed"] for r in run_q("SELECT fkey, allowed FROM role_funcs WHERE role = 'technician'")}
    check(_rl.status_code == 200 and "Set Permission" not in _rl.get_data(as_text=True) and "disabled" in _rp and _rp_post == 403
          and 'href="/roles"' in _menu2 and _own == 403 and _ed == 302 and _tech.get("usr.role_module") == 1
          and _tech.get("nas.module") == (_tech_nas0["allowed"] if _tech_nas0 else 0),   # technicians never have Network
          "staff: User Role > Module shows roles read-only; Edit changes another role, never their own, and only the "
          "ticks they have themselves")
    _rc = client(ids["reseller"])
    check(_rc.get("/admins").status_code == 403 and _rc.get("/roles").status_code == 403,
          "resellers never open Users or Roles (they have My Users)")
    # BTRC: off until ticked, then staff (never resellers) open it
    _bt0 = st.get("/reports/btrc").status_code
    ticks("support", _base + ["rep.module", "rep.btrc", "rep.sales"])
    _bt1 = st.get("/reports/btrc").status_code
    _rep = st.get("/reports").get_data(as_text=True)
    check(_bt0 == 403 and _bt1 == 200 and "tab=traffic" not in _rep and "tab=profit" in _rep,
          "Reports: BTRC opens for staff only with its tick; each report tab follows its own tick")
    # receipts open with Subscribers or Accounting (Activate & Print by a reseller), payment Reverse follows its tick
    _rs_user = one("SELECT username FROM panel_admins WHERE id = %s", (ids["reseller"],))["username"]
    run("INSERT INTO payments (subscriber_id, username, amount, method, days_added, received_by, note) "
        "SELECT id, username, 1, 'cash', 0, %s, 'ci receipt' FROM subscribers WHERE created_by = %s LIMIT 1", (_rs_user, _rs_user))
    _pay = one("SELECT id FROM payments WHERE note = 'ci receipt'")
    _rcpt = _rc.get("/payments/%d/receipt" % _pay["id"]).status_code if _pay else 0
    ticks("support", ["pay.module", "inv.module"])
    _rev0 = st.post("/payments/%d/delete" % _pay["id"], data={"_csrf": "ci"}).status_code if _pay else 0
    ticks("support", ["pay.module", "inv.module", "inv.reverse"])
    _pays = st.get("/payments").get_data(as_text=True)
    _rev1 = st.post("/payments/%d/delete" % _pay["id"], data={"_csrf": "ci"}).status_code if _pay else 0
    check(_pay and _rcpt == 200 and _rev0 == 403 and _rev1 == 302 and not one("SELECT id FROM payments WHERE note = 'ci receipt'")
          and "/delete" in _pays, "a reseller opens the receipt of their own payment; staff reverse a payment only with Reverse")
    run("DELETE FROM payments WHERE note = 'ci receipt'")
    # ---- subscriber ID shown as RN01 (Settings > Subscriber > Subscriber ID Prefix), typed any way
    import core as _core, mod_bksms as _bks
    _s = one("SELECT id, username FROM subscribers ORDER BY id LIMIT 1")
    with flask_app.test_request_context("/"):
        _codes = [_core.parse_sub_code(v) for v in ("RN05", "rn5", "RN-05", "rn 5", "#5", "ID: 5", "5", "05")]
        _bad = [_core.parse_sub_code(v) for v in ("RX05", "RN", "5a", "", "0")]
        _shown = (_core.sub_code(5), _core.sub_code(125))
        _match = _bks.find({"ref": "RN%02d" % _s["id"], "payer": ""})
    _list = adm.get("/subscribers").get_data(as_text=True)
    _search = adm.get("/subscribers?q=RN%02d" % _s["id"]).get_data(as_text=True)
    _view = adm.get("/subscribers/%d" % _s["id"]).get_data(as_text=True)
    check(_codes == [5] * 8 and _bad == [None] * 5 and _shown == ("RN05", "RN125") and _match[0] and _match[0]["id"] == _s["id"]
          and _match[2] and ">RN%02d<" % _s["id"] in _search and _s["username"] in _search and "ID RN%02d" % _s["id"] in _view,
          "subscriber IDs show as RN01; RN05 / rn5 / #5 / 5 all mean subscriber 5 (list, search, profile, bKash reference)")
    adm.post("/settings/subscriber", data={"_csrf": "ci", "sub_id_prefix": ""})
    _plain = adm.get("/subscribers").get_data(as_text=True)
    adm.post("/settings/subscriber", data={"_csrf": "ci", "sub_id_prefix": "rn"})
    _back = one("SELECT svalue FROM settings WHERE skey = 'sub_id_prefix'")
    check(">%d<" % _s["id"] in _plain and _back and _back["svalue"] == "RN",
          "an empty prefix shows the plain number; the prefix is saved in capitals")
    # Subscribers menu pages fill the width with the sidebar open; other pages keep their limit
    _full = [adm.get(u).get_data(as_text=True) for u in ("/subscribers", "/subscribers?status=expired", "/subscribers/charges",
                                                        "/online", "/import", "/subscribers/%d" % _s["id"])]
    _norm = [adm.get(u).get_data(as_text=True) for u in ("/", "/payments")]
    check(all('class="content full"' in h for h in _full) and all('class="content"' in h for h in _norm),
          "every Subscribers menu page uses the full width; other pages do not")
    # Documents ?sub=<id>: a reseller never sees the username of a customer who is not theirs
    _foreign = one("SELECT id, username FROM subscribers WHERE created_by = %s ORDER BY id LIMIT 1", (_adm_name,))
    _rdoc = client(ids["reseller"]).get("/documents?sub=%d" % _foreign["id"])
    _adoc = adm.get("/documents?sub=%d" % _foreign["id"]).get_data(as_text=True)
    check(_rdoc.status_code in (200, 403) and _foreign["username"] not in _rdoc.get_data(as_text=True)
          and _foreign["username"] in _adoc, "Documents: ?sub=<id> does not tell a reseller someone else's customer's username")
    # subscriber list: up to 2000 a page; name above, username under it
    _p2k = adm.get("/subscribers?per=2000").get_data(as_text=True)
    _p5k = adm.get("/subscribers?per=5000").get_data(as_text=True)
    _pay2k = adm.get("/payments?per=2000").get_data(as_text=True)
    _named = one("SELECT id, username, full_name FROM subscribers WHERE full_name <> '' ORDER BY id DESC LIMIT 1")
    _one = adm.get("/subscribers?q=%s" % _named["username"]).get_data(as_text=True)
    check(all(('<option value="%d"' % n) in _p2k for n in (500, 1000, 1500, 2000)) and '<option value="2000" selected' in _p2k
          and '<option value="50" selected' in _p5k and '<option value="2000"' not in _pay2k
          and ('<b>%s</b></a><br><a class="uname"' % _named["full_name"]) in _one,
          "subscriber list: 500 / 1000 / 1500 / 2000 a page (only there); the name with the username under it")
    # user profile > Ledgers: a date range narrows both tables
    _rname = one("SELECT username FROM panel_admins WHERE id = %s", (ids["reseller"],))["username"]
    run("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by, created_at) VALUES "
        "(%s, 7, 7, 'cash', 'ci-ldg-old', 'admin', '2020-01-05 10:00:00')", (ids["reseller"],))
    run("INSERT INTO payments (username, amount, method, note, received_by, paid_at) VALUES "
        "('ci-x', 9, 'cash', 'ci-pay-old', %s, '2020-01-05 10:00:00'), ('ci-x', 9, 'cash', 'ci-pay-new', %s, NOW())", (_rname, _rname))
    _l_all = adm.get("/admins/%d" % ids["reseller"]).get_data(as_text=True)
    _l_old = adm.get("/admins/%d?from=2020-01-01&to=2020-01-31" % ids["reseller"]).get_data(as_text=True)
    _l_bad = adm.get("/admins/%d?from=junk" % ids["reseller"]).status_code
    check("ci-pay-new" in _l_all and 'name="from"' in _l_all
          and "ci-ldg-old" in _l_old and "ci-pay-old" in _l_old and "ci-pay-new" not in _l_old
          and "1 payment received" in _l_old and _l_bad == 200,
          "user profile Ledgers: From / To dates narrow the balance ledger and payments received, with totals")
    # every user (sellers too) sets their own profile picture on My Profile; it shows in the sidebar / top bar
    import io as _io
    _png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    _rc = client(ids["reseller"])
    _rp0 = _rc.get("/profile").get_data(as_text=True)
    _rs = _rc.post("/profile/photo/save", data={"_csrf": "ci", "file": (_io.BytesIO(_png), "me.png")},
                   content_type="multipart/form-data").status_code
    _rrow = one("SELECT photo FROM panel_admins WHERE id = %s", (ids["reseller"],))["photo"]
    _rhome = _rc.get("/").get_data(as_text=True)
    _rimg = _rc.get("/profile/photo")
    _rimg_ok, _rimg_data = _rimg.status_code, _rimg.data
    _rimg.close()                      # send_file keeps the file open until closed (Windows will not delete it)
    _rbad = _rc.post("/profile/photo/save", data={"_csrf": "ci", "file": (_io.BytesIO(b"GIF89a" + b"\0" * 20), "x.gif")},
                     content_type="multipart/form-data")
    _rkeep = one("SELECT photo FROM panel_admins WHERE id = %s", (ids["reseller"],))["photo"]
    _rrm = _rc.post("/profile/photo/save", data={"_csrf": "ci", "remove": "1"}).status_code
    _rgone = one("SELECT photo FROM panel_admins WHERE id = %s", (ids["reseller"],))["photo"]
    _rno = _rc.get("/profile/photo").status_code
    check("Profile picture" in _rp0 and _rs == 302 and _rrow == "png" and "/profile/photo?v=png" in _rhome
          and _rimg_ok == 200 and _rimg_data == _png and _rkeep == "png" and _rrm == 302 and _rgone == "" and _rno == 404,
          "My Profile: a reseller saves, sees and removes their own picture (PNG/JPG only)")
    # sellers see their balance in its own box on the right of the top bar; office users do not
    _bal = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _rtop = client(ids["reseller"]).get("/subscribers").get_data(as_text=True)
    _atop = adm.get("/subscribers").get_data(as_text=True)
    check('class="bal-box' in _rtop and ("{:,.2f}".format(_bal)) in _rtop and 'class="bal-box' not in _atop,
          "top bar: a reseller's balance in its own box on every page; not for office users")
    # ---- field work and owner tools (mod_field.py, mod_ops.py, dailyreport.py)
    import datetime as _dt
    import io as _fio
    _rc2 = client(ids["reseller"])
    _rsub = one("SELECT id, username FROM subscribers WHERE created_by = %s ORDER BY id LIMIT 1", (_rname,))
    _asub = one("SELECT id, username FROM subscribers WHERE created_by = %s ORDER BY id LIMIT 1", (_adm_name,))
    _today = _dt.date.today()
    # call log + follow-ups
    _c1 = _rc2.post("/subscribers/%d/calls" % _rsub["id"], data={"_csrf": "ci", "outcome": "promised", "note": "ci-will-pay-10th",
                                                                 "follow_up": _today.isoformat()}).status_code
    _cpast = _rc2.post("/subscribers/%d/calls" % _rsub["id"], data={"_csrf": "ci", "outcome": "called", "note": "x",
                                                                    "follow_up": (_today - _dt.timedelta(days=2)).isoformat()})
    _cn = one("SELECT COUNT(*) AS c FROM sub_calls WHERE subscriber_id = %s", (_rsub["id"],))["c"]
    _cforeign = _rc2.post("/subscribers/%d/calls" % _asub["id"], data={"_csrf": "ci", "outcome": "called", "note": "ci-foreign"}).status_code
    adm.post("/subscribers/%d/calls" % _asub["id"], data={"_csrf": "ci", "outcome": "no_answer", "note": "ci-admin-call",
                                                          "follow_up": _today.isoformat()})
    _fu_r = _rc2.get("/follow-ups").get_data(as_text=True)
    _fu_a = adm.get("/follow-ups").get_data(as_text=True)
    _sv = adm.get("/subscribers/%d" % _rsub["id"]).get_data(as_text=True)
    _cid = one("SELECT id FROM sub_calls WHERE subscriber_id = %s AND done = 0", (_rsub["id"],))["id"]
    _cid_a = one("SELECT id FROM sub_calls WHERE subscriber_id = %s AND done = 0", (_asub["id"],))["id"]
    _cdone_foreign = _rc2.post("/calls/%d/done" % _cid_a, data={"_csrf": "ci"}).status_code
    _rc2.post("/calls/%d/done" % _cid, data={"_csrf": "ci"})
    _cdone = one("SELECT done FROM sub_calls WHERE id = %s", (_cid,))["done"]
    check(_c1 == 302 and _cn == 1 and _cforeign == 403 and "ci-will-pay-10th" in _fu_r and "ci-admin-call" not in _fu_r
          and "ci-will-pay-10th" in _fu_a and "ci-admin-call" in _fu_a and 'id="tab-calls"' in _sv and "ci-will-pay-10th" in _sv
          and _cdone_foreign == 403 and _cdone == 1,
          "call log: notes with a call-again date show under Follow-ups (sellers: their own customers only), past dates refused, Done clears it")
    # location + customer map
    _l1 = adm.post("/subscribers/%d/location" % _rsub["id"], data={"_csrf": "ci", "where": "https://www.google.com/maps/@23.810331,90.412521,17z"}).status_code
    _lrow = one("SELECT lat, lng FROM subscribers WHERE id = %s", (_rsub["id"],))
    adm.post("/subscribers/%d/location" % _asub["id"], data={"_csrf": "ci", "where": "23.7, 90.35"})
    _lbad = adm.post("/subscribers/%d/location" % _asub["id"], data={"_csrf": "ci", "where": "near the mosque"})
    _arow = one("SELECT lat FROM subscribers WHERE id = %s", (_asub["id"],))
    _md_a = json.loads(adm.get("/map/data").get_data(as_text=True))
    _md_r = json.loads(_rc2.get("/map/data").get_data(as_text=True))
    _mp = adm.get("/map").get_data(as_text=True)
    adm.post("/subscribers/%d/location" % _asub["id"], data={"_csrf": "ci", "remove": "1"})
    _gone = one("SELECT lat FROM subscribers WHERE id = %s", (_asub["id"],))["lat"]
    check(_l1 == 302 and abs(float(_lrow["lat"]) - 23.810331) < 1e-6 and abs(float(_lrow["lng"]) - 90.412521) < 1e-6
          and abs(float(_arow["lat"]) - 23.7) < 1e-6 and {p["id"] for p in _md_a} >= {_rsub["id"], _asub["id"]}
          and _asub["id"] not in {p["id"] for p in _md_r} and _rsub["id"] in {p["id"] for p in _md_r}
          and all(p["s"] in ("online", "offline", "expired", "suspended") for p in _md_a)
          and 'id="cmap"' in _mp and "js/map.js" in _mp and _gone is None,
          "customer location: a Google Maps link or 'lat, lng' sets it, other text is refused; the map shows only the seller's own customers")
    # daily report (page + the Telegram text use the same numbers)
    run("INSERT INTO payments (username, amount, method, note, received_by, paid_at, days_added) VALUES ('ci-x', 321, 'bkash', 'ci-daily', %s, NOW(), 30)", (_adm_name,))
    _dr = adm.get("/reports/daily").get_data(as_text=True)
    _dr_y = adm.get("/reports/daily?day=%s" % (_today - _dt.timedelta(days=1)).isoformat()).status_code
    _dr_r = _rc2.get("/reports/daily").status_code
    import dailyreport as _drm
    _conn = db.connect()
    with _conn.cursor() as _cur:
        _d = _drm.build(_cur, _today)
        _txt = _drm.text(_d, "CI ISP", "BDT")
    _conn.close()
    check("Daily report" in _dr and "Bkash" in _dr and _dr_y == 200 and _dr_r == 403 and _d["pay"]["total"] >= 321
          and _d["pay"]["renewals"] >= 1 and "CI ISP daily report" in _txt and "Collected" in _txt,
          "daily report: today's money by method and person, dues, tickets and follow-ups; office only; the Telegram text matches")
    # monthly statement of sellers
    _st = adm.get("/statements").get_data(as_text=True)
    _st_csv = adm.get("/statements?csv=1")
    _st_r = _rc2.get("/statements").get_data(as_text=True)
    _st_old = adm.get("/statements?month=2020-01").status_code
    check(_rname in _st and "Monthly statement" in _st and _st_csv.status_code == 200 and _st_csv.mimetype == "text/csv"
          and _rname in _st_csv.get_data(as_text=True) and _rname in _st_r and _st_old == 200,
          "monthly statement: each reseller's balance added / used / closing, renewals and customers; CSV; sellers see their own")
    # technician jobs

    run("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, assigned_to, created_by) "
        "VALUES (%s, %s, 'ci-job-fiber', 'connection', 'urgent', 'open', %s, 'admin')", (_rsub["id"], _rsub["username"], ids["admin"]))
    _tid = one("SELECT id FROM tickets WHERE subject = 'ci-job-fiber'")["id"]
    _jb = adm.get("/jobs").get_data(as_text=True)
    _js = adm.post("/jobs/%d/start" % _tid, data={"_csrf": "ci"}).status_code
    _jst = one("SELECT status FROM tickets WHERE id = %s", (_tid,))["status"]
    _jr = _rc2.post("/jobs/%d/done" % _tid, data={"_csrf": "ci", "note": "not mine"}).status_code
    _jshort = adm.post("/jobs/%d/done" % _tid, data={"_csrf": "ci", "note": ""})
    _png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    _jd = adm.post("/jobs/%d/done" % _tid, data={"_csrf": "ci", "note": "Fiber re-spliced, online",
                                                 "photo": (_fio.BytesIO(_png), "w.png")}, content_type="multipart/form-data").status_code
    _jrow = one("SELECT status, closed_at FROM tickets WHERE id = %s", (_tid,))
    _jph = one("SELECT photo FROM ticket_replies WHERE ticket_id = %s AND photo <> ''", (_tid,))
    _jimg = adm.get("/tickets/photo/%s" % _jph["photo"]) if _jph else None
    _jimg_ok = bool(_jimg) and _jimg.status_code == 200 and _jimg.data == _png
    if _jimg:
        _jimg.close()
    _tv = adm.get("/tickets/%d" % _tid).get_data(as_text=True)
    check("ci-job-fiber" in _jb and "tel:" in _jb and "Directions" in _jb and _js == 302 and _jst == "pending" and _jr == 403
          and _jrow["status"] == "resolved" and _jrow["closed_at"] and _jimg_ok and "/tickets/photo/" in _tv
          and adm.get("/tickets/photo/../../etc").status_code == 404,
          "technician jobs: my tickets with call / directions; Start, then Done with a note and a photo (shown on the ticket); not someone else's")
    # dues: reminder SMS to the ticked customers, once a day each
    run("UPDATE subscribers SET expiry_date = CURDATE() - INTERVAL 5 DAY, status = 'active', phone = '01711999888', sms_on = 1 WHERE id = %s", (_rsub["id"],))
    _du = adm.get("/dues?over=4-7").get_data(as_text=True)
    _o0 = one("SELECT COUNT(*) AS c FROM outbox WHERE subscriber_id = %s", (_rsub["id"],))["c"]
    _dr1 = adm.post("/dues/remind", data={"_csrf": "ci", "ids": [str(_rsub["id"])]}).status_code
    _o1 = one("SELECT COUNT(*) AS c FROM outbox WHERE subscriber_id = %s", (_rsub["id"],))["c"]
    adm.post("/dues/remind", data={"_csrf": "ci", "ids": [str(_rsub["id"])]})
    _o2 = one("SELECT COUNT(*) AS c FROM outbox WHERE subscriber_id = %s", (_rsub["id"],))["c"]
    _ob = one("SELECT body FROM outbox WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (_rsub["id"],))["body"]
    _dr_res = _rc2.post("/dues/remind", data={"_csrf": "ci", "ids": [str(_rsub["id"])]}).status_code
    check("data-dues-id" in _du and ('value="%d"' % _rsub["id"]) in _du and "Last call" in _du and _dr1 == 302
          and _o1 == _o0 + 1 and _o2 == _o1 and ("%02d)" % _rsub["id"]) in _ob and "  " not in _ob and _dr_res == 403,
          "dues: tick customers (or everyone shown) and send the due reminder SMS with their ID; at most one a day each")
    run("DELETE FROM role_funcs WHERE role IN ('support', 'technician')")
    run("DELETE FROM role_perms WHERE role IN ('support', 'technician')")
    for r in _snap_f:
        run("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, %s)", (r["role"], r["fkey"], r["allowed"]))
    for r in _snap_p:
        run("INSERT INTO role_perms (role, module, level) VALUES (%s, %s, %s)", (r["role"], r["module"], r["level"]))
    # technician (built-in defaults): fix customers in the field, no money / expiry / delete
    import mod_field as _mf
    if not run_q("SELECT id FROM panel_admins WHERE username = 'ci_tech'"):
        run("INSERT INTO panel_admins (username, full_name, password_hash, role) VALUES ('ci_tech', 'CI Tech', 'x', 'technician')")
    _tc = client(one("SELECT id FROM panel_admins WHERE username = 'ci_tech'")["id"])
    _tv = _tc.get("/subscribers/%d" % _rsub["id"]).status_code
    _te = _tc.get("/subscribers/%d/edit" % _rsub["id"]).get_data(as_text=True)
    _tl = _tc.post("/subscribers/%d/location" % _rsub["id"], data={"_csrf": "ci", "where": "23.811111, 90.422222"}).status_code
    _tdeny = [_tc.get(u).status_code for u in ("/reports/daily", "/dues", "/payments")] + [
        _tc.post("/subscribers/%d/delete" % _rsub["id"], data={"_csrf": "ci"}).status_code,
        _tc.post("/subscribers/%d/balance" % _rsub["id"], data={"_csrf": "ci", "amount": "100"}).status_code,
        _tc.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci"}).status_code]
    _tok = [_tc.get(u).status_code for u in ("/online", "/map", "/jobs", "/follow-ups", "/subscribers/new")]
    _tn = _tc.post("/subscribers/new", data={"_csrf": "ci", "username": "ci.tech.new", "password": "pass1234", "full_name": "Tech New",
                                             "phone": "01711000777", "identity": "19901234567", "plan_id": "1", "status": "active",
                                             "expiry_date": "2099-01-01", "where": "https://www.google.com/maps/place/Home/@23.7,90.3,17z/data=!3d23.712345!4d90.398765"})
    _tnr = one("SELECT expiry_date, lat, lng FROM subscribers WHERE username = 'ci.tech.new'")
    _pp = _tc.get("/").headers.get("Permissions-Policy", "")
    _parsed = [_mf.parse_location(t) for t in ("23.810331,+90.412521", "https://www.google.com/maps/search/23.810331,+90.412521?entry=tts",
                                               "https://maps.google.com/?q=23.810331%2C90.412521", "23.810331 90.412521")]
    check(_tv == 200 and 'name="where"' in _te and "data-loc-here" in _te and _tl == 302 and all(c == 403 for c in _tdeny)
          and all(c == 200 for c in _tok) and _tn.status_code == 302 and _tnr and _tnr["expiry_date"] < _dt.date.today()
          and abs(float(_tnr["lat"]) - 23.712345) < 1e-6 and abs(float(_tnr["lng"]) - 90.398765) < 1e-6
          and "geolocation=(self)" in _pp and all(p == (23.810331, 90.412521) for p in _parsed),
          "technician: sees and fixes customers (address, map location, MAC), adds new ones that start expired; "
          "no money, dues, reports or delete; the panel lets its pages ask for the phone's location")
    # technician: locked areas stay shut even when ticked on Users > Roles; no password change; own home page
    run("INSERT INTO role_perms (role, module, level) VALUES ('technician', 'network', 'edit'), ('technician', 'plans', 'edit') "
        "ON DUPLICATE KEY UPDATE level = 'edit'")
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('technician', 'sub.password', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    _tlock = [_tc.get(u).status_code for u in ("/nas", "/plans", "/my-packages", "/inventory", "/org", "/payments", "/reports/btrc")]
    _tpw = _tc.post("/subscribers/%d/password" % _rsub["id"], data={"_csrf": "ci", "password": "newpass99"}).status_code
    _te2 = _tc.get("/subscribers/%d/edit" % _rsub["id"]).get_data(as_text=True)
    _pw_off = re.search(r'<input id="password"[^>]*disabled', _te2) is not None
    _tsv = _tc.get("/subscribers/%d" % _rsub["id"]).get_data(as_text=True)
    run("DELETE FROM role_perms WHERE role = 'technician'")
    run("DELETE FROM role_funcs WHERE role = 'technician' AND fkey = 'sub.password'")
    _tech_uid = one("SELECT id FROM panel_admins WHERE username = 'ci_tech'")["id"]
    run("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, assigned_to, created_by) "
        "VALUES (%s, %s, 'ci-tech-onu', 'connection', 'high', 'open', %s, 'admin')", (_rsub["id"], _rsub["username"], _tech_uid))
    _ttid = one("SELECT id FROM tickets WHERE subject = 'ci-tech-onu'")["id"]
    _home = _tc.get("/").get_data(as_text=True)
    _ts = _tc.post("/jobs/%d/start" % _ttid, data={"_csrf": "ci", "next": "/"})
    _tu = _tc.post("/jobs/%d/update" % _ttid, data={"_csrf": "ci", "next": "/", "state": "parts", "note": "Need a new ONU"})
    _tu_row = one("SELECT body FROM ticket_replies WHERE ticket_id = %s ORDER BY id DESC LIMIT 1", (_ttid,))["body"]
    _td = _tc.post("/jobs/%d/done" % _ttid, data={"_csrf": "ci", "next": "/", "note": "ONU replaced, online"})
    _td_st = one("SELECT status FROM tickets WHERE id = %s", (_ttid,))["status"]
    _home2 = _tc.get("/").get_data(as_text=True)
    _menu = _home
    check(all(c == 403 for c in _tlock) and _tpw == 403 and _pw_off and 'id="tab-ledgers"' not in _tsv and 'data-tab="payments"' not in _tsv
          and "Next up" in _home and "ci-tech-onu" in _home and 'data-kind=' not in _home
          and 'href="/nas"' not in _menu and 'href="/plans"' not in _menu and 'href="/my-packages"' not in _menu
          and 'href="/inventory"' not in _menu and 'href="/payments"' not in _menu
          and _ts.status_code == 302 and _ts.headers["Location"].endswith("/") and _tu.status_code == 302
          and _tu_row == "Waiting for parts: Need a new ONU" and _td.status_code == 302 and _td_st == "resolved"
          and "ci-tech-onu" in _home2 and "Done today" in _home2,
          "technician: never Network, Accounting, Packages, My Packages, Inventory, Zones or reports (even if ticked); no password "
          "change; home shows their jobs (no graphs or ISP numbers) with Start, Update and Done")

    # ================= tickets v2
    def setv(k, v):
        run("INSERT INTO settings (skey, svalue) VALUES (%s, %s) ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)", (k, v))
    import ticketlib as _tl
    run("INSERT IGNORE INTO areas (name) VALUES ('CI-Area')")
    run("UPDATE subscribers SET area = 'CI-Area', phone = '01711555444', sms_on = 1, status = 'active' WHERE id = %s", (_asub["id"],))
    run("UPDATE subscribers SET phone = '01711555333', sms_on = 1 WHERE id = %s", (_rsub["id"],))
    for k, v in (("sla_on", "1"), ("sla_urgent", "2"), ("ticket_auto_assign", "1"), ("ticket_sms_on", "1"), ("ticket_app_on", "1"),
                 ("ticket_otp_on", "0"), ("portal_on", "1"), ("ticket_portal_on", "1")):
        setv(k, v)
    # settings: area technician, ready reply
    _tset = adm.post("/tickets/settings", data={"_csrf": "ci", "act": "techs", "area__CI-Area": str(_tech_uid)}).status_code
    adm.post("/tickets/settings", data={"_csrf": "ci", "act": "canned_add", "title": "CI restart", "body": "Please restart the router (ci)."})
    _tset_tech = _tc.get("/tickets/settings").status_code
    # new ticket: found by phone, urgent = 2 hours, given to the area technician, customer told
    _lk = json.loads(adm.get("/tickets/lookup?q=01711555444").get_data(as_text=True))
    _lk_res = json.loads(_rc2.get("/tickets/lookup?q=%s" % _asub["username"]).get_data(as_text=True))
    _ob0 = one("SELECT COUNT(*) AS c FROM outbox WHERE subscriber_id = %s", (_asub["id"],))["c"]
    adm.post("/tickets/new", data={"_csrf": "ci", "customer": "01711555444", "subject": "ci-v2-urgent", "category": "connection",
                                   "priority": "urgent", "body": "red light"})
    _t1 = one("SELECT * FROM tickets WHERE subject = 'ci-v2-urgent'")
    _ob1 = run_q("SELECT body FROM outbox WHERE subscriber_id = %s ORDER BY id", (_asub["id"],))[_ob0:]
    _due_h = (_t1["due_at"] - _t1["created_at"]).total_seconds() / 3600 if _t1["due_at"] else 0
    _app = one("SELECT COUNT(*) AS c FROM sub_notifications WHERE subscriber_id = %s AND kind = 'ticket'", (_asub["id"],))["c"]
    adm.post("/tickets/new", data={"_csrf": "ci", "customer": _asub["username"], "subject": "ci-v2-phone", "category": "slow",
                                   "priority": "normal", "solved": "1"})
    _t2 = one("SELECT status, closed_at FROM tickets WHERE subject = 'ci-v2-phone'")
    _tv1 = adm.get("/tickets/%d" % _t1["id"]).get_data(as_text=True)
    _tl1 = adm.get("/tickets?view=free").status_code
    check(_tset == 302 and _tset_tech == 403 and any(r["id"] == _asub["id"] and "online" in r for r in _lk)
          and not any(r["id"] == _asub["id"] for r in _lk_res) and 1.9 < _due_h < 2.1 and _t1["assigned_to"] == _tech_uid
          and _t1["to_office"] == 1 and any("#%d" % _t1["id"] in _b["body"] for _b in _ob1) and len(_ob1) >= 2 and _app >= 2
          and _t2["status"] == "resolved" and _t2["closed_at"] and "CI restart" in _tv1 and "left" in _tv1 and _tl1 == 200,
          "tickets: find the customer by phone; urgent = 2 h limit; the area technician gets it; customer SMS + app notice; "
          "solved on the phone; ready replies; settings for administrators / managers only")
    # alert: nobody took it / past the time limit -> Telegram once
    _sent = []
    import alertlib as _al
    _orig_send = _al.send
    _al.send = lambda s, text: (_sent.append(text) or (True, ""))
    setv("tg_on", "1"); setv("tg_token", "x"); setv("tg_chat", "1"); setv("sla_unassigned_min", "60")
    adm.post("/tickets/new", data={"_csrf": "ci", "subject": "ci-v2-nobody", "category": "other", "priority": "normal"})
    run("UPDATE tickets SET created_at = NOW() - INTERVAL 3 HOUR WHERE subject = 'ci-v2-nobody'")
    run("UPDATE tickets SET due_at = NOW() - INTERVAL 5 MINUTE WHERE id = %s", (_t1["id"],))
    _conn = db.connect()
    with _conn.cursor() as _cur:
        _s = db.load_settings(_cur)
        _n1 = _tl.sla_run(_conn, _cur, _s)
        _n2 = _tl.sla_run(_conn, _cur, _s)
    _conn.close()
    _al.send = _orig_send
    setv("tg_on", "0")
    _late_list = adm.get("/tickets?view=late").get_data(as_text=True)
    check(_n1 >= 2 and _n2 == 0 and _sent and "ci-v2-nobody" in _sent[0] and "ci-v2-urgent" in _sent[0] and "ci-v2-urgent" in _late_list
          and "sla-late" in _late_list,
          "tickets: Telegram alert once for a ticket nobody took and one past its time limit; the list shows it late")
    # resellers: only their own, never internal notes, fix it themselves or send it to the office
    _r_list = _rc2.get("/tickets").get_data(as_text=True)
    _r_foreign = _rc2.get("/tickets/%d" % _t1["id"]).status_code
    _rc2.post("/tickets/new", data={"_csrf": "ci", "kind": "customer", "customer": _rsub["username"], "subject": "ci-v2-res-own",
                                    "category": "slow", "priority": "normal", "handler": "me"})
    _rt = one("SELECT * FROM tickets WHERE subject = 'ci-v2-res-own'")
    adm.post("/tickets/%d/reply" % _rt["id"], data={"_csrf": "ci", "body": "ci-secret-office-note", "internal": "1"})
    _r_view = _rc2.get("/tickets/%d" % _rt["id"]).get_data(as_text=True)
    _a_view = adm.get("/tickets/%d" % _rt["id"]).get_data(as_text=True)
    _r_upd = _rc2.post("/tickets/%d/update" % _rt["id"], data={"_csrf": "ci", "assigned_to": str(ids["admin"])}).status_code
    _r_fw = _rc2.post("/tickets/%d/to-office" % _rt["id"], data={"_csrf": "ci", "note": "tried restart"}).status_code
    _rt2 = one("SELECT to_office, due_at FROM tickets WHERE id = %s", (_rt["id"],))
    _rc2.post("/tickets/new", data={"_csrf": "ci", "kind": "seller", "subject": "ci-v2-res-balance", "category": "balance", "priority": "normal"})
    _rb = one("SELECT to_office, kind, seller, subscriber_id FROM tickets WHERE subject = 'ci-v2-res-balance'")
    _r_bulk = _rc2.post("/tickets/bulk", data={"_csrf": "ci", "ids": [str(_t1["id"])], "action": "resolved"}).status_code
    _r_board = _rc2.get("/tickets/board").status_code
    _r_rep = _rc2.get("/tickets/report").status_code
    check("ci-v2-urgent" not in _r_list and _r_foreign == 403 and _rt["to_office"] == 0 and _rt["seller"] == _rname and _rt["due_at"] is None
          and "ci-secret-office-note" not in _r_view and "ci-secret-office-note" in _a_view and "Send to office" in _r_view
          and _r_upd == 403 and _r_fw == 302 and _rt2["to_office"] == 1 and _rt2["due_at"] is not None
          and _rb["to_office"] == 1 and _rb["kind"] == "seller" and _rb["seller"] == _rname and _rb["subscriber_id"] is None
          and _r_bulk == 403 and _r_board == 302 and _r_rep == 403,
          "tickets: a reseller sees only their downline's tickets (no internal notes), fixes one or sends it to the office, "
          "asks the office for their own needs; no assigning, bulk, board or report")
    # bulk, board, report
    _bk = adm.post("/tickets/bulk", data={"_csrf": "ci", "ids": [str(_rt["id"])], "action": "assign", "assign_to": str(_tech_uid)}).status_code
    _bd = adm.get("/tickets/board").get_data(as_text=True)
    _mv = adm.post("/tickets/%d/move" % _rt["id"], data={"_csrf": "ci", "col": "working"}).status_code
    _mv_st = one("SELECT status FROM tickets WHERE id = %s", (_rt["id"],))["status"]
    _rp = adm.get("/tickets/report?days=30").get_data(as_text=True)
    check(_bk == 302 and one("SELECT assigned_to FROM tickets WHERE id = %s", (_rt["id"],))["assigned_to"] == _tech_uid
          and 'data-col="new"' in _bd and "ci-v2-urgent" in _bd and _mv == 302 and _mv_st == "pending"
          and "Per person" in _rp and "Areas with the most tickets" in _rp,
          "tickets: tick several and assign; board columns with drag / buttons; report per person, area and repeat customers")
    # outages + customer portal: open, follow, reopen, rate
    _og = adm.post("/outages", data={"_csrf": "ci", "area": "CI-Area", "message": "ci fiber cut near the bridge"}).status_code
    _pc = flask_app.test_client()
    with _pc.session_transaction() as _ps:
        _ps["csrf"] = "ci"; _ps["cid"] = _asub["id"]
    _ph = _pc.get("/portal").get_data(as_text=True)
    _psup = _pc.get("/portal/support").get_data(as_text=True)
    _pnew = _pc.post("/portal/support", data={"_csrf": "ci", "category": "connection", "body": "ci portal no internet"}).status_code
    _pt = one("SELECT * FROM tickets WHERE subscriber_id = %s AND source = 'portal' ORDER BY id DESC LIMIT 1", (_asub["id"],))
    _pv = _pc.get("/portal/support/%d" % _pt["id"]).get_data(as_text=True)
    _pforeign = _pc.get("/portal/support/%d" % _rt["id"]).status_code
    adm.post("/tickets/%d/reply" % _pt["id"], data={"_csrf": "ci", "body": "ci office answer", "status": "resolved"})
    _pv2 = _pc.get("/portal/support/%d" % _pt["id"]).get_data(as_text=True)
    _prate = _pc.post("/portal/support/%d/rate" % _pt["id"], data={"_csrf": "ci", "stars": "4", "note": "quick"}).status_code
    _prate2 = _pc.post("/portal/support/%d/rate" % _pt["id"], data={"_csrf": "ci", "stars": "1"}).status_code
    _pr = one("SELECT rating, rating_note FROM tickets WHERE id = %s", (_pt["id"],))
    _pc.post("/portal/support/%d/reply" % _pt["id"], data={"_csrf": "ci", "body": "still down", "reopen": "1"})
    _preopen = one("SELECT status FROM tickets WHERE id = %s", (_pt["id"],))["status"]
    _oid = one("SELECT id FROM outages WHERE message = 'ci fiber cut near the bridge'")["id"]
    adm.post("/outages", data={"_csrf": "ci", "act": "close", "id": str(_oid)})
    _ph2 = _pc.get("/portal").get_data(as_text=True)
    _rated_list = adm.get("/tickets?view=portal").get_data(as_text=True)
    check(_og == 302 and "ci fiber cut near the bridge" in _ph and "ci fiber cut near the bridge" in _psup and _pnew == 302
          and _pt["to_office"] == 1 and _pt["priority"] == "high" and "ci portal no internet" in _pv and _pforeign == 404
          and "ci office answer" in _pv2 and "ps-stars" in _pv2 and _prate == 302 and _pr["rating"] == 4 and _pr["rating_note"] == "quick"
          and _preopen == "open" and "ci fiber cut near the bridge" not in _ph2 and "portal" in _rated_list,
          "tickets: an area outage shows on the portal until fixed; customers open a complaint, follow it, rate it once and reopen it")
    # technician visit: I'm here (GPS), materials out of stock, the customer's code
    run("UPDATE subscribers SET lat = 23.8, lng = 90.4 WHERE id = %s", (_asub["id"],))
    run("INSERT INTO inventory_items (name, unit, qty) VALUES ('CI ONU', 'pcs', 5)")
    _item = one("SELECT id FROM inventory_items WHERE name = 'CI ONU'")["id"]
    _vt = _t1["id"]
    _arr = _tc.post("/jobs/%d/arrived" % _vt, data={"_csrf": "ci", "next": "/", "lat": "23.8005", "lng": "90.4"}).status_code
    _va = one("SELECT arrived_at, arrived_lat, status FROM tickets WHERE id = %s", (_vt,))
    _vnote = one("SELECT body FROM ticket_replies WHERE ticket_id = %s ORDER BY id DESC LIMIT 1", (_vt,))["body"]
    setv("ticket_otp_on", "1")
    _jb = _tc.get("/jobs").get_data(as_text=True)
    _d0 = _tc.post("/jobs/%d/done" % _vt, data={"_csrf": "ci", "next": "/jobs", "note": "replaced ONU", "item": str(_item), "qty": "2"})
    _st0 = one("SELECT status FROM tickets WHERE id = %s", (_vt,))["status"]
    _otp = _tc.post("/jobs/%d/otp" % _vt, data={"_csrf": "ci", "next": "/jobs"})
    _sms = one("SELECT body FROM outbox WHERE subscriber_id = %s AND body LIKE %s ORDER BY id DESC LIMIT 1", (_asub["id"], "%code for complaint%"))
    _code = re.search(r"is (\d{6})", _sms["body"]).group(1) if _sms else ""
    _bad = "000000" if _code != "000000" else "111111"
    _tc.post("/jobs/%d/done" % _vt, data={"_csrf": "ci", "next": "/jobs", "note": "replaced ONU", "otp": _bad})
    _tries = one("SELECT status, otp_tries FROM tickets WHERE id = %s", (_vt,))
    _too_much = _tc.post("/jobs/%d/done" % _vt, data={"_csrf": "ci", "next": "/jobs", "note": "replaced ONU", "otp": _code,
                                                     "item": str(_item), "qty": "99"})
    _st_mid = one("SELECT status FROM tickets WHERE id = %s", (_vt,))["status"]
    _dn = _tc.post("/jobs/%d/done" % _vt, data={"_csrf": "ci", "next": "/jobs", "note": "replaced ONU", "otp": _code,
                                               "item": str(_item), "qty": "2"})
    _vend = one("SELECT status, otp_ok FROM tickets WHERE id = %s", (_vt,))
    _stock = one("SELECT qty FROM inventory_items WHERE id = %s", (_item,))["qty"]
    _mat = one("SELECT qty FROM ticket_materials WHERE ticket_id = %s", (_vt,))
    _move = one("SELECT COUNT(*) AS c FROM inventory_moves WHERE item_id = %s AND kind = 'out'", (_item,))["c"]
    _done_sms = one("SELECT COUNT(*) AS c FROM outbox WHERE subscriber_id = %s AND body LIKE %s", (_asub["id"], "%#" + str(_vt) + " is solved%"))["c"]
    _tview = adm.get("/tickets/%d" % _vt).get_data(as_text=True)
    setv("ticket_otp_on", "0")
    check(_arr == 302 and _va["arrived_at"] and abs(float(_va["arrived_lat"]) - 23.8005) < 1e-6 and _va["status"] == "pending"
          and "m from the saved location" in _vnote and "arrived " in _jb
          and "Customer's code" in _jb and _d0.headers["Location"].endswith("#done-%d" % _vt) and _st0 == "pending"
          and _otp.status_code == 302 and _code and _tries["status"] == "pending" and _tries["otp_tries"] == 1
          and _st_mid == "pending" and _dn.status_code == 302 and _vend["status"] == "resolved" and _vend["otp_ok"] == 1
          and _stock == 3 and _mat and _mat["qty"] == 2 and _move >= 1 and _done_sms >= 1 and "Materials used" in _tview
          and "Arrived" in _tview,
          "technician visit: I'm here with GPS (distance to the saved location), materials taken out of stock, Done needs the "
          "customer's SMS code (wrong code counted), the customer gets the solved SMS with the rating link")
    # staff app: a technician's phone gets a notification when a job is given to them
    import sender as _snd2
    import webpush as _wp2
    _pk = json.loads(_tc.get("/me/push/key").get_data(as_text=True))
    _pk_ok = (len(_pk.get("key") or "") > 80) if _wp2.available() else _pk.get("key") is None
    _keep_av2, _keep_keys2 = _wp2.available, _wp2.keys
    _wp2.available = lambda: True
    _wp2.keys = lambda cur: ("priv", "pub")
    _fake = {"endpoint": "https://fcm.googleapis.com/fcm/send/ci-tech-phone", "keys": {"p256dh": "B" * 87, "auth": "A" * 22}, "first": True}
    _psub = _tc.post("/me/push/subscribe", json=_fake, headers={"X-CSRF-Token": "ci"}).status_code
    _pbad = _tc.post("/me/push/subscribe", json=dict(_fake, endpoint="https://10.0.0.5/x"), headers={"X-CSRF-Token": "ci"}).status_code
    run("UPDATE staff_notifications SET pushed_at = NOW() WHERE pushed_at IS NULL")
    adm.post("/tickets/new", data={"_csrf": "ci", "customer": _asub["username"], "subject": "ci-push-job", "category": "connection",
                                   "priority": "urgent", "assigned_to": str(_tech_uid)})
    _pj = one("SELECT id FROM tickets WHERE subject = 'ci-push-job'")["id"]
    _sn = one("SELECT * FROM staff_notifications WHERE admin_id = %s AND title LIKE %s", (_tech_uid, "%%#%d%%" % _pj))
    adm.post("/tickets/new", data={"_csrf": "ci", "subject": "ci-push-self", "category": "other", "priority": "normal",
                                   "assigned_to": str(ids["admin"])})
    _self = one("SELECT COUNT(*) AS c FROM staff_notifications WHERE title LIKE %s", ("%ci-push-self%",))["c"]
    adm.post("/tickets/new", data={"_csrf": "ci", "subject": "ci-push-bulk", "category": "other", "priority": "normal"})
    _pb = one("SELECT id FROM tickets WHERE subject = 'ci-push-bulk'")["id"]
    adm.post("/tickets/bulk", data={"_csrf": "ci", "ids": [str(_pb)], "action": "assign", "assign_to": str(_tech_uid)})
    _bulk_n = one("SELECT COUNT(*) AS c FROM staff_notifications WHERE admin_id = %s AND title LIKE %s", (_tech_uid, "%ci-push-bulk%"))["c"]
    _ptest = json.loads(_tc.post("/me/push/test", headers={"X-CSRF-Token": "ci"}).get_data(as_text=True))
    _pushed = []
    _c4 = db.connect()
    with _c4.cursor() as _cur:
        _np = _snd2.staff_push_round(_c4, _cur, {"support_email": "noc@example.net"},
                                     send=lambda sub, data, *a, **k: (_pushed.append(data) or (201, "")))
        _cur.execute("SELECT COUNT(*) AS c FROM staff_notifications WHERE pushed_at IS NULL")
        _left = _cur.fetchone()["c"]
        _snd2.staff_push_round(_c4, _cur, {}, send=lambda *a, **k: (201, ""))
        _c4.commit()
    _c4.close()
    run("INSERT INTO staff_notifications (admin_id, title) VALUES (%s, 'ci gone')", (_tech_uid,))
    _c5 = db.connect()
    with _c5.cursor() as _cur:
        _snd2.staff_push_round(_c5, _cur, {}, send=lambda *a, **k: (410, "gone"))
    _c5.close()
    _wp2.available, _wp2.keys = _keep_av2, _keep_keys2
    _gone = one("SELECT COUNT(*) AS c FROM staff_push_subs WHERE admin_id = %s", (_tech_uid,))["c"]
    _th = _tc.get("/").get_data(as_text=True)
    _rhome = _rc2.get("/").get_data(as_text=True)
    _sw = _tc.get("/sw.js").get_data(as_text=True)
    _job_push = next((d for d in _pushed if "ci-push-job" in d["title"]), None)
    check(_pk_ok and _psub == 200 and _pbad == 400 and _sn and _sn["urgent"] == 1 and _sn["link"] == "/jobs"
          and "URGENT" in _sn["title"] and _self == 0 and _bulk_n == 1 and _ptest.get("ok") and _np >= 3 and _left == 0
          and _job_push and _job_push["staff"] == 1 and _job_push["url"] == "/jobs" and _job_push["urgent"] == 1 and _gone == 0
          and "data-staff-push" in _th and "js/staff-push.js" in _th and "js/staff-push.js" not in _rhome
          and "staff-push" in _sw and "requireInteraction" in _sw,
          "staff app: a technician turns on job alerts; every job given to them (new, bulk, board...) rings their phone "
          "(urgent stays on screen), not when they take it themselves; a gone phone is forgotten")
    # monthly clean-up: on the 1st, tickets closed before that month go (numbers kept per month); open ones stay
    _old = "TIMESTAMP(DATE_FORMAT(CURDATE() - INTERVAL 2 MONTH, '%%Y-%%m-10 10:00:00'))"
    run("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, assigned_to, created_by, created_at, "
        "closed_at, due_at, rating, source) VALUES (%s, %s, 'ci-purge-old', 'connection', 'normal', 'resolved', %s, 'admin', "
        + _old + " - INTERVAL 3 HOUR, " + _old + ", " + _old + " + INTERVAL 1 HOUR, 5, 'portal')", (_asub["id"], _asub["username"], _tech_uid))
    run("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, created_by, created_at, closed_at) "
        "VALUES (%s, %s, 'ci-purge-old-open', 'slow', 'normal', 'open', 'admin', " + _old + ", NULL)", (_asub["id"], _asub["username"]))
    run("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, created_by, created_at, closed_at) "
        "VALUES (%s, %s, 'ci-purge-now', 'slow', 'normal', 'closed', 'admin', NOW(), NOW())", (_asub["id"], _asub["username"]))
    _po = one("SELECT id FROM tickets WHERE subject = 'ci-purge-old'")["id"]
    run("INSERT INTO ticket_replies (ticket_id, author, body, internal, photo) VALUES (%s, 'tech', 'Done', 0, %s)", (_po, "t%d-00000000000000aa.png" % _po))
    _pdir = os.path.join(os.environ["PANEL_DATA_DIR"], "ticket_photos")
    os.makedirs(_pdir, exist_ok=True)
    _ppath = os.path.join(_pdir, "t%d-00000000000000aa.png" % _po)
    open(_ppath, "wb").write(b"x")
    _month_key = _dt.date.today().strftime("%Y-%m")
    _old_month = (_dt.date.today().replace(day=1) - _dt.timedelta(days=40)).strftime("%Y-%m")
    setv("ticket_purge_on", "1"); setv("ticket_purge_month", "")
    _c6 = db.connect()
    with _c6.cursor() as _cur:
        _n_old = _cur.execute("SELECT 1 FROM tickets WHERE status IN ('resolved', 'closed') AND COALESCE(closed_at, updated_at) < %s",
                              (_dt.date.today().replace(day=1),))
        _gone1 = _tl.purge_closed(_c6, _cur, db.load_settings(_cur), os.environ["PANEL_DATA_DIR"])
        _gone2 = _tl.purge_closed(_c6, _cur, db.load_settings(_cur), os.environ["PANEL_DATA_DIR"])
    _c6.close()
    _left_t = {r["subject"] for r in run_q("SELECT subject FROM tickets WHERE subject LIKE %s", ("ci-purge-%",))}
    _st_all = one("SELECT * FROM ticket_month_stats WHERE month = %s AND kind = 'all'", (_old_month,))
    _st_tech = one("SELECT * FROM ticket_month_stats WHERE month = %s AND kind = 'tech' AND ref = %s", (_old_month, str(_tech_uid)))
    _rp2 = adm.get("/tickets/report").get_data(as_text=True)
    check(_gone1 == _n_old and _gone1 >= 1 and _gone2 == 0 and _left_t == {"ci-purge-old-open", "ci-purge-now"}
          and not run_q("SELECT id FROM ticket_replies WHERE ticket_id = %s", (_po,)) and not os.path.exists(_ppath)
          and one("SELECT svalue FROM settings WHERE skey = 'ticket_purge_month'")["svalue"] == _month_key
          and _st_all and _st_all["n"] >= 1 and _st_all["rating_n"] >= 1 and _st_all["portal"] >= 1 and _st_all["met"] >= 1
          and _st_tech and _st_tech["n"] >= 1 and _st_tech["fix_sum"] >= 180 and "Past months" in _rp2 and _old_month in _rp2,
          "tickets: on the 1st, tickets closed before that month are removed with their messages and photos (open ones stay); "
          "each month's numbers stay on Report > Past months; once a month only")
    # subscriber list: the expiry shows the date and the time the package ends
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 400 DAY, expiry_time = '14:30' WHERE id = %s", (_asub["id"],))
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 399 DAY, expiry_time = NULL WHERE id = %s", (_rsub["id"],))
    run("UPDATE plans SET fixed_expire_time = NULL WHERE id = (SELECT plan_id FROM subscribers WHERE id = %s)", (_rsub["id"],))
    _sl = adm.get("/subscribers?sort=expiry&dir=desc&per=500").get_data(as_text=True)
    _i_a, _i_r = _sl.find("/subscribers/%d\"" % _asub["id"]), _sl.find("/subscribers/%d\"" % _rsub["id"])
    _cell_a = _sl[_i_a:_i_a + 6000]
    _cell_r = _sl[_i_r:_i_r + 6000]
    check(_i_a != -1 and _i_r != -1 and "02:30 PM" in _cell_a and "11:59 PM" in _cell_r and _i_a < _i_r,
          "subscriber list: Expiration shows the date and the end time (the customer's own, else the package's, else 11:59 PM)")
    # packages: a package given on the Allocation page is never free; a renewal costs the days it adds
    run("INSERT INTO plans (name, label, description, price, validity_days, duration, duration_type, policy_group, download_mbps, upload_mbps) "
        "VALUES ('ciprobe', 'CI probe 20M', 'probe', 800, 30, 30, 'day', '', 20, 20)")
    _ppid = one("SELECT id FROM plans WHERE name = 'ciprobe'")["id"]
    _have = [str(r["plan_id"]) for r in run_q("SELECT plan_id FROM user_plans WHERE user_id = %s", (ids["reseller"],))]
    adm.post("/allocation/save", data={"_csrf": "ci", "user_id": str(ids["reseller"]), "plans": _have + [str(_ppid)]})
    _alloc_price = one("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _ppid))["price"]
    run("UPDATE panel_admins SET balance = 1000 WHERE id = %s", (ids["reseller"],))
    _old_plan = one("SELECT plan_id FROM subscribers WHERE id = %s", (_rsub["id"],))["plan_id"]
    run("UPDATE subscribers SET plan_id = %s WHERE id = %s", (_ppid, _rsub["id"]))
    _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "30", "amount": "0"})
    _b1 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    run("UPDATE user_plans SET price = NULL WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _ppid))
    run("UPDATE panel_admins SET balance = 1000 WHERE id = %s", (ids["reseller"],))
    _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "30", "amount": "0"})
    _b2 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    run("UPDATE user_plans SET price = 400 WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _ppid))
    run("UPDATE panel_admins SET balance = 1000 WHERE id = %s", (ids["reseller"],))
    _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "60", "amount": "0"})
    _b3 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _e0 = one("SELECT expiry_date FROM subscribers WHERE id = %s", (_rsub["id"],))["expiry_date"]
    _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "3650", "amount": "0"})
    _b4 = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _e1 = one("SELECT expiry_date FROM subscribers WHERE id = %s", (_rsub["id"],))["expiry_date"]
    run("UPDATE subscribers SET plan_id = %s WHERE id = %s", (_old_plan, _rsub["id"]))
    _mp = _rc2.get("/my-packages").get_data(as_text=True)
    _none = adm.post("/allocation/save", data={"_csrf": "ci", "user_id": str(ids["subreseller"])}, follow_redirects=True).get_data(as_text=True)
    _blank = adm.post("/plans/save", data={"_csrf": "ci", "id": str(_ppid), "description": "probe", "duration": "30",
                                           "duration_type": "day", "price": "", "pool_name": ""}, follow_redirects=True).get_data(as_text=True)
    _pr_after = float(one("SELECT price FROM plans WHERE id = %s", (_ppid,))["price"])
    check(float(_alloc_price) == 800 and _b1 == 200 and _b2 == 200 and _b3 == 200 and _b4 == 200 and _e1 == _e0
          and "CI probe 20M" in _mp and "no package" in _none and "Enter the price" in _blank and _pr_after == 800,
          "packages: Allocation gives a package at its own price; no price set = the package price, never free; 60 days of a "
          "30-day package cost twice; 10 years at once is refused when the balance cannot pay it; a blank price is refused")
    # monthly renew (Users > Roles > Reseller > Subscriber > Monthly Renew): a 30-day package renews to the same date
    # next month for one month's price
    _rid = ids["reseller"]
    _rperm = adm.get("/roles/reseller/permissions").get_data(as_text=True)
    _ticked0 = 'name="sub.monthly_renew"' in _rperm and 'name="sub.monthly_renew" value="1" checked' not in _rperm
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('reseller', 'sub.monthly_renew', 1) ON DUPLICATE KEY UPDATE allowed = 1")
    _rperm2 = adm.get("/roles/reseller/permissions").get_data(as_text=True)
    run("UPDATE subscribers SET plan_id = %s WHERE id = %s", (_ppid, _rsub["id"]))
    run("UPDATE user_plans SET price = 400 WHERE user_id = %s AND plan_id = %s", (_rid, _ppid))

    def _renew_from(day):
        run("UPDATE subscribers SET expiry_date = %s WHERE id = %s", (day, _rsub["id"]))
        run("UPDATE panel_admins SET balance = 1000 WHERE id = %s", (_rid,))
        _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "30", "amount": "0"})
        return (one("SELECT expiry_date FROM subscribers WHERE id = %s", (_rsub["id"],))["expiry_date"],
                1000 - float(one("SELECT balance FROM panel_admins WHERE id = %s", (_rid,))["balance"]))
    _y = _dt.date.today().year + 1
    _m1 = _renew_from(_dt.date(_y, 1, 5))
    _m2 = _renew_from(_dt.date(_y, 1, 31))
    _m3 = _renew_from(_dt.date(_y, 2, 5))
    run("UPDATE role_funcs SET allowed = 0 WHERE role = 'reseller' AND fkey = 'sub.monthly_renew'")
    _d1 = _renew_from(_dt.date(_y, 1, 5))
    run("DELETE FROM role_funcs WHERE role = 'reseller' AND fkey = 'sub.monthly_renew'")
    run("UPDATE subscribers SET plan_id = %s WHERE id = %s", (_old_plan, _rsub["id"]))
    _feb_end = _dt.date(_y, 3, 1) - _dt.timedelta(days=1)
    check(_ticked0 and "Monthly Renew" in _rperm2
          and _m1 == (_dt.date(_y, 2, 5), 400.0) and _m2 == (_feb_end, 400.0) and _m3 == (_dt.date(_y, 3, 5), 400.0)
          and _d1 == (_dt.date(_y, 2, 4), 400.0),
          "monthly renew: ticked on the Reseller role (Subscriber tab), a 30-day package renews 5 Jan -> 5 Feb "
          "(31 Jan -> end of Feb, 5 Feb -> 5 Mar) for one month's price; off = 30 days")
    _menus = [client(ids[k]).get("/").get_data(as_text=True) for k in ("reseller", "subreseller")]
    _gone = [_rc2.get(u).status_code for u in ("/my-profit", "/my-profit.csv")]
    check(all('href="/my-profit"' not in m and "My Profit" not in m for m in _menus) and _gone == [404, 404]
          and "rep.myprofit" not in adm.get("/roles/reseller/permissions").get_data(as_text=True),
          "My Profit is gone: no menu item, no page, no CSV and no permission tick")
    # activation page: the customer's expiry shows the date and the time it ends
    run("UPDATE subscribers SET expiry_date = '2027-01-17', expiry_time = NULL WHERE id = %s", (_asub["id"],))
    run("UPDATE plans SET fixed_expire_time = NULL WHERE id = (SELECT plan_id FROM subscribers WHERE id = %s)", (_asub["id"],))
    _lk1 = json.loads(adm.get("/subscribers/lookup?q=%s" % _asub["username"]).get_data(as_text=True))["items"]
    run("UPDATE subscribers SET expiry_time = '14:30' WHERE id = %s", (_asub["id"],))
    _lk2 = json.loads(adm.get("/subscribers/lookup?q=%s" % _asub["username"]).get_data(as_text=True))["items"]
    _e1 = next(i["expiry"] for i in _lk1 if i["id"] == _asub["id"])
    _e2 = next(i["expiry"] for i in _lk2 if i["id"] == _asub["id"])
    check(_e1 == "17 Jan 2027 11:59 PM" and _e2 == "17 Jan 2027 2:30 PM",
          "activation page: Expires shows the date and time (the customer's own, else the package's, else 11:59 PM)")
    # Add Package: a mistake keeps what was typed and marks the box; a deleted policy is named; fees are checkboxes
    _fee_id = one("SELECT id FROM fees ORDER BY id LIMIT 1")
    _np = adm.post("/plans/save", data={"_csrf": "ci", "name": "cinewpkg", "description": "CI New Pkg", "price": "750", "duration": "1",
                                        "duration_type": "month", "pool_name": "", "quota_daily_gb": "12",
                                        "fees": [str(_fee_id["id"])] if _fee_id else []})
    _npt = _np.get_data(as_text=True)
    _np2 = adm.post("/plans/save", data={"_csrf": "ci", "name": "cinewpkg", "description": "CI New Pkg", "price": "abc", "duration": "30",
                                         "duration_type": "day", "pool_name": "", "policy_group": ""}).get_data(as_text=True)
    run("UPDATE plans SET policy_group = 'ci-gone-policy' WHERE id = %s", (_ppid,))
    _ed = adm.get("/plans/%d/edit" % _ppid).get_data(as_text=True)
    check(_np.status_code == 200 and 'value="cinewpkg"' in _npt and 'value="CI New Pkg"' in _npt and 'value="750"' in _npt
          and 'value="12"' in _npt and 'option value="month" selected' in _npt and 'field bad' in _npt and "Choose a Policy" in _npt
          and (not _fee_id or ('name="fees" value="%d" checked' % _fee_id["id"]) in _npt)
          and not one("SELECT id FROM plans WHERE name = 'cinewpkg'")
          and "field bad" in _np2 and ("Check the price" in _np2 or "must be a number" in _np2)
          and "ci-gone-policy" in _ed and "was deleted" in _ed and 'type="checkbox" name="fees"' in _ed
          and "Display name (Description)" in _ed and "Saved for later" in _ed and "Not printed yet" in _ed,
          "Add Package: a mistake keeps everything typed and marks the box red; a deleted policy is named; fees are checkboxes; "
          "settings nothing uses yet are labelled so")
    # subscriber page: Why Refused only for one's own customers; balance never below zero; Due counts an expired
    # package; Enable / Disable Net each need their own tick
    _dg_other = _rc2.get("/diagnose?user=%s" % _asub["username"]).get_data(as_text=True)
    _dg_own = _rc2.get("/diagnose?user=%s" % _rsub["username"]).get_data(as_text=True)
    run("UPDATE subscribers SET balance = 50, status = 'active', net_disabled = 0, expiry_date = CURDATE() - INTERVAL 3 DAY "
        "WHERE id = %s", (_asub["id"],))
    adm.post("/subscribers/%d/balance" % _asub["id"], data={"_csrf": "ci", "amount": "-500"})
    _bal_after = float(one("SELECT balance FROM subscribers WHERE id = %s", (_asub["id"],))["balance"])
    _pp = float(one("SELECT p.price FROM subscribers s JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (_asub["id"],))["price"] or 0)
    _sv = adm.get("/subscribers/%d" % _asub["id"]).get_data(as_text=True)
    _due_txt = "%.2f" % max(_pp - 50, 0)
    run("INSERT INTO role_funcs (role, fkey, allowed) VALUES ('support', 'sub.net_on', 1), ('support', 'sub.net_off', 0) "
        "ON DUPLICATE KEY UPDATE allowed = VALUES(allowed)")
    _net_off = st.post("/subscribers/%d/net" % _asub["id"], data={"_csrf": "ci"}).status_code
    _still_on = one("SELECT net_disabled FROM subscribers WHERE id = %s", (_asub["id"],))["net_disabled"]
    run("DELETE FROM role_funcs WHERE role = 'support' AND fkey IN ('sub.net_on', 'sub.net_off')")
    check("No customer of yours" in _dg_other and "Open subscriber" not in _dg_other and "Open subscriber" in _dg_own
          and _bal_after == 50 and ("package expired" in _sv if _pp > 50 else True) and _due_txt in _sv
          and _net_off == 403 and _still_on == 0,
          "subscriber page: Why Refused shows a reseller only their own customers; a balance never goes below zero; "
          "Due includes an expired package; turning the internet off needs Disable Net")
    # side menu: Monthly statement sits under Accounting (after Dues), no longer under Users
    _hm = adm.get("/").get_data(as_text=True)
    _i_dues, _i_st, _i_users = _hm.find('href="/dues"'), _hm.find('href="/statements"'), _hm.find('href="/admins"')
    _acct_end = _hm.find('href="/inventory"')
    _st_support = st.get("/statements").status_code
    check(_i_dues != -1 and _i_dues < _i_st and (_acct_end == -1 or _i_st < _acct_end) and _hm.count('href="/statements"') == 1
          and _st_support == 200,
          "side menu: Monthly statement is under Accounting (once), and Accounting users can open it")

    # ================= permission ticks that used to do nothing
    def rf(role, **keys):
        for k, v in keys.items():
            run("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE allowed = VALUES(allowed)",
                (role, k.replace("__", "."), 1 if v else 0))
    _snap_rf = run_q("SELECT role, fkey, allowed FROM role_funcs WHERE role IN ('support', 'reseller')")
    # a group's Module switch off turns the whole group off (Manage Invoices > View alone opens nothing)
    rf("support", inv__module=0, pay__module=0, inv__view=1)
    _pay_gate = st.get("/payments").status_code
    rf("support", pay__module=1)
    _pay_ok = st.get("/payments").status_code
    # logs: each log page its own tick
    rf("support", log__login=1, log__act=0, log__sess=0)
    _lg = [st.get(u).status_code for u in ("/logs", "/logs?tab=activity", "/logs/sessions")]
    # My Profile > Change Photo
    rf("reseller", prof__photo=0)
    _ph_off = _rc2.post("/profile/photo/save", data={"_csrf": "ci", "remove": "1"}).status_code
    rf("reseller", prof__photo=1)
    _ph_on = _rc2.post("/profile/photo/save", data={"_csrf": "ci", "remove": "1"}).status_code
    # Custom Expiry - Disable Time hides the time box (never for the administrator)
    rf("reseller", sub__custom_expiry_nt=1)
    _act_r = _rc2.get("/subscribers/activation").get_data(as_text=True)
    _act_a = adm.get("/subscribers/activation").get_data(as_text=True)
    # Mid-Period Package Change
    _other = one("SELECT up.plan_id FROM user_plans up WHERE up.user_id = %s AND up.plan_id <> (SELECT plan_id FROM subscribers "
                 "WHERE id = %s) LIMIT 1", (ids["reseller"], _rsub["id"]))
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 10 DAY, status = 'active' WHERE id = %s", (_rsub["id"],))
    import mod_subs as _ms
    _mid = None
    if _other:
        with flask_app.test_request_context("/"):
            from flask import g as _fg
            import core as _core
            _fg.me = one("SELECT id, username, full_name, role, is_active, balance, photo FROM panel_admins WHERE id = %s", (ids["reseller"],))
            _fg.perms = _core.load_perms("reseller")
            _fg.funcs = _core.load_funcs("reseller", _fg.perms)
            _sr = one("SELECT * FROM subscribers WHERE id = %s", (_rsub["id"],))
            _mid = _ms.activate_one(_sr, one("SELECT * FROM plans WHERE id = %s", (_other["plan_id"],)), 30, False, "cash", 0)
            _core.get_db().rollback()
    # Manage Profit: off = the profits stay as they were
    _sub_user = one("SELECT id, username FROM panel_admins WHERE id = %s", (ids["subreseller"],))
    _rp_plan = one("SELECT up.plan_id, up.price FROM user_plans up WHERE up.user_id = %s AND up.price IS NOT NULL LIMIT 1", (ids["reseller"],))
    _prof_off = _prof_on = None
    if _rp_plan:
        rf("reseller", pkg__profit=0)
        _rc2.post("/my-users/%d/packages" % _sub_user["id"], data={"_csrf": "ci", "price_%d" % _rp_plan["plan_id"]: str(_rp_plan["price"]),
                                                                    "sp_%d" % _rp_plan["plan_id"]: "99"})
        _prof_off = one("SELECT sub_profit FROM user_plans WHERE user_id = %s AND plan_id = %s", (_sub_user["id"], _rp_plan["plan_id"]))
        rf("reseller", pkg__profit=1)
        _rc2.post("/my-users/%d/packages" % _sub_user["id"], data={"_csrf": "ci", "price_%d" % _rp_plan["plan_id"]: str(_rp_plan["price"]),
                                                                    "sp_%d" % _rp_plan["plan_id"]: "99"})
        _prof_on = one("SELECT sub_profit FROM user_plans WHERE user_id = %s AND plan_id = %s", (_sub_user["id"], _rp_plan["plan_id"]))
    run("DELETE FROM role_funcs WHERE role IN ('support', 'reseller')")
    for r in _snap_rf:
        run("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, %s)", (r["role"], r["fkey"], r["allowed"]))
    check(_pay_gate == 403 and _pay_ok == 200 and _lg == [200, 403, 403] and _ph_off == 403 and _ph_on == 302
          and '<div class="field" hidden><label class="lbl" for="act-exp-time">' in _act_r
          and '<div class="field" ><label class="lbl" for="act-exp-time">' in _act_a
          and (_mid is None or (not _mid["ok"] and "Mid-Period Package Change" in _mid["error"]))
          and (_prof_off is None or float(_prof_off["sub_profit"] or 0) != 99) and (_prof_on is None or float(_prof_on["sub_profit"]) == 99),
          "permission ticks now work: a group's Module off closes the group; each log page has its own tick; Change Photo; "
          "Disable Time hides the time box; Mid-Period Package Change; Manage Profit")

    # ================= new features behind ticks
    # Transfer Subscribers + Subscriber Limitation
    run("INSERT INTO tickets (subscriber_id, username, subject, status, created_by, seller) VALUES (%s, %s, 'ci-transfer-ticket', 'open', 'admin', '')",
        (_asub["id"], _asub["username"]))
    _old_owner = one("SELECT created_by FROM subscribers WHERE id = %s", (_asub["id"],))["created_by"]
    _tr = adm.post("/subscribers/bulk", data={"_csrf": "ci", "action": "transfer", "to_user": _rname, "ids": [str(_asub["id"])]})
    _tr_owner = one("SELECT created_by FROM subscribers WHERE id = %s", (_asub["id"],))["created_by"]
    _tr_tk = one("SELECT seller FROM tickets WHERE subject = 'ci-transfer-ticket'")["seller"]
    _cnt = one("SELECT COUNT(*) AS c FROM subscribers WHERE created_by = %s", (_rname,))["c"]
    run("UPDATE panel_admins SET max_subs = %s WHERE id = %s", (_cnt, ids["reseller"]))
    _spare = one("SELECT id FROM subscribers WHERE created_by = %s AND id <> %s LIMIT 1", (_old_owner, _asub["id"]))
    _tr2 = adm.post("/subscribers/bulk", data={"_csrf": "ci", "action": "transfer", "to_user": _rname, "ids": [str(_spare["id"])]},
                    follow_redirects=True).get_data(as_text=True) if _spare else ""
    _spare_owner = one("SELECT created_by FROM subscribers WHERE id = %s", (_spare["id"],))["created_by"] if _spare else None
    _new_full = _rc2.post("/subscribers/new", data={"_csrf": "ci", "username": "ci.over.limit", "password": "pass1234", "phone": "",
                                                    "status": "active"}).get_data(as_text=True)
    run("UPDATE panel_admins SET max_subs = NULL WHERE id = %s", (ids["reseller"],))
    _tr_res = _rc2.post("/subscribers/bulk", data={"_csrf": "ci", "action": "transfer", "to_user": _sub_user["username"],
                                                   "ids": [str(_asub["id"])]}).status_code
    adm.post("/subscribers/bulk", data={"_csrf": "ci", "action": "transfer", "to_user": _old_owner, "ids": [str(_asub["id"])]})
    check(_tr.status_code == 302 and _tr_owner == _rname and _tr_tk == _rname and (not _spare or (_spare_owner == _old_owner and "at most" in _tr2))
          and "No room for a new customer" in _new_full and _tr_res == 403
          and one("SELECT created_by FROM subscribers WHERE id = %s", (_asub["id"],))["created_by"] == _old_owner,
          "Transfer Subscribers: moves customers (and their open tickets) to another reseller; off for resellers unless ticked; "
          "Subscriber Limitation stops transfers and new customers past the limit")
    # Login Link
    _ll = adm.post("/subscribers/%d/login-link" % _asub["id"], data={"_csrf": "ci"}).get_data(as_text=True)
    _tok = re.search(r"/portal/link/([A-Za-z0-9_\-]+)", _ll)
    _pc2 = flask_app.test_client()
    _l1 = _pc2.get("/portal/link/%s" % _tok.group(1)) if _tok else None
    with _pc2.session_transaction() as _s2:
        _cid = _s2.get("cid")
    _pc3 = flask_app.test_client()
    _l2 = _pc3.get("/portal/link/%s" % _tok.group(1)) if _tok else None
    check(bool(_tok) and _l1.status_code == 302 and _l1.headers["Location"].endswith("/portal") and _cid == _asub["id"]
          and _l2.status_code == 302 and "/portal/login" in _l2.headers["Location"]
          and one("SELECT COUNT(*) AS c FROM portal_login_links WHERE subscriber_id = %s AND used_at IS NOT NULL", (_asub["id"],))["c"] >= 1,
          "Login Link: a one-time link signs the customer in to the portal once; a second use goes to the login page")
    # Revert Last Invoice (office renewal, then a reseller's renewal with its cost refunded)
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 5 DAY, status = 'active', balance = 0 WHERE id = %s", (_asub["id"],))
    _e_before = one("SELECT expiry_date FROM subscribers WHERE id = %s", (_asub["id"],))["expiry_date"]
    adm.post("/subscribers/%d/renew" % _asub["id"], data={"_csrf": "ci", "days": "30", "amount": "100"})
    _pay = one("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (_asub["id"],))
    _svr = adm.get("/subscribers/%d" % _asub["id"]).get_data(as_text=True)
    _rv = adm.post("/subscribers/%d/revert" % _asub["id"], data={"_csrf": "ci", "payment_id": str(_pay["id"])})
    _e_after = one("SELECT expiry_date FROM subscribers WHERE id = %s", (_asub["id"],))["expiry_date"]
    _gone = not one("SELECT id FROM payments WHERE id = %s", (_pay["id"],))
    run("UPDATE panel_admins SET balance = 1000 WHERE id = %s", (ids["reseller"],))
    run("UPDATE subscribers SET expiry_date = CURDATE() + INTERVAL 5 DAY, status = 'active' WHERE id = %s", (_rsub["id"],))
    _rc2.post("/subscribers/%d/renew" % _rsub["id"], data={"_csrf": "ci", "days": "30", "amount": "0"})
    _b_mid = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    _rpay = one("SELECT id FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 1", (_rsub["id"],))
    _rv_res = _rc2.post("/subscribers/%d/revert" % _rsub["id"], data={"_csrf": "ci", "payment_id": str(_rpay["id"])}).status_code
    adm.post("/subscribers/%d/revert" % _rsub["id"], data={"_csrf": "ci", "payment_id": str(_rpay["id"])})
    _b_back = float(one("SELECT balance FROM panel_admins WHERE id = %s", (ids["reseller"],))["balance"])
    check(_pay["prev_expiry"] == _e_before and "Revert Last Invoice" in _svr and _rv.status_code == 302 and _e_after == _e_before and _gone
          and _b_mid < 1000 and _rv_res == 403 and _b_back == 1000,
          "Revert Last Invoice: the last renewal is undone (expiry back, payment removed, the reseller's cost refunded); off for "
          "resellers unless ticked")
    # exports and the payment gateway log
    _ex = [adm.get(u) for u in ("/logs/export.csv?tab=activity", "/logs/export.csv", "/logs/sessions.csv", "/documents/export.csv",
                                "/inventory/export.csv", "/cashflow/categories.csv", "/logs/payments?csv=1")]
    _pg = adm.get("/logs/payments").get_data(as_text=True)
    _ex_res = _rc2.get("/logs/payments").status_code
    check(all(r.status_code == 200 and r.mimetype == "text/csv" for r in _ex) and "Payment gateway" in _pg and _ex_res == 403,
          "exports: logs, sessions, documents, inventory and cashflow categories as CSV; payment gateway log for the office")
    # invoice (extra charge) mass actions
    run("INSERT INTO sub_charges (subscriber_id, name, amount, created_by) VALUES (%s, 'ci-mass-fee', 77, 'admin')", (_asub["id"],))
    _chid = one("SELECT id FROM sub_charges WHERE name = 'ci-mass-fee'")["id"]
    adm.post("/subscribers/charges/bulk", data={"_csrf": "ci", "action": "paid", "ids": [str(_chid)]})
    _c1 = one("SELECT paid FROM sub_charges WHERE id = %s", (_chid,))["paid"]
    _p1 = one("SELECT COUNT(*) AS c FROM payments WHERE subscriber_id = %s AND note = 'Extra fee: ci-mass-fee'", (_asub["id"],))["c"]
    adm.post("/subscribers/charges/bulk", data={"_csrf": "ci", "action": "unpaid", "ids": [str(_chid)]})
    _c2 = one("SELECT paid FROM sub_charges WHERE id = %s", (_chid,))["paid"]
    _p2 = one("SELECT COUNT(*) AS c FROM payments WHERE subscriber_id = %s AND note = 'Extra fee: ci-mass-fee'", (_asub["id"],))["c"]
    adm.post("/subscribers/charges/bulk", data={"_csrf": "ci", "action": "delete", "ids": [str(_chid)]})
    _chp = adm.get("/subscribers/charges").get_data(as_text=True)
    check(_c1 == 1 and _p1 == 1 and _c2 == 0 and _p2 == 0 and not one("SELECT id FROM sub_charges WHERE id = %s", (_chid,))
          and "data-ch-all" in _chp,
          "invoice mass actions: tick charges, mark paid (payment recorded), mark unpaid (payment removed) or delete")
    # User Packages > Delete
    _rm_used = adm.post("/admins/%d/packages/%d/remove" % (ids["reseller"], one("SELECT plan_id FROM subscribers WHERE id = %s", (_rsub["id"],))["plan_id"]),
                        data={"_csrf": "ci"}, follow_redirects=True).get_data(as_text=True)
    adm.post("/admins/%d/packages/%d/remove" % (ids["reseller"], _ppid), data={"_csrf": "ci"})
    _rm_gone = not one("SELECT plan_id FROM user_plans WHERE user_id = %s AND plan_id = %s", (ids["reseller"], _ppid))
    check("Not removed" in _rm_used and _rm_gone,
          "User Packages > Delete: removes a package from a user, not while their customers use it")
    return bad


# ===================================================================== page walk
SKIP = re.compile(r"ping|sync|test|export|download|backup|kick|disconnect|delete|logout|restore|reload|sample|walk|poll|probe|"
                  r"send|run|bkash_callback|portal_bkash|print_all")


def blocked(*a, **k):
    raise OSError("blocked in the page test (no processes or network)")


def main():
    conn = db.connect()
    print("== seeding sample data")
    ids = seed(conn)
    conn.close()

    subprocess.run = subprocess.Popen = subprocess.check_output = subprocess.call = blocked
    urllib.request.urlopen = blocked
    import app as panel
    flask_app = panel.app
    flask_app.config.update(TESTING=False, PROPAGATE_EXCEPTIONS=True)

    cur_conn = db.connect(); c = cur_conn.cursor()
    c.execute("SELECT token FROM portal_payments WHERE id = 1"); token = c.fetchone()["token"]
    cur_conn.close()
    values = {"key": token, "slug": "bkash", "kind": "logo", "role": "support", "name": "none.sql.gz"}

    rules = [r for r in flask_app.url_map.iter_rules() if "GET" in r.methods and r.endpoint != "static"]
    users = [("admin", ids["admin"]), ("reseller", ids["reseller"]), ("staff", ids["staff"]), ("portal", None)]
    results, failures = {}, []
    for role, uid in users:
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s["csrf"] = "ci"
            if uid:
                s["uid"] = uid
            else:
                s["cid"] = ids["portal_sub"]
        for r in rules:
            if SKIP.search(r.endpoint) or (role == "portal") != r.endpoint.startswith("portal"):
                continue
            url = r.rule
            for arg in r.arguments:
                url = re.sub(r"<(?:[a-z]+:)?%s>" % arg, str(values.get(arg, 1)), url)
            t0 = time.time()
            try:
                code = client.get(url).status_code
                err = None
            except Exception as exc:
                code, err = 500, exc
            ms = int((time.time() - t0) * 1000)
            results[code] = results.get(code, 0) + 1
            if os.environ.get("PAGES_VERBOSE"):
                print("  %s %-8s %s" % (code, role, url))
            if code >= 500:
                where, msg = "panel/app.py", "HTTP %s" % code
                if err is not None:
                    frames = [f for f in traceback.extract_tb(err.__traceback__) if f.filename.startswith(ROOT)]
                    if frames:
                        where = "%s:%s" % (os.path.relpath(frames[-1].filename, ROOT).replace(os.sep, "/"), frames[-1].lineno)
                    msg = "%s: %s" % (type(err).__name__, str(err)[:160])
                failures.append((role, url, r.endpoint, where, msg))
            elif ms > 3000:
                print("  slow: %s %s %d ms" % (role, url, ms))

    print("== results:", ", ".join("%s x%d" % (k, v) for k, v in sorted(results.items())))
    for role, url, ep, where, msg in failures:
        print("FAIL %-8s %-40s %s  (%s)" % (role, url, msg, where))
        if GH:
            f, _, line = where.partition(":")
            print("::error file=%s,line=%s::%s %s (as %s): %s" % (f, line or 1, ep, url, role, msg.replace("\n", " ")))
    print("== %d page(s) failed" % len(failures) if failures else "== all pages answered without errors")
    print("== business rules")
    broken = check_rules(flask_app, ids)
    for b in broken:
        if GH:
            print("::error file=tests/test_pages.py,line=1::Rule failed: %s" % b)
    print("== %d rule(s) failed" % len(broken) if broken else "== all rules hold")
    return 1 if failures or broken else 0


if __name__ == "__main__":
    sys.exit(main())
