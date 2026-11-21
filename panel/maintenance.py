"""Runs every 5 minutes from a systemd timer.

  * records the number of online users (feeds the dashboard curve)
  * records total download / upload throughput and each session's rate (dashboard bandwidth chart)
  * sends Telegram alerts (router down, customers dropping, server load, FreeRADIUS, daily summary)
  * records CPU, RAM, disk and database size (Reports > Server; kept 180 days)
  * closes sessions that stopped sending updates (power cut, cable pulled)
  * once a day: trims login logs, archives old accounting rows, trims history
"""
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta

import db
import msglib
import alertlib
import oltsync
import radius

BATCH = 20000
MAX_BATCHES = 100
TRAFFIC_KEEP_DAYS = 35


def session_rate(cur_row, prev):
    """(down_bps, up_bps) of one open session.

    Routers only send new counters at each interim update (Acct-Interim-Interval, 10 minutes by default), so the
    rate is the byte difference between the last two updates divided by the time between them. Between updates the
    previous rate is kept. A session seen for the first time gets its average since it started."""
    i, o, upd = int(cur_row["i"] or 0), int(cur_row["o"] or 0), cur_row["upd"]
    if prev is None:
        start = cur_row.get("start")
        secs = (upd - start).total_seconds() if upd and start else 0
        return (int(o * 8 / secs), int(i * 8 / secs)) if secs >= 60 else (0, 0)
    if upd and prev["upd"] and upd > prev["upd"]:
        secs = (upd - prev["upd"]).total_seconds()
        # a counter that went down means the router reset it: use the new value as the bytes for this interval
        d_out = o - prev["out_oct"] if o >= prev["out_oct"] else o
        d_in = i - prev["in_oct"] if i >= prev["in_oct"] else i
        return int(d_out * 8 / secs), int(d_in * 8 / secs)
    return int(prev["down_bps"]), int(prev["up_bps"])


def record_traffic(conn, cur):
    """Save every open session's current rate in traffic_last and the total in traffic_snapshots.
    Download = what the customer receives (Acct-Output-Octets); upload = Acct-Input-Octets."""
    cur.execute("SELECT radacctid, username, acctinputoctets AS i, acctoutputoctets AS o, acctstarttime AS start, "
                "COALESCE(acctupdatetime, acctstarttime) AS upd FROM radacct WHERE acctstoptime IS NULL")
    rows = cur.fetchall()
    cur.execute("SELECT radacctid, in_oct, out_oct, upd, down_bps, up_bps FROM traffic_last")
    prev = {r["radacctid"]: r for r in cur.fetchall()}
    out, down_total, up_total = [], 0, 0
    for r in rows:
        down, up = session_rate(r, prev.get(r["radacctid"]))
        down_total += down
        up_total += up
        out.append((r["radacctid"], (r["username"] or "")[:64], int(r["i"] or 0), int(r["o"] or 0), r["upd"], down, up))
    cur.execute("DELETE FROM traffic_last")
    for k in range(0, len(out), 2000):
        cur.executemany("INSERT INTO traffic_last (radacctid, username, in_oct, out_oct, upd, down_bps, up_bps) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", out[k:k + 2000])
    cur.execute("INSERT INTO traffic_snapshots (ts, down_bps, up_bps, sessions) VALUES (NOW(), %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE down_bps = VALUES(down_bps), up_bps = VALUES(up_bps), sessions = VALUES(sessions)",
                (down_total, up_total, len(out)))
    cur.execute("DELETE FROM traffic_snapshots WHERE ts < NOW() - INTERVAL %s DAY", (TRAFFIC_KEEP_DAYS,))
    conn.commit()
    return down_total, up_total


