"""Message helpers with no Flask dependency (used by the web panel, the sender service and maintenance).

Everything that touches the database takes an open cursor, so callers decide when to commit.
"""
import re

SYSTEM_TEMPLATE_NAMES = ("welcome", "payment", "expiry", "due", "ticket_open", "ticket_assigned", "ticket_done",
                         "ticket_otp", "outage")
VARIABLES = ("name", "username", "phone", "plan", "price", "expiry", "amount", "password", "isp", "id", "paylink",
             "ticket", "tech", "techphone", "link", "code", "message")

DEFAULT_TEMPLATES = {
    ("sms", "welcome"): {"subject": "", "body":
        "Welcome to {isp}! Internet ID: {username}  Password: {password}  Package: {plan}  Valid until: {expiry}."},
    ("sms", "payment"): {"subject": "", "body":
        "Dear {name}, we received BDT {amount}. Your {plan} package is valid until {expiry}. Thank you - {isp}"},
    ("sms", "expiry"): {"subject": "", "body":
        "Dear {name}, your internet expires on {expiry}. Please pay BDT {price} to stay connected. - {isp}"},
    ("sms", "due"): {"subject": "", "body":
        "Dear {name} ({id}), your internet ended on {expiry}. Please pay BDT {amount} to reconnect. {paylink} - {isp}"},
    ("email", "due"): {"subject": "Your internet bill is due - {isp}", "body":
        "Dear {name},\n\nYour {plan} package ended on {expiry}. Please pay BDT {amount} to reconnect.\n{paylink}\n\n"
        "Thank you,\n{isp}"},
    ("email", "welcome"): {"subject": "Welcome to {isp}", "body":
        "Dear {name},\n\nYour internet connection is ready.\n\nUsername: {username}\nPassword: {password}\n"
        "Package: {plan}\nValid until: {expiry}\n\nThank you,\n{isp}"},
    ("email", "payment"): {"subject": "Payment received - {isp}", "body":
        "Dear {name},\n\nWe received your payment of BDT {amount}.\nPackage: {plan}\nValid until: {expiry}\n\n"
        "Thank you,\n{isp}"},
    ("email", "expiry"): {"subject": "Your internet expires on {expiry}", "body":
        "Dear {name},\n\nYour {plan} package expires on {expiry}.\nPlease pay BDT {price} to stay connected.\n\n"
        "Thank you,\n{isp}"},
}

def _ticket_defaults():
    import ticketlib
    for name, body in ticketlib.SMS_TEMPLATES.items():
        DEFAULT_TEMPLATES.setdefault(("sms", name), {"subject": "", "body": body})


_ticket_defaults()

_VAR = re.compile(r"\{(\w+)\}")


def render(text, ctx):
    """Replace {name}-style variables. Unknown variables become empty text."""
    return _VAR.sub(lambda m: str(ctx.get(m.group(1), "")), text or "")


def money(value):
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    return "{:,.0f}".format(v) if v == int(v) else "{:,.2f}".format(v)


def normalize_phone(raw, cc="880", fmt="intl"):
    """Return a phone number in the gateway format, or None when it does not look like a number.

    fmt: intl = 8801711000000, local = 01711000000, plus = +8801711000000
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    cc = re.sub(r"\D", "", cc or "")
    if cc and digits.startswith(cc) and len(digits) > len(cc) + 6:
        national = digits[len(cc):]
    elif digits.startswith("0"):
        national = digits[1:]
    elif cc and len(digits) <= 10:
        national = digits
    else:
        return digits if 8 <= len(digits) <= 15 else None
    if not 6 <= len(national) <= 12:
        return None
    if fmt == "local":
        return "0" + national
    if fmt == "plus":
        return "+" + cc + national
    return cc + national


def sub_context(sub, isp, extra=None):
    expiry = sub.get("expiry_date")
    ctx = {
        "name": sub.get("full_name") or sub.get("username") or "",
        "username": sub.get("username") or "",
        "phone": sub.get("phone") or "",
        "plan": sub.get("plan_label") or "",
        "price": money(sub.get("price")),
        "expiry": expiry.strftime("%d %b %Y") if expiry else "no expiry",
        "amount": "",
        "password": "",
        "isp": isp or "",
    }
    ctx.update(extra or {})
    return ctx


def get_template(cur, kind, name):
    cur.execute("SELECT subject, body FROM message_templates WHERE kind = %s AND name = %s", (kind, name))
    row = cur.fetchone()
    if row:
        return row
    return DEFAULT_TEMPLATES.get((kind, name), {"subject": "", "body": ""})


INSERT_SQL = ("INSERT IGNORE INTO outbox (channel, to_addr, subject, body, subscriber_id, dedupe, batch, created_by) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")


def enqueue(cur, channel, to_addr, body, subject="", subscriber_id=None, dedupe=None, batch=None, by=""):
    """Queue one message. Returns 1 if queued, 0 if it was a duplicate."""
    cur.execute(INSERT_SQL, (channel, to_addr[:160], (subject or "")[:200], body, subscriber_id,
                             dedupe[:100] if dedupe else None, batch, (by or "")[:64]))
    return cur.rowcount


def enqueue_many(cur, rows, chunk=500):
    """rows: tuples of (channel, to_addr, subject, body, subscriber_id, dedupe, batch, by)."""
    done = 0
    for i in range(0, len(rows), chunk):
        part = [(c, a[:160], (s or "")[:200], b, sid, (d[:100] if d else None), bt, (by or "")[:64])
                for c, a, s, b, sid, d, bt, by in rows[i:i + chunk]]
        cur.executemany(INSERT_SQL, part)
        done += cur.rowcount
    return done


def parse_days(text):
    """'3, 1,0' -> [0, 1, 3] (only 0-60 allowed)."""
    out = set()
    for part in re.split(r"[,\s;]+", text or ""):
        if part.isdigit() and 0 <= int(part) <= 60:
            out.add(int(part))
    return sorted(out)
