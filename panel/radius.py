"""Everything that touches FreeRADIUS tables or talks to routers.

Subscriber data lives in the panel's own tables; this module mirrors what
FreeRADIUS needs into the standard radcheck / radreply / radusergroup /
radgroupreply tables so FreeRADIUS never has to know about the panel.
"""
import ipaddress
import re
import subprocess
from datetime import date, datetime, time, timedelta

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

SAFE_ID = re.compile(r"^[A-Za-z0-9_.@:/\-]{1,128}$")


def radius_date(d, at=None):
    """FreeRADIUS 'Expiration' format. The account works through the whole last day unless a time is given."""
    clock = "%s:00" % at if at and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at) else "23:59:59"
    return "%s %02d %d %s" % (MONTHS[d.month - 1], d.day, d.year, clock)


def rate_string(plan):
    """MikroTik rate limit is rx/tx from the ROUTER's view = upload/download for the client."""
    custom = (plan.get("rate_custom") or "").strip()
    if custom:
        return custom
    return "%dM/%dM" % (plan["upload_mbps"], plan["download_mbps"])


# ----------------------------------------------------------------- plans
def sync_plan(cur, plan, interim):
    cur.execute("DELETE FROM radgroupreply WHERE groupname = %s", (plan["name"],))
    rows = [] if plan.get("speed_from_policy") else [("Mikrotik-Rate-Limit", ":=", rate_string(plan))]
    if plan.get("pool_name"):
        rows.append(("Framed-Pool", ":=", plan["pool_name"]))
    if int(interim) > 0:
        rows.append(("Acct-Interim-Interval", ":=", str(int(interim))))
    if plan.get("quota_session_min"):
        rows.append(("Session-Timeout", ":=", str(int(plan["quota_session_min"]) * 60)))
    cur.executemany(
        "INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (%s, %s, %s, %s)",
        [(plan["name"],) + r for r in rows],
    )
    cur.execute(
        "UPDATE radcheck rc JOIN subscribers s ON s.username = rc.username "
        "SET rc.value = %s WHERE s.plan_id = %s AND rc.attribute = 'Simultaneous-Use'",
        (str(plan["simultaneous_use"]), plan["id"]),
    )


def remove_plan(cur, name):
    cur.execute("DELETE FROM radgroupreply WHERE groupname = %s", (name,))
    cur.execute("DELETE FROM radgroupcheck WHERE groupname = %s", (name,))


# ----------------------------------------------------------- subscribers
# Expired customers who should see the unpaid-bill page instead of being refused (Settings > Expired notice)
EXPIRED_GROUP = "expired-notice"
EXPIRED_LIST = "expired"           # the MikroTik address list the routers redirect to the notice page

# the moment a customer's package ends, in SQL (needs the aliases s = subscribers and p = plans)
END_MOMENT = ("(CASE WHEN s.grace_until IS NOT NULL AND s.grace_until > s.expiry_date "
              "THEN TIMESTAMP(s.grace_until, COALESCE(NULLIF(p.fixed_expire_time, ''), '23:59:59')) "
              "ELSE TIMESTAMP(s.expiry_date, COALESCE(NULLIF(s.expiry_time, ''), NULLIF(p.fixed_expire_time, ''), '23:59:59')) END)")


def _clock_time(text):
    try:
        parts = [int(x) for x in (text or "").split(":")[:3]]
        return time(*parts) if len(parts) >= 2 else None
    except (TypeError, ValueError):
        return None


def account_end(sub, plan):
    """The moment this customer's package ends (grace period included), or None when there is no expiry date.
    Same rule as END_MOMENT and the Expiration attribute."""
    plan = plan or {}
    expiry, at = sub.get("expiry_date"), sub.get("expiry_time") or plan.get("fixed_expire_time")
    grace = sub.get("grace_until")
    if grace and expiry and grace > expiry:
        expiry, at = grace, plan.get("fixed_expire_time")
    if not expiry:
        return None
    return datetime.combine(expiry, _clock_time(at) or time(23, 59, 59))


def expired_notice_group(cur):
    """EXPIRED_GROUP when Settings > Expired notice is on, else ""."""
    cur.execute("SELECT svalue FROM settings WHERE skey = 'expired_notice_on'")
    row = cur.fetchone()
    return EXPIRED_GROUP if row and row["svalue"] == "1" else ""


def notice_hours(cur):
    """Settings > Expired notice > "How long": hours after the end moment an expired customer keeps the slow speed and
    the notice page (time to pay by bKash). After that they are refused like any expired account. 0 = no limit."""
    cur.execute("SELECT svalue FROM settings WHERE skey = 'expired_notice_hours'")
    row = cur.fetchone()
    try:
        return max(0, int((row or {}).get("svalue") or 0))
    except ValueError:
        return 0


