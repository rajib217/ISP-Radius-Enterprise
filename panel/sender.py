"""Delivers queued SMS and emails. Runs as the isp-panel-sender systemd service (one instance only).

Every 10 seconds it takes a few waiting messages per channel (as many as the per-minute speed limit allows),
sends them, and records the result. A message that fails three times is marked failed and can be retried
from the Messages page.
"""
import signal
import sys
import time
import traceback

import db
import sendlib

CYCLE = 10          # seconds between rounds
MAX_TRIES = 3
RUNNING = True


def stop(*_):
    global RUNNING
    RUNNING = False


def quota(rate):
    return max(int(rate) * CYCLE // 60, 1)


def mark(conn, cur, row, error):
    if error is None:
        cur.execute("UPDATE outbox SET status = 'sent', sent_at = NOW(), error = '' WHERE id = %s", (row["id"],))
    else:
        tries = row["tries"] + 1
        cur.execute("UPDATE outbox SET tries = %s, error = %s, status = %s WHERE id = %s",
                    (tries, error[:250], "failed" if tries >= MAX_TRIES else "pending", row["id"]))
    conn.commit()


def work(conn, cur, s):
    sent = 0
    for channel, ready, rate in (("sms", bool(s["sms_url"]), s["sms_rate"]),
                                 ("email", bool(s["smtp_host"]), s["email_rate"])):
        if not ready:
            continue
        cur.execute("SELECT id, to_addr, subject, body, tries FROM outbox WHERE channel = %s AND status = 'pending' "
                    "ORDER BY id LIMIT %s", (channel, quota(rate)))
        rows = cur.fetchall()
        if not rows:
            continue
        mailer = sendlib.Mailer(s) if channel == "email" else None
        try:
            for row in rows:
                if not RUNNING:
                    break
                try:
                    if channel == "sms":
                        sendlib.send_sms(s, row["to_addr"], row["body"])
                    else:
                        mailer.send(row["to_addr"], row["subject"], row["body"])
                    mark(conn, cur, row, None)
                    sent += 1
                except sendlib.SendError as exc:
                    mark(conn, cur, row, str(exc))
        finally:
            if mailer:
                mailer.close()
    return sent


PUSH_BATCH = 50


def push_round(conn, cur, s, send=None):
    """Send Web Push for new in-app notifications (made in the last day) to every browser the customer allowed.
    A subscription the push service reports gone (404 / 410) is deleted; one that keeps failing is dropped after 20."""
    import webpush
    if s.get("portal_on") != "1" or not webpush.available():
        return 0
    send = send or webpush.send
    cur.execute("SELECT n.id, n.subscriber_id, n.title, n.body, n.link FROM sub_notifications n WHERE n.pushed_at IS NULL "
                "AND n.created_at > NOW() - INTERVAL 1 DAY ORDER BY n.id LIMIT %s", (PUSH_BATCH,))
    rows = cur.fetchall()
    if not rows:
        return 0
    priv, pub = webpush.keys(cur)
    conn.commit()
    subject = webpush.subject_of(s)
    sent = 0
    for n in rows:
        cur.execute("UPDATE sub_notifications SET pushed_at = NOW() WHERE id = %s AND pushed_at IS NULL", (n["id"],))
        conn.commit()
        cur.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE subscriber_id = %s", (n["subscriber_id"],))
        subs = cur.fetchall()
        if not subs:
            continue
        cur.execute("SELECT COUNT(*) AS c FROM sub_notifications WHERE subscriber_id = %s AND read_at IS NULL", (n["subscriber_id"],))
        unread = int(cur.fetchone()["c"])
        data = {"title": n["title"], "body": n["body"], "url": n["link"] or "/portal/notifications",
                "tag": "n%d" % n["id"], "unread": unread}
        for sub in subs:
            if not RUNNING:
                break
            status, _detail = send(sub, data, priv, pub, subject)
            if status in (200, 201, 202):
                cur.execute("UPDATE push_subscriptions SET last_ok = NOW(), fails = 0 WHERE id = %s", (sub["id"],))
                sent += 1
            elif status in (404, 410):
                cur.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub["id"],))
            else:
                cur.execute("UPDATE push_subscriptions SET fails = fails + 1 WHERE id = %s", (sub["id"],))
                cur.execute("DELETE FROM push_subscriptions WHERE id = %s AND fails >= 20", (sub["id"],))
            conn.commit()
    return sent


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    conn = None
    while RUNNING:
        try:
            if conn is None:
                conn = db.connect()
            cur = conn.cursor()
            s = db.load_settings(cur)
            conn.commit()
            work(conn, cur, s)
            try:
                push_round(conn, cur, s)
            except Exception:      # the push tables appear after update.sh; never stop SMS / email for it
                conn.rollback()
                traceback.print_exc()
        except Exception:  # database restarted, bad settings ... keep the service alive
            traceback.print_exc()
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            conn = None
        for _ in range(CYCLE):
            if not RUNNING:
                break
            time.sleep(1)
    if conn:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
