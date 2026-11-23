"""Ticket helpers with no Flask dependency (the web panel, the customer portal and the 5-minute maintenance job).

  * time limit (SLA): hours per priority (Settings on Tickets > Settings), or a shorter one per category
  * area technicians: a new office ticket goes to the technician of the customer's area with the fewest open jobs
  * customer messages: SMS (templates ticket_open / ticket_assigned / ticket_done / ticket_otp / outage, editable under
    SMS > Templates) and an app notification on the portal
  * sla_run: the maintenance job's Telegram alert for a new ticket nobody took and for one past its time limit

Everything takes an open cursor, so callers decide when to commit.
"""
import hashlib
import math
from datetime import datetime, timedelta

import msglib

PRIORITY_KEYS = ("urgent", "high", "normal", "low")
ALERT_UNASSIGNED = 1          # tickets.alerted bits: each alert is sent once
ALERT_OVERDUE = 2
ALERT_RATING = 4

SMS_TEMPLATES = {
    "ticket_open": "Dear {name}, we got your complaint #{ticket}. We will fix it as soon as possible. - {isp}",
    "ticket_assigned": "Dear {name}, technician {tech} {techphone} will handle your complaint #{ticket}. - {isp}",
    "ticket_done": "Dear {name}, your complaint #{ticket} is solved. Please rate our service: {link} - {isp}",
    "ticket_otp": "Your code for complaint #{ticket} is {code}. Give it to the technician only when the work is done. - {isp}",
    "outage": "Dear {name}, {message} - {isp}",
}
APP_TEXT = {        # (title, body) for the customer's app; {ticket}, {tech} filled in
    "ticket_open": ("অভিযোগ পাওয়া গেছে", "আপনার অভিযোগ #{ticket} আমরা পেয়েছি। দ্রুত সমাধান করা হবে।"),
    "ticket_assigned": ("টেকনিশিয়ান ঠিক হয়েছে", "অভিযোগ #{ticket} দেখবেন {tech}।"),
    "ticket_reply": ("অফিসের উত্তর", "অভিযোগ #{ticket}: {text}"),
    "ticket_done": ("সমস্যার সমাধান হয়েছে", "অভিযোগ #{ticket} সমাধান হয়েছে। আমাদের সেবা কেমন লাগলো, রেটিং দিন।"),
}


def num(s, key, default=0.0):
    try:
        return float(s.get(key) or default)
    except (TypeError, ValueError):
        return float(default)


def sla_hours(s, category, priority):
    """The time limit in hours for a ticket, or None when time limits are off."""
    if s.get("sla_on") != "1":
        return None
    hours = num(s, "sla_" + (priority if priority in PRIORITY_KEYS else "normal"), 24)
    cat = num(s, "sla_cat_" + (category or "other"), 0)
    if cat > 0:
        hours = min(hours, cat) if hours > 0 else cat
    return hours if hours > 0 else None


def due_for(s, category, priority, start=None):
    h = sla_hours(s, category, priority)
    return (start or datetime.now()) + timedelta(hours=h) if h else None


def pick_tech(cur, area):
    """The active technician of this area with the fewest open jobs (ties: the one who got a job longest ago)."""
    if not area:
        return None
    cur.execute("SELECT a.id, (SELECT COUNT(*) FROM tickets t WHERE t.assigned_to = a.id AND t.status IN ('open', 'pending')) AS n, "
                "(SELECT MAX(t.created_at) FROM tickets t WHERE t.assigned_to = a.id) AS last "
                "FROM ticket_area_techs m JOIN panel_admins a ON a.id = m.admin_id "
                "WHERE m.area = %s AND a.is_active = 1 ORDER BY n, last IS NOT NULL, last, a.id LIMIT 1", (area,))
    row = cur.fetchone()
    return row["id"] if row else None


