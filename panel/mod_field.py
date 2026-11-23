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
from datetime import date

from flask import abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import core
from core import audit, commit, insert, pager, q, route, x

OUTCOMES = [("called", "Talked"), ("no_answer", "No answer"), ("promised", "Promised to pay"), ("paid", "Paid"),
            ("complaint", "Complaint"), ("switched_off", "Phone off"), ("other", "Other")]
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
             "p.label AS plan FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
             "WHERE s.lat IS NOT NULL AND s.lng IS NOT NULL" + sc + " LIMIT 20000", sp)
    online = {r["username"] for r in q("SELECT DISTINCT username FROM radacct WHERE acctstoptime IS NULL")}
    today = date.today()
    out = []
    for r in rows:
        if r["status"] == "suspended":
            state = "suspended"
        elif r["expiry_date"] and r["expiry_date"] < today:
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
    if names is not None and t["sid"] and t["owner"] not in names:
        abort(403)
    return t


@route("/jobs", module="tickets")
def jobs():
    show = request.args.get("show", "mine")
    where = ["t.status IN ('open', 'pending')"]
    params = []
    if show == "free":
        where.append("t.assigned_to IS NULL")
    elif show == "done":
        where = ["t.status IN ('resolved', 'closed')", "t.assigned_to = %s", "t.closed_at >= NOW() - INTERVAL 7 DAY"]
        params.append(g.me["id"])
    else:
        show = "mine"
        where.append("t.assigned_to = %s")
        params.append(g.me["id"])
    names = core.my_downline()
    if names is not None:
        where.append("(t.subscriber_id IS NULL OR s.created_by IN (%s))" % ",".join(["%s"] * len(names)))
        params += list(names)
    rows = q("SELECT t.*, s.id AS sid, s.full_name AS sub_name, s.phone AS sub_phone, s.address AS sub_address, "
             "s.area AS sub_area, s.lat, s.lng, "
             "(SELECT r.body FROM ticket_replies r WHERE r.ticket_id = t.id AND r.internal = 0 ORDER BY r.id DESC LIMIT 1) AS last_note "
             "FROM tickets t LEFT JOIN subscribers s ON s.id = t.subscriber_id WHERE " + " AND ".join(where) +
             " ORDER BY FIELD(t.priority, 'urgent', 'high', 'normal', 'low'), t.created_at LIMIT 200", params)
    import mod_tickets
    counts = q("SELECT SUM(status IN ('open', 'pending') AND assigned_to = %s) AS mine, "
               "SUM(status IN ('open', 'pending') AND assigned_to IS NULL) AS free FROM tickets", (g.me["id"],), one=True)
    return render_template("jobs.html", rows=rows, show=show, categories=dict(mod_tickets.CATEGORIES),
                           priorities=dict(mod_tickets.PRIORITIES),
                           counts={"mine": int(counts["mine"] or 0), "free": int(counts["free"] or 0)})


@route("/jobs/<int:tid>/start", module="tickets", methods=["POST"])
def job_start(tid):
    t = _job(tid)
    if t["status"] not in ("open", "pending"):
        flash("This job is already finished.", "warn")
        return redirect(url_for("jobs"))
    who = t["assigned_to"] or g.me["id"]          # taking a free job makes it yours
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal) VALUES (%s, %s, %s, 0)",
           (tid, g.me["username"], "Work started."))
    x("UPDATE tickets SET status = 'pending', assigned_to = %s, updated_at = NOW() WHERE id = %s", (who, tid))
    audit("job_started", "#%d" % tid)
    commit()
    flash("Job #%d started." % tid, "success")
    return redirect(url_for("jobs"))


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
        return redirect(url_for("jobs"))
    note = (request.form.get("note") or "").strip()[:5000]
    if len(note) < 3:
        flash("Write what was done (a few words) before marking the job done.", "error")
        return redirect(url_for("jobs"))
    photo, err = _save_job_photo(tid)
    if err:
        flash(err, "error")
        return redirect(url_for("jobs"))
    insert("INSERT INTO ticket_replies (ticket_id, author, body, internal, photo) VALUES (%s, %s, %s, 0, %s)",
           (tid, g.me["username"], "Done: " + note, photo))
    x("UPDATE tickets SET status = 'resolved', closed_at = NOW(), assigned_to = %s, updated_at = NOW() WHERE id = %s",
      (t["assigned_to"] or g.me["id"], tid))
    audit("job_done", "#%d" % tid, note[:200])
    commit()
    flash("Job #%d marked done." % tid, "success")
    return redirect(url_for("jobs"))


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
