"""Subscriber profile page: extra data for the tabs, balance, migration, service switches, photo, password."""
import os
import re
from datetime import date, timedelta

from flask import abort, flash, g, jsonify, redirect, request, send_file, url_for

import core
import radius
from core import audit, commit, get_db, insert, q, route, x

PHOTO_TYPES = {b"\x89PNG\r\n\x1a\n": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg")}
GB = 1024 ** 3


def _photo_path(sid):
    d = os.path.join(core.DATA_DIR, "photos")
    if os.path.isdir(d):
        for n in os.listdir(d):
            if n.startswith("%d." % sid):
                return os.path.join(d, n)
    return None


def _sub(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and sub["created_by"] not in names:
        abort(403)
    return sub


def _plan(plan_id):
    return q("SELECT * FROM plans WHERE id = %s", (plan_id,), one=True) if plan_id else None


def _resync(sub, **over):
    """Rewrite the RADIUS rows from the current subscriber row (after changing a flag)."""
    row = q("SELECT * FROM subscribers WHERE id = %s", (sub["id"],), one=True)
    row.update(over)
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, row["username"], _plan(row["plan_id"]), row["status"], row["expiry_date"],
                               row["static_ip"])


def _back(sid, tab=""):
    return redirect(url_for("subscriber_view", sid=sid) + (("#" + tab) if tab else ""))


# ===================================================================== page data
def context(sub, sessions):
    sid, user = sub["id"], sub["username"]
    out = {}
    out["photo"] = url_for("subscriber_photo", sid=sid, v=int(os.path.getmtime(_photo_path(sid)))) if _photo_path(sid) else ""
    out["due_charges"] = float(q("SELECT COALESCE(SUM(amount), 0) AS t FROM sub_charges WHERE subscriber_id = %s AND paid = 0",
                                 (sid,), one=True)["t"] or 0)
    # an expired package is owed too: its price less what is already in the balance (as Accounting > Dues counts it)
    out["due_package"] = 0.0
    if sub.get("status") != "suspended" and sub.get("expiry_date") and sub["expiry_date"] < date.today() and sub.get("plan_id"):
        out["due_package"] = round(max(float(sub.get("price") or 0) - float(sub.get("balance") or 0), 0), 2)
    out["due"] = round(out["due_charges"] + out["due_package"], 2)
    out["open_tickets"] = q("SELECT COUNT(*) AS c FROM tickets WHERE subscriber_id = %s AND status IN ('open', 'pending')",
                            (sid,), one=True)["c"]
    out["quota_gb"] = sub.get("quota_total_gb")
    last = q("SELECT * FROM radacct WHERE username = %s ORDER BY radacctid DESC LIMIT 1", (user,), one=True)
    out["last"] = last
    nas_ip = (sessions[0]["nasipaddress"] if sessions else (last or {}).get("nasipaddress"))
    nas = q("SELECT shortname, nasname FROM nas WHERE nasname = %s LIMIT 1", (nas_ip,), one=True) if nas_ip else None
    out["nas_name"] = (nas["shortname"] if nas and nas.get("shortname") else nas_ip) or ""
    mac = (sessions[0]["callingstationid"] if sessions else (last or {}).get("callingstationid")) or ""
    out["mac_now"] = mac
    prefix = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()[:6]
    v = q("SELECT vendor FROM mac_vendors WHERE prefix = %s", (prefix,), one=True) if len(prefix) == 6 else None
    out["mac_vendor"] = v["vendor"] if v else ""
    out["groups"] = [r["groupname"] for r in q("SELECT groupname FROM radusergroup WHERE username = %s ORDER BY priority",
                                                (user,))]
    out["last_activation"] = q("SELECT paid_at FROM payments WHERE subscriber_id = %s AND days_added > 0 "
                               "ORDER BY id DESC LIMIT 1", (sid,), one=True)
    import mod_extras
    out["revert"] = mod_extras.revertable(sid) if core.can_fn("sub.revert") else None
    out["isp_name"] = core.cfg()["isp_name"]
    out["interim_min"] = round(int(core.cfg()["interim_interval"] or 0) / 60, 2)
    return out


def tabs(sub, sessions):
    sid, user = sub["id"], sub["username"]
    t = {}
    t["payments"] = q("SELECT * FROM payments WHERE subscriber_id = %s ORDER BY id DESC LIMIT 300", (sid,))
    t["charges"] = q("SELECT * FROM sub_charges WHERE subscriber_id = %s ORDER BY id DESC LIMIT 300", (sid,))
    try:
        t["notifs"] = q("SELECT kind, title, body, created_by, created_at, read_at FROM sub_notifications "
                        "WHERE subscriber_id = %s ORDER BY id DESC LIMIT 50", (sid,))
    except Exception:
        t["notifs"] = []
    t["messages"] = q("SELECT channel, to_addr, subject, body, status, error, created_at, sent_at FROM outbox "
                      "WHERE subscriber_id = %s ORDER BY id DESC LIMIT 50", (sid,))
    attrs = []
    for table in ("radcheck", "radreply"):
        for r in q("SELECT attribute, op, value FROM %s WHERE username = %%s ORDER BY id" % table, (user,)):
            val = "••••••••" if r["attribute"].lower().endswith("password") else r["value"]
            attrs.append({"table": "Check" if table == "radcheck" else "Reply", "attribute": r["attribute"],
                          "op": r["op"], "value": val})
    t["attrs"] = attrs
    t["macs"] = q("SELECT callingstationid AS mac, COUNT(*) AS n, MAX(acctstarttime) AS last FROM radacct "
                  "WHERE username = %s AND callingstationid <> '' GROUP BY callingstationid ORDER BY last DESC LIMIT 20",
                  (user,))
    t["logins"] = q("SELECT reply, authdate FROM radpostauth WHERE username = %s ORDER BY id DESC LIMIT 50", (user,))
    t["sessions"] = q("SELECT acctstarttime, acctstoptime, acctsessiontime, acctinputoctets, acctoutputoctets, "
                      "framedipaddress, callingstationid, acctterminatecause FROM radacct WHERE username = %s "
                      "ORDER BY radacctid DESC LIMIT 50", (user,))
    t["coa"] = q("SELECT nas_ip, ok, message, created_by, created_at FROM coa_log WHERE username = %s ORDER BY id DESC LIMIT 50",
                 (user,))
    t["activity"] = q("SELECT admin, action, detail, created_at FROM audit_log WHERE target = %s ORDER BY id DESC LIMIT 50",
                      (user,))
    t["tickets"] = q("SELECT id, subject, status, priority, updated_at FROM tickets WHERE subscriber_id = %s "
                     "ORDER BY id DESC LIMIT 30", (sid,))
    t["months"] = q("SELECT DATE_FORMAT(acctstarttime, '%%Y-%%m') AS m, COALESCE(SUM(acctoutputoctets), 0) AS down, "
                    "COALESCE(SUM(acctinputoctets), 0) AS up, COUNT(*) AS sessions, COALESCE(SUM(acctsessiontime), 0) AS secs "
                    "FROM radacct WHERE username = %s AND acctstarttime >= %s GROUP BY m ORDER BY m DESC",
                    (user, (date.today().replace(day=1) - timedelta(days=150)).replace(day=1)))
    t["fees"] = q("SELECT f.name, f.kind, f.mode, f.value FROM fees f JOIN plan_fees pf ON pf.fee_id = f.id "
                  "WHERE f.active = 1 AND pf.plan_id = %s ORDER BY f.name", (sub["plan_id"],)) if sub["plan_id"] else []
    balance_rows = q("SELECT id, amount, balance_after, note, created_by, created_at FROM sub_ledger WHERE subscriber_id = %s "
                     "ORDER BY id DESC LIMIT 300", (sid,))
    t["invoices"] = invoices(t["payments"], t["charges"])
    t["ledger"] = ledger(sub, t["payments"], t["charges"], balance_rows)
    return t


# ===================================================================== invoices and the customer's ledger
# balance lines that are only the other half of a package payment (already shown as that invoice's payment)
_PACKAGE_BALANCE = ("Activation +", "Mass activation +", "Auto renew +", "Renewal +",
                    "Extra from activation payment", "Rest of activation from balance")
_RECEIVED = re.compile(r"Received ([0-9]+(?:\.[0-9]+)?)")


def _f(v):
    return round(float(v or 0), 2)


def invoices(payments, charges):
    """Every bill of the customer, newest first: each package activation / renewal (a payment row with days) and each
    extra service charge."""
    out = []
    for p in payments:
        if not p["days_added"]:
            continue
        paid_by = "customer balance" if p["method"] == "balance" else p["method"]
        out.append({"no": "INV-%d" % p["id"], "at": p["paid_at"], "kind": "package",
                    "text": "Package +%d day%s" % (p["days_added"], "" if p["days_added"] == 1 else "s")
                            + (" (until %s)" % p["period_to"].strftime("%d %b %Y") if p["period_to"] else ""),
                    "amount": _f(p["amount"]), "paid": True, "via": paid_by, "note": p["note"] or "",
                    "receipt": p["id"], "charge": None, "by": p["received_by"]})
    for c in charges:
        out.append({"no": "CHG-%d" % c["id"], "at": c["created_at"], "kind": "charge", "text": c["name"],
                    "amount": _f(c["amount"]), "paid": bool(c["paid"]), "via": "", "note": c["note"] or "",
                    "receipt": None, "charge": c["id"], "by": c["created_by"]})
    return sorted(out, key=lambda e: e["at"], reverse=True)


def ledger(sub, payments, charges, balance_rows):
    """The customer's account, newest first, with Debit (bills, money taken off) and Credit (money in) and the
    running balance. The balance is what the customer has in hand: their stored balance minus unpaid extra charges
    (negative = they owe). It is worked out backwards from today's figure, so the newest line always matches it.

      package invoice  debit  = the price;  its payment credit = the money received (none when paid from the balance)
      extra charge     debit  = the charge; paying it later is a credit (a payment without days)
      balance top-up / deduct (Add Balance, Mass Payment, ...)  credit / debit
    Balance lines written as the other half of a package payment are not repeated."""
    rows = []
    for p in payments:
        amt = _f(p["amount"])
        if p["days_added"]:
            rows.append({"at": p["paid_at"], "seq": 1, "kind": "Invoice", "no": "INV-%d" % p["id"],
                         "text": "Package +%d days%s%s" % (p["days_added"],
                                                          " until %s" % p["period_to"].strftime("%d %b %Y") if p["period_to"] else "",
                                                          ", paid from the balance" if p["method"] == "balance" else ""),
                         "debit": amt, "credit": 0.0, "by": p["received_by"]})
            if p["method"] != "balance":
                m = _RECEIVED.search(p["note"] or "")
                got = _f(m.group(1)) if m else amt
                rows.append({"at": p["paid_at"], "seq": 2, "kind": "Payment", "no": "INV-%d" % p["id"],
                             "text": "Received by %s%s" % (p["method"], (" · " + p["note"]) if p["note"] else ""),
                             "debit": 0.0, "credit": got, "by": p["received_by"]})
        else:
            rows.append({"at": p["paid_at"], "seq": 2, "kind": "Payment", "no": "PAY-%d" % p["id"],
                         "text": "Received by %s%s" % (p["method"], (" · " + p["note"]) if p["note"] else ""),
                         "debit": 0.0, "credit": amt, "by": p["received_by"]})
    for c in charges:
        rows.append({"at": c["created_at"], "seq": 0, "kind": "Invoice", "no": "CHG-%d" % c["id"],
                     "text": c["name"] + ("" if c["paid"] else " (unpaid)"), "debit": _f(c["amount"]), "credit": 0.0,
                     "by": c["created_by"]})
    for b in balance_rows:
        note = b["note"] or ""
        if note.startswith(_PACKAGE_BALANCE):
            continue
        amt = _f(b["amount"])
        rows.append({"at": b["created_at"], "seq": 3, "kind": "Balance top-up" if amt >= 0 else "Balance deduct",
                     "no": "", "text": note or ("Balance added" if amt >= 0 else "Balance taken off"),
                     "debit": -amt if amt < 0 else 0.0, "credit": amt if amt > 0 else 0.0, "by": b["created_by"]})
    rows.sort(key=lambda e: (e["at"], e["seq"]), reverse=True)
    due = sum(_f(c["amount"]) for c in charges if not c["paid"])
    running = round(_f(sub["balance"]) - due, 2)
    for r in rows:
        r["balance"] = running
        running = round(running - r["credit"] + r["debit"], 2)
    return rows[:200]


# ===================================================================== actions
@route("/subscribers/<int:sid>/migrate", module="subscribers", methods=["POST"])
def subscriber_migrate(sid):
    sub = _sub(sid)
    if not core.can_fn("svc.packages"):
        flash("You are not allowed to change packages (Service Settings > Packages).", "error")
        return _back(sid)
    pid = request.form.get("plan_id", "")
    plan = _plan(int(pid)) if pid.isdigit() else None
    if not plan:
        flash("Choose a package.", "error")
        return _back(sid)
    allowed = core.allowed_plan_ids()
    if allowed is not None and plan["id"] not in allowed:
        abort(403)
    x("UPDATE subscribers SET plan_id = %s, quota_hit = '' WHERE id = %s", (plan["id"], sid))
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], plan, sub["status"], sub["expiry_date"], sub["static_ip"])
    audit("migrated", sub["username"], "to %s" % plan["name"])
    commit()
    flash("%s moved to %s. The new speed applies at the next login." % (sub["username"], plan["label"] or plan["name"]), "success")
    return _back(sid)


