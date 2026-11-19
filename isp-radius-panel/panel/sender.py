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