def ensure_expired_group(cur, rate=""):
    """(Re)write the RADIUS reply of the notice group: put the customer in the router's "expired" address list, at a
    small speed so the notice page and bKash still open."""
    cur.execute("DELETE FROM radgroupreply WHERE groupname = %s", (EXPIRED_GROUP,))
    rows = [("Mikrotik-Address-List", ":=", EXPIRED_LIST)]
    if rate:
        rows.append(("Mikrotik-Rate-Limit", ":=", rate))
    cur.executemany("INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES (%s, %s, %s, %s)",
                    [(EXPIRED_GROUP,) + r for r in rows])


def resync_username(cur, username):
    """Rewrite one customer's RADIUS rows from the database (used just before an expired session is dropped, so the
    reconnect already lands in the expired policy / notice group)."""
    cur.execute("SELECT * FROM subscribers WHERE username = %s", (username,))
    s = cur.fetchone()
    if not s:
        return
    plan = None
    if s.get("plan_id"):
        cur.execute("SELECT * FROM plans WHERE id = %s", (s["plan_id"],))
        plan = cur.fetchone()
    sync_subscriber(cur, s["username"], plan, s["status"], s["expiry_date"], s["static_ip"])


def sync_subscriber(cur, username, plan, status, expiry, static_ip, password=None):
    """Rewrite the RADIUS rows for one subscriber.

    password=None keeps the existing Cleartext-Password row untouched.
    """
    cur.execute(
        "DELETE FROM radcheck WHERE username = %s "
        "AND attribute IN ('Expiration', 'Simultaneous-Use', 'Auth-Type', 'Calling-Station-Id')",
        (username,),
    )
    # grace period and MAC binding live on the subscriber row; the row may not exist yet while it is being created
    cur.execute("SELECT grace_until, mac_bind, quota_hit, net_disabled, expiry_time FROM subscribers WHERE username = %s",
                (username,))
    extra = cur.fetchone() or {}
    grace, mac, hit = extra.get("grace_until"), (extra.get("mac_bind") or ""), (extra.get("quota_hit") or "")
    # the moment the account ends: the subscriber's own time, else the package's fixed time, else the end of the day
    at = extra.get("expiry_time") or (plan or {}).get("fixed_expire_time")
    if grace and expiry and grace > expiry:
        expiry, at = grace, (plan or {}).get("fixed_expire_time")   # a grace period runs to the end of its day
    if password is not None:
        cur.execute(
            "DELETE FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password'",
            (username,),
        )
        cur.execute(
            "INSERT INTO radcheck (username, attribute, op, value) "
            "VALUES (%s, 'Cleartext-Password', ':=', %s)",
            (username, password),
        )
    plan = plan or {}
    # ended = the package's own end moment has passed (not only the date: an account that ends at 14:00 is expired at
    # 14:00, so its next login already lands in the expired policy / notice page)
    ends = datetime.combine(expiry, _clock_time(at) or time(23, 59, 59)) if expiry else None
    expired = bool(ends and ends <= datetime.now())
    expired_policy = plan.get("expired_policy") or ""
    if expired and not expired_policy:
        expired_policy = expired_notice_group(cur)
        hours = notice_hours(cur) if expired_policy else 0
        if hours and ends + timedelta(hours=hours) <= datetime.now():
            expired_policy = ""          # the time to pay from the notice page is over: refused, call the office
    restricted, reject = "", False       # a policy that replaces the plan's group, or an outright refusal
    if status == "suspended":
        restricted = plan.get("disabled_policy") or ""
        reject = not restricted
    elif hit in ("total", "daily", "weekly"):
        restricted = plan.get("quota_%s_policy" % hit) if plan.get("quota_%s_action" % hit) == "policy" else ""
        reject = not restricted
    elif expired and expired_policy:
        restricted = expired_policy
    if extra.get("net_disabled"):
        restricted, reject = "", True      # "Disable Net": login refused, profile stays active
    rows = [("Simultaneous-Use", ":=", str(plan.get("simultaneous_use") or 1))]
    if expiry and not (expired and restricted and status != "suspended" and not hit):
        rows.append(("Expiration", ":=", radius_date(expiry, at)))
    if reject:
        rows.append(("Auth-Type", ":=", "Reject"))
    if mac:
        rows.append(("Calling-Station-Id", "==", mac))
    cur.executemany(
        "INSERT INTO radcheck (username, attribute, op, value) VALUES (%s, %s, %s, %s)",
        [(username,) + r for r in rows],
    )

    cur.execute("DELETE FROM radusergroup WHERE username = %s", (username,))
    groups = [restricted] if restricted else [g for g in (plan.get("name"), plan.get("policy_group")) if g]
    for prio, grp in enumerate(groups, 1):
        cur.execute("INSERT INTO radusergroup (username, groupname, priority) VALUES (%s, %s, %s)",
                    (username, grp, prio))

    cur.execute(
        "DELETE FROM radreply WHERE username = %s AND attribute = 'Framed-IP-Address'",
        (username,),
    )
    if static_ip:
        cur.execute(
            "INSERT INTO radreply (username, attribute, op, value) "
            "VALUES (%s, 'Framed-IP-Address', ':=', %s)",
            (username, static_ip),
        )


