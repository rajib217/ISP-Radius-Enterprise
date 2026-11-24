"""Support tickets.

Who sees what
  * office (administrator, manager, support, accountant, technician): every ticket. The office queue is the tickets with
    to_office = 1; a reseller's ticket they handle themselves (to_office = 0) is shown but has no time limit or alert.
  * resellers / sub-resellers / retailers: only tickets of their own downline (tickets.seller), never internal notes,
    no assigning. They fix a ticket themselves or press "Send to office"; their own problems (balance, package, a
    whole area down) are "to the office" tickets with no customer.
  * customers: their own tickets on the portal (mod_support.py).

Pages
  /tickets              list, filters, time left, tick several -> assign / status / priority     Tickets > All tickets
  /tickets/new          find the customer by name, ID or phone; "solved on the phone"            Tickets > New ticket
  /tickets/<id>         conversation, ready replies, visit (arrived, materials), rating
  /tickets/board        New / Assigned / In progress / Done columns, drag a card to move it     Tickets > Board
  /tickets/report       time to fix, time limit kept, per technician, area, repeat customers    Tickets > Report
  /tickets/settings     time limits, area technicians, ready replies, customer messages         Tickets > Settings
  /outages              "work on the line in this area": portal notice and optional SMS         Tickets > Outages
"""
from datetime import date, datetime, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

import core
import ticketlib
from core import admin_required, audit, commit, get_db, insert, pager, q, route, x

CATEGORIES = [("connection", "No connection"), ("slow", "Slow speed"), ("billing", "Billing"),
              ("new", "New connection"), ("shift", "Shift / relocation"), ("hardware", "Hardware / ONU"),
              ("other", "Other")]
SELLER_CATEGORIES = [("balance", "Balance / payment"), ("package", "Package / price"), ("area", "Whole area down")]
PRIORITIES = [("low", "Low"), ("normal", "Normal"), ("high", "High"), ("urgent", "Urgent")]
STATUSES = [("open", "Open"), ("pending", "Pending"), ("resolved", "Resolved"), ("closed", "Closed")]
ALL_CATEGORIES = dict(CATEGORIES + SELLER_CATEGORIES)


# ===================================================================== who may do what
def seller():
    """A reseller / sub-reseller / retailer, or a reseller's own technician: limited to that downline's tickets."""
    return g.me["role"] in core.SELLER_ROLES or core.seller_boss() is not None


def manager():
    return g.me["role"] in ("admin", "manager")


def _scope(alias="t"):
    """(' AND t.seller IN (...)', params) for sellers, otherwise ('', [])."""
    names = core.my_downline()
    if names is None:
        return "", []
    return " AND %s.seller IN (%s)" % (alias, ",".join(["%s"] * len(names))), list(names)


def _staff():
    """People a ticket can be given to (office only)."""
    return q("SELECT a.id, a.username, a.full_name, a.role, a.phone FROM panel_admins a LEFT JOIN panel_admins p ON p.id = a.parent_id "
             "WHERE a.is_active = 1 AND a.role NOT IN ('reseller', 'subreseller', 'retailer') "
             "AND (p.id IS NULL OR p.role NOT IN ('reseller', 'subreseller', 'retailer')) "   # not a reseller's own technician
             "ORDER BY a.role = 'technician' DESC, a.username")


def own_techs():
    """Who a reseller may give a ticket they fix themselves to: their (downline's) own technicians."""
    return core.seller_techs() if seller() else []


def own_tech_id(raw):
    """The technician id from a form, if it is one of the reseller's own technicians (else None)."""
    return int(raw) if str(raw or "").isdigit() and int(raw) in {r["id"] for r in own_techs()} else None


def seller_of_sub(sub):
    """The reseller / sub-reseller / retailer a customer belongs to ('' for the office's own customers)."""
    who = (sub or {}).get("created_by") or ""
    if not who:
        return ""
    row = q("SELECT role FROM panel_admins WHERE username = %s", (who,), one=True)
    return who if row and row["role"] in core.SELLER_ROLES else ""


