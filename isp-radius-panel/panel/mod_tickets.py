"""Support tickets."""
from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import admin_required, audit, commit, get_db, insert, pager, q, route, x

CATEGORIES = [("connection", "No connection"), ("slow", "Slow speed"), ("billing", "Billing"),
              ("new", "New connection"), ("shift", "Shift / relocation"), ("hardware", "Hardware / ONU"),
              ("other", "Other")]
PRIORITIES = [("low", "Low"), ("normal", "Normal"), ("high", "High"), ("urgent", "Urgent")]
STATUSES = [("open", "Open"), ("pending", "Pending"), ("resolved", "Resolved"), ("closed", "Closed")]


def _staff():
    return q("SELECT id, username, full_name FROM panel_admins WHERE is_active = 1 ORDER BY username")


@route("/tickets", module="tickets")
def tickets():
    where, params = [], []
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
    if request.args.get("mine") == "1":
        where.append("t.assigned_to = %s")
        params.append(g.me["id"])
    if request.args.get("category") in dict(CATEGORIES):
        where.append("t.category = %s")
        params.append(request.args["category"])
    if request.args.get("priority") in dict(PRIORITIES):
        where.append("t.priority = %s")
        params.append(request.args["priority"])
    if request.args.get("dept", "").isdigit():
        where.append("t.department_id = %s")
        params.append(int(request.args["dept"]))
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = q("SELECT COUNT(*) AS c FROM tickets t" + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT t.*, a.username AS assignee, d.name AS dept_name, s.id AS sid, s.full_name AS sub_name "
             "FROM tickets t LEFT JOIN panel_admins a ON a.id = t.assigned_to "
             "LEFT JOIN departments d ON d.id = t.department_id LEFT JOIN subscribers s ON s.id = t.subscriber_id" +
             cond + " ORDER BY t.updated_at DESC, t.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    counts = {r["status"]: r["c"] for r in q("SELECT status, COUNT(*) AS c FROM tickets GROUP BY status")}
    counts["open_all"] = counts.get("open", 0) + counts.get("pending", 0)
    depts = q("SELECT id, name FROM departments ORDER BY name")
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("tickets.html", rows=rows, pg=pg, counts=counts, depts=depts, keep=keep,
                           categories=CATEGORIES, priorities=PRIORITIES)


@route("/tickets/new", module="tickets", methods=["GET", "POST"])
def ticket_new():
    form = {"username": "", "subject": "", "category": "connection", "priority": "normal", "body": "",
            "department_id": "", "assigned_to": ""}
    if request.args.get("sub", "").isdigit():
        s = q("SELECT username FROM subscribers WHERE id = %s", (int(request.args["sub"]),), one=True)
        if s:
            form["username"] = s["username"]
    errors = {}
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in form}
        sub = None
        if form["username"]:
            sub = q("SELECT id, username FROM subscribers WHERE username = %s", (form["username"],), one=True)
            if not sub:
                errors["username"] = "No subscriber has this username. Leave it empty for a general ticket."
        if not 3 <= len(form["subject"]) <= 200:
            errors["subject"] = "Enter a subject of 3-200 characters."
        if form["category"] not in dict(CATEGORIES):
            form["category"] = "other"
        if form["priority"] not in dict(PRIORITIES):
            form["priority"] = "normal"
        if len(form["body"]) > 5000:
            errors["body"] = "Too long (5000 characters max)."
        if not errors:
            dept = int(form["department_id"]) if form["department_id"].isdigit() else None
            who = int(form["assigned_to"]) if form["assigned_to"].isdigit() else None
            tid = insert("INSERT INTO tickets (subscriber_id, username, subject, category, priority, status, "
                         "department_id, assigned_to, created_by) VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s)",
                         (sub["id"] if sub else None, sub["username"] if sub else "", form["subject"],
                          form["category"], form["priority"], dept, who, g.me["username"]))
            if form["body"]:
                insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
                       (tid, g.me["username"], form["body"]))
            audit("ticket_created", "#%d" % tid, form["subject"])
            commit()
            flash("Ticket #%d created." % tid, "success")
            return redirect(url_for("ticket_view", tid=tid))
        flash("Please fix the highlighted fields.", "error")
    return render_template("ticket_new.html", form=form, errors=errors, depts=q("SELECT id, name FROM departments "
                           "ORDER BY name"), staff=_staff(), categories=CATEGORIES, priorities=PRIORITIES)


