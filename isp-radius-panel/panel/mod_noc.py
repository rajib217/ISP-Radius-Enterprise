"""Logs & System Health: live RADIUS logins (with the reason a login was refused), staff actions and router health.

The page polls /api/noc every 3 seconds and only asks for rows newer than the ones it already shows, so each
poll is a couple of indexed queries. Router figures come from the isp-panel-noc service (noc.py, every 30 s).
"""
import re
from datetime import date, datetime

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

import core
from core import audit, commit, q, route

AUTH_ROWS = 100
AUDIT_ROWS = 60


def _staff_only():
    if core.my_downline() is not None:     # sellers would see other sellers' customers here
        abort(403)


def _norm_mac(text):
    return re.sub(r"[^0-9A-F]", "", (text or "").upper())


def explain(row, sub):
    """Plain reason for one post-auth row. Uses FreeRADIUS's own message when there is one, and the customer's
    record to tell the common cases apart (MAC lock, expiry, suspension) when the message is vague."""
    if row["reply"] == "Access-Accept":
        return "OK", "ok"
    raw = (row.get("reason") or "").strip()
    low = raw.lower()
    if not sub:
        return "Unknown user (not in the panel)", "bad"
    when = row["authdate"].date() if isinstance(row.get("authdate"), datetime) else date.today()
    exp = sub.get("expiry_date")
    grace = sub.get("grace_until")
    if grace and exp and grace > exp:
        exp = grace
    bound, seen = _norm_mac(sub.get("mac_bind")), _norm_mac(row.get("mac"))
    if "expired" in low or "expiration" in low:
        return "Expired", "warn"
    if bound and seen and bound != seen:
        return "MAC mismatch (locked to %s)" % sub["mac_bind"], "bad"
    if "multiple logins" in low or "simultaneous" in low:
        return "Already online (device limit)", "warn"
    if sub.get("status") == "suspended" or sub.get("net_disabled"):
        return "Suspended / internet disabled", "bad"
    if any(k in low for k in ("password", "chap", "does not match", "incorrect", "login incorrect")):
        return "Wrong password", "bad"
    if exp and exp < when:
        return "Expired", "warn"
    if "no \"known good\" password" in low or "not found" in low:
        return "No password set for this user", "bad"
    return (raw[:120] or "Refused"), "bad"


def _auth_rows(after):
    try:
        rows = q("SELECT id, username, reply, authdate, reason, nas_ip, mac FROM radpostauth WHERE id > %s "
                 "ORDER BY id DESC LIMIT %s", (after, AUTH_ROWS))
    except Exception:          # columns arrive with install.sh; until then show what radpostauth has
        rows = q("SELECT id, username, reply, authdate FROM radpostauth WHERE id > %s ORDER BY id DESC LIMIT %s",
                 (after, AUTH_ROWS))
    if not rows:
        return []
    names = list({r["username"] for r in rows if r["username"]})
    subs = {}
    if names:
        subs = {s["username"]: s for s in q(
            "SELECT username, status, expiry_date, grace_until, mac_bind, net_disabled FROM subscribers "
            "WHERE username IN (%s)" % ",".join(["%s"] * len(names)), names)}
    nas = {n["nasname"]: n["shortname"] for n in q("SELECT nasname, shortname FROM nas")}
    out = []
    for r in rows:
        why, tone = explain(r, subs.get(r["username"]))
        out.append({"id": r["id"], "user": r["username"] or "", "ok": r["reply"] == "Access-Accept",
                    "time": r["authdate"].strftime("%H:%M:%S") if r.get("authdate") else "",
                    "date": r["authdate"].strftime("%d %b %Y") if r.get("authdate") else "",
                    "reason": why, "tone": tone, "nas": nas.get(r.get("nas_ip") or "", r.get("nas_ip") or ""),
                    "mac": r.get("mac") or ""})
    return out


def _audit_rows(after):
    rows = q("SELECT id, admin, action, target, detail, ip, created_at FROM audit_log WHERE id > %s "
             "ORDER BY id DESC LIMIT %s", (after, AUDIT_ROWS))
    return [{"id": r["id"], "who": r["admin"], "action": r["action"].replace("_", " "), "target": r["target"],
             "detail": r["detail"][:160], "ip": r["ip"], "time": r["created_at"].strftime("%H:%M:%S"),
             "date": r["created_at"].strftime("%d %b")} for r in rows]


