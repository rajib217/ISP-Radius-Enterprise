"""In-app notifications for customers (the customer portal / its installed app).

Written with a plain database cursor so the web panel, the portal and the maintenance job can all use it.
A notification with a `key` is created only once per customer (the unique key stops duplicates: one per payment,
one per "expires in 3 days" for a given expiry date, and so on).

  kinds: payment, balance, expiring, expired, custom, ticket (a complaint: received, technician, reply, solved)
  read_at NULL = unread (the bell's red number and the app icon badge)"""

KINDS = ("payment", "balance", "expiring", "expired", "custom", "ticket")
KEEP_DAYS = 120


def add(cur, sid, kind, title, body, link="", key=None, by=""):
    """Create one notification. Returns 1 when created, 0 when it already existed (same key) or failed."""
    try:
        cur.execute("INSERT IGNORE INTO sub_notifications (subscriber_id, kind, title, body, link, dedupe, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (sid, kind if kind in KINDS else "custom", (title or "")[:120], (body or "")[:600], (link or "")[:255],
                     (str(key)[:120] if key is not None else None), (by or "")[:64]))
        return cur.rowcount
    except Exception:              # table missing until update.sh has applied the schema: never break a payment
        return 0


def unread(cur, sid):
    try:
        cur.execute("SELECT COUNT(*) AS c FROM sub_notifications WHERE subscriber_id = %s AND read_at IS NULL", (sid,))
        return int(cur.fetchone()["c"])
    except Exception:
        return 0


def money(amount, currency):
    return "%s %.2f" % (currency or "BDT", float(amount or 0))


def bn_date(d):
    return d.strftime("%d-%m-%Y") if d else ""


def payment(cur, sid, amount, new_expiry, currency, key, by=""):
    if float(amount or 0) > 0:
        title = "পেমেন্ট সম্পন্ন হয়েছে"
        body = "%s পেমেন্ট পাওয়া গেছে। আপনার ইন্টারনেট %s পর্যন্ত চালু থাকবে। ধন্যবাদ!" % (money(amount, currency), bn_date(new_expiry))
    else:
        title = "প্যাকেজ চালু হয়েছে"
        body = "আপনার ইন্টারনেট %s পর্যন্ত চালু থাকবে।" % bn_date(new_expiry)
    return add(cur, sid, "payment", title, body, "/portal#invoices", key="pay:%s" % key, by=by)


def balance(cur, sid, amount, new_balance, currency, key, by=""):
    return add(cur, sid, "balance", "ব্যালেন্সে টাকা যোগ হয়েছে",
               "আপনার ব্যালেন্সে %s যোগ হয়েছে। এখন ব্যালেন্স %s।" % (money(amount, currency), money(new_balance, currency)),
               "/portal", key="bal:%s" % key, by=by)


def expiring(cur, sid, days, expiry):
    when = "আজ" if days == 0 else ("আগামীকাল" if days == 1 else "%d দিন পর" % days)
    return add(cur, sid, "expiring", "মেয়াদ শেষ হতে চলেছে",
               "আপনার ইন্টারনেট প্যাকেজের মেয়াদ %s (%s) শেষ হবে। সংযোগ চালু রাখতে সময়মতো বিল পরিশোধ করুন।" % (when, bn_date(expiry)),
               "/portal/pay", key="exp:%s:%d" % (expiry, days))


def expired(cur, sid, expiry):
    return add(cur, sid, "expired", "প্যাকেজের মেয়াদ শেষ",
               "আপনার ইন্টারনেট প্যাকেজের মেয়াদ %s তারিখে শেষ হয়েছে। আবার চালু করতে বিল পরিশোধ করুন।" % bn_date(expiry),
               "/portal/pay", key="end:%s" % expiry)


def run_expiry(conn, cur, days_list=(3, 1, 0)):
    """Maintenance: "expires in N days" and "expired" notifications. Safe to run every few minutes (unique keys)."""
    made = 0
    for d in days_list:
        cur.execute("SELECT id, expiry_date FROM subscribers WHERE status = 'active' AND expiry_date = CURDATE() + INTERVAL %s DAY "
                    "AND (grace_until IS NULL OR grace_until <= expiry_date)", (d,))
        for r in cur.fetchall():
            made += expiring(cur, r["id"], d, r["expiry_date"])
    import radius
    cur.execute("SELECT s.id, s.expiry_date FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
                "WHERE s.status = 'active' AND s.expiry_date >= CURDATE() - INTERVAL 3 DAY AND s.expiry_date <= CURDATE() "
                "AND " + radius.END_MOMENT + " <= NOW()")
    for r in cur.fetchall():
        made += expired(cur, r["id"], r["expiry_date"])
    cur.execute("DELETE FROM sub_notifications WHERE created_at < NOW() - INTERVAL %s DAY", (KEEP_DAYS,))
    conn.commit()
    return made
