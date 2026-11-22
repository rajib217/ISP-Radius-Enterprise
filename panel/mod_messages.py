"""Email and SMS: compose, templates, and the message log (the sender service delivers the queue)."""
import re
import secrets

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

import core
import msglib
import sendlib
from core import BASE_FROM, EMAIL_RE, audit, cfg, commit, get_db, insert, pager, q, route, x

SUB_COLS = "s.id, s.username, s.full_name, s.phone, s.email, s.expiry_date, p.label AS plan_label, p.price"
BATCH_RE = re.compile(r"^[0-9a-f]{12}$")
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_ \-]{2,64}$")
MAX_RECIPIENTS = 50000


def _recipient_where(src, channel):
    where, params = core.sub_filters(src)
    has = "s.phone <> ''" if channel == "sms" else "s.email <> ''"
    return ((where + " AND " + has) if where else (" WHERE " + has)), params


def _templates(kind):
    """System templates (with defaults filled in) followed by the custom ones."""
    rows = q("SELECT id, name, subject, body FROM message_templates WHERE kind = %s ORDER BY name", (kind,))
    custom = {r["name"]: r for r in rows}
    out = []
    for name in msglib.SYSTEM_TEMPLATE_NAMES:
        t = custom.get(name)
        d = msglib.DEFAULT_TEMPLATES[(kind, name)]
        out.append({"id": t["id"] if t else None, "name": name, "system": True, "custom": bool(t),
                    "subject": (t or d)["subject"], "body": (t or d)["body"]})
    out += [dict(r, system=False, custom=True) for r in rows if r["name"] not in msglib.SYSTEM_TEMPLATE_NAMES]
    return out


def _gateway_ready(channel, s):
    return sendlib.sms_ready(s) if channel == "sms" else bool(s["smtp_host"])


# ===================================================================== compose
def _compose(channel):
    s = cfg()
    form = {"audience": "filter", "plan": request.args.get("plan", ""), "area": request.args.get("area", ""),
            "status": request.args.get("status", ""), "username": request.args.get("username", ""),
            "numbers": "", "subject": "", "body": ""}
    if form["username"]:
        form["audience"] = "single"
    errors = []
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "").strip() for k in form}
        form["audience"] = form["audience"] if form["audience"] in ("filter", "single", "numbers") else "filter"
        if not form["body"]:
            errors.append("Write the message.")
        elif channel == "sms" and len(form["body"]) > 1000:
            errors.append("An SMS can be at most 1000 characters.")
        elif len(form["body"]) > 10000:
            errors.append("The message is too long.")
        if channel == "email" and not 1 <= len(form["subject"]) <= 200:
            errors.append("Enter a subject (up to 200 characters).")
        targets = []   # (address, subscriber row or None)
        skipped = 0
        if not errors:
            if form["audience"] == "numbers" and channel == "sms":
                seen = set()
                for raw in re.split(r"[\s,;]+", f.get("numbers", "")):
                    if not raw:
                        continue
                    num = msglib.normalize_phone(raw, s["sms_country_code"], s["sms_number_format"])
                    if num and num not in seen:
                        seen.add(num)
                        targets.append((num, None))
                    elif not num:
                        skipped += 1
            elif form["audience"] == "single":
                row = q("SELECT " + SUB_COLS + BASE_FROM + " WHERE s.username = %s", (form["username"],), one=True)
                if not row:
                    errors.append("No subscriber has that username.")
                else:
                    targets.append((None, row))
            else:
                where, params = _recipient_where(f, channel)
                rows = q("SELECT " + SUB_COLS + BASE_FROM + where + " ORDER BY s.id LIMIT %d" % (MAX_RECIPIENTS + 1),
                         params)
                if len(rows) > MAX_RECIPIENTS:
                    errors.append("More than %d recipients. Narrow the filter." % MAX_RECIPIENTS)
                targets = [(None, r) for r in rows]
        if not errors:
            batch, out = secrets.token_hex(6), []
            for addr, sub in targets:
                if sub is not None:
                    addr = (msglib.normalize_phone(sub["phone"], s["sms_country_code"], s["sms_number_format"])
                            if channel == "sms" else (sub["email"] or "").strip())
                    if channel == "email" and addr and not EMAIL_RE.match(addr):
                        addr = None
                if not addr:
                    skipped += 1
                    continue
                ctx = msglib.sub_context(sub or {}, s["isp_name"])
                out.append((channel, addr, msglib.render(form["subject"], ctx), msglib.render(form["body"], ctx),
                            sub["id"] if sub else None, None, batch, g.me["username"]))
            if not out:
                errors.append("There is nobody to send to: no valid %s found." %
                              ("phone number" if channel == "sms" else "email address"))
            else:
                with get_db().cursor() as cur:
                    msglib.enqueue_many(cur, out)
                audit("message_queued", channel, "%d messages, batch %s" % (len(out), batch))
                commit()
                rate = int(s["sms_rate" if channel == "sms" else "email_rate"] or 0) or 1
                flash("%d message%s queued%s. They are sent in the background, about %d per minute." %
                      (len(out), "" if len(out) == 1 else "s", " (%d skipped)" % skipped if skipped else "", rate),
                      "success")
                return redirect(url_for("messages", batch=batch))
        for e in errors:
            flash(e, "error")
    return render_template("compose.html", channel=channel, form=form, errors=errors, tpls=_templates(channel),
                           plans=core.filter_plans(q("SELECT id, label FROM plans ORDER BY download_mbps, label")),
                           areas=core.area_names(), variables=msglib.VARIABLES, ready=_gateway_ready(channel, s),
                           sender=core.service_state("isp-panel-sender"))


