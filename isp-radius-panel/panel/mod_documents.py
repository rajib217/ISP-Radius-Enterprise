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
    if request.args.get("sub", "").isdigit():
        where.append("d.subscriber_id = %s")
        params.append(int(request.args["sub"]))
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    join = " FROM documents d LEFT JOIN subscribers s ON s.id = d.subscriber_id"
    total = q("SELECT COUNT(*) AS c" + join + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT d.*, s.username AS sub_username, s.id AS sid" + join + cond +
             " ORDER BY d.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    for r in rows:
        r["inline"] = os.path.splitext(r["filename"])[1].lower() in INLINE
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("documents.html", rows=rows, pg=pg, keep=keep, categories=CATEGORIES,
                           allowed=", ".join(sorted(e.lstrip(".") for e in ALLOWED)))


@route("/documents/upload", module="documents", methods=["POST"])
def document_upload():
    if core.cfg().get("doc_upload") != "1":
        flash("Document upload is switched off in Settings > Subscriber.", "error")
        return redirect(url_for("documents"))
    f = request.form
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("documents"))
    original = secure_filename(upload.filename) or "file"
    ext = os.path.splitext(original)[1].lower()
    if ext not in ALLOWED:
        flash("This file type is not allowed. Allowed: %s." % ", ".join(sorted(e.lstrip(".") for e in ALLOWED)), "error")
        return redirect(url_for("documents"))
    head = upload.stream.read(8)
    upload.stream.seek(0)
    if ext in SIGNATURES and not any(head.startswith(sig) for sig in SIGNATURES[ext]):
        flash("The file content does not match its %s extension." % ext, "error")
        return redirect(url_for("documents"))
    sub_id = None
    if f.get("username", "").strip():
        sub = q("SELECT id FROM subscribers WHERE username = %s", (f["username"].strip(),), one=True)
        if not sub:
            flash("No subscriber has the username %s." % f["username"].strip(), "error")
            return redirect(url_for("documents"))
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
    audit("document_uploaded", original)
    commit()
    flash("Document uploaded.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("documents"))


@route("/documents/<int:did>/download", module="documents")
def document_download(did):
    d = q("SELECT * FROM documents WHERE id = %s", (did,), one=True) or abort(404)
    path = os.path.join(docs_dir(), os.path.basename(d["stored"]))
    if not os.path.isfile(path):
        abort(404)
    ext = os.path.splitext(d["filename"])[1].lower()
    inline = request.args.get("inline") == "1" and ext in INLINE
    return send_file(path, mimetype=mimetypes.guess_type(d["filename"])[0] or "application/octet-stream",
                     as_attachment=not inline, download_name=d["filename"])


@route("/documents/<int:did>/delete", module="documents", methods=["POST"])
def document_delete(did):
    d = q("SELECT * FROM documents WHERE id = %s", (did,), one=True) or abort(404)
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
