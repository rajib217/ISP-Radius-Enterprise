"""Customer portal > Support: the customer opens a complaint, follows it, writes to the office and rates the work.

  /portal/support              my complaints, a new one (and the outage notice for my area)
  /portal/support/<id>         the conversation (no internal notes), reply / "still not fixed", rating 1-5 stars

Switched on with Tickets > Settings > "Customers open tickets on the portal" (ticket_portal_on) and the portal itself.
"""
from flask import abort, flash, redirect, render_template, request, url_for

import core
import mod_portal
import mod_tickets
import ticketlib
from core import audit, commit, get_db, insert, q, route, x

CATEGORIES = [("connection", "ইন্টারনেট নেই"), ("slow", "স্পিড কম / বারবার কেটে যায়"), ("hardware", "রাউটার / ONU সমস্যা"),
              ("billing", "বিল / পেমেন্ট"), ("shift", "লাইন অন্য জায়গায় নেওয়া"), ("other", "অন্যান্য")]
STATUS_BN = {"open": "পাওয়া গেছে", "pending": "কাজ চলছে", "resolved": "সমাধান হয়েছে", "closed": "বন্ধ"}
MAX_OPEN = 3            # open complaints at a time
MAX_DAY = 5             # new complaints a day


def _need():
    sub = mod_portal._need()
    if not sub:
        return None
    if core.cfg().get("ticket_portal_on") != "1":
        abort(404)
    return sub


def _ctx(sub, **kw):
    s = core.cfg()
    with get_db().cursor() as cur:
        outs = ticketlib.active_outages(cur, sub.get("area") or "")
    # who the customer calls: their reseller when they belong to one (as on the portal home), else the office
    owner = mod_portal._owner(sub)
    help_phone = owner["phone"] if owner and owner["role"] != "admin" and owner.get("phone") else s.get("support_phone", "")
    return dict(sub=sub, isp=s.get("isp_name", ""), phone=s.get("support_phone", ""), help_phone=help_phone,
                outages=outs, status_bn=STATUS_BN,
                cats=dict(CATEGORIES + [("new", "নতুন সংযোগ")] + mod_tickets.SELLER_CATEGORIES), **kw)


@route("/portal/support", methods=["GET", "POST"])
def portal_support():
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    form = {"category": "connection", "subject": "", "body": ""}
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in form}
        cat = form["category"] if form["category"] in dict(CATEGORIES) else "other"
        n_open = q("SELECT COUNT(*) AS c FROM tickets WHERE subscriber_id = %s AND status IN ('open', 'pending')", (sub["id"],), one=True)["c"]
        n_day = q("SELECT COUNT(*) AS c FROM tickets WHERE subscriber_id = %s AND created_at >= CURDATE()", (sub["id"],), one=True)["c"]
        err = ""
        if n_open >= MAX_OPEN:
            err = "আপনার %d টি অভিযোগ এখনো খোলা আছে। নতুন কিছু জানাতে খোলা অভিযোগেই লিখুন।" % n_open
        elif n_day >= MAX_DAY:
            err = "আজ আর নতুন অভিযোগ করা যাবে না। খোলা অভিযোগে লিখুন বা ফোন করুন।"
        elif len(form["body"]) < 5:
            err = "সমস্যাটা কয়েক শব্দে লিখুন।"
        if err:
            flash(err, "error")
        else:
            s = core.cfg()
            subject = form["subject"][:120] or dict(CATEGORIES)[cat]
            to_office = not (s.get("ticket_portal_route") == "seller" and mod_tickets.seller_of_sub(sub))
            pri = "high" if cat == "connection" else "normal"
            tid = mod_tickets.create(sub, subject, cat, pri, form["body"][:3000], "customer:" + sub["username"],
                                     source="portal", to_office=to_office)
            audit("ticket_created", "#%d" % tid, "portal: " + subject, who="customer:" + sub["username"])
            mod_tickets.watch_alert(tid, "new", "New complaint", form["body"])   # admins + the customer's reseller chain
            commit()
            flash("অভিযোগ #%d পাঠানো হয়েছে। কাজের খবর এখানে আর নোটিফিকেশনে পাবেন।" % tid, "success")
            return redirect(url_for("portal_ticket", tid=tid))
    rows = q("SELECT t.*, a.full_name AS tech_name, a.username AS tech_user FROM tickets t LEFT JOIN panel_admins a "
             "ON a.id = t.assigned_to WHERE t.subscriber_id = %s ORDER BY t.status IN ('open', 'pending') DESC, t.id DESC LIMIT 50",
             (sub["id"],))
    return render_template("portal_support.html", rows=rows, form=form, categories=CATEGORIES, **_ctx(sub))


