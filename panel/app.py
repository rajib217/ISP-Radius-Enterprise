"""ISP RADIUS Panel - web admin for FreeRADIUS + MariaDB (PPPoE subscribers)."""
import csv
import io
import ipaddress
import os
import re
import secrets
import shutil
import subprocess
from collections import deque
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import core
import dash
import db
import nav
import permcatalog
import radius
from core import (BASE_FROM, EMAIL_RE, NAS_NAME_RE, PASSWORD_RE, PER_PAGE_CHOICES, PHONE_RE, PLAN_RE,  # noqa: F401
                  POOL_RE, RATE_RE, USERNAME_RE, admin_required, audit, cfg, commit, get_db, insert, n,
                  pager, parse_date, q, safe_next, service_state, sub_filters, x)

# which permission module each of the original pages belongs to (new pages declare their own)
core.ENDPOINT_MODULE.update({
    "subscribers": "subscribers", "subscriber_new": "subscribers", "subscriber_edit": "subscribers",
    "subscriber_view": "subscribers", "subscriber_toggle": "subscribers", "subscriber_kick": "subscribers",
    "subscriber_delete": "subscribers", "subscribers_bulk": "subscribers", "subscribers_export": "subscribers",
    "import_sample": "subscribers", "import_csv": "subscribers",
    "subscriber_renew": ("subscribers", "accounting"), "receipt": ("subscribers", "accounting"),
    "online": "online", "online_disconnect": "online", "api_traffic": "online",
    "plans": "plans", "plan_save": "plans", "plan_delete": "plans",
    "nas_list": "network", "nas_save": "network", "nas_delete": "network", "tools": "network",
    "tools_reload": "network", "logs": "logs",
})
core.ENDPOINT_NEED["tools"] = "view"   # testing a login changes nothing

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.secret_key = os.environ.get("PANEL_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PANEL_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=4 * 1024 ** 3,   # only the backup upload may be large; gate() holds every other page to 20 MB
)



# ====================================================================
#  database helpers
# ====================================================================


















# ====================================================================
#  health check (update.sh and the GitHub deploy check this after every update)
# ====================================================================
_REVISION = None


@app.route("/healthz")
def healthz():
    """200 + {"ok": true, "revision": ...} when this worker runs and the database answers; 503 otherwise.
    Shows nothing private: no versions of other software, no counts, no errors."""
    global _REVISION
    if _REVISION is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "REVISION")) as f:
                _REVISION = f.read().strip()[:40]
        except OSError:
            _REVISION = "unknown"
    try:
        q("SELECT 1")
        ok = True
    except Exception:
        ok = False
    resp = jsonify({"ok": ok, "revision": _REVISION})
    resp.status_code = 200 if ok else 503
    resp.headers["Cache-Control"] = "no-store"
    return resp

# ====================================================================
#  template helpers
# ====================================================================
@app.template_filter("bytes")
def f_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0


@app.template_filter("clock")
def f_clock(value):
    """'15:30' -> '3:30 PM'."""
    try:
        h, m = (int(v) for v in str(value).split(":")[:2])
    except (TypeError, ValueError):
        return value or ""
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


@app.template_filter("bps")
def f_bps(value):
    """Bits per second in network units (1000-based): 940 Kbps, 18.4 Mbps, 1.84 Gbps."""
    value = float(value or 0)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if value < 1000 or unit == "Gbps":
            return ("%d %s" % (value, unit)) if unit == "bps" else ("%.2f %s" % (value, unit) if value < 100 else "%.0f %s" % (value, unit))
        value /= 1000.0


@app.template_filter("dur")
def f_dur(seconds):
    s = int(seconds or 0)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm" % m
    return "%ds" % s


@app.template_filter("dt")
def f_dt(value, fmt="%d %b %Y, %H:%M"):
    return value.strftime(fmt) if value else "—"


@app.template_filter("d")
def f_d(value):
    return value.strftime("%d %b %Y") if value else "—"


@app.template_filter("money")
def f_money(value):
    value = float(value or 0)
    return "{:,.0f}".format(value) if value == int(value) else "{:,.2f}".format(value)


@app.template_filter("ago")
def f_ago(value):
    if not value:
        return "never"
    secs = int((datetime.now() - value).total_seconds())
    if secs < 60:
        return "just now"
    return f_dur(secs) + " ago"


def asset_url(name):
    """Static file URL with its modification time, so browsers (and nginx's 7 day cache) fetch new versions."""
    try:
        v = int(os.path.getmtime(os.path.join(app.static_folder, name)))
    except OSError:
        v = 0
    return url_for("static", filename=name, v=v)


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {
        "me": getattr(g, "me", None),
        "switched_from": session.get("real_uid"),
        "isp_name": cfg()["isp_name"] if "uid" in session else "ISP Panel",
        "brand": mod_settings.brand_urls(),
        "gateways": mod_settings.GATEWAYS,
        "ptype_of": mod_people._ptype,
        "app_cfg": cfg() if "uid" in session else {},
        "show_credit": cfg()["show_credit"] == "1" if "uid" in session else False,
        "copyright_text": cfg()["copyright_text"] if "uid" in session else "",
        "currency": cfg()["currency"] if "uid" in session else "",
        "csrf_input": lambda: Markup('<input type="hidden" name="_csrf" value="%s">' % session["csrf"]),
        "csrf_value": session["csrf"],
        "endpoint": request.endpoint or "",
        "today": date.today(),
        "asset": asset_url,
        "can": core.can,
        "can_fn": core.can_fn,
        "bw": core.bw_text,
        "nav": nav.build() if getattr(g, "me", None) else [],
    }




# ====================================================================
#  security: CSRF + login gate
# ====================================================================
@app.before_request
def gate():
    if request.endpoint == "static":
        return None
    if (request.content_length or 0) > 20 * 1024 * 1024 and request.endpoint != "backup_upload":
        abort(413)
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not session.get("csrf") or not secrets.compare_digest(sent, session["csrf"]):
            abort(400, "Your session expired. Reload the page and try again.")
    if request.endpoint in ("login", "branding", "healthz") or (request.endpoint or "").startswith("portal"):
        return None          # the customer portal has its own sign-in (mod_portal.py)
    uid = session.get("uid")
    if not uid:
        nxt = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("login", next=nxt))
    me = q("SELECT id, username, full_name, role, is_active, balance FROM panel_admins WHERE id = %s",
           (uid,), one=True)
    if not me or not me["is_active"] or core.user_blocked(me["id"]):
        session.clear()
        return redirect(url_for("login"))
    g.me = me
    core.apply_timezone(cfg().get("timezone"))
    g.perms = core.load_perms(me["role"])
    g.funcs = core.load_funcs(me["role"], g.perms)
    sid = (request.view_args or {}).get("sid")
    if sid is not None and core.my_downline() is not None:
        own = q("SELECT created_by FROM subscribers WHERE id = %s", (sid,), one=True)
        if own and own["created_by"] not in core.my_downline():
            abort(403)
    mods = core.ENDPOINT_MODULE.get(request.endpoint)
    if mods:
        need = core.ENDPOINT_NEED.get(request.endpoint) or ("view" if request.method in ("GET", "HEAD") else "edit")
        if not core.can(mods, need):
            abort(403)
        # sellers never open the package pages (list, add / edit, policy, allocation, fees, package report), whatever
        # their role permissions say: they see only their allocated packages, under My Packages
        if me["role"] in core.SELLER_ROLES and "plans" in ((mods,) if isinstance(mods, str) else mods):
            abort(403)
    fn = permcatalog.ENDPOINT_FN.get(request.endpoint)
    if fn and me["role"] != "admin" and not any(core.can_fn(k) for k in ((fn,) if isinstance(fn, str) else fn)):
        abort(403)
    return None




@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def http_error(err):
    messages = {
        400: ("Request not accepted", err.description),
        403: ("Not allowed", "Your account does not have permission for this page."),
        404: ("Page not found", "That page does not exist or was removed."),
        413: ("File too large", "The upload is larger than 20 MB."),
    }
    title, text = messages.get(err.code, ("Error", str(err)))
    return render_template("error.html", code=err.code, title=title, text=text), err.code


