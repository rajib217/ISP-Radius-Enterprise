"""Telegram alerts. Used by maintenance.py (every 5 minutes) and by Settings > Alerts (test button).

Nothing here needs Flask. Alerts fire once per problem (and once more when it is fixed); the memory of what was
already announced lives in the settings table under 'alert_state'."""
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import db

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
CHAT_RE = re.compile(r"^(-?\d{4,20}|@[A-Za-z0-9_]{5,32})$")


def tg_call(token, method, params=None):
    """Call the Telegram Bot API. Returns (ok, result_or_error_text)."""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            body = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except (ValueError, OSError):
            return False, "Telegram answered HTTP %s." % e.code
        return False, str(body.get("description") or "Telegram refused the request.")
    except (urllib.error.URLError, OSError, ValueError):
        return False, "Could not reach Telegram (api.telegram.org). Check the server's internet access."
    return bool(body.get("ok")), body.get("result") if body.get("ok") else str(body.get("description") or "Unknown error.")


def send(s, text):
    """Send one message to the configured chat. Returns (ok, error text)."""
    if not (s.get("tg_token") and s.get("tg_chat")):
        return False, "Telegram is not set up."
    ok, res = tg_call(s["tg_token"], "sendMessage", {"chat_id": s["tg_chat"], "text": text[:3900],
                                                      "disable_web_page_preview": "true"})
    return ok, ("" if ok else str(res))


def _ping(ip):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=5).returncode == 0
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return False


