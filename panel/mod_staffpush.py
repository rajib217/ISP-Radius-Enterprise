"""Staff app notifications: switch them on for this phone and test them (Web Push, see staffpush.py / sender.py).

  /me/push/key           the server's public VAPID key for this browser
  /me/push/subscribe     keep this phone's push subscription (POST JSON, X-CSRF-Token header)
  /me/push/unsubscribe   forget it
  /me/push/test          queue a test notification to my own phones"""
import hashlib

from flask import g, jsonify, request

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
