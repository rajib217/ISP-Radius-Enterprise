"""Reports > BTRC Report (admin only): the BTRC "downstream" subscriber upload sheet.

One row per customer who was activated or renewed in the chosen month, with exactly the columns of BTRC's sample
(isp_sample_downstream_upload.xlsx):

  client_type, connection_type, bandwidth_distribution_point, connectivity_type   one setting each for the whole ISP
  client_username                    the PPPoE username
  activation_date                    the customer's LAST activation / renewal in that month (an Excel date)
  bandwidth_allocation               the package's speed as a plain number of Mbps (15, not "15mb"): the package's
                                     Bandwidth field, else the number in its name / label, else its download Mbps
  allocated_ip                       the fixed IP, else the IP of the customer's latest session
  division, district, thana          one setting each for the whole ISP
  address                            the customer's address, else their area
  client_mobile, client_email        from the customer record
  selling_price_bdt_excluding_vat    the customer's price without taxes / fees: their seller's price (with the
                                     subscriber profit) for a seller's customer, otherwise the package price"""
import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

from flask import Response, abort, flash, redirect, render_template, request, url_for

import core
import db
from core import admin_required, audit, commit, q, route

COLUMNS = ["client_type", "connection_type", "client_username", "bandwidth_distribution_point", "connectivity_type",
           "activation_date", "bandwidth_allocation", "allocated_ip", "division", "district", "thana", "address",
           "client_mobile", "client_email", "selling_price_bdt_excluding_vat"]
SETTINGS = [  # (setting key, label, default)
    ("btrc_division", "Division", ""), ("btrc_district", "District", ""), ("btrc_thana", "Thana / Upazila", ""),
    ("btrc_client_type", "Client type", "Home"), ("btrc_connection_type", "Connection type", "Wired"),
    ("btrc_distribution_point", "Bandwidth distribution point", "NOC"), ("btrc_connectivity_type", "Connectivity type", "Shared"),
]
CHUNK = 500


