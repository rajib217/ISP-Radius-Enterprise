"""Accounting > bKash Payments: every bKash checkout the panel started, with its bKash result.
(Accounting > bKash SMS Payments, money read from bKash's SMS, is at the end of this file.)

  portal_payments -> "Customer renewal"   (customers renewing in the portal)
  topups          -> "Balance top-up"     (resellers adding balance with Add Balance)

Only gateway payments are listed (with bKash's Trx ID and the status: Completed, Pending, Failed, Cancelled). Payments
staff typed in by hand with the method "bKash" are in Payments / Ledger Reports, not here.

Office staff can also add by hand bKash money a reseller / sub-reseller / retailer sent to the company's number (Trx ID
and note): it goes into their balance (user_ledger, method bkash) and shows here as "bKash received (by hand)".

Permissions (Users > Roles > Accounting > bKash Payments; not tied to the Accounting module level), same as the ledger:
  * staff with Accounting access see everything; Export needs the Export tick (or Accounting edit)
  * resellers / sub-resellers / retailers only when "Module" is ticked for their role: their own top-ups, the people
    below them, and those people's customers; Export needs the Export tick."""
import csv
import io
import re
import secrets
from datetime import date, datetime, timedelta

from flask import Response, abort, flash, g, redirect, render_template, request, url_for

import core
from core import audit, commit, insert, q, route
from mod_ledger import _safe, _xlsx

STATUSES = [("completed", "Completed"), ("pending", "Pending"), ("failed", "Failed"), ("cancelled", "Cancelled")]
KINDS = [("renewal", "Customer renewal"), ("topup", "Balance top-up"), ("manual", "bKash received (by hand)")]
PTYPES = [("reseller", "Resellers"), ("subreseller", "Sub-resellers"), ("retailer", "Retailers")]
TRX_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")
MANUAL = "manual"                 # topups.payment_id of a payment added by hand
EXPORT_CAP = 100000


def _allowed():
    me = g.me
    if me["role"] == "admin":
        return "all"
    if me["role"] in core.SELLER_ROLES:
        return "mine" if core.can_fn("bkp.module") else None
    return "all" if (core.can("accounting") or core.can_fn("bkp.module")) else None


def _can_export(scope):
    if g.me["role"] == "admin" or core.can_fn("bkp.export"):
        return True
    return scope == "all" and core.can("accounting", "edit")


def _filters():
    a = request.args
    today = date.today()
    try:
        start = date.fromisoformat(a.get("from") or "")
    except ValueError:
        start = today - timedelta(days=30)
    try:
        end = date.fromisoformat(a.get("to") or "")
    except ValueError:
        end = today
    if end < start:
        start, end = end, start
    ptype = a.get("ptype") if a.get("ptype") in dict(PTYPES) else ""
    return {"from": start, "to": end, "q": (a.get("q") or "").strip()[:64],
            "status": a.get("status") if a.get("status") in dict(STATUSES) else "",
            "kind": a.get("kind") if a.get("kind") in dict(KINDS) else "",
            "ptype": ptype, "owner": (a.get("owner") or "").strip()[:64]}


