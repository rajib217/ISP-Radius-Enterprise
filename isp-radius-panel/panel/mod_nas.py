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
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", "freeradius"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


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
    rows = q("SELECT n.id, n.nasname, n.shortname, n.type, n.description, n.group_id, n.coa_enabled, n.last_status, "
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
                           server=request.host.split(":")[0])


@route("/nas/save", module="network", methods=["POST"])
def nas_save():
    f = request.form
    nid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, short = f.get("nasname", "").strip(), f.get("shortname", "").strip()
    secret, desc = f.get("secret", "").strip(), f.get("description", "").strip()[:200]
    ntype = f.get("type") if f.get("type") in dict(NAS_TYPES) else "other"
    group = int(f["group_id"]) if f.get("group_id", "").isdigit() else None
    coa = 1 if f.get("coa_enabled") == "1" else 0
    if not NAS_NAME_RE.match(name) or not (1 <= len(short) <= 32) or not re.match(r"^[\x21-\x7e]{6,60}$", secret):
        flash("Router IP, a short name (max 32) and a secret of 6-60 characters without spaces are required.", "error")
        return redirect(url_for("nas_list"))
    if q("SELECT id FROM nas WHERE nasname = %s AND id <> %s", (name, nid), one=True):
        flash("A router with address %s already exists." % name, "error")
        return redirect(url_for("nas_list"))
    if nid:
        x("UPDATE nas SET nasname=%s, shortname=%s, type=%s, secret=%s, description=%s, group_id=%s, coa_enabled=%s "
          "WHERE id=%s", (name, short, ntype, secret, desc, group, coa, nid))
    else:
        insert("INSERT INTO nas (nasname, shortname, type, secret, description, group_id, coa_enabled) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s)", (name, short, ntype, secret, desc, group, coa))
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
def mac_prefix(text):
    h = re.sub(r"[^0-9A-Fa-f]", "", text or "").upper()
    return h[:6] if len(h) >= 6 else ""


LINE_RE = re.compile(r'^\s*"?([0-9A-Fa-f]{2}[:\-.]?[0-9A-Fa-f]{2}[:\-.]?[0-9A-Fa-f]{2})"?\s*(?:\(hex\))?[,;\t ]+\s*"?(.+?)"?\s*$')


def parse_vendors(text):
    out = {}
    for line in io.StringIO(text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("MA-"):                      # IEEE oui.csv: Registry,Assignment,Organization,Address
            try:
                row = next(csv.reader([line]))
                p, v = mac_prefix(row[1]), row[2].strip()
            except (StopIteration, IndexError):
                continue
        else:
            m = LINE_RE.match(line)
            if not m:
                continue
            p, v = mac_prefix(m.group(1)), m.group(2).strip().strip('"')
        if len(p) == 6 and v:
            out[p] = v[:128]
    return out


@route("/mac-vendors", module="network")
def mac_vendors():
    lookup = request.args.get("mac", "").strip()
    found = None
    if lookup:
        p = mac_prefix(lookup)
        row = q("SELECT vendor FROM mac_vendors WHERE prefix = %s", (p,), one=True) if p else None
        found = {"mac": lookup, "prefix": p, "vendor": row["vendor"] if row else ""}
    total = q("SELECT COUNT(*) AS c FROM mac_vendors", one=True)["c"]
    vendors = {r["prefix"]: r["vendor"] for r in q("SELECT prefix, vendor FROM mac_vendors")} if total <= 60000 else {}
    sessions = q("SELECT callingstationid AS mac, COUNT(*) AS c FROM radacct WHERE acctstoptime IS NULL "
                 "AND callingstationid <> '' GROUP BY callingstationid LIMIT 5000")
    counts = {}
    for s in sessions:
        v = vendors.get(mac_prefix(s["mac"]), "Unknown")
        counts[v] = counts.get(v, 0) + s["c"]
    by_vendor = sorted(counts.items(), key=lambda kv: -kv[1])
    return render_template("mac_vendors.html", found=found, total=total, by_vendor=by_vendor,
                           recent=q("SELECT prefix, vendor FROM mac_vendors ORDER BY vendor LIMIT 50"))


@route("/mac-vendors/import", module="network", methods=["POST"])
def mac_vendor_import():
    text = request.form.get("text", "")
    upload = request.files.get("file")
    if upload and upload.filename:
        text += "\n" + upload.stream.read(8 * 1024 * 1024).decode("utf-8", "replace")
    rows = parse_vendors(text)
    if not rows:
        flash("No vendors found. Use lines like  AA:BB:CC,Vendor name  or the IEEE oui.csv file.", "error")
        return redirect(url_for("mac_vendors"))
    items = list(rows.items())
    with get_db().cursor() as cur:
        for i in range(0, len(items), 2000):
            cur.executemany("REPLACE INTO mac_vendors (prefix, vendor) VALUES (%s, %s)", items[i:i + 2000])
    audit("mac_vendors_imported", "", "%d rows" % len(items))
    commit()
    flash("%d vendors saved." % len(items), "success")
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
    f = request.form
    oid = int(f["id"]) if f.get("id", "").isdigit() else 0
    try:
        olt = int(f["olt_id"]) if f.get("olt_id", "").isdigit() else None
        rx = round(float(f["rx_dbm"]), 2) if f.get("rx_dbm", "").strip() else None
        assert rx is None or -60 <= rx <= 10
        mac = f.get("mac", "").strip()
        if mac:
            mac = re.sub(r"[^0-9A-F]", "", mac.upper())
            assert len(mac) == 12
            mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
        status = f.get("status") if f.get("status") in dict(ONU_STATUS) else "online"
        vals = (olt, f.get("name", "").strip()[:64], f.get("serial", "").strip()[:64], mac,
                f.get("username", "").strip()[:64], f.get("pon_port", "").strip()[:32], rx, status,
                f.get("note", "").strip()[:255])
        assert vals[1] or vals[2] or mac
    except (ValueError, AssertionError, KeyError):
        flash("Give the ONU a name, serial or MAC, and a valid signal (-60 to 10 dBm).", "error")
        return redirect(url_for("onus"))
    if core.my_downline() is not None:
        if olt not in [r["id"] for r in _my_olts()]:
            flash("Pick one of your own OLTs for the ONU.", "error")
            return redirect(url_for("onus"))
        if oid:
            _own_onu(oid)
    if oid:
        x("UPDATE onus SET olt_id=%s, name=%s, serial=%s, mac=%s, username=%s, pon_port=%s, rx_dbm=%s, status=%s, "
          "note=%s WHERE id=%s", vals + (oid,))
    else:
        insert("INSERT INTO onus (olt_id, name, serial, mac, username, pon_port, rx_dbm, status, note) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", vals)
    audit("onu_saved", vals[1] or vals[2] or mac)
    commit()
    flash("ONU saved.", "success")
    return redirect(url_for("onus"))


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