def _mine(sub, tid):
    t = q("SELECT t.*, a.full_name AS tech_name, a.username AS tech_user, a.phone AS tech_phone FROM tickets t "
          "LEFT JOIN panel_admins a ON a.id = t.assigned_to WHERE t.id = %s", (tid,), one=True)
    if not t or t["subscriber_id"] != sub["id"]:
        abort(404)
    return t


@route("/portal/support/<int:tid>")
def portal_ticket(tid):
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login", next=request.path))
    t = _mine(sub, tid)
    replies = q("SELECT author, body, photo, created_at FROM ticket_replies WHERE ticket_id = %s AND internal = 0 ORDER BY id", (tid,))
    return render_template("portal_ticket.html", t=t, replies=replies, **_ctx(sub))


@route("/portal/support/<int:tid>/reply", methods=["POST"])
def portal_ticket_reply(tid):
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    t = _mine(sub, tid)
    body = request.form.get("body", "").strip()[:3000]
    reopen = request.form.get("reopen") == "1" and t["status"] in ("resolved", "closed")
    if not body:
        flash("কিছু লিখুন।", "error")
        return redirect(url_for("portal_ticket", tid=tid))
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
           (tid, "customer:" + sub["username"], body))
    if reopen:
        s = core.cfg()
        x("UPDATE tickets SET status = 'open', closed_at = NULL, alerted = 0, due_at = %s, updated_at = NOW() WHERE id = %s",
          (ticketlib.due_for(s, t["category"], "high") if t["to_office"] else None, tid))
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 1)",
               (tid, "customer:" + sub["username"], "The customer says it is still not fixed: reopened."))
    else:
        x("UPDATE tickets SET updated_at = NOW() WHERE id = %s", (tid,))
    audit("ticket_reply", "#%d" % tid, "portal" + (" reopen" if reopen else ""), who="customer:" + sub["username"])
    commit()
    flash("আবার খোলা হয়েছে, আমরা দেখছি।" if reopen else "পাঠানো হয়েছে।", "success")
    return redirect(url_for("portal_ticket", tid=tid))


@route("/portal/support/<int:tid>/rate", methods=["POST"])
def portal_ticket_rate(tid):
    sub = _need()
    if not sub:
        return redirect(url_for("portal_login"))
    t = _mine(sub, tid)
    stars = request.form.get("stars", "")
    if t["status"] not in ("resolved", "closed") or t["rating"] or not stars.isdigit() or not 1 <= int(stars) <= 5:
        flash("রেটিং দেওয়া যায়নি।", "error")
        return redirect(url_for("portal_ticket", tid=tid))
    note = request.form.get("note", "").strip()[:500]
    x("UPDATE tickets SET rating = %s, rating_note = %s, rated_at = NOW() WHERE id = %s", (int(stars), note, tid))
    with get_db().cursor() as cur:
        ticketlib.rating_alert(cur, core.cfg(), t, int(stars), note)
    audit("ticket_rated", "#%d" % tid, "%s/5" % stars, who="customer:" + sub["username"])
    commit()
    flash("ধন্যবাদ! আপনার মতামত আমাদের সেবা আরও ভালো করবে।", "success")
    return redirect(url_for("portal_ticket", tid=tid))
