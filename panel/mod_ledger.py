"""Accounting > Ledger Reports: every money movement in one list, built from the records the panel already keeps.

  payments     -> "Invoice"  (the customer paid for a package; amount shown as a charge to the customer)
               -> "Profit"   (what the selling reseller kept: customer price minus their package cost)
  sub_ledger   -> "Topup" / "Balance Used"   (the customer's own balance, with balance before and after)
  user_ledger  -> "Package Cost" / "Balance Topup" / "Balance Add" / "Balance Deduct"
                  (a reseller's / staff member's balance, with balance before and after)

Permissions (Users > Roles > Accounting > Ledger Reports; not tied to the Accounting module level):
  * staff with Accounting access see everything; Export needs the Export tick (or Accounting edit)
  * resellers / sub-resellers / retailers see it only when "Module" is ticked for their role, and then only their own
    account, the people below them and those people's customers; Export needs the Export tick.

Nothing is written here: history from before this page existed shows too."""
import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

from flask import Response, abort, g, render_template, request

import core
from core import audit, commit, q, route

TYPES = [  # (key, label, source)
    ("invoice", "Invoice", "P"), ("profit", "Profit", "R"),
    ("topup", "Topup", "S+"), ("used", "Balance Used", "S-"),
    ("cost", "Package Cost", "Ua"), ("utopup", "Balance Topup", "Ub"),
    ("add", "Balance Add", "U+"), ("deduct", "Balance Deduct", "U-"),
]
PREFIX = {"P": "INV", "R": "PRF", "S": "SBL", "U": "USL"}
EXPORT_CAP = 100000


def _allowed():
    me = g.me
    if me["role"] == "admin":
        return "all"
    if me["role"] in core.SELLER_ROLES:
        return "mine" if core.can_fn("led.module") else None
    return "all" if (core.can("accounting") or core.can_fn("led.module")) else None


def _can_export(scope):
    if g.me["role"] == "admin" or core.can_fn("led.export"):
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
    kind = a.get("type") if a.get("type") in dict((k, 1) for k, _, _ in TYPES) else ""
    term = (a.get("q") or "").strip()[:64]
    return {"from": start, "to": end, "type": kind, "q": term}