def _health():
    try:
        rows = q("SELECT n.id, n.shortname, n.nasname, n.snmp_community, h.checked_at, h.reachable, h.rtt_ms, h.snmp_ok, "
                 "h.cpu_pct, h.ram_pct, h.uptime_s, h.board, h.pppoe, h.error, h.fails FROM nas n "
                 "LEFT JOIN nas_health h ON h.nas_id = n.id ORDER BY n.shortname LIMIT 300")
    except Exception:
        return []
    out = []
    for r in rows:
        state = "unknown" if not r["checked_at"] else ("up" if r["reachable"] else ("down" if (r["fails"] or 0) >= 3 else "flaky"))
        out.append({"id": r["id"], "name": r["shortname"] or r["nasname"], "ip": r["nasname"], "state": state,
                    "rtt": float(r["rtt_ms"]) if r["rtt_ms"] is not None else None, "snmp": bool(r["snmp_ok"]),
                    "cpu": float(r["cpu_pct"]) if r["cpu_pct"] is not None else None,
                    "ram": float(r["ram_pct"]) if r["ram_pct"] is not None else None,
                    "uptime": int(r["uptime_s"]) if r["uptime_s"] is not None else None, "board": r["board"] or "",
                    "pppoe": int(r["pppoe"] or 0), "error": r["error"] or "",
                    "checked": r["checked_at"].strftime("%H:%M:%S") if r["checked_at"] else ""})
    return out


def _service():
    row = q("SELECT svalue FROM settings WHERE skey = 'noc_heartbeat'", one=True)
    try:
        age = int((datetime.now() - datetime.strptime(row["svalue"], "%Y-%m-%d %H:%M:%S")).total_seconds()) if row else None
    except (ValueError, TypeError):
        age = None
    return {"age": age, "ok": age is not None and age < 120}


def _summary():
    rows = q("SELECT reply, COUNT(*) AS c FROM radpostauth WHERE authdate >= NOW() - INTERVAL 1 HOUR GROUP BY reply")
    by = {r["reply"]: int(r["c"]) for r in rows}
    online = q("SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL", one=True)["c"]
    return {"accept": by.get("Access-Accept", 0), "reject": by.get("Access-Reject", 0), "online": int(online or 0)}


@route("/noc", module="logs")
def noc():
    _staff_only()
    s = core.cfg()
    return render_template("noc.html", s=s, cpu_limit=s.get("noc_cpu_alert") or "85")


@route("/api/noc", module="logs")
def api_noc():
    _staff_only()
    def num(key):
        v = request.args.get(key, "0")
        return int(v) if v.isdigit() else 0
    return jsonify(auth=_auth_rows(num("auth")), audit=_audit_rows(num("audit")), health=_health(),
                   service=_service(), summary=_summary(), time=datetime.now().strftime("%H:%M:%S"))


@route("/noc/settings", module="logs", methods=["POST"])
@core.admin_required
def noc_settings():
    f = request.form
    community = f.get("noc_snmp_community", "").strip()
    if community and not re.match(r"^[\x21-\x7e]{1,64}$", community):
        flash("The SNMP community may only contain printable characters without spaces (max 64).", "error")
        return redirect(url_for("noc"))
    try:
        cpu = max(10, min(100, int(f.get("noc_cpu_alert", "85"))))
    except ValueError:
        cpu = 85
    with core.get_db().cursor() as cur:
        for k, v in (("noc_snmp_community", community), ("noc_cpu_alert", str(cpu)),
                     ("al_noc", "1" if f.get("al_noc") else "0"), ("auto_kick_expired", "1" if f.get("auto_kick_expired") else "0")):
            core.db.save_setting(cur, k, v)
    audit("noc_settings", "", "cpu %d%%, auto disconnect %s" % (cpu, "on" if f.get("auto_kick_expired") else "off"))
    commit()
    flash("Saved. The NOC service uses the new values from its next round (within 30 seconds).", "success")
    return redirect(url_for("noc"))