@route("/tickets/<int:tid>", module="tickets")
def ticket_view(tid):
    t = q("SELECT t.*, d.name AS dept_name, a.username AS assignee, s.id AS sid, s.full_name AS sub_name, "
          "s.phone AS sub_phone, s.area AS sub_area FROM tickets t LEFT JOIN departments d ON d.id = t.department_id "
          "LEFT JOIN panel_admins a ON a.id = t.assigned_to LEFT JOIN subscribers s ON s.id = t.subscriber_id "
          "WHERE t.id = %s", (tid,), one=True) or abort(404)
    replies = q("SELECT * FROM ticket_replies WHERE ticket_id = %s ORDER BY id", (tid,))
    return render_template("ticket_view.html", t=t, replies=replies,
                           depts=q("SELECT id, name FROM departments ORDER BY name"), staff=_staff(),
                           categories=CATEGORIES, priorities=PRIORITIES, statuses=STATUSES)


def _set_status(tid, status):
    if status in ("resolved", "closed"):
        x("UPDATE tickets SET status = %s, closed_at = NOW() WHERE id = %s", (status, tid))
    else:
        x("UPDATE tickets SET status = %s, closed_at = NULL WHERE id = %s", (status, tid))


@route("/tickets/<int:tid>/reply", module="tickets", methods=["POST"])
def ticket_reply(tid):
    q("SELECT id FROM tickets WHERE id = %s", (tid,), one=True) or abort(404)
    body = request.form.get("body", "").strip()
    status = request.form.get("status", "")
    if body:
        if len(body) > 5000:
            flash("The reply is too long (5000 characters max).", "error")
            return redirect(url_for("ticket_view", tid=tid))
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, %s)",
               (tid, g.me["username"], body, 1 if request.form.get("internal") == "1" else 0))
    if status in dict(STATUSES):
        _set_status(tid, status)
    elif not body:
        flash("Write a reply or pick a new status.", "warn")
        return redirect(url_for("ticket_view", tid=tid))
    x("UPDATE tickets SET updated_at = NOW() WHERE id = %s", (tid,))
    audit("ticket_reply", "#%d" % tid, status)
    commit()
    flash("Ticket updated.", "success")
    return redirect(url_for("ticket_view", tid=tid))


@route("/tickets/<int:tid>/update", module="tickets", methods=["POST"])
def ticket_update(tid):
    q("SELECT id FROM tickets WHERE id = %s", (tid,), one=True) or abort(404)
    f = request.form
    dept = int(f["department_id"]) if f.get("department_id", "").isdigit() else None
    who = int(f["assigned_to"]) if f.get("assigned_to", "").isdigit() else None
    category = f.get("category") if f.get("category") in dict(CATEGORIES) else "other"
    priority = f.get("priority") if f.get("priority") in dict(PRIORITIES) else "normal"
    x("UPDATE tickets SET department_id = %s, assigned_to = %s, category = %s, priority = %s, updated_at = NOW() "
      "WHERE id = %s", (dept, who, category, priority, tid))
    audit("ticket_updated", "#%d" % tid)
    commit()
    flash("Ticket details saved.", "success")
    return redirect(url_for("ticket_view", tid=tid))


@route("/tickets/<int:tid>/delete", module="tickets", methods=["POST"])
@admin_required
def ticket_delete(tid):
    q("SELECT id FROM tickets WHERE id = %s", (tid,), one=True) or abort(404)
    x("DELETE FROM ticket_replies WHERE ticket_id = %s", (tid,))
    x("DELETE FROM tickets WHERE id = %s", (tid,))
    audit("ticket_deleted", "#%d" % tid)
    commit()
    flash("Ticket #%d deleted." % tid, "success")
    return redirect(url_for("tickets"))
