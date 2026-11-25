"""Helpers shared by app.py and the feature modules.

This file never imports app.py, so every module can import it without import cycles.
Feature modules register pages with the @route decorator; app.py attaches them all
at the end with core.attach(app).
"""
import os
import re
import subprocess
from datetime import datetime
from functools import wraps

from flask import abort, g, request

import db
import msglib

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@\-]{3,64}$")
PLAN_RE = re.compile(r"^[A-Za-z0-9_\-]{2,64}$")
PASSWORD_RE = re.compile(r"^[\x21-\x7e]{4,64}$")
FORMAT_RE = re.compile(r"^\[(\d{1,3})(?:-(\d{1,3}))?\]$")


def format_range(spec):
    """'[11-13]' -> (11, 13), '[11]' -> (11, 11); None if the text is not a valid format."""
    m = FORMAT_RE.match((spec or "").strip())
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    return (lo, hi) if 0 < lo <= hi <= 64 else None


def check_format(value, on, spec):
    """Error text when the switch is on and the digits of value are outside the allowed length, else ''."""
    if on != "1" or not value:
        return ""
    rng = format_range(spec)
    if not rng:
        return ""
    digits = len(re.sub(r"\D", "", value))
    if not rng[0] <= digits <= rng[1]:
        return "Needs %s digits." % (("%d to %d" % rng) if rng[0] != rng[1] else rng[0])
    return ""


PHONE_RE = re.compile(r"^[0-9+\-() ]{0,32}$")
POOL_RE = re.compile(r"^[A-Za-z0-9_.\-]{0,64}$")
RATE_RE = re.compile(r"^[0-9A-Za-z/ .]{0,255}$")
NAS_NAME_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,128}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PER_PAGE_CHOICES = (25, 50, 100, 200)
SUB_PER_PAGE = (25, 50, 100, 200, 500, 1000, 1500, 2000)     # the subscriber list goes up to 2000 a page
BASE_FROM = " FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id"

DATA_DIR = os.environ.get("PANEL_DATA_DIR", "/var/lib/isp-panel")


# ===================================================================== routing registry
ROUTES = []            # (rule, view function, options)
ENDPOINT_MODULE = {}   # endpoint -> permission module (a name, or a tuple meaning "any of")
ENDPOINT_NEED = {}     # endpoint -> level needed, when it differs from GET=view / POST=edit


def route(rule, module=None, need=None, **opts):
    def deco(fn):
        ROUTES.append((rule, fn, opts))
        if module:
            ENDPOINT_MODULE[fn.__name__] = module
        if need:
            ENDPOINT_NEED[fn.__name__] = need
        return fn
    return deco


def attach(app):
    for rule, fn, opts in ROUTES:
        app.add_url_rule(rule, endpoint=fn.__name__, view_func=fn, **opts)
    app.teardown_appcontext(close_db)


# ===================================================================== database
def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            if exc:
                conn.rollback()
            conn.close()
        except Exception:
            pass


def q(sql, args=None, one=False):
    with get_db().cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchone() if one else cur.fetchall()


def x(sql, args=None):
    """Run a write statement, return affected row count."""
    with get_db().cursor() as cur:
        cur.execute(sql, args or ())
        return cur.rowcount


def insert(sql, args=None):
    with get_db().cursor() as cur:
        cur.execute(sql, args or ())
        return cur.lastrowid


def commit():
    get_db().commit()


def cfg():
    if "settings" not in g:
        with get_db().cursor() as cur:
            g.settings = db.load_settings(cur)
    return g.settings


def audit(action, target="", detail="", who=None):
    me = getattr(g, "me", None)
    x("INSERT INTO audit_log (admin, action, target, detail, ip) VALUES (%s, %s, %s, %s, %s)",
      ((who or (me["username"] if me else ""))[:64], action[:64], str(target)[:128],
       str(detail)[:500], (request.remote_addr or "")[:45]))


def n(value):
    return int(value or 0)


def fmt_money(value):
    return msglib.money(value)


# ===================================================================== small helpers
def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


