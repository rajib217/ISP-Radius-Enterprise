"""Network: IP pools and network devices (routers themselves stay on the Routers page)."""
import ipaddress
import re
import subprocess

from flask import abort, flash, redirect, render_template, request, url_for
from datetime import datetime

import core
from core import audit, commit, insert, q, route, x

DEVICE_KINDS = [("router", "Router"), ("olt", "OLT"), ("switch", "Switch"), ("ap", "Access point / radio"),
                ("server", "Server"), ("other", "Other")]
POOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")


def parse_ranges(text):
    """'10.0.0.2-10.0.0.254, 10.1.0.0/24' -> list of (first, last) integers. Raises ValueError."""
    out = []
    for part in re.split(r"[,\n;]+", text or ""):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [ipaddress.IPv4Address(p.strip()) for p in part.split("-", 1)]
            if int(a) > int(b):
                raise ValueError("%s is higher than %s" % (a, b))
            out.append((int(a), int(b)))
        elif "/" in part:
            net = ipaddress.IPv4Network(part, strict=False)
            hosts = list(net.hosts()) if net.prefixlen < 31 else list(net)
            out.append((int(hosts[0]), int(hosts[-1])))
        else:
            ip = ipaddress.IPv4Address(part)
            out.append((int(ip), int(ip)))
    if not out:
        raise ValueError("Enter at least one range")
    return out


def ranges_text(ranges):
    return ",".join("%s-%s" % (ipaddress.IPv4Address(a), ipaddress.IPv4Address(b)) if a != b
                    else str(ipaddress.IPv4Address(a)) for a, b in ranges)


def snippet(pool):
    ranges = parse_ranges(pool["ranges"])
    lines = ["/ip pool add name=%s ranges=%s" % (pool["name"], ranges_text(ranges))]
    prof = "/ppp profile add name=%s remote-address=%s" % (pool["name"], pool["name"])
    if pool.get("gateway"):
        prof += " local-address=%s" % pool["gateway"]
    lines.append(prof)
    return "\n".join(lines)


@route("/ip-pools", module="network")
def ip_pools():
    rows = q("SELECT p.*, n.shortname AS router FROM ip_pools p LEFT JOIN nas n ON n.id = p.nas_id ORDER BY p.name")
    online = [int(ipaddress.IPv4Address(r["framedipaddress"])) for r in q(
        "SELECT framedipaddress FROM radacct WHERE acctstoptime IS NULL AND framedipaddress <> ''")
        if _is_ip(r["framedipaddress"])]
    plan_count = {r["pool_name"]: r["c"] for r in q(
        "SELECT pool_name, COUNT(*) AS c FROM plans WHERE pool_name IS NOT NULL GROUP BY pool_name")}
    for r in rows:
        try:
            rng = parse_ranges(r["ranges"])
        except ValueError:
            rng = []
        r["size"] = sum(b - a + 1 for a, b in rng)
        r["used"] = sum(1 for ip in online if any(a <= ip <= b for a, b in rng))
        r["pct"] = int(100 * r["used"] / r["size"]) if r["size"] else 0
        r["plans"] = plan_count.get(r["name"], 0)
        r["snippet"] = snippet(r) if rng else ""
    routers = q("SELECT id, shortname FROM nas ORDER BY shortname")
    return render_template("ip_pools.html", rows=rows, routers=routers)


def _is_ip(text):
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


@route("/ip-pools/save", module="network", methods=["POST"])
def ip_pool_save():
    f = request.form
    pid = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, gateway = f.get("name", "").strip(), f.get("gateway", "").strip()
    try:
        assert POOL_NAME_RE.match(name)
        rng = parse_ranges(f.get("ranges", ""))
        if gateway:
            ipaddress.IPv4Address(gateway)
    except (ValueError, AssertionError) as exc:
        flash("Check the pool: name (letters, numbers, . _ -), ranges like 10.20.0.2-10.20.3.254 and a valid "
              "gateway. %s" % (exc if isinstance(exc, ValueError) else ""), "error")
        return redirect(url_for("ip_pools"))
    if q("SELECT id FROM ip_pools WHERE name = %s AND id <> %s", (name, pid), one=True):
        flash("A pool named %s already exists." % name, "error")
        return redirect(url_for("ip_pools"))
    nas_id = int(f["nas_id"]) if f.get("nas_id", "").isdigit() else None
    vals = (name, ranges_text(rng), gateway, nas_id, f.get("note", "").strip()[:255])
    if pid:
        x("UPDATE ip_pools SET name = %s, ranges = %s, gateway = %s, nas_id = %s, note = %s WHERE id = %s",
          vals + (pid,))
    else:
        insert("INSERT INTO ip_pools (name, ranges, gateway, nas_id, note) VALUES (%s, %s, %s, %s, %s)", vals)
    audit("ip_pool_saved", name)
    commit()
    flash("Pool saved. Create the same pool on the router with the commands shown under it.", "success")
    return redirect(url_for("ip_pools"))