def record_server_stats(cur):
    """One CPU / RAM / disk / database-size sample of this server (read from /proc, so no extra software is needed)."""
    def cpu_times():
        with open("/proc/stat") as fh:
            v = [int(x) for x in fh.readline().split()[1:9]]
        return sum(v), v[3] + v[4]               # total time, idle + iowait
    t1, i1 = cpu_times()
    time.sleep(1)
    t2, i2 = cpu_times()
    cpu = max(0.0, min(100.0, 100.0 * (1 - (i2 - i1) / (t2 - t1)))) if t2 > t1 else 0.0
    mem = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            mem[k] = int(v.split()[0])
    total = mem["MemTotal"] // 1024
    used = (mem["MemTotal"] - mem.get("MemAvailable", mem.get("MemFree", 0))) // 1024
    disk = shutil.disk_usage("/")
    cur.execute("SELECT COALESCE(SUM(data_length + index_length), 0) AS b FROM information_schema.tables "
                "WHERE table_schema = DATABASE()")
    db_mb = int(cur.fetchone()["b"]) // 1048576
    cur.execute("INSERT INTO server_stats (ts, cpu_pct, load1, mem_used_mb, mem_total_mb, disk_used_gb, disk_total_gb, db_mb) "
                "VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE cpu_pct = VALUES(cpu_pct), "
                "load1 = VALUES(load1), mem_used_mb = VALUES(mem_used_mb), mem_total_mb = VALUES(mem_total_mb), "
                "disk_used_gb = VALUES(disk_used_gb), disk_total_gb = VALUES(disk_total_gb), db_mb = VALUES(db_mb)",
                (round(cpu, 2), round(os.getloadavg()[0], 2), used, total,
                 round(disk.used / 2 ** 30, 2), round(disk.total / 2 ** 30, 2), db_mb))


def delete_in_batches(conn, cur, sql, args):
    total = 0
    for _ in range(MAX_BATCHES):
        cur.execute(sql + " LIMIT %d" % BATCH, args)
        n = cur.rowcount
        conn.commit()
        total += n
        if n < BATCH:
            break
    return total


def archive_accounting(conn, cur, months):
    moved = 0
    for _ in range(MAX_BATCHES):
        cur.execute(
            "SELECT MAX(radacctid) AS m FROM (SELECT radacctid FROM radacct "
            "WHERE acctstoptime IS NOT NULL AND acctstoptime < NOW() - INTERVAL %s MONTH "
            "ORDER BY radacctid LIMIT 5000) t",
            (months,),
        )
        top = cur.fetchone()["m"]
        if top is None:
            break
        cur.execute(
            "INSERT IGNORE INTO radacct_archive SELECT * FROM radacct "
            "WHERE radacctid <= %s AND acctstoptime IS NOT NULL "
            "AND acctstoptime < NOW() - INTERVAL %s MONTH",
            (top, months),
        )
        cur.execute(
            "DELETE FROM radacct WHERE radacctid <= %s AND acctstoptime IS NOT NULL "
            "AND acctstoptime < NOW() - INTERVAL %s MONTH",
            (top, months),
        )
        moved += cur.rowcount
        conn.commit()
    return moved


def send_reminders(conn, cur, s):
    """Queue expiry reminders (SMS / email) for customers whose package ends in N days. Safe to run twice."""
    queued = 0
    for channel in ("sms", "email"):
        days = msglib.parse_days(s["auto_%s_expiry_days" % channel])
        if not days:
            continue
        cur.execute("SELECT subject, body FROM message_templates WHERE kind = %s AND name = 'expiry'", (channel,))
        tpl = cur.fetchone() or msglib.DEFAULT_TEMPLATES[(channel, "expiry")]
        for d in days:
            cur.execute("SELECT s.id, s.username, s.full_name, s.phone, s.email, s.expiry_date, "
                        "p.label AS plan_label, p.price FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
                        "WHERE s.status = 'active' AND s.%s_on = 1 AND s.expiry_date = CURDATE() + INTERVAL %%s DAY" % channel, (d,))
            for sub in cur.fetchall():
                if channel == "sms":
                    addr = msglib.normalize_phone(sub["phone"], s["sms_country_code"], s["sms_number_format"])
                else:
                    addr = (sub["email"] or "").strip()
                    addr = addr if "@" in addr else None
                if not addr:
                    continue
                ctx = msglib.sub_context(sub, s["isp_name"])
                queued += msglib.enqueue(cur, channel, addr, msglib.render(tpl["body"], ctx),
                                         msglib.render(tpl["subject"], ctx), sub["id"],
                                         dedupe="expiry:%s:%s:%s:%s" % (sub["id"], sub["expiry_date"], d, channel))
            conn.commit()
    return queued