class _SafeCsv:
    """csv.writer whose cells can never run as a spreadsheet formula: text starting with = + - @ (that is not a number)
    gets a leading ' (a customer name typed as =HYPERLINK(...) stays text when the export is opened in Excel)."""
    def __init__(self, out):
        import csv
        self._w = csv.writer(out)

    @staticmethod
    def cell(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", chr(9), chr(13)) and not re.match(r"^-?[0-9]", v):
            return "'" + v
        return v

    def writerow(self, row):
        return self._w.writerow([self.cell(v) for v in row])

    def writerows(self, rows):
        for r in rows:
            self.writerow(r)


def csv_writer(out):
    return _SafeCsv(out)


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("Use the format YYYY-MM-DD.")


def pager(total, default_per=50, choices=PER_PAGE_CHOICES):
    try:
        per = int(request.args.get("per", default_per))
    except ValueError:
        per = default_per
    per = per if per in choices else default_per
    pages = max((total + per - 1) // per, 1)
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    return {"page": page, "per": per, "pages": pages, "total": total,
            "offset": (page - 1) * per, "first": (page - 1) * per + 1 if total else 0,
            "last": min(page * per, total)}


def page_number():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except ValueError:
        return 1


def date_range(default_days=29):
    """(start, end) from ?from= and ?to=, never more than a year."""
    from datetime import date, timedelta
    today = date.today()
    try:
        end = parse_date(request.args.get("to")) or today
        start = parse_date(request.args.get("from")) or (end - timedelta(days=default_days))
    except ValueError:
        start, end = today - timedelta(days=default_days), today
    if start > end or (end - start).days > 366:
        start, end = end - timedelta(days=default_days), end
    return start, end


def seller_boss():
    """The reseller / sub-reseller / retailer a technician belongs to (they added them under My Users), or None for the
    office's own staff. Such a technician sees and fixes only that reseller's customers."""
    me = getattr(g, "me", None)
    if not me or me["role"] in SELLER_ROLES or not me.get("parent_id"):
        return None
    if "boss" not in g:
        g.boss = q("SELECT id, username, full_name, role FROM panel_admins WHERE id = %s AND role IN ('reseller', 'subreseller', "
                   "'retailer')", (me["parent_id"],), one=True)
    return g.boss


def my_downline():
    """Usernames whose customers this user may see: themselves and everyone below them. None = no limit.
    A reseller's own technician sees their reseller's downline."""
    if g.me["role"] not in SELLER_ROLES:
        boss = seller_boss()
        if not boss:
            return None
        if "downline" not in g:
            g.downline = downline_of(boss["id"], boss["username"])
        return g.downline
    if "downline" not in g:
        g.downline = downline_of(g.me["id"], g.me["username"])
    return g.downline


def seller_techs(names=None):
    """Technicians a reseller (or their technician) may give jobs to: those added under anyone in the downline."""
    names = names if names is not None else my_downline()
    if not names:
        return []
    return q("SELECT a.id, a.username, a.full_name, a.phone, a.role FROM panel_admins a JOIN panel_admins p ON p.id = a.parent_id "
             "WHERE a.role = 'technician' AND a.is_active = 1 AND p.username IN (%s) ORDER BY a.username"
             % ",".join(["%s"] * len(names)), list(names))


def downline_of(uid, username):
    """A panel user's username and the usernames of everyone below them (any depth)."""
    rows = q("SELECT id, username, parent_id FROM panel_admins")
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    names, todo, seen = [username], [uid], {uid}
    while todo:
        for r in kids.get(todo.pop(), []):
            if r["id"] not in seen:
                seen.add(r["id"])
                names.append(r["username"])
                todo.append(r["id"])
    return names


def seller_pays(amount, note, method="transfer"):
    """A reseller / sub-reseller / retailer gives `amount` of their own balance to someone below them (a negative amount
    takes it back). Writes their ledger line. Returns (True, 0) or (False, their balance) when it is too low."""
    uid = g.me["id"]
    with get_db().cursor() as cur:
        if amount > 0:
            cur.execute("UPDATE panel_admins SET balance = balance - %s WHERE id = %s AND balance >= %s", (amount, uid, amount))
            if cur.rowcount != 1:
                cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (uid,))
                r = cur.fetchone()
                return False, float(r["balance"] or 0) if r else 0.0
        else:
            cur.execute("UPDATE panel_admins SET balance = balance - %s WHERE id = %s", (amount, uid))
        cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (uid,))
        after = cur.fetchone()
        cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (uid, -amount, list(after.values())[0] if after else 0, method[:32], note[:255], g.me["username"]))
    return True, 0


