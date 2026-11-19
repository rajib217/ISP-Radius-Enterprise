"""Notices: announcements shown as banners on the dashboard. Switched on in Settings > Software > Modules."""
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import audit, commit, insert, q, route, x

KINDS = [("info", "Information"), ("warn", "Warning"), ("bad", "Urgent")]


def active_notices():
    """Notices that are on and inside their date range (used by the dashboard)."""
    return q("SELECT id, title, body, kind FROM notices WHERE active = 1 "
             "AND (starts IS NULL OR starts <= CURDATE()) AND (ends IS NULL OR ends >= CURDATE()) "
             "ORDER BY id DESC LIMIT 5")


def _date(v):
    try:
        return date.fromisoformat((v or "").strip()) if (v or "").strip() else None
    except ValueError:
        return None


@route("/notices", module="notices")
def notices():
    rows = q("SELECT * FROM notices ORDER BY id DESC LIMIT 200")
    return render_template("notices.html", rows=rows, kinds=KINDS, today=date.today())


@route("/notices/save", module="notices", need="edit", methods=["POST"])
def notice_save():
    f = request.form
    nid = int(f["id"]) if f.get("id", "").isdigit() else 0
    title, body = f.get("title", "").strip()[:160], f.get("body", "").strip()
    kind = f.get("kind") if f.get("kind") in dict(KINDS) else "info"
    starts, ends = _date(f.get("starts")), _date(f.get("ends"))
    if not title or len(body) > 5000:
        flash("Give the notice a title (text up to 5000 characters).", "error")
        return redirect(url_for("notices"))
    if starts and ends and ends < starts:
        flash("The end date is before the start date.", "error")
        return redirect(url_for("notices"))
    active = 1 if f.get("active") == "1" else 0
    if nid:
        q("SELECT id FROM notices WHERE id = %s", (nid,), one=True) or abort(404)
        x("UPDATE notices SET title=%s, body=%s, kind=%s, active=%s, starts=%s, ends=%s WHERE id=%s",
          (title, body, kind, active, starts, ends, nid))
    else:
        nid = insert("INSERT INTO notices (title, body, kind, active, starts, ends, created_by) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s)", (title, body, kind, active, starts, ends, g.me["username"]))
    audit("notice_saved", "notice", title)
    commit()
    flash("Notice saved.", "success")
    return redirect(url_for("notices"))


@route("/notices/<int:nid>/delete", module="notices", need="edit", methods=["POST"])
def notice_delete(nid):
    x("DELETE FROM notices WHERE id = %s", (nid,))
    audit("notice_deleted", "notice", str(nid))
    commit()
    flash("Notice deleted.", "success")
    return redirect(url_for("notices"))
