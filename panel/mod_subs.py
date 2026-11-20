"""Subscriber tools: renew helper, activation by username, grace period, MAC binding, extra service charges."""
import re
from datetime import date, datetime, timedelta

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

import core
import radius
from core import audit, commit, get_db, insert, pager, q, route, x

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:\-.]?[0-9A-Fa-f]{2}){5}$")


def norm_mac(text):
    text = (text or "").strip()
    if not text:
        return ""
    if not MAC_RE.match(text):
        raise ValueError("A MAC address looks like AA:BB:CC:DD:EE:FF.")
    h = re.sub(r"[^0-9A-Fa-f]", "", text).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def _plan(plan_id):
    return q("SELECT * FROM plans WHERE id = %s", (plan_id,), one=True) if plan_id else None


def fixed_expiry(new_expiry, base, day):
    """Snap to the chosen day of the month (clamped to the month's length); never earlier than the start."""
    import calendar
    last = calendar.monthrange(new_expiry.year, new_expiry.month)[1]
    snapped = new_expiry.replace(day=min(int(day), last))
    if snapped <= base:
        y, m = (snapped.year + (snapped.month == 12), snapped.month % 12 + 1)
        snapped = date(y, m, min(int(day), calendar.monthrange(y, m)[1]))
    return snapped


def add_months(base, months):
    """Calendar months: 31 Jan + 1 month = 28/29 Feb, 15 Mar + 1 month = 15 Apr (day is clamped to the month's length)."""
    import calendar
    total = base.year * 12 + base.month - 1 + months
    y, m = divmod(total, 12)
    return date(y, m + 1, min(base.day, calendar.monthrange(y, m + 1)[1]))


def plan_expiry(plan, base, days):
    """New expiry for a renewal. When the days are the package's own length, months and years are real calendar
    months and years (not 30 or 365 days); a custom number of days is added as plain days."""
    if plan and plan.get("duration_type") in ("month", "year") and plan.get("duration") \
            and days == int(plan.get("validity_days") or 0):
        n = int(plan["duration"]) * (12 if plan["duration_type"] == "year" else 1)
        return add_months(base, n)
    return base + timedelta(days=days)


_KEEP = object()
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clock(hhmm):
    """'15:30' -> '3:30 PM' (how times are shown in the panel)."""
    h, m = (int(v) for v in hhmm.split(":")[:2])
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def end_moment(sub, plan):
    """When the account ends now: the expiry date at the subscriber's time, else the package's, else 23:59:59."""
    if not sub.get("expiry_date"):
        return None
    at = sub.get("expiry_time") or (plan or {}).get("fixed_expire_time") or "23:59"
    h, m = (int(v) for v in at.split(":")[:2])
    return datetime.combine(sub["expiry_date"], datetime.min.time()).replace(hour=h, minute=m, second=59 if at == "23:59" else 0)