def scope_sql(alias="s"):
    """(' AND s.created_by IN (...)', params) for sellers, otherwise ('', [])."""
    names = my_downline()
    if names is None:
        return "", []
    return " AND %s.created_by IN (%s)" % (alias, ",".join(["%s"] * len(names))), list(names)


def olt_scope(alias="d"):
    """(' AND d.created_by IN (...)', params) so a seller only meets OLTs they or their downline added.
    Admins and staff see every OLT. OLTs with no owner (added before this feature) stay admin-only."""
    names = my_downline()
    if names is None:
        return "", []
    return " AND %s.created_by IN (%s)" % (alias, ",".join(["%s"] * len(names))), list(names)


def onu_scope(alias="o"):
    """Same idea for ONUs: only those on an OLT the seller can see."""
    sql, p = olt_scope("od")
    if not sql and not p and my_downline() is None:
        return "", []
    return " AND %s.olt_id IN (SELECT od.id FROM net_devices od WHERE od.kind = 'olt'%s)" % (alias, sql), p


# when the package ends / a grace period still running (db.py: shared with the daily report)
PKG_END = db.PKG_END
GRACE_ON = db.GRACE_ON


def stale_minutes():
    """Silence that makes an open session 'Stale': two missed router updates (Settings > interim interval), at least 15."""
    try:
        interim = int(cfg().get("interim_interval") or 600)
    except (TypeError, ValueError):
        interim = 600
    return max(15, -(-interim * 2 // 60))


def sub_filters(args):
    """WHERE clause for the subscribers table (alias s, plans alias p)."""
    where, params = [], []
    names = my_downline()
    if names is not None:
        where.append("s.created_by IN (%s)" % ",".join(["%s"] * len(names)))
        params += names
    text = args.get("q", "").strip()
    if text:
        like = "%" + text + "%"
        code = parse_sub_code(text)
        where.append("(s.username LIKE %s OR s.full_name LIKE %s OR s.phone LIKE %s "
                     "OR s.address LIKE %s OR s.static_ip LIKE %s" + (" OR s.id = %d" % code if code else "") + ")")
        params += [like] * 5
    if args.get("plan", "").isdigit():
        where.append("s.plan_id = %s")
        params.append(int(args["plan"]))
    if args.get("area"):
        where.append("s.area = %s")
        params.append(args["area"])
    status = args.get("status", "")
    online = "s.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL)"
    # the lists count from the moment the package really ends (the customer's time, else the package's, else 23:59:59),
    # the same moment RADIUS cuts them at (radius.END_MOMENT) - not just the date
    expired = "s.status = 'active' AND s.expiry_date IS NOT NULL AND " + PKG_END + " <= NOW()"
    running = "s.status = 'active' AND (s.expiry_date IS NULL OR " + PKG_END + " > NOW() OR " + GRACE_ON + ")"
    disabled = "(s.status = 'suspended' OR s.net_disabled = 1 OR (" + expired + " AND NOT " + GRACE_ON + "))"
    expiring = {"exp1": 1, "exp3": 3, "exp7": 7, "exp14": 14, "exp28": 28, "expiring": 7}
    if status == "active":
        where.append(running)
    elif status == "suspended":
        where.append("s.status = 'suspended'")
    elif status == "expired":
        where.append(expired)
    elif status in expiring:                  # ends within the next N days (N x 24 hours), not yet ended
        where.append("s.status = 'active' AND s.expiry_date IS NOT NULL AND " + PKG_END + " > NOW() AND "
                     + PKG_END + " <= NOW() + INTERVAL %d DAY" % expiring[status])
    elif status == "online":
        where.append(online)
    elif status == "offline":                 # customers who may use the internet now but are not connected
        where.append(running + " AND s.net_disabled = 0 AND NOT (" + online + ")")
    elif status == "stale":
        where.append("s.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL "
                     "AND COALESCE(acctupdatetime, acctstarttime) < NOW() - INTERVAL %d MINUTE)" % stale_minutes())
    elif status == "expired_online":          # should be cut: package ended, no grace, still connected
        where.append(expired + " AND NOT " + GRACE_ON + " AND " + online)
    elif status == "profile_disabled_online":
        where.append("s.status = 'suspended' AND " + online)
    elif status == "internet_disabled_online":
        where.append(disabled + " AND " + online)
    elif status == "internet_disabled":
        where.append(disabled)
    elif status == "mac":
        where.append("s.mac_bind <> ''")
    elif status == "grace":
        where.append(GRACE_ON)
    elif status == "renewed":                 # renewed by the system from the customer's balance (maintenance.py)
        where.append("s.id IN (SELECT subscriber_id FROM payments WHERE days_added > 0 AND received_by = 'auto' "
                     "AND paid_at >= NOW() - INTERVAL 7 DAY)")
    if args.get("seller"):
        where.append("s.created_by = %s")
        params.append(args["seller"])
    nas = args.get("nas", "")
    if nas and NAS_NAME_RE.match(nas):     # router: the customer's saved NAS, or a live session on it now
        where.append("(s.nas_ip = %s OR s.username IN (SELECT username FROM radacct WHERE acctstoptime IS NULL "
                     "AND nasipaddress = %s))")
        params += [nas, nas]
    for key, op in (("exp_from", ">="), ("exp_to", "<=")):
        v = args.get(key, "")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            where.append("s.expiry_date " + op + " %s")
            params.append(v)
    # join date = when the customer was added; activation date = a payment that added days (Activation / Renew)
    for key, op in (("join_from", "s.created_at >= %s"), ("join_to", "s.created_at < %s + INTERVAL 1 DAY")):
        v = args.get(key, "")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            where.append(op)
            params.append(v)
    act = [args.get(k, "") for k in ("act_from", "act_to")]
    act = [v if re.match(r"^\d{4}-\d{2}-\d{2}$", v) else "" for v in act]
    if act[0] or act[1]:
        sub = "s.id IN (SELECT subscriber_id FROM payments WHERE days_added > 0"
        if act[0]:
            sub += " AND paid_at >= %s"
            params.append(act[0])
        if act[1]:
            sub += " AND paid_at < %s + INTERVAL 1 DAY"
            params.append(act[1])
        where.append(sub + ")")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def service_state(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=4)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


# ===================================================================== roles and permissions
MODULES = [
    ("subscribers", "Subscribers"), ("online", "Online sessions"), ("plans", "Packages"),
    ("accounting", "Accounting"), ("inventory", "Inventory"), ("network", "Network"),
    ("area", "Areas"), ("tickets", "Tickets"), ("email", "Email"), ("sms", "SMS"),
    ("logs", "Logs"), ("notes", "Notes"), ("reports", "Reports"), ("documents", "Documents"),
    ("zones", "Department / Zone"), ("notices", "Notices"),
]
ROLES = [("admin", "Administrator"), ("manager", "Manager"), ("support", "Support"),
         ("accountant", "Accountant"), ("technician", "Technician"), ("collector", "Bill Collector"), ("reseller", "Reseller"),
         ("subreseller", "Sub-reseller"), ("retailer", "Retailer")]
STAFF_ROLES = ("manager", "support", "accountant", "technician", "collector")
SELLER_ROLES = ("reseller", "subreseller", "retailer")   # see only their own customers and those of their downline
LEVELS = ("none", "view", "edit")
RANK = {"none": 0, "view": 1, "edit": 2}


def _all(level):
    return {key: level for key, _ in MODULES}


DEFAULT_PERMS = {
    "manager": _all("edit"),
    "support": dict(subscribers="edit", online="edit", plans="view", accounting="view", inventory="view",
                    network="view", area="view", tickets="edit", email="none", sms="edit", logs="view",
                    notes="edit", reports="view", documents="edit", zones="view"),
    "accountant": dict(subscribers="view", online="none", plans="view", accounting="edit", inventory="none",
                       network="none", area="view", tickets="none", email="edit", sms="edit", logs="none",
                       notes="edit", reports="view", documents="view", zones="none"),
    "reseller": dict(subscribers="edit", online="view", plans="view", accounting="none", inventory="none",
                     network="none", area="view", tickets="edit", email="none", sms="edit", logs="none",
                     notes="edit", reports="none", documents="edit", zones="none"),
    "subreseller": dict(subscribers="edit", online="view", plans="view", accounting="none", inventory="none",
                        network="none", area="view", tickets="edit", email="none", sms="none", logs="none",
                        notes="edit", reports="none", documents="edit", zones="none"),
    "retailer": dict(subscribers="edit", online="none", plans="view", accounting="none", inventory="none",
                     network="none", area="none", tickets="edit", email="none", sms="none", logs="none",
                     notes="edit", reports="none", documents="none", zones="none"),
    "technician": dict(subscribers="edit", online="edit", plans="none", accounting="none", inventory="none",
                       network="none", area="view", tickets="edit", email="none", sms="none", logs="view",
                       notes="edit", reports="none", documents="view", zones="none"),
    # Bill Collector: sees customers and their dues, takes payments (Collection pages), tickets and notes. Their own
    # collections, report and deposits are on the Collection pages; never the office's accounting, reports or logs.
    "collector": dict(subscribers="view", online="none", plans="none", accounting="none", inventory="none",
                      network="none", area="view", tickets="edit", email="none", sms="none", logs="none",
                      notes="edit", reports="none", documents="none", zones="none", notices="view"),
}
# Areas a built-in role never gets, whatever Users > Roles says (the role's job has nothing to do there).
# Technician: no network devices, money, packages (or My Packages), stock, zones or ISP reports.
ROLE_LOCKS = {"technician": ("network", "accounting", "plans", "inventory", "zones", "reports"),
              "collector": ("online", "network", "accounting", "plans", "inventory", "zones", "reports", "email", "sms",
                            "logs", "documents")}


def load_perms(role):
    perms = _load_perms(role)
    for m in modules_off():
        perms[m] = "none"
    return perms


def _load_perms(role):
    if role == "admin":
        return _all("edit")
    perms = _all("none")
    perms.update(DEFAULT_PERMS.get(role, {}))
    perms.setdefault("notices", "view")
    if perms.get("notices") == "none" and "notices" not in DEFAULT_PERMS.get(role, {}):
        perms["notices"] = "view"
    for r in q("SELECT module, level FROM role_perms WHERE role = %s", (role,)):
        if r["module"] in perms and r["level"] in RANK:
            perms[r["module"]] = r["level"]
    for m in ROLE_LOCKS.get(role, ()):
        perms[m] = "none"
    return perms


def load_funcs(role, perms):
    """Set of function keys (permcatalog) this role may use. Admin: all of them."""
    import permcatalog as pc
    if role == "admin":
        return set(pc.KEYS)
    saved = {r["fkey"]: r["allowed"] for r in q("SELECT fkey, allowed FROM role_funcs WHERE role = %s", (role,))}
    seller = role in SELLER_ROLES
    ticked = {k for k in pc.KEYS if (bool(saved[k]) if k in saved else pc.default_on(k, perms, seller, role))}
    return pc.effective(ticked, perms) - pc.ROLE_NEVER.get(role, frozenset())


_funcs_upgraded = False


def upgrade_role_funcs():
    """Once (settings key perm_enforce_v1): tick permcatalog.NOW_ENFORCED for every role that could already use them,
    so wiring those ticks in takes nothing away from anyone. Later saves on Users > Roles decide as usual."""
    global _funcs_upgraded
    if _funcs_upgraded:
        return
    import permcatalog as pc
    db_ = get_db()
    with db_.cursor() as cur:
        cur.execute("INSERT IGNORE INTO settings (skey, svalue) VALUES ('perm_enforce_v1', %s)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M"),))
        if cur.rowcount == 1:                    # this process is the first: the others find the key already there
            rows = []
            for role, _label in role_list():
                if role == "admin":
                    continue
                perms = _load_perms(role)
                special = pc.ROLE_KEYS.get(role)
                for k in pc.NOW_ENFORCED:
                    mod, flags = pc.KEY_INFO[k]
                    if special and k.startswith(special[0]) and k not in special[1]:
                        continue
                    if mod is None or RANK.get(perms.get(mod, "none"), 0) >= RANK["view" if "v" in flags else "edit"]:
                        rows.append((role, k))
            if rows:
                cur.executemany("INSERT INTO role_funcs (role, fkey, allowed) VALUES (%s, %s, 1) "
                                "ON DUPLICATE KEY UPDATE allowed = 1", rows)
            cur.executemany("UPDATE role_funcs SET allowed = 0 WHERE fkey = %s", [(k,) for k in pc.NOW_CLEARED])
    db_.commit()
    _funcs_upgraded = True