@app.errorhandler(500)
def server_error(err):
    return render_template("error.html", code=500, title="Something went wrong",
                           text="The server could not finish this request. Check the panel log: "
                                "journalctl -u isp-panel"), 500


# ====================================================================
#  login / logout / account
# ====================================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or ""
        username = request.form.get("username", "").strip()[:64]
        recent = q("SELECT COUNT(*) AS c FROM audit_log WHERE action = 'login_failed' "
                   "AND ip = %s AND created_at > NOW() - INTERVAL 10 MINUTE", (ip,), one=True)["c"]
        if recent >= 8:
            error = "Too many failed attempts from this address. Try again in 10 minutes."
        else:
            user = q("SELECT * FROM panel_admins WHERE username = %s", (username,), one=True)
            if user and user["is_active"] and check_password_hash(user["password_hash"],
                                                                  request.form.get("password", "")) \
                    and core.user_blocked(user["id"]):
                audit("login_blocked", target=username, who=username)
                commit()
                error = "This account is switched off."
            elif user and user["is_active"] and check_password_hash(user["password_hash"],
                                                                    request.form.get("password", "")):
                session.clear()
                session["uid"] = user["id"]
                session["csrf"] = secrets.token_hex(16)
                session.permanent = True
                x("UPDATE panel_admins SET last_login = NOW() WHERE id = %s", (user["id"],))
                audit("login", who=user["username"])
                commit()
                return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
            audit("login_failed", target=username, who=username)
            commit()
            error = "Wrong username or password."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    audit("logout")
    commit()
    session.clear()
    return redirect(url_for("login"))




# ====================================================================
#  dashboard
# ====================================================================
def server_info():
    info = {}
    try:
        info["load"] = os.getloadavg()
        info["cpus"] = os.cpu_count() or 1
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
        info["mem_total"] = mem["MemTotal"]
        info["mem_used"] = mem["MemTotal"] - mem.get("MemAvailable", mem["MemFree"])
    except (OSError, KeyError, ValueError):
        pass
    try:
        du = shutil.disk_usage("/")
        info["disk_total"], info["disk_used"] = du.total, du.used
    except OSError:
        pass
    try:
        with open("/proc/uptime") as fh:
            info["uptime"] = int(float(fh.read().split()[0]))
    except OSError:
        pass
    return info


def _notices():
    if not core.can("notices"):
        return []
    import mod_notices
    return mod_notices.active_notices()


