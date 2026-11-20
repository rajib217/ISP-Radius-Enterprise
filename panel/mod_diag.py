"""Why was this customer refused? Reads the RADIUS tables and explains each rule that could say no."""
from datetime import datetime

from flask import redirect, render_template, request, url_for

import core
from core import cfg, q, route


def _expiry(value):
    try:
        return datetime.strptime(value, "%b %d %Y %H:%M:%S")
    except (TypeError, ValueError):
        try:
            return datetime.strptime(value, "%b %d %Y")
        except (TypeError, ValueError):
            return None


def diagnose(user):
    """List of (level, title, detail); level is ok, warn or bad."""
    out = []
    add = lambda lvl, title, detail="": out.append((lvl, title, detail))
    sub = q("SELECT s.*, p.name AS plan_name, p.label AS plan_label FROM subscribers s "
            "LEFT JOIN plans p ON p.id = s.plan_id WHERE s.username = %s", (user,), one=True)
    rc = q("SELECT attribute, op, value FROM radcheck WHERE username = %s", (user,))
    if not sub and not rc:
        like = q("SELECT username FROM subscribers WHERE username LIKE %s OR username LIKE %s LIMIT 8",
                 (user.split("@")[0] + "%", "%" + user.split("@")[0] + "%"))
        add("bad", "This exact username does not exist",
            "FreeRADIUS answers Refused for a name it cannot find. The name the router sends must match the panel letter for letter, "
            "including the part after @ and upper/lower case." +
            ((" Similar names in the panel: " + ", ".join(r["username"] for r in like) + ".") if like else ""))
        return sub, out
    if not sub:
        add("warn", "No subscriber record", "RADIUS rows exist for this name but the panel has no subscriber with it (it may have been deleted).")
    byattr = {}
    for r in rc:
        byattr.setdefault(r["attribute"], []).append(r)
    if "Cleartext-Password" in byattr:
        add("ok", "Password is set in RADIUS")
    else:
        add("bad", "No password in RADIUS", "FreeRADIUS has no Cleartext-Password row for this name, so it cannot check the login. Open the subscriber, press Password, and set one.")
    if "Auth-Type" in byattr and any(str(r["value"]).lower() == "reject" for r in byattr["Auth-Type"]):
        why = []
        if sub and sub.get("status") == "suspended":
            why.append("the profile is suspended and its package has no Next Disabled Action policy")
        if sub and sub.get("net_disabled"):
            why.append("Disable Net is on")
        if sub and sub.get("quota_hit"):
            why.append("the %s data quota is used up" % sub["quota_hit"])
        add("bad", "Login is blocked on purpose", "The account carries Auth-Type Reject: " + (", ".join(why) or "set by a suspension, quota or Disable Net") + ".")
    else:
        add("ok", "Not blocked by suspension, quota or Disable Net")
    exp = byattr.get("Expiration")
    if exp:
        when = _expiry(exp[0]["value"])
        if when and when < datetime.now():
            add("bad", "Package expired", "Expiration is %s. Renew it with Activation, or give a grace period." % exp[0]["value"])
        else:
            add("ok", "Not expired", "Expiration: %s" % exp[0]["value"])
    else:
        add("ok", "No expiry date", "This customer never expires.")
    mac = byattr.get("Calling-Station-Id")
    last = q("SELECT callingstationid, nasipaddress, acctstarttime FROM radacct WHERE username = %s ORDER BY acctstarttime DESC LIMIT 1", (user,), one=True)
    if mac:
        seen = (last or {}).get("callingstationid") or ""
        norm = lambda m: m.replace("-", ":").lower()
        if seen and norm(seen) != norm(mac[0]["value"]):
            add("bad", "Locked to another device", "Locked to %s, but the last connection came from %s. Use Reset MAC Address if the customer changed the router." % (mac[0]["value"], seen))
        else:
            add("warn", "MAC lock is on", "Only %s can log in. Reset MAC Address if the router was replaced." % mac[0]["value"])
    sim = byattr.get("Simultaneous-Use")
    limit = int(sim[0]["value"]) if sim and str(sim[0]["value"]).isdigit() else 0
    o = q("SELECT COUNT(*) AS c, MAX(acctupdatetime) AS upd FROM radacct WHERE username = %s AND acctstoptime IS NULL", (user,), one=True) or {}
    if limit and (o.get("c") or 0) >= limit:
        add("bad", "Already connected (%d open session%s, limit %d)" % (o["c"], "" if o["c"] == 1 else "s", limit),
            "FreeRADIUS refuses a new login while the old session is still open. If the customer is really offline the session is stale: "
            "use Disconnect on the subscriber page, or wait for the 5-minute clean-up. Last update: %s." % (o.get("upd") or "unknown"))
    else:
        add("ok", "Session limit is fine", "%s open session(s), limit %s." % (o.get("c") or 0, limit or "none"))
    groups = q("SELECT groupname, priority FROM radusergroup WHERE username = %s ORDER BY priority", (user,))
    if not groups:
        add("warn", "Not in any RADIUS group", "The customer would connect without a speed limit or pool. Press Sync on the subscriber page.")
    else:
        names = [g["groupname"] for g in groups]
        add("ok", "Groups: " + ", ".join(names))
        checks = q("SELECT groupname, attribute, op, value FROM radgroupcheck WHERE groupname IN (%s)" % ",".join(["%s"] * len(names)), names)
        if checks:
            add("warn", "Groups carry check rules", "These must all match or the login is refused: " +
                "; ".join("%s: %s %s %s" % (c["groupname"], c["attribute"], c["op"], c["value"]) for c in checks) + ".")
    if sub and not sub.get("plan_name"):
        add("warn", "No package assigned")
    if "@" in user:
        add("warn", "Username has a realm (@...)", "If FreeRADIUS strips the part after @ (suffix or realm module), the name it looks up will not match. Test with radtest using the full name.")
    return sub, out


@route("/diagnose", module="subscribers")
def diagnose_page():
    user = request.args.get("user", "").strip()[:64]
    sub, checks, attempts = None, [], []
    if user:
        sub, checks = diagnose(user)
        attempts = q("SELECT reply, authdate FROM radpostauth WHERE username = %s ORDER BY id DESC LIMIT 8", (user,))
    bad = [c for c in checks if c[0] == "bad"]
    return render_template("diagnose.html", user=user, sub=sub, checks=checks, attempts=attempts, bad=bad)