def noc_alive(cur):
    """True when the isp-panel-noc service finished a round in the last 2 minutes."""
    cur.execute("SELECT svalue FROM settings WHERE skey = 'noc_heartbeat'")
    row = cur.fetchone()
    try:
        return bool(row) and (datetime.now() - datetime.strptime(row["svalue"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 120
    except (ValueError, TypeError):
        return False


def _num(s, key, default):
    try:
        return float(s.get(key) or default)
    except ValueError:
        return float(default)


def run(conn, cur, s):
    """Check everything that is switched on and send what is new. Returns the number of messages sent."""
    if s.get("tg_on") != "1" or not (s.get("tg_token") and s.get("tg_chat")):
        return 0
    cur.execute("SELECT svalue FROM settings WHERE skey = 'alert_state'")
    row = cur.fetchone()
    try:
        state = json.loads(row["svalue"]) if row else {}
    except (ValueError, TypeError):
        state = {}
    name = s.get("isp_name") or "RADIUS panel"
    msgs = []

    # ---- routers that stop answering ping (two checks in a row = about 10 minutes, so one lost packet is ignored)
    #      Skipped while the NOC service (noc.py) is running: it checks every 30 seconds and sends these alerts itself.
    if s.get("al_nas") == "1" and not noc_alive(cur):
        cur.execute("SELECT id, shortname, nasname FROM nas ORDER BY id LIMIT 300")
        nas = cur.fetchall()
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda r: _ping(r["nasname"]), nas))
        seen = state.setdefault("nas", {})
        for r, up in zip(nas, results):
            if up is None:
                break                                   # ping is not installed: skip this check
            k, label = str(r["id"]), "%s (%s)" % (r["shortname"] or r["nasname"], r["nasname"])
            e = seen.setdefault(k, {"fails": 0, "down": False})
            if up:
                if e["down"]:
                    msgs.append("✅ Router back up: %s" % label)
                e.update(fails=0, down=False)
            else:
                e["fails"] += 1
                if e["fails"] >= 2 and not e["down"]:
                    e["down"] = True
                    msgs.append("🔴 Router DOWN: %s does not answer ping." % label)
        for k in [k for k in seen if k not in {str(r["id"]) for r in nas}]:
            del seen[k]

    # ---- sudden drop in online customers
    if s.get("al_drop") == "1":
        cur.execute("SELECT online FROM stats_snapshots ORDER BY ts DESC LIMIT 1")
        now_row = cur.fetchone()
        cur.execute("SELECT online FROM stats_snapshots WHERE ts <= NOW() - INTERVAL 10 MINUTE ORDER BY ts DESC LIMIT 1")
        before = cur.fetchone()
        if now_row and before and int(before["online"]) >= 20:
            now_n, was = int(now_row["online"]), int(before["online"])
            pct = (was - now_n) * 100.0 / was
            drop = state.setdefault("drop", {"on": False})
            if pct >= _num(s, "al_drop_pct", 25) and not drop["on"]:
                drop["on"] = True
                msgs.append("🔴 Customers dropping: online went from %d to %d in 10 minutes (-%d%%). Check the router, uplink or power."
                            % (was, now_n, round(pct)))
            elif drop["on"] and pct < _num(s, "al_drop_pct", 25) / 2:
                drop["on"] = False
                msgs.append("✅ Online customers recovered: %d online." % now_n)

    # ---- this server
    if s.get("al_server") == "1":
        cur.execute("SELECT cpu_pct, mem_used_mb, mem_total_mb, disk_used_gb, disk_total_gb FROM server_stats ORDER BY ts DESC LIMIT 1")
        st = cur.fetchone()
        if st:
            checks = {"cpu": (float(st["cpu_pct"] or 0), _num(s, "al_cpu", 90), "CPU"),
                      "ram": (100.0 * st["mem_used_mb"] / max(st["mem_total_mb"], 1), _num(s, "al_ram", 90), "RAM"),
                      "disk": (100.0 * float(st["disk_used_gb"]) / max(float(st["disk_total_gb"]), 0.1), _num(s, "al_disk", 85), "Disk")}
            srv = state.setdefault("server", {})
            for key, (val, limit, label) in checks.items():
                e = srv.setdefault(key, {"hits": 0, "on": False})
                if val >= limit:
                    e["hits"] += 1
                    if e["hits"] >= (2 if key == "cpu" else 1) and not e["on"]:
                        e["on"] = True
                        msgs.append("🟠 Server %s is high: %d%% (limit %d%%)." % (label, round(val), round(limit)))
                else:
                    if e["on"] and val < limit - 5:
                        msgs.append("✅ Server %s is normal again: %d%%." % (label, round(val)))
                        e["on"] = False
                    if not e["on"]:
                        e["hits"] = 0

    # ---- FreeRADIUS itself
    if s.get("al_radius") == "1":
        try:
            active = subprocess.run(["systemctl", "is-active", "freeradius"], capture_output=True, timeout=5).stdout.decode().strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            active = ""
        if active:
            e = state.setdefault("radius", {"down": False})
            if active != "active" and not e["down"]:
                e["down"] = True
                msgs.append("🔴 FreeRADIUS is %s. Customers cannot log in until it is started (systemctl start freeradius)." % active)
            elif active == "active" and e["down"]:
                e["down"] = False
                msgs.append("✅ FreeRADIUS is running again.")

    # ---- morning summary, once a day
    today = date.today().isoformat()
    if s.get("al_daily") == "1" and datetime.now().hour >= int(_num(s, "al_daily_hour", 8)) and state.get("daily") != today:
        cur.execute("SELECT SUM(expiry_date = CURDATE()) AS t0, SUM(expiry_date = CURDATE() + INTERVAL 1 DAY) AS t1, "
                    "SUM(expiry_date BETWEEN CURDATE() + INTERVAL 2 DAY AND CURDATE() + INTERVAL 3 DAY) AS t3, "
                    "SUM(DATE(created_at) = CURDATE() - INTERVAL 1 DAY) AS newc, COUNT(*) AS total FROM subscribers")
        a = cur.fetchone() or {}
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS m, COUNT(*) AS n FROM payments WHERE DATE(paid_at) = CURDATE() - INTERVAL 1 DAY")
        p = cur.fetchone() or {}
        cur.execute("SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL")
        on = cur.fetchone()["c"]
        cur_name = s.get("currency", "")
        msgs.append("☀️ %s daily summary\nOnline now: %d of %d customers\nExpiring today: %d, tomorrow: %d, in 2-3 days: %d\n"
                    "Yesterday: %d new customers, %d payments, %s %.2f collected"
                    % (name, on, int(a.get("total") or 0), int(a.get("t0") or 0), int(a.get("t1") or 0), int(a.get("t3") or 0),
                       int(a.get("newc") or 0), int(p.get("n") or 0), cur_name, float(p.get("m") or 0)))
        state["daily"] = today

    sent = 0
    for m in msgs:
        ok, err = send(s, "%s\n%s" % (name, m) if not m.startswith("☀️") else m)
        sent += ok
        if not ok:
            print("telegram alert not sent: %s" % err)
    db.save_setting(cur, "alert_state", json.dumps(state))
    conn.commit()
    return sent