def _parts(f, scope, with_status=True):
    lo = datetime.combine(f["from"], datetime.min.time())
    hi = datetime.combine(f["to"] + timedelta(days=1), datetime.min.time())
    names = list(core.my_downline() or []) if scope == "mine" else None
    if names is not None and not names:
        return []
    like = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if f["q"] else None
    out = []

    def common(alias, who):
        w, args = " WHERE %s.created_at >= %%s AND %s.created_at < %%s" % (alias, alias), [lo, hi]
        if names is not None:
            w += " AND %s IN (%s)" % (who, ",".join(["%s"] * len(names)))
            args += names
        if with_status and f["status"]:
            w += " AND %s.status = %%s" % alias
            args.append(f["status"])
        return w, args

    if f["kind"] in ("", "renewal"):
        w, args = common("pp", "s.created_by")
        if f["owner"]:                    # one reseller / sub-reseller / retailer: their customers
            w += " AND s.created_by = %s"
            args.append(f["owner"])
        elif f["ptype"]:                  # a whole tier: the customers of everyone of that type
            w += " AND s.created_by IN (SELECT username FROM panel_admins WHERE role = %s)"
            args.append(f["ptype"])
        sql = ("SELECT 'R' AS src, pp.id AS sid, pp.created_at AS created, 'Customer renewal' AS kind, s.username AS account, "
               "'subscriber' AS role, pp.amount AS amount, pp.fee AS fee, pp.status AS status, pp.trx_id AS trx, "
               "pp.payment_id AS payment_id, pp.message AS message, pp.days AS days, pp.payment_ref AS receipt, NULL AS note "
               "FROM portal_payments pp JOIN subscribers s ON s.id = pp.subscriber_id" + w)
        if like:
            sql += " AND (s.username LIKE %s OR pp.trx_id LIKE %s OR pp.payment_id LIKE %s)"
            args += [like, like, like]
        out.append((sql, args, "pp.created_at"))
    if f["kind"] in ("", "topup", "manual"):
        w, args = common("t", "a.username")
        if f["kind"] == "topup":
            w += " AND t.payment_id <> '%s'" % MANUAL
        elif f["kind"] == "manual":
            w += " AND t.payment_id = '%s'" % MANUAL
        if f["owner"]:
            w += " AND a.username = %s"
            args.append(f["owner"])
        elif f["ptype"]:
            w += " AND a.role = %s"
            args.append(f["ptype"])
        sql = ("SELECT 'T' AS src, t.id AS sid, t.created_at AS created, "
               "IF(t.payment_id = '" + MANUAL + "', 'bKash received (by hand)', 'Balance top-up') AS kind, a.username AS account, "
               "a.role AS role, t.amount AS amount, t.fee AS fee, t.status AS status, t.trx_id AS trx, t.payment_id AS payment_id, "
               "t.message AS message, NULL AS days, NULL AS receipt, t.note AS note "
               "FROM topups t JOIN panel_admins a ON a.id = t.user_id" + w)
        if like:                          # the Trx ID, the account, its name, the note or who added it
            sql += (" AND (a.username LIKE %s OR a.full_name LIKE %s OR t.trx_id LIKE %s OR t.payment_id LIKE %s "
                    "OR t.note LIKE %s OR t.message LIKE %s)")
            args += [like] * 6
        out.append((sql, args, "t.created_at"))
    return out


def _summary(f, scope):
    """Totals for the chosen period / type / search, per status (the status filter itself is ignored here)."""
    sums = {k: {"n": 0, "amount": 0.0, "fee": 0.0} for k, _ in STATUSES}
    for sql, args, _ in _parts(f, scope, with_status=False):
        for r in q("SELECT status, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount, COALESCE(SUM(fee), 0) AS fee "
                   "FROM (" + sql + ") t GROUP BY status", args):
            if r["status"] in sums:
                sums[r["status"]]["n"] += int(r["n"])
                sums[r["status"]]["amount"] += float(r["amount"])
                sums[r["status"]]["fee"] += float(r["fee"])
    return sums


def _count(parts):
    return sum(q("SELECT COUNT(*) AS c FROM (" + sql + ") t", args, one=True)["c"] for sql, args, _ in parts)


def _page(parts, limit, offset):
    if not parts:
        return []
    need = limit + offset
    union = " UNION ALL ".join("(%s ORDER BY %s DESC LIMIT %d)" % (sql, col, need) for sql, _, col in parts)
    rows = q("SELECT * FROM (" + union + ") t ORDER BY created DESC, sid DESC LIMIT %s OFFSET %s",
             [a for _, ar, _ in parts for a in ar] + [limit, offset])
    for r in rows:
        r["total"] = float(r["amount"] or 0) + float(r["fee"] or 0)
        r["code"] = "%s-%s" % ("PRT" if r["src"] == "R" else "TOP", r["sid"])
    return rows


