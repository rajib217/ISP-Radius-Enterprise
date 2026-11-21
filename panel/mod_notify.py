"""App notifications to many customers at once (Subscribers > their portal / installed app).

One customer: the subscriber page's Send Notification button (mod_subview.subscriber_notify).
Many customers: /notifications/send. Pick a group (plan, area, status, only phones that allow notifications) or one
username, write a title and message ({name}, {expiry} ... are filled per customer). Each send is a batch: the history
below the form shows how many got it and how many have read it, and a batch can be taken back (deleted).
The sender service pushes new notifications to the phones that allowed them (sender.push_round)."""
import re
import secrets

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

import core
import msglib
from core import BASE_FROM, audit, cfg, commit, get_db, q, route

MAX_RECIPIENTS = 50000
BATCH_RE = re.compile(r"^[0-9a-f]{12}$")
SUB_COLS = "s.id, s.username, s.full_name, s.phone, s.email, s.expiry_date, p.label AS plan_label, p.price"
STATUSES = (("", "Any status"), ("active", "Active"), ("expiring", "Expiring in 7 days"), ("exp3", "Expiring in 3 days"),
            ("expired", "Expired"), ("suspended", "Suspended"), ("online", "Online now"), ("offline", "Offline"))
PUSH_ONLY = "s.id IN (SELECT subscriber_id FROM push_subscriptions)"


def _where(src):
    where, params = core.sub_filters(src)
    if src.get("push_only") == "1":
        where = (where + " AND " + PUSH_ONLY) if where else (" WHERE " + PUSH_ONLY)
    return where, params


def _mine():
    """History filter: sellers see the batches sent by themselves and the people below them."""
    names = core.my_downline()
    if names is None:
        return "", []
    return " AND created_by IN (%s)" % ",".join(["%s"] * len(names)), list(names)


def _history():
    sc, sp = _mine()
    return q("SELECT batch, MIN(title) AS title, MIN(body) AS body, MIN(created_by) AS created_by, MIN(created_at) AS at, "
             "COUNT(*) AS n, SUM(read_at IS NOT NULL) AS seen, SUM(pushed_at IS NOT NULL) AS pushed "
             "FROM sub_notifications WHERE batch IS NOT NULL" + sc + " GROUP BY batch ORDER BY MIN(id) DESC LIMIT 30", sp)


@route("/notifications/send", module="subscribers", methods=["GET", "POST"])
def notify_send():
    s = cfg()
    form = {"audience": "filter", "plan": request.args.get("plan", ""), "area": request.args.get("area", ""),
            "status": request.args.get("status", ""), "username": request.args.get("username", ""),
            "push_only": "", "title": "", "body": ""}
    if form["username"]:
        form["audience"] = "single"
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in form}
        form["audience"] = "single" if form["audience"] == "single" else "filter"
        errors = []
        if not 1 <= len(form["title"]) <= 120:
            errors.append("Write a title (up to 120 characters).")
        if not 1 <= len(form["body"]) <= 600:
            errors.append("Write the message (up to 600 characters).")
        rows = []
        if not errors:
            if form["audience"] == "single":
                where, params = core.sub_filters({})
                where = (where + " AND " if where else " WHERE ") + "s.username = %s"
                rows = q("SELECT " + SUB_COLS + BASE_FROM + where, params + [form["username"]])
                if not rows:
                    errors.append("No customer of yours has that username.")
            else:
                where, params = _where(f)
                rows = q("SELECT " + SUB_COLS + BASE_FROM + where + " ORDER BY s.id LIMIT %d" % (MAX_RECIPIENTS + 1), params)
                if len(rows) > MAX_RECIPIENTS:
                    errors.append("More than %d customers. Narrow the group." % MAX_RECIPIENTS)
                elif not rows:
                    errors.append("No customer matches this group.")
        if not errors:
            batch, me = secrets.token_hex(6), g.me["username"]
            out = []
            for r in rows:
                ctx = msglib.sub_context(r, s.get("isp_name", ""))
                out.append((r["id"], msglib.render(form["title"], ctx)[:120], msglib.render(form["body"], ctx)[:600], me, batch))
            with get_db().cursor() as cur:
                for i in range(0, len(out), 1000):
                    cur.executemany("INSERT INTO sub_notifications (subscriber_id, kind, title, body, created_by, batch) "
                                    "VALUES (%s, 'custom', %s, %s, %s, %s)", out[i:i + 1000])
            audit("app_notification_bulk", form["username"] if form["audience"] == "single" else "",
                  "%d customers, batch %s: %s" % (len(out), batch, form["title"]))
            commit()
            flash("Notification sent to %d customer%s. It shows in their portal / app%s." % (
                len(out), "" if len(out) == 1 else "s",
                "" if s.get("portal_on") == "1" else " once the customer portal is switched on"), "success")
            return redirect(url_for("notify_send"))
        for e in errors:
            flash(e, "error")
    return render_template("notify_send.html", form=form, statuses=STATUSES, history=_history(),
                           plans=core.filter_plans(q("SELECT id, label FROM plans ORDER BY download_mbps, label")),
                           areas=core.area_names(), variables=[v for v in msglib.VARIABLES if v not in ("password", "amount")],
                           portal_on=s.get("portal_on") == "1")


@route("/notifications/recipients", module="subscribers")
def notify_recipients():
    where, params = _where(request.args)
    return jsonify(count=q("SELECT COUNT(*) AS c" + BASE_FROM + where, params, one=True)["c"])


@route("/notifications/batch/<batch>/delete", module="subscribers", methods=["POST"])
def notify_batch_delete(batch):
    """Take a sent batch back: it disappears from every customer's portal (phones already notified keep the pop-up)."""
    if not BATCH_RE.match(batch):
        abort(404)
    sc, sp = _mine()
    with get_db().cursor() as cur:
        cur.execute("DELETE FROM sub_notifications WHERE batch = %s" + sc, [batch] + sp)
        n = cur.rowcount
    if not n:
        abort(404)
    audit("app_notification_bulk_delete", "", "batch %s, %d removed" % (batch, n))
    commit()
    flash("Notification removed from %d customer%s." % (n, "" if n == 1 else "s"), "success")
    return redirect(url_for("notify_send"))
