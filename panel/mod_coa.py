"""Network > CoA Request Log: two tabs.

  CoA requests      every disconnect request the panel sent to a router (table coa_log; Settings > Software >
                    CoA Request Log switches the logging).
  New PPPoE requests  logins the RADIUS server refused because the username is not a customer yet (radpostauth,
                    Access-Reject, username in neither radcheck nor subscribers): a new customer whose router is
                    already set up. "Add New Subscriber" opens the New subscriber form with the username, and the
                    password when the router sent it (PAP), already filled in.

Resellers and sub-resellers who may add customers see the requests that came from their own routers (NAS > Reseller,
theirs or their downline's); routers with no reseller stay with the admin and staff.

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
    """Staff: the Network module and the CoA Requests permission. Sellers: whenever they may add customers."""
    if core.my_downline() is not None:
        return core.can("subscribers", "edit") or (core.can("network") and core.can_fn("log.coa"))
    return core.can("network") and core.can_fn("log.coa")


def sees_new():
    """Anyone who may add customers. Staff see every router's requests, a seller only their own routers' (new_scope)."""
    return core.can("subscribers", "edit")


def new_scope():
    """(' AND ...', params): a seller only meets requests from routers assigned to them or their downline."""
    names = core.my_downline()
    if names is None:
        return "", []
    if not names:
        return " AND 1 = 0", []
    return (" AND p.nas_ip IN (SELECT nasname FROM nas WHERE seller IN (%s))" % ",".join(["%s"] * len(names)),
            list(names))


def request_row(rid):
    """One refused login of an unknown username, for the New subscriber form (None when it is not one)."""
    if not sees_new():
        return None
    sc, sp = new_scope()
    try:
        return q("SELECT p.id, p.username, p.pass, p.nas_ip, p.mac, p.authdate FROM radpostauth p WHERE p.id = %s AND "
                 + UNKNOWN + sc, [int(rid)] + sp, one=True)
    except (ValueError, TypeError):
        return None
    except Exception:                    # reason / nas_ip / mac columns arrive with update.sh
        return None


@route("/network/coa", module=("network", "subscribers"))   # mod_coa.allowed() decides; sellers may lack Network
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
        sc, sp = new_scope()
        where, params = UNKNOWN + sc, list(sp)
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
                hit = q("SELECT MAX(p.id) AS id FROM radpostauth p WHERE p.username = %s AND p.pass <> '' "
                        "AND p.reply = 'Access-Reject'" + sc, [r["username"]] + sp, one=True)
                r["rid"] = hit["id"] or r["rid"]
    new_count = None
    if sees_new():
        sc, sp = new_scope()
        new_count = q("SELECT COUNT(DISTINCT p.username) AS c FROM radpostauth p WHERE " + UNKNOWN + sc
                      + " AND p.authdate >= NOW() - INTERVAL 1 DAY", sp, one=True)["c"]
    return render_template("coa_log.html", tab=tab, rows=rows, pg=pg, text=text, sees_new=sees_new(),
                           seller=core.my_downline() is not None,
                           new_count=new_count, result=request.args.get("result", ""),
                           logging_on=core.cfg().get("coa_log") == "1",
                           keep={k: v for k, v in request.args.items() if k != "page"})