def subscriber_automation(conn, cur):
    """Per-customer switches: Auto MAC lock (bind the first device seen) and Auto renew (pay from the balance)."""
    import re
    from datetime import date, timedelta
    import mod_subs
    cfgs = db.load_settings(cur)
    cur.execute("SELECT * FROM plans")
    plans = {p["id"]: p for p in cur.fetchall()}
    done = 0
    mode = cfgs.get("mac_lock_mode", "off")
    if mode == "remove":                      # clear every MAC binding, a batch per run, then switch itself off
        cur.execute("SELECT * FROM subscribers WHERE mac_bind <> '' LIMIT 500")
        rows = cur.fetchall()
        for s in rows:
            cur.execute("UPDATE subscribers SET mac_bind = '' WHERE id = %s", (s["id"],))
            radius.sync_subscriber(cur, s["username"], plans.get(s["plan_id"]), s["status"], s["expiry_date"], s["static_ip"])
            done += 1
        if not rows:
            db.save_setting(cur, "mac_lock_mode", "off")
        conn.commit()
    # NAS: learn the router each customer connects to; follow it when NAS Auto Update is on
    cur.execute("UPDATE subscribers s JOIN (SELECT username, MIN(nasipaddress) AS nas FROM radacct "
                "WHERE acctstoptime IS NULL GROUP BY username) a ON a.username = s.username "
                "SET s.nas_ip = a.nas WHERE s.nas_ip = ''" + (" OR s.nas_ip <> a.nas" if cfgs.get("nas_auto_update") == "1" else ""))
    conn.commit()
    cur.execute("SELECT s.id, MIN(a.callingstationid) AS mac FROM subscribers s JOIN radacct a ON a.username = s.username "
                "AND a.acctstoptime IS NULL WHERE (s.auto_mac = 1 OR %s) AND s.mac_bind = '' AND a.callingstationid <> '' "
                "GROUP BY s.id LIMIT 500", (1 if mode == "lock" else 0,))
    for row in cur.fetchall():
        h = re.sub(r"[^0-9A-Fa-f]", "", row["mac"]).upper()
        if len(h) != 12:
            continue
        cur.execute("UPDATE subscribers SET mac_bind = %s WHERE id = %s AND mac_bind = ''", (":".join(h[i:i + 2] for i in range(0, 12, 2)), row["id"]))
        cur.execute("SELECT * FROM subscribers WHERE id = %s", (row["id"],))
        s = cur.fetchone()
        radius.sync_subscriber(cur, s["username"], plans.get(s["plan_id"]), s["status"], s["expiry_date"], s["static_ip"])
        done += 1
    conn.commit()
    cur.execute("SELECT * FROM subscribers WHERE auto_renew = 1 AND status = 'active' AND plan_id IS NOT NULL "
                "AND expiry_date IS NOT NULL AND expiry_date <= CURDATE() AND net_disabled = 0 LIMIT 500")
    renew_rows = cur.fetchall()
    cur.execute("SELECT id, username, role, parent_id FROM panel_admins")
    blocked = db.blocked_usernames(cur.fetchall(), cfgs)
    for s in renew_rows:
        if s.get("created_by") in blocked:
            continue
        plan = plans.get(s["plan_id"])
        if not plan or not plan.get("validity_days"):
            continue
        price = float(plan["price"] or 0)
        amount = price
        cur.execute("SELECT f.mode, f.value FROM fees f JOIN plan_fees pf ON pf.fee_id = f.id WHERE f.active = 1 AND pf.plan_id = %s",
                    (plan["id"],))
        for f in cur.fetchall():
            amount += price * float(f["value"]) / 100.0 if f["mode"] == "percent" else float(f["value"])
        amount = round(amount, 2)
        have = float(s["balance"] or 0)
        if have + 0.001 < amount:
            if cfgs.get("renew_fail_log") == "1":
                cur.execute("SELECT 1 AS x FROM audit_log WHERE action = 'auto_renew_failed' AND target = %s "
                            "AND created_at >= CURDATE() LIMIT 1", (s["username"],))
                if not cur.fetchone():
                    cur.execute("INSERT INTO audit_log (admin, action, target, detail) VALUES ('auto', 'auto_renew_failed', %s, %s)",
                                (s["username"], "Balance %.2f is below the %.2f needed" % (have, amount)))
            continue
        base = max(s["expiry_date"], date.today())
        new_expiry = mod_subs.plan_expiry(plan, base, int(plan["validity_days"]))
        if plan.get("fixed_expire_day"):
            new_expiry = mod_subs.fixed_expiry(new_expiry, base, plan["fixed_expire_day"])
        # a reseller's customer: the reseller pays their package price from their own balance first
        cur.execute("SELECT id, username, role FROM panel_admins WHERE username = %s", (s.get("created_by"),))
        seller = cur.fetchone()
        paid_by = cost = None
        if seller and seller["role"] in mod_subs.core.SELLER_ROLES:
            cost = mod_subs._price_of(cur, seller["id"], plan["id"])
            paid_by = seller["id"] if cost else None
            why = mod_subs.charge_seller(cur, seller["id"], plan["id"], "Auto renew %s" % s["username"], "auto")
            if why:
                cur.execute("SELECT 1 AS x FROM audit_log WHERE action = 'auto_renew_failed' AND target = %s "
                            "AND created_at >= CURDATE() LIMIT 1", (s["username"],))
                if not cur.fetchone():
                    cur.execute("INSERT INTO audit_log (admin, action, target, detail) VALUES ('auto', 'auto_renew_failed', %s, %s)",
                                (s["username"], "Reseller %s: %s" % (seller["username"], why)))
                continue
        left = round(have - amount, 2)
        cur.execute("UPDATE subscribers SET balance = %s, expiry_date = %s WHERE id = %s", (left, new_expiry, s["id"]))
        cur.execute("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, period_to, received_by, "
                    "payer_id, cost) VALUES (%s, %s, %s, %s, 'balance', 'Auto renew', %s, 'auto', %s, %s)",
                    (s["id"], s["username"], amount, (new_expiry - base).days, new_expiry, paid_by, cost if paid_by else None))
        cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, 'auto')",
                    (s["id"], -amount, left, "Auto renew +%d days" % (new_expiry - base).days))
        radius.sync_subscriber(cur, s["username"], plan, "active", new_expiry, s["static_ip"])
        done += 1
    conn.commit()
    return done


