"""Field work: the customer call log and follow-ups, customer locations and the customer map, and the technician's
job list (tickets assigned to them, finished with a note and a photo).

  /subscribers/<sid>/calls      add a call note (outcome, note, call again on)      subscriber page > Call Log
  /calls/<cid>/done             a follow-up is finished
  /follow-ups                   follow-ups due today / overdue / coming up            Subscribers > Follow-ups
  /subscribers/<sid>/location   save the customer's map position (or a pasted Google Maps link)
  /map, /map/data               every customer with a position: online green, offline grey, expired red
  /jobs                         the technician's open tickets, phone and map one tap away   Tickets > My jobs
  /jobs/<tid>/start, /done      "work started" / "done" (note + optional photo) on a ticket
  /tickets/photo/<name>         a job photo
Sellers see only their own downline's customers everywhere here (core.scope_sql / the sid check in app.gate)."""
import os
import re
import secrets
from datetime import date, datetime, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import core
import db
import mod_tickets
from core import audit, commit, insert, pager, q, route, x

OUTCOMES = [("called", "Talked"), ("no_answer", "No answer"), ("promised", "Promised to pay"), ("paid", "Paid"),
            ("complaint", "Complaint"), ("switched_off", "Phone off"), ("other", "Other"),
            # a bill collector's door visits (mod_collect.collect_visit)
            ("visit_not_home", "Visited: not home"), ("visit_later", "Visited: will pay later"),
            ("visit_refused", "Visited: refused"), ("visit_no_addr", "Visited: address not found")]
PHOTO_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg"}
PHOTO_MAX = 5 * 1024 * 1024


# ===================================================================== call log
def calls_for(sid, limit=100):
    """A customer's call notes, newest first (subscriber page)."""
    return q("SELECT id, outcome, note, follow_up, done, created_by, created_at FROM sub_calls WHERE subscriber_id = %s "
             "ORDER BY id DESC LIMIT %s", (sid, limit))


def last_calls(ids):
    """{subscriber_id: newest call row} for a page of customers (Dues list)."""
    if not ids:
        return {}
    rows = q("SELECT c.subscriber_id, c.outcome, c.note, c.follow_up, c.created_at, c.created_by FROM sub_calls c "
             "JOIN (SELECT subscriber_id, MAX(id) AS mid FROM sub_calls WHERE subscriber_id IN (%s) GROUP BY subscriber_id) m "
             "ON m.mid = c.id" % ",".join(["%s"] * len(ids)), list(ids))
    return {r["subscriber_id"]: r for r in rows}


