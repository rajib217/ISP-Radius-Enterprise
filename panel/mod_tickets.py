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
from datetime import datetime, timedelta

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
    return g.me["role"] in core.SELLER_ROLES


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
    return q("SELECT id, username, full_name, role, phone FROM panel_admins WHERE is_active = 1 AND role NOT IN "
             "('reseller', 'subreseller', 'retailer') ORDER BY role = 'technician' DESC, username")


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
                dept = who = None
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
            commit()
            flash(("Ticket #%d saved as solved." if solved else "Ticket #%d created.") % tid, "success")
            return redirect(url_for("ticket_view", tid=tid))
        flash("Please fix the highlighted fields.", "error")
    return render_template("ticket_new.html", form=form, errors=errors, depts=q("SELECT id, name FROM departments ORDER BY name"),
                           staff=[] if is_seller else _staff(), categories=CATEGORIES, seller_categories=SELLER_CATEGORIES,
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
                           staff=[] if is_seller else _staff(), categories=CATEGORIES, all_cats=ALL_CATEGORIES,
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


@route("/tickets/<int:tid>/to-office", module="tickets", methods=["POST"])
def ticket_to_office(tid):
    """A reseller cannot fix it: the office takes over (time limit and technician start now)."""
    t = load(tid)
    if t["to_office"]:
        flash("The office already has this ticket.", "warn")
        return redirect(url_for("ticket_view", tid=tid))
    s = core.cfg()
    who = None
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
    x("DELETE FROM ticket_replies WHERE ticket_id = %s", (tid,))
    x("DELETE FROM ticket_materials WHERE ticket_id = %s", (tid,))
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
@route("/tickets/report", module="tickets")
def ticket_report():
    if seller():
        abort(403)
    days = request.args.get("days", 30, type=int)
    days = days if days in (7, 30) else 30
    since = datetime.now() - timedelta(days=days)
    tot = q("SELECT COUNT(*) AS opened, SUM(status IN ('resolved', 'closed')) AS closed, "
            "SUM(status IN ('open', 'pending')) AS still_open, "
            "AVG(CASE WHEN closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, created_at, closed_at) END) AS fix_min, "
            "AVG(CASE WHEN first_reply_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, created_at, first_reply_at) END) AS reply_min, "
            "SUM(due_at IS NOT NULL AND closed_at IS NOT NULL AND closed_at <= due_at) AS sla_met, "
            "SUM(due_at IS NOT NULL AND ((closed_at IS NOT NULL AND closed_at > due_at) OR (closed_at IS NULL AND due_at < NOW()))) AS sla_missed, "
            "AVG(rating) AS rating, COUNT(rating) AS rated, SUM(source = 'portal') AS portal "
            "FROM tickets WHERE created_at >= %s AND to_office = 1", (since,), one=True)
    techs = q("SELECT a.id, a.username, a.full_name, a.role, COUNT(t.id) AS jobs, SUM(t.status IN ('resolved', 'closed')) AS done, "
              "SUM(t.status IN ('open', 'pending')) AS open_now, "
              "AVG(CASE WHEN t.closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, t.created_at, t.closed_at) END) AS fix_min, "
              "SUM(t.due_at IS NOT NULL AND t.closed_at IS NOT NULL AND t.closed_at <= t.due_at) AS met, "
              "SUM(t.due_at IS NOT NULL AND t.closed_at IS NOT NULL) AS timed, AVG(t.rating) AS rating, "
              "SUM(t.arrived_at IS NOT NULL) AS visits FROM tickets t JOIN panel_admins a ON a.id = t.assigned_to "
              "WHERE t.created_at >= %s GROUP BY a.id ORDER BY done DESC, jobs DESC", (since,))
    areas = q("SELECT COALESCE(NULLIF(s.area, ''), '(no area)') AS area, COUNT(*) AS n, SUM(t.status IN ('open', 'pending')) AS open_now, "
              "SUM(t.category = 'connection') AS no_conn FROM tickets t JOIN subscribers s ON s.id = t.subscriber_id "
              "WHERE t.created_at >= %s GROUP BY COALESCE(NULLIF(s.area, ''), '(no area)') ORDER BY n DESC LIMIT 15", (since,))
    cats = q("SELECT category, COUNT(*) AS n, AVG(CASE WHEN closed_at IS NOT NULL THEN TIMESTAMPDIFF(MINUTE, created_at, closed_at) END) "
             "AS fix_min FROM tickets WHERE created_at >= %s GROUP BY category ORDER BY n DESC", (since,))
    repeat = q("SELECT s.id, s.username, s.full_name, s.area, COUNT(*) AS n, MAX(t.created_at) AS last FROM tickets t "
               "JOIN subscribers s ON s.id = t.subscriber_id WHERE t.created_at >= %s AND t.subscriber_id IN (SELECT p.subscriber_id "
               "FROM tickets p JOIN tickets r ON r.subscriber_id = p.subscriber_id AND r.id > p.id "
               "AND r.created_at <= p.created_at + INTERVAL 7 DAY WHERE p.created_at >= %s) "
               "GROUP BY s.id ORDER BY n DESC, last DESC LIMIT 15", (since, since - timedelta(days=7)))
    try:
        mats = q("SELECT i.name, i.unit, SUM(m.qty) AS qty, COUNT(DISTINCT m.ticket_id) AS jobs FROM ticket_materials m "
                 "JOIN inventory_items i ON i.id = m.item_id WHERE m.created_at >= %s GROUP BY i.id ORDER BY qty DESC LIMIT 10", (since,))
    except Exception:
        mats = []
    with get_db().cursor() as cur:
        months = ticketlib.month_stats(cur)
    return render_template("ticket_report.html", days=days, tot=tot, techs=techs, areas=areas, cats=cats, repeat=repeat,
                           mats=mats, all_cats=ALL_CATEGORIES, hours_text=hours_text, months=months,
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
    techs = q("SELECT id, username, full_name, role FROM panel_admins WHERE is_active = 1 AND role IN ('technician', 'support', "
              "'manager') ORDER BY role = 'technician' DESC, username")
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
