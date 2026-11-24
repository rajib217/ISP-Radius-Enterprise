"""Packages: list, the full Add / Edit Package page, and saving every setting."""
import re

from flask import abort, flash, redirect, render_template, request, url_for

import core
import radius
from core import audit, cfg, commit, get_db, insert, q, route, x
from core import POOL_RE, PLAN_RE, RATE_RE
import mod_policy

DURATION_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
SERVICES = ["Internet", "IPTV", "Hotspot", "Other"]
ACTIONS = [("disconnect", "Disconnect / Terminate"), ("policy", "Switch to policy")]
WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
class Bad(ValueError):
    """A problem with the form that is worth showing to the user as written; field = the box to mark red."""

    def __init__(self, message, field=""):
        super().__init__(message)
        self.field = field


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@route("/plans", module="plans")
def plans():
    rows = q("SELECT p.*, (SELECT COUNT(*) FROM subscribers s WHERE s.plan_id = p.id) AS users "
             "FROM plans p ORDER BY p.download_mbps, p.label")
    for r in rows:
        r["rate"] = "" if r.get("speed_from_policy") else radius.rate_string(r)
    return render_template("plans.html", rows=rows)


def _form_page(plan, err="", fees_sel=None):
    users = q("SELECT id, username, full_name, role FROM panel_admins WHERE role <> 'admin' AND is_active = 1 "
              "ORDER BY role, username")
    sel_fees, sel_users = [], []
    if plan and plan.get("id"):
        sel_fees = [r["fee_id"] for r in q("SELECT fee_id FROM plan_fees WHERE plan_id = %s", (plan["id"],))]
        sel_users = [r["user_id"] for r in q("SELECT user_id FROM user_plans WHERE plan_id = %s", (plan["id"],))]
    if fees_sel is not None:
        sel_fees = fees_sel
    return render_template("plan_form.html", p=plan or {}, fees=q("SELECT id, name, kind, mode, value FROM fees "
                           "WHERE active = 1 ORDER BY name"), policies=mod_policy.policy_names(), users=users,
                           sel_fees=sel_fees, sel_users=sel_users, services=SERVICES, actions=ACTIONS,
                           weekdays=WEEKDAYS, roles=core.role_labels(), err=err)


# every box of the form, so a page with a mistake comes back with what was typed
FORM_KEYS = ("name", "description", "invoice_desc", "billing_type", "service_type", "bandwidth_value", "bandwidth_type",
             "price", "pool_name", "simultaneous_use", "duration", "duration_type", "invoice_day", "fixed_expire_day",
             "fixed_expire_acct_mode", "fixed_expire_time", "policy_group", "expired_policy", "disabled_policy",
             "quota_total_gb", "quota_daily_gb", "quota_weekly_gb", "quota_total_action", "quota_daily_action",
             "quota_weekly_action", "quota_total_policy", "quota_daily_policy", "quota_weekly_policy", "quota_weekly_reset",
             "quota_session_min")
FORM_SWITCHES = ("auto_renew", "require_balance", "auto_invoice", "fixed_expire_on", "fixed_expire_acct", "fixed_expire_time_on")


def _typed(f, pid):
    """The form as the person filled it in (for showing it again with the mistake marked)."""
    p = dict(q("SELECT * FROM plans WHERE id = %s", (pid,), one=True) or {}) if pid else {}
    for k in FORM_KEYS:
        if k in f:
            p[k] = f.get(k, "").strip()
    for k in FORM_SWITCHES:
        p[k] = 1 if f.get(k) == "1" else 0
    if not p.get("fixed_expire_time_on"):      # a switch turned off shows off again
        p["fixed_expire_time"] = ""
    if not p.get("fixed_expire_on"):
        p["fixed_expire_day"] = ""
    for k in ("quota_weekly_reset",):
        try:
            p[k] = int(p.get(k) or 0)
        except ValueError:
            p[k] = 0
    return p


@route("/plans/new", module="plans")
def plan_new():
    return _form_page(None)


@route("/plans/<int:pid>/edit", module="plans")
def plan_edit(pid):
    return _form_page(q("SELECT * FROM plans WHERE id = %s", (pid,), one=True) or abort(404))