@route("/subscribers/<int:sid>/calls", module="subscribers", methods=["POST"])
def call_add(sid):
    sub = q("SELECT id, username FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    f = request.form
    back = redirect(core.safe_next(f.get("next")) or (url_for("subscriber_view", sid=sid) + "#calls"))
    outcome = f.get("outcome") if f.get("outcome") in dict(OUTCOMES) else "other"
    note = (f.get("note") or "").strip()[:500]
    try:
        follow = core.parse_date(f.get("follow_up"))
    except ValueError:
        flash("Pick a valid date to call again, or leave it empty.", "error")
        return back
    if follow and follow < date.today():
        flash("The call-again date is in the past.", "error")
        return back
    if not note and outcome == "other" and not follow:
        flash("Write what was said, or pick an outcome.", "error")
        return back
    # a new note settles the customer's earlier open follow-ups: the newest one is what counts now
    x("UPDATE sub_calls SET done = 1 WHERE subscriber_id = %s AND done = 0", (sid,))
    insert("INSERT INTO sub_calls (subscriber_id, outcome, note, follow_up, created_by) VALUES (%s, %s, %s, %s, %s)",
           (sid, outcome, note, follow, g.me["username"]))
    audit("call_logged", sub["username"], "%s%s" % (dict(OUTCOMES)[outcome], (", call again " + follow.isoformat()) if follow else ""))
    commit()
    flash("Call saved." + (" Reminder on %s under Subscribers > Follow-ups." % follow.strftime("%d %b") if follow else ""), "success")
    return back


def _call_row(cid):
    c = q("SELECT c.*, s.created_by AS owner, s.username FROM sub_calls c JOIN subscribers s ON s.id = c.subscriber_id "
          "WHERE c.id = %s", (cid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and c["owner"] not in names:
        abort(403)
    return c


@route("/calls/<int:cid>/done", module="subscribers", methods=["POST"])
def call_done(cid):
    c = _call_row(cid)
    x("UPDATE sub_calls SET done = 1 WHERE id = %s", (cid,))
    audit("follow_up_done", c["username"])
    commit()
    flash("Follow-up marked done.", "success")
    return redirect(core.safe_next(request.form.get("next")) or url_for("follow_ups"))


@route("/follow-ups", module="subscribers")
def follow_ups():
    when = request.args.get("when", "due")
    today = date.today()
    where = ["c.done = 0", "c.follow_up IS NOT NULL"]
    params = []
    if when == "upcoming":
        where.append("c.follow_up > %s")
    else:
        when = "due"
        where.append("c.follow_up <= %s")
    params.append(today)
    if request.args.get("mine") == "1":
        where.append("c.created_by = %s")
        params.append(g.me["username"])
    sc, sp = core.scope_sql("s")
    cond = " WHERE " + " AND ".join(where) + sc
    params += sp
    join = " FROM sub_calls c JOIN subscribers s ON s.id = c.subscriber_id LEFT JOIN plans p ON p.id = s.plan_id"
    total = q("SELECT COUNT(*) AS c" + join + cond, params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT c.*, s.username, s.full_name, s.phone, s.area, s.expiry_date, p.price, p.label AS plan_label, "
             "DATEDIFF(%s, c.follow_up) AS late" + join + cond +
             " ORDER BY c.follow_up, c.id LIMIT %s OFFSET %s", [today] + params + [pg["per"], pg["offset"]])
    for r in rows:
        r["code"] = core.sub_code(r["subscriber_id"])
    counts = q("SELECT SUM(c.follow_up <= %s) AS due, SUM(c.follow_up > %s) AS upcoming" + join +
               " WHERE c.done = 0 AND c.follow_up IS NOT NULL" + sc, [today, today] + sp, one=True)
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("follow_ups.html", rows=rows, pg=pg, keep=keep, when=when, outcomes=dict(OUTCOMES),
                           counts={"due": int(counts["due"] or 0), "upcoming": int(counts["upcoming"] or 0)})


# ===================================================================== customer location and map
_PAIR = re.compile(r"(-?\d{1,3}\.\d+)\s*(?:,|\s)\s*\+?(-?\d{1,3}\.\d+)")
_AT = re.compile(r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)")
_PIN = re.compile(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)")          # the exact pin inside a Google Maps place link
_Q = re.compile(r"[?&](?:q|query|ll|destination)=(-?\d{1,3}\.\d+),\+?(-?\d{1,3}\.\d+)", re.I)
# the "Share" button of the Google Maps app gives a short link with no position in it: the panel asks Google where it
# leads (only for these hosts) and reads the position from the address it points to
_SHORT = re.compile(r"https?://(?:maps\.app\.goo\.gl|goo\.gl/maps)/[A-Za-z0-9_\-/?=&.]+", re.I)


def _expand_short(url):
    """Follow a Google Maps short link (up to 3 redirects, without downloading the page) and return where it leads."""
    import urllib.error
    import urllib.request

    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoFollow)
    for _hop in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ISP panel; location)"})
        try:
            opener.open(req, timeout=6).close()
            return url
        except urllib.error.HTTPError as e:
            nxt = e.headers.get("Location")
            if e.code not in (301, 302, 303, 307, 308) or not nxt:
                return url
            url = nxt
            if not _SHORT.match(url):
                return url
    return url


def parse_location(text):
    """(lat, lng) from '23.8103, 90.4125', a Google Maps link (…/@23.81,90.41,17z or ?q=23.81,90.41), or None.
    Raises ValueError for text that is not a position."""
    text = (text or "").strip()
    if not text:
        return None
    short = _SHORT.search(text)
    if short:
        try:
            text = _expand_short(short.group(0))
        except Exception:
            raise ValueError("Could not open that Google Maps short link. In Google Maps, touch and hold the spot "
                             "to drop a pin, then copy the numbers it shows (like 23.8103, 90.4125) and paste them.")
    from urllib.parse import unquote
    text = unquote(text)
    m = _PIN.search(text) or _AT.search(text) or _Q.search(text) or _PAIR.search(text)
    if not m:
        raise ValueError("Paste a Google Maps link or type the position as 23.8103, 90.4125.")
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        raise ValueError("That position is outside the map.")
    return round(lat, 6), round(lng, 6)


@route("/subscribers/<int:sid>/location", module="subscribers", methods=["POST"])
def location_save(sid):
    sub = q("SELECT id, username FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    back = redirect(core.safe_next(request.form.get("next")) or url_for("subscriber_view", sid=sid))
    if request.form.get("remove"):
        x("UPDATE subscribers SET lat = NULL, lng = NULL WHERE id = %s", (sid,))
        audit("location_removed", sub["username"])
        commit()
        flash("Location removed.", "success")
        return back
    try:
        pos = parse_location(request.form.get("where"))
    except ValueError as e:
        flash(str(e), "error")
        return back
    if not pos:
        flash("Paste a Google Maps link, type the position, or press Use my location.", "error")
        return back
    x("UPDATE subscribers SET lat = %s, lng = %s WHERE id = %s", (pos[0], pos[1], sid))
    audit("location_saved", sub["username"], "%s, %s" % pos)
    commit()
    flash("Location saved: it shows on Subscribers > Map.", "success")
    return back


@route("/map", module="subscribers")
def customer_map():
    sc, sp = core.scope_sql("s")
    n = q("SELECT COUNT(*) AS c, SUM(lat IS NOT NULL) AS placed FROM subscribers s WHERE 1 = 1" + sc, sp, one=True)
    s = core.cfg()
    centre = None
    try:
        if s.get("latitude") and s.get("longitude"):
            centre = [float(s["latitude"]), float(s["longitude"])]
    except ValueError:
        centre = None
    return render_template("customer_map.html", total=int(n["c"] or 0), placed=int(n["placed"] or 0),
                           areas=core.area_names(), centre=centre)


@route("/map/data", module="subscribers")
def customer_map_data():
    sc, sp = core.scope_sql("s")
    rows = q("SELECT s.id, s.username, s.full_name, s.phone, s.area, s.lat, s.lng, s.status, s.expiry_date, "
             "s.expiry_date IS NOT NULL AND " + db.PKG_END + " <= NOW() AS ended, "
             "p.label AS plan FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
             "WHERE s.lat IS NOT NULL AND s.lng IS NOT NULL" + sc + " LIMIT 20000", sp)
    online = {r["username"] for r in q("SELECT DISTINCT username FROM radacct WHERE acctstoptime IS NULL")}
    today = date.today()
    out = []
    for r in rows:
        if r["status"] == "suspended":
            state = "suspended"
        elif r["ended"]:                  # at the package's real end time, as the lists and RADIUS
            state = "expired"
        else:
            state = "online" if r["username"] in online else "offline"
        out.append({"id": r["id"], "code": core.sub_code(r["id"]), "u": r["username"], "n": r["full_name"] or "",
                    "ph": r["phone"] or "", "a": r["area"] or "", "p": r["plan"] or "", "s": state,
                    "lat": float(r["lat"]), "lng": float(r["lng"]), "url": url_for("subscriber_view", sid=r["id"])})
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ===================================================================== technician jobs
def _job(tid):
    t = q("SELECT t.*, s.id AS sid, s.full_name AS sub_name, s.phone AS sub_phone, s.address AS sub_address, "
          "s.area AS sub_area, s.lat, s.lng, s.created_by AS owner FROM tickets t "
          "LEFT JOIN subscribers s ON s.id = t.subscriber_id WHERE t.id = %s", (tid,), one=True) or abort(404)
    if t["assigned_to"] and t["assigned_to"] != g.me["id"] and g.me["role"] not in ("admin", "manager"):
        abort(403)                       # someone else's job: only an administrator or manager acts on it
    names = core.my_downline()
    if names is not None and (t["seller"] not in names or (t["to_office"] and t["assigned_to"] != g.me["id"])):
        abort(403)                       # a reseller's technician: only that reseller's own tickets, not the office's
    if names is None and g.me["role"] == "technician" and not t["to_office"] and t["assigned_to"] != g.me["id"]:
        abort(403)                       # an office technician does not take a ticket the reseller fixes themselves
    return t


def _free_sql(alias="t"):
    """(sql, params) for jobs nobody has yet that this user may take: the office's tickets for office staff,
    the reseller's self-handled tickets for a reseller's technician."""
    names = core.my_downline()
    if names is None:
        return "%s.assigned_to IS NULL AND %s.to_office = 1" % (alias, alias), []
    return ("%s.assigned_to IS NULL AND %s.to_office = 0 AND %s.seller IN (%s)"
            % (alias, alias, alias, ",".join(["%s"] * len(names)))), list(names)


def job_rows(where, params, order="FIELD(t.priority, 'urgent', 'high', 'normal', 'low'), t.created_at", limit=200):
    """Tickets with the customer's phone, address and position and the last public note (sellers: their own only)."""
    where, params = list(where), list(params)
    names = core.my_downline()
    if names is not None:
        where.append("t.seller IN (%s)" % ",".join(["%s"] * len(names)))
        params += list(names)
    return q("SELECT t.*, s.id AS sid, s.full_name AS sub_name, s.phone AS sub_phone, s.address AS sub_address, "
             "s.area AS sub_area, s.lat, s.lng, "
             "(SELECT r.body FROM ticket_replies r WHERE r.ticket_id = t.id AND r.internal = 0 ORDER BY r.id DESC LIMIT 1) AS last_note "
             "FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id WHERE " + " AND ".join(where) +
             " ORDER BY " + order + " LIMIT %d" % int(limit), params)


def _job_back(dialog=""):
    """Back to the page the button was on; with dialog, that job's Done box opens again (wrong code, materials)."""
    url = (core.safe_next(request.form.get("next")) or url_for("jobs")).split("#")[0]
    return redirect(url + ("#" + dialog if dialog else ""))


@route("/jobs", module="tickets")
def jobs():
    show = request.args.get("show", "mine")
    where = ["t.status IN ('open', 'pending')"]
    params = []
    if show == "free":
        sql, fp = _free_sql()
        where.append(sql)
        params += fp
    elif show == "done":
        where = ["t.status IN ('resolved', 'closed')", "t.assigned_to = %s", "t.closed_at >= NOW() - INTERVAL 7 DAY"]
        params.append(g.me["id"])
    else:
        show = "mine"
        where.append("t.assigned_to = %s")
        params.append(g.me["id"])
    rows = job_rows(where, params)
    import mod_tickets
    sql, fp = _free_sql()
    counts = q("SELECT SUM(t.status IN ('open', 'pending') AND t.assigned_to = %s) AS mine, "
               "SUM(t.status IN ('open', 'pending') AND " + sql + ") AS free FROM tickets t", [g.me["id"]] + fp, one=True)
    return render_template("jobs.html", rows=rows, show=show, categories=mod_tickets.ALL_CATEGORIES, stock=stock_items(),
                           otp_on=core.cfg().get("ticket_otp_on") == "1",
                           priorities=dict(mod_tickets.PRIORITIES),
                           counts={"mine": int(counts["mine"] or 0), "free": int(counts["free"] or 0)})


@route("/jobs/<int:tid>/start", module="tickets", methods=["POST"])
def job_start(tid):
    t = _job(tid)
    if t["status"] not in ("open", "pending"):
        flash("This job is already finished.", "warn")
        return _job_back()
    who = t["assigned_to"] or g.me["id"]          # taking a free job makes it yours
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
           (tid, g.me["username"], "Work started."))
    x("UPDATE tickets SET status = 'pending', assigned_to = %s, updated_at = NOW() WHERE id = %s", (who, tid))
    audit("job_started", "#%d" % tid)
    mod_tickets.watch_alert(tid, "started", "Work started")
    commit()
    flash("Job #%d started." % tid, "success")
    return _job_back()


def _save_job_photo(tid):
    """The uploaded photo of the finished work, saved under DATA_DIR/ticket_photos. Returns (name, error)."""
    up = request.files.get("photo")
    if not up or not up.filename:
        return "", ""
    data = up.read(PHOTO_MAX + 1)
    ext = next((v for k, v in PHOTO_TYPES.items() if data.startswith(k)), None)
    if not ext or len(data) > PHOTO_MAX:
        return "", "The photo must be a JPG or PNG up to 5 MB."
    folder = os.path.join(core.DATA_DIR, "ticket_photos")
    os.makedirs(folder, exist_ok=True)
    name = "t%d-%s.%s" % (tid, secrets.token_hex(8), ext)
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data)
    return name, ""


