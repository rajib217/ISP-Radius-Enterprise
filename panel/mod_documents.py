"""Documents: agreements, ID copies, invoices. Files live on disk, outside the web root."""
import mimetypes
import os
import secrets

from flask import abort, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

import core
from core import admin_required, audit, commit, insert, pager, q, route, x

CATEGORIES = ["NID / ID copy", "Agreement", "Invoice", "Receipt", "Photo", "Network diagram", "Other"]
ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".txt", ".csv", ".doc", ".docx", ".xls", ".xlsx"}
INLINE = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
SIGNATURES = {".pdf": (b"%PDF",), ".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8",), ".jpeg": (b"\xff\xd8",),
              ".gif": (b"GIF8",), ".webp": (b"RIFF",)}


def _scope(alias="d", sub_alias="s"):
    """SQL condition + args limiting a seller to documents of their own downline's customers and files they uploaded
    themselves; ("", []) for admin / staff."""
    names = core.my_downline()
    if names is None:
        return "", []
    if not names:
        return "%s.uploaded_by = %%s" % alias, [g.me["username"]]
    return ("(%s.created_by IN (%s) OR %s.uploaded_by = %%s)" % (sub_alias, ",".join(["%s"] * len(names)), alias),
            list(names) + [g.me["username"]])


def _allowed(d):
    """May the signed-in person open this document row (with its customer's created_by as `owner`)?"""
    names = core.my_downline()
    return names is None or d.get("owner") in names or d["uploaded_by"] == g.me["username"]


def _doc(did):
    d = q("SELECT d.*, s.created_by AS owner FROM documents d LEFT JOIN subscribers s ON s.id = d.subscriber_id "
          "WHERE d.id = %s", (did,), one=True) or abort(404)
    if not _allowed(d):
        abort(403)
    return d


def for_subscriber(sid):
    """The customer's documents for their page (newest first), with a flag for files a browser can show."""
    rows = q("SELECT id, title, category, filename, size, uploaded_by, created_at FROM documents WHERE subscriber_id = %s "
             "ORDER BY id DESC LIMIT 200", (sid,))
    for r in rows:
        r["inline"] = os.path.splitext(r["filename"])[1].lower() in INLINE
    return rows


def docs_dir():
    path = os.path.join(core.DATA_DIR, "documents")
    os.makedirs(path, exist_ok=True)
    return path


@route("/documents", module="documents")
def documents():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(d.title LIKE %s OR d.filename LIKE %s OR s.username LIKE %s)")
        params += ["%" + text + "%"] * 3
    if request.args.get("category"):
        where.append("d.category = %s")
        params.append(request.args["category"])
    only = None
    if request.args.get("sub", "").isdigit():
        where.append("d.subscriber_id = %s")
        params.append(int(request.args["sub"]))
        only = q("SELECT id, username, created_by FROM subscribers WHERE id = %s", (int(request.args["sub"]),), one=True)
        names = core.my_downline()
        if only and names is not None and only["created_by"] not in names:
            only = None          # a seller never learns the username of someone else's customer from ?sub=<id>
    sc, sp = _scope()
    if sc:
        where.append(sc)
        params += sp
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    join = " FROM documents d LEFT JOIN subscribers s ON s.id = d.subscriber_id"
    total = q("SELECT COUNT(*) AS c" + join + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT d.*, s.username AS sub_username, s.id AS sid" + join + cond +
             " ORDER BY d.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    for r in rows:
        r["inline"] = os.path.splitext(r["filename"])[1].lower() in INLINE
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("documents.html", rows=rows, pg=pg, keep=keep, categories=CATEGORIES, only=only,
                           allowed=", ".join(sorted(e.lstrip(".") for e in ALLOWED)))


@route("/documents/upload", module="documents", methods=["POST"])
def document_upload():
    f = request.form
    back = redirect(core.safe_next(f.get("next")) or url_for("documents"))   # errors go back where the upload started
    if core.cfg().get("doc_upload") != "1":
        flash("Document upload is switched off in Settings > Subscriber.", "error")
        return back
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a file to upload.", "error")
        return back
    original = secure_filename(upload.filename) or "file"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED:
        flash("This file type is not allowed. Allowed: %s." % ", ".join(sorted(e.lstrip(".") for e in ALLOWED)), "error")
        return back
    head = upload.stream.read(8)
    upload.stream.seek(0)
    if ext in SIGNATURES and not any(head.startswith(sig) for sig in SIGNATURES[ext]):
        flash("The file content does not match its %s extension." % ext, "error")
        return back
    sub_id = None
    if f.get("username", "").strip():
        sub = q("SELECT id, created_by FROM subscribers WHERE username = %s", (f["username"].strip(),), one=True)
        names = core.my_downline()
        if not sub or (names is not None and sub["created_by"] not in names):
            flash("No subscriber of yours has the username %s." % f["username"].strip(), "error")
            return back
        sub_id = sub["id"]
    category = f.get("category") if f.get("category") in CATEGORIES else "Other"
    stored = secrets.token_hex(16) + ext
    path = os.path.join(docs_dir(), stored)
    upload.save(path)
    size = os.path.getsize(path)
    insert("INSERT INTO documents (title, category, subscriber_id, filename, stored, size, uploaded_by) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s)",
           ((f.get("title", "").strip() or original)[:200], category, sub_id, original[:255], stored, size,
            g.me["username"]))
    audit("document_uploaded", f.get("username", "").strip() or original, original)
    commit()
    flash("Document uploaded.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("documents"))


@route("/documents/<int:did>/download", module="documents")
def document_download(did):
    d = _doc(did)
    path = os.path.join(docs_dir(), os.path.basename(d["stored"]))
    if not os.path.isfile(path):
        abort(404)
    ext = os.path.splitext(d["filename"])[1].lower()
    inline = request.args.get("inline") == "1" and ext in INLINE
    return send_file(path, mimetype=mimetypes.guess_type(d["filename"])[0] or "application/octet-stream",
                     as_attachment=not inline, download_name=d["filename"])


@route("/documents/<int:did>/delete", module="documents", methods=["POST"])
def document_delete(did):
    d = _doc(did)
    if d["uploaded_by"] != g.me["username"] and g.me["role"] not in ("admin", "manager"):
        abort(403)
    x("DELETE FROM documents WHERE id = %s", (did,))
    audit("document_deleted", d["filename"])
    commit()
    try:
        os.remove(os.path.join(docs_dir(), os.path.basename(d["stored"])))
    except OSError:
        pass
    flash("Document deleted.", "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("documents"))
