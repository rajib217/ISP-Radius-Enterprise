"""NOC service: router health every 30 seconds, Telegram alerts, and RADIUS disconnects.

Runs as the isp-panel-noc systemd service (one instance only), next to FreeRADIUS and the web panel but
independent of both: restarting the panel or this service never touches FreeRADIUS or a customer's session.

Every 30 seconds:
  * probes all routers at once (ThreadPoolExecutor), each with a 2-second limit for ping and for SNMP, so an
    offline router only costs those 2 seconds and never stops the round;
  * reads CPU %, RAM %, uptime and model over SNMP v2c (HOST-RESOURCES-MIB, which RouterOS supports) when a
    community is set, and counts live PPPoE sessions per router from radacct;
  * sends a Telegram message when a router stops answering for 3 rounds (90 s) or its CPU stays over the limit
    for 2 rounds, and again when it recovers;
  * sends RADIUS Disconnect-Requests (radclient) for sessions the panel queued (coa_queue, e.g. after renewing an
    expired customer) and for customers whose package ran out while they were online.

Each step catches its own errors: a dead router, a slow SNMP agent or a database restart skips that step for one
round, and the loop carries on.
"""
import re
import signal
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime

import alertlib
import core
import db
import radius
import snmplib

INTERVAL = 30          # seconds between rounds
PROBE_TIMEOUT = 2      # seconds per router, for ping and for each SNMP request
MAX_WORKERS = 32
DOWN_ROUNDS = 3        # rounds without an answer before "router down" is sent (90 s)
HOT_ROUNDS = 2         # rounds over the CPU limit before "high CPU" is sent
KICK_BATCH = 50        # disconnects per round at most, so a mass expiry cannot flood the routers
KICK_BACKOFF = 1800    # seconds before a disconnect that failed is tried again for the same session
RUNNING = True

OID_DESCR = "1.3.6.1.2.1.1.1.0"
OID_UPTIME = "1.3.6.1.2.1.1.3.0"              # hundredths of a second
OID_CPU = "1.3.6.1.2.1.25.3.3.1.2"            # hrProcessorLoad, one row per core
OID_ST_TYPE = "1.3.6.1.2.1.25.2.3.1.2"        # hrStorageType
OID_ST_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
OID_ST_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_RAM = "1.3.6.1.2.1.25.2.1.2"
RTT_RE = re.compile(r"time[=<]([\d.]+)\s*ms")

_failed_kicks = {}     # radacctid -> time of the last failed disconnect


def stop(*_):
    global RUNNING
    RUNNING = False


