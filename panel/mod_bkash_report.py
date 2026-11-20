"""Accounting > bKash Payments: every bKash checkout the panel started, with its bKash result.

  portal_payments -> "Customer renewal"   (customers renewing in the portal)
  topups          -> "Balance top-up"     (resellers adding balance with Add Balance)

Only gateway payments are listed (with bKash's Trx ID and the status: Completed, Pending, Failed, Cancelled). Payments
staff typed in by hand with the method "bKash" are in Payments / Ledger Reports, not here.

Permissions (Users > Roles > Accounting > bKash Payments; not tied to the Accounting module level), same as the ledger:
  * staff with Accounting access see everything; Export needs the Export tick (or Accounting edit)
  * resellers / sub-resellers / retailers only when "Module" is ticked for their role: their own top-ups, the people
    below them, and those people's customers; Export needs the Export tick."""
import csv
import io
from datetime import date, datetime, timedelta

from flask import Response, abort, g, render_template, request

import core
from core import audit, commit, q, route
from mod_ledger import _safe, _xlsx

STATUSES = [("completed", "Completed"), ("pending", "Pending"), ("failed", "Failed"), ("cancelled", "Cancelled")]
KINDS = [("renewal", "Customer renewal"), ("topup", "Balance top-up")]
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
    return {"from": start, "to": end, "q": (a.get("q") or "").strip()[:64],
            "status": a.get("status") if a.get("status") in dict(STATUSES) else "",
            "kind": a.get("kind") if a.get("kind") in dict(KINDS) else ""}


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
        sql = ("SELECT 'R' AS src, pp.id AS sid, pp.created_at AS created, 'Customer renewal' AS kind, s.username AS account, "
               "'subscriber' AS role, pp.amount AS amount, pp.fee AS fee, pp.status AS status, pp.trx_id AS trx, "
               "pp.payment_id AS payment_id, pp.message AS message, pp.days AS days, pp.payment_ref AS receipt "
               "FROM portal_payments pp JOIN subscribers s ON s.id = pp.subscriber_id" + w)
        if like:
            sql += " AND (s.username LIKE %s OR pp.trx_id LIKE %s OR pp.payment_id LIKE %s)"
            args += [like, like, like]
        out.append((sql, args, "pp.created_at"))
    if f["kind"] in ("", "topup"):
        w, args = common("t", "a.username")
        sql = ("SELECT 'T' AS src, t.id AS sid, t.created_at AS created, 'Balance top-up' AS kind, a.username AS account, "
               "a.role AS role, t.amount AS amount, t.fee AS fee, t.status AS status, t.trx_id AS trx, t.payment_id AS payment_id, "
               "t.message AS message, NULL AS days, NULL AS receipt "
               "FROM topups t JOIN panel_admins a ON a.id = t.user_id" + w)
        if like:
            sql += " AND (a.username LIKE %s OR t.trx_id LIKE %s OR t.payment_id LIKE %s)"
            args += [like, like, like]
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
                           scope=scope, sums=_summary(f, scope), can_export=_can_export(scope))


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