@route("/sms/compose", module="sms", methods=["GET", "POST"])
def sms_compose():
    return _compose("sms")


@route("/email/compose", module="email", methods=["GET", "POST"])
def email_compose():
    return _compose("email")


@route("/api/recipients", module=("sms", "email"))
def api_recipients():
    channel = "email" if request.args.get("channel") == "email" else "sms"
    if not core.can(channel):
        abort(403)
    where, params = _recipient_where(request.args, channel)
    return jsonify(count=q("SELECT COUNT(*) AS c" + BASE_FROM + where, params, one=True)["c"])


# ===================================================================== templates
def _kind():
    return "email" if request.values.get("kind") == "email" else "sms"


@route("/templates", module=("sms", "email"))
def templates_page():
    kind = _kind()
    if not core.can(kind):
        abort(403)
    return render_template("templates_page.html", kind=kind, rows=_templates(kind), variables=msglib.VARIABLES)


@route("/templates/save", module=("sms", "email"), methods=["POST"])
def template_save():
    kind = _kind()
    if not core.can(kind, "edit"):
        abort(403)
    f = request.form
    name, subject, body = f.get("name", "").strip(), f.get("subject", "").strip()[:200], f.get("body", "").strip()
    if not TEMPLATE_NAME_RE.match(name) or not body or len(body) > 5000:
        flash("A template needs a name (2-64 letters, numbers, space, _ -) and a message under 5000 characters.", "error")
        return redirect(url_for("templates_page", kind=kind))
    if kind == "sms":
        subject = ""
    x("INSERT INTO message_templates (kind, name, subject, body) VALUES (%s, %s, %s, %s) "
      "ON DUPLICATE KEY UPDATE subject = VALUES(subject), body = VALUES(body)", (kind, name, subject, body))
    audit("template_saved", "%s/%s" % (kind, name))
    commit()
    flash("Template saved.", "success")
    return redirect(url_for("templates_page", kind=kind))


@route("/templates/<int:tid>/delete", module=("sms", "email"), methods=["POST"])
def template_delete(tid):
    row = q("SELECT kind, name FROM message_templates WHERE id = %s", (tid,), one=True) or abort(404)
    if not core.can(row["kind"], "edit"):
        abort(403)
    x("DELETE FROM message_templates WHERE id = %s", (tid,))
    audit("template_deleted", "%s/%s" % (row["kind"], row["name"]))
    commit()
    flash("Template deleted%s." % (" - the built-in text is used again" if row["name"] in msglib.SYSTEM_TEMPLATE_NAMES
                                    else ""), "success")
    return redirect(url_for("templates_page", kind=row["kind"]))


# ===================================================================== message log
def _log_scope(src, edit=False):
    allowed = [c for c in ("sms", "email") if core.can(c, "edit" if edit else "view")]
    where, params = ["o.channel IN (%s)" % ",".join(["%s"] * len(allowed))], list(allowed)
    if src.get("channel") in allowed:
        where.append("o.channel = %s")
        params.append(src["channel"])
    if BATCH_RE.match(src.get("batch", "")):
        where.append("o.batch = %s")
        params.append(src["batch"])
    return where, params


@route("/messages", module=("sms", "email"))
def messages():
    where, params = _log_scope(request.args)
    counts = {r["status"]: r["c"] for r in q(
        "SELECT o.status, COUNT(*) AS c FROM outbox o WHERE " + " AND ".join(where) + " GROUP BY o.status", params)}
    if request.args.get("status") in ("pending", "sent", "failed"):
        where.append("o.status = %s")
        params.append(request.args["status"])
    text = request.args.get("q", "").strip()
    if text:
        where.append("o.to_addr LIKE %s")
        params.append(text + "%")
    cond = " WHERE " + " AND ".join(where)
    total = q("SELECT COUNT(*) AS c FROM outbox o" + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT o.*, s.id AS sid FROM outbox o LEFT JOIN subscribers s ON s.id = o.subscriber_id" + cond +
             " ORDER BY o.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("messages.html", rows=rows, pg=pg, counts=counts, keep=keep,
                           sender=core.service_state("isp-panel-sender"))


@route("/messages/retry", module=("sms", "email"), methods=["POST"])
def messages_retry():
    where, params = _log_scope(request.form, edit=True)
    if where[0].endswith("IN ()") or not params:
        abort(403)
    n_rows = x("UPDATE outbox o SET o.status = 'pending', o.tries = 0, o.error = '' WHERE o.status = 'failed' AND " +
               " AND ".join(where), params)
    audit("messages_retried", "", "%d" % n_rows)
    commit()
    flash("%d failed message%s put back in the queue." % (n_rows, "" if n_rows == 1 else "s"), "success")
    return redirect(url_for("messages", **{k: v for k, v in request.form.items() if k in ("channel", "batch")}))


@route("/messages/cancel", module=("sms", "email"), methods=["POST"])
def messages_cancel():
    where, params = _log_scope(request.form, edit=True)
    if not params:
        abort(403)
    n_rows = x("DELETE o FROM outbox o WHERE o.status = 'pending' AND " + " AND ".join(where), params)
    audit("messages_cancelled", "", "%d" % n_rows)
    commit()
    flash("%d waiting message%s cancelled." % (n_rows, "" if n_rows == 1 else "s"), "success")
    return redirect(url_for("messages", **{k: v for k, v in request.form.items() if k in ("channel", "batch")}))