QUOTA_KINDS = ("total", "daily", "weekly")


def _resync(cur, sub, plan):
    cur.execute("SELECT * FROM subscribers WHERE id = %s", (sub["id"],))
    row = cur.fetchone()
    radius.sync_subscriber(cur, row["username"], plan, row["status"], row["expiry_date"], row["static_ip"])


def apply_plan_states(conn, cur, s):
    """Things that change with time and need RADIUS rewritten: expired customers moving into the
    plan's 'next expired action' policy, and customers who passed (or got back under) a data quota."""
    cur.execute("SELECT * FROM plans")
    plans = {p["id"]: p for p in cur.fetchall()}
    changed = 0

    # 1. expired customers whose plan keeps them online in a restricted policy, or (Settings > Expired notice)
    #    in the notice group that shows them the unpaid-bill page; counted from the package's end moment
    notice = radius.EXPIRED_GROUP if s.get("expired_notice_on") == "1" else ""
    if notice:
        radius.ensure_expired_group(cur, s.get("expired_notice_rate", ""))
    for p in plans.values():
        group = p["expired_policy"] or notice
        if not group:
            continue
        cur.execute(
            "SELECT s.id FROM subscribers s JOIN plans p ON p.id = s.plan_id LEFT JOIN radusergroup g "
            "ON g.username = s.username AND g.groupname = %s WHERE s.plan_id = %s AND s.status = 'active' "
            "AND s.expiry_date IS NOT NULL AND " + radius.END_MOMENT + " <= NOW() AND g.username IS NULL LIMIT 2000",
            (group, p["id"]))
        for sub in cur.fetchall():
            _resync(cur, sub, p)
            changed += 1
    if not notice:                 # switched off: customers still in the notice group go back to being refused
        cur.execute("SELECT DISTINCT s.id, s.plan_id FROM radusergroup g JOIN subscribers s ON s.username = g.username "
                    "WHERE g.groupname = %s LIMIT 2000", (radius.EXPIRED_GROUP,))
        for sub in cur.fetchall():
            _resync(cur, sub, plans.get(sub["plan_id"]))
            changed += 1
    conn.commit()

    # 2. data quotas, counted from radacct
    today = date.today()
    weekly_start = {}
    hits = {}
    for p in plans.values():
        for kind in QUOTA_KINDS:
            gb = p["quota_%s_gb" % kind]
            if not gb or float(gb) <= 0:
                continue
            limit = int(float(gb) * 1024 ** 3)
            if kind == "total":
                cur.execute(
                    "SELECT s.id, s.username FROM subscribers s JOIN radacct a ON a.username = s.username "
                    "AND a.acctstarttime >= (s.expiry_date - INTERVAL %s DAY) WHERE s.plan_id = %s "
                    "AND s.status = 'active' GROUP BY s.id, s.username "
                    "HAVING SUM(a.acctinputoctets + a.acctoutputoctets) >= %s",
                    (int(p["validity_days"]), p["id"], limit))
            else:
                if kind == "daily":
                    start = today
                else:
                    back = (today.weekday() + 1 - int(p["quota_weekly_reset"])) % 7   # 0 = Sunday
                    start = today - timedelta(days=back)
                cur.execute(
                    "SELECT s.id, s.username FROM subscribers s JOIN radacct a ON a.username = s.username "
                    "AND a.acctstarttime >= %s WHERE s.plan_id = %s AND s.status = 'active' "
                    "GROUP BY s.id, s.username HAVING SUM(a.acctinputoctets + a.acctoutputoctets) >= %s",
                    (start, p["id"], limit))
            for r in cur.fetchall():
                hits.setdefault(r["id"], kind)
    cur.execute("SELECT id, quota_hit, plan_id, username FROM subscribers WHERE quota_hit <> ''")
    flagged = {r["id"]: r for r in cur.fetchall()}
    port = int(s.get("coa_port") or 3799)
    for sid, kind in hits.items():
        if flagged.get(sid, {}).get("quota_hit") == kind:
            continue
        cur.execute("SELECT id, username, plan_id FROM subscribers WHERE id = %s", (sid,))
        sub = cur.fetchone()
        cur.execute("UPDATE subscribers SET quota_hit = %s WHERE id = %s", (kind, sid))
        _resync(cur, sub, plans.get(sub["plan_id"]))
        conn.commit()
        radius.kick_all(cur, sub["username"], port)      # reconnect lands in the restricted group or is refused
        changed += 1
    for sid, sub in flagged.items():
        if sid not in hits:
            cur.execute("UPDATE subscribers SET quota_hit = '' WHERE id = %s", (sid,))
            _resync(cur, sub, plans.get(sub["plan_id"]))
            conn.commit()
            changed += 1
    return changed