def do_renew(sub, days, amount, method="cash", note="", notify=True, exact=False, expiry_time=_KEEP):
    """Add days (from today, or from the current expiry if it is later), activate and record the payment.
    exact=True uses a date the person typed as it is (no whole-month or fixed-day rounding).
    expiry_time: "HH:MM" (or None for the package's time / end of day) to store with the new expiry; left out, the
    subscriber's stored time is kept."""
    base = max(sub["expiry_date"] or date.today(), date.today())
    plan = _plan(sub["plan_id"])
    new_expiry = plan_expiry(plan, base, days)
    if plan and plan.get("fixed_expire_day"):
        new_expiry = fixed_expiry(new_expiry, base, plan["fixed_expire_day"])
    if exact:
        new_expiry = base + timedelta(days=days)
    x("UPDATE subscribers SET expiry_date = %s, status = 'active' WHERE id = %s", (new_expiry, sub["id"]))
    if expiry_time is not _KEEP:
        x("UPDATE subscribers SET expiry_time = %s WHERE id = %s", (expiry_time, sub["id"]))
    # what a reseller / sub-reseller / retailer paid for this package (set by charge_me just before), kept for the profit report
    charged = g.pop("charged", None) or (None, None)
    pay_id = insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, period_to, "
                    "received_by, payer_id, cost) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (sub["id"], sub["username"], amount, (new_expiry - base).days, method[:32], note[:255], new_expiry,
                     g.me["username"], charged[0], charged[1]))
    g.last_payment = pay_id
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], plan, "active", new_expiry, sub["static_ip"])
        # renewed after the package ran out (or while suspended): the live session may still sit in the expired /
        # disabled policy, so ask the NOC service to disconnect it; the router reconnects with the full package
        was_limited = sub.get("status") == "suspended" or (sub.get("expiry_date") and sub["expiry_date"] < date.today())
        if was_limited:
            try:
                cur.execute("INSERT INTO coa_queue (username, reason, created_by) VALUES (%s, 'renewed', %s)",
                            (sub["username"], g.me["username"][:64]))
            except Exception:      # table missing until install.sh is re-run; the renewal itself must still go through
                pass
    if notify:
        core.notify("payment", sub["id"], {"amount": core.fmt_money(amount)}, key=pay_id)
    return new_expiry


def _price_of(cur, user_id, plan_id):
    """What this person pays for the package (Users > Assign Packages), or None when no price is set."""
    cur.execute("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = %s", (user_id, plan_id))
    r = cur.fetchone()
    try:
        return round(float(r["price"]), 2) if r and r["price"] is not None else None
    except (TypeError, ValueError):
        return None


def charge_seller(cur, seller_id, plan_id, label, by):
    """Take the seller's package price from their balance for one activation or renewal and write it in their ledger.
    Returns None when done (or when no price is set for them), or a short reason when their balance is too low."""
    cost = _price_of(cur, seller_id, plan_id) if plan_id else None
    if not cost:
        return None
    cur.execute("UPDATE panel_admins SET balance = balance - %s WHERE id = %s AND balance >= %s", (cost, seller_id, cost))
    if cur.rowcount != 1:
        cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (seller_id,))
        r = cur.fetchone()
        return "balance (%.2f) is lower than the package cost (%.2f)" % (float(r["balance"] or 0) if r else 0.0, cost)
    cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (seller_id,))
    after = cur.fetchone()
    cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                "VALUES (%s, %s, %s, 'activation', %s, %s)",
                (seller_id, -cost, list(after.values())[0] if after else 0, label[:255], by))
    return None


def payer_for(sub):
    """Who pays the package price when this customer is activated: the reseller / sub-reseller / retailer doing it, or,
    when admin or staff do it, the seller who owns the customer. None when nobody is charged."""
    if g.me["role"] in core.SELLER_ROLES:
        return {"id": g.me["id"], "username": g.me["username"], "mine": True}
    owner = q("SELECT id, username, role FROM panel_admins WHERE username = %s", (sub.get("created_by"),), one=True)
    if owner and owner["role"] in core.SELLER_ROLES:
        return {"id": owner["id"], "username": owner["username"], "mine": False}
    return None


def charge_me(sub, label):
    """Take the package price from whoever pays for this activation (see payer_for). Returns None when done or when
    nobody is charged, or a sentence saying whose balance is too low."""
    g.pop("charged", None)
    payer = payer_for(sub)
    if not payer:
        return None
    if not payer["mine"]:
        label = "%s (by %s)" % (label, g.me["username"])
    with get_db().cursor() as cur:
        why = charge_seller(cur, payer["id"], sub["plan_id"], label, g.me["username"])
    if why:
        return ("your " if payer["mine"] else "%s's " % payer["username"]) + why
    with get_db().cursor() as cur:
        cost = _price_of(cur, payer["id"], sub["plan_id"]) if sub.get("plan_id") else None
    if cost:
        g.charged = (payer["id"], cost)
    return None