def log(msg):
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ===================================================================== probing
def ping(ip):
    """(answered, rtt_ms). answered is None when the ping command is missing."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", str(PROBE_TIMEOUT), "-n", "--", ip],
                           capture_output=True, text=True, timeout=PROBE_TIMEOUT + 1)
    except FileNotFoundError:
        return None, None
    except subprocess.TimeoutExpired:
        return False, None
    m = RTT_RE.search(r.stdout or "")
    return r.returncode == 0, (float(m.group(1)) if m else None)


def snmp_read(ip, community, port):
    """CPU, RAM, uptime and model of a router. Raises snmplib.SnmpError when it does not answer."""
    c = snmplib.Snmp(ip, community, "v2c", port, PROBE_TIMEOUT, 0)
    try:
        ticks = c.get(OID_UPTIME)
        out = {"uptime_s": int(ticks) // 100 if isinstance(ticks, int) else None,
               "board": snmplib.text(c.get(OID_DESCR))[:160]}
        loads = [v for _, v in c.walk(OID_CPU, max_rows=256) if isinstance(v, int)]
        out["cpu_pct"] = round(sum(loads) / float(len(loads)), 1) if loads else None
        out["ram_pct"] = None
        for oid, kind in c.walk(OID_ST_TYPE, max_rows=64):
            if kind == HR_RAM:
                idx = oid[-1]
                size, used = c.get("%s.%d" % (OID_ST_SIZE, idx)), c.get("%s.%d" % (OID_ST_USED, idx))
                if isinstance(size, int) and isinstance(used, int) and size > 0:
                    out["ram_pct"] = round(100.0 * used / size, 1)
                break
        return out
    finally:
        c.close()


def probe(router, default_community):
    """Everything we can learn about one router within the time limit. Never raises."""
    res = {"reachable": False, "rtt_ms": None, "snmp_ok": False, "cpu_pct": None, "ram_pct": None,
           "uptime_s": None, "board": "", "error": ""}
    try:
        ok, rtt = ping(router["nasname"])
        res["rtt_ms"] = rtt
        community = (router.get("snmp_community") or default_community or "").strip()
        if community and ok is not False:
            try:
                res.update(snmp_read(router["nasname"], community, int(router.get("snmp_port") or 161)))
                res["snmp_ok"] = True
            except snmplib.SnmpError as exc:
                res["error"] = str(exc)[:255]
        elif community and ok is False:
            res["error"] = "No ping reply"
        res["reachable"] = bool(ok) or res["snmp_ok"]
        if ok is None and not community:
            res["error"] = "ping is not installed and no SNMP community is set"
    except Exception as exc:          # one odd router must never stop the others
        res["error"] = ("probe failed: %s" % exc)[:255]
    return res


def probe_all(routers, default_community):
    if not routers:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(routers)))
    try:
        futures = {pool.submit(probe, r, default_community): r["id"] for r in routers}
        # 2 s ping + up to a few 2 s SNMP requests; anything slower than this counts as no answer this round
        done, _ = wait(futures, timeout=PROBE_TIMEOUT * 6)
        out = {}
        for f, rid in futures.items():
            out[rid] = f.result() if f in done else {"reachable": False, "error": "no answer within the time limit"}
        return out
    finally:
        pool.shutdown(wait=False)


# ===================================================================== health round
def health_round(conn, cur, s):
    cur.execute("SELECT id, shortname, nasname, snmp_community, snmp_port FROM nas ORDER BY id LIMIT 500")
    routers = cur.fetchall()
    cur.execute("SELECT nasipaddress AS ip, COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL GROUP BY nasipaddress")
    ppp = {r["ip"]: int(r["c"]) for r in cur.fetchall()}
    cur.execute("SELECT * FROM nas_health")
    prev = {r["nas_id"]: r for r in cur.fetchall()}
    conn.commit()        # end the read snapshot before the slow part

    results = probe_all(routers, s.get("noc_snmp_community", ""))
    try:
        limit = float(s.get("noc_cpu_alert") or 85)
    except ValueError:
        limit = 85.0
    alerts = []
    for r in routers:
        res, p = results.get(r["id"], {}), prev.get(r["id"]) or {}
        label = "%s (%s)" % (r["shortname"] or r["nasname"], r["nasname"])
        up = bool(res.get("reachable"))
        fails = 0 if up else int(p.get("fails") or 0) + 1
        cpu = res.get("cpu_pct")
        hot = int(p.get("hot") or 0) + 1 if (cpu is not None and cpu > limit) else 0
        down_alerted, cpu_alerted = int(p.get("down_alerted") or 0), int(p.get("cpu_alerted") or 0)
        if fails >= DOWN_ROUNDS and not down_alerted:
            down_alerted = 1
            alerts.append("🔴 Router OFFLINE: %s. No answer for %d seconds (%s)." % (label, fails * INTERVAL, res.get("error") or "no reply"))
        elif up and down_alerted:
            down_alerted = 0
            alerts.append("✅ Router back online: %s." % label)
        if hot >= HOT_ROUNDS and not cpu_alerted:
            cpu_alerted = 1
            alerts.append("🔥 High CPU on %s: %.0f%% (limit %.0f%%). %d PPPoE sessions." % (label, cpu, limit, ppp.get(r["nasname"], 0)))
        elif cpu_alerted and cpu is not None and cpu < limit - 10:
            cpu_alerted = 0
            alerts.append("✅ CPU back to normal on %s: %.0f%%." % (label, cpu))
        cur.execute(
            "INSERT INTO nas_health (nas_id, checked_at, reachable, rtt_ms, snmp_ok, cpu_pct, ram_pct, uptime_s, board, pppoe, "
            "error, fails, hot, down_alerted, cpu_alerted) VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE checked_at = NOW(), reachable = VALUES(reachable), rtt_ms = VALUES(rtt_ms), "
            "snmp_ok = VALUES(snmp_ok), cpu_pct = VALUES(cpu_pct), ram_pct = VALUES(ram_pct), uptime_s = VALUES(uptime_s), "
            "board = IF(VALUES(board) = '', board, VALUES(board)), pppoe = VALUES(pppoe), error = VALUES(error), "
            "fails = VALUES(fails), hot = VALUES(hot), down_alerted = VALUES(down_alerted), cpu_alerted = VALUES(cpu_alerted)",
            (r["id"], 1 if up else 0, res.get("rtt_ms"), 1 if res.get("snmp_ok") else 0, cpu, res.get("ram_pct"),
             res.get("uptime_s"), res.get("board") or "", ppp.get(r["nasname"], 0), (res.get("error") or "")[:255],
             fails, hot, down_alerted, cpu_alerted))
        # the Routers page shows the same state; "down" only after the same 3 rounds, so one lost reply does not flap it
        if up or fails >= DOWN_ROUNDS:
            cur.execute("UPDATE nas SET last_status = %s, last_checked = NOW() WHERE id = %s", ("up" if up else "down", r["id"]))
    if routers:
        cur.execute("DELETE FROM nas_health WHERE nas_id NOT IN (%s)" % ",".join(["%s"] * len(routers)), [r["id"] for r in routers])
    else:
        cur.execute("DELETE FROM nas_health")
    cur.execute("INSERT INTO settings (skey, svalue) VALUES ('noc_heartbeat', %s) "
                "ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    down = {r["nasname"] for r in routers if not results.get(r["id"], {}).get("reachable")}
    if alerts and s.get("al_noc", "1") == "1" and s.get("tg_on") == "1":
        ok, err = alertlib.send(s, "📡 %s\n%s" % (s.get("isp_name") or "RADIUS panel", "\n".join(alerts)))
        log("telegram: %d alert(s) %s" % (len(alerts), "sent" if ok else "NOT sent: " + err))
    return down


# ===================================================================== disconnects
def _disconnect(row, secret, port):
    try:
        return radius.disconnect_session(row["nasipaddress"], secret, port, row["username"], row["acctsessionid"], row["framedipaddress"])
    except Exception as exc:
        return False, "radclient failed: %s" % exc


# When an account ends, exactly as FreeRADIUS's Expiration sees it (radius.sync_subscriber): the subscriber's own time,
# else the package's fixed time, else the END of the day (23:59:59). A grace period runs to the end of its day.
END_MOMENT = radius.END_MOMENT        # one definition, shared with maintenance.py

def coa_round(conn, cur, s, down_routers):
    """Send queued disconnects and drop sessions of customers whose package ran out. Returns (sent, failed)."""
    port = int(s.get("coa_port") or 3799)
    cur.execute("SELECT nasname, secret FROM nas WHERE coa_enabled = 1")
    secrets = {r["nasname"]: r["secret"] for r in cur.fetchall()}
    todo = []

    # 1) asked for by the panel (renewal of an expired customer, ...)
    cur.execute("SELECT id, username, reason FROM coa_queue ORDER BY id LIMIT %s", (KICK_BATCH,))
    queued = cur.fetchall()
    for qrow in queued:
        cur.execute("SELECT radacctid, username, nasipaddress, acctsessionid, framedipaddress FROM radacct "
                    "WHERE username = %s AND acctstoptime IS NULL", (qrow["username"],))
        todo += [dict(r, why=qrow["reason"] or "queued") for r in cur.fetchall()]
    if queued:
        cur.execute("DELETE FROM coa_queue WHERE id IN (%s)" % ",".join(["%s"] * len(queued)), [r["id"] for r in queued])
    cur.execute("DELETE FROM coa_queue WHERE created_at < NOW() - INTERVAL 1 DAY")

    # 2) package ran out while online: the session started before the Expiration moment, so FreeRADIUS never saw it end.
    #    A customer moved into an "expired" policy reconnects into that policy; others are refused at the next login.
    if s.get("auto_kick_expired", "1") == "1" and len(todo) < KICK_BATCH:
        cur.execute(
            "SELECT ra.radacctid, ra.username, ra.nasipaddress, ra.acctsessionid, ra.framedipaddress FROM radacct ra "
            "JOIN subscribers s ON s.username = ra.username LEFT JOIN plans p ON p.id = s.plan_id "
            "WHERE ra.acctstoptime IS NULL AND s.status = 'active' AND s.expiry_date IS NOT NULL "
            "AND " + END_MOMENT + " <= NOW() AND ra.acctstarttime < " + END_MOMENT + " "
            "ORDER BY ra.radacctid LIMIT %s", (KICK_BATCH * 2,))
        todo += [dict(r, why="expired") for r in cur.fetchall()]
        # rewrite their RADIUS rows first: the reconnect then lands in the expired policy / notice page at once,
        # instead of being refused until the next maintenance run
        for uname in sorted({r["username"] for r in todo if r["why"] == "expired"}):
            radius.resync_username(cur, uname)
    conn.commit()

    now, seen, jobs = time.time(), set(), []
    for r in todo:
        if r["radacctid"] in seen or len(jobs) >= KICK_BATCH:
            continue
        seen.add(r["radacctid"])
        if now - _failed_kicks.get(r["radacctid"], 0) < KICK_BACKOFF:
            continue                      # tried recently and the router refused: do not hammer it
        if r["nasipaddress"] in down_routers or not secrets.get(r["nasipaddress"]):
            continue                      # router offline, or CoA switched off for it
        jobs.append(r)
    if not jobs:
        return 0, 0
    with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as pool:
        results = list(pool.map(lambda r: _disconnect(r, secrets[r["nasipaddress"]], port), jobs))
    sent = failed = 0
    for r, (ok, msg) in zip(jobs, results):
        radius.log_coa(cur, r["username"], r["nasipaddress"], ok, "%s: %s" % (r["why"], msg), "noc", s.get("coa_log", "1"))
        if ok:
            sent += 1
            _failed_kicks.pop(r["radacctid"], None)
        else:
            failed += 1
            _failed_kicks[r["radacctid"]] = now
    for k in [k for k, t in _failed_kicks.items() if now - t > KICK_BACKOFF * 2]:
        del _failed_kicks[k]
    conn.commit()
    return sent, failed


# ===================================================================== loop
def one_round():
    conn = db.connect()
    try:
        cur = conn.cursor()
        s = db.load_settings(cur)
        core.apply_timezone(s.get("timezone"))     # log lines and the heartbeat use the panel's Local Timezone too
        conn.commit()
        down = set()
        try:
            down = health_round(conn, cur, s)
        except Exception:
            conn.rollback()
            log("health round failed:\n" + traceback.format_exc(limit=3))
        try:
            sent, failed = coa_round(conn, cur, s, down)
            if sent or failed:
                log("disconnects: %d sent, %d failed" % (sent, failed))
        except Exception:
            conn.rollback()
            log("disconnect round failed:\n" + traceback.format_exc(limit=3))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log("NOC service started (every %d s, %d s per router)" % (INTERVAL, PROBE_TIMEOUT))
    while RUNNING:
        started = time.monotonic()
        try:
            one_round()
        except Exception:          # the database is down or restarting: wait for the next round
            log("round skipped:\n" + traceback.format_exc(limit=2))
        while RUNNING and time.monotonic() - started < INTERVAL:
            time.sleep(0.5)
    log("NOC service stopped")


if __name__ == "__main__":
    sys.exit(main())