@route("/jobs/<int:tid>/done", module="tickets", methods=["POST"])
def job_done(tid):
    t = _job(tid)
    if t["status"] not in ("open", "pending"):
        flash("This job is already finished.", "warn")
        return _job_back()
    note = (request.form.get("note") or "").strip()[:5000]
    if len(note) < 3:
        flash("Write what was done (a few words) before marking the job done.", "error")
        return _job_back()
    otp_err = _check_otp(t)
    if otp_err:
        flash(otp_err, "error")
        return _job_back("done-%d" % tid)
    mats, err = _materials(t)
    if err:
        flash(err, "error")
        return _job_back("done-%d" % tid)
    photo, err = _save_job_photo(tid)
    if err:
        flash(err, "error")
        return _job_back("done-%d" % tid)
    _use_materials(t, mats)
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal, photo) VALUES (%s, %s, %s, 0, %s)",
           (tid, g.me["username"], "Done: " + note, photo))
    x("UPDATE tickets SET status = 'resolved', closed_at = NOW(), assigned_to = %s, first_reply_at = COALESCE(first_reply_at, NOW()), "
      "updated_at = NOW() WHERE id = %s", (t["assigned_to"] or g.me["id"], tid))
    mod_tickets.done(t)
    audit("job_done", "#%d" % tid, note[:200])
    mod_tickets.watch_alert(tid, "done", "Job done", note)
    commit()
    flash("Job #%d marked done." % tid, "success")
    return _job_back()