@route("/subscribers/<int:sid>/balance", module="subscribers", methods=["POST"])
def subscriber_balance(sid):
    sub = _sub(sid)
    try:
        amount = round(float(request.form.get("amount", "0")), 2)
        assert 0 < abs(amount) < 10 ** 7
    except (ValueError, AssertionError):
        flash("Enter an amount (use a minus sign to take money off the balance).", "error")
        return _back(sid)
    new = round(float(sub["balance"] or 0) + amount, 2)
    if new < 0:                       # nobody takes more than the customer has (office staff included)
        flash("The customer's balance cannot go below zero (it is %.2f)." % float(sub["balance"] or 0), "error")
        return _back(sid)
    if g.me["role"] in core.SELLER_ROLES:
        # a seller's money comes out of their own balance (taking it back returns it to them)
        ok, have = core.seller_pays(amount, "Balance %s %s" % ("to" if amount > 0 else "back from", sub["username"]))
        if not ok:
            flash("Your balance (%.2f) is lower than %.2f. Add balance to yourself first. Nothing was changed." % (have, amount), "error")
            return _back(sid)
    x("UPDATE subscribers SET balance = %s WHERE id = %s", (new, sid))
    insert("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
           (sid, amount, new, request.form.get("note", "").strip()[:255], g.me["username"]))
    audit("balance_changed", sub["username"], "%+.2f -> %.2f" % (amount, new))
    if amount > 0:
        import appnotify
        with get_db().cursor() as cur:
            appnotify.balance(cur, sid, amount, new, core.cfg().get("currency"), "%s:%s" % (sid, q(
                "SELECT MAX(id) AS m FROM sub_ledger WHERE subscriber_id = %s", (sid,), one=True)["m"]), g.me["username"])
    commit()
    flash("Balance is now %.2f." % new, "success")
    return _back(sid, "ledgers")