def charge_info(sub):
    """What the activation window shows: {'cost', 'who', 'mine', 'balance', 'low'} or None."""
    payer = payer_for(sub)
    if not payer or not sub.get("plan_id"):
        return None
    with get_db().cursor() as cur:
        cost = _price_of(cur, payer["id"], sub["plan_id"])
        cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (payer["id"],))
        r = cur.fetchone()
    if not cost:
        return None
    bal = float(r["balance"] or 0) if r else 0.0
    return {"cost": cost, "who": payer["username"], "mine": payer["mine"], "balance": bal, "low": bal < cost}


def _own(sub):
    """Sellers may only touch their own downline's customers."""
    names = core.my_downline()
    if names is not None and sub["created_by"] not in names:
        abort(403)


# ===================================================================== activation window
def _state(sub):
    if sub["status"] == "suspended":
        return "suspended"
    if sub["expiry_date"] and sub["expiry_date"] < date.today():
        return "expired"
    return "active"


def _visible_subs(where, args):
    """Subscribers the signed-in person may activate (a seller only sees their own downline's customers)."""
    names = core.my_downline()
    if names is not None:
        where += " AND created_by IN (" + ",".join(["%s"] * len(names)) + ")"
        args = list(args) + list(names)
    return q("SELECT id, username, full_name, phone, status, plan_id, expiry_date, expiry_time, created_by, balance FROM subscribers WHERE " + where +
             " ORDER BY username LIMIT 15", args)


def _sub_json(r):
    return {"id": r["id"], "username": r["username"], "name": r["full_name"] or "", "phone": r.get("phone") or "", "state": _state(r),
            "plan_id": r["plan_id"],
            "expiry": (r["expiry_date"].strftime("%d %b %Y") + (" " + _clock(r["expiry_time"]) if r.get("expiry_time") else ""))
            if r["expiry_date"] else "",
            "expiry_iso": r["expiry_date"].isoformat() if r["expiry_date"] else ""}


@route("/subscribers/activation", module="subscribers", need="edit")
def subscriber_activation():
    plans = core.filter_plans(q("SELECT * FROM plans ORDER BY download_mbps, label"))
    pre = None
    if request.args.get("username"):
        rows = _visible_subs("username = %s", (request.args["username"].strip(),))
        pre = _sub_json(rows[0]) if rows else None
    return render_template("subscriber_activation.html", plans=plans, pre=pre, today=date.today(),
                           back=core.safe_next(request.args.get("next")))


@route("/subscribers/lookup", module="subscribers", need="edit")
def subscriber_lookup():
    """Typing in the Subscriber box: matching customers, plus what the seller pays when a package is chosen."""
    term = request.args.get("q", "").strip()[:64]
    if len(term) < 2:
        return jsonify({"items": []})
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = _visible_subs("(username LIKE %s OR full_name LIKE %s OR phone LIKE %s)", (like, like, like))
    return jsonify({"items": [_sub_json(r) for r in rows]})


PAY_METHODS = ("cash", "bKash", "Nagad", "bank", "other", "balance")


def customer_price(sub, plan, days=None):
    """What the customer pays for this package: their seller's price (with the subscriber profit) for a seller's
    customer, otherwise the package price; plus the package's taxes / fees. Other than the package's own number of
    days (custom expiry) is charged pro rata."""
    import mod_policy
    base = mod_policy.seller_price(dict(sub, plan_id=plan["id"])) or float(plan["price"] or 0)
    price = mod_policy.fee_total(base, plan["id"])
    full = int(plan["validity_days"] or 30)
    if days and days != full:
        price = price * days / full
    return round(price, 2)


def _custom_days(sub, expiry_text):
    base = max(sub["expiry_date"] or date.today(), date.today())
    try:
        days = (date.fromisoformat(expiry_text or "") - base).days
    except ValueError:
        return None
    return days if 1 <= days <= 3650 else None