# ---- the visit: "I'm here" (time and GPS), materials from stock, the customer's code
def stock_items():
    """Stock a technician may record as used (name, unit, how many left); they never see the Inventory pages."""
    try:
        return q("SELECT id, name, unit, qty FROM inventory_items WHERE qty > 0 ORDER BY category, name LIMIT 300")
    except Exception:
        return []


def _materials(t):
    """[(item, qty)] from the form's item / qty rows, or an error when something is not in stock."""
    out = []
    items = request.form.getlist("item")
    qtys = request.form.getlist("qty")
    for iid, n in zip(items, qtys):
        if not iid.isdigit() or not n.strip():
            continue
        if not n.strip().isdigit() or not 1 <= int(n) <= 10000:
            return [], "Materials: the amount must be a whole number."
        it = q("SELECT id, name, unit, qty FROM inventory_items WHERE id = %s", (int(iid),), one=True)
        if not it:
            return [], "Materials: that item is not in stock any more."
        if it["qty"] < int(n):
            return [], "Materials: only %d %s of %s left in stock." % (it["qty"], it["unit"], it["name"])
        out.append((it, int(n)))
    return out, ""


def _use_materials(t, mats):
    for it, n in mats:
        x("UPDATE inventory_items SET qty = qty - %s WHERE id = %s", (n, it["id"]))
        insert("INSERT INTO inventory_moves (item_id, kind, qty, subscriber_id, username, note, by_user) "
               "VALUES (%s, 'out', %s, %s, %s, %s, %s)", (it["id"], n, t.get("sid"), t.get("username") or "",
                                                          "Ticket #%d" % t["id"], g.me["username"]))
        insert("INSERT INTO ticket_materials (ticket_id, item_id, qty, by_user) VALUES (%s, %s, %s, %s)",
               (t["id"], it["id"], n, g.me["username"]))
    if mats:
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 1)",
               (t["id"], g.me["username"], "Materials used: " + ", ".join("%d %s %s" % (n, it["unit"], it["name"]) for it, n in mats)))