def poll_olts(conn, cur):
    """Read the ONU list of every OLT that has automatic polling on."""
    cur.execute("SELECT d.*, s.* FROM net_devices d JOIN olt_snmp s ON s.device_id = d.id "
                "WHERE d.kind = 'olt' AND s.auto_poll = 1 LIMIT 40")
    done = 0
    for d in cur.fetchall():
        cfg = {k: d[k] for k in ("vendor", "pon_type", "version", "community", "port", "timeout", "retries", "if_regex",
                                 "oid_status", "oid_name", "oid_serial", "oid_rx", "oid_mac", "status_map", "rx_div")}
        ok, msg = oltsync.poll(cur, {"id": d["id"], "ip": d["ip"]}, cfg)
        conn.commit()
        done += ok
    return done


def bkash_sweep():
    """Confirm bKash payments whose callback never arrived (browser closed, timeout). The renewal code needs the
    Flask app, so this runs inside a request context of the panel itself."""
    from app import app
    import bkash
    with app.test_request_context("/maintenance/bkash"):
        return bkash.sweep()


def main():
    conn = db.connect()
    cur = conn.cursor()
    s = db.load_settings(cur)
    import core
    core.apply_timezone(s.get("timezone"))

    cur.execute("SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL")
    online = cur.fetchone()["c"]
    cur.execute("INSERT INTO stats_snapshots (ts, online) VALUES (NOW(), %s) "
                "ON DUPLICATE KEY UPDATE online = VALUES(online)", (online,))
    conn.commit()

    try:
        record_traffic(conn, cur)
    except Exception as exc:   # a missing table (install.sh not re-run yet) must not stop the rest of the job
        conn.rollback()
        print("traffic sample failed: %s" % exc)

    try:
        record_server_stats(cur)
        conn.commit()
    except Exception as exc:   # a failed sample must not stop the rest of the job
        conn.rollback()
        print("server sample failed: %s" % exc)

    try:
        sent = alertlib.run(conn, cur, s)
        if sent:
            print("sent %d Telegram alert(s)" % sent)
    except Exception as exc:   # alerts are a bonus: never let them stop the rest of the job
        conn.rollback()
        print("alerts failed: %s" % exc)

    try:
        fixed = bkash_sweep()
        if fixed:
            print("bKash: settled or closed %d stuck payment(s)" % fixed)
    except Exception as exc:   # bKash being down must not stop the rest of the job
        print("bKash check failed: %s" % exc)

    try:
        n_auto = subscriber_automation(conn, cur)
        if n_auto:
            print("auto MAC lock / auto renew: %d customers" % n_auto)
    except Exception as exc:   # one broken step must not stop the rest of the job
        conn.rollback()
        print("subscriber automation failed: %s" % exc)

    done = apply_plan_states(conn, cur, s)
    if done:
        print("updated %d customers for expiry / quota changes" % done)

    try:
        polled = poll_olts(conn, cur)
        if polled:
            print("read ONUs from %d OLT(s)" % polled)
    except Exception as exc:   # a broken OLT must not stop the rest of the job
        print("OLT polling failed: %s" % exc, file=sys.stderr)

    stale = int(s["stale_minutes"])
    if stale > 0:
        cur.execute(
            "UPDATE radacct SET acctstoptime = COALESCE(acctupdatetime, acctstarttime), "
            "acctterminatecause = 'Stale-Session' "
            "WHERE acctstoptime IS NULL "
            "AND COALESCE(acctupdatetime, acctstarttime) < NOW() - INTERVAL %s MINUTE",
            (stale,),
        )
        closed = cur.rowcount
        conn.commit()
        if closed:
            print("closed %d stale sessions" % closed)

    cur.execute("SELECT svalue FROM settings WHERE skey = 'last_daily'")
    row = cur.fetchone()
    today = date.today().isoformat()
    if datetime.now().hour >= 3 and (not row or row["svalue"] != today):
        n = delete_in_batches(conn, cur,
                              "DELETE FROM radpostauth WHERE authdate < NOW() - INTERVAL %d DAY"
                              % max(int(s["authlog_days"]), 1), ())
        print("deleted %d old login attempts" % n)
        cur.execute("DELETE FROM coa_log WHERE created_at < NOW() - INTERVAL 60 DAY")
        cur.execute("DELETE FROM onu_metrics WHERE ts < NOW() - INTERVAL 8 DAY")
        cur.execute("DELETE FROM stats_snapshots WHERE ts < NOW() - INTERVAL 14 DAY")
        try:
            keep = max(int(s.get("server_stats_days") or 180), 7)
        except ValueError:
            keep = 180
        cur.execute("DELETE FROM server_stats WHERE ts < NOW() - INTERVAL %d DAY" % keep)
        cur.execute("DELETE FROM audit_log WHERE created_at < NOW() - INTERVAL 365 DAY")
        cur.execute("DELETE FROM outbox WHERE status = 'sent' AND created_at < NOW() - INTERVAL 90 DAY")
        conn.commit()
        months = int(s["archive_months"])
        if months > 0:
            print("archived %d accounting rows" % archive_accounting(conn, cur, months))
        db.save_setting(cur, "last_daily", today)
        conn.commit()
        # encrypt any secret settings still stored as plaintext (pre-encryption installs)
        n_enc = db.encrypt_existing_secrets(cur)
        if n_enc:
            conn.commit()
            print("encrypted %d secret settings" % n_enc)
    # expiry reminders once a day at the configured hour (the unique key stops duplicates if it runs again)
    cur.execute("SELECT svalue FROM settings WHERE skey = 'last_reminder'")
    rem = cur.fetchone()
    if datetime.now().hour >= int(s["reminder_hour"]) and (not rem or rem["svalue"] != today):
        print("queued %d reminders" % send_reminders(conn, cur, s))
        db.save_setting(cur, "last_reminder", today)
        conn.commit()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # let systemd record the failure
        print("maintenance failed: %s" % exc, file=sys.stderr)
        sys.exit(1)