@route("/subscribers/charge-info", module="subscribers", need="edit")
def subscriber_charge_info():
    """For the Activation window: what the seller pays, what the customer pays, and the customer's balance."""
    rows = _visible_subs("username = %s", (request.args.get("u", "").strip(),))
    plan_id = request.args.get("plan", type=int)
    plan = _plan(plan_id) if plan_id else None
    if not rows or not plan or not core.plan_allowed(plan_id, rows[0]["plan_id"]):
        return jsonify({"charge": None, "price": None})
    sub = rows[0]
    days = _custom_days(sub, request.args.get("exp")) if request.args.get("exp") else None
    return jsonify({"charge": charge_info(dict(sub, plan_id=plan_id)), "price": customer_price(sub, plan, days),
                    "balance": round(float(sub["balance"] or 0), 2)})


@route("/subscribers/activate", module="subscribers", methods=["POST"])
def subscriber_activate():
    f = request.form
    back = core.safe_next(f.get("next")) or url_for("subscriber_activation")
    sub = q("SELECT * FROM subscribers WHERE username = %s", (f.get("username", "").strip(),), one=True)
    if not sub:
        flash("Choose a subscriber from the list.", "error")
        return redirect(back)
    _own(sub)
    try:
        plan_id = int(f.get("plan_id") or sub["plan_id"] or 0)
    except ValueError:
        plan_id = 0
    plan = _plan(plan_id)
    if not plan:
        flash("Choose a package.", "error")
        return redirect(back)
    if not core.plan_allowed(plan_id, sub["plan_id"]):
        abort(403)
    paying = f.get("payment") == "on"
    base = max(sub["expiry_date"] or date.today(), date.today())
    custom = f.get("custom") == "on"
    if custom and not core.can_fn("sub.custom_expiry"):
        flash("You do not have permission to set a custom expiry date. Ask the administrator to allow Custom Expiry.", "error")
        return redirect(back)
    method = f.get("method") if f.get("method") in PAY_METHODS else "cash"
    from_balance = not paying or method == "balance"      # Payment off = the package is paid from the customer's balance
    try:
        if custom:
            days = _custom_days(sub, f.get("expiry"))
            if days is None:
                raise ValueError
            custom_time = (f.get("expiry_time") or "").strip()
            if custom_time and not TIME_RE.match(custom_time):
                raise ValueError
        else:
            days = int(plan["validity_days"] or 30)
        received = 0.0 if from_balance else round(float(f.get("amount") or 0), 2)
        assert 0 <= received < 10 ** 8
    except (ValueError, AssertionError):
        flash("Pick an expiry date after %s (within 10 years) and enter a valid amount." % base.strftime("%d %b %Y"), "error")
        return redirect(back)
    price = customer_price(sub, plan, days)
    have = round(float(sub["balance"] or 0), 2)
    # what the customer's balance changes by: the whole price when paid from the balance; otherwise money received
    # beyond the price is added to it, and a shortfall is taken from it
    change = -price if from_balance else round(received - price, 2)
    if change < 0 and have + change < -0.001:
        flash("Not activated: %s's balance (%.2f) is lower than the %.2f needed%s. Add balance to the customer, or take "
              "the payment in cash. Nothing was changed." % (sub["username"], have, -change,
                                                           "" if from_balance else " besides the %.2f received" % received), "error")
        return redirect(back)
    user_note = f.get("note", "").strip()
    sub2 = dict(sub, plan_id=plan_id)
    why = charge_me(sub2, "Activation %s +%d days" % (sub["username"], days))
    if why:
        flash("Not activated: %s. Add balance first. Nothing was changed." % why, "error")
        return redirect(back)
    if plan_id != sub["plan_id"]:
        x("UPDATE subscribers SET plan_id = %s WHERE id = %s", (plan_id, sub["id"]))
    if change:
        with get_db().cursor() as cur:
            # locked, conditional update: two activations at once can never take the balance below zero
            cur.execute("UPDATE subscribers SET balance = balance + %s WHERE id = %s AND balance + %s >= 0",
                        (change, sub["id"], change))
            if cur.rowcount != 1:
                get_db().rollback()
                flash("Not activated: the customer's balance changed meanwhile. Try again. Nothing was changed.", "error")
                return redirect(back)
            cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
            after = float(cur.fetchone()["balance"])
            if from_balance:
                text = "Activation +%d days (%s)" % (days, plan["label"] or plan["name"])
            elif change > 0:
                text = "Extra from activation payment (received %.2f, package %.2f)" % (received, price)
            else:
                text = "Rest of activation from balance (received %.2f, package %.2f)" % (received, price)
            cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                        (sub["id"], change, after, text[:255], g.me["username"]))
    if from_balance:
        pay_method, auto = "balance", "Paid from the customer's balance"
    elif change > 0:
        pay_method, auto = method, "Received %.2f, %.2f added to the balance" % (received, change)
    elif change < 0:
        pay_method, auto = method + "+balance", "Received %.2f, %.2f taken from the balance" % (received, -change)
    else:
        pay_method, auto = method, ""
    note = "; ".join(t for t in (user_note, auto) if t)
    # the payment row is the package price (revenue); extra money waits in the balance until it is used
    # the time the new expiry ends at: the time typed with a custom date; otherwise the moment of activation, unless the
    # account is still running (then its time is kept) or the package has its own fixed time
    if custom:
        new_time = custom_time or datetime.now().strftime("%H:%M")
    elif plan.get("fixed_expire_time"):
        new_time = None
    else:
        running = end_moment(sub, _plan(sub["plan_id"]))
        new_time = sub.get("expiry_time") if running and running > datetime.now() else datetime.now().strftime("%H:%M")
    new_expiry = do_renew(sub2, days, price, pay_method[:32], note, notify=price > 0, exact=custom, expiry_time=new_time)
    audit("activated", sub["username"], "%s, +%d days, %.2f via %s%s" % (plan["label"] or plan["name"], days, price, pay_method,
                                                                     (", balance %+.2f" % change) if change else ""))
    commit()
    shown_time = new_time or plan.get("fixed_expire_time") or "23:59"
    flash("%s is active until %s %s." % (sub["username"], new_expiry.strftime("%d %b %Y"), _clock(shown_time)), "success")
    if f.get("print") and g.get("last_payment"):
        return redirect(url_for("receipt", pid=g.last_payment, print=1))   # Activate & Print: open the receipt and print it
    return redirect(back)