def otp_needed(t):
    return core.cfg().get("ticket_otp_on") == "1" and bool(t.get("sid")) and bool((t.get("sub_phone") or "").strip())


def _otp_hash(salt, code):
    import hashlib
    return hashlib.sha256(("%s|%s" % (salt, code)).encode()).hexdigest()


def _check_otp(t):
    if not otp_needed(t):
        return ""
    if request.form.get("skip_otp") == "1" and g.me["role"] in ("admin", "manager"):
        insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 1)",
               (t["id"], g.me["username"], "Finished without the customer's code."))
        return ""
    code = (request.form.get("otp") or "").strip()
    if not t.get("otp_hash"):
        return "Press \"Send code\": the customer gets a code by SMS to give you when the work is done."
    if t["otp_tries"] >= 5:
        return "Too many wrong codes. Send a new code to the customer."
    salt, _sep, h = t["otp_hash"].partition("$")
    if not code.isdigit() or not secrets.compare_digest(_otp_hash(salt, code), h):
        x("UPDATE tickets SET otp_tries = otp_tries + 1 WHERE id = %s", (t["id"],))
        commit()
        return "That code is not right. Ask the customer for the code in their SMS."
    x("UPDATE tickets SET otp_ok = 1 WHERE id = %s", (t["id"],))
    return ""


