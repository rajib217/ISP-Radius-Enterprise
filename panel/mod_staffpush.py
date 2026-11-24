"""Staff app notifications: switch them on for this phone and test them (Web Push, see staffpush.py / sender.py).

  /me/push/key           the server's public VAPID key for this browser
  /me/push/subscribe     keep this phone's push subscription (POST JSON, X-CSRF-Token header)
  /me/push/unsubscribe   forget it
  /me/push/test          queue a test notification to my own phones"""
import hashlib

from flask import g, jsonify, request, url_for

import core
import staffpush
from core import audit, commit, get_db, route, x


@route("/me/push/key")
def staff_push_key():
    import webpush
    if not webpush.available():
        return jsonify({"key": None})
    with get_db().cursor() as cur:
        _priv, pub = webpush.keys(cur)
        n = staffpush.subscribed(cur, g.me["id"])
    commit()
    return jsonify({"key": pub, "phones": n})


@route("/me/push/subscribe", methods=["POST"])
def staff_push_subscribe():
    import webpush
    d = request.get_json(silent=True) or {}
    endpoint = (d.get("endpoint") or "").strip()
    keys = d.get("keys") or {}
    p256dh, auth = (keys.get("p256dh") or "").strip(), (keys.get("auth") or "").strip()
    if not webpush.endpoint_ok(endpoint) or not (40 <= len(p256dh) <= 255) or not (8 <= len(auth) <= 64):
        return jsonify({"ok": False, "error": "This browser's push address is not accepted."}), 400
    h = hashlib.sha1(endpoint.encode()).hexdigest()
    x("INSERT INTO staff_push_subs (admin_id, endpoint_hash, endpoint, p256dh, auth, user_agent) VALUES (%s, %s, %s, %s, %s, %s) "
      "ON DUPLICATE KEY UPDATE admin_id = VALUES(admin_id), p256dh = VALUES(p256dh), auth = VALUES(auth), "
      "user_agent = VALUES(user_agent), fails = 0",
      (g.me["id"], h, endpoint, p256dh, auth, (request.headers.get("User-Agent") or "")[:255]))
    if d.get("first"):
        audit("staff_push_enabled", g.me["username"])
    commit()
    return jsonify({"ok": True})


@route("/me/push/unsubscribe", methods=["POST"])
def staff_push_unsubscribe():
    endpoint = ((request.get_json(silent=True) or {}).get("endpoint") or "").strip()
    x("DELETE FROM staff_push_subs WHERE admin_id = %s AND endpoint_hash = %s", (g.me["id"], hashlib.sha1(endpoint.encode()).hexdigest()))
    commit()
    return jsonify({"ok": True})


@route("/me/push/test", methods=["POST"])
def staff_push_test():
    with get_db().cursor() as cur:
        n = staffpush.subscribed(cur, g.me["id"])
        if n:
            staffpush.queue(cur, g.me["id"], "Test notification", "Job alerts work on this phone. %s" % (core.cfg().get("isp_name") or ""),
                            "/")
    commit()
    return jsonify({"ok": bool(n), "phones": n})


# ===================================================================== job alerts page and technician location
@route("/me/alerts")
def staff_alerts():
    """Menu > Job alerts: switch this phone's job notifications on or off (and, for a technician, location sharing)."""
    if g.me["role"] in core.SELLER_ROLES:
        from flask import abort
        abort(403)
    from flask import render_template
    with get_db().cursor() as cur:
        phones = staffpush.subscribed(cur, g.me["id"])
    loc = core.q("SELECT updated_at FROM staff_locations WHERE admin_id = %s", (g.me["id"],), one=True)
    return render_template("staff_alerts.html", phones=phones, loc=loc)


@route("/me/location", methods=["POST"])
def staff_location():
    """A technician's phone sends where it is (while the panel app is open): the office sees it on the technician map."""
    if g.me["role"] != "technician":
        return jsonify({"ok": False}), 403
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d.get("lat")), float(d.get("lng"))
        acc = int(float(d.get("acc") or 0)) or None
    except (TypeError, ValueError):
        return jsonify({"ok": False}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return jsonify({"ok": False}), 400
    x("INSERT INTO staff_locations (admin_id, lat, lng, accuracy, updated_at) VALUES (%s, %s, %s, %s, NOW()) "
      "ON DUPLICATE KEY UPDATE lat = VALUES(lat), lng = VALUES(lng), accuracy = VALUES(accuracy), updated_at = NOW()",
      (g.me["id"], round(lat, 6), round(lng, 6), min(acc, 100000) if acc else None))
    commit()
    return jsonify({"ok": True})


LIVE_MINUTES = 10           # a position newer than this shows the technician as live


def _can_see_techs():
    return g.me["role"] in ("admin", "manager")


@route("/map/technicians")
def tech_map():
    from flask import abort, render_template
    if not _can_see_techs():
        abort(403)
    n = core.q("SELECT COUNT(*) AS c FROM panel_admins WHERE role = 'technician' AND is_active = 1", one=True)["c"]
    placed = core.q("SELECT COUNT(*) AS c FROM staff_locations l JOIN panel_admins a ON a.id = l.admin_id "
                    "WHERE a.role = 'technician' AND a.is_active = 1", one=True)["c"]
    s = core.cfg()
    centre = [float(s.get("latitude") or 23.8103), float(s.get("longitude") or 90.4125)]
    return render_template("tech_map.html", total=n, placed=placed, centre=centre, live_minutes=LIVE_MINUTES)


@route("/map/technicians/data")
def tech_map_data():
    from flask import abort
    if not _can_see_techs():
        abort(403)
    from datetime import datetime
    out = []
    for r in core.q("SELECT a.id, a.username, a.full_name, a.phone, l.lat, l.lng, l.accuracy, l.updated_at, "
                    "(SELECT COUNT(*) FROM tickets t WHERE t.assigned_to = a.id AND t.status IN ('open', 'pending')) AS jobs "
                    "FROM staff_locations l JOIN panel_admins a ON a.id = l.admin_id WHERE a.role = 'technician' AND a.is_active = 1"):
        mins = int((datetime.now() - r["updated_at"]).total_seconds() // 60)
        ago = "just now" if mins < 1 else ("%d min ago" % mins if mins < 60 else ("%d h ago" % (mins // 60) if mins < 1440 else "%d days ago" % (mins // 1440)))
        out.append({"id": r["id"], "code": "%d open job%s" % (r["jobs"], "" if r["jobs"] == 1 else "s"), "u": r["username"],
                    "n": r["full_name"] or r["username"], "ph": r["phone"] or "", "p": "Seen " + ago,
                    "a": ("±%d m" % r["accuracy"]) if r["accuracy"] else "", "s": "online" if mins < LIVE_MINUTES else "offline",
                    "lat": float(r["lat"]), "lng": float(r["lng"]), "url": url_for("admin_view", aid=r["id"])})
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp
