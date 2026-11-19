"""Package menu: Policy (RADIUS group attributes), Allocation (who may sell which plan), Tax / Extra fee."""
import re
from collections import OrderedDict

from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import admin_required, audit, commit, get_db, insert, q, route, x

GROUP_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
ATTR_RE = re.compile(r"^[A-Za-z0-9_:\-]{2,64}$")
OPS = [":=", "=", "==", "+=", "!=", ">", ">=", "<", "<=", "=~", "!~"]
TYPES = [("reply", "Reply (sent to the router)"), ("check", "Check (must match to log in)")]


def _plan_names():
    return {r["name"] for r in q("SELECT name FROM plans")}


# ===================================================================== Policy
@route("/policy", module="plans")
def policy():
    text = request.args.get("q", "").strip()
    rows = q("SELECT groupname, attribute, op, value, 'reply' AS ptype, id FROM radgroupreply "
             "UNION ALL SELECT groupname, attribute, op, value, 'check' AS ptype, id FROM radgroupcheck "
             "ORDER BY groupname, ptype, id")
    plans = _plan_names()
    groups = OrderedDict()
    for r in rows:
        gr = groups.setdefault(r["groupname"], {"name": r["groupname"], "rows": [], "plan": r["groupname"] in plans})
        gr["rows"].append(r)
    out = [gr for gr in groups.values() if not gr["plan"]]
    if text:
        low = text.lower()
        out = [gr for gr in out if low in gr["name"].lower()
               or any(low in str(r["attribute"]).lower() or low in str(r["value"]).lower() for r in gr["rows"])]
    members = {r["groupname"]: r["c"] for r in q("SELECT groupname, COUNT(*) AS c FROM radusergroup GROUP BY groupname")}
    used = {}
    for r in q("SELECT id, policy_group, expired_policy, disabled_policy FROM plans"):
        for k in {r["policy_group"], r["expired_policy"], r["disabled_policy"]}:
            if k:
                used[k] = used.get(k, 0) + 1
    subs = {}
    for r in q("SELECT p.policy_group AS g, COUNT(*) AS c FROM subscribers s JOIN plans p ON p.id = s.plan_id "
               "WHERE p.policy_group <> '' GROUP BY p.policy_group"):
        subs[r["g"]] = r["c"]
    for gr in out:
        gr["users"] = members.get(gr["name"], 0) + subs.get(gr["name"], 0)
        gr["packages"] = used.get(gr["name"], 0)
        gr["edit"] = [{"ptype": r["ptype"], "attribute": r["attribute"], "op": r["op"], "value": r["value"]}
                      for r in gr["rows"]]
    return render_template("policy.html", groups=out, ops=OPS, types=TYPES, total=len(out))


def _read_rows(form):
    ptypes, attrs, ops, vals = (form.getlist("ptype"), form.getlist("attribute"), form.getlist("op"),
                                form.getlist("value"))
    rows = []
    for i, a in enumerate(attrs):
        a = a.strip()
        if not a:
            continue
        pt = ptypes[i] if i < len(ptypes) else "reply"
        op = ops[i] if i < len(ops) else ":="
        v = (vals[i] if i < len(vals) else "").strip()
        if pt not in ("reply", "check") or not ATTR_RE.match(a) or op not in OPS or len(v) > 253:
            raise ValueError("Check attribute %d: the name, the operator and the value (253 characters max)." % (i + 1))
        rows.append((pt, a, op, v))
    if not rows:
        raise ValueError("Add at least one attribute.")
    return rows