@route("/subscribers/<int:sid>/settings", module="subscribers", methods=["POST"])
def subscriber_settings(sid):
    sub = _sub(sid)
    keys = {"sms_on": "svc.sms", "email_on": "svc.email", "auto_mac": "svc.maclock", "auto_renew": "svc.skip_renew"}
    # a switch the person may not change keeps its saved value
    flags = {k: (1 if request.form.get(k) else 0) if core.can_fn(keys[k]) else (1 if sub[k] else 0) for k in keys}
    x("UPDATE subscribers SET sms_on = %s, email_on = %s, auto_mac = %s, auto_renew = %s WHERE id = %s",
      (flags["sms_on"], flags["email_on"], flags["auto_mac"], flags["auto_renew"], sid))
    audit("service_settings", sub["username"], ", ".join("%s=%d" % kv for kv in flags.items()))
    commit()
    flash("Service settings saved.", "success")
    return _back(sid)


@route("/subscribers/<int:sid>/net", module="subscribers", methods=["POST"])
def subscriber_net(sid):
    sub = _sub(sid)
    off = 0 if sub["net_disabled"] else 1
    if not core.can_fn("sub.net_off" if off else "sub.net_on"):    # each direction has its own tick
        abort(403)
    x("UPDATE subscribers SET net_disabled = %s WHERE id = %s", (off, sid))
    _resync(sub, net_disabled=off)
    audit("net_disabled" if off else "net_enabled", sub["username"])
    commit()
    if off:
        import app as appmod
        appmod.kick_user(sub["username"])
    flash("Internet %s for %s." % ("disabled" if off else "enabled", sub["username"]), "success")
    return _back(sid)


