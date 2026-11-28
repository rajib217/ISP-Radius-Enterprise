"""Network menu: NAS (routers), NAS groups, MAC vendors, ONUs, traffic graphs and the topology diagram."""
import csv
import io
import re
import subprocess
from collections import OrderedDict

from flask import abort, flash, redirect, render_template, request, url_for
from markupsafe import escape

import core
import mod_network
from core import NAS_NAME_RE, audit, commit, get_db, insert, pager, q, route, x

NAS_TYPES = [("mikrotik", "MikroTik"), ("cisco", "Cisco"), ("juniper", "Juniper"), ("huawei", "Huawei"),
             ("other", "Other")]
GROUP_RE = re.compile(r"^[A-Za-z0-9_. \-]{2,64}$")


def reload_radius():
    sync_firewall()
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "freeradius"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def sync_firewall():
    """Open (or close) RADIUS in UFW for the routers now saved; the minute timer does the same if this fails."""
    try:
        subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block", "isp-panel-fwsync.service"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# ===================================================================== NAS
@route("/nas", module="network")
def nas_list():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(n.nasname LIKE %s OR n.shortname LIKE %s OR n.description LIKE %s)")
        params += ["%" + text + "%"] * 3
    if request.args.get("group", "").isdigit():
        where.append("n.group_id = %s")
        params.append(int(request.args["group"]))
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = q("SELECT COUNT(*) AS c FROM nas n" + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT n.id, n.nasname, n.shortname, n.type, n.description, n.group_id, n.coa_enabled, n.last_status, n.snmp_community, n.seller, "
             "n.last_checked, g.name AS group_name, "
             "(SELECT COUNT(*) FROM radacct ra WHERE ra.nasipaddress = n.nasname AND ra.acctstoptime IS NULL) "
             "AS online FROM nas n LEFT JOIN nas_groups g ON g.id = n.group_id" + cond +
             " ORDER BY n.id LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    if core.can("network", "edit"):
        secrets = {r["id"]: r["secret"] for r in q("SELECT id, secret FROM nas")}
        for r in rows:
            r["secret"] = secrets.get(r["id"], "")
    for r in rows:
        r["type_name"] = dict(NAS_TYPES).get(r["type"], (r["type"] or "Other").capitalize())
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("nas.html", rows=rows, pg=pg, keep=keep, types=NAS_TYPES,
                           groups=q("SELECT id, name FROM nas_groups ORDER BY name"),
                           server=request.host.split(":")[0], sellers=_sellers())


def _sellers():
    """Resellers a router can belong to (only admins and staff assign routers; a seller never sees this choice)."""
    if core.my_downline() is not None:
        return []
    return q("SELECT username, full_name, role FROM panel_admins WHERE role IN (%s) ORDER BY username"
             % ",".join(["%s"] * len(core.SELLER_ROLES)), list(core.SELLER_ROLES))


@route("/nas/save", module="network", methods=["POST"])
def nas_save():
    f = request.form
    nid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, short = f.get("nasname", "").strip(), f.get("shortname", "").strip()
    secret, desc = f.get("secret", "").strip(), f.get("description", "").strip()[:200]
    ntype = f.get("type") if f.get("type") in dict(NAS_TYPES) else "other"
    group = int(f["group_id"]) if f.get("group_id", "").isdigit() else None
    coa = 1 if f.get("coa_enabled") == "1" else 0
    seller = None                       # None = keep what is saved (a seller editing a router cannot change it)
    if core.my_downline() is None:
        seller = f.get("seller", "").strip()
        if seller and not q("SELECT 1 AS x FROM panel_admins WHERE username = %s AND role IN (%s)"
                            % ("%s", ",".join(["%s"] * len(core.SELLER_ROLES))), [seller] + list(core.SELLER_ROLES), one=True):
            flash("Pick a reseller from the list.", "error")
            return redirect(url_for("nas_list"))
    community = f.get("snmp_community", "").strip()
    if community and not re.match(r"^[!-~]{1,64}$", community):
        flash("The SNMP community may only contain printable characters without spaces (max 64).", "error")
        return redirect(url_for("nas_list"))
    if not NAS_NAME_RE.match(name) or not (1 <= len(short) <= 32) or not re.match(r"^[\x21-\x7e]{6,60}$", secret):
        flash("Router IP, a short name (max 32) and a secret of 6-60 characters without spaces are required.", "error")
        return redirect(url_for("nas_list"))
    if q("SELECT id FROM nas WHERE nasname = %s AND id <> %s", (name, nid), one=True):
        flash("A router with address %s already exists." % name, "error")
        return redirect(url_for("nas_list"))
    if nid:
        x("UPDATE nas SET nasname=%s, shortname=%s, type=%s, secret=%s, description=%s, group_id=%s, coa_enabled=%s, "
          "snmp_community=%s WHERE id=%s", (name, short, ntype, secret, desc, group, coa, community, nid))
    else:
        insert("INSERT INTO nas (nasname, shortname, type, secret, description, group_id, coa_enabled, snmp_community) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (name, short, ntype, secret, desc, group, coa, community))
    if seller is not None:
        x("UPDATE nas SET seller = %s WHERE nasname = %s", (seller, name))
    audit("nas_saved", name)
    commit()
    ok, msg = reload_radius()
    flash("Router saved and RADIUS restarted, so it is active now." if ok else
          "Router saved, but RADIUS could not restart (%s). Run: sudo systemctl restart freeradius" % (msg or "unknown error"),
          "success" if ok else "warn")
    return redirect(url_for("nas_list"))


@route("/nas/<int:nid>/delete", module="network", methods=["POST"])
def nas_delete(nid):
    row = q("SELECT nasname FROM nas WHERE id = %s", (nid,), one=True) or abort(404)
    x("DELETE FROM nas WHERE id = %s", (nid,))
    x("UPDATE net_devices SET nas_id = NULL WHERE nas_id = %s", (nid,))
    audit("nas_deleted", row["nasname"])
    commit()
    reload_radius()
    flash("Router %s removed." % row["nasname"], "success")
    return redirect(url_for("nas_list"))


def _check_nas(row, result=...):
    if result is ...:
        result = mod_network.ping(row["nasname"])
    status = "up" if result else ("down" if result is False else "")
    x("UPDATE nas SET last_status = %s, last_checked = NOW() WHERE id = %s", (status, row["id"]))
    return result


@route("/nas/<int:nid>/ping", module="network", methods=["POST"])
def nas_ping(nid):
    row = q("SELECT id, nasname, shortname FROM nas WHERE id = %s", (nid,), one=True) or abort(404)
    result = _check_nas(row)
    commit()
    if result is None:
        flash("The ping command is not installed on the server (apt install iputils-ping).", "warn")
    else:
        flash("%s answers." % row["shortname"] if result else
              "%s did not answer. It may block ping, or be down." % row["shortname"], "success" if result else "warn")
    return redirect(url_for("nas_list"))


@route("/nas/ping-all", module="network", methods=["POST"])
def nas_ping_all():
    up = down = 0
    rows = q("SELECT id, nasname FROM nas LIMIT 200")
    answers = mod_network.ping_many(row["nasname"] for row in rows)
    for row in rows:
        r = _check_nas(row, answers.get(row["nasname"]))
        up += r is True
        down += r is False
    commit()
    flash("%d answering, %d not answering." % (up, down), "success" if not down else "warn")
    return redirect(url_for("nas_list"))


# ===================================================================== NAS groups
@route("/nas-groups", module="network")
def nas_groups():
    rows = q("SELECT g.id, g.name, g.note, COUNT(n.id) AS routers, "
             "COALESCE(SUM((SELECT COUNT(*) FROM radacct ra WHERE ra.nasipaddress = n.nasname "
             "AND ra.acctstoptime IS NULL)), 0) AS online FROM nas_groups g LEFT JOIN nas n ON n.group_id = g.id "
             "GROUP BY g.id, g.name, g.note ORDER BY g.name")
    return render_template("nas_groups.html", rows=rows)


@route("/nas-groups/save", module="network", methods=["POST"])
def nas_group_save():
    f = request.form
    gid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name = f.get("name", "").strip()
    if not GROUP_RE.match(name):
        flash("Group name: 2-64 letters, numbers, space, dot, dash or underscore.", "error")
        return redirect(core.safe_next(f.get("next")) or url_for("nas_groups"))
    if q("SELECT id FROM nas_groups WHERE name = %s AND id <> %s", (name, gid), one=True):
        flash("A group named %s already exists." % name, "error")
        return redirect(core.safe_next(f.get("next")) or url_for("nas_groups"))
    if gid:
        x("UPDATE nas_groups SET name = %s, note = %s WHERE id = %s", (name, f.get("note", "").strip()[:255], gid))
    else:
        insert("INSERT INTO nas_groups (name, note) VALUES (%s, %s)", (name, f.get("note", "").strip()[:255]))
    audit("nas_group_saved", name)
    commit()
    flash("Group saved.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("nas_groups"))


@route("/nas-groups/<int:gid>/delete", module="network", methods=["POST"])
def nas_group_delete(gid):
    row = q("SELECT name FROM nas_groups WHERE id = %s", (gid,), one=True) or abort(404)
    x("UPDATE nas SET group_id = NULL WHERE group_id = %s", (gid,))
    x("DELETE FROM nas_groups WHERE id = %s", (gid,))
    audit("nas_group_deleted", row["name"])
    commit()
    flash("Group deleted. Its routers are now ungrouped.", "success")
    return redirect(url_for("nas_groups"))


# ===================================================================== MAC vendors
BLOCK_LEN = {"MA-L": 6, "MA-M": 7, "MA-S": 9}
IEEE_FILES = ("https://standards-oui.ieee.org/oui/oui.csv", "https://standards-oui.ieee.org/oui28/mam.csv",
              "https://standards-oui.ieee.org/oui36/oui36.csv")


def _hex(text):
    return re.sub(r"[^0-9A-Fa-f]", "", text or "").upper()


def mac_prefix(text):
    h = _hex(text)
    return h[:6] if len(h) >= 6 else ""


def vendor_of(mac):
    """Who made the device with this MAC: the longest IEEE block that matches (MA-S, then MA-M, then MA-L), or ''."""
    h = _hex(mac)
    keys = [h[:n] for n in (9, 7, 6) if len(h) >= n]
    if not keys:
        return ""
    row = q("SELECT vendor FROM mac_vendors WHERE prefix IN (" + ",".join(["%s"] * len(keys)) + ") "
            "ORDER BY LENGTH(prefix) DESC LIMIT 1", keys, one=True)
    return row["vendor"] if row else ""


def fmt_prefix(p):
    """0050C2 -> 00:50:C2, 0050C21 -> 00:50:C2:1, 0050C2123 -> 00:50:C2:12:3"""
    return ":".join(p[i:i + 2] for i in range(0, len(p), 2))


LINE_RE = re.compile(r'^\s*"?([0-9A-Fa-f]{2}[:\-.]?[0-9A-Fa-f]{2}[:\-.]?[0-9A-Fa-f]{2})"?\s*(?:\(hex\))?[,;\t ]+\s*"?(.+?)"?\s*$')


def _day(text):
    m = re.match(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text or "")
    return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


def parse_vendors(text):
    """{prefix: (vendor, block, private, date)} from the IEEE files (oui.csv, mam.csv, oui36.csv), the maclookup.app
    export (Mac Prefix,Vendor Name,Private,Block Type,Last Update) or plain lines  AA:BB:CC,Vendor name."""
    out = {}
    for line in io.StringIO(text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = next(csv.reader([line]))
        except (StopIteration, csv.Error):
            continue
        if row and row[0].strip().upper() in BLOCK_LEN and len(row) >= 3:           # IEEE: Registry,Assignment,Name,...
            block, p, v = row[0].strip().upper(), _hex(row[1]), row[2].strip()
            if len(p) != BLOCK_LEN[block]:
                continue
            item = (v, block, 1 if v.lower() == "private" else 0, None)
        elif len(row) >= 4 and row[3].strip().upper() in BLOCK_LEN:                  # maclookup.app export
            block, p, v = row[3].strip().upper(), _hex(row[0]), row[1].strip()
            if len(p) != BLOCK_LEN[block]:
                continue
            item = (v, block, 1 if row[2].strip().lower() in ("true", "1", "yes") else 0, _day(row[4]) if len(row) > 4 else None)
        else:
            m = LINE_RE.match(line)
            if not m:
                continue
            p, v = mac_prefix(m.group(1)), m.group(2).strip().strip('"')
            item = (v, "MA-L", 0, None)
        if p and item[0]:
            out[p] = (item[0][:128],) + item[1:]
    return out


def _save_vendors(rows):
    """REPLACE the parsed rows; an entry with no date of its own gets today's."""
    items = [(p, v, b, pv, d) for p, (v, b, pv, d) in rows.items()]
    with get_db().cursor() as cur:
        for i in range(0, len(items), 2000):
            cur.executemany("REPLACE INTO mac_vendors (prefix, vendor, block_type, private, updated) "
                            "VALUES (%s, %s, %s, %s, COALESCE(%s, CURDATE()))", items[i:i + 2000])
    return len(items)


def _vendor_where():
    term = request.args.get("q", "").strip()[:64]
    if not term:
        return term, "", []
    h = _hex(term)
    if len(h) >= 2 and len(h) * 1.0 >= len(re.sub(r"[\s:\-.]", "", term)):          # looks like a MAC / prefix
        keys = [h[:n] for n in (9, 7, 6) if len(h) >= n]
        if keys:                                                                       # a whole MAC: its own block
            return term, " WHERE prefix IN (" + ",".join(["%s"] * len(keys)) + ") OR vendor LIKE %s", keys + ["%" + term + "%"]
        return term, " WHERE prefix LIKE %s OR vendor LIKE %s", [h + "%", "%" + term + "%"]
    return term, " WHERE vendor LIKE %s", ["%" + term + "%"]


@route("/mac-vendors", module="network")
def mac_vendors():
    term, where, args = _vendor_where()
    total_all = q("SELECT COUNT(*) AS c FROM mac_vendors", one=True)["c"]
    total = q("SELECT COUNT(*) AS c FROM mac_vendors" + where, args, one=True)["c"] if where else total_all
    pg = pager(total)
    rows = q("SELECT prefix, vendor, block_type, private, updated FROM mac_vendors" + where +
             " ORDER BY prefix LIMIT %s OFFSET %s", args + [pg["per"], pg["offset"]])
    for r in rows:
        r["shown"] = fmt_prefix(r["prefix"])
    # customers online now, by the maker of their router (MA-L blocks)
    by_vendor = [(r["vendor"] or "Unknown", r["c"]) for r in q(
        "SELECT v.vendor, COUNT(*) AS c FROM radacct a LEFT JOIN mac_vendors v ON v.prefix = "
        "LEFT(UPPER(REPLACE(REPLACE(REPLACE(a.callingstationid, ':', ''), '-', ''), '.', '')), 6) "
        "WHERE a.acctstoptime IS NULL AND a.callingstationid <> '' GROUP BY v.vendor ORDER BY c DESC LIMIT 15")]
    keep = {k: v for k, v in (("q", term), ("per", pg["per"])) if v}
    return render_template("mac_vendors.html", rows=rows, pg=pg, keep=keep, term=term, total_all=total_all,
                           by_vendor=by_vendor, ieee=IEEE_FILES)


@route("/mac-vendors/export.csv", module="network")
def mac_vendor_export():
    from flask import Response
    term, where, args = _vendor_where()
    out = io.StringIO()
    w = core.csv_writer(out)
    w.writerow(["MAC Prefix", "Vendor Name", "Privacy", "Block Type", "Last Update"])
    for r in q("SELECT prefix, vendor, block_type, private, updated FROM mac_vendors" + where + " ORDER BY prefix", args):
        w.writerow([fmt_prefix(r["prefix"]), r["vendor"], "Private" if r["private"] else "Public", r["block_type"],
                    r["updated"].isoformat() if r["updated"] else ""])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=mac-vendors.csv"})


@route("/mac-vendors/import", module="network", methods=["POST"])
def mac_vendor_import():
    text = request.form.get("text", "")
    upload = request.files.get("file")
    if upload and upload.filename:
        text += "\n" + upload.stream.read(16 * 1024 * 1024).decode("utf-8", "replace")
    rows = parse_vendors(text)
    if not rows:
        flash("No vendors found. Use the IEEE oui.csv / mam.csv / oui36.csv file, the maclookup.app CSV, "
              "or lines like  AA:BB:CC,Vendor name.", "error")
        return redirect(url_for("mac_vendors"))
    n = _save_vendors(rows)
    audit("mac_vendors_imported", "", "%d rows" % n)
    commit()
    flash("%s vendors saved." % "{:,}".format(n), "success")
    return redirect(url_for("mac_vendors"))


@route("/mac-vendors/update", module="network", methods=["POST"])
def mac_vendor_update():
    """Download the three IEEE registries (MA-L, MA-M, MA-S) straight from standards-oui.ieee.org and save them."""
    import urllib.request
    rows, failed = {}, []
    for url in IEEE_FILES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ISP RADIUS Panel vendor update)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                rows.update(parse_vendors(r.read(32 * 1024 * 1024).decode("utf-8", "replace")))
        except Exception as exc:                 # no internet, IEEE refusing: say so and keep what is there
            failed.append("%s (%s)" % (url.rsplit("/", 1)[-1], str(exc)[:80]))
    if not rows:
        flash("Could not download the IEEE list: %s. Download oui.csv in a browser and use Import instead." % "; ".join(failed), "error")
        return redirect(url_for("mac_vendors"))
    n = _save_vendors(rows)
    audit("mac_vendors_imported", "IEEE", "%d rows" % n)
    commit()
    flash("%s vendors updated from IEEE.%s" % ("{:,}".format(n), (" Not downloaded: " + "; ".join(failed)) if failed else ""),
          "warn" if failed else "success")
    return redirect(url_for("mac_vendors"))


@route("/mac-vendors/clear", module="network", methods=["POST"])
def mac_vendor_clear():
    x("DELETE FROM mac_vendors")
    audit("mac_vendors_cleared")
    commit()
    flash("Vendor list cleared.", "success")
    return redirect(url_for("mac_vendors"))


# ===================================================================== ONUs
ONU_STATUS = [("online", "Online"), ("offline", "Offline"), ("los", "LOS (no light)")]


SIGNALS = [("excellent", "Excellent", "ok"), ("good", "Good", "ok"), ("fair", "Fair", "info"),
           ("poor", "Poor", "warn"), ("critical", "Critical", "bad")]
# Rx power bands in dBm (lower bound inclusive): excellent >= -14, good >= -20, fair >= -23, poor >= -28
SIGNAL_SQL = {"excellent": ("o.rx_dbm >= -14", []), "good": ("o.rx_dbm >= -20 AND o.rx_dbm < -14", []),
              "fair": ("o.rx_dbm >= -23 AND o.rx_dbm < -20", []), "poor": ("o.rx_dbm >= -28 AND o.rx_dbm < -23", []),
              "critical": ("o.rx_dbm < -28", []), "none": ("o.rx_dbm IS NULL", [])}
_POS = re.compile(r"(\d+)/(\d+)(?:/(\d+))?:(\d+)")


def signal_of(rx):
    """Return (key, label, pill colour) for an Rx power in dBm, or None."""
    if rx is None:
        return None
    rx = float(rx)
    for limit, i in ((-14, 0), (-20, 1), (-23, 2), (-28, 3)):
        if rx >= limit:
            return SIGNALS[i]
    return SIGNALS[4]


def position_of(o):
    """Split an ONU's name/PON text into (slot, pon, ont) where the OLT's naming allows it."""
    m = _POS.search("%s %s" % (o.get("pon_port") or "", o.get("name") or ""))
    if not m:
        return "", o.get("pon_port") or "", ""
    a, b_, c, ont = m.groups()
    return (("%s/%s" % (a, b_)), c, ont) if c is not None else (a, b_, ont)


def _my_olts():
    sc, sp = core.olt_scope("d")
    return q("SELECT d.id, d.name FROM net_devices d WHERE d.kind = 'olt'" + sc + " ORDER BY d.name", sp)


def _own_onu(oid):
    """The ONU if this user may touch it, else 404."""
    sc, sp = core.onu_scope("o")
    o = q("SELECT o.id FROM onus o WHERE o.id = %s" + sc, [oid] + sp, one=True)
    return o or abort(404)


@route("/onus", module="network")
def onus():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(o.name LIKE %s OR o.serial LIKE %s OR o.mac LIKE %s OR o.username LIKE %s)")
        params += ["%" + text + "%"] * 4
    if request.args.get("olt", "").isdigit():
        where.append("o.olt_id = %s")
        params.append(int(request.args["olt"]))
    if request.args.get("status") in dict(ONU_STATUS):
        where.append("o.status = %s")
        params.append(request.args["status"])
    if request.args.get("signal") in SIGNAL_SQL:
        where.append(SIGNAL_SQL[request.args["signal"]][0])
    if request.args.get("mapping") == "mapped":
        where.append("o.username <> ''")
    elif request.args.get("mapping") == "unmapped":
        where.append("o.username = ''")
    osc, osp = core.onu_scope("o")
    if osc:
        where.append(osc[5:])
        params += osp
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = q("SELECT COUNT(*) AS c FROM onus o" + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT o.*, d.name AS olt_name, s.id AS sid FROM onus o LEFT JOIN net_devices d ON d.id = o.olt_id "
             "LEFT JOIN subscribers s ON s.username = o.username AND o.username <> ''" + cond +
             " ORDER BY o.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    for o in rows:
        o["slot"], o["pon"], o["ont"] = position_of(o)
        o["sig"] = signal_of(o["rx_dbm"])
    match_onu_customers(rows)
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("onus.html", rows=rows, pg=pg, keep=keep, statuses=ONU_STATUS, signals=SIGNALS,
                           olts=_my_olts())


@route("/onus/<int:oid>", module="network")
def onu_view(oid):
    o = q("SELECT o.*, d.name AS olt_name, d.id AS olt_ref, s.id AS sid FROM onus o "
          "LEFT JOIN net_devices d ON d.id = o.olt_id "
          "LEFT JOIN subscribers s ON s.username = o.username AND o.username <> '' WHERE o.id = %s" + core.onu_scope("o")[0],
          [oid] + core.onu_scope("o")[1], one=True)
    if not o:
        abort(404)
    o["slot"], o["pon"], o["ont"] = position_of(o)
    o["sig"] = signal_of(o["rx_dbm"])
    full = "%s/%s:%s" % (o["slot"], o["pon"], o["ont"]) if o["ont"] else (o["pon_port"] or "")
    span = request.args.get("range", "24h")
    hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}.get(span, 24)
    if span not in ("1h", "6h", "24h", "7d"):
        span = "24h"
    hist = q("SELECT ts, rx_dbm, tx_dbm, temp_c FROM onu_metrics WHERE onu_id = %s AND ts >= NOW() - INTERVAL %s HOUR "
             "ORDER BY ts LIMIT 3000", (oid, hours))
    fmt = "%H:%M" if hours <= 24 else "%d %b"
    num = lambda v: None if v is None else float(v)
    chart = {"t": [h["ts"].strftime(fmt) for h in hist], "full": [h["ts"].strftime("%Y-%m-%d %H:%M") for h in hist],
             "rx": [num(h["rx_dbm"]) for h in hist], "tx": [num(h["tx_dbm"]) for h in hist],
             "temp": [num(h["temp_c"]) for h in hist]}
    return render_template("onu_view.html", o=o, full=full, chart=chart, span=span, statuses=ONU_STATUS,
                           olts=_my_olts())


@route("/onus/<int:oid>/map", module="network", methods=["POST"])
def onu_map(oid):
    """Link an ONU to a subscriber by username (empty username unlinks it)."""
    _own_onu(oid)
    user = request.form.get("username", "").strip()[:64]
    ssc, ssp = core.scope_sql("s")
    if user and not q("SELECT id FROM subscribers s WHERE s.username = %s" + ssc, [user] + ssp, one=True):
        flash("No subscriber with the username %s." % user, "error")
        return redirect(core.safe_next(request.form.get("next")) or url_for("onus"))
    x("UPDATE onus SET username = %s WHERE id = %s", (user, oid))
    audit("onu_mapped", "#%d" % oid, user or "unlinked")
    commit()
    flash("ONU linked to %s." % user if user else "ONU unlinked.", "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("onus"))


@route("/onus/save", module="network", methods=["POST"])
def onu_save():
    """ONUs are never added by hand: they come from the OLT's SNMP sync, which also keeps their
    name, serial, MAC, port, signal and status. Only the note is edited here (and the customer via onu_map)."""
    f = request.form
    oid = int(f["id"]) if f.get("id", "").isdigit() else 0
    if not oid:
        flash("ONUs can't be added by hand. Add the OLT and run Sync ONUs; its ONUs are added automatically.", "error")
        return redirect(url_for("onus"))
    _own_onu(oid)
    x("UPDATE onus SET note = %s WHERE id = %s", (f.get("note", "").strip()[:255], oid))
    audit("onu_saved", "#%d" % oid)
    commit()
    flash("ONU saved.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("onu_view", oid=oid))


@route("/onus/<int:oid>/delete", module="network", methods=["POST"])
def onu_delete(oid):
    _own_onu(oid)
    x("DELETE FROM onus WHERE id = %s", (oid,))
    audit("onu_deleted", "#%d" % oid)
    commit()
    flash("ONU deleted.", "success")
    return redirect(url_for("onus"))


# ===================================================================== graph
@route("/network/graph", module="network")
def network_graph():
    per_nas = q("SELECT COALESCE(n.shortname, ra.nasipaddress) AS name, COUNT(*) AS c FROM radacct ra "
                "LEFT JOIN nas n ON n.nasname = ra.nasipaddress WHERE ra.acctstoptime IS NULL "
                "GROUP BY ra.nasipaddress, n.shortname ORDER BY c DESC LIMIT 20")
    traffic = q("SELECT COALESCE(n.shortname, ra.nasipaddress) AS name, SUM(ra.acctinputoctets) AS up, "
                "SUM(ra.acctoutputoctets) AS down FROM radacct ra LEFT JOIN nas n ON n.nasname = ra.nasipaddress "
                "WHERE ra.acctstarttime >= NOW() - INTERVAL 7 DAY GROUP BY ra.nasipaddress, n.shortname "
                "ORDER BY down DESC LIMIT 20")
    snaps = q("SELECT ts, online FROM stats_snapshots WHERE ts >= NOW() - INTERVAL 24 HOUR ORDER BY ts")
    online = {"labels": [n["name"] for n in per_nas],
              "series": [{"name": "Online now", "values": [core.n(n["c"]) for n in per_nas]}], "unit": "number"}
    tr = {"labels": [t["name"] for t in traffic],
          "series": [{"name": "Download", "values": [core.n(t["down"]) for t in traffic]},
                     {"name": "Upload", "values": [core.n(t["up"]) for t in traffic]}]}
    line = {"labels": [s["ts"].strftime("%H:%M") for s in snaps], "stamps": [s["ts"].strftime("%d %b %H:%M") for s in snaps],
            "values": [s["online"] for s in snaps]}
    return render_template("network_graph.html", online=online, traffic=tr, line=line, has_nas=bool(per_nas))


# ===================================================================== diagram
def _node(x_, y, w, h, title, sub, kind, status=""):
    dot = {"up": "ok", "down": "bad"}.get(status, "idle")
    return ('<g class="nd %s"><rect x="%d" y="%d" width="%d" height="%d" rx="8"/>'
            '<circle cx="%d" cy="%d" r="5" class="dot %s"/>'
            '<text x="%d" y="%d" class="t1">%s</text><text x="%d" y="%d" class="t2">%s</text></g>'
            % (kind, x_, y, w, h, x_ + 14, y + h // 2, dot, x_ + 26, y + 20, escape(title)[:26],
               x_ + 26, y + 37, escape(sub)[:30]))


@route("/network/diagram", module="network")
def network_diagram():
    if core.my_downline() is not None:
        return redirect(url_for("olts"))
    nases = q("SELECT n.id, n.shortname, n.nasname, n.last_status, g.name AS gname, "
              "(SELECT COUNT(*) FROM radacct ra WHERE ra.nasipaddress = n.nasname AND ra.acctstoptime IS NULL) AS online "
              "FROM nas n LEFT JOIN nas_groups g ON g.id = n.group_id ORDER BY g.name, n.shortname LIMIT 60")
    devs = q("SELECT d.id, d.name, d.kind, d.ip, d.nas_id, d.last_status, "
             "(SELECT COUNT(*) FROM onus o WHERE o.olt_id = d.id) AS onus FROM net_devices d ORDER BY d.kind, d.name LIMIT 200")
    kinds = dict(mod_network.DEVICE_KINDS)
    by_nas = OrderedDict((n["id"], []) for n in nases)
    loose = []
    for d in devs:
        (by_nas[d["nas_id"]] if d["nas_id"] in by_nas else loose).append(d)
    W, H, GAP = 210, 50, 14
    xs, xn, xd = 20, 290, 580
    parts, y = [], 20
    node_y = {}
    # devices column (grouped by router), then routers centred on their devices
    dev_nodes = []
    for n in nases:
        start = y
        for d in by_nas[n["id"]]:
            sub = "%s%s" % (kinds.get(d["kind"], d["kind"]), " · %d ONUs" % d["onus"] if d["kind"] == "olt" else "")
            dev_nodes.append((n["id"], y, d, sub))
            y += H + GAP
        end = y - GAP if y > start else start + H
        node_y[n["id"]] = (start + end) // 2 - H // 2 if y > start else start
        if y == start:
            y += H + GAP
        y += 10
    nas_nodes_y = node_y
    lines, boxes = [], []
    total_h = max(y, 120)
    srv_y = total_h // 2 - H // 2
    boxes.append(_node(xs, srv_y, 200, H, "RADIUS server", "FreeRADIUS", "srv", "up"))
    for n in nases:
        ny = nas_nodes_y[n["id"]]
        lines.append('<path d="M%d %d C%d %d %d %d %d %d" class="ln"/>' % (xs + 200, srv_y + H // 2, xs + 245, srv_y + H // 2,
                                                                            xn - 45, ny + H // 2, xn, ny + H // 2))
        boxes.append(_node(xn, ny, W, H, n["shortname"], "%s · %d online" % (n["nasname"], n["online"]), "nas", n["last_status"]))
    for nid, dy, d, sub in dev_nodes:
        ny = nas_nodes_y[nid]
        lines.append('<path d="M%d %d C%d %d %d %d %d %d" class="ln"/>' % (xn + W, ny + H // 2, xn + W + 40, ny + H // 2,
                                                                            xd - 40, dy + H // 2, xd, dy + H // 2))
        boxes.append(_node(xd, dy, W, H, d["name"], sub, "dev", d["last_status"]))
    if loose:
        y2 = total_h + 10
        boxes.append('<text x="%d" y="%d" class="t2">Not linked to a router</text>' % (xn, y2))
        for i, d in enumerate(loose[:40]):
            sub = kinds.get(d["kind"], d["kind"])
            boxes.append(_node(xn + (i % 3) * (W + 14), y2 + 10 + (i // 3) * (H + GAP), W, H, d["name"], sub, "dev",
                               d["last_status"]))
        total_h = y2 + 20 + ((len(loose[:40]) + 2) // 3) * (H + GAP)
    width = xd + W + 20
    svg = ('<svg viewBox="0 0 %d %d" class="diagram" role="img" aria-label="Network diagram" width="%d" height="%d">%s%s</svg>'
           % (width, total_h + 20, width, total_h + 20, "".join(lines), "".join(boxes)))
    return render_template("network_diagram.html", svg=svg, empty=not nases and not devs)

# ===================================================================== which customer uses an ONU
def _mac12(text):
    h = re.sub(r"[^0-9A-Fa-f]", "", text or "").upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else ""


def match_onu_customers(rows):
    """For ONUs not linked to a customer, find who uses them (dashboard Offline ONUs and Network > ONUs):
      1. the ONU's name or serial on the OLT is the customer's PPPoE username (often written in the ONU description)
      2. the ONU's MAC is the customer's bound MAC (MAC binding)
      3. the ONU's MAC is the calling MAC of a customer's live PPPoE session (an ONU in router mode dials PPPoE itself)
    Sets r["guess"] = {"sid", "username", "full_name", "how"}. Only customers this person may see are used. Nothing is
    saved: the pages offer a Link button for that."""
    todo = [r for r in rows if not r.get("sid") and not r.get("username")]
    if not todo:
        return
    ssc, ssp = core.scope_sql("s")
    keys = {(t or "").strip().lower() for r in todo for t in (r.get("name"), r.get("serial")) if (t or "").strip()}
    by_name = {}
    if keys:
        for s in q("SELECT s.id, s.username, s.full_name FROM subscribers s WHERE LOWER(s.username) IN (%s)"
                   % ",".join(["%s"] * len(keys)) + ssc, list(keys) + ssp):
            by_name[s["username"].lower()] = s
    macs = {_mac12(r.get("mac")) for r in todo} - {""}
    by_bind, by_session = {}, {}
    if macs:
        for s in q("SELECT s.id, s.username, s.full_name, s.mac_bind FROM subscribers s WHERE s.mac_bind IN (%s)"
                   % ",".join(["%s"] * len(macs)) + ssc, list(macs) + ssp):
            by_bind[s["mac_bind"]] = s
        # live sessions only (acctstoptime IS NULL is indexed); routers send the MAC as AA:BB:.., aa:bb:.. or AA-BB-..
        forms = sorted({f for m in macs for f in (m, m.lower(), m.replace(":", "-"), m.lower().replace(":", "-"))})
        live = q("SELECT DISTINCT r.username, r.callingstationid FROM radacct r WHERE r.acctstoptime IS NULL "
                 "AND r.callingstationid IN (%s)" % ",".join(["%s"] * len(forms)), forms)
        if live:
            users = {r["username"] for r in live}
            subs = {s["username"]: s for s in q(
                "SELECT s.id, s.username, s.full_name FROM subscribers s WHERE s.username IN (%s)"
                % ",".join(["%s"] * len(users)) + ssc, list(users) + ssp)}
            for r in live:
                if r["username"] in subs:
                    by_session[_mac12(r["callingstationid"])] = subs[r["username"]]
    for r in todo:
        hit, how = None, ""
        for t in (r.get("name"), r.get("serial")):
            s = by_name.get((t or "").strip().lower())
            if s:
                hit, how = s, "ONU name"
                break
        m = _mac12(r.get("mac"))
        if not hit and m in by_bind:
            hit, how = by_bind[m], "MAC"
        if not hit and m in by_session:
            hit, how = by_session[m], "live session"
        if hit:
            r["guess"] = {"sid": hit["id"], "username": hit["username"], "full_name": hit["full_name"] or "", "how": how}