@route("/policy/save", module="plans", methods=["POST"])
def policy_save():
    f = request.form
    name, orig = f.get("group", "").strip(), f.get("orig", "").strip()
    try:
        if not GROUP_RE.match(name):
            raise ValueError("Group name: 2-64 letters, numbers, dot, dash or underscore.")
        rows = _read_rows(f)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("policy"))
    plans = _plan_names()
    if name in plans or (orig and orig in plans):
        flash("%s belongs to a plan. Change it on the Packages page." % (orig or name), "error")
        return redirect(url_for("policy"))
    exists = q("SELECT 1 AS x FROM radgroupreply WHERE groupname = %s UNION SELECT 1 FROM radgroupcheck "
               "WHERE groupname = %s LIMIT 1", (name, name), one=True)
    if exists and name != orig:
        flash("A policy named %s already exists." % name, "error")
        return redirect(url_for("policy"))
    old = orig or name
    x("DELETE FROM radgroupreply WHERE groupname = %s", (old,))
    x("DELETE FROM radgroupcheck WHERE groupname = %s", (old,))
    with get_db().cursor() as cur:
        for pt, a, op, v in rows:
            table = "radgroupreply" if pt == "reply" else "radgroupcheck"
            cur.execute("INSERT INTO " + table + " (groupname, attribute, op, value) VALUES (%s, %s, %s, %s)",
                        (name, a, op, v))
    if orig and orig != name:
        x("UPDATE radusergroup SET groupname = %s WHERE groupname = %s", (name, orig))
    audit("policy_saved", name, "%d attributes" % len(rows))
    commit()
    flash("Policy %s saved. It applies the next time a customer connects." % name, "success")
    return redirect(url_for("policy"))


@route("/policy/delete", module="plans", methods=["POST"])
def policy_delete():
    name = request.form.get("group", "").strip()
    if not name or name in _plan_names():
        flash("Plan groups are removed by deleting the plan.", "error")
        return redirect(url_for("policy"))
    users = q("SELECT COUNT(*) AS c FROM radusergroup WHERE groupname = %s", (name,), one=True)["c"]
    if users:
        flash("%d customer%s still use %s. Move them to another plan first." % (users, "" if users == 1 else "s", name),
              "error")
        return redirect(url_for("policy"))
    x("DELETE FROM radgroupreply WHERE groupname = %s", (name,))
    x("DELETE FROM radgroupcheck WHERE groupname = %s", (name,))
    audit("policy_deleted", name)
    commit()
    flash("Policy %s deleted." % name, "success")
    return redirect(url_for("policy"))


@route("/policy/attribute/delete", module="plans", methods=["POST"])
def policy_attr_delete():
    f = request.form
    name, ptype = f.get("group", "").strip(), f.get("ptype", "")
    rid = f.get("id", "")
    if not name or name in _plan_names() or ptype not in ("reply", "check") or not rid.isdigit():
        abort(404)
    table = "radgroupreply" if ptype == "reply" else "radgroupcheck"
    x("DELETE FROM " + table + " WHERE id = %s AND groupname = %s", (int(rid), name))
    audit("policy_attribute_deleted", name, rid)
    commit()
    flash("Attribute removed from %s." % name, "success")
    return redirect(url_for("policy"))


# ===================================================================== Allocation
@route("/allocation", module="plans")
@admin_required
def allocation():
    users = q("SELECT id, username, full_name, role FROM panel_admins WHERE role <> 'admin' AND is_active = 1 "
              "ORDER BY role, username")
    plans = q("SELECT id, label, price FROM plans ORDER BY download_mbps, label")
    given = {}
    for r in q("SELECT user_id, plan_id FROM user_plans"):
        given.setdefault(r["user_id"], []).append(r["plan_id"])
    names = {p["id"]: p["label"] for p in plans}
    for u in users:
        u["plan_ids"] = given.get(u["id"], [])
        u["plan_names"] = [names[i] for i in u["plan_ids"] if i in names]
    return render_template("allocation.html", users=users, plans=plans, roles=core.role_labels())