@route("/subscribers/<int:sid>/sync", module="subscribers", methods=["POST"])
def subscriber_sync(sid):
    sub = _sub(sid)
    _resync(sub)
    audit("synced", sub["username"])
    commit()
    flash("RADIUS rows rewritten for %s." % sub["username"], "success")
    return _back(sid)


@route("/subscribers/<int:sid>/reset-mac", module="subscribers", methods=["POST"])
def subscriber_reset_mac(sid):
    sub = _sub(sid)
    x("UPDATE subscribers SET mac_bind = '' WHERE id = %s", (sid,))
    _resync(sub, mac_bind="")
    audit("mac_unbound", sub["username"])
    commit()
    flash("MAC binding cleared. The next device that connects is accepted%s." %
          (" and locked automatically" if sub["auto_mac"] else ""), "success")
    return _back(sid, "mac")


@route("/subscribers/<int:sid>/password", module="subscribers", methods=["POST"])
def subscriber_password(sid):
    sub = _sub(sid)
    pw = request.form.get("password", "")
    if not core.PASSWORD_RE.match(pw):
        flash("The password must be 4 to 64 characters with no spaces.", "error")
        return _back(sid)
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], _plan(sub["plan_id"]), sub["status"], sub["expiry_date"],
                               sub["static_ip"], password=pw)
    audit("password_changed", sub["username"])
    commit()
    flash("Password changed. The customer's router must use the new one at the next login.", "success")
    return _back(sid)


@route("/subscribers/<int:sid>/invoice", module="subscribers", methods=["POST"])
def subscriber_invoice(sid):
    sub = _sub(sid)
    try:
        name = request.form.get("name", "").strip()
        amount = round(float(request.form.get("amount", "0")), 2)
        assert 1 <= len(name) <= 64 and 0 < amount < 10 ** 7
    except (ValueError, AssertionError):
        flash("Enter an invoice name and an amount above zero.", "error")
        return _back(sid)
    insert("INSERT INTO sub_charges (subscriber_id, name, amount, note, created_by) VALUES (%s, %s, %s, %s, %s)",
           (sid, name, amount, request.form.get("note", "").strip()[:255], g.me["username"]))
    audit("charge_added", sub["username"], "%s %s" % (name, amount))
    commit()
    flash("Invoice added.", "success")
    return _back(sid, "invoices")


@route("/subscribers/<int:sid>/photo", module="subscribers")
def subscriber_photo(sid):
    _sub(sid)
    f = _photo_path(sid)
    if not f:
        abort(404)
    resp = send_file(f, mimetype="image/png" if f.endswith(".png") else "image/jpeg", max_age=3600)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def save_photo(sid, data):
    """Store a PNG / JPG profile photo for the customer. Returns an error sentence, or None when saved."""
    kind = next((v for k, v in PHOTO_TYPES.items() if data.startswith(k)), None)
    if not data or len(data) > 2 * 1024 * 1024 or not kind:
        return "Choose a PNG or JPG photo up to 2 MB."
    d = os.path.join(core.DATA_DIR, "photos")
    os.makedirs(d, exist_ok=True)
    old = _photo_path(sid)
    if old:
        os.remove(old)
    with open(os.path.join(d, "%d.%s" % (sid, kind[0])), "wb") as fh:
        fh.write(data)
    x("UPDATE subscribers SET photo = %s WHERE id = %s", (kind[0], sid))
    return None


