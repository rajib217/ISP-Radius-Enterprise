"""Network > CoA Request Log: two tabs.

  CoA requests      every disconnect request the panel sent to a router (table coa_log; Settings > Software >
                    CoA Request Log switches the logging).
  New PPPoE requests  logins the RADIUS server refused because the username is not a customer yet (radpostauth,
                    Access-Reject, username in neither radcheck nor subscribers): a new customer whose router is
                    already set up. "Add New Subscriber" opens the New subscriber form with the username, and the
                    password when the router sent it (PAP), already filled in.

The attempted password is kept only for such unknown usernames (database trigger radpostauth_pass in
panel_schema.sql; existing customers' wrong passwords are never stored) and is wiped after 3 days (maintenance.py).
It never goes into a URL: the button carries the log row's id and the form reads the password from the database."""
from flask import abort, render_template, request

import core
from core import pager, q, route

UNKNOWN = ("p.reply = 'Access-Reject' AND p.username <> '' "
           "AND NOT EXISTS (SELECT 1 FROM radcheck rc WHERE rc.username = p.username) "
           "AND NOT EXISTS (SELECT 1 FROM subscribers s2 WHERE s2.username = p.username)")


def allowed():
    return core.can("network") and core.can_fn("log.coa")


def sees_new():
    """Unknown usernames belong to nobody yet: only staff who may add customers for the whole ISP see them."""
    return core.my_downline() is None and core.can("subscribers", "edit")


def request_row(rid):
    """One refused login of an unknown username, for the New subscriber form (None when it is not one)."""
    if not sees_new():
        return None
    try:
        return q("SELECT p.id, p.username, p.pass, p.nas_ip, p.mac, p.authdate FROM radpostauth p WHERE p.id = %s AND "
                 + UNKNOWN, (int(rid),), one=True)
    except (ValueError, TypeError):
        return None
    except Exception:                    # reason / nas_ip / mac columns arrive with update.sh
        return None


@route("/network/coa", module="network")
def coa_log():
    if not allowed():
        abort(403)
    tab = "new" if request.args.get("tab") == "new" and sees_new() else "coa"
    text = request.args.get("q", "").strip()
    rows, pg = [], None
    if tab == "coa":
        where, params = [], []
        names = core.my_downline()
        if names is not None:
            if not names:
                where.append("1 = 0")
            else:
                where.append("c.username IN (SELECT username FROM subscribers WHERE created_by IN (%s))"
                             % ",".join(["%s"] * len(names)))
                params += list(names)
        if text:
            where.append("(c.username LIKE %s OR c.nas_ip LIKE %s OR c.message LIKE %s)")
            params += ["%" + text + "%"] * 3
        if request.args.get("result") in ("ok", "fail"):
            where.append("c.ok = %s" % (1 if request.args["result"] == "ok" else 0))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = q("SELECT COUNT(*) AS c FROM coa_log c" + w, params, one=True)["c"]
        pg = pager(total)
        rows = q("SELECT c.id, c.username, c.nas_ip, c.ok, c.message, c.created_by, c.created_at, s.id AS sid "
                 "FROM coa_log c LEFT JOIN subscribers s ON s.username = c.username" + w +
                 " ORDER BY c.id DESC LIMIT %s OFFSET %s", params + [pg["per"], (pg["page"] - 1) * pg["per"]])
    else:
        where, params = UNKNOWN, []
        if text:
            where += " AND (p.username LIKE %s OR p.nas_ip LIKE %s OR p.mac LIKE %s)"
            params += ["%" + text + "%"] * 3
        total = q("SELECT COUNT(DISTINCT p.username) AS c FROM radpostauth p WHERE " + where, params, one=True)["c"]
        pg = pager(total)
        rows = q("SELECT p.username, COUNT(*) AS tries, MAX(p.authdate) AS last, MIN(p.authdate) AS first, MAX(p.id) AS rid, "
                 "SUBSTRING_INDEX(GROUP_CONCAT(p.nas_ip ORDER BY p.id DESC), ',', 1) AS nas_ip, "
                 "SUBSTRING_INDEX(GROUP_CONCAT(p.mac ORDER BY p.id DESC), ',', 1) AS mac, "
                 "MAX(p.pass <> '') AS has_pw "
                 "FROM radpostauth p WHERE " + where + " GROUP BY p.username ORDER BY last DESC LIMIT %s OFFSET %s",
                 params + [pg["per"], (pg["page"] - 1) * pg["per"]])
        # the newest row that still has the password, so the form gets it even if a later try came without
        for r in rows:
            if r["has_pw"]:
                hit = q("SELECT MAX(id) AS id FROM radpostauth WHERE username = %s AND pass <> '' AND reply = 'Access-Reject'",
                        (r["username"],), one=True)
                r["rid"] = hit["id"] or r["rid"]
    new_count = None
    if sees_new():
        new_count = q("SELECT COUNT(DISTINCT p.username) AS c FROM radpostauth p WHERE " + UNKNOWN
                      + " AND p.authdate >= NOW() - INTERVAL 1 DAY", one=True)["c"]
    return render_template("coa_log.html", tab=tab, rows=rows, pg=pg, text=text, sees_new=sees_new(),
                           new_count=new_count, result=request.args.get("result", ""),
                           logging_on=core.cfg().get("coa_log") == "1",
                           keep={k: v for k, v in request.args.items() if k != "page"})