# ===================================================================== grace period and MAC binding
@route("/subscribers/<int:sid>/grace", module="subscribers", methods=["POST"])
def subscriber_grace(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    try:
        days = int(request.form.get("days", "0"))
        assert 0 <= days <= 90
    except (ValueError, AssertionError):
        flash("Grace days must be between 0 and 90 (0 removes the grace period).", "error")
        return redirect(url_for("subscriber_view", sid=sid))
    until = (date.today() + timedelta(days=days)) if days else None
    x("UPDATE subscribers SET grace_until = %s WHERE id = %s", (until, sid))
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], _plan(sub["plan_id"]), sub["status"], sub["expiry_date"],
                               sub["static_ip"])
    audit("grace_set" if days else "grace_removed", sub["username"], "%d days" % days)
    commit()
    flash("Grace period until %s." % until.strftime("%d %b %Y") if until else "Grace period removed.", "success")
    return redirect(url_for("subscriber_view", sid=sid))


@route("/subscribers/<int:sid>/mac", module="subscribers", methods=["POST"])
def subscriber_mac(sid):
    sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True) or abort(404)
    try:
        mac = norm_mac(request.form.get("mac"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("subscriber_view", sid=sid))
    x("UPDATE subscribers SET mac_bind = %s WHERE id = %s", (mac, sid))
    with get_db().cursor() as cur:
        radius.sync_subscriber(cur, sub["username"], _plan(sub["plan_id"]), sub["status"], sub["expiry_date"],
                               sub["static_ip"])
    audit("mac_bound" if mac else "mac_unbound", sub["username"], mac)
    commit()
    flash("Login is locked to %s." % mac if mac else "MAC binding removed.", "success")
    return redirect(url_for("subscriber_view", sid=sid))


# ===================================================================== extra service charges
@route("/subscribers/charges", module="subscribers")
def charges():
    where, params = ["1=1"], []
    names = core.my_downline()
    if names is not None:
        where.append("s.created_by IN (%s)" % ",".join(["%s"] * len(names)))
        params += names
    text = request.args.get("q", "").strip()
    if text:
        where.append("(s.username LIKE %s OR c.name LIKE %s)")
        params += ["%" + text + "%"] * 2
    if request.args.get("paid") in ("0", "1"):
        where.append("c.paid = %s")
        params.append(int(request.args["paid"]))
    cond = " WHERE " + " AND ".join(where)
    join = " FROM sub_charges c JOIN subscribers s ON s.id = c.subscriber_id"
    tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(CASE WHEN c.paid = 0 THEN c.amount ELSE 0 END), 0) AS due" +
            join + cond, params, one=True)
    pg = pager(tot["c"])
    rows = q("SELECT c.*, s.username, s.full_name" + join + cond + " ORDER BY c.id DESC LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    keep = {k: v for k, v in request.args.items() if k != "page"}
    fees = q("SELECT id, name, kind, mode, value FROM fees WHERE active = 1 ORDER BY name")
    return render_template("charges.html", rows=rows, tot=tot, pg=pg, keep=keep, fees=fees)


@route("/subscribers/charges/save", module="subscribers", methods=["POST"])
def charge_save():
    f = request.form
    sub = q("SELECT * FROM subscribers WHERE username = %s", (f.get("username", "").strip(),), one=True)
    if not sub:
        flash("No subscriber has that username.", "error")
        return redirect(url_for("charges"))
    _own(sub)
    try:
        name = f.get("name", "").strip()
        amount = round(float(f.get("amount", "0")), 2)
        assert 1 <= len(name) <= 64 and 0 < amount < 10 ** 7
    except (ValueError, AssertionError):
        flash("Enter a charge name and an amount above zero.", "error")
        return redirect(url_for("charges"))
    insert("INSERT INTO sub_charges (subscriber_id, name, amount, note, created_by) VALUES (%s, %s, %s, %s, %s)",
           (sub["id"], name, amount, f.get("note", "").strip()[:255], g.me["username"]))
    audit("charge_added", sub["username"], "%s %s" % (name, amount))
    commit()
    flash("Charge added to %s." % sub["username"], "success")
    return redirect(url_for("charges"))


def _charge(cid):
    row = q("SELECT c.*, s.created_by AS owner FROM sub_charges c JOIN subscribers s ON s.id = c.subscriber_id "
            "WHERE c.id = %s", (cid,), one=True) or abort(404)
    names = core.my_downline()
    if names is not None and row["owner"] not in names:
        abort(403)
    return row


@route("/subscribers/charges/<int:cid>/paid", module="subscribers", methods=["POST"])
def charge_paid(cid):
    row = _charge(cid)
    if not row["paid"]:
        x("UPDATE sub_charges SET paid = 1, paid_at = NOW() WHERE id = %s", (cid,))
        insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, received_by) "
               "SELECT s.id, s.username, %s, 0, 'cash', %s, %s FROM subscribers s WHERE s.id = %s",
               (row["amount"], "Extra fee: " + row["name"], g.me["username"], row["subscriber_id"]))
        audit("charge_paid", "#%d" % cid, row["name"])
        commit()
    flash("Marked as paid and recorded as a payment.", "success")
    return redirect(url_for("charges"))


@route("/subscribers/charges/<int:cid>/delete", module="subscribers", methods=["POST"])
def charge_delete(cid):
    _charge(cid)
    x("DELETE FROM sub_charges WHERE id = %s", (cid,))
    audit("charge_deleted", "#%d" % cid)
    commit()
    flash("Charge deleted.", "success")
    return redirect(url_for("charges"))