@route("/subscribers/<int:sid>/photo/save", module="subscribers", methods=["POST"])
def subscriber_photo_save(sid):
    sub = _sub(sid)
    old = _photo_path(sid)
    if request.form.get("remove"):
        if old:
            os.remove(old)
        x("UPDATE subscribers SET photo = '' WHERE id = %s", (sid,))
        commit()
        flash("Photo removed.", "success")
        return _back(sid)
    up = request.files.get("file")
    why = save_photo(sid, up.read(2 * 1024 * 1024 + 1) if up else b"")
    if why:
        flash(why, "error")
        return _back(sid)
    audit("photo_saved", sub["username"])
    commit()
    flash("Photo saved.", "success")
    return _back(sid)


@route("/subscribers/<int:sid>/rate", module="subscribers")
def subscriber_rate(sid):
    """Current download / upload speed of one customer (JSON, polled by the live chart on their page).

    The speed is worked out by the 5-minute traffic job from the router's byte counters, and routers send new counters
    only at each interim update (5 minutes on MikroTik by default), so the value changes in steps, not every second."""
    sub = q("SELECT username FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    return rate_response(sub["username"])


def rate_response(username):
    """The live-speed JSON for one username (admin page and customer portal)."""
    sub = {"username": username}
    sessions = q("SELECT radacctid, COALESCE(acctupdatetime, acctstarttime) AS upd FROM radacct "
                 "WHERE username = %s AND acctstoptime IS NULL", (sub["username"],))
    down = up = 0
    known = False
    if sessions:
        try:
            ids = [r["radacctid"] for r in sessions]
            for t in q("SELECT down_bps, up_bps FROM traffic_last WHERE radacctid IN (%s)" % ",".join(["%s"] * len(ids)), ids):
                down += int(t["down_bps"] or 0)
                up += int(t["up_bps"] or 0)
                known = True
        except Exception:       # traffic_last appears after install.sh / update.sh has run
            known = False
    newest = max((r["upd"] for r in sessions if r["upd"]), default=None)
    resp = jsonify(online=bool(sessions), known=known, down=down, up=up,
                   updated=newest.strftime("%H:%M") if newest else "")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@route("/subscribers/<int:sid>/notify", module="subscribers", methods=["POST"])
def subscriber_notify(sid):
    """Send this customer an in-app notification (their portal / app shows it with the unread mark)."""
    sub = _sub(sid)
    title = request.form.get("title", "").strip()[:120]
    body = request.form.get("body", "").strip()[:600]
    if not title or not body:
        flash("Write a title and a message.", "error")
        return _back(sid, "notifications")
    import appnotify
    with get_db().cursor() as cur:
        appnotify.add(cur, sid, "custom", title, body, by=g.me["username"])
    audit("app_notification_sent", sub["username"], title)
    commit()
    flash("Notification sent to %s. It shows in their app%s." % (
        sub["username"], "" if core.cfg().get("portal_on") == "1" else " once the customer portal is switched on"), "success")
    return _back(sid, "notifications")