@route("/bkash-payments")
def bkash_payments():
    scope = _allowed() or abort(403)
    f = _filters()
    parts = _parts(f, scope)
    pg = core.pager(_count(parts))
    rows = _page(parts, pg["per"], pg["offset"])
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("bkash_payments.html", rows=rows, pg=pg, f=f, keep=keep, statuses=STATUSES, kinds=KINDS,
                           scope=scope, sums=_summary(f, scope), can_export=_can_export(scope), ptypes=PTYPES,
                           partners=_partners(scope), can_add=_can_add(), today=date.today())


def _partners(scope):
    """Resellers / sub-resellers / retailers for the account pickers: everyone for the office, the downline for a seller."""
    names = list(core.my_downline() or []) if scope == "mine" else None
    sql = ("SELECT id, username, full_name, role, balance, is_active FROM panel_admins WHERE role IN ('reseller', 'subreseller', "
           "'retailer')")
    if names is not None:
        if not names:
            return []
        sql += " AND username IN (%s)" % ",".join(["%s"] * len(names))
    return q(sql + " ORDER BY FIELD(role, 'reseller', 'subreseller', 'retailer'), username", names or [])


def _can_add():
    """Office only: an administrator, staff with Accounting edit, or the Add By Hand tick. Never a seller or their staff."""
    if g.me["role"] == "admin":
        return True
    if g.me["role"] in core.SELLER_ROLES or core.my_downline() is not None:
        return False
    return core.can("accounting", "edit") or core.can_fn("bkp.add")


def trx_used(trx, skip=()):
    """Where this bKash Trx ID was already booked (a top-up, a portal payment, an SMS payment, a collection), or ''.
    skip: tables not to look in (the caller's own, which has its own duplicate guard)."""
    if not trx:
        return ""
    for table, label in (("topups", "a top-up"), ("portal_payments", "a customer's portal payment"),
                         ("sms_payments", "a bKash SMS payment"), ("collections", "a bill collection")):
        if table in skip:
            continue
        try:
            if q("SELECT 1 AS x FROM " + table + " WHERE trx_id = %s LIMIT 1", (trx,), one=True):
                return label
        except Exception:             # a table that does not exist yet
            continue
    return ""


@route("/bkash-payments/add", methods=["POST"])
def bkash_payment_add():
    """Book bKash money a reseller / sub-reseller / retailer sent: their balance goes up, the ledger says so, and it is
    listed here (searchable by Trx ID, account and note)."""
    if not _can_add():
        abort(403)
    f = request.form
    back = url_for("bkash_payments")
    who = q("SELECT id, username, role, is_active FROM panel_admins WHERE id = %s AND role IN ('reseller', 'subreseller', 'retailer')",
            (int(f["user_id"]) if (f.get("user_id") or "").isdigit() else 0,), one=True)
    trx = (f.get("trx_id") or "").strip().upper()
    note = " ".join((f.get("note") or "").split())[:200]
    try:
        amount = round(float(f.get("amount") or 0), 2)
    except ValueError:
        amount = 0
    err = ""
    if not who:
        err = "Choose the reseller, sub-reseller or retailer who sent the money."
    elif not 0 < amount < 10 ** 8:
        err = "Enter the amount received."
    elif not TRX_RE.match(trx):
        err = "Enter the bKash Trx ID (6-20 letters / digits, from the bKash message)."
    else:
        used = trx_used(trx)
        if used:
            err = "Trx ID %s is already booked as %s. Nothing was added." % (trx, used)
    if err:
        flash(err, "error")
        return redirect(back)
    tid = insert("INSERT INTO topups (user_id, amount, fee, method, note, token, payment_id, trx_id, status, message) "
                 "VALUES (%s, %s, 0, 'bkash', %s, %s, %s, %s, 'completed', %s)",
                 (who["id"], amount, note, secrets.token_hex(16), MANUAL, trx, ("Added by %s" % g.me["username"])[:255]))
    with core.get_db().cursor() as cur:
        cur.execute("UPDATE panel_admins SET balance = balance + %s WHERE id = %s", (amount, who["id"]))
        cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (who["id"],))
        after = float(cur.fetchone()["balance"])
        cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) VALUES (%s, %s, %s, 'bkash', %s, %s)",
                    (who["id"], amount, after, ("bKash %s (by hand)%s" % (trx, (": " + note) if note else ""))[:255], g.me["username"]))
    audit("bkash_manual", who["username"], "%.2f bKash %s%s" % (amount, trx, (" - " + note) if note else ""))
    commit()
    flash("%.2f received from %s (Trx ID %s). Their balance is now %.2f." % (amount, who["username"], trx, after), "success")
    return redirect(url_for("bkash_payments", q=trx, kind="manual", **{"from": date.today().isoformat()}))