def _sub(cur, sid):
    cur.execute("SELECT s.id, s.username, s.full_name, s.phone, s.email, s.expiry_date, s.sms_on, s.area, "
                "p.label AS plan_label, p.price FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (sid,))
    return cur.fetchone()


def sms_to(cur, s, sub, name, ctx, key, force=False):
    """Queue one SMS from template `name` to the customer. force: send even when ticket SMS are off (the code)."""
    if not sub or (not force and s.get("ticket_sms_on") != "1") or not sub.get("sms_on", 1):
        return 0
    addr = msglib.normalize_phone(sub.get("phone"), s.get("sms_country_code", "880"), s.get("sms_number_format", "intl"))
    if not addr:
        return 0
    tpl = msglib.get_template(cur, "sms", name)
    body = " ".join(msglib.render(tpl["body"], msglib.sub_context(sub, s.get("isp_name", ""), ctx)).split())
    return msglib.enqueue(cur, "sms", addr, body, subscriber_id=sub["id"], dedupe=key)


def notify(cur, s, t, event, by="", link="", extra=None):
    """Tell the ticket's customer: SMS (when that template exists for the event) and an app notification."""
    if not t.get("subscriber_id"):
        return 0
    sub = _sub(cur, t["subscriber_id"])
    if not sub:
        return 0
    ctx = {"ticket": t["id"], "link": link}
    ctx.update(extra or {})
    n = 0
    if event in SMS_TEMPLATES:
        n += sms_to(cur, s, sub, event, ctx, "%s:%s:%s" % (event, t["id"], ctx.get("tech", "")))
    if s.get("ticket_app_on") == "1" and event in APP_TEXT:
        import appnotify
        title, body = APP_TEXT[event]
        fill = {"ticket": t["id"], "tech": ctx.get("tech", ""), "text": (ctx.get("text") or "")[:300]}
        n += appnotify.add(cur, sub["id"], "ticket", title, body.format(**fill), "/portal/support/%d" % t["id"],
                           key=None if event == "ticket_reply" else "%s:%s:%s" % (event, t["id"], ctx.get("tech", "")), by=by)
    return n


def otp_hash(secret, tid, code):
    return hashlib.sha256(("%s|%s|%s" % (secret, tid, code)).encode()).hexdigest()


def distance_m(lat1, lng1, lat2, lng2):
    """Metres between two positions (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = p2 - p1, math.radians(float(lng2) - float(lng1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def active_outages(cur, area=None):
    """Outages going on now: all of them, or those for this area plus the ones for every area."""
    try:
        if area is None:
            cur.execute("SELECT * FROM outages WHERE active = 1 ORDER BY id DESC")
        else:
            cur.execute("SELECT * FROM outages WHERE active = 1 AND (area = '' OR area = %s) ORDER BY id DESC", (area or "",))
        return cur.fetchall()
    except Exception:             # table missing until update.sh has applied the schema
        return []


def sla_run(conn, cur, s):
    """Telegram alerts (once each): an office ticket nobody took for sla_unassigned_min minutes, and an open ticket past
    its time limit. Returns how many tickets were alerted."""
    if s.get("tg_on") != "1" or not (s.get("tg_token") and s.get("tg_chat")):
        return 0
    import alertlib
    lines, marks = [], []
    mins = int(num(s, "sla_unassigned_min", 60))
    if mins > 0:
        cur.execute("SELECT id, subject, username, priority, created_at FROM tickets WHERE status = 'open' AND to_office = 1 "
                    "AND assigned_to IS NULL AND (alerted & %s) = 0 AND created_at <= NOW() - INTERVAL %s MINUTE "
                    "ORDER BY id LIMIT 30", (ALERT_UNASSIGNED, mins))
        for t in cur.fetchall():
            lines.append("• #%d %s%s (%s) - nobody took it for %d min" % (
                t["id"], t["subject"][:60], (" / " + t["username"]) if t["username"] else "", t["priority"], mins))
            marks.append((ALERT_UNASSIGNED, t["id"]))
    if s.get("sla_on") == "1":
        cur.execute("SELECT t.id, t.subject, t.username, t.priority, t.due_at, a.username AS who FROM tickets t "
                    "LEFT JOIN panel_admins a ON a.id = t.assigned_to WHERE t.status IN ('open', 'pending') AND t.to_office = 1 "
                    "AND t.due_at IS NOT NULL AND t.due_at < NOW() AND (t.alerted & %s) = 0 ORDER BY t.due_at LIMIT 30",
                    (ALERT_OVERDUE,))
        for t in cur.fetchall():
            lines.append("• #%d %s%s (%s) - past its time limit, %s" % (
                t["id"], t["subject"][:60], (" / " + t["username"]) if t["username"] else "", t["priority"],
                ("with " + t["who"]) if t["who"] else "nobody on it"))
            marks.append((ALERT_OVERDUE, t["id"]))
    if not lines:
        return 0
    ok, _err = alertlib.send(s, "🎫 %s tickets need attention\n%s" % (s.get("isp_name", ""), "\n".join(lines)))
    if ok:
        cur.executemany("UPDATE tickets SET alerted = alerted | %s WHERE id = %s", marks)
        conn.commit()
    return len(set(m[1] for m in marks)) if ok else 0


def rating_alert(cur, s, t, stars, note):
    """A low rating from the customer: tell the manager on Telegram (once per ticket)."""
    if stars > int(num(s, "ticket_low_rating", 2)) or s.get("tg_on") != "1":
        return False
    import alertlib
    ok, _ = alertlib.send(s, "⭐ Low rating %d/5 on ticket #%d %s (%s)%s" % (
        stars, t["id"], t["subject"][:60], t.get("username") or "", (": " + note[:200]) if note else ""))
    if ok:
        cur.execute("UPDATE tickets SET alerted = alerted | %s WHERE id = %s", (ALERT_RATING, t["id"]))
    return ok


# ===================================================================== monthly clean-up of closed tickets
_STAT_COLS = ("n", "fix_sum", "fix_n", "reply_sum", "reply_n", "met", "timed", "rating_sum", "rating_n", "portal", "visits")
_STAT_SQL = ("COUNT(*) AS n, "
             "COALESCE(SUM(TIMESTAMPDIFF(MINUTE, t.created_at, COALESCE(t.closed_at, t.updated_at))), 0) AS fix_sum, COUNT(*) AS fix_n, "
             "COALESCE(SUM(CASE WHEN t.first_reply_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, t.created_at, t.first_reply_at) END), 0) AS reply_sum, "
             "SUM(t.first_reply_at IS NOT NULL) AS reply_n, "
             "SUM(t.due_at IS NOT NULL AND COALESCE(t.closed_at, t.updated_at) <= t.due_at) AS met, SUM(t.due_at IS NOT NULL) AS timed, "
             "COALESCE(SUM(t.rating), 0) AS rating_sum, COUNT(t.rating) AS rating_n, SUM(t.source = 'portal') AS portal, "
             "SUM(t.arrived_at IS NOT NULL) AS visits")


def first_of_month(day):
    return day.replace(day=1)


def purge_closed(conn, cur, s, data_dir, today=None):
    """On the 1st of each month (or the first run after it): add the numbers of every ticket closed before this month to
    ticket_month_stats, then delete those tickets with their messages, materials and photos. Open tickets stay.
    Runs once a month (settings key ticket_purge_month). Returns how many tickets were removed."""
    import os
    from datetime import date
    if s.get("ticket_purge_on") != "1":
        return 0
    today = today or date.today()
    month = today.strftime("%Y-%m")
    if s.get("ticket_purge_month") == month:
        return 0
    cutoff = first_of_month(today)
    old = ("FROM tickets t WHERE t.status IN ('resolved', 'closed') AND COALESCE(t.closed_at, t.updated_at) < %s")
    cur.execute("SELECT COUNT(*) AS c " + old, (cutoff,))
    total = int(cur.fetchone()["c"])
    if total:
        mon = "DATE_FORMAT(COALESCE(t.closed_at, t.updated_at), '%%Y-%%m')"
        groups = (("all", "''", "''", ""),
                  ("tech", "CAST(t.assigned_to AS CHAR)", "COALESCE(NULLIF(a.full_name, ''), a.username)",
                   " JOIN panel_admins a ON a.id = t.assigned_to"),
                  ("area", "COALESCE(NULLIF(s.area, ''), '(no area)')", "COALESCE(NULLIF(s.area, ''), '(no area)')",
                   " JOIN subscribers s ON s.id = t.subscriber_id"),
                  ("cat", "t.category", "t.category", ""))
        rows = []
        for kind, ref, name, join in groups:
            cur.execute("SELECT " + mon + " AS month, " + ref + " AS ref, MAX(" + name + ") AS name, " + _STAT_SQL +
                        " FROM tickets t" + join + " WHERE t.status IN ('resolved', 'closed') AND COALESCE(t.closed_at, t.updated_at) < %s"
                        " GROUP BY month, ref", (cutoff,))
            for r in cur.fetchall():
                rows.append((r["month"], kind, str(r["ref"] or "")[:64], str(r["name"] or "")[:128]) +
                            tuple(int(r[c] or 0) for c in _STAT_COLS))
        if rows:
            cur.executemany("INSERT INTO ticket_month_stats (month, kind, ref, name, " + ", ".join(_STAT_COLS) + ") VALUES (" +
                            ", ".join(["%s"] * (4 + len(_STAT_COLS))) + ") ON DUPLICATE KEY UPDATE name = VALUES(name), " +
                            ", ".join("%s = %s + VALUES(%s)" % (c, c, c) for c in _STAT_COLS), rows)
        # the photos of the work go with the tickets
        cur.execute("SELECT r.photo FROM ticket_replies r JOIN tickets t ON t.id = r.ticket_id WHERE r.photo <> '' AND "
                    "t.status IN ('resolved', 'closed') AND COALESCE(t.closed_at, t.updated_at) < %s", (cutoff,))
        photos = [r["photo"] for r in cur.fetchall()]
        for sub in ("ticket_replies r", "ticket_materials r"):
            cur.execute("DELETE r FROM " + sub + " JOIN tickets t ON t.id = r.ticket_id WHERE t.status IN ('resolved', 'closed') "
                        "AND COALESCE(t.closed_at, t.updated_at) < %s", (cutoff,))
        cur.execute("DELETE t " + old, (cutoff,))
        for name in photos:
            path = os.path.join(data_dir, "ticket_photos", os.path.basename(name))
            try:
                os.remove(path)
            except OSError:
                pass
    cur.execute("INSERT INTO settings (skey, svalue) VALUES ('ticket_purge_month', %s) ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)",
                (month,))
    conn.commit()
    return total


def month_stats(cur, limit=12):
    """Saved monthly numbers, newest month first: [{'month', 'all': row, 'tech': [rows], 'area': [rows], 'cat': [rows]}]."""
    try:
        cur.execute("SELECT * FROM ticket_month_stats WHERE month IN (SELECT month FROM (SELECT DISTINCT month FROM ticket_month_stats "
                    "ORDER BY month DESC LIMIT %s) m) ORDER BY month DESC, kind, n DESC", (limit,))
        rows = cur.fetchall()
    except Exception:
        return []
    out, by = [], {}
    for r in rows:
        for a, b, key in (("fix_sum", "fix_n", "fix_min"), ("reply_sum", "reply_n", "reply_min"), ("rating_sum", "rating_n", "rating")):
            r[key] = (r[a] / r[b]) if r[b] else None
        r["in_time"] = (r["met"] * 100.0 / r["timed"]) if r["timed"] else None
        m = by.get(r["month"])
        if not m:
            m = by[r["month"]] = {"month": r["month"], "all": None, "tech": [], "area": [], "cat": []}
            out.append(m)
        if r["kind"] == "all":
            m["all"] = r
        elif r["kind"] in m:
            m[r["kind"]].append(r)
    return out