def _num(f, key, low, high, cast=int, blank=None):
    raw = f.get(key, "").strip()
    if raw == "":
        return blank
    v = cast(raw)
    if not low <= v <= high:
        raise ValueError(key)
    return v


def _fees_typed(f):
    return sorted({int(v) for v in f.getlist("fees") if v.isdigit()})


def _price(raw):
    try:
        v = round(float(raw), 2)
    except (TypeError, ValueError):
        raise Bad("The price must be a number, e.g. 500.", "price")
    if v != v or not 0 <= v < 10 ** 8:          # not a number / negative / too big
        raise Bad("Check the price.", "price")
    return v


@route("/plans/save", module="plans", methods=["POST"])
def plan_save():
    f = request.form
    pid = int(f["id"]) if f.get("id", "").isdigit() else 0
    policies = set(mod_policy.policy_names())

    def policy(key, required=False):
        v = f.get(key, "").strip()
        if required and not v:
            raise Bad("Choose a Policy: it carries the speed limit and other RADIUS settings.", key)
        if v and v not in policies:
            raise Bad("Policy %s does not exist any more: pick another one." % v, key)
        return v

    def flag(key):
        return 1 if f.get(key) == "1" else 0

    try:
        name = f.get("name", "").strip()
        if not pid and not PLAN_RE.match(name):
            raise Bad("Name: 2-64 letters, numbers, dash or underscore (no spaces).", "name")
        description = f.get("description", "").strip()[:255]
        if not description:
            raise Bad("Enter a description.", "description")
        if not f.get("price", "").strip():
            raise Bad("Enter the price (type 0 for a free package).", "price")
        dtype = f.get("duration_type", "day")
        if dtype not in DURATION_DAYS:
            raise Bad("Choose the duration type.", "duration_type")
        duration = _num(f, "duration", 1, 3650)
        if duration is None:
            raise Bad("Enter the duration.", "duration")
        days = duration * DURATION_DAYS[dtype]
        if days > 3650:
            raise Bad("The duration is too long (10 years at most).", "duration")
        pool = f.get("pool_name", "").strip()
        if not POOL_RE.match(pool):
            raise Bad("Check the IP pool name.", "pool_name")
        bw_raw, bw_type = f.get("bandwidth_value", "").strip(), f.get("bandwidth_type", "").strip()
        bw_val = None
        if bw_raw:
            try:
                bw_val = float(bw_raw)
            except ValueError:
                raise Bad("The bandwidth must be a number, e.g. 20.", "bandwidth_value")
            if bw_type not in ("Kbps", "Mbps", "Gbps") or not 0 < bw_val < 10 ** 9:
                raise Bad("Check the Bandwidth and pick its type (Kbps, Mbps or Gbps).", "bandwidth_value")
        else:
            bw_type = ""
        # the list columns keep a rounded Mbps figure; the speed that reaches the router comes from the Policy
        up = down = max(1, -(-int(bw_val * {"Kbps": 1e3, "Mbps": 1e6, "Gbps": 1e9}[bw_type]) // 10 ** 6)) if bw_val else 1
        fixed_day = _num(f, "fixed_expire_day", 1, 31) if flag("fixed_expire_on") else None
        if flag("fixed_expire_on") and fixed_day is None:
            raise Bad("Enter the fixed expire day (1-31).", "fixed_expire_day")
        ftime = f.get("fixed_expire_time", "").strip() if flag("fixed_expire_time_on") else None
        if flag("fixed_expire_time_on") and not TIME_RE.match(ftime or ""):
            raise Bad("Pick the fixed expire time.", "fixed_expire_time")
        inv_day = _num(f, "invoice_day", 1, 31) if flag("auto_invoice") else None
        quotas = {}
        for kind in ("total", "daily", "weekly"):
            quotas["quota_%s_gb" % kind] = _num(f, "quota_%s_gb" % kind, 0, 10 ** 6, float)
            action = f.get("quota_%s_action" % kind, "disconnect")
            if action not in ("disconnect", "policy"):
                raise Bad("Check the quota actions.")
            quotas["quota_%s_action" % kind] = action
            quotas["quota_%s_policy" % kind] = policy("quota_%s_policy" % kind)
            if quotas["quota_%s_gb" % kind] and action == "policy" and not quotas["quota_%s_policy" % kind]:
                raise Bad("Choose the policy to switch to when the %s quota is used." % kind, "quota_%s_policy" % kind)
        quotas["quota_weekly_reset"] = _num(f, "quota_weekly_reset", 0, 6, int, 0)
        quotas["quota_session_min"] = _num(f, "quota_session_min", 0, 10 ** 6)
        quotas["quota_session_action"] = "disconnect"
        quotas["quota_session_policy"] = ""
        vals = {
            "label": description[:128] or name, "description": description,
            "invoice_desc": f.get("invoice_desc", "").strip()[:255],
            "billing_type": "postpaid" if f.get("billing_type") == "postpaid" else "prepaid",
            "service_type": f.get("service_type") if f.get("service_type") in SERVICES else "Internet",
            "download_mbps": down, "upload_mbps": up, "rate_custom": None, "speed_from_policy": 1,
            "bandwidth_value": bw_val, "bandwidth_type": bw_type, "pool_name": pool or None,
            "price": _price(f.get("price")), "validity_days": days, "duration": duration,
            "duration_type": dtype, "simultaneous_use": _num(f, "simultaneous_use", 1, 10, int, 1),
            "auto_renew": flag("auto_renew"), "require_balance": flag("require_balance"),
            "auto_invoice": flag("auto_invoice"), "invoice_day": inv_day,
            "fixed_expire_day": fixed_day, "fixed_expire_acct": flag("fixed_expire_acct"),
            "fixed_expire_acct_mode": "monthly" if f.get("fixed_expire_acct_mode") == "monthly" else "daily",
            "fixed_expire_time": ftime, "policy_group": policy("policy_group", True),
            "expired_policy": policy("expired_policy"), "disabled_policy": policy("disabled_policy"),
        }
        vals.update(quotas)
        if not 0 <= vals["price"] < 10 ** 8:
            raise Bad("Check the price.", "price")
    except Bad as exc:
        flash(str(exc), "error")
        return _form_page(_typed(f, pid), err=exc.field, fees_sel=_fees_typed(f))
    except (KeyError, ValueError) as exc:
        field = str(exc.args[0]) if exc.args and str(exc.args[0]) in FORM_KEYS else ""
        flash("Check the package fields: bandwidth, price and durations must be valid numbers.", "error")
        return _form_page(_typed(f, pid), err=field, fees_sel=_fees_typed(f))

    if pid:
        old = q("SELECT * FROM plans WHERE id = %s", (pid,), one=True) or abort(404)
        name = old["name"]
        cols = list(vals)
        x("UPDATE plans SET " + ", ".join("%s = %%s" % c for c in cols) + " WHERE id = %s",
          [vals[c] for c in cols] + [pid])
    else:
        if q("SELECT 1 AS x FROM plans WHERE name = %s", (name,), one=True):
            flash("A package named %s already exists." % name, "error")
            return _form_page(_typed(f, 0), err="name", fees_sel=_fees_typed(f))
        cols = ["name"] + list(vals)
        pid = insert("INSERT INTO plans (" + ", ".join(cols) + ") VALUES (" + ", ".join(["%s"] * len(cols)) + ")",
                     [name] + [vals[c] for c in vals])
    fee_ids = sorted({int(v) for v in f.getlist("fees") if v.isdigit()})
    x("DELETE FROM plan_fees WHERE plan_id = %s", (pid,))
    with get_db().cursor() as cur:
        if fee_ids:
            cur.executemany("INSERT INTO plan_fees (plan_id, fee_id) VALUES (%s, %s)", [(pid, i) for i in fee_ids])
        radius.sync_plan(cur, q("SELECT * FROM plans WHERE id = %s", (pid,), one=True), cfg()["interim_interval"])
    audit("plan_saved", name, "policy %s, %d days" % (vals["policy_group"], days))
    commit()
    flash("Package %s saved. Policy changes apply the next time each customer connects." % name, "success")
    return redirect(url_for("plans"))