def _parts(f, scope):
    """One SELECT per source, each already filtered (date, type, search, seller scope). Columns are the same in all."""
    lo, hi = datetime.combine(f["from"], datetime.min.time()), datetime.combine(f["to"] + timedelta(days=1), datetime.min.time())
    names = list(core.my_downline() or []) if scope == "mine" else None
    like = "%" + f["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" if f["q"] else None
    src = dict((k, s) for k, _, s in TYPES).get(f["type"], "")
    out = []

    def inlist(col):
        return (" AND %s IN (%s)" % (col, ",".join(["%s"] * len(names))), list(names)) if names is not None else ("", [])

    if names is not None and not names:
        return []
    # payments: Invoice
    if src in ("", "P"):
        w, p = inlist("s.created_by")
        sql = ("SELECT 'P' AS src, p.id AS sid, p.paid_at AS created, 'Invoice' AS kind, p.username AS p_user, 'subscriber' AS p_role, "
               "p.received_by AS part_user, -p.amount AS amount, p.id AS ref_id, 'Invoice' AS ref_kind, NULL AS pbal, NULL AS nbal, "
               "p.received_by AS action_by, TRIM(CONCAT(p.method, ' ', p.note)) AS note "
               "FROM payments p LEFT JOIN subscribers s ON s.id = p.subscriber_id WHERE p.paid_at >= %s AND p.paid_at < %s" + w)
        args = [lo, hi] + p
        if like:
            sql += " AND (p.username LIKE %s OR p.received_by LIKE %s OR p.note LIKE %s)"
            args += [like, like, like]
        out.append((sql, args, "p.paid_at"))
    # payments: Profit of the selling reseller
    if src in ("", "R"):
        w, p = inlist("a.username")
        sql = ("SELECT 'R' AS src, p.id AS sid, p.paid_at AS created, 'Profit' AS kind, a.username AS p_user, a.role AS p_role, "
               "p.username AS part_user, p.amount - p.cost AS amount, p.id AS ref_id, 'Invoice' AS ref_kind, NULL AS pbal, NULL AS nbal, "
               "p.received_by AS action_by, '' AS note FROM payments p JOIN panel_admins a ON a.id = p.payer_id "
               "WHERE p.cost IS NOT NULL AND p.paid_at >= %s AND p.paid_at < %s" + w)
        args = [lo, hi] + p
        if like:
            sql += " AND (p.username LIKE %s OR a.username LIKE %s)"
            args += [like, like]
        out.append((sql, args, "p.paid_at"))
    # the customer's own balance
    if src in ("", "S+", "S-"):
        w, p = inlist("s.created_by")
        sql = ("SELECT 'S' AS src, sl.id AS sid, sl.created_at AS created, IF(sl.amount >= 0, 'Topup', 'Balance Used') AS kind, "
               "s.username AS p_user, 'subscriber' AS p_role, sl.created_by AS part_user, sl.amount AS amount, sl.id AS ref_id, "
               "'Balance' AS ref_kind, sl.balance_after - sl.amount AS pbal, sl.balance_after AS nbal, sl.created_by AS action_by, sl.note AS note "
               "FROM sub_ledger sl JOIN subscribers s ON s.id = sl.subscriber_id WHERE sl.created_at >= %s AND sl.created_at < %s" + w)
        args = [lo, hi] + p
        if src == "S+":
            sql += " AND sl.amount >= 0"
        elif src == "S-":
            sql += " AND sl.amount < 0"
        if like:
            sql += " AND (s.username LIKE %s OR sl.created_by LIKE %s OR sl.note LIKE %s)"
            args += [like, like, like]
        out.append((sql, args, "sl.created_at"))
    # a reseller's / staff member's balance
    if src in ("", "Ua", "Ub", "U+", "U-"):
        w, p = inlist("a.username")
        sql = ("SELECT 'U' AS src, ul.id AS sid, ul.created_at AS created, CASE WHEN ul.method = 'activation' THEN 'Package Cost' "
               "WHEN ul.method = 'bkash' THEN 'Balance Topup' WHEN ul.amount >= 0 THEN 'Balance Add' ELSE 'Balance Deduct' END AS kind, "
               "a.username AS p_user, a.role AS p_role, '' AS part_user, ul.amount AS amount, ul.id AS ref_id, 'Ledger' AS ref_kind, "
               "ul.balance_after - ul.amount AS pbal, ul.balance_after AS nbal, ul.created_by AS action_by, ul.note AS note "
               "FROM user_ledger ul JOIN panel_admins a ON a.id = ul.user_id WHERE ul.created_at >= %s AND ul.created_at < %s" + w)
        args = [lo, hi] + p
        sql += {"Ua": " AND ul.method = 'activation'", "Ub": " AND ul.method = 'bkash'",
                "U+": " AND ul.method NOT IN ('activation', 'bkash') AND ul.amount >= 0",
                "U-": " AND ul.method NOT IN ('activation', 'bkash') AND ul.amount < 0"}.get(src, "")
        if like:
            sql += " AND (a.username LIKE %s OR ul.created_by LIKE %s OR ul.note LIKE %s)"
            args += [like, like, like]
        out.append((sql, args, "ul.created_at"))
    return out


_PARTICIPANT = [re.compile(p) for p in (r"^(?:Activation|Renewal|Auto renew) (\S+)", r"^From (\S+?)\.", r"^Balance (?:to|back from) (\S+)")]


def _finish(rows):
    """Roles for participants, the participant of a staff / reseller balance row (from its note), and display codes."""
    roles = {r["username"]: r["role"] for r in q("SELECT username, role FROM panel_admins")}
    for r in rows:
        if r["src"] == "U" and not r["part_user"]:
            for pat in _PARTICIPANT:
                m = pat.search(r["note"] or "")
                if m:
                    r["part_user"] = m.group(1)
                    break
        part = r["part_user"] or ""
        if part.startswith("portal:"):                  # paid by the customer in the portal (bKash)
            r["part_user"], r["part_role"] = part[7:], "portal"
        elif r["src"] == "R":
            r["part_role"] = "subscriber"
        elif r["src"] == "U" and part and part not in roles:
            r["part_role"] = "subscriber"                # "Activation <customer> +30 days"
        else:
            r["part_role"] = roles.get(part, "")
        r["trx"] = "%s-%s" % (PREFIX[r["src"]], r["sid"])
        r["ref"] = "#%s %s" % (r["ref_id"], r["ref_kind"])
    return rows


def _count(parts):
    total = 0
    for sql, args, _ in parts:
        total += q("SELECT COUNT(*) AS c FROM (" + sql + ") t", args, one=True)["c"]
    return total


def _page(parts, limit, offset):
    if not parts:
        return []
    need = limit + offset            # each source can give at most this many rows to the page
    union = " UNION ALL ".join("(%s ORDER BY %s DESC LIMIT %d)" % (sql, col, need) for sql, _, col in parts)
    args = [a for _, ar, _ in parts for a in ar]
    return q("SELECT * FROM (" + union + ") t ORDER BY created DESC, sid DESC LIMIT %s OFFSET %s", args + [limit, offset])


@route("/ledger")
def ledger():
    scope = _allowed() or abort(403)
    f = _filters()
    parts = _parts(f, scope)
    pg = core.pager(_count(parts))
    rows = _finish(_page(parts, pg["per"], pg["offset"]))
    keep = {k: v for k, v in request.args.items() if k != "page"}
    can_export = _can_export(scope)
    return render_template("ledger.html", rows=rows, pg=pg, f=f, keep=keep, types=TYPES, scope=scope, can_export=can_export)


def _export_rows():
    scope = _allowed() or abort(403)
    if not _can_export(scope):
        abort(403)
    f = _filters()
    rows = _finish(_page(_parts(f, scope), EXPORT_CAP, 0))
    head = ["ID", "Trx ID", "Type", "Primary", "Primary role", "Participant", "Participant role", "Amount", "Ref ID",
            "P.Balance", "N.Balance", "Created", "Action By", "Note"]
    data = [[r["sid"], r["trx"], r["kind"], r["p_user"], r["p_role"], r["part_user"] or "", r["part_role"] or "",
             float(r["amount"] or 0), r["ref"], "" if r["pbal"] is None else float(r["pbal"]),
             "" if r["nbal"] is None else float(r["nbal"]), r["created"].strftime("%Y-%m-%d %H:%M"), r["action_by"] or "",
             r["note"] or ""] for r in rows]
    audit("export", "ledger", "%d rows, %s to %s" % (len(data), f["from"], f["to"]))
    commit()
    return head, data, f


@route("/ledger.csv")
def ledger_csv():
    head, data, f = _export_rows()
    out = io.StringIO()
    out.write("﻿")                   # Excel opens UTF-8 CSV correctly with a BOM
    w = csv.writer(out)
    w.writerow(head)
    for row in data:
        w.writerow([_safe(c) for c in row])
    return Response(out.getvalue(), mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename=ledger-%s-to-%s.csv" % (f["from"], f["to"])})


def _safe(v):
    """Text that starts like a formula is prefixed with ' so a spreadsheet never runs it."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and not re.match(r"^-?\d", v):
        return "'" + v
    return v


def _xlsx(head, data):
    """A plain one-sheet .xlsx (inline strings, numbers as numbers), no extra library needed."""
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell(ref, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s"><v>%s</v></c>' % (ref, v)
        text = str(v)
        if text[:1] in ("=", "+", "-", "@") and not re.match(r"^-?\d", text):
            text = "'" + text          # never let a cell be read as a formula
        return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, escape(text))

    rows = [head] + data
    body = "".join('<row r="%d">%s</row>' % (n + 1, "".join(cell("%s%d" % (col(i), n + 1), v) for i, v in enumerate(r)))
                   for n, r in enumerate(rows))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.'
                   'openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                   'org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships"><sheets><sheet name="Ledger" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="'
                   'http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


@route("/ledger.xlsx")
def ledger_xlsx():
    head, data, f = _export_rows()
    return Response(_xlsx(head, data), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": "attachment; filename=ledger-%s-to-%s.xlsx" % (f["from"], f["to"])})
