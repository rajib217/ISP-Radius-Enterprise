"""OLTs: add / edit with SNMP settings, read ONUs from the OLT, SNMP test and walk."""
import logging
import re
import threading
import time

from flask import abort, g, flash, redirect, render_template, request, url_for

import core
import db
import oltsync
import snmplib
from core import audit, commit, get_db, insert, q, route, x
from mod_network import _is_ip

log = logging.getLogger(__name__)

PON_TYPES = ["EPON", "GPON", "XGS-PON", "XG-PON"]
OID_RE = re.compile(r"^\d+(\.\d+){3,}$")
COMMUNITY_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
SNMP_COLS = ("vendor", "model", "pon_type", "version", "community", "port", "timeout", "retries", "auto_poll",
             "if_regex", "oid_status", "oid_name", "oid_serial", "oid_rx", "oid_mac", "status_map", "rx_div", "oid_tx", "oid_temp", "temp_div", "oid_volt", "oid_curr", "oid_dist",
             "volt_div", "curr_div")


def _row(did):
    sc, sp = core.olt_scope("d")
    d = q("SELECT d.*, s.* FROM net_devices d LEFT JOIN olt_snmp s ON s.device_id = d.id "
          "WHERE d.id = %s AND d.kind = 'olt'" + sc, [did] + sp, one=True)
    return d or abort(404)


def _cfg(d):
    """SNMP settings with defaults for an OLT that has none saved yet."""
    base = {"vendor": "other", "model": "", "pon_type": "EPON", "version": "v2c", "community": "public",
            "port": 161, "timeout": 5, "retries": 3, "auto_poll": 1, "if_regex": "", "oid_status": "",
            "oid_name": "", "oid_serial": "", "oid_rx": "", "oid_mac": "", "status_map": "", "rx_div": 1, "oid_tx": "", "oid_temp": "", "temp_div": 1, "oid_volt": "", "oid_curr": "",
            "oid_dist": "", "volt_div": 1, "curr_div": 1}
    base.update({k: d[k] for k in SNMP_COLS if d.get(k) not in (None,)})
    return base


@route("/olts", module="network")
def olts():
    sc, sp = core.olt_scope("d")
    rows = q("SELECT d.id, d.name, d.ip, d.location, d.last_status, d.last_checked, n.shortname AS router, "
             "s.vendor, s.model, s.pon_type, s.auto_poll, s.last_poll, s.last_ok, s.last_msg, "
             "(SELECT COUNT(*) FROM onus o WHERE o.olt_id = d.id) AS total, "
             "(SELECT COUNT(*) FROM onus o WHERE o.olt_id = d.id AND o.status = 'online') AS online, "
             "(SELECT COUNT(*) FROM onus o WHERE o.olt_id = d.id AND o.status = 'los') AS los, "
             "s.device_id AS has_snmp FROM net_devices d LEFT JOIN olt_snmp s ON s.device_id = d.id "
             "LEFT JOIN nas n ON n.id = d.nas_id WHERE d.kind = 'olt'" + sc + " ORDER BY d.name", sp)
    sums = {"total": sum(r["total"] for r in rows), "online": sum(r["online"] for r in rows),
            "los": sum(r["los"] for r in rows)}
    sums["offline"] = sums["total"] - sums["online"] - sums["los"]
    return render_template("olts.html", rows=rows, sums=sums, vendors=dict(oltsync.VENDORS))


def _form(d):
    cfg = _cfg(d) if d else _cfg({})
    return render_template("olt_form.html", d=d or {}, c=cfg, vendors=oltsync.VENDORS, pon_types=PON_TYPES,
                           presets=oltsync.PRESETS, routers=[] if core.my_downline() is not None else q("SELECT id, shortname FROM nas ORDER BY shortname"),
                           default_regex=oltsync.DEFAULT_REGEX)


@route("/olts/new", module="network")
def olt_new():
    return _form(None)


@route("/olts/<int:did>/edit", module="network")
def olt_edit(did):
    return _form(_row(did))