@route("/jobs/<int:tid>/otp", module="tickets", methods=["POST"])
def job_otp(tid):
    """Send the customer a 6-digit code by SMS (once a minute at most)."""
    t = _job(tid)
    if t["status"] not in ("open", "pending") or not otp_needed(t):
        flash("No code is needed for this job.", "warn")
        return _job_back()
    if t["otp_sent_at"] and (datetime.now() - t["otp_sent_at"]).total_seconds() < 60:
        flash("A code was sent less than a minute ago. Wait a little before sending another.", "warn")
        return _job_back("done-%d" % tid)
    code = "%06d" % secrets.randbelow(1000000)
    salt = secrets.token_hex(8)
    x("UPDATE tickets SET otp_hash = %s, otp_sent_at = NOW(), otp_tries = 0, otp_ok = 0 WHERE id = %s",
      ("%s$%s" % (salt, _otp_hash(salt, code)), tid))
    import ticketlib
    with core.get_db().cursor() as cur:
        sub = ticketlib._sub(cur, t["sid"])
        n = ticketlib.sms_to(cur, core.cfg(), sub, "ticket_otp", {"ticket": tid, "code": code},
                             "otp:%d:%s" % (tid, salt), force=True)
    audit("job_otp_sent", "#%d" % tid)
    commit()
    flash("Code sent to the customer's phone. Ask them for it when the work is done." if n else
          "The SMS could not be queued (check the customer's phone number and SMS settings).", "success" if n else "error")
    return _job_back("done-%d" % tid)


@route("/jobs/<int:tid>/arrived", module="tickets", methods=["POST"])
def job_arrived(tid):
    """The technician reached the customer: time and (when the phone allows) position, compared with the saved one."""
    t = _job(tid)
    if t["status"] not in ("open", "pending"):
        flash("This job is already finished.", "warn")
        return _job_back()
    lat = lng = None
    try:
        lat, lng = float(request.form.get("lat", "")), float(request.form.get("lng", ""))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            lat = lng = None
    except ValueError:
        lat = lng = None
    x("UPDATE tickets SET arrived_at = NOW(), arrived_lat = %s, arrived_lng = %s, status = 'pending', "
      "assigned_to = COALESCE(assigned_to, %s), updated_at = NOW() WHERE id = %s", (lat, lng, g.me["id"], tid))
    where = ""
    if lat is not None and t.get("lat") is not None:
        import ticketlib
        where = " (%d m from the saved location)" % ticketlib.distance_m(lat, lng, t["lat"], t["lng"])
    elif lat is None:
        where = " (no GPS)"
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 1)",
           (tid, g.me["username"], "Arrived at the customer" + where + "."))
    audit("job_arrived", "#%d" % tid, where.strip(" ()"))
    mod_tickets.watch_alert(tid, "arrived", "Technician arrived", where.strip(" ()"))
    commit()
    flash("Arrival noted for job #%d." % tid, "success")
    return _job_back()


JOB_STATES = {"working": "Still working", "parts": "Waiting for parts", "customer": "Customer not home",
              "later": "Come back later"}