@route("/ip-pools/<int:pid>/delete", module="network", methods=["POST"])
def ip_pool_delete(pid):
    row = q("SELECT name FROM ip_pools WHERE id = %s", (pid,), one=True) or abort(404)
    x("DELETE FROM ip_pools WHERE id = %s", (pid,))
    audit("ip_pool_deleted", row["name"])
    commit()
    flash("Pool %s removed from this list (the router is not changed)." % row["name"], "success")
    return redirect(url_for("ip_pools"))


# ===================================================================== devices
def _staff_only():
    """Generic devices are shared infrastructure: resellers only get their own OLTs (see mod_olt)."""
    if core.my_downline() is not None:
        abort(403)


@route("/devices", module="network")
def devices():
    if core.my_downline() is not None:
        return redirect(url_for("olts"))
    kind = request.args.get("kind") if request.args.get("kind") in dict(DEVICE_KINDS) else ""
    if kind == "olt":
        return redirect(url_for("olts"))
    rows = q("SELECT d.*, z.name AS zone_name, n.shortname AS router, "
             "(SELECT COUNT(*) FROM onus o WHERE o.olt_id = d.id) AS onus FROM net_devices d "
             "LEFT JOIN zones z ON z.id = d.zone_id LEFT JOIN nas n ON n.id = d.nas_id"
             + (" WHERE d.kind = %s" if kind else "") +
             " ORDER BY FIELD(d.last_status, 'down', 'unknown', 'up'), d.name", (kind,) if kind else None)
    counts = {"up": 0, "down": 0, "unknown": 0}
    for r in rows:
        counts[r["last_status"] if r["last_status"] in counts else "unknown"] += 1
    zones = q("SELECT id, name FROM zones ORDER BY name")
    return render_template("devices.html", rows=rows, zones=zones, kinds=DEVICE_KINDS, counts=counts, kind=kind,
                           routers=q("SELECT id, shortname FROM nas ORDER BY shortname"))


@route("/devices/save", module="network", methods=["POST"])
def device_save():
    _staff_only()
    f = request.form
    did = int(f["id"]) if f.get("id", "").isdigit() else 0
    name, ip = f.get("name", "").strip(), f.get("ip", "").strip()
    kind = f.get("kind") if f.get("kind") in dict(DEVICE_KINDS) else "other"
    if not 1 <= len(name) <= 128 or not _is_ip(ip):
        flash("Enter a device name and a valid IPv4 address.", "error")
        return redirect(url_for("devices"))
    zone = int(f["zone_id"]) if f.get("zone_id", "").isdigit() else None
    router = int(f["nas_id"]) if f.get("nas_id", "").isdigit() else None
    vals = (name, kind, ip, f.get("location", "").strip()[:128], zone, f.get("notes", "").strip()[:255], router)
    if did:
        x("UPDATE net_devices SET name = %s, kind = %s, ip = %s, location = %s, zone_id = %s, notes = %s, "
          "nas_id = %s WHERE id = %s", vals + (did,))
    else:
        insert("INSERT INTO net_devices (name, kind, ip, location, zone_id, notes, nas_id) "
               "VALUES (%s, %s, %s, %s, %s, %s, %s)", vals)
    audit("device_saved", name)
    commit()
    flash("Saved.", "success")
    return redirect(core.safe_next(f.get("next")) or url_for("devices"))


@route("/devices/<int:did>/delete", module="network", methods=["POST"])
def device_delete(did):
    _staff_only()
    row = q("SELECT name FROM net_devices WHERE id = %s", (did,), one=True) or abort(404)
    x("DELETE FROM net_devices WHERE id = %s", (did,))
    audit("device_deleted", row["name"])
    commit()
    flash("Device %s deleted." % row["name"], "success")
    return redirect(url_for("devices"))


def ping(ip):
    """True if the host answers one ICMP echo within a second. None if ping is not installed."""
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=5)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


def _check(device):
    result = ping(device["ip"])
    if result is None:
        return None
    x("UPDATE net_devices SET last_status = %s, last_checked = %s WHERE id = %s",
      ("up" if result else "down", datetime.now(), device["id"]))
    return result


@route("/devices/<int:did>/ping", module="network", methods=["POST"])
def device_ping(did):
    _staff_only()
    d = q("SELECT * FROM net_devices WHERE id = %s", (did,), one=True) or abort(404)
    result = _check(d)
    if result is None:
        flash("The ping command is not installed on the server (apt install iputils-ping).", "error")
        return redirect(url_for("devices"))
    commit()
    flash("%s (%s) is %s." % (d["name"], d["ip"], "reachable" if result else "NOT answering"),
          "success" if result else "error")
    return redirect(url_for("devices"))


@route("/devices/ping-all", module="network", methods=["POST"])
def device_ping_all():
    _staff_only()
    rows = q("SELECT * FROM net_devices ORDER BY name LIMIT 60")
    up = down = 0
    for d in rows:
        result = _check(d)
        if result is None:
            flash("The ping command is not installed on the server (apt install iputils-ping).", "error")
            return redirect(url_for("devices"))
        up += bool(result)
        down += not result
    commit()
    flash("Checked %d device%s: %d reachable, %d not answering." % (len(rows), "" if len(rows) == 1 else "s", up, down),
          "success" if not down else "warn")
    return redirect(url_for("devices"))
