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