def _month():
    """(first day, first day of next month) of ?month=YYYY-MM, default this month."""
    m = re.match(r"^(\d{4})-(\d{2})$", request.args.get("month", "") or "")
    today = date.today()
    y, mo = (int(m.group(1)), int(m.group(2))) if m and 1 <= int(m.group(2)) <= 12 else (today.year, today.month)
    start = date(y, mo, 1)
    end = date(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return start, end


def _settings():
    s = core.cfg()
    return {k: (s.get(k) or d) for k, _, d in SETTINGS}


_UNIT = {"k": 0.001, "kbps": 0.001, "m": 1, "mb": 1, "mbps": 1, "g": 1000, "gb": 1000, "gbps": 1000}


def _number(mbps):
    mbps = round(float(mbps), 3)
    return int(mbps) if mbps == int(mbps) else mbps


def package_mbps(row):
    """The package's speed in Mbps as a number: Bandwidth field, else the number in the name / label ("15mb" -> 15)."""
    v = row.get("bandwidth_value")
    if v not in (None, "", 0):
        try:
            return _number(float(v) * _UNIT.get((row.get("bandwidth_type") or "Mbps").lower(), 1))
        except (TypeError, ValueError):
            pass
    texts = [t for t in (row.get("plan_name"), row.get("plan_label")) if t]
    for pattern in (r"(\d+(?:\.\d+)?)\s*(kbps|mbps|gbps|kb|mb|gb|k|m|g)\b", r"(\d+(?:\.\d+)?)()"):  # "15mb" first, then a bare "15"
        for text in texts:
            m = re.search(pattern, text, re.I)
            if m:
                return _number(float(m.group(1)) * _UNIT.get((m.group(2) or "m").lower(), 1))
    return _number(row.get("download_mbps") or 0)


def rows_for(start, end):
    """The report rows (dicts keyed by COLUMNS) for customers activated / renewed in [start, end)."""
    subs = q("SELECT s.id, s.username, s.phone, s.email, s.address, s.area, s.static_ip, s.created_by, s.plan_id, "
             "p.download_mbps, p.price, p.bandwidth_value, p.bandwidth_type, p.name AS plan_name, p.label AS plan_label, "
             "a.last_act FROM (SELECT subscriber_id, MAX(paid_at) AS last_act FROM payments "
             "WHERE paid_at >= %s AND paid_at < %s AND days_added > 0 AND subscriber_id IS NOT NULL GROUP BY subscriber_id) a "
             "JOIN subscribers s ON s.id = a.subscriber_id LEFT JOIN plans p ON p.id = s.plan_id ORDER BY s.username",
             (start, end))
    # sellers' customer prices: (seller username, plan id) -> price + subscriber profit
    seller_price = {(r["username"], r["plan_id"]): float(r["price"]) + float(r["sub_profit"] or 0)
                    for r in q("SELECT a.username, up.plan_id, up.price, up.sub_profit FROM user_plans up "
                               "JOIN panel_admins a ON a.id = up.user_id WHERE up.price IS NOT NULL")}
    # latest session IP of each customer without a fixed IP (one indexed MAX per customer, in chunks)
    need = [s["username"] for s in subs if not s["static_ip"]]
    last_ip = {}
    for i in range(0, len(need), CHUNK):
        part = need[i:i + CHUNK]
        for r in q("SELECT r.username, r.framedipaddress FROM radacct r JOIN (SELECT username, MAX(radacctid) AS mid FROM radacct "
                   "WHERE username IN (" + ",".join(["%s"] * len(part)) + ") GROUP BY username) m ON r.radacctid = m.mid", part):
            last_ip[r["username"]] = r["framedipaddress"] or ""
    st = _settings()
    out = []
    for s in subs:
        price = seller_price.get((s["created_by"], s["plan_id"]))
        if price is None:
            price = float(s["price"] or 0)
        out.append({
            "client_type": st["btrc_client_type"], "connection_type": st["btrc_connection_type"],
            "client_username": s["username"], "bandwidth_distribution_point": st["btrc_distribution_point"],
            "connectivity_type": st["btrc_connectivity_type"], "activation_date": s["last_act"].date(),
            "bandwidth_allocation": package_mbps(s), "allocated_ip": s["static_ip"] or last_ip.get(s["username"], ""),
            "division": st["btrc_division"], "district": st["btrc_district"], "thana": st["btrc_thana"],
            "address": (s["address"] or "").strip() or (s["area"] or ""), "client_mobile": s["phone"] or "",
            "client_email": s["email"] or "", "selling_price_bdt_excluding_vat": round(price, 2),
        })
    return out


def _no_sellers():
    if core.my_downline() is not None:
        abort(403)


@route("/reports/btrc", module="reports")
def btrc_report():
    _no_sellers()
    start, end = _month()
    rows = rows_for(start, end)
    st = _settings()
    missing = [label for k, label, _ in SETTINGS[:3] if not st[k]]
    return render_template("btrc.html", rows=rows[:50], total=len(rows), month=start.strftime("%Y-%m"), start=start,
                           settings=st, fields=SETTINGS, columns=COLUMNS, missing=missing, tab="btrc")


@route("/reports/btrc/settings", module="reports", methods=["POST"])
@admin_required
def btrc_settings():
    f = request.form
    with core.get_db().cursor() as cur:
        for key, _, default in SETTINGS:
            db.save_setting(cur, key, (f.get(key, "") or "").strip()[:64] or default)
    audit("settings_saved", "btrc report")
    commit()
    flash("BTRC report settings saved.", "success")
    return redirect(url_for("btrc_report", month=f.get("month", "")))


# ------------------------------------------------------------------ Excel
def _xlsx(columns, rows):
    """One sheet like BTRC's sample: a header row, dates as real Excel dates (m/d/yyyy), numbers as numbers."""
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell(ref, v):
        if isinstance(v, date):
            return '<c r="%s" s="1"><v>%d</v></c>' % (ref, (v - date(1899, 12, 30)).days)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s"><v>%s</v></c>' % (ref, ("%g" % v) if isinstance(v, float) else v)
        text = str(v)
        if text[:1] in ("=", "+", "-", "@"):
            text = "'" + text
        if text == "":
            return ""
        return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, escape(text))

    lines = ['<row r="1">%s</row>' % "".join(cell("%s1" % col(i), c) for i, c in enumerate(columns))]
    for n, r in enumerate(rows, start=2):
        lines.append('<row r="%d">%s</row>' % (n, "".join(cell("%s%d" % (col(i), n), r[c]) for i, c in enumerate(columns))))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % "".join(lines))
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/'
              'spreadsheetml/2006/main"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.'
                   'openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                   'org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.'
                   'openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="'
                   'http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                   'Target="styles.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/styles.xml", styles)
    return buf.getvalue()


@route("/reports/btrc.xlsx", module="reports")
def btrc_xlsx():
    _no_sellers()
    start, end = _month()
    rows = rows_for(start, end)
    audit("export", "btrc report", "%s: %d customers" % (start.strftime("%Y-%m"), len(rows)))
    commit()
    return Response(_xlsx(COLUMNS, rows), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=btrc-downstream-%s.xlsx" % start.strftime("%Y-%m")})