@route("/jobs/<int:tid>/update", module="tickets", methods=["POST"])
def job_update(tid):
    """A progress note on a started job (where it stands, a note, maybe a photo); the job stays in progress."""
    t = _job(tid)
    if t["status"] not in ("open", "pending"):
        flash("This job is already finished.", "warn")
        return _job_back()
    state = JOB_STATES.get(request.form.get("state"), JOB_STATES["working"])
    note = (request.form.get("note") or "").strip()[:5000]
    if len(note) < 3:
        flash("Write a short note about where the job stands.", "error")
        return _job_back()
    mats, err = _materials(t)
    if err:
        flash(err, "error")
        return _job_back("upd-%d" % tid)
    photo, err = _save_job_photo(tid)
    if err:
        flash(err, "error")
        return _job_back()
    _use_materials(t, mats)
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal, photo) VALUES (%s, %s, %s, 0, %s)",
           (tid, g.me["username"], "%s: %s" % (state, note), photo))
    x("UPDATE tickets SET status = 'pending', assigned_to = %s, updated_at = NOW() WHERE id = %s",
      (t["assigned_to"] or g.me["id"], tid))
    audit("job_updated", "#%d" % tid, state)
    # each progress note is news (no once-only key): count the replies so far to keep the keys apart
    mod_tickets.watch_alert(tid, "update%d" % q("SELECT COUNT(*) AS c FROM ticket_replies WHERE ticket_id = %s", (tid,), one=True)["c"],
                            state, note)
    commit()
    flash("Job #%d updated: %s." % (tid, state.lower()), "success")
    return _job_back()


# ===================================================================== technician home page
def tech_home(notices=None):
    """The technician's dashboard: today's jobs first, nothing about the ISP's numbers (no graphs, money or totals)."""
    import mod_tickets
    me = g.me["id"]
    mine = job_rows(["t.status IN ('open', 'pending')", "t.assigned_to = %s"], [me])
    done_today = job_rows(["t.status IN ('resolved', 'closed')", "t.assigned_to = %s", "t.closed_at >= CURDATE()"], [me],
                          order="t.closed_at DESC", limit=20)
    sql, fp = _free_sql()
    free = q("SELECT COUNT(*) AS c FROM tickets t WHERE t.status IN ('open', 'pending') AND " + sql, fp, one=True)["c"]
    week = q("SELECT COUNT(*) AS c FROM tickets WHERE assigned_to = %s AND status IN ('resolved', 'closed') "
             "AND closed_at >= CURDATE() - INTERVAL 6 DAY", (me,), one=True)["c"]
    today = date.today()
    fu = q("SELECT c.id, c.subscriber_id, c.note, c.outcome, c.follow_up, s.username, s.full_name, s.phone "
           "FROM sub_calls c JOIN subscribers s ON s.id = c.subscriber_id "
           "WHERE c.done = 0 AND c.follow_up IS NOT NULL AND c.follow_up <= %s AND c.created_by = %s "
           "ORDER BY c.follow_up LIMIT 8", (today, g.me["username"]))
    for r in fu:
        r["code"] = core.sub_code(r["subscriber_id"])
    counts = {"open": len(mine), "urgent": sum(1 for t in mine if t["priority"] in ("urgent", "high")),
              "working": sum(1 for t in mine if t["status"] == "pending"), "done": len(done_today),
              "week": int(week or 0), "free": int(free or 0), "fu": len(fu)}
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
    return render_template("tech_home.html", mine=mine, done_today=done_today, counts=counts, follow=fu,
                           outcomes=dict(OUTCOMES), greet=greet, today=today, notices=notices or [],
                           categories=mod_tickets.ALL_CATEGORIES, priorities=dict(mod_tickets.PRIORITIES), stock=stock_items(),
                           otp_on=core.cfg().get("ticket_otp_on") == "1")


@route("/tickets/photo/<name>", module="tickets")
def ticket_photo(name):
    if not re.fullmatch(r"t(\d+)-[0-9a-f]{16}\.(png|jpg)", name):
        abort(404)
    path = os.path.join(core.DATA_DIR, "ticket_photos", name)
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype="image/png" if name.endswith(".png") else "image/jpeg", max_age=86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ===================================================================== work & ratings (kept after a ticket is deleted)
WORK_PRESETS = ((29, "30 days"), (89, "90 days"), (364, "1 year"))


def work_techs():
    """Technicians whose work this user may see: a technician only themselves; admin / manager every technician;
    a reseller / sub-reseller / retailer the technicians added under their downline. None = no access."""
    role = g.me["role"]
    cols = "SELECT a.id, a.username, a.full_name, a.phone, a.is_active, p.username AS boss FROM panel_admins a " \
           "LEFT JOIN panel_admins p ON p.id = a.parent_id WHERE a.role = 'technician'"
    if role == "technician":
        return q(cols + " AND a.id = %s", (g.me["id"],))
    if role in ("admin", "manager"):
        return q(cols + " ORDER BY a.is_active DESC, a.username")
    if role in core.SELLER_ROLES:
        names = core.my_downline() or [g.me["username"]]
        return q(cols + " AND p.username IN (%s) ORDER BY a.is_active DESC, a.username" % ",".join(["%s"] * len(names)), list(names))
    return None