@app.route("/")
def dashboard():
    seller = core.my_downline() is not None
    tabs = dash.allowed_tabs()
    tab = request.args.get("tab") if request.args.get("tab") in dict(tabs) else (tabs[0][0] if tabs else "")
    tabdata = dash.load(tab) if tab else None
    if seller:
        # a seller's own customers (and their downline's) whose package ends within 7 days
        names = list(core.my_downline() or [])
        where = ("WHERE s.status = 'active' AND s.expiry_date BETWEEN CURDATE() AND CURDATE() + INTERVAL 7 DAY "
                 "AND s.created_by IN (" + ",".join(["%s"] * len(names)) + ") ") if names else ""
        total = q("SELECT COUNT(*) AS c FROM subscribers s " + where, names, one=True)["c"] if names else 0
        per = 20                                        # 20 customers a page (the owner's choice)
        pages = max((total + per - 1) // per, 1)
        page = min(max(request.args.get("page", 1, type=int) or 1, 1), pages)
        exp_pg = {"page": page, "per": per, "pages": pages, "total": total, "offset": (page - 1) * per,
                  "first": (page - 1) * per + 1 if total else 0, "last": min(page * per, total)}
        my_expiring = q("SELECT s.id, s.username, s.full_name, s.phone, s.expiry_date, s.created_by, "
                        "COALESCE(p.label, p.name) AS plan_label FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
                        + where + "ORDER BY s.expiry_date, s.username LIMIT %s OFFSET %s",
                        names + [per, exp_pg["offset"]]) if names else []
        return render_template("dashboard.html", seller=True, tabs=tabs, tab=tab, td=tabdata, attention=core.attention(),
                               notices=_notices(), my_expiring=my_expiring, exp_pg=exp_pg,
                               exp_keep={"tab": tab} if request.args.get("tab") else {}, today=date.today())
    st = q("SELECT COUNT(*) AS total, "
           "SUM(status = 'suspended') AS suspended, "
           "SUM(status = 'active' AND expiry_date IS NOT NULL AND expiry_date < CURDATE()) AS expired, "
           "SUM(status = 'active' AND (expiry_date IS NULL OR expiry_date >= CURDATE())) AS active, "
           "SUM(status = 'active' AND expiry_date BETWEEN CURDATE() AND CURDATE() + INTERVAL 7 DAY) AS expiring "
           "FROM subscribers", one=True)
    stats = {k: n(v) for k, v in st.items()}
    online = q("SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL", one=True)["c"]

    snaps = q("SELECT ts, online FROM stats_snapshots WHERE ts >= NOW() - INTERVAL 24 HOUR ORDER BY ts")
    series = {"labels": [s["ts"].strftime("%H:%M") for s in snaps],
              "stamps": [s["ts"].strftime("%d %b %H:%M") for s in snaps],
              "values": [s["online"] for s in snaps]}
    peak = max(series["values"] + [online])

    auth = {r["reply"]: r["c"] for r in q(
        "SELECT reply, COUNT(*) AS c FROM radpostauth "
        "WHERE authdate >= NOW() - INTERVAL 24 HOUR GROUP BY reply")}
    plans = q("SELECT COALESCE(p.label, 'No plan') AS name, COUNT(*) AS c FROM subscribers s "
              "LEFT JOIN plans p ON p.id = s.plan_id GROUP BY p.id, p.label ORDER BY c DESC")
    by_nas = q("SELECT ra.nasipaddress AS ip, COALESCE(nas.shortname, ra.nasipaddress) AS name, "
               "COUNT(*) AS c FROM radacct ra LEFT JOIN nas ON nas.nasname = ra.nasipaddress "
               "WHERE ra.acctstoptime IS NULL GROUP BY ra.nasipaddress, nas.shortname ORDER BY c DESC")
    rejects = q("SELECT username, authdate FROM radpostauth WHERE reply = 'Access-Reject' "
                "ORDER BY id DESC LIMIT 6")
    expiring = q("SELECT id, username, full_name, expiry_date FROM subscribers WHERE status = 'active' "
                 "AND expiry_date BETWEEN CURDATE() AND CURDATE() + INTERVAL 7 DAY "
                 "ORDER BY expiry_date, username LIMIT 6")
    month_start = date.today().replace(day=1)
    revenue = q("SELECT COALESCE(SUM(amount), 0) AS s, COUNT(*) AS c FROM payments WHERE paid_at >= %s",
                (month_start,), one=True)
    plan_mix = {"items": [{"name": p["name"], "value": p["c"]} for p in plans]}
    money = core.can("accounting")
    v3 = {
        "growth": dash.growth_pct(stats["total"]),
        "dues": dash.dues_total() if money else None,
        "money": money,
        "bw": core.can("online"),
        "now": dash.throughput_now() if core.can("online") else None,
        "traffic": dash.traffic_series("24h") if core.can("online") else None,
        "revenue6": dash.revenue_6m() if money else None,
        "mix": dash.status_mix(),
    }
    return render_template("dashboard.html", stats=stats, online=online, peak=peak, series=series,
                           plan_mix=plan_mix, v3=v3,
                           auth_ok=n(auth.get("Access-Accept")), auth_bad=n(auth.get("Access-Reject")),
                           plans=plans, by_nas=by_nas, rejects=rejects, expiring=expiring,
                           revenue=revenue, server=server_info(), attention=core.attention(),
                           notices=_notices(),
                           seller=False, tabs=tabs, tab=tab, td=tabdata)


@app.route("/api/live")
def api_live():
    online = q("SELECT COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL", one=True)["c"]
    bad = q("SELECT COUNT(*) AS c FROM radpostauth WHERE reply = 'Access-Reject' "
            "AND authdate >= NOW() - INTERVAL 1 HOUR", one=True)["c"]
    return jsonify(online=online, rejects_hour=bad, time=datetime.now().strftime("%H:%M:%S"))


@app.route("/api/traffic")
def api_traffic():
    """Bandwidth chart data for the dashboard's 1H / 24H / 7D / 30D buttons."""
    if core.my_downline() is not None:
        abort(403)
    rng = request.args.get("range", "24h")
    return jsonify(dash.traffic_series(rng if rng in dash.TRAFFIC_RANGES else "24h"))


# ====================================================================
#  subscribers
# ====================================================================
SORTS = {"id": "s.id", "username": "s.username", "name": "s.full_name", "plan": "p.label",
         "expiry": "s.expiry_date", "created": "s.created_at", "area": "s.area"}






def decorate(rows):
    today = date.today()
    for r in rows:
        if r["status"] == "suspended":
            r["state"] = "suspended"
        elif r["expiry_date"] and r["expiry_date"] < today:
            r["state"] = "expired"
        else:
            r["state"] = "active"
        r["days_left"] = (r["expiry_date"] - today).days if r["expiry_date"] else None
    return rows




@app.route("/subscribers")
def subscribers():
    where, params = sub_filters(request.args)
    total = q("SELECT COUNT(*) AS c" + BASE_FROM + where, params, one=True)["c"]
    pg = pager(total)
    sort = request.args.get("sort", "created")
    sort = sort if sort in SORTS else "created"
    direction = "ASC" if request.args.get("dir") == "asc" else "DESC"
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.area, s.status, s.expiry_date, s.grace_until, "
             "s.mac_bind, s.static_ip, s.created_by, s.balance, s.photo, s.plan_id, p.label AS plan_label, p.download_mbps, p.upload_mbps, p.bandwidth_value, p.bandwidth_type, "
             "p.price AS plan_price, p.validity_days AS plan_days, "
             "a.full_name AS seller_name, a.role AS seller_role, "
             "(SELECT value FROM radcheck rc WHERE rc.username = s.username AND rc.attribute = 'Cleartext-Password' "
             "LIMIT 1) AS password, "
             "(SELECT COALESCE(SUM(c.amount), 0) FROM sub_charges c WHERE c.subscriber_id = s.id AND c.paid = 0) "
             "AS due" + BASE_FROM + " LEFT JOIN panel_admins a ON a.username = s.created_by" + where +
             " ORDER BY " + SORTS[sort] + " " + direction + ", s.id DESC LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    decorate(rows)
    if rows:
        marks = ",".join(["%s"] * len(rows))
        names = [r["username"] for r in rows]
        live = {}       # username -> newest open session (IP, MAC, router)
        for a in q("SELECT username, framedipaddress, callingstationid, nasipaddress, acctstarttime FROM radacct "
                   "WHERE acctstoptime IS NULL AND username IN (%s) ORDER BY acctstarttime" % marks, names):
            live[a["username"]] = a
        try:            # per-customer rate from the 5-minute traffic job (table appears after install.sh is re-run)
            rates = {t["username"]: t for t in q(
                "SELECT username, SUM(down_bps) AS down, SUM(up_bps) AS up FROM traffic_last "
                "WHERE username IN (%s) GROUP BY username" % marks, names)}
        except Exception:
            rates = {}
        for r in rows:
            a = live.get(r["username"])
            r["online"] = a is not None
            r["live_ip"] = (a["framedipaddress"] if a else "") or r["static_ip"] or ""
            r["live_mac"] = (a["callingstationid"] if a else "") or r["mac_bind"] or ""
            r["live_nas"] = a["nasipaddress"] if a else ""
            t = rates.get(r["username"]) if a else None
            r["rx_bps"], r["tx_bps"] = (float(t["down"] or 0), float(t["up"] or 0)) if t else (None, None)
    plans = core.filter_plans(q("SELECT id, label FROM plans ORDER BY download_mbps, label"))
    areas = [r["area"] for r in q("SELECT DISTINCT area FROM subscribers WHERE area <> '' ORDER BY area")]
    sellers = q("SELECT username, full_name, role FROM panel_admins ORDER BY username") if core.my_downline() is None else []
    dl = core.my_downline()
    sales = sellers if dl is None else (q("SELECT username, full_name, role FROM panel_admins WHERE username IN (%s) ORDER BY username"
                                          % ",".join(["%s"] * len(dl)), list(dl)) if dl else [])
    nas_rows = q("SELECT id, shortname, nasname FROM nas ORDER BY shortname") if dl is None else []
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("subscribers.html", rows=rows, pg=pg, plans=plans,
                           act_plans=core.filter_plans(q("SELECT * FROM plans ORDER BY download_mbps, label")), areas=areas, sellers=sellers,
                           sales=sales, nas_rows=nas_rows,
                           roles=core.role_labels(), keep=keep, sort=sort, direction=direction.lower(),
                           total_all=pg["total"])


def get_plan(plan_id):
    if not plan_id:
        return None
    return q("SELECT * FROM plans WHERE id = %s", (plan_id,), one=True)




def read_sub_form(form, creating, sub_id=0):
    data = {k: form.get(k, "").strip() for k in
            ("username", "password", "full_name", "phone", "email", "address", "area",
             "plan_id", "status", "expiry_date", "static_ip", "notes", "identity")}
    errors = {}
    if creating:
        if not USERNAME_RE.match(data["username"]):
            errors["username"] = "3-64 characters: letters, numbers and . _ @ -"
        if not PASSWORD_RE.match(data["password"]):
            errors["password"] = "4-64 characters, no spaces."
    elif data["password"] and not PASSWORD_RE.match(data["password"]):
        errors["password"] = "4-64 characters, no spaces. Leave empty to keep the current one."
    if len(data["full_name"]) > 128:
        errors["full_name"] = "Too long (128 max)."
    if not PHONE_RE.match(data["phone"]):
        errors["phone"] = "Digits, + - ( ) and spaces only."
    c = cfg()
    if "phone" not in errors:
        bad = core.check_format(data["phone"], c["phone_format_on"], c["phone_format"])
        if bad:
            errors["phone"] = bad
    if len(data["identity"]) > 32:
        errors["identity"] = "Too long (32 max)."
    else:
        bad = core.check_format(data["identity"], c["identity_format_on"], c["identity_format"])
        if bad:
            errors["identity"] = bad
    if "phone" not in errors and data["phone"] and c["dup_phone"] != "1":
        if q("SELECT username FROM subscribers WHERE phone = %s AND id <> %s LIMIT 1", (data["phone"], sub_id), one=True):
            errors["phone"] = "Another subscriber already uses this phone number."
    if "identity" not in errors and data["identity"] and c["dup_nid"] != "1":
        if q("SELECT username FROM subscribers WHERE identity = %s AND id <> %s LIMIT 1", (data["identity"], sub_id), one=True):
            errors["identity"] = "Another subscriber already uses this NID/NIC."
    if not data["identity"] and c["identity_optional"] != "1":
        errors["identity"] = "Identity (NID/NIC) is required."
    if data["email"] and ("@" not in data["email"] or len(data["email"]) > 128):
        errors["email"] = "Enter a valid email address."
    if len(data["address"]) > 255:
        errors["address"] = "Too long (255 max)."
    if len(data["area"]) > 64:
        errors["area"] = "Too long (64 max)."
    current_plan = None
    if sub_id:
        old_row = q("SELECT plan_id FROM subscribers WHERE id = %s", (sub_id,), one=True)
        current_plan = old_row["plan_id"] if old_row else None
    if data["plan_id"] and (not data["plan_id"].isdigit() or not get_plan(int(data["plan_id"]))):
        errors["plan_id"] = "Pick one of the listed plans."
    elif data["plan_id"] and not core.plan_allowed(int(data["plan_id"]), current_plan):
        errors["plan_id"] = "You are not allowed to use this plan."
    if data["status"] not in ("active", "suspended"):
        data["status"] = "active"
    # Service Settings permissions: a field the person may not change keeps its saved value (when editing) or its default
    locks = (("plan_id", "svc.packages"), ("status", "svc.profile_status"), ("static_ip", "svc.ip"), ("password", "svc.conn_pw"))
    shut = [f for f, key in locks if not core.can_fn(key) and not (creating and f in ("plan_id", "password"))]
    if shut:
        was = {} if creating else (q("SELECT plan_id, status, static_ip FROM subscribers WHERE id = %s", (sub_id,), one=True) or {})
        for f in shut:
            if f == "password":
                data["password"] = ""
            elif f == "status":
                data["status"] = was.get("status") if not creating and was.get("status") in ("active", "suspended") else "active"
            elif f == "static_ip":
                data["static_ip"] = str(was.get("static_ip") or "") if not creating else ""
            else:
                data["plan_id"] = str(was.get("plan_id") or "")
    can_exp = core.can_fn("sub.expiry")
    if not can_exp:                # without the Expiration Date permission the date cannot be typed in or changed
        old = None if creating else q("SELECT expiry_date FROM subscribers WHERE id = %s", (sub_id,), one=True)
        was = old["expiry_date"] if old else None
        data["expiry_date"] = was.isoformat() if hasattr(was, "isoformat") else ""
    try:
        data["expiry"] = parse_date(data["expiry_date"])
    except ValueError as exc:
        data["expiry"] = None
        errors["expiry_date"] = str(exc)
    if not can_exp and creating:   # a new customer starts expired until the first activation
        data["expiry"] = date.today() - timedelta(days=1)
    if data["static_ip"]:
        try:
            ipaddress.IPv4Address(data["static_ip"])
            taken = q("SELECT username FROM subscribers WHERE static_ip = %s AND id <> %s",
                      (data["static_ip"], sub_id), one=True)
            if taken:
                errors["static_ip"] = "Already assigned to %s." % taken["username"]
        except ValueError:
            errors["static_ip"] = "Enter an IPv4 address like 10.20.0.15."
    if len(data["notes"]) > 2000:
        errors["notes"] = "Too long (2000 max)."
    return data, errors


@app.route("/subscribers/new", methods=["GET", "POST"])
def subscriber_new():
    plans = core.filter_plans(q("SELECT * FROM plans ORDER BY download_mbps, label"))
    form, errors = {"status": "active"}, {}
    if request.args.get("plan", "").isdigit():
        form["plan_id"] = request.args["plan"]
    if request.method == "POST":
        form, errors = read_sub_form(request.form, creating=True)
        if not errors.get("username") and (
                q("SELECT 1 AS x FROM subscribers WHERE username = %s", (form["username"],), one=True)
                or q("SELECT 1 AS x FROM radcheck WHERE username = %s LIMIT 1", (form["username"],), one=True)):
            errors["username"] = "This username is already in use."
        if not errors:
            plan = get_plan(int(form["plan_id"])) if form["plan_id"] else None
            sid = insert(
                "INSERT INTO subscribers (username, full_name, phone, email, address, area, plan_id, "
                "status, expiry_date, static_ip, notes, created_by, identity, serial) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (form["username"], form["full_name"], form["phone"], form["email"], form["address"],
                 form["area"], plan["id"] if plan else None, form["status"], form["expiry"],
                 form["static_ip"] or None, form["notes"], g.me["username"], form["identity"], core.next_serial()))
            with get_db().cursor() as cur:
                radius.sync_subscriber(cur, form["username"], plan, form["status"], form["expiry"],
                                       form["static_ip"] or None, password=form["password"])
            core.remember_area(form["area"])
            core.notify("welcome", sid)
            audit("subscriber_created", form["username"])
            commit()
            flash("Subscriber %s created." % form["username"], "success")
            return redirect(url_for("subscriber_view", sid=sid))
        flash("Please fix the highlighted fields.", "error")
    return render_template("subscriber_form.html", mode="new", plans=plans, form=form, errors=errors, sub=None,
                           areas=core.area_names())