def can_fn(key):
    me = getattr(g, "me", None)
    if me and me["role"] == "admin":
        return True
    return key in (getattr(g, "funcs", None) or ())


def apply_timezone(name):
    """Make Python's local time follow the Local Timezone setting (database times are not changed)."""
    import time
    if name and os.environ.get("TZ") != name and hasattr(time, "tzset"):
        os.environ["TZ"] = name
        time.tzset()


LOCALTIME = "/etc/localtime"
TIMEZONE_FILE = "/etc/timezone"


def server_timezone():
    """The server's own timezone name (what MariaDB's NOW() and services that do not read the setting use), or "".

    /etc/localtime is what the system clock really uses, and `timedatectl set-timezone` (install.sh) only changes that
    link. The old Debian file /etc/timezone is not updated by it and can still say Etc/UTC, so it is only a fallback."""
    try:
        link = os.path.realpath(LOCALTIME).replace(os.sep, "/")
        if "zoneinfo/" in link and os.path.exists(LOCALTIME):
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open(TIMEZONE_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def same_timezone(a, b):
    """True when two zone names give the same clock (same name, or same UTC offset in January and July)."""
    if not a or not b or a == b:
        return True
    from datetime import datetime as _dt
    import zoneinfo
    try:
        za, zb = zoneinfo.ZoneInfo(a), zoneinfo.ZoneInfo(b)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return True            # cannot tell: do not raise a false alarm
    return all(_dt(2026, m, 15, 12, tzinfo=za).utcoffset() == _dt(2026, m, 15, 12, tzinfo=zb).utcoffset() for m in (1, 7))


def modules_off():
    row = q("SELECT svalue FROM settings WHERE skey = 'modules_off'", one=True)
    return {m for m in (row["svalue"] if row else "").split(",") if m in dict(MODULES)}


def can(module, level="view"):
    perms = getattr(g, "perms", None)
    if not perms:
        return False
    if isinstance(module, (tuple, list)):
        return any(can(m, level) for m in module)
    return RANK.get(perms.get(module, "none"), 0) >= RANK[level]


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if g.me["role"] != "admin":
            abort(403)
        return fn(*a, **k)
    return wrapper


# packages a user may sell (no rows = every package)
def allowed_plan_ids():
    if "allowed_plans" not in g:
        if g.me["role"] == "admin":
            g.allowed_plans = None
        else:
            ids = {r["plan_id"] for r in q("SELECT plan_id FROM user_plans WHERE user_id = %s", (g.me["id"],))}
            # resellers, sub-resellers and retailers sell ONLY what the admin allocated to them (none allocated = none);
            # staff with no allocation keep seeing every package
            g.allowed_plans = ids if g.me["role"] in SELLER_ROLES else (ids or None)
    return g.allowed_plans


def filter_plans(plans):
    ids = allowed_plan_ids()
    return plans if ids is None else [p for p in plans if p["id"] in ids]


def plan_allowed(plan_id, current=None):
    """May the signed-in user give this package? Staff may also keep a customer's current package; sellers may not
    (an unallocated package has no seller price, so renewing it would cost them nothing)."""
    ids = allowed_plan_ids()
    if ids is None or plan_id in ids:
        return True
    return current is not None and plan_id == current and g.me["role"] not in SELLER_ROLES


# ===================================================================== areas
def remember_area(name):
    name = (name or "").strip()
    if name:
        x("INSERT IGNORE INTO areas (name) VALUES (%s)", (name[:64],))


def area_names():
    return [r["name"] for r in q("SELECT name FROM areas ORDER BY name")]


# ===================================================================== automatic messages
def notify(event, sub_id, extra=None, key=None):
    """Queue the automatic SMS / email for an event ('welcome' or 'payment') if it is switched on."""
    s = cfg()
    channels = [c for c, flag in (("sms", "auto_sms_" + event), ("email", "auto_email_" + event))
                if s.get(flag) == "1"]
    if not channels:
        return 0
    sub = q("SELECT s.id, s.username, s.full_name, s.phone, s.email, s.expiry_date, s.sms_on, s.email_on, "
            "p.label AS plan_label, p.price "
            "FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s", (sub_id,), one=True)
    if not sub:
        return 0
    more = dict(extra or {})
    if event == "welcome":
        pw = q("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password' LIMIT 1",
               (sub["username"],), one=True)
        more["password"] = pw["value"] if pw else ""
    ctx = msglib.sub_context(sub, s["isp_name"], more)
    channels = [c for c in channels if sub.get("sms_on" if c == "sms" else "email_on", 1)]
    queued = 0
    with get_db().cursor() as cur:
        for ch in channels:
            if ch == "sms":
                addr = msglib.normalize_phone(sub["phone"], s["sms_country_code"], s["sms_number_format"])
            else:
                addr = (sub["email"] or "").strip()
                addr = addr if EMAIL_RE.match(addr) else None
            if not addr:
                continue
            tpl = msglib.get_template(cur, ch, event)
            queued += msglib.enqueue(cur, ch, addr, msglib.render(tpl["body"], ctx),
                                     msglib.render(tpl["subject"], ctx), sub["id"],
                                     dedupe="%s:%s:%s" % (event, key or sub["id"], ch))
    return queued


# ===================================================================== dashboard helper
def attention():
    """Counts that need a human, limited to what this user may open."""
    rows = []
    if can("tickets"):
        names = my_downline()            # a reseller counts only their own downline's tickets
        c = q("SELECT COUNT(*) AS c FROM tickets WHERE status IN ('open', 'pending')" +
              ("" if names is None else " AND seller IN (%s)" % ",".join(["%s"] * len(names))),
              [] if names is None else list(names), one=True)["c"]
        rows.append({"label": "Open tickets", "c": c, "endpoint": "tickets", "args": {"status": "open"}})
    if can("inventory"):
        c = q("SELECT COUNT(*) AS c FROM inventory_items WHERE min_qty > 0 AND qty <= min_qty", one=True)["c"]
        rows.append({"label": "Items low on stock", "c": c, "endpoint": "inventory", "args": {"low": "1"}})
    if can("network"):
        sc, sp = olt_scope("d")
        if my_downline() is None:           # admin / staff: every device
            c = q("SELECT COUNT(*) AS c FROM net_devices d WHERE d.last_status = 'down'", one=True)["c"]
            rows.append({"label": "Devices down", "c": c, "endpoint": "devices", "args": {}})
        elif can_fn("olt.module"):          # resellers: only their own OLTs
            c = q("SELECT COUNT(*) AS c FROM net_devices d WHERE d.kind = 'olt' AND d.last_status = 'down'" + sc, sp, one=True)["c"]
            rows.append({"label": "OLTs down", "c": c, "endpoint": "olts", "args": {}})
    if can(("sms", "email")):
        c = q("SELECT COUNT(*) AS c FROM outbox WHERE status = 'failed' AND created_at >= NOW() - INTERVAL 1 DAY",
              one=True)["c"]
        rows.append({"label": "Failed messages, 24 h", "c": c, "endpoint": "messages", "args": {"status": "failed"}})
    if g.me["role"] == "admin":
        import db
        enc = q("SELECT skey, svalue FROM settings WHERE svalue LIKE 'enc:v1:%%'")
        bad = db.unreadable_secrets([(r["skey"], r["svalue"]) for r in enc])
        if bad:
            rows.append({"label": "Secret settings unreadable: check PANEL_SETTINGS_KEY in panel.env", "c": len(bad),
                         "endpoint": "settings", "args": {}})
    return rows


# ===================================================================== subscriber ID as customers see it
def sub_code(sid):
    """The subscriber ID as the panel shows it and customers write it: Settings > Subscriber > Subscriber ID Prefix, then
    the number with at least two digits (RN01, RN02 ... RN125). An empty prefix shows the plain number."""
    if sid is None or sid == "":
        return ""
    prefix = (cfg().get("sub_id_prefix") or "").strip()
    return "%s%02d" % (prefix, int(sid)) if prefix else str(sid)


def parse_sub_code(text):
    """The subscriber ID number in what someone typed: RN01, rn1, RN-01, RN 01, #12, ID: 12 or 12. None otherwise."""
    prefix = (cfg().get("sub_id_prefix") or "").strip()
    alt = (r"|" + re.escape(prefix) + r"\s*[-#.:]?\s*") if prefix else ""
    m = re.fullmatch(r"(?:#|ID\s*[:#.-]?\s*" + alt + r")?(\d{1,9})", (text or "").strip(), re.I)
    return int(m.group(1)) if m and int(m.group(1)) else None


def ref_sub_codes(text):
    """Subscriber IDs written with the prefix in a bKash reference: RN01, rn01, RN 01, rn 01, Rn01, Rn 01 (the prefix in
    any case, at most one space, then the number; "RN05 bill" counts too). [] when there is none or no prefix is set."""
    prefix = (cfg().get("sub_id_prefix") or "").strip()
    if not prefix or not text:
        return []
    found = re.findall(r"(?<![A-Za-z0-9])" + re.escape(prefix) + r" ?(\d{1,9})(?!\d)", text, re.I)
    return list(dict.fromkeys(int(n) for n in found))


# ===================================================================== subscriber serial numbers
def next_serial():
    """Next customer serial from the Serial Number Prefix setting (empty prefix = random 8-digit number)."""
    import secrets
    prefix = (cfg().get("serial_prefix") or "").strip()
    if not prefix:
        for _ in range(20):
            cand = str(secrets.randbelow(90000000) + 10000000)
            if not q("SELECT 1 AS x FROM subscribers WHERE serial = %s", (cand,), one=True):
                return cand
        return cand
    m = re.match(r"^([A-Za-z]*)([0-9]*)$", prefix)
    letters, digits = m.group(1), m.group(2)
    row = q("SELECT MAX(CAST(SUBSTRING(serial, %s) AS UNSIGNED)) AS m FROM subscribers WHERE serial REGEXP %s",
            (len(letters) + 1, "^" + letters + "[0-9]+$"), one=True)
    last = int(row["m"]) if row and row["m"] is not None else (int(digits) if digits else 0)
    if digits and int(digits) > last:
        last = int(digits)
    return "%s%0*d" % (letters, len(digits), last + 1)


# ===================================================================== partner tiers (Settings > Partner Management)
TIERS = db.TIERS


def tier_name(role, plural=False, settings=None):
    """The (possibly renamed) word for a partner tier."""
    s = settings if settings is not None else cfg()
    for key, one, many in TIERS:
        if key == role:
            name = (s.get("tier_name_" + key) or "").strip() or one
            if plural:
                return (s.get("tier_plural_" + key) or "").strip() or ((s.get("tier_name_" + key) or "").strip() or many)
            return name
    return role


def custom_roles():
    """Roles created on the Role page (office-staff type). [] when the table does not exist yet."""
    if "custom_roles" not in g:
        try:
            g.custom_roles = q("SELECT role_key, name, created_by, created_at FROM custom_roles ORDER BY name")
        except Exception:
            g.custom_roles = []
    return g.custom_roles


def staff_roles():
    return tuple(STAFF_ROLES) + tuple(r["role_key"] for r in custom_roles())


def role_list():
    """Built-in roles (partner tiers renamed) followed by the custom ones."""
    return [(k, tier_name(k) if k in dict((t[0], 1) for t in TIERS) else label) for k, label in ROLES] + \
           [(r["role_key"], r["name"]) for r in custom_roles()]


def role_labels():
    return dict(role_list())


def tiers_off(settings=None):
    return db.tiers_off(settings if settings is not None else cfg())


def blocked_usernames(admins, settings=None):
    return db.blocked_usernames(admins, settings if settings is not None else cfg())


def user_blocked(user_id):
    if not tiers_off():
        return False
    rows = q("SELECT id, username, role, parent_id FROM panel_admins")
    me = next((r["username"] for r in rows if r["id"] == user_id), None)
    return me in blocked_usernames(rows)


def bw_text(plan):
    """'10 Mbps' for the Bandwidth shown on a package (blank when none was entered)."""
    v = plan.get("bandwidth_value") if plan else None
    if v in (None, "", 0):
        return ""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "%s %s" % (("%d" % v) if v == int(v) else ("%g" % v), plan.get("bandwidth_type") or "Mbps")