def work_rows(ids, since=None, until=None):
    """Every job of these technicians (finished, or given, from `since` to before `until`): live tickets plus the saved
    records of tickets that were deleted. Newest first."""
    if not ids:
        return []
    mark = ",".join(["%s"] * len(ids))
    live = q("SELECT t.id AS ticket_id, t.assigned_to AS admin_id, t.username, COALESCE(NULLIF(s.full_name, ''), t.username) AS customer, "
             "t.subject, t.category, t.status, t.created_at, t.closed_at, CASE WHEN t.to_office = 1 THEN t.due_at END AS due_at, "
             "t.arrived_at, t.rating, t.rating_note, 0 AS deleted FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id "
             "WHERE t.assigned_to IN (" + mark + ")", list(ids))
    try:
        gone = q("SELECT w.ticket_id, w.admin_id, w.username, w.customer, w.subject, w.category, w.status, w.created_at, w.closed_at, "
                 "w.due_at, w.arrived_at, w.rating, w.rating_note, 1 AS deleted FROM staff_work_log w "
                 "WHERE w.kind = 'job' AND w.admin_id IN (" + mark + ") AND NOT EXISTS (SELECT 1 FROM tickets t WHERE t.id = w.ticket_id)",
                 list(ids))
    except Exception:
        gone = []
    rows = [r for r in list(live) + list(gone)
            if (not since or (r["closed_at"] or r["created_at"]) >= since) and (not until or (r["closed_at"] or r["created_at"]) < until)]
    rows.sort(key=lambda r: r["closed_at"] or r["created_at"] or datetime.min, reverse=True)
    return rows


def work_sum(rows):
    done = [r for r in rows if r["status"] in ("resolved", "closed")]
    timed = [r for r in done if r["due_at"] and r["closed_at"]]
    rated = [r["rating"] for r in rows if r["rating"]]
    return {"jobs": len(rows), "done": len(done), "open": sum(1 for r in rows if r["status"] in ("open", "pending")),
            "in_time": (sum(1 for r in timed if r["closed_at"] <= r["due_at"]) * 100.0 / len(timed)) if timed else None,
            "rating": (sum(rated) * 1.0 / len(rated)) if rated else None, "rated": len(rated),
            "visits": sum(1 for r in rows if r["arrived_at"])}


@route("/tech-work", module="tickets")
def tech_work():
    """Work & ratings: each technician sees their own; admin all; a reseller the technicians they added."""
    techs = work_techs()
    if techs is None:
        abort(403)
    every = request.args.get("all") == "1"                 # all time
    start, end = core.date_range(89)
    since = None if every else datetime.combine(start, datetime.min.time())
    until = None if every else datetime.combine(end + timedelta(days=1), datetime.min.time())
    today = date.today()
    keep = {"all": "1"} if every else {"from": start.isoformat(), "to": end.isoformat()}
    presets = [(label, {"from": (today - timedelta(days=d)).isoformat(), "to": today.isoformat()}) for d, label in WORK_PRESETS]
    presets.append(("All time", {"all": "1"}))
    view = dict(start=start, end=end, every=every, keep=keep, presets=presets)
    by_id = {t["id"]: t for t in techs}
    pick = request.args.get("tech", type=int)
    if g.me["role"] == "technician":
        pick = g.me["id"]
    if pick is not None and pick not in by_id:
        abort(403)
    rows = work_rows([pick] if pick else list(by_id), since, until)
    if pick:
        return render_template("tech_work.html", tech=by_id[pick], rows=rows[:500], total=work_sum(rows), techs=techs,
                               categories=mod_tickets.ALL_CATEGORIES, solo=g.me["role"] == "technician", **view)
    per = {}
    for r in rows:
        per.setdefault(r["admin_id"], []).append(r)
    table = [dict(t, **work_sum(per.get(t["id"], []))) for t in techs]
    table.sort(key=lambda t: (-t["done"], -t["jobs"], t["username"]))
    return render_template("tech_work.html", tech=None, table=table, total=work_sum(rows), solo=False, **view)