def remove_subscriber(cur, username):
    for table in ("radcheck", "radreply", "radusergroup"):
        cur.execute("DELETE FROM " + table + " WHERE username = %s", (username,))


# ------------------------------------------------------- router commands
def _run(cmd, stdin, timeout):
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return "radclient is not installed (apt install freeradius-utils)"
    except subprocess.TimeoutExpired:
        return "timeout"


def _quote_ok(value):
    return not any(ch in value for ch in ('"', "\\", "\n", "\r"))


def disconnect_session(nas_ip, secret, port, username, session_id, framed_ip=None):
    """Send a RADIUS Disconnect-Request (RFC 5176) to the router. Returns (ok, message)."""
    try:
        ipaddress.ip_address(nas_ip)
    except ValueError:
        return False, "NAS address %r is not an IP address; cannot send a disconnect." % nas_ip
    if not SAFE_ID.match(session_id or "") or not _quote_ok(username):
        return False, "Session data contains unsupported characters."
    pkt = 'User-Name = "%s"\nAcct-Session-Id = "%s"\n' % (username, session_id)
    if framed_ip:
        try:
            ipaddress.IPv4Address(framed_ip)
            pkt += "Framed-IP-Address = %s\n" % framed_ip
        except ValueError:
            pass
    out = _run(["radclient", "-x", "-t", "2", "-r", "1", "%s:%d" % (nas_ip, int(port)),
                "disconnect", secret], pkt, 10)
    if "Disconnect-ACK" in out:
        return True, "Disconnected"
    if "Disconnect-NAK" in out:
        return False, "Router refused (session already gone?)"
    if "timeout" in out.lower() or "no reply" in out.lower():
        return False, "Router did not answer on port %s. Is incoming RADIUS enabled on it?" % port
    return False, out.strip().splitlines()[-1] if out.strip() else "No response from router"


def test_login(username, password, secret):
    """Run a real Access-Request against the local FreeRADIUS. Returns (accepted, output)."""
    if not (_quote_ok(username) and _quote_ok(password)):
        return False, 'Quotes and backslashes are not supported in this test.'
    pkt = 'User-Name = "%s"\nUser-Password = "%s"\n' % (username, password)
    out = _run(["radclient", "-x", "-t", "3", "-r", "1", "127.0.0.1:1812", "auth", secret], pkt, 10)
    # never echo the tested password back to the page
    out = out.replace(password, "********") if password else out
    return "Access-Accept" in out, out.strip()


def log_coa(cur, username, nas_ip, ok, msg, by="", enabled="1"):
    """Record a disconnect request (CoA Request Log setting)."""
    if enabled != "1":
        return
    cur.execute("INSERT INTO coa_log (username, nas_ip, ok, message, created_by) VALUES (%s, %s, %s, %s, %s)",
                (username[:64], (nas_ip or "")[:45], 1 if ok else 0, str(msg or "")[:255], by[:64]))


def kick_all(cur, username, port):
    """Drop every live session of a user from the maintenance job. Returns the number dropped."""
    cur.execute("SELECT radacctid, nasipaddress, acctsessionid, framedipaddress FROM radacct "
                "WHERE username = %s AND acctstoptime IS NULL", (username,))
    sessions = cur.fetchall()
    cur.execute("SELECT nasname, secret FROM nas WHERE coa_enabled = 1")
    secrets = {r["nasname"]: r["secret"] for r in cur.fetchall()}
    done = 0
    for s in sessions:
        if secrets.get(s["nasipaddress"]):
            ok, msg = disconnect_session(s["nasipaddress"], secrets[s["nasipaddress"]], port, username,
                                         s["acctsessionid"], s["framedipaddress"])
            cur.execute("SELECT svalue FROM settings WHERE skey = 'coa_log'")
            row = cur.fetchone()
            log_coa(cur, username, s["nasipaddress"], ok, msg, "auto", row["svalue"] if row else "1")
            done += bool(ok)
    return done