@route("/olts/save", module="network", methods=["POST"])
def olt_save():
    f = request.form
    did = int(f["id"]) if f.get("id", "").isdigit() else 0
    back = url_for("olt_edit", did=did) if did else url_for("olt_new")
    try:
        name, ip = f.get("name", "").strip(), f.get("ip", "").strip()
        if not 1 <= len(name) <= 128:
            raise ValueError("Enter the OLT name.")
        if not _is_ip(ip):
            raise ValueError("Enter a valid IPv4 address for the OLT.")
        vendor = f.get("vendor", "")
        if vendor not in dict(oltsync.VENDORS):
            raise ValueError("Select the vendor.")
        pon = f.get("pon_type") if f.get("pon_type") in PON_TYPES else ""
        if not pon:
            raise ValueError("Select the PON type.")
        version = f.get("version") if f.get("version") in ("v1", "v2c") else "v2c"
        community = f.get("community", "").strip()
        if not COMMUNITY_RE.match(community):
            raise ValueError("Enter the SNMP community (no spaces).")
        port, timeout, retries = int(f.get("port") or 161), int(f.get("timeout") or 5), int(f.get("retries") or 3)
        if not (1 <= port <= 65535 and 1 <= timeout <= 30 and 0 <= retries <= 10):
            raise ValueError("Check the SNMP port (1-65535), timeout (1-30 s) and retries (0-10).")
        oids = {}
        for k in ("oid_status", "oid_name", "oid_serial", "oid_rx", "oid_tx", "oid_temp", "oid_volt", "oid_curr", "oid_dist", "oid_mac"):
            v = f.get(k, "").strip().strip(".")
            if v and not OID_RE.match(v):
                raise ValueError("%s is not a valid OID (numbers separated by dots)." % v)
            oids[k] = v
        smap = f.get("status_map", "").strip()
        if smap and not oltsync.status_map(smap):
            raise ValueError("Status values look like 1=online,2=offline,3=los.")
        rx_regex = f.get("if_regex", "").strip()[:128]
        if rx_regex:
            re.compile(rx_regex)
        rx_div = max(int(f.get("rx_div") or 1), 1)
        temp_div = max(int(f.get("temp_div") or 1), 1)
        volt_div = max(int(f.get("volt_div") or 1), 1)
        curr_div = max(int(f.get("curr_div") or 1), 1)
    except re.error:
        flash("The ONU interface pattern is not a valid regular expression.", "error")
        return redirect(back)
    except ValueError as exc:
        flash(str(exc) if " " in str(exc) else "Check the SNMP numbers.", "error")
        return redirect(back)
    router = int(f["nas_id"]) if f.get("nas_id", "").isdigit() else None
    if core.my_downline() is not None:      # sellers do not pick routers; keep what an edit already had
        router = _row(did)["nas_id"] if did else None
    dev = (name, ip, f.get("location", "").strip()[:128], f.get("notes", "").strip()[:255], router)
    if did:
        _row(did)
        x("UPDATE net_devices SET name = %s, ip = %s, location = %s, notes = %s, nas_id = %s WHERE id = %s",
          dev + (did,))
    else:
        did = insert("INSERT INTO net_devices (name, kind, ip, location, notes, nas_id, created_by) "
                     "VALUES (%s, 'olt', %s, %s, %s, %s, %s)", (name, ip) + dev[2:] + (g.me["username"],))
    x("INSERT INTO olt_snmp (device_id, vendor, model, pon_type, version, community, port, timeout, retries, "
      "auto_poll, if_regex, oid_status, oid_name, oid_serial, oid_rx, oid_mac, status_map, rx_div, oid_tx, oid_temp, temp_div, oid_volt, oid_curr, oid_dist, volt_div, curr_div) "
      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE vendor=VALUES(vendor), "
      "model=VALUES(model), pon_type=VALUES(pon_type), version=VALUES(version), community=VALUES(community), "
      "port=VALUES(port), timeout=VALUES(timeout), retries=VALUES(retries), auto_poll=VALUES(auto_poll), "
      "if_regex=VALUES(if_regex), oid_status=VALUES(oid_status), oid_name=VALUES(oid_name), "
      "oid_serial=VALUES(oid_serial), oid_rx=VALUES(oid_rx), oid_mac=VALUES(oid_mac), status_map=VALUES(status_map), "
      "rx_div=VALUES(rx_div), oid_tx=VALUES(oid_tx), oid_temp=VALUES(oid_temp), temp_div=VALUES(temp_div), oid_volt=VALUES(oid_volt), oid_curr=VALUES(oid_curr), "
      "oid_dist=VALUES(oid_dist), volt_div=VALUES(volt_div), curr_div=VALUES(curr_div)",
      (did, vendor, f.get("model", "").strip()[:64], pon, version, community, port, timeout, retries,
       1 if f.get("auto_poll") == "1" else 0, rx_regex, oids["oid_status"], oids["oid_name"], oids["oid_serial"],
       oids["oid_rx"], oids["oid_mac"], smap, rx_div, oids["oid_tx"], oids["oid_temp"], temp_div, oids["oid_volt"], oids["oid_curr"], oids["oid_dist"],
       volt_div, curr_div))
    audit("olt_saved", name)
    commit()
    flash("OLT saved. Use Sync ONUs to read its ONU list.", "success")
    return redirect(url_for("olts"))