@route("/allocation/save", module="plans", methods=["POST"])
@admin_required
def allocation_save():
    uid = request.form.get("user_id", "")
    u = q("SELECT id, username FROM panel_admins WHERE id = %s AND role <> 'admin'", (uid,), one=True) if uid.isdigit() else None
    if not u:
        abort(404)
    ids = sorted({int(p) for p in request.form.getlist("plans") if p.isdigit()})
    if ids:
        x("DELETE FROM user_plans WHERE user_id = %s AND plan_id NOT IN (" + ",".join(["%s"] * len(ids)) + ")",
          [u["id"]] + ids)
        with get_db().cursor() as cur:
            cur.executemany("INSERT IGNORE INTO user_plans (user_id, plan_id) VALUES (%s, %s)", [(u["id"], p) for p in ids])
    else:
        x("DELETE FROM user_plans WHERE user_id = %s", (u["id"],))
    audit("allocation_saved", u["username"], "%d plans" % len(ids))
    commit()
    flash("%s can now use %s." % (u["username"], "%d plan%s" % (len(ids), "" if len(ids) == 1 else "s") if ids else "every plan"),
          "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("allocation"))


# ===================================================================== Tax / Extra fee
@route("/fees", module="plans")
def fees():
    rows = q("SELECT * FROM fees ORDER BY kind, name")
    return render_template("fees.html", rows=rows)


@route("/fees/save", module="plans", methods=["POST"])
def fee_save():
    f = request.form
    fid = int(f["id"]) if f.get("id", "").isdigit() else 0
    try:
        name = f.get("name", "").strip()
        value = round(float(f.get("value", "0")), 2)
        kind = f.get("kind") if f.get("kind") in ("tax", "fee") else "fee"
        mode = f.get("mode") if f.get("mode") in ("percent", "fixed") else "fixed"
        assert 1 <= len(name) <= 64 and 0 <= value < 10 ** 7 and (mode == "fixed" or value <= 100)
    except (ValueError, AssertionError):
        flash("Enter a name and a value (a percentage cannot be more than 100).", "error")
        return redirect(url_for("fees"))
    vals = (name, kind, mode, value, 1 if f.get("active") == "1" else 0, f.get("note", "").strip()[:255])
    if fid:
        x("UPDATE fees SET name = %s, kind = %s, mode = %s, value = %s, active = %s, note = %s WHERE id = %s",
          vals + (fid,))
    else:
        insert("INSERT INTO fees (name, kind, mode, value, active, note) VALUES (%s, %s, %s, %s, %s, %s)", vals)
    audit("fee_saved", name)
    commit()
    flash("Saved.", "success")
    return redirect(url_for("fees"))


@route("/fees/<int:fid>/delete", module="plans", methods=["POST"])
def fee_delete(fid):
    row = q("SELECT name FROM fees WHERE id = %s", (fid,), one=True) or abort(404)
    x("DELETE FROM fees WHERE id = %s", (fid,))
    audit("fee_deleted", row["name"])
    commit()
    flash("Deleted.", "success")
    return redirect(url_for("fees"))


def policy_names():
    """Groups that are not plans: usable as an extra policy, an expired/disabled action or a quota action."""
    plans = _plan_names()
    names = {r["g"] for r in q("SELECT groupname AS g FROM radgroupreply UNION SELECT groupname FROM radgroupcheck")}
    return sorted(names - plans)


def seller_price(sub):
    """The price a partner set for this subscriber's package (their cost plus the subscriber profit), or None."""
    if not sub.get("created_by") or not sub.get("plan_id"):
        return None
    r = q("SELECT up.price, up.sub_profit FROM user_plans up JOIN panel_admins a ON a.id = up.user_id "
          "WHERE a.username = %s AND up.plan_id = %s", (sub["created_by"], sub["plan_id"]), one=True)
    if r and r.get("price") is not None:
        try:
            return float(r["price"]) + float(r["sub_profit"] or 0)
        except (TypeError, ValueError):
            return None
    return None


def fee_total(price, plan_id=None):
    """Price plus the active taxes / fees chosen on the plan, used as the suggested amount when renewing."""
    price = float(price or 0)
    total = price
    if not plan_id:
        return round(total, 2)
    for r in q("SELECT f.mode, f.value FROM fees f JOIN plan_fees pf ON pf.fee_id = f.id "
               "WHERE f.active = 1 AND pf.plan_id = %s", (plan_id,)):
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        total += price * v / 100.0 if r["mode"] == "percent" else v
    return round(total, 2)