@app.route("/subscribers/<int:sid>/edit", methods=["GET", "POST"])
def subscriber_edit(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    plans = core.filter_plans(q("SELECT * FROM plans ORDER BY download_mbps, label"))
    errors = {}
    form = dict(sub)
    form["expiry_date"] = sub["expiry_date"].isoformat() if sub["expiry_date"] else ""
    form["plan_id"] = str(sub["plan_id"] or "")
    form["static_ip"] = sub["static_ip"] or ""
    form["password"] = ""
    if request.method == "POST":
        form, errors = read_sub_form(request.form, creating=False, sub_id=sid)
        form["username"] = sub["username"]
        if not errors:
            plan = get_plan(int(form["plan_id"])) if form["plan_id"] else None
            x("UPDATE subscribers SET full_name=%s, phone=%s, email=%s, address=%s, area=%s, plan_id=%s, "
              "status=%s, expiry_date=%s, static_ip=%s, notes=%s, identity=%s WHERE id=%s",
              (form["full_name"], form["phone"], form["email"], form["address"], form["area"],
               plan["id"] if plan else None, form["status"], form["expiry"],
               form["static_ip"] or None, form["notes"], form["identity"], sid))
            with get_db().cursor() as cur:
                radius.sync_subscriber(cur, sub["username"], plan, form["status"], form["expiry"],
                                       form["static_ip"] or None, password=form["password"] or None)
            core.remember_area(form["area"])
            audit("subscriber_updated", sub["username"],
                  "password changed" if form["password"] else "")
            commit()
            flash("Changes saved.", "success")
            return redirect(url_for("subscriber_view", sid=sid))
        flash("Please fix the highlighted fields.", "error")
    return render_template("subscriber_form.html", mode="edit", plans=plans, form=form, errors=errors, sub=sub,
                           areas=core.area_names())


def nas_secrets():
    return {r["nasname"]: r["secret"] for r in q("SELECT nasname, secret FROM nas WHERE coa_enabled = 1")}


def kick_user(username, limit=None):
    """Ask the routers to drop every online session of this user. Returns (kicked, failed)."""
    sessions = q("SELECT radacctid, nasipaddress, acctsessionid, framedipaddress FROM radacct "
                 "WHERE username = %s AND acctstoptime IS NULL", (username,))
    secrets_by_nas, port = nas_secrets(), cfg()["coa_port"]
    kicked = failed = 0
    for s in sessions:
        secret = secrets_by_nas.get(s["nasipaddress"])
        if not secret:
            failed += 1
            continue
        ok, msg = radius.disconnect_session(s["nasipaddress"], secret, port, username,
                                            s["acctsessionid"], s["framedipaddress"])
        radius.log_coa(get_db().cursor(), username, s["nasipaddress"], ok, msg, g.me["username"], cfg()["coa_log"])
        kicked += ok
        failed += (not ok)
    return kicked, failed


@app.route("/subscribers/<int:sid>")
def subscriber_view(sid):
    sub = q("SELECT s.*, p.label AS plan_label, p.name AS plan_name, p.download_mbps, p.upload_mbps, p.bandwidth_value, p.bandwidth_type, "
            "p.price, p.validity_days, p.duration, p.duration_type, p.quota_total_gb, p.policy_group FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
            "WHERE s.id = %s", (sid,), one=True) or abort(404)
    decorate([sub])
    user = sub["username"]
    pw = q("SELECT value FROM radcheck WHERE username = %s AND attribute = 'Cleartext-Password' LIMIT 1",
           (user,), one=True)
    sessions = q("SELECT radacctid, nasipaddress, framedipaddress, callingstationid, acctstarttime, "
                 "acctinputoctets, acctoutputoctets, acctsessionid FROM radacct "
                 "WHERE username = %s AND acctstoptime IS NULL ORDER BY acctstarttime DESC", (user,))
    history = q("SELECT acctstarttime, acctstoptime, acctsessiontime, acctinputoctets, acctoutputoctets, "
                "framedipaddress, callingstationid, acctterminatecause FROM radacct "
                "WHERE username = %s AND acctstoptime IS NOT NULL ORDER BY radacctid DESC LIMIT 12", (user,))
    today = date.today()
    month = q("SELECT COALESCE(SUM(acctinputoctets), 0) AS up, COALESCE(SUM(acctoutputoctets), 0) AS down, "
              "COUNT(*) AS sessions FROM radacct WHERE username = %s AND acctstarttime >= %s",
              (user, today.replace(day=1)), one=True)
    start = today - timedelta(days=29)
    daily = {r["d"]: r for r in q(
        "SELECT DATE(acctstarttime) AS d, SUM(acctinputoctets) AS up, SUM(acctoutputoctets) AS down "
        "FROM radacct WHERE username = %s AND acctstarttime >= %s GROUP BY DATE(acctstarttime)",
        (user, start))}
    days = [start + timedelta(days=i) for i in range(30)]
    usage = {"labels": [d.strftime("%d %b") for d in days],
             "series": [{"name": "Download", "values": [n(daily.get(d, {}).get("down")) for d in days]},
                        {"name": "Upload", "values": [n(daily.get(d, {}).get("up")) for d in days]}]}
    auth = q("SELECT reply, authdate FROM radpostauth WHERE username = %s ORDER BY id DESC LIMIT 6", (user,))
    pays = q("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 10", (sid,))
    plans = core.filter_plans(q("SELECT id, label, price, validity_days FROM plans ORDER BY download_mbps"))
    tickets = q("SELECT id, subject, status, priority, updated_at FROM tickets WHERE subscriber_id = %s "
                "ORDER BY id DESC LIMIT 5", (sid,))
    docs = q("SELECT id, title, category, created_at FROM documents WHERE subscriber_id = %s "
             "ORDER BY id DESC LIMIT 5", (sid,))
    notes = q("SELECT id, title, body, author, created_at FROM notes WHERE subscriber_id = %s "
              "AND (user_id = %s OR shared = 1) ORDER BY id DESC LIMIT 3", (sid, g.me["id"]))
    extra = mod_subview.context(sub, sessions)
    if sessions and sessions[0]["acctstarttime"]:
        extra["uptime"] = max(int((datetime.now() - sessions[0]["acctstarttime"]).total_seconds()), 0)
    return render_template("subscriber_view.html", sub=sub, x=extra, t=mod_subview.tabs(sub, sessions),
                           month_gb=(n(month["up"]) + n(month["down"])) / mod_subview.GB,
                           password=pw["value"] if pw else "",
                           sessions=sessions, history=history, month=month, usage=usage, auth=auth,
                           pays=pays, plans=plans, tickets=tickets, docs=docs, notes=notes,
                           open_renew=bool(request.args.get("renew")),
                           renew_amount=mod_policy.fee_total(mod_policy.seller_price(sub) or sub["price"], sub["plan_id"]),
                           charge=mod_subs.charge_info(sub))


@app.route("/subscribers/<int:sid>/renew", methods=["POST"])
def subscriber_renew(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    back = safe_next(request.form.get("next")) or url_for("subscriber_view", sid=sid)   # the list's Renew drawer returns to the list
    try:
        days = int(request.form.get("days", "0"))
        amount = round(float(request.form.get("amount", "0") or 0), 2)
        assert 1 <= days <= 3650 and 0 <= amount < 10 ** 8
    except (ValueError, AssertionError):
        flash("Enter the number of days (1-3650) and a valid amount.", "error")
        return redirect(back)
    method = request.form.get("method", "cash")
    why = mod_subs.charge_me(sub, "Renewal %s +%d days" % (sub["username"], days))
    if why:
        flash("Not renewed: %s. Add balance first. Nothing was changed." % why, "error")
        return redirect(back)
    if request.form.get("from_balance"):
        have = float(sub["balance"] or 0)
        if amount > have + 0.001:
            flash("The balance (%.2f) is lower than the amount (%.2f). Nothing was changed." % (have, amount), "error")
            return redirect(back)
        x("UPDATE subscribers SET balance = %s WHERE id = %s", (round(have - amount, 2), sid))
        insert("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
               (sid, -amount, round(have - amount, 2), "Renewal +%d days" % days, g.me["username"]))
        method = "balance"
    new_expiry = mod_subs.do_renew(sub, days, amount, method, request.form.get("note", ""))
    audit("renewed", sub["username"], "+%d days, %s" % (days, amount))
    commit()
    flash("Renewed until %s." % new_expiry.strftime("%d %b %Y"), "success")
    return redirect(back)


@app.route("/subscribers/<int:sid>/toggle", methods=["POST"])
def subscriber_toggle(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    status = "active" if sub["status"] == "suspended" else "suspended"
    x("UPDATE subscribers SET status = %s WHERE id = %s", (status, sid))
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], get_plan(sub["plan_id"]), status,
                               sub["expiry_date"], sub["static_ip"])
    audit("suspended" if status == "suspended" else "activated", sub["username"])
    commit()
    if status == "suspended":
        kicked, failed = kick_user(sub["username"])
        flash("%s is suspended%s." % (sub["username"],
              " and was disconnected" if kicked else (" (could not disconnect the live session)" if failed else "")),
              "success" if not failed else "warn")
    else:
        flash("%s is active again." % sub["username"], "success")
    return redirect(request.form.get("next") and safe_next(request.form["next"]) or url_for("subscriber_view", sid=sid))


@app.route("/subscribers/<int:sid>/kick", methods=["POST"])
def subscriber_kick(sid):
    sub = q("SELECT username FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    kicked, failed = kick_user(sub["username"])
    audit("disconnected", sub["username"], "%d ok, %d failed" % (kicked, failed))
    commit()
    if kicked and not failed:
        flash("Disconnected. The router will let the customer reconnect right away.", "success")
    elif failed:
        flash("Could not disconnect %d session(s). Check the router's incoming RADIUS setting." % failed, "error")
    else:
        flash("This customer has no live session.", "warn")
    return redirect(safe_next(request.form.get("next")) or url_for("subscriber_view", sid=sid))


@app.route("/subscribers/<int:sid>/delete", methods=["POST"])
@admin_required
def subscriber_delete(sid):
    sub = q("SELECT username FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    kick_user(sub["username"])
    with get_db().cursor() as cur:
        radius.remove_subscriber(cur, sub["username"])
    x("DELETE FROM subscribers WHERE id = %s", (sid,))
    audit("subscriber_deleted", sub["username"])
    commit()
    flash("%s was deleted. Accounting history is kept." % sub["username"], "success")
    return redirect(url_for("subscribers"))


MASS_TEXT = {"box_pop_no": 64, "box_pop_addr": 128, "mc_board": 64, "electricity": 64, "cable_type": 64,
             "uplink_port": 64, "fiber_code": 64, "fiber_color": 64}
CONN_TYPES = ("PPPoE", "Hotspot", "DHCP", "Static IP")


def _mass_settings_form(f):
    """Validate the Mass Service Settings form. Returns a dict, or an error string. Empty fields are left alone."""
    out = {"status": "", "expiry": None, "plan": None, "extra": {}}
    ex = out["extra"]
    st = f.get("profile_status", "")
    if st:
        if st not in ("active", "suspended"):
            return "Profile status must be Active or Suspended."
        out["status"] = st
    for field, col in (("sms_status", "sms_on"), ("email_status", "email_on"), ("auto_mac_status", "auto_mac")):
        v = f.get(field, "")
        if v:
            if v not in ("on", "off"):
                return "Choose Enabled or Disabled."
            ex[col] = 1 if v == "on" else 0
    ct = f.get("conn_type", "")
    if ct:
        if ct not in CONN_TYPES:
            return "Unknown connection type."
        ex["conn_type"] = ct
    if f.get("nas_id", "").isdigit():
        nas = q("SELECT nasname FROM nas WHERE id = %s", (int(f["nas_id"]),), one=True)
        if not nas:
            return "That NAS no longer exists."
        ex["nas_ip"] = nas["nasname"][:45]
    sp = f.get("salesperson", "").strip()
    if sp:
        names = core.my_downline()
        if (names is not None and sp not in names) or not q("SELECT id FROM panel_admins WHERE username = %s", (sp,), one=True):
            return "That salesperson is not available to you."
        ex["created_by"] = sp
    if f.get("plan_id", "").isdigit():
        plan = get_plan(int(f["plan_id"]))
        if not plan or not core.plan_allowed(plan["id"]):
            return "You cannot use that package."
        out["plan"] = plan
    if f.get("expiry", "").strip():
        try:
            out["expiry"] = date.fromisoformat(f["expiry"].strip())
        except ValueError:
            return "Expiration date must look like 2026-12-31."
    for field, col, kind in (("vol_total", "vol_total_gb", float), ("vol_used", "vol_used_gb", float),
                             ("sess_total", "sess_total_min", int), ("sess_used", "sess_used_min", int)):
        v = f.get(field, "").strip()
        if v:
            try:
                n = kind(v)
                assert 0 <= n < 10 ** 9
            except (ValueError, AssertionError):
                return "Volume and session values must be zero or more."
            ex[col] = n
    dt = f.get("disc_type", "")
    if dt:
        if dt not in ("fixed", "percent"):
            return "Choose Fixed or Percentage for the discount."
        ex["disc_type"] = dt
    dv = f.get("discount", "").strip()
    if dv:
        try:
            n = float(dv)
            assert 0 <= n < 10 ** 7 and (ex.get("disc_type", dt) != "percent" or n <= 100)
        except (ValueError, AssertionError):
            return "The discount must be zero or more (at most 100 for a percentage)."
        ex["discount"] = n
    for col, width in MASS_TEXT.items():
        v = f.get(col, "").strip()
        if v:
            ex[col] = v[:width]
    # Service Settings permissions: anything the person may not change is left out
    out["dropped"] = []
    for col, key, label in (("sms_on", "svc.sms", "SMS status"), ("email_on", "svc.email", "Email status"),
                            ("auto_mac", "svc.maclock", "Auto MAC lock"), ("conn_type", "svc.conn_type", "Connection type"),
                            ("nas_ip", "svc.nas", "NAS"), ("vol_total_gb", "svc.vol_total", "Total volume"),
                            ("vol_used_gb", "svc.vol_used", "Used volume"), ("sess_total_min", "svc.sess_total", "Total session"),
                            ("sess_used_min", "svc.sess_used", "Used session")):
        if col in ex and not core.can_fn(key):
            del ex[col]
            out["dropped"].append(label)
    if out["status"] and not core.can_fn("svc.profile_status"):
        out["status"] = ""
        out["dropped"].append("Profile status")
    if out["plan"] and not core.can_fn("svc.packages"):
        out["plan"] = None
        out["dropped"].append("Package")
    return out


@app.route("/subscribers/bulk", methods=["POST"])
def subscribers_bulk():
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()][:1000]
    action = request.form.get("action", "")
    back = safe_next(request.form.get("next")) or url_for("subscribers")
    if not ids or action not in ("suspend", "activate", "plan", "extend", "delete", "renew", "grace", "unbind", "settings"):
        flash("Select at least one subscriber and an action.", "warn")
        return redirect(back)
    if action == "delete" and g.me["role"] != "admin":
        abort(403)
    marks = ",".join(["%s"] * len(ids))
    subs = q("SELECT * FROM subscribers WHERE id IN (%s)" % marks, ids)
    names = core.my_downline()
    if names is not None:
        subs = [s for s in subs if s["created_by"] in names]
    plan = None
    if action == "plan" and not core.can_fn("svc.packages"):
        flash("You are not allowed to change packages here.", "error")
        return redirect(back)
    if action == "plan":
        plan = get_plan(int(request.form["plan_id"])) if request.form.get("plan_id", "").isdigit() else None
        if not plan or not core.plan_allowed(plan["id"]):
            flash("Choose a plan first.", "warn")
            return redirect(back)
    days = 0
    if action == "extend" and not core.can_fn("sub.expiry"):
        flash("You are not allowed to change expiry dates. Use Activation, which takes the package cost from your balance.", "error")
        return redirect(back)
    if action == "extend":
        try:
            days = int(request.form.get("days", "0"))
            assert 1 <= days <= 3650
        except (ValueError, AssertionError):
            flash("Enter the number of days to add (1-3650).", "error")
            return redirect(back)
    grace_days = 0
    if action == "grace":
        try:
            grace_days = int(request.form.get("days", "0"))
            assert 0 <= grace_days <= 90
        except (ValueError, AssertionError):
            flash("Grace days must be between 0 and 90.", "error")
            return redirect(back)
    mass = {}
    if action == "settings":
        mass = _mass_settings_form(request.form)
        if isinstance(mass, str):
            flash(mass, "error")
            return redirect(back)
        if mass["dropped"]:
            flash("Not changed, because you are not allowed to: %s." % ", ".join(mass["dropped"]), "warn")
        if mass["expiry"] and not core.can_fn("sub.expiry"):
            mass["expiry"] = None
            flash("The expiry date was not changed: you are not allowed to change expiry dates.", "warn")
        if not (mass["extra"] or mass["status"] or mass["expiry"] or mass["plan"]):
            flash("Nothing to change: choose or fill at least one setting.", "warn")
            return redirect(back)
    plans_by_id = {p["id"]: p for p in q("SELECT * FROM plans")}
    kicked_names, failures = [], 0
    if action == "renew":
        # Mass payment: each customer pays their own plan price and gets the plan's validity
        done = skipped = 0
        stopped = ""
        for s in subs:
            p = plans_by_id.get(s["plan_id"])
            if not p:
                skipped += 1
                continue
            why = mod_subs.charge_me(s, "Mass activation %s +%d days" % (s["username"], int(p["validity_days"] or 30)))
            if why:
                stopped = why
                break
            mod_subs.do_renew(s, int(p["validity_days"] or 30), float(p["price"] or 0), "cash", "Mass payment")
            done += 1
        audit("bulk_renew", "%d subscribers" % done)
        commit()
        flash("%d payment(s) recorded and renewed%s." % (done, ", %d skipped (no plan)" % skipped if skipped else ""),
              "success")
        if stopped:
            flash("Stopped: %s. Add balance first, then renew the rest." % stopped, "error")
        return redirect(back)
    with get_db().cursor() as cur:
        for s in subs:
            cur_plan = plans_by_id.get(s["plan_id"])
            status, expiry, pid = s["status"], s["expiry_date"], s["plan_id"]
            if action == "delete":
                radius.remove_subscriber(cur, s["username"])
                cur.execute("DELETE FROM subscribers WHERE id = %s", (s["id"],))
                continue
            if action == "suspend":
                status = "suspended"
                kicked_names.append(s["username"])
            elif action == "activate":
                status = "active"
            elif action == "plan":
                pid, cur_plan = plan["id"], plan
            elif action == "extend":
                expiry = max(expiry or date.today(), date.today()) + timedelta(days=days)
                status = "active"
            elif action == "grace":
                cur.execute("UPDATE subscribers SET grace_until = %s WHERE id = %s",
                            ((date.today() + timedelta(days=grace_days)) if grace_days else None, s["id"]))
            elif action == "unbind":
                cur.execute("UPDATE subscribers SET mac_bind = '' WHERE id = %s", (s["id"],))
            elif action == "settings":
                if mass["status"]:
                    status = mass["status"]
                    if status == "suspended" and s["status"] != "suspended":
                        kicked_names.append(s["username"])
                if mass["expiry"]:
                    expiry = mass["expiry"]
                if mass["plan"]:
                    pid, cur_plan = mass["plan"]["id"], mass["plan"]
                if mass["extra"]:
                    cols = list(mass["extra"])
                    cur.execute("UPDATE subscribers SET " + ", ".join("%s = %%s" % c for c in cols) + " WHERE id = %s",
                                [mass["extra"][c] for c in cols] + [s["id"]])
            cur.execute("UPDATE subscribers SET status = %s, expiry_date = %s, plan_id = %s WHERE id = %s",
                        (status, expiry, pid, s["id"]))
            radius.sync_subscriber(cur, s["username"], cur_plan, status, expiry, s["static_ip"])
    audit("bulk_" + action, "%d subscribers" % len(subs))
    commit()
    msg = "%d subscriber(s) updated." % len(subs)
    if (action == "suspend" and request.form.get("kick") == "1") or (action == "settings" and kicked_names):
        done = 0
        for name in kicked_names[:300]:
            k, f = kick_user(name)
            done += k
            failures += f
            if failures >= 5 and not done:
                break
        msg += " %d live session(s) disconnected." % done
    flash(msg, "success")
    return redirect(back)


EXPORT_COLUMNS = ["username", "password", "full_name", "phone", "email", "address", "area", "plan",
                  "status", "expiry_date", "static_ip", "created_at"]


@app.route("/subscribers/export.csv")
def subscribers_export():
    with_pw = request.args.get("passwords") == "1" and g.me["role"] == "admin"
    where, params = sub_filters(request.args)
    rows = q("SELECT s.username, rc.value AS password, s.full_name, s.phone, s.email, s.address, s.area, "
             "p.name AS plan, s.status, s.expiry_date, s.static_ip, s.created_at" + BASE_FROM +
             " LEFT JOIN radcheck rc ON rc.username = s.username AND rc.attribute = 'Cleartext-Password'" +
             where + " ORDER BY s.username", params)
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    cols = EXPORT_COLUMNS if with_pw else [c for c in EXPORT_COLUMNS if c != "password"]
    writer.writerow(cols)
    for r in rows:
        writer.writerow([r[c] if r[c] is not None else "" for c in cols])
    audit("export", "subscribers", "%d rows%s" % (len(rows), ", with passwords" if with_pw else ""))
    commit()
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=subscribers-%s.csv" % date.today().isoformat()})


# ====================================================================
#  CSV import
# ====================================================================
IMPORT_FIELDS = ["username", "password", "plan", "full_name", "phone", "email", "address", "area",
                 "expiry", "static_ip", "status"]


@app.route("/import/sample.csv")
def import_sample():
    body = ("username,password,plan,full_name,phone,address,area,expiry,static_ip,status\n"
            "rahim01,Pass1234,10Mbps,Abdur Rahim,01711000000,\"House 4, Road 2\",Pahartali,2026-12-31,,active\n"
            "karim02,Net5678,20Mbps,Karim Uddin,01811000000,Agrabad,Agrabad,2027-01-15,10.20.0.15,active\n")
    return Response(body, mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=subscribers-sample.csv"})


def _import_choices():
    """Dropdowns on the import page: the NAS and the salesperson (reseller / sub-reseller / retailer) for every imported row."""
    dl = core.my_downline()
    if dl is None:
        sales = q("SELECT username, full_name, role FROM panel_admins WHERE role <> 'admin' AND is_active = 1 ORDER BY username")
    else:
        sales = q("SELECT username, full_name, role FROM panel_admins WHERE username IN (%s) ORDER BY username"
                  % ",".join(["%s"] * len(dl)), list(dl)) if dl else []
    nas_rows = q("SELECT id, shortname, nasname FROM nas ORDER BY shortname") if core.can_fn("svc.nas") else []
    return {"sales": sales, "nas_rows": nas_rows}


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    report = None
    choices = _import_choices()
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file first.", "warn")
            return render_template("import.html", report=None, **choices)
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("The file is not UTF-8. In Excel use Save As > CSV UTF-8.", "error")
            return render_template("import.html", report=None, **choices)
        reader = csv.DictReader(io.StringIO(text))
        reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        if "username" not in reader.fieldnames:
            flash("The first row must contain a 'username' column. Download the sample to see the layout.", "error")
            return render_template("import.html", report=None, **choices)
        nas_ip, owner = "", ""
        if request.form.get("nas_id", "").isdigit():
            hit = next((n for n in choices["nas_rows"] if n["id"] == int(request.form["nas_id"])), None)
            if not hit:
                flash("That NAS is not available. Nothing was imported.", "error")
                return render_template("import.html", report=None, **choices)
            nas_ip = hit["nasname"][:45]
        if request.form.get("salesperson", "").strip():
            owner = request.form["salesperson"].strip()
            if owner not in {r["username"] for r in choices["sales"]}:
                flash("That salesperson is not available to you. Nothing was imported.", "error")
                return render_template("import.html", report=None, **choices)
        update = request.form.get("mode") == "update"
        plans = {p["name"].lower(): p for p in core.filter_plans(q("SELECT * FROM plans"))}
        existing = {r["username"] for r in q("SELECT username FROM subscribers")}
        report = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
        with get_db().cursor() as cur:
            for line, row in enumerate(reader, start=2):
                row = {k: (v or "").strip() for k, v in row.items() if k}
                user = row.get("username", "")
                try:
                    if not USERNAME_RE.match(user):
                        raise ValueError("invalid username")
                    pw = row.get("password", "")
                    if pw and not PASSWORD_RE.match(pw):
                        raise ValueError("password must be 4-64 characters without spaces")
                    plan = None
                    if row.get("plan"):
                        plan = plans.get(row["plan"].lower())
                        if not plan:
                            raise ValueError("unknown plan '%s'" % row["plan"])
                    expiry = parse_date(row.get("expiry", "")) if core.can_fn("sub.expiry") else None
                    ip = row.get("static_ip") or None
                    if ip:
                        ipaddress.IPv4Address(ip)
                    status = row.get("status", "active").lower()
                    status = status if status in ("active", "suspended") else "active"
                    if user in existing:
                        if not update:
                            report["skipped"] += 1
                            continue
                        cur.execute("SELECT * FROM subscribers WHERE username = %s", (user,))
                        old = cur.fetchone()
                        if not core.can_fn("svc.packages"):
                            plan = None
                        if not core.can_fn("svc.profile_status"):
                            status = old["status"]
                        if not core.can_fn("svc.conn_pw"):
                            pw = ""
                        if not core.can_fn("svc.ip"):
                            ip = None
                        plan = plan or plans.get(next((k for k, v in plans.items() if v["id"] == old["plan_id"]), ""))
                        fields = {k: row[k] for k in ("full_name", "phone", "email", "address", "area") if row.get(k)}
                        sets = ["expiry_date = %s", "static_ip = %s", "status = %s", "plan_id = %s"]
                        args = [expiry or old["expiry_date"], ip or old["static_ip"], status,
                                plan["id"] if plan else None]
                        if nas_ip:
                            sets.append("nas_ip = %s")
                            args.append(nas_ip)
                        if owner:
                            sets.append("created_by = %s")
                            args.append(owner)
                        for k, v in fields.items():
                            sets.append(k + " = %s")
                            args.append(v[:255])
                        cur.execute("UPDATE subscribers SET " + ", ".join(sets) + " WHERE id = %s", args + [old["id"]])
                        radius.sync_subscriber(cur, user, plan, status, expiry or old["expiry_date"],
                                               ip or old["static_ip"], password=pw or None)
                        report["updated"] += 1
                    else:
                        if not pw:
                            raise ValueError("password is required for new subscribers")
                        cur.execute(
                            "INSERT INTO subscribers (username, full_name, phone, email, address, area, plan_id, "
                            "status, expiry_date, static_ip, created_by, serial, nas_ip) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (user, row.get("full_name", "")[:128], row.get("phone", "")[:32],
                             row.get("email", "")[:128], row.get("address", "")[:255], row.get("area", "")[:64],
                             plan["id"] if plan else None, status,
                             expiry or (None if core.can_fn("sub.expiry") else date.today() - timedelta(days=1)),
                             ip, owner or g.me["username"], core.next_serial(), nas_ip))
                        radius.sync_subscriber(cur, user, plan, status,
                                               expiry or (None if core.can_fn("sub.expiry") else date.today() - timedelta(days=1)),
                                               ip, password=pw)
                        existing.add(user)
                        report["created"] += 1
                except Exception as exc:  # one bad row must not stop the import
                    report["failed"] += 1
                    if len(report["errors"]) < 40:
                        report["errors"].append((line, user or "-", str(exc)))
                if (report["created"] + report["updated"]) % 500 == 0:
                    get_db().commit()
        audit("import", upload.filename, ("%(created)d new, %(updated)d updated, %(failed)d failed" % report)
              + (", salesperson %s" % owner if owner else "") + (", NAS %s" % nas_ip if nas_ip else ""))
        commit()
        flash("Import finished: %(created)d created, %(updated)d updated, %(skipped)d skipped, "
              "%(failed)d failed." % report, "success" if not report["failed"] else "warn")
    return render_template("import.html", report=report, **choices)


# ====================================================================
#  live sessions
# ====================================================================
@app.route("/online")
def online():
    where, params = ["ra.acctstoptime IS NULL"], []
    text = request.args.get("q", "").strip()
    if text:
        like = text + "%"
        where.append("(ra.username LIKE %s OR ra.framedipaddress LIKE %s OR ra.callingstationid LIKE %s)")
        params += [like, like, like]
    nas_ip = request.args.get("nas", "")
    if nas_ip:
        where.append("ra.nasipaddress = %s")
        params.append(nas_ip)
    cond = " WHERE " + " AND ".join(where)
    total = q("SELECT COUNT(*) AS c FROM radacct ra" + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT ra.radacctid, ra.username, ra.nasipaddress, ra.framedipaddress, ra.callingstationid, "
             "ra.acctstarttime, ra.acctinputoctets, ra.acctoutputoctets, s.id AS sid, s.full_name, "
             "COALESCE(nas.shortname, ra.nasipaddress) AS nas_name, "
             "TIMESTAMPDIFF(SECOND, ra.acctstarttime, NOW()) AS uptime "
             "FROM radacct ra LEFT JOIN subscribers s ON s.username = ra.username "
             "LEFT JOIN nas ON nas.nasname = ra.nasipaddress" + cond +
             " ORDER BY ra.acctstarttime DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    nases = q("SELECT ra.nasipaddress AS ip, COALESCE(nas.shortname, ra.nasipaddress) AS name, COUNT(*) AS c "
              "FROM radacct ra LEFT JOIN nas ON nas.nasname = ra.nasipaddress WHERE ra.acctstoptime IS NULL "
              "GROUP BY ra.nasipaddress, nas.shortname ORDER BY c DESC")
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("online.html", rows=rows, pg=pg, nases=nases, keep=keep)


@app.route("/online/disconnect", methods=["POST"])
def online_disconnect():
    back = safe_next(request.form.get("next")) or url_for("online")
    sess = q("SELECT * FROM radacct WHERE radacctid = %s AND acctstoptime IS NULL",
             (request.form.get("id", "0"),), one=True)
    if not sess:
        flash("That session has already ended.", "warn")
        return redirect(back)
    secret = nas_secrets().get(sess["nasipaddress"])
    if not secret:
        ok, msg = False, "Router %s is not in the NAS list, so there is no shared secret to use." % sess["nasipaddress"]
    else:
        ok, msg = radius.disconnect_session(sess["nasipaddress"], secret, cfg()["coa_port"], sess["username"],
                                            sess["acctsessionid"], sess["framedipaddress"])
    radius.log_coa(get_db().cursor(), sess["username"], sess["nasipaddress"], ok, msg, g.me["username"], cfg()["coa_log"])
    if ok:
        audit("disconnected", sess["username"])
        flash("%s was disconnected." % sess["username"], "success")
    elif request.form.get("force") == "1" and g.me["role"] == "admin":
        x("UPDATE radacct SET acctstoptime = NOW(), acctterminatecause = 'Admin-Reset' "
          "WHERE radacctid = %s AND acctstoptime IS NULL", (sess["radacctid"],))
        audit("session_cleared", sess["username"], msg)
        flash("Session removed from the list. (%s)" % msg, "warn")
    else:
        flash("Could not disconnect %s: %s" % (sess["username"], msg), "error")
    commit()
    return redirect(back)


# ====================================================================
#  plans
# ====================================================================
@app.route("/plans/<int:pid>/delete", methods=["POST"])
def plan_delete(pid):
    plan = q("SELECT * FROM plans WHERE id = %s", (pid,), one=True) or abort(404)
    used = q("SELECT COUNT(*) AS c FROM subscribers WHERE plan_id = %s", (pid,), one=True)["c"]
    if used:
        flash("%d subscriber(s) still use %s. Move them to another plan first." % (used, plan["label"]), "error")
        return redirect(url_for("plans"))
    with get_db().cursor() as cur:
        radius.remove_plan(cur, plan["name"])
    x("DELETE FROM plan_fees WHERE plan_id = %s", (pid,))
    x("DELETE FROM user_plans WHERE plan_id = %s", (pid,))
    x("DELETE FROM plans WHERE id = %s", (pid,))
    audit("plan_deleted", plan["name"])
    commit()
    flash("Plan %s deleted." % plan["label"], "success")
    return redirect(url_for("plans"))


# ====================================================================
#  NAS / routers
# ====================================================================
def reload_radius():
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "freeradius"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


# ====================================================================
#  logs, reports
# ====================================================================
@app.route("/logs")
def logs():
    tab = "activity" if request.args.get("tab") == "activity" else "auth"
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    per, text = 50, request.args.get("q", "").strip()
    if tab == "auth":
        where, params = [], []
        if text:
            where.append("username LIKE %s")
            params.append(text + "%")
        result = request.args.get("result", "")
        if result in ("accept", "reject"):
            where.append("reply = %s")
            params.append("Access-Accept" if result == "accept" else "Access-Reject")
        rows = q("SELECT username, reply, authdate FROM radpostauth" +
                 ((" WHERE " + " AND ".join(where)) if where else "") +
                 " ORDER BY id DESC LIMIT %s OFFSET %s", params + [per + 1, (page - 1) * per])
    else:
        if text:
            rows = q("SELECT * FROM audit_log WHERE admin LIKE %s OR target LIKE %s OR action LIKE %s "
                     "ORDER BY id DESC LIMIT %s OFFSET %s", [text + "%"] * 3 + [per + 1, (page - 1) * per])
        else:
            rows = q("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s OFFSET %s", (per + 1, (page - 1) * per))
    has_next = len(rows) > per
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("logs.html", tab=tab, rows=rows[:per], page=page, has_next=has_next, keep=keep)




# ====================================================================
#  tools, settings, admins
# ====================================================================


def tail(path, lines=80):
    try:
        with open(path, errors="replace") as fh:
            return "".join(deque(fh, maxlen=lines))
    except OSError as exc:
        return "Cannot read %s (%s)" % (path, exc.strerror)


@app.route("/tools", methods=["GET", "POST"])
def tools():
    result = None
    if request.method == "POST":
        user, pw = request.form.get("username", "").strip(), request.form.get("password", "")
        if not user or not pw:
            flash("Enter a username and password to test.", "warn")
        else:
            ok, out = radius.test_login(user, pw, os.environ.get("RADIUS_LOCAL_SECRET", ""))
            result = {"ok": ok, "output": out, "user": user}
    services = [(label, svc, service_state(svc)) for label, svc in
                (("RADIUS server", "freeradius"), ("Database", "mariadb"), ("Web server", "nginx"),
                 ("This panel", "isp-panel"), ("Maintenance timer", "isp-panel-maint.timer"))]
    log = tail("/var/log/freeradius/radius.log")
    return render_template("tools.html", result=result, services=services, log=log, server=server_info())


@app.route("/tools/reload", methods=["POST"])
@admin_required
def tools_reload():
    ok, msg = reload_radius()
    audit("radius_reload", detail=msg)
    commit()
    flash("FreeRADIUS restarted." if ok else "Restart failed: %s" % (msg or "unknown error"),
          "success" if ok else "error")
    return redirect(url_for("tools"))


import mod_people  # noqa: E402,F401  (each module registers its pages with core.route)
import mod_accounting  # noqa: E402,F401
import mod_inventory  # noqa: E402,F401
import mod_network  # noqa: E402,F401
import mod_tickets  # noqa: E402,F401
import mod_messages  # noqa: E402,F401
import mod_settings  # noqa: E402,F401
import mod_notes  # noqa: E402,F401
import mod_documents  # noqa: E402,F401
import mod_reports  # noqa: E402,F401
import mod_policy  # noqa: E402,F401
import mod_subs  # noqa: E402,F401
import mod_plans  # noqa: E402,F401
import mod_nas  # noqa: E402,F401
import mod_olt  # noqa: E402,F401
import mod_subview  # noqa: E402,F401
import mod_notices  # noqa: E402,F401
import mod_backup  # noqa: E402,F401
import mod_diag  # noqa: E402,F401
import mod_topup  # noqa: E402,F401
import mod_portal  # noqa: E402,F401
import mod_ledger  # noqa: E402,F401
import mod_bkash_report  # noqa: E402,F401
import mod_btrc  # noqa: E402,F401
import mod_noc  # noqa: E402,F401

core.attach(app)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)
