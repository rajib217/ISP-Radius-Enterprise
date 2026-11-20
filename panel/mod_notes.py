"""Notes: private or shared sticky notes, optionally linked to a customer."""
from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import audit, commit, insert, q, route, x

COLORS = ["yellow", "green", "blue", "pink", "gray"]


@route("/notes", module="notes")
def notes():
    where, params = ["(n.user_id = %s OR n.shared = 1)"], [g.me["id"]]
    text = request.args.get("q", "").strip()
    if text:
        where.append("(n.title LIKE %s OR n.body LIKE %s)")
        params += ["%" + text + "%"] * 2
    if request.args.get("mine") == "1":
        where.append("n.user_id = %s")
        params.append(g.me["id"])
    if request.args.get("sub", "").isdigit():
        where.append("n.subscriber_id = %s")
        params.append(int(request.args["sub"]))
    rows = q("SELECT n.*, s.username AS sub_username FROM notes n LEFT JOIN subscribers s ON s.id = n.subscriber_id "
             "WHERE " + " AND ".join(where) + " ORDER BY n.pinned DESC, n.updated_at DESC LIMIT 300", params)
    for r in rows:
        r["mine"] = r["user_id"] == g.me["id"] or g.me["role"] == "admin"
    return render_template("notes.html", rows=rows, colors=COLORS)


@route("/notes/save", module="notes", methods=["POST"])
def note_save():
    f = request.form
    nid = int(f["id"]) if f.get("id", "").isdigit() else 0
    title, body = f.get("title", "").strip()[:120], f.get("body", "").strip()
    color = f.get("color") if f.get("color") in COLORS else "yellow"
    if not (title or body) or len(body) > 5000:
        flash("Write a title or some text (up to 5000 characters).", "error")
        return redirect(url_for("notes"))
    sub_id = None
    if f.get("username", "").strip():
        sub = q("SELECT id FROM subscribers WHERE username = %s", (f["username"].strip(),), one=True)
        if not sub:
            flash("No subscriber has the username %s." % f["username"].strip(), "error")
            return redirect(url_for("notes"))
        sub_id = sub["id"]
    shared = 1 if f.get("shared") == "1" else 0
    if nid:
        old = q("SELECT user_id FROM notes WHERE id = %s", (nid,), one=True) or abort(404)
        if old["user_id"] != g.me["id"] and g.me["role"] != "admin":
            abort(403)
        x("UPDATE notes SET title = %s, body = %s, color = %s, shared = %s, subscriber_id = %s WHERE id = %s",
          (title, body, color, shared, sub_id, nid))
    else:
        insert("INSERT INTO notes (user_id, author, title, body, color, shared, subscriber_id) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s)", (g.me["id"], g.me["username"], title, body, color, shared, sub_id))
    commit()
    flash("Note saved.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("notes"))


@route("/notes/<int:nid>/pin", module="notes", methods=["POST"])
def note_pin(nid):
    old = q("SELECT user_id FROM notes WHERE id = %s", (nid,), one=True) or abort(404)
    if old["user_id"] != g.me["id"] and g.me["role"] != "admin":
        abort(403)
    x("UPDATE notes SET pinned = 1 - pinned WHERE id = %s", (nid,))
    commit()
    return redirect(url_for("notes"))


@route("/notes/<int:nid>/delete", module="notes", methods=["POST"])
def note_delete(nid):
    old = q("SELECT user_id FROM notes WHERE id = %s", (nid,), one=True) or abort(404)
    if old["user_id"] != g.me["id"] and g.me["role"] != "admin":
        abort(403)
    x("DELETE FROM notes WHERE id = %s", (nid,))
    audit("note_deleted", "#%d" % nid)
    commit()
    flash("Note deleted.", "success")
    return redirect(url_for("notes"))
