"""Web Push for staff (the panel's installed app): "New job #12" on a technician's phone when a ticket is given to them.

Same machinery as the customer app (webpush.py, the one service worker /sw.js): the panel queues a row in
staff_notifications, the isp-panel-sender service pushes it to every phone the person switched on (sender.staff_push_round).
No Flask here: the web panel and the sender both use it."""


def queue(cur, admin_id, title, body="", link="", key=None, urgent=False):
    """Queue one notification for a staff member. Returns 1 when queued (0: duplicate key or table missing)."""
    if not admin_id:
        return 0
    try:
        cur.execute("INSERT IGNORE INTO staff_notifications (admin_id, title, body, link, dedupe, urgent) VALUES (%s, %s, %s, %s, %s, %s)",
                    (admin_id, (title or "")[:120], (body or "")[:600], (link or "")[:255],
                     str(key)[:120] if key is not None else None, 1 if urgent else 0))
        return cur.rowcount
    except Exception:              # the table appears after update.sh: never break an assignment for it
        return 0


def subscribed(cur, admin_id):
    try:
        cur.execute("SELECT COUNT(*) AS c FROM staff_push_subs WHERE admin_id = %s", (admin_id,))
        return int(cur.fetchone()["c"])
    except Exception:
        return 0