def load(tid):
    """One ticket with its customer and assignee, or 404 / 403 (a seller outside their downline)."""
    t = q("SELECT t.*, d.name AS dept_name, a.username AS assignee, a.full_name AS assignee_name, a.phone AS assignee_phone, "
          "s.id AS sid, s.full_name AS sub_name, s.phone AS sub_phone, s.area AS sub_area, s.address AS sub_address, "
          "s.lat, s.lng, s.created_by AS sub_owner FROM tickets t LEFT JOIN departments d ON d.id = t.department_id "
          "LEFT JOIN panel_admins a ON a.id = t.assigned_to LEFT JOIN subscribers s ON s.id = t.subscriber_id "
          "WHERE t.id = %s", (tid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and t["seller"] not in names:
        abort(403)
    return t


def time_left(t, now=None):
    """{'state': ok|soon|late|met|missed|none, 'mins': minutes left (negative = late)} for the list and the board."""
    if not t.get("due_at") or not t.get("to_office", 1):
        return {"state": "none", "mins": None}
    if t["status"] in ("resolved", "closed"):
        return {"state": "met" if t.get("closed_at") and t["closed_at"] <= t["due_at"] else "missed", "mins": None}
    mins = int(((t["due_at"] - (now or datetime.now())).total_seconds()) // 60)
    total = max(1, int((t["due_at"] - t["created_at"]).total_seconds() // 60)) if t.get("created_at") else 60
    return {"state": "late" if mins < 0 else ("soon" if mins <= max(30, total // 4) else "ok"), "mins": mins}


def left_text(mins):
    a = abs(int(mins or 0))
    txt = ("%dd %dh" % (a // 1440, a % 1440 // 60)) if a >= 1440 else (("%dh %dm" % (a // 60, a % 60)) if a >= 60 else "%dm" % a)
    return ("late " + txt) if (mins or 0) < 0 else (txt + " left")


def hours_text(mins):
    if mins is None:
        return "—"
    mins = float(mins)
    return ("%.1f h" % (mins / 60)) if mins >= 60 else ("%d min" % mins)


# ===================================================================== shared actions (also used by mod_field and mod_support)
def tech_text(admin_id):
    a = q("SELECT username, full_name, phone FROM panel_admins WHERE id = %s", (admin_id,), one=True) if admin_id else None
    if not a:
        return {}
    return {"tech": a["full_name"] or a["username"], "techphone": ("(%s)" % a["phone"]) if a["phone"] else ""}


def job_alert(tid, admin_id):
    """Phone notification (staff app) to the person a ticket was just given to; not when they took it themselves."""
    me = getattr(g, "me", None)
    if not admin_id or (me and me["id"] == admin_id):
        return 0
    r = q("SELECT t.id, t.subject, t.priority, t.category, s.full_name, s.username, s.area, s.address, a.role FROM tickets t "
          "LEFT JOIN subscribers s ON s.id = t.subscriber_id JOIN panel_admins a ON a.id = %s WHERE t.id = %s",
          (admin_id, tid), one=True)
    if not r:
        return 0
    import staffpush
    where = ", ".join(v for v in ((r["address"] or "")[:60], r["area"]) if v)
    body = " · ".join(v for v in ((r["full_name"] or r["username"] or ""), where, ALL_CATEGORIES.get(r["category"], "")) if v)
    urgent = r["priority"] in ("urgent", "high")
    title = "%sNew job #%d: %s" % ("URGENT · " if r["priority"] == "urgent" else "", tid, r["subject"][:70])
    with get_db().cursor() as cur:
        return staffpush.queue(cur, admin_id, title, body, "/jobs" if r["role"] == "technician" else "/tickets/%d" % tid,
                               key="job:%d:%d" % (tid, admin_id), urgent=urgent)


def watchers(seller_name):
    """Who hears about a ticket's news: every active administrator, plus the customer's reseller / sub-reseller / retailer
    and everyone above them up the chain (retailer -> sub-reseller -> reseller)."""
    ids = [r["id"] for r in q("SELECT id FROM panel_admins WHERE role = 'admin' AND is_active = 1")]
    row = q("SELECT id, role, parent_id, is_active FROM panel_admins WHERE username = %s", (seller_name,), one=True) if seller_name else None
    seen = set()
    while row and row["role"] in core.SELLER_ROLES and row["id"] not in seen:
        seen.add(row["id"])
        if row["is_active"]:
            ids.append(row["id"])
        row = q("SELECT id, role, parent_id, is_active FROM panel_admins WHERE id = %s", (row["parent_id"],), one=True)             if row["parent_id"] else None
    return list(dict.fromkeys(ids))


def watch_alert(tid, event, title, note=""):
    """Phone notification (staff app, Job alerts) about ticket #tid to its watchers, not to the person who did it.
    event: a short key (new, started, arrived, update, done ...) so one event is sent once per person."""
    r = q("SELECT t.id, t.subject, t.seller, t.category, s.full_name, s.username, s.area, a.full_name AS tech_name, "
          "a.username AS tech_user FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id "
          "LEFT JOIN panel_admins a ON a.id = t.assigned_to WHERE t.id = %s", (tid,), one=True)
    if not r:
        return 0
    me = getattr(g, "me", None)
    who = [i for i in watchers(r["seller"]) if not me or i != me["id"]]
    if not who:
        return 0
    import staffpush
    body = " · ".join(v for v in ((r["full_name"] or r["username"] or ""), r["area"] or "",
                                  ("Technician " + (r["tech_name"] or r["tech_user"])) if (r["tech_name"] or r["tech_user"]) else "",
                                  (note or "")[:140]) if v)
    head = "#%d %s: %s" % (tid, title, r["subject"][:70])
    n = 0
    with get_db().cursor() as cur:
        for aid in who:
            n += staffpush.queue(cur, aid, head, body, "/tickets/%d" % tid, key="tk:%d:%s:%d" % (tid, event, aid))
    return n


def assigned(t, admin_id):
    """After a ticket got a (new) person: ring their phone, tell the customer who is coming."""
    job_alert(t["id"], admin_id)
    if not admin_id or not t.get("subscriber_id"):
        return
    with get_db().cursor() as cur:
        ticketlib.notify(cur, core.cfg(), t, "ticket_assigned", by=g.me["username"], extra=tech_text(admin_id))


def done(t):
    """After a ticket was solved: SMS / app message to the customer with the rating link."""
    if not t.get("subscriber_id"):
        return
    try:
        link = url_for("portal_ticket", tid=t["id"], _external=True) if core.cfg().get("portal_on") == "1" else ""
    except Exception:
        link = ""
    me = getattr(g, "me", None)
    with get_db().cursor() as cur:
        ticketlib.notify(cur, core.cfg(), t, "ticket_done", by=me["username"] if me else "", link=link)


def create(sub, subject, category, priority, body, by, source="office", kind="customer", to_office=True,
           assign=None, dept=None, solved=False):
    """Insert a ticket (and its first message), work out its time limit and technician, tell the customer.
    Returns the new id. The caller commits."""
    s = core.cfg()
    seller_name = seller_of_sub(sub) if sub else ""
    if not seller_name and source == "seller":
        seller_name = by
    due = ticketlib.due_for(s, category, priority) if to_office and not solved else None
    if to_office and not assign and not solved and sub and s.get("ticket_auto_assign") == "1" and kind == "customer":
        with get_db().cursor() as cur:
            assign = ticketlib.pick_tech(cur, sub.get("area"))
    tid = insert("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, department_id, assigned_to, "
                 "created_by, due_at, source, kind, seller, to_office, closed_at) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (sub["id"] if sub else None, sub["username"] if sub else "", subject[:200], category, priority,
                  "resolved" if solved else "open", dept, assign, by, due, source, kind, seller_name,
                  1 if to_office else 0, datetime.now() if solved else None))
    if body:
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)", (tid, by, body[:5000]))
    if solved:
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
               (tid, by, "Solved on the phone."))
        x("UPDATE tickets SET first_reply_at = NOW() WHERE id = %s", (tid,))
    t = {"id": tid, "subscriber_id": sub["id"] if sub else None}
    if assign and not solved:
        job_alert(tid, assign)
    if sub and not solved:
        with get_db().cursor() as cur:
            ticketlib.notify(cur, s, t, "ticket_open", by=by)
            if assign:
                ticketlib.notify(cur, s, t, "ticket_assigned", by=by, extra=tech_text(assign))
    return tid


def find_customers(text, limit=8):
    """Customers matching a name, username, ID (RN05) or phone, limited to this user's own (sellers)."""
    text = (text or "").strip()
    if len(text) < 2:
        return []
    where, params = core.sub_filters({})
    where = (where + " AND " if where else " WHERE ")
    digits = "".join(ch for ch in text if ch.isdigit())
    conds, cp = ["s.username = %s", "s.username LIKE %s", "s.full_name LIKE %s"], [text, text + "%", "%" + text + "%"]
    if len(digits) >= 6:
        conds.append("REPLACE(REPLACE(s.phone, ' ', ''), '-', '') LIKE %s")
        cp.append("%" + digits[-10:] + "%")
    sid = core.parse_sub_code(text) if len(digits) < 8 else None
    if sid:
        conds.append("s.id = %s")
        cp.append(sid)
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.area, s.address, s.status, s.expiry_date, s.created_by "
             "FROM subscribers s" + where + "(" + " OR ".join(conds) + ") ORDER BY s.username = %s DESC, s.id DESC LIMIT %s",
             params + cp + [text, limit])
    if not rows:
        return []
    names = [r["username"] for r in rows]
    on = {r["username"] for r in q("SELECT DISTINCT username FROM radacct WHERE acctstoptime IS NULL AND username IN (" +
                                   ",".join(["%s"] * len(names)) + ")", names)}
    last = {}
    for r in q("SELECT t.subscriber_id, t.id, t.subject, t.status, t.created_at FROM tickets t WHERE t.subscriber_id IN (" +
               ",".join(["%s"] * len(rows)) + ") ORDER BY t.id DESC", [r["id"] for r in rows]):
        last.setdefault(r["subscriber_id"], r)
    for r in rows:
        r["online"] = r["username"] in on
        r["code"] = core.sub_code(r["id"])
        r["last_ticket"] = last.get(r["id"])
    return rows


def resolve_customer(text):
    rows = find_customers(text, limit=5)
    t = text.strip().lower()
    exact = [r for r in rows if r["username"].lower() == t or r["code"].lower() == t]
    if exact:
        return exact[0]
    return rows[0] if len(rows) == 1 else None


# ===================================================================== list
@route("/tickets", module="tickets")
def tickets():
    where, params = [], []
    sc, sp = _scope()
    text = request.args.get("q", "").strip()
    if text:
        if text.lstrip("#").isdigit():
            where.append("(t.id = %s OR t.username LIKE %s)")
            params += [int(text.lstrip("#")), "%" + text + "%"]
        else:
            where.append("(t.subject LIKE %s OR t.username LIKE %s)")
            params += ["%" + text + "%"] * 2
    status = request.args.get("status", "")
    if status == "open":
        where.append("t.status IN ('open', 'pending')")
    elif status in dict(STATUSES):
        where.append("t.status = %s")
        params.append(status)
    view = request.args.get("view", "")
    if view == "late":
        where.append("t.status IN ('open', 'pending') AND t.to_office = 1 AND t.due_at < NOW()")
    elif view == "free":
        where.append("t.status IN ('open', 'pending') AND t.to_office = 1 AND t.assigned_to IS NULL")
    elif view == "reseller":
        where.append("t.to_office = 0")
    elif view == "office":
        where.append("t.to_office = 1")
    elif view == "portal":
        where.append("t.source = 'portal'")
    if request.args.get("mine") == "1":
        where.append("t.assigned_to = %s")
        params.append(g.me["id"])
    if request.args.get("category") in ALL_CATEGORIES:
        where.append("t.category = %s")
        params.append(request.args["category"])
    if request.args.get("priority") in dict(PRIORITIES):
        where.append("t.priority = %s")
        params.append(request.args["priority"])
    if request.args.get("dept", "").isdigit():
        where.append("t.department_id = %s")
        params.append(int(request.args["dept"]))
    if request.args.get("area"):
        where.append("s.area = %s")
        params.append(request.args["area"])
    cond = " WHERE 1 = 1" + ("".join(" AND " + w for w in where)) + sc
    params += sp
    base = " FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id"
    total = q("SELECT COUNT(*) AS c" + base + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT t.*, a.username AS assignee, d.name AS dept_name, s.id AS sid, s.full_name AS sub_name, s.area AS sub_area"
             + base + " LEFT JOIN panel_admins a ON a.id = t.assigned_to LEFT JOIN departments d ON d.id = t.department_id" +
             cond + " ORDER BY t.status IN ('open', 'pending') DESC, (t.to_office = 1 AND t.due_at IS NOT NULL) DESC, t.due_at, "
             "t.updated_at DESC, t.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    now = datetime.now()
    for r in rows:
        r["left"] = time_left(r, now)
    counts = {r["status"]: r["c"] for r in q("SELECT t.status, COUNT(*) AS c FROM tickets t WHERE 1 = 1" + sc +
                                              " GROUP BY t.status", sp)}
    counts["open_all"] = counts.get("open", 0) + counts.get("pending", 0)
    extra = q("SELECT SUM(t.status IN ('open', 'pending') AND t.to_office = 1 AND t.due_at < NOW()) AS late, "
              "SUM(t.status IN ('open', 'pending') AND t.to_office = 1 AND t.assigned_to IS NULL) AS free, "
              "SUM(t.status IN ('open', 'pending') AND t.to_office = 0) AS reseller FROM tickets t WHERE 1 = 1" + sc, sp, one=True)
    counts.update({k: int(v or 0) for k, v in extra.items()})
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("tickets.html", rows=rows, pg=pg, counts=counts, depts=q("SELECT id, name FROM departments ORDER BY name"),
                           keep=keep, categories=CATEGORIES + (SELLER_CATEGORIES if seller() else []), all_cats=ALL_CATEGORIES,
                           priorities=PRIORITIES, staff=[] if seller() else _staff(), is_seller=seller(),
                           areas=core.area_names(), left_text=left_text)


@route("/tickets/bulk", module="tickets", methods=["POST"])
def tickets_bulk():
    """Several ticked tickets at once (office): give to someone, change the status or the priority."""
    if seller():
        abort(403)
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()][:500]
    back = redirect(core.safe_next(request.form.get("next")) or url_for("tickets"))
    act = request.form.get("action", "")
    if not ids:
        flash("Tick one or more tickets first.", "warn")
        return back
    mark = ",".join(["%s"] * len(ids))
    rows = q("SELECT id, subscriber_id, assigned_to, status FROM tickets WHERE id IN (" + mark + ")", ids)
    if act == "assign":
        who = request.form.get("assign_to", "")
        who = int(who) if who.isdigit() else None
        if who and not q("SELECT id FROM panel_admins WHERE id = %s AND is_active = 1", (who,), one=True):
            abort(400)
        x("UPDATE tickets SET assigned_to = %s, updated_at = NOW() WHERE id IN (" + mark + ")", [who] + ids)
        for t in rows:
            if who and t["assigned_to"] != who:
                assigned(t, who)
        msg = "given to %s" % (tech_text(who).get("tech") if who else "nobody")
    elif act in ("resolved", "closed", "open"):
        if act == "open":
            x("UPDATE tickets SET status = 'open', closed_at = NULL, updated_at = NOW() WHERE id IN (" + mark + ")", ids)
        else:
            x("UPDATE tickets SET status = %s, closed_at = COALESCE(closed_at, NOW()), updated_at = NOW() WHERE id IN (" +
              mark + ")", [act] + ids)
            for t in rows:
                if t["status"] in ("open", "pending") and act == "resolved":
                    done(t)
        msg = "marked %s" % act
    elif act in dict(PRIORITIES):
        x("UPDATE tickets SET priority = %s, updated_at = NOW() WHERE id IN (" + mark + ")", [act] + ids)
        s = core.cfg()
        for t in q("SELECT id, category, priority, created_at FROM tickets WHERE id IN (" + mark + ") AND to_office = 1", ids):
            x("UPDATE tickets SET due_at = %s WHERE id = %s", (ticketlib.due_for(s, t["category"], t["priority"], t["created_at"]), t["id"]))
        msg = "set to %s priority" % act
    else:
        flash("Pick what to do with the ticked tickets.", "warn")
        return back
    audit("tickets_bulk", ",".join("#%d" % i for i in ids)[:120], act)
    commit()
    flash("%d ticket%s %s." % (len(ids), "" if len(ids) == 1 else "s", msg), "success")
    return back


# ===================================================================== new
@route("/tickets/lookup", module="tickets")
def ticket_lookup():
    """Customers for the New ticket box (name, username, ID or phone): online now and their last ticket."""
    out = []
    for r in find_customers(request.args.get("q", "")):
        lt = r["last_ticket"]
        out.append({"id": r["id"], "code": r["code"], "username": r["username"], "name": r["full_name"] or "",
                    "phone": r["phone"] or "", "area": r["area"] or "", "online": r["online"], "status": r["status"],
                    "expiry": r["expiry_date"].strftime("%d %b %Y") if r["expiry_date"] else "",
                    "last": ({"id": lt["id"], "subject": lt["subject"], "status": lt["status"],
                              "when": lt["created_at"].strftime("%d %b %Y")} if lt else None)})
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


NEAR_METRES = 500           # Find Technicians: how close to the customer
NEAR_FRESH_MIN = 120        # ...and how recent the technician's position must be


@route("/tickets/nearby-techs", module="tickets")
def ticket_nearby_techs():
    """New ticket > Find Technicians: technicians whose phone was last seen within 500 m of the customer's map location."""
    allowed = {r["id"] for r in (own_techs() if seller() else _staff())}   # a reseller finds only their own technicians
    sub = resolve_customer(request.args.get("customer", ""))
    if not sub:
        return jsonify({"ok": False, "why": "Pick the customer first."})
    row = q("SELECT lat, lng, address, notes FROM subscribers WHERE id = %s", (sub["id"],), one=True)
    if row and (row["lat"] is None or row["lng"] is None):
        # the Location box is empty, but a Google Maps link or "23.81, 90.41" may sit in the address or the notes:
        # use it, and keep it as the customer's location from now on
        import mod_field
        for text in (row["address"], row["notes"]):
            try:
                pos = mod_field.parse_location(text) if text and any(ch.isdigit() for ch in text) else None
            except ValueError:
                pos = None
            if pos:
                x("UPDATE subscribers SET lat = %s, lng = %s WHERE id = %s AND lat IS NULL", (pos[0], pos[1], sub["id"]))
                audit("location_saved", sub["username"], "%s, %s (read from the address / notes)" % pos)
                commit()
                row["lat"], row["lng"] = pos
                break
    if not row or row["lat"] is None or row["lng"] is None:
        return jsonify({"ok": False, "why": "%s has no saved map location: on the customer's page, Address Information > Location "
                                            "says \"Not set\" (a map drawn from the address text is only a search, not a saved "
                                            "point). Set it there with Use my location or a Google Maps link, or assign by hand."
                                            % sub["username"]})
    out = []
    for r in q("SELECT a.id, a.username, a.full_name, a.phone, l.lat, l.lng, l.updated_at, "
               "(SELECT COUNT(*) FROM tickets t WHERE t.assigned_to = a.id AND t.status IN ('open', 'pending')) AS jobs "
               "FROM staff_locations l JOIN panel_admins a ON a.id = l.admin_id WHERE a.role = 'technician' AND a.is_active = 1 "
               "AND l.updated_at >= NOW() - INTERVAL %s MINUTE", (NEAR_FRESH_MIN,)):
        if r["id"] not in allowed:
            continue
        d = ticketlib.distance_m(row["lat"], row["lng"], r["lat"], r["lng"])
        if d <= NEAR_METRES:
            mins = int((datetime.now() - r["updated_at"]).total_seconds() // 60)
            out.append({"id": r["id"], "name": r["full_name"] or r["username"], "m": int(d), "jobs": int(r["jobs"] or 0),
                        "seen": "just now" if mins < 1 else ("%d min ago" % mins if mins < 60 else "%d h ago" % (mins // 60))})
    out.sort(key=lambda t: (t["m"], t["jobs"]))
    resp = jsonify({"ok": True, "customer": sub["username"], "radius": NEAR_METRES, "techs": out})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@route("/tickets/new", module="tickets", methods=["GET", "POST"])
def ticket_new():
    is_seller = seller()
    form = {"customer": "", "subject": "", "category": "connection", "priority": "normal", "body": "", "department_id": "",
            "assigned_to": "", "kind": "customer", "handler": "office", "solved": ""}
    if request.args.get("sub", "").isdigit():
        s = q("SELECT username FROM subscribers WHERE id = %s", (int(request.args["sub"]),), one=True)
        if s:
            form["customer"] = s["username"]
    if is_seller and request.args.get("kind") == "seller":
        form.update(kind="seller", category="balance")
    errors = {}
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in form}
        kind = "seller" if is_seller and form["kind"] == "seller" else "customer"
        sub = None
        if kind == "customer" and form["customer"]:
            sub = resolve_customer(form["customer"])
            if not sub:
                errors["customer"] = "No customer of yours matches this. Type the username, ID (like RN05) or phone, or leave it empty."
        elif kind == "customer" and is_seller:
            errors["customer"] = "Pick the customer this problem is about."
        if not 3 <= len(form["subject"]) <= 200:
            errors["subject"] = "Enter a subject of 3-200 characters."
        cats = dict(SELLER_CATEGORIES + [("other", "Other")]) if kind == "seller" else dict(CATEGORIES)
        if form["category"] not in cats:
            form["category"] = "other"
        if form["priority"] not in dict(PRIORITIES):
            form["priority"] = "normal"
        if len(form["body"]) > 5000:
            errors["body"] = "Too long (5000 characters max)."
        if not errors:
            if is_seller:
                to_office = kind == "seller" or form["handler"] != "me"
                dept = None
                who = None if to_office else own_tech_id(form["assigned_to"])   # "I will fix it": my own technician
                solved = False
            else:
                to_office = True
                dept = int(form["department_id"]) if form["department_id"].isdigit() else None
                who = int(form["assigned_to"]) if form["assigned_to"].isdigit() else None
                solved = form["solved"] == "1"
            tid = create(sub, form["subject"], form["category"], form["priority"], form["body"], g.me["username"],
                         source="seller" if is_seller else "office", kind=kind, to_office=to_office, assign=who,
                         dept=dept, solved=solved)
            audit("ticket_created", "#%d" % tid, form["subject"])
            if who and is_seller:
                assigned(load(tid), who)          # ring the reseller's technician, tell the customer who is coming
            commit()
            flash(("Ticket #%d saved as solved." if solved else "Ticket #%d created.") % tid, "success")
            return redirect(url_for("ticket_view", tid=tid))
        flash("Please fix the highlighted fields.", "error")
    return render_template("ticket_new.html", form=form, errors=errors, depts=q("SELECT id, name FROM departments ORDER BY name"),
                           staff=own_techs() if is_seller else _staff(), categories=CATEGORIES, seller_categories=SELLER_CATEGORIES,
                           priorities=PRIORITIES, is_seller=is_seller)


# ===================================================================== one ticket
@route("/tickets/<int:tid>", module="tickets")
def ticket_view(tid):
    t = load(tid)
    is_seller = seller()
    replies = q("SELECT * FROM ticket_replies WHERE ticket_id = %s" + (" AND internal = 0" if is_seller else "") +
                " ORDER BY id", (tid,))
    try:
        mats = q("SELECT m.qty, m.by_user, m.created_at, i.name, i.unit FROM ticket_materials m "
                 "JOIN inventory_items i ON i.id = m.item_id WHERE m.ticket_id = %s ORDER BY m.id", (tid,))
    except Exception:
        mats = []
    dist = None
    if t["arrived_lat"] is not None and t["lat"] is not None:
        dist = int(ticketlib.distance_m(t["arrived_lat"], t["arrived_lng"], t["lat"], t["lng"]))
    with get_db().cursor() as cur:
        outs = ticketlib.active_outages(cur, t["sub_area"] or "") if t["sid"] else []
    canned = q("SELECT id, title, body FROM ticket_canned ORDER BY title") if core.can("tickets", "edit") else []
    return render_template("ticket_view.html", t=t, replies=replies, depts=q("SELECT id, name FROM departments ORDER BY name"),
                           staff=own_techs() if is_seller else _staff(), categories=CATEGORIES, all_cats=ALL_CATEGORIES,
                           priorities=PRIORITIES, statuses=STATUSES, is_seller=is_seller, left=time_left(t),
                           left_text=left_text, mats=mats, dist=dist, outages=outs, canned=canned,
                           can_status=(not is_seller) or not t["to_office"])


@route("/tickets/<int:tid>/reply", module="tickets", methods=["POST"])
def ticket_reply(tid):
    t = load(tid)
    is_seller = seller()
    body = request.form.get("body", "").strip()
    status = request.form.get("status", "")
    internal = 1 if request.form.get("internal") == "1" and not is_seller else 0
    if is_seller and t["to_office"]:
        status = ""                       # the office runs it now: the reseller can still write
    if body:
        if len(body) > 5000:
            flash("The reply is too long (5000 characters max).", "error")
            return redirect(url_for("ticket_view", tid=tid))
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, %s)",
               (tid, g.me["username"], body, internal))
        if not internal:
            x("UPDATE tickets SET first_reply_at = COALESCE(first_reply_at, NOW()) WHERE id = %s", (tid,))
            if t["subscriber_id"] and status not in ("resolved", "closed"):
                with get_db().cursor() as cur:
                    ticketlib.notify(cur, core.cfg(), t, "ticket_reply", by=g.me["username"], extra={"text": body})
    if status in dict(STATUSES):
        _set_status(tid, status)
        if status == "resolved" and t["status"] in ("open", "pending"):
            done(t)
    elif not body:
        flash("Write a reply or pick a new status.", "warn")
        return redirect(url_for("ticket_view", tid=tid))
    x("UPDATE tickets SET updated_at = NOW() WHERE id = %s", (tid,))
    audit("ticket_reply", "#%d" % tid, status)
    commit()
    flash("Ticket updated.", "success")
    return redirect(url_for("ticket_view", tid=tid))


def _set_status(tid, status):
    if status in ("resolved", "closed"):
        x("UPDATE tickets SET status = %s, closed_at = COALESCE(closed_at, NOW()) WHERE id = %s", (status, tid))
    else:
        x("UPDATE tickets SET status = %s, closed_at = NULL WHERE id = %s", (status, tid))


@route("/tickets/<int:tid>/update", module="tickets", methods=["POST"])
def ticket_update(tid):
    if seller():
        abort(403)
    t = load(tid)
    f = request.form
    dept = int(f["department_id"]) if f.get("department_id", "").isdigit() else None
    who = int(f["assigned_to"]) if f.get("assigned_to", "").isdigit() else None
    category = f.get("category") if f.get("category") in ALL_CATEGORIES else "other"
    priority = f.get("priority") if f.get("priority") in dict(PRIORITIES) else "normal"
    due = t["due_at"]
    if t["to_office"] and (category != t["category"] or priority != t["priority"]):
        due = ticketlib.due_for(core.cfg(), category, priority, t["created_at"])
    x("UPDATE tickets SET department_id = %s, assigned_to = %s, category = %s, priority = %s, due_at = %s, updated_at = NOW() "
      "WHERE id = %s", (dept, who, category, priority, due, tid))
    if who and who != t["assigned_to"]:
        assigned(t, who)
    audit("ticket_updated", "#%d" % tid)
    commit()
    flash("Ticket details saved.", "success")
    return redirect(url_for("ticket_view", tid=tid))


@route("/tickets/<int:tid>/assign-own", module="tickets", methods=["POST"])
def ticket_assign_own(tid):
    """A ticket the reseller fixes themselves: give it to (or take it from) one of their own technicians."""
    if not seller():
        abort(403)
    t = load(tid)
    if t["to_office"] or t["status"] not in ("open", "pending"):
        flash("Only an open ticket you handle yourself can be given to your technician.", "warn")
        return redirect(url_for("ticket_view", tid=tid))
    raw = request.form.get("assigned_to", "")
    who = own_tech_id(raw)
    if raw and not who:
        abort(403)
    x("UPDATE tickets SET assigned_to = %s, updated_at = NOW() WHERE id = %s", (who, tid))
    if who and who != t["assigned_to"]:
        assigned(t, who)
    audit("ticket_assigned", "#%d" % tid, str(who or "nobody"))
    commit()
    flash("Ticket #%d given to your technician." % tid if who else "Ticket #%d has no technician now." % tid, "success")
    return redirect(url_for("ticket_view", tid=tid))


@route("/tickets/<int:tid>/to-office", module="tickets", methods=["POST"])
def ticket_to_office(tid):
    """A reseller cannot fix it: the office takes over (time limit and technician start now)."""
    t = load(tid)
    if t["to_office"]:
        flash("The office already has this ticket.", "warn")
        return redirect(url_for("ticket_view", tid=tid))
    s = core.cfg()
    who = None
    if t["assigned_to"] and t["assigned_to"] in {r["id"] for r in own_techs()}:
        # the reseller's own technician does not work office tickets: the office picks its own person
        x("UPDATE tickets SET assigned_to = NULL WHERE id = %s", (tid,))
        t["assigned_to"] = None
    if t["sid"] and s.get("ticket_auto_assign") == "1" and not t["assigned_to"]:
        with get_db().cursor() as cur:
            who = ticketlib.pick_tech(cur, t["sub_area"])
    x("UPDATE tickets SET to_office = 1, due_at = %s, assigned_to = COALESCE(assigned_to, %s), alerted = 0, updated_at = NOW() "
      "WHERE id = %s", (ticketlib.due_for(s, t["category"], t["priority"]), who, tid))
    note = request.form.get("note", "").strip()[:1000]
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
           (tid, g.me["username"], "Sent to the office." + ((" " + note) if note else "")))
    if who:
        assigned(t, who)
    audit("ticket_to_office", "#%d" % tid)
    commit()
    flash("Ticket #%d sent to the office. You can follow it here." % tid, "success")
    return redirect(url_for("ticket_view", tid=tid))


@route("/tickets/<int:tid>/delete", module="tickets", methods=["POST"])
@admin_required
def ticket_delete(tid):
    q("SELECT id FROM tickets WHERE id = %s", (tid,), one=True) or abort(404)
    with get_db().cursor() as cur:
        ticketlib.save_work(cur, "t.id = %s", (tid,))     # the technician's work and rating stay in their record
    x("DELETE FROM ticket_replies WHERE ticket_id = %s", (tid,))
    x("DELETE FROM tickets WHERE id = %s", (tid,))
    audit("ticket_deleted", "#%d" % tid)
    commit()
    flash("Ticket #%d deleted." % tid, "success")
    return redirect(url_for("tickets"))


# ===================================================================== board
BOARD_COLS = [("new", "New", "Nobody on it yet"), ("assigned", "Assigned", "Given to someone, not started"),
              ("working", "In progress", "Being worked on"), ("done", "Done", "Solved in the last 3 days")]


@route("/tickets/board", module="tickets")
def ticket_board():
    if seller():
        return redirect(url_for("tickets"))
    where, params = ["t.to_office = 1"], []
    if request.args.get("area"):
        where.append("s.area = %s")
        params.append(request.args["area"])
    if request.args.get("who", "").isdigit():
        where.append("t.assigned_to = %s")
        params.append(int(request.args["who"]))
    rows = q("SELECT t.*, a.username AS assignee, a.full_name AS assignee_name, s.full_name AS sub_name, s.area AS sub_area "
             "FROM tickets t LEFT JOIN panel_admins a ON a.id = t.assigned_to LEFT JOIN subscribers s ON s.id = t.subscriber_id "
             "WHERE " + " AND ".join(where) + " AND (t.status IN ('open', 'pending') OR t.closed_at >= NOW() - INTERVAL 3 DAY) "
             "ORDER BY FIELD(t.priority, 'urgent', 'high', 'normal', 'low'), t.due_at IS NULL, t.due_at, t.id LIMIT 400", params)
    cols = {k: [] for k, _l, _h in BOARD_COLS}
    now = datetime.now()
    for t in rows:
        t["left"] = time_left(t, now)
        col = ("done" if t["status"] in ("resolved", "closed") else "working" if t["status"] == "pending"
               else "assigned" if t["assigned_to"] else "new")
        cols[col].append(t)
    return render_template("ticket_board.html", cols=cols, board_cols=BOARD_COLS, staff=_staff(), areas=core.area_names(),
                           all_cats=ALL_CATEGORIES, left_text=left_text)


@route("/tickets/<int:tid>/move", module="tickets", methods=["POST"])
def ticket_move(tid):
    """A card dropped on another board column (or the Assign box on the card)."""
    if seller():
        abort(403)
    t = load(tid)
    col = request.form.get("col", "")
    who = request.form.get("assign_to", "")
    back = redirect(core.safe_next(request.form.get("next")) or url_for("ticket_board"))
    has_who = bool(t["assigned_to"])
    if who.isdigit():
        who = int(who)
        if not q("SELECT id FROM panel_admins WHERE id = %s AND is_active = 1", (who,), one=True):
            abort(400)
        x("UPDATE tickets SET assigned_to = %s, updated_at = NOW() WHERE id = %s", (who, tid))
        if who != t["assigned_to"]:
            assigned(t, who)
        has_who = True
    if col == "new":
        x("UPDATE tickets SET assigned_to = NULL, status = 'open', closed_at = NULL, updated_at = NOW() WHERE id = %s", (tid,))
    elif col == "assigned":
        if not has_who:
            flash("Pick who gets ticket #%d (the box on the card), then it moves to Assigned." % tid, "warn")
            return back
        x("UPDATE tickets SET status = 'open', closed_at = NULL, updated_at = NOW() WHERE id = %s", (tid,))
    elif col == "working":
        x("UPDATE tickets SET status = 'pending', closed_at = NULL, assigned_to = COALESCE(assigned_to, %s), updated_at = NOW() "
          "WHERE id = %s", (g.me["id"], tid))
    elif col == "done":
        if t["status"] in ("open", "pending"):
            insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 1)",
                   (tid, g.me["username"], "Marked done on the board."))
            _set_status(tid, "resolved")
            done(t)
    elif col:
        abort(400)
    audit("ticket_moved", "#%d" % tid, col or "assign")
    commit()
    return back


# ===================================================================== report
# Every ticket the report counts: the live ones plus the removed ones kept in staff_work_log (deleted by hand or by
# the monthly clean-up), so the numbers do not change when tickets are removed.
def _u(col):
    """One collation for a text column on both sides of the UNION (tickets and staff_work_log may differ)."""
    return "CONVERT(%s USING utf8mb4) COLLATE utf8mb4_unicode_ci" % col


REPORT_SRC = (
    "(SELECT t.id, t.subscriber_id, " + _u("t.category") + " AS category, " + _u("t.status") + " AS status, t.created_at, "
    "t.closed_at, t.first_reply_at, t.due_at, t.rating, " + _u("t.source") + " AS source, t.to_office, t.assigned_to, t.arrived_at, "
    + _u("COALESCE(NULLIF(s.area, ''), '(no area)')") + " AS area FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id "
    "UNION ALL SELECT w.ticket_id, w.subscriber_id, " + _u("w.category") + ", " + _u("w.status") + ", w.created_at, w.closed_at, "
    "w.first_reply_at, COALESCE(w.raw_due_at, w.due_at), w.rating, " + _u("w.source") + ", w.to_office, NULLIF(w.admin_id, 0), "
    "w.arrived_at, " + _u("COALESCE(NULLIF(w.area, ''), '(no area)')") + " FROM staff_work_log w "
    "WHERE NOT EXISTS (SELECT 1 FROM tickets x WHERE x.id = w.ticket_id)) r")
REPORT_PRESETS = ((0, "Today"), (6, "7 days"), (29, "30 days"), (89, "90 days"))


@route("/tickets/report", module="tickets")
def ticket_report():
    if seller():
        abort(403)
    start, end = core.date_range(29)
    if request.args.get("days", type=int) in (7, 30) and not request.args.get("from"):     # old links
        start = end - timedelta(days=request.args.get("days", type=int) - 1)
    lo, hi = datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())
    rng = (lo, hi)
    try:
        q("SELECT 1 FROM staff_work_log LIMIT 1")
        src = REPORT_SRC
    except Exception:                    # before update.sh added the table: live tickets only
        src = REPORT_SRC.split(" UNION ALL ")[0] + ") r"
    tot = q("SELECT COUNT(*) AS opened, SUM(r.status IN ('resolved', 'closed')) AS closed, "
            "SUM(r.status IN ('open', 'pending')) AS still_open, "
            "AVG(CASE WHEN r.closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, r.created_at, r.closed_at) END) AS fix_min, "
            "AVG(CASE WHEN r.first_reply_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, r.created_at, r.first_reply_at) END) AS reply_min, "
            "SUM(r.due_at IS NOT NULL AND r.closed_at IS NOT NULL AND r.closed_at <= r.due_at) AS sla_met, "
            "SUM(r.due_at IS NOT NULL AND ((r.closed_at IS NOT NULL AND r.closed_at > r.due_at) OR (r.closed_at IS NULL AND r.due_at < NOW()))) AS sla_missed, "
            "AVG(r.rating) AS rating, COUNT(r.rating) AS rated, SUM(r.source = 'portal') AS portal "
            "FROM " + src + " WHERE r.created_at >= %s AND r.created_at < %s AND r.to_office = 1", rng, one=True)
    techs = q("SELECT a.id, a.username, a.full_name, a.role, COUNT(r.id) AS jobs, SUM(r.status IN ('resolved', 'closed')) AS done, "
              "SUM(r.status IN ('open', 'pending')) AS open_now, "
              "AVG(CASE WHEN r.closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, r.created_at, r.closed_at) END) AS fix_min, "
              "SUM(r.due_at IS NOT NULL AND r.closed_at IS NOT NULL AND r.closed_at <= r.due_at) AS met, "
              "SUM(r.due_at IS NOT NULL AND r.closed_at IS NOT NULL) AS timed, AVG(r.rating) AS rating, "
              "SUM(r.arrived_at IS NOT NULL) AS visits FROM " + src + " JOIN panel_admins a ON a.id = r.assigned_to "
              "WHERE r.created_at >= %s AND r.created_at < %s GROUP BY a.id ORDER BY done DESC, jobs DESC", rng)
    areas = q("SELECT r.area, COUNT(*) AS n, SUM(r.status IN ('open', 'pending')) AS open_now, "
              "SUM(r.category = 'connection') AS no_conn FROM " + src + " WHERE r.subscriber_id IS NOT NULL "
              "AND r.created_at >= %s AND r.created_at < %s GROUP BY r.area ORDER BY n DESC LIMIT 15", rng)
    cats = q("SELECT r.category, COUNT(*) AS n, AVG(CASE WHEN r.closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, r.created_at, r.closed_at) END) "
             "AS fix_min FROM " + src + " WHERE r.created_at >= %s AND r.created_at < %s GROUP BY r.category ORDER BY n DESC", rng)
    # customers who came back with a new problem within 7 days of an earlier one, in this period
    seen = q("SELECT r.subscriber_id, r.created_at FROM " + src + " WHERE r.subscriber_id IS NOT NULL AND r.created_at >= %s "
             "AND r.created_at < %s ORDER BY r.subscriber_id, r.created_at", (lo - timedelta(days=7), hi))
    hits, prev = {}, {}
    for s in seen:
        p = prev.get(s["subscriber_id"])
        if p and s["created_at"] <= p + timedelta(days=7) and s["created_at"] >= lo:
            h = hits.setdefault(s["subscriber_id"], {"n": 1, "last": s["created_at"]})
            h["n"] += 1
            h["last"] = max(h["last"], s["created_at"])
        prev[s["subscriber_id"]] = s["created_at"]
    repeat = []
    if hits:
        ids = sorted(hits, key=lambda k: (-hits[k]["n"], -hits[k]["last"].timestamp()))[:15]
        subs = {s["id"]: s for s in q("SELECT id, username, full_name, area FROM subscribers WHERE id IN (%s)" %
                                      ",".join(["%s"] * len(ids)), ids)}
        repeat = [dict(subs[i], n=hits[i]["n"], last=hits[i]["last"]) for i in ids if i in subs]
    try:
        mats = q("SELECT i.name, i.unit, SUM(m.qty) AS qty, COUNT(DISTINCT m.ticket_id) AS jobs FROM ticket_materials m "
                 "JOIN inventory_items i ON i.id = m.item_id WHERE m.created_at >= %s AND m.created_at < %s "
                 "GROUP BY i.id ORDER BY qty DESC LIMIT 10", rng)
    except Exception:
        mats = []
    with get_db().cursor() as cur:
        months = ticketlib.month_stats(cur)
    today = date.today()
    presets = [(label, (today - timedelta(days=d)).isoformat(), today.isoformat()) for d, label in REPORT_PRESETS]
    presets.append(("This month", today.replace(day=1).isoformat(), today.isoformat()))
    return render_template("ticket_report.html", start=start, end=end, presets=presets, tot=tot, techs=techs, areas=areas,
                           cats=cats, repeat=repeat, mats=mats, all_cats=ALL_CATEGORIES, hours_text=hours_text, months=months,
                           purge_on=core.cfg().get("ticket_purge_on") == "1")


# ===================================================================== settings
@route("/tickets/settings", module="tickets", methods=["GET", "POST"])
def ticket_settings():
    if not manager():
        abort(403)
    if request.method == "POST":
        f = request.form
        act = f.get("act", "limits")
        with get_db().cursor() as cur:
            if act == "limits":
                import db
                vals = {}
                for k in ("sla_urgent", "sla_high", "sla_normal", "sla_low", "sla_unassigned_min", "ticket_low_rating") + tuple(
                        "sla_cat_" + c for c, _n in CATEGORIES):
                    v = f.get(k, "").strip()
                    if v and not v.replace(".", "", 1).isdigit():
                        flash("Time limits are numbers of hours (minutes for the nobody-took-it alert).", "error")
                        return redirect(url_for("ticket_settings"))
                    vals[k] = v
                for k in ("sla_on", "ticket_auto_assign", "ticket_sms_on", "ticket_app_on", "ticket_otp_on", "ticket_portal_on",
                          "ticket_purge_on"):
                    vals[k] = "1" if f.get(k) == "1" else "0"
                vals["ticket_portal_route"] = "seller" if f.get("ticket_portal_route") == "seller" else "office"
                for k, v in vals.items():
                    db.save_setting(cur, k, v)
                audit("ticket_settings_saved")
                flash("Ticket settings saved. New time limits apply to new tickets.", "success")
            elif act == "techs":
                cur.execute("DELETE FROM ticket_area_techs")
                rows = []
                for area in core.area_names():
                    for v in f.getlist("area__" + area):
                        if v.isdigit():
                            rows.append((area, int(v)))
                if rows:
                    cur.executemany("INSERT IGNORE INTO ticket_area_techs (area, admin_id) VALUES (%s, %s)", rows)
                audit("ticket_area_techs_saved", "", "%d" % len(rows))
                flash("Area technicians saved.", "success")
            elif act == "canned_add":
                title, body = f.get("title", "").strip()[:80], f.get("body", "").strip()[:3000]
                if len(title) < 2 or len(body) < 2:
                    flash("Give the ready reply a short name and its text.", "error")
                else:
                    cur.execute("INSERT INTO ticket_canned (title, body, created_by) VALUES (%s, %s, %s)", (title, body, g.me["username"]))
                    flash("Ready reply added.", "success")
            elif act == "canned_del" and f.get("id", "").isdigit():
                cur.execute("DELETE FROM ticket_canned WHERE id = %s", (int(f["id"]),))
                flash("Ready reply deleted.", "success")
        commit()
        return redirect(url_for("ticket_settings") + "#" + ("canned" if act.startswith("canned") else act))
    techs = [s for s in _staff() if s["role"] in ("technician", "support", "manager")]
    mapping = {}
    for r in q("SELECT area, admin_id FROM ticket_area_techs"):
        mapping.setdefault(r["area"], set()).add(r["admin_id"])
    return render_template("ticket_settings.html", s=core.cfg(), categories=CATEGORIES, areas=core.area_names(), techs=techs,
                           mapping=mapping, canned=q("SELECT * FROM ticket_canned ORDER BY title"))


# ===================================================================== outages
@route("/outages", module="tickets", methods=["GET", "POST"])
def outages():
    if seller():
        abort(403)
    if request.method == "POST":
        f = request.form
        if f.get("act") == "close" and f.get("id", "").isdigit():
            o = q("SELECT * FROM outages WHERE id = %s AND active = 1", (int(f["id"]),), one=True) or abort(404)
            x("UPDATE outages SET active = 0, closed_at = NOW() WHERE id = %s", (o["id"],))
            sent = 0
            if f.get("sms") == "1" and core.can("sms", "edit"):
                sent = _outage_sms(o["area"], "the problem on the line is fixed. Your internet should work now; "
                                   "restart the router if it does not.", "outage-fixed:%d" % o["id"])
            audit("outage_closed", o["area"] or "all areas", "%d SMS" % sent)
            commit()
            flash("Outage closed%s." % ((" and %d SMS queued" % sent) if sent else ""), "success")
            return redirect(url_for("outages"))
        area = f.get("area", "").strip()
        if area and area not in core.area_names():
            abort(400)
        msg = f.get("message", "").strip()[:500]
        if len(msg) < 5:
            flash("Write what is happening, e.g. 'Fiber cut on the main road, we are fixing it, back by 6 pm'.", "error")
            return redirect(url_for("outages"))
        ends = None
        if f.get("ends_at"):
            try:
                ends = datetime.strptime(f["ends_at"], "%Y-%m-%dT%H:%M")
            except ValueError:
                ends = None
        oid = insert("INSERT INTO outages (area, message, ends_at, created_by) VALUES (%s, %s, %s, %s)",
                     (area, msg, ends, g.me["username"]))
        sent = 0
        if f.get("sms") == "1" and core.can("sms", "edit"):
            sent = _outage_sms(area, msg, "outage:%d" % oid)
            x("UPDATE outages SET sms_sent = %s WHERE id = %s", (sent, oid))
        audit("outage_posted", area or "all areas", msg[:100])
        commit()
        flash("Outage posted: customers%s see it on their portal%s." % (
            (" in " + area) if area else "", (" and %d SMS were queued" % sent) if sent else ""), "success")
        return redirect(url_for("outages"))
    rows = q("SELECT o.*, (SELECT COUNT(*) FROM subscribers s WHERE o.area = '' OR s.area = o.area COLLATE utf8mb4_unicode_ci) AS customers, "
             "(SELECT COUNT(*) FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id WHERE t.created_at >= o.created_at "
             "AND (o.closed_at IS NULL OR t.created_at <= o.closed_at) AND (o.area = '' OR s.area = o.area COLLATE utf8mb4_unicode_ci)) AS tickets "
             "FROM outages o ORDER BY o.active DESC, o.id DESC LIMIT 50")
    return render_template("outages.html", rows=rows, areas=core.area_names(), can_sms=core.can("sms", "edit"))


def _outage_sms(area, message, key):
    s = core.cfg()
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.sms_on, s.expiry_date, s.area, NULL AS plan_label, NULL AS price "
             "FROM subscribers s WHERE s.status = 'active' AND s.phone <> '' AND s.sms_on = 1" + (" AND s.area = %s" if area else ""),
             (area,) if area else ())
    n = 0
    with get_db().cursor() as cur:
        for sub in rows:
            n += ticketlib.sms_to(cur, s, sub, "outage", {"message": message}, "%s:%d" % (key, sub["id"]), force=True)
    return n