def drop_olt(did):
    """Delete an OLT with every ONU under it (synced or added by hand) and their signal history.
    Returns how many ONUs went. The caller commits."""
    n = q("SELECT COUNT(*) AS n FROM onus WHERE olt_id = %s", (did,), one=True)["n"]
    x("DELETE m FROM onu_metrics m JOIN onus o ON o.id = m.onu_id WHERE o.olt_id = %s", (did,))
    x("DELETE FROM onus WHERE olt_id = %s", (did,))
    x("DELETE FROM olt_snmp WHERE device_id = %s", (did,))
    x("DELETE FROM net_devices WHERE id = %s", (did,))
    return n


@route("/olts/<int:did>/delete", module="network", methods=["POST"])
def olt_delete(did):
    d = _row(did)
    n = drop_olt(did)
    audit("olt_deleted", "%s (%d ONUs)" % (d["name"], n))
    commit()
    flash("OLT %s and its %d ONU(s) deleted." % (d["name"], n), "success")
    return redirect(url_for("olts"))


@route("/olts/<int:did>/sync", module="network", methods=["POST"])
def olt_sync(did):
    d = _row(did)
    if not d.get("device_id"):
        flash("Save the SNMP settings of this OLT first.", "error")
        return redirect(url_for("olt_edit", did=did))
    with get_db().cursor() as cur:
        ok, msg = oltsync.poll(cur, d, _cfg(d))
    commit()
    flash("%s: %s." % (d["name"], msg) if ok else "%s: %s" % (d["name"], msg), "success" if ok else "error")
    return redirect(core.safe_next(request.form.get("next")) or url_for("olts"))


_SYNC_LOCK = threading.Lock()


def _sync_in_background(devices):
    """Read each OLT on its own database connection, so the web request that started it returns at once.
    Reading 40 OLTs over SNMP can take minutes; doing it inside the request held a panel worker the whole time."""
    try:
        conn = db.connect()
    except Exception:
        _SYNC_LOCK.release()
        log.exception("OLT sync-all: cannot connect to the database")
        return
    try:
        for d, cfg in devices:
            try:
                with conn.cursor() as cur:
                    oltsync.poll(cur, d, cfg)
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("OLT sync-all: %s failed", d.get("name") or d.get("ip"))
    finally:
        conn.close()
        _SYNC_LOCK.release()


@route("/olts/sync-all", module="network", methods=["POST"])
def olt_sync_all():
    back = url_for("onus") if "onus" in (request.referrer or "") else url_for("olts")
    if not _SYNC_LOCK.acquire(blocking=False):
        flash("A sync of all OLTs is already running. Refresh this page in a minute.", "warn")
        return redirect(back)
    try:
        sc, sp = core.olt_scope("d")
        rows = q("SELECT d.*, s.* FROM net_devices d JOIN olt_snmp s ON s.device_id = d.id WHERE d.kind = 'olt'" + sc +
                 " LIMIT 40", sp)
        devices = [(d, _cfg(d)) for d in rows]
        threading.Thread(target=_sync_in_background, args=(devices,), daemon=True, name="olt-sync-all").start()
    except Exception:
        _SYNC_LOCK.release()
        raise
    flash("Reading %d OLT%s in the background. Refresh this page in a minute to see the results."
          % (len(devices), "" if len(devices) == 1 else "s"), "success")
    return redirect(back)


@route("/olts/<int:did>/test", module="network", methods=["POST"])
def olt_test(did):
    d = _row(did)
    c = _cfg(d)
    cl = oltsync.client(d, c)
    try:
        descr, name, up = cl.get("1.3.6.1.2.1.1.1.0"), cl.get("1.3.6.1.2.1.1.5.0"), cl.get("1.3.6.1.2.1.1.3.0")
        days = (up or 0) // 8640000
        flash("SNMP works. %s - %s (up %d days)." % (snmplib.text(name) or d["name"], snmplib.text(descr)[:120], days),
              "success")
    except snmplib.SnmpError as exc:
        flash("SNMP test failed: %s" % exc, "error")
    finally:
        cl.close()
    return redirect(url_for("olts"))


@route("/olts/<int:did>/walk", module="network")
def olt_walk(did):
    d = _row(did)
    oid = request.args.get("oid", "1.3.6.1.2.1.1").strip()
    rows, error = [], ""
    if request.args.get("oid"):
        cl = oltsync.client(d, _cfg(d))
        try:
            for o, v in cl.walk(oid, max_rows=300):
                rows.append((snmplib.fmt_oid(o), snmplib.text(v)[:200]))
        except snmplib.SnmpError as exc:
            error = str(exc)
        finally:
            cl.close()
    return render_template("olt_walk.html", d=d, oid=oid, rows=rows, error=error, ran=bool(request.args.get("oid")))