def _export():
    scope = _allowed() or abort(403)
    if not _can_export(scope):
        abort(403)
    f = _filters()
    rows = _page(_parts(f, scope), EXPORT_CAP, 0)
    head = ["ID", "Type", "Account", "Role", "Amount", "bKash fee", "Total", "Status", "bKash Trx ID", "Payment ID", "Message", "Created"]
    data = [[r["code"], r["kind"], r["account"], r["role"], float(r["amount"] or 0), float(r["fee"] or 0), r["total"],
             r["status"].capitalize(), r["trx"] or "", r["payment_id"] or "", r["message"] or "",
             r["created"].strftime("%Y-%m-%d %H:%M")] for r in rows]
    audit("export", "bkash payments", "%d rows, %s to %s" % (len(data), f["from"], f["to"]))
    commit()
    return head, data, f


@route("/bkash-payments.csv")
def bkash_payments_csv():
    head, data, f = _export()
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(head)
    for row in data:
        w.writerow([_safe(c) for c in row])
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=bkash-payments-%s-to-%s.csv" % (f["from"], f["to"])})


@route("/bkash-payments.xlsx")
def bkash_payments_xlsx():
    head, data, f = _export()
    return Response(_xlsx(head, data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": "attachment; filename=bkash-payments-%s-to-%s.xlsx" % (f["from"], f["to"])})


# ===================================================================== Accounting > bKash SMS Payments
# Money received on the bKash number and read from bKash's SMS (phone forwarder app or the USB GSM modem), what the panel
# did with it (renewed, added to the balance, waiting for a match, ignored). Same permission pattern as above, its own
# ticks (Users > Roles > Accounting > bKash SMS Payments): staff with Accounting access see all and book the unmatched
# ones (admin); resellers / sub-resellers / retailers see their own customers' payments (and their downline's) only.
SMS_STATES = [("renewed", "Renewed"), ("balance", "Added to balance"), ("unmatched", "Needs a match"), ("ignored", "Ignored")]


def _sms_allowed():
    me = g.me
    if me["role"] == "admin":
        return "all"
    if me["role"] in core.SELLER_ROLES:
        return "mine" if core.can_fn("bks.module") else None
    return "all" if (core.can("accounting") or core.can_fn("bks.module")) else None


def _sms_can_export(scope):
    if g.me["role"] == "admin" or core.can_fn("bks.export"):
        return True
    return scope == "all" and core.can("accounting", "edit")


def _sms_filters():
    f = _filters()
    a = request.args
    f["status"] = a.get("status") if a.get("status") in dict(SMS_STATES) else ""
    f["source"] = a.get("source") if a.get("source") in ("phone", "modem") else ""
    return f


def _sms_where(f, scope, with_status=True):
    lo = datetime.combine(f["from"], datetime.min.time())
    hi = datetime.combine(f["to"] + timedelta(days=1), datetime.min.time())
    w = " WHERE p.status <> 'working' AND COALESCE(p.paid_at, p.created_at) >= %s AND COALESCE(p.paid_at, p.created_at) < %s"
    args = [lo, hi]
    if scope == "mine":
        names = list(core.my_downline() or [])
        if not names:
            return None, []
        # only payments booked to (or suggested for) one of their customers; an unknown payer's money stays with staff
        w += " AND p.status IN ('renewed', 'balance') AND s.created_by IN (%s)" % ",".join(["%s"] * len(names))
        args += names
    if with_status and f["status"]:
        w += " AND p.status = %s"
        args.append(f["status"])
    if f["source"]:
        w += " AND p.source = %s"
        args.append(f["source"])
    if f["q"]:
        like = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        w += " AND (p.trx_id LIKE %s OR p.payer LIKE %s OR p.ref LIKE %s OR p.username LIKE %s OR s.username LIKE %s)"
        args += [like] * 5
    return w, args


SMS_FROM = " FROM sms_payments p LEFT JOIN subscribers s ON s.id = p.subscriber_id"


@route("/bkash-sms-payments")
def bksms_payments():
    scope = _sms_allowed() or abort(403)
    f = _sms_filters()
    w, args = _sms_where(f, scope)
    total = q("SELECT COUNT(*) AS c" + SMS_FROM + w, args, one=True)["c"] if w else 0
    pg = core.pager(total)
    rows = q("SELECT p.*, s.username AS sub_name, s.created_by AS owner" + SMS_FROM + w +
             " ORDER BY COALESCE(p.paid_at, p.created_at) DESC, p.id DESC LIMIT %s OFFSET %s", args + [pg["per"], pg["offset"]]) if total else []
    sums = {k: {"n": 0, "amount": 0.0} for k, _ in SMS_STATES}
    w2, args2 = _sms_where(f, scope, with_status=False)
    if w2:
        for r in q("SELECT p.status, COUNT(*) AS n, COALESCE(SUM(p.amount), 0) AS amount" + SMS_FROM + w2 + " GROUP BY p.status", args2):
            if r["status"] in sums:
                sums[r["status"]] = {"n": int(r["n"]), "amount": float(r["amount"])}
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("bksms_payments.html", rows=rows, pg=pg, f=f, keep=keep, states=SMS_STATES, sums=sums, scope=scope,
                           can_export=_sms_can_export(scope), can_book=g.me["role"] == "admin")


def _sms_export():
    scope = _sms_allowed() or abort(403)
    if not _sms_can_export(scope):
        abort(403)
    f = _sms_filters()
    w, args = _sms_where(f, scope)
    rows = q("SELECT p.*, s.username AS sub_name" + SMS_FROM + w + " ORDER BY COALESCE(p.paid_at, p.created_at) DESC, p.id DESC "
             "LIMIT %d" % EXPORT_CAP, args) if w else []
    names = dict(SMS_STATES)
    head = ["ID", "Paid at", "bKash Trx ID", "Amount", "From (bKash number)", "Reference", "Customer", "Result", "Message", "Source", "Booked by"]
    data = [["SMS-%d" % r["id"], (r["paid_at"] or r["created_at"]).strftime("%Y-%m-%d %H:%M"), r["trx_id"], float(r["amount"] or 0),
             r["payer"] or "", r["ref"] or "", r["sub_name"] or r["username"] or "", names.get(r["status"], r["status"]),
             r["message"] or "", "Modem" if r["source"] == "modem" else "Phone", r["done_by"] or ""] for r in rows]
    audit("export", "bkash sms payments", "%d rows, %s to %s" % (len(data), f["from"], f["to"]))
    commit()
    return head, data, f


@route("/bkash-sms-payments.csv")
def bksms_payments_csv():
    head, data, f = _sms_export()
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow(head)
    for row in data:
        w.writerow([_safe(c) for c in row])
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=bkash-sms-payments-%s-to-%s.csv" % (f["from"], f["to"])})


@route("/bkash-sms-payments.xlsx")
def bksms_payments_xlsx():
    head, data, f = _sms_export()
    return Response(_xlsx(head, data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": "attachment; filename=bkash-sms-payments-%s-to-%s.xlsx" % (f["from"], f["to"])})
