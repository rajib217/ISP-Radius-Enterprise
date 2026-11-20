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
