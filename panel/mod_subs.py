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


def plan_expiry(plan, base, days, monthly=False):
    """New expiry for a renewal. When the days are the package's own length, months and years are real calendar
    months and years (not 30 or 365 days); a custom number of days is added as plain days.
    monthly (the customer's reseller has Monthly renew): a 30 / 60 / 90 ... day package renewed for its own length
    becomes 1 / 2 / 3 calendar months (5 Jan -> 5 Feb, 31 Jan -> 28 Feb); the price stays the package's."""
    full = int((plan or {}).get("validity_days") or 0)
    if plan and plan.get("duration_type") in ("month", "year") and plan.get("duration") and days == full:
        n = int(plan["duration"]) * (12 if plan["duration_type"] == "year" else 1)
        return add_months(base, n)
    if monthly and full and days == full and full % 30 == 0:
        return add_months(base, full // 30)
    return base + timedelta(days=days)


MONTHLY_KEY = "sub.monthly_renew"     # Users > Roles > (Reseller ...) > Subscriber > Monthly Renew


def role_monthly(cur, role):
    """Is Monthly Renew ticked for this reseller / sub-reseller / retailer role? (off unless ticked)"""
    if role not in core.SELLER_ROLES:
        return False
    cur.execute("SELECT allowed FROM role_funcs WHERE role = %s AND fkey = %s", (role, MONTHLY_KEY))
    r = cur.fetchone()
    return bool(r and r["allowed"])


def monthly_for(sub):
    """Does this customer's reseller / sub-reseller / retailer renew by calendar month (Monthly Renew on their role)?"""
    who = (sub or {}).get("created_by")
    if not who:
        return False
    r = q("SELECT role FROM panel_admins WHERE username = %s", (who,), one=True)
    if not r:
        return False
    with get_db().cursor() as cur:
        return role_monthly(cur, r["role"])


_KEEP = object()
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clock(hhmm):
    """'15:30' -> '3:30 PM' (how times are shown in the panel)."""
    h, m = (int(v) for v in hhmm.split(":")[:2])
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def time_hidden():
    """Custom Expiry - Disable Time ticked: this person picks the date only (never the administrator)."""
    return g.me["role"] != "admin" and core.can_fn("sub.custom_expiry_nt")


def end_moment(sub, plan):
    """When the account ends now: the expiry date at the subscriber's time, else the package's, else 23:59:59."""
    if not sub.get("expiry_date"):
        return None
    at = sub.get("expiry_time") or (plan or {}).get("fixed_expire_time") or "23:59"
    h, m = (int(v) for v in at.split(":")[:2])
    return datetime.combine(sub["expiry_date"], datetime.min.time()).replace(hour=h, minute=m, second=59 if at == "23:59" else 0)


def do_renew(sub, days, amount, method="cash", note="", notify=True, exact=False, expiry_time=_KEEP, from_balance=0.0,
             prev_plan_id=_KEEP):
    """Add days (from today, or from the current expiry if it is later), activate and record the payment.
    exact=True uses a date the person typed as it is (no whole-month or fixed-day rounding).
    expiry_time: "HH:MM" (or None for the package's time / end of day) to store with the new expiry; left out, the
    subscriber's stored time is kept."""
    base = max(sub["expiry_date"] or date.today(), date.today())
    plan = _plan(sub["plan_id"])
    new_expiry = plan_expiry(plan, base, days, monthly=monthly_for(sub))
    if plan and plan.get("fixed_expire_day"):
        new_expiry = fixed_expiry(new_expiry, base, plan["fixed_expire_day"])
    if exact:
        new_expiry = base + timedelta(days=days)
    x("UPDATE subscribers SET expiry_date = %s, status = 'active' WHERE id = %s", (new_expiry, sub["id"]))
    if expiry_time is not _KEEP:
        x("UPDATE subscribers SET expiry_time = %s WHERE id = %s", (expiry_time, sub["id"]))
    # what a reseller / sub-reseller / retailer paid for this package (set by charge_me just before), kept for the profit report
    charged = g.pop("charged", None) or (None, None)
    # prev_*: what this renewal changed, so Revert Last Invoice can put it back; from_balance: what it took from (or,
    # negative, added to) the customer's balance
    pay_id = insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, period_to, "
                    "received_by, payer_id, cost, prev_expiry, prev_expiry_time, prev_status, from_balance, prev_plan_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (sub["id"], sub["username"], amount, (new_expiry - base).days, method[:32], note[:255], new_expiry,
                     g.me["username"], charged[0], charged[1], sub.get("expiry_date"), sub.get("expiry_time"),
                     sub.get("status"), round(float(from_balance or 0), 2),
                     sub.get("plan_id") if prev_plan_id is _KEEP else prev_plan_id))
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
    import appnotify             # in the customer's app, always (the SMS / email switch above is separate)
    with get_db().cursor() as cur:
        appnotify.payment(cur, sub["id"], amount, new_expiry, core.cfg().get("currency"), pay_id, g.me["username"])
    return new_expiry


def _price_of(cur, user_id, plan_id):
    """What this person pays for the package (Users > Assign Packages), or None when no price is set."""
    cur.execute("SELECT price FROM user_plans WHERE user_id = %s AND plan_id = %s", (user_id, plan_id))
    r = cur.fetchone()
    try:
        return round(float(r["price"]), 2) if r and r["price"] is not None else None
    except (TypeError, ValueError):
        return None


def seller_cost(cur, seller_id, plan_id, days=None):
    """What the seller pays for one activation: their price for the package (Users > the seller > Packages), or the
    package's own price when none is set for them (never free by mistake; an explicit 0 stays free). A renewal for
    more or fewer days than the package's own length costs that share of it (60 days of a 30-day package = 2x).
    None when there is no package."""
    if not plan_id:
        return None
    cost = _price_of(cur, seller_id, plan_id)
    cur.execute("SELECT price, validity_days FROM plans WHERE id = %s", (plan_id,))
    plan = cur.fetchone()
    if not plan:
        return cost
    if cost is None:
        cost = round(float(plan["price"] or 0), 2)
    full = int(plan["validity_days"] or 0)
    if days and full and int(days) != full:
        cost = round(cost * int(days) / full, 2)
    return cost


def charge_seller(cur, seller_id, plan_id, label, by, days=None):
    """Take the seller's package price (seller_cost) from their balance for one activation or renewal and write it in
    their ledger. Returns None when done (or when the price is 0), or a short reason when their balance is too low."""
    cost = seller_cost(cur, seller_id, plan_id, days)
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


def charge_me(sub, label, days=None):
    """Take the package price from whoever pays for this activation (see payer_for). Returns None when done or when
    nobody is charged, or a sentence saying whose balance is too low."""
    g.pop("charged", None)
    payer = payer_for(sub)
    if not payer:
        return None
    if not payer["mine"]:
        label = "%s (by %s)" % (label, g.me["username"])
    with get_db().cursor() as cur:
        why = charge_seller(cur, payer["id"], sub["plan_id"], label, g.me["username"], days)
    if why:
        return ("your " if payer["mine"] else "%s's " % payer["username"]) + why
    with get_db().cursor() as cur:
        cost = seller_cost(cur, payer["id"], sub["plan_id"], days) if sub.get("plan_id") else None
    if cost:
        g.charged = (payer["id"], cost)
    return None


def charge_info(sub, days=None):
    """What the activation window shows: {'cost', 'who', 'mine', 'balance', 'low'} or None."""
    payer = payer_for(sub)
    if not payer or not sub.get("plan_id"):
        return None
    with get_db().cursor() as cur:
        cost = seller_cost(cur, payer["id"], sub["plan_id"], days)
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
    return q("SELECT id, username, full_name, phone, status, plan_id, expiry_date, expiry_time, created_by, balance, "
             "(SELECT p.fixed_expire_time FROM plans p WHERE p.id = subscribers.plan_id) AS plan_time FROM subscribers WHERE " + where +
             " ORDER BY username LIMIT 15", args)


def _ends_text(r):
    """'17 Jan 2027 11:59 PM': the date and the time the package ends (the customer's own time, else the package's,
    else the end of the day), as the subscriber list shows it."""
    if not r.get("expiry_date"):
        return ""
    try:
        end = end_moment(r, {"fixed_expire_time": r.get("plan_time")})
    except (TypeError, ValueError):
        return r["expiry_date"].strftime("%d %b %Y")
    return end.strftime("%d %b %Y") + " " + _clock(end.strftime("%H:%M"))


def _sub_json(r):
    return {"id": r["id"], "username": r["username"], "name": r["full_name"] or "", "phone": r.get("phone") or "", "state": _state(r),
            "plan_id": r["plan_id"],
            "expiry": _ends_text(r),
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
    return jsonify({"charge": charge_info(dict(sub, plan_id=plan_id), days), "price": customer_price(sub, plan, days),
                    "balance": round(float(sub["balance"] or 0), 2)})


def activate_one(sub, plan, days, from_balance, method, received, custom=False, custom_time="", user_note="",
                 notify=True, label="Activation"):
    """Activate / renew one customer for `days` on `plan` (the rules of the Activation window, also used by Mass
    Activation). Nothing is committed here: the caller commits, or rolls back on failure.

    from_balance: the whole price comes out of the customer's balance; otherwise `received` (money taken by `method`)
    pays it, extra goes into the balance and a shortfall is taken from it. A seller also pays their own package cost
    (charge_me). Returns {"ok": True, "expiry", "time", "price", "method", "change"} or {"ok": False, "error", "seller"}
    where seller=True means the seller's own balance ran out (no point trying further customers)."""
    plan_id = plan["id"]
    price = customer_price(sub, plan, days)
    have = round(float(sub["balance"] or 0), 2)
    # what the customer's balance changes by: the whole price when paid from the balance; otherwise money received
    # beyond the price is added to it, and a shortfall is taken from it
    change = -price if from_balance else round(received - price, 2)
    if change < 0 and have + change < -0.001:
        return {"ok": False, "seller": False, "error":
                "Not activated: %s's balance (%.2f) is lower than the %.2f needed%s. Add balance to the customer, or take "
                "the payment in cash. Nothing was changed." % (sub["username"], have, -change,
                                                             "" if from_balance else " besides the %.2f received" % received)}
    running = bool(sub.get("expiry_date") and sub["expiry_date"] >= date.today() and sub.get("status") == "active")
    if plan_id != sub["plan_id"] and sub["plan_id"] and running and not core.can_fn("sub.mid_change"):
        return {"ok": False, "seller": False, "error":
                "Not activated: %s's package is still running until %s. Changing the package before then needs the "
                "Mid-Period Package Change permission (or use Migration). Nothing was changed."
                % (sub["username"], sub["expiry_date"].strftime("%d %b %Y"))}
    sub2 = dict(sub, plan_id=plan_id)
    why = charge_me(sub2, "%s %s +%d days" % (label, sub["username"], days), days)
    if why:
        return {"ok": False, "seller": True, "error": "Not activated: %s. Add balance first. Nothing was changed." % why}
    if plan_id != sub["plan_id"]:
        x("UPDATE subscribers SET plan_id = %s WHERE id = %s", (plan_id, sub["id"]))
    if change:
        with get_db().cursor() as cur:
            # locked, conditional update: two activations at once can never take the balance below zero
            cur.execute("UPDATE subscribers SET balance = balance + %s WHERE id = %s AND balance + %s >= 0",
                        (change, sub["id"], change))
            if cur.rowcount != 1:
                get_db().rollback()
                return {"ok": False, "seller": False,
                        "error": "Not activated: the customer's balance changed meanwhile. Try again. Nothing was changed."}
            cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
            after = float(cur.fetchone()["balance"])
            if from_balance:
                text = "%s +%d days (%s)" % (label, days, plan["label"] or plan["name"])
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
    new_expiry = do_renew(sub2, days, price, pay_method[:32], note, notify=notify and price > 0, exact=custom,
                          expiry_time=new_time, from_balance=-change, prev_plan_id=sub["plan_id"])
    audit("activated", sub["username"], "%s, +%d days, %.2f via %s%s" % (plan["label"] or plan["name"], days, price, pay_method,
                                                                     (", balance %+.2f" % change) if change else ""))
    return {"ok": True, "expiry": new_expiry, "time": new_time, "price": price, "method": pay_method, "change": change}


MASS_METHODS = (("balance", "Subscriber Balance"), ("cash", "Cash received"), ("smart", "Smart Payment"))
MASS_LIMIT = 1000


def _mass_subs(back):
    """The ticked customers this person may touch (a seller: only their downline's), or None after a flash."""
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()][:MASS_LIMIT]
    if not ids:
        flash("Tick the customers in the list first.", "warn")
        return None
    subs = q("SELECT * FROM subscribers WHERE id IN (%s) ORDER BY username" % ",".join(["%s"] * len(ids)), ids)
    names = core.my_downline()
    if names is not None:
        subs = [s for s in subs if s["created_by"] in names]
    if not subs:
        flash("None of the ticked customers can be changed by you.", "warn")
        return None
    return subs


def _names(rows, limit=8):
    shown = ", ".join(rows[:limit])
    return shown + (" and %d more" % (len(rows) - limit) if len(rows) > limit else "")


@route("/subscribers/mass-activate", module="subscribers", methods=["POST"])
def subscribers_mass_activate():
    """Mass Activation: renew every ticked customer on their current package or a chosen one.
      balance: only customers whose balance covers the price are activated (paid from the balance), others skipped
      cash:    everyone is activated, the price is recorded as cash received
      smart:   the customer's balance when it covers the price, otherwise cash received
    A seller's own package cost is taken from their balance for each customer, as in the Activation window."""
    f = request.form
    back = core.safe_next(f.get("next")) or url_for("subscribers")
    subs = _mass_subs(back)
    if subs is None:
        return redirect(back)
    mode = f.get("method") if f.get("method") in dict(MASS_METHODS) else ""
    if not mode:
        flash("Choose a payment method.", "error")
        return redirect(back)
    chosen = None
    if f.get("plan_id", "") not in ("", "current"):
        chosen = _plan(int(f["plan_id"])) if f["plan_id"].isdigit() else None
        if not chosen:
            flash("Choose a package.", "error")
            return redirect(back)
    custom = f.get("custom") == "on"
    custom_time = "" if time_hidden() else (f.get("expiry_time") or "").strip()
    if custom and not core.can_fn("sub.custom_expiry"):
        flash("You do not have permission to set a custom expiry date. Ask the administrator to allow Custom Expiry.", "error")
        return redirect(back)
    if custom and custom_time and not TIME_RE.match(custom_time):
        flash("Enter the expiry time as HH:MM.", "error")
        return redirect(back)
    notify = f.get("notify") == "on"
    done, low, no_plan, not_allowed, bad_date, other = [], [], [], [], [], []
    stopped = ""
    for sub in subs:
        plan = chosen or _plan(sub["plan_id"])
        if not plan:
            no_plan.append(sub["username"])
            continue
        if not core.plan_allowed(plan["id"], sub["plan_id"]):
            not_allowed.append(sub["username"])
            continue
        if custom:
            days = _custom_days(sub, f.get("expiry"))
            if days is None:
                bad_date.append(sub["username"])
                continue
        else:
            days = int(plan["validity_days"] or 30)
        price = customer_price(sub, plan, days)
        enough = round(float(sub["balance"] or 0), 2) + 0.001 >= price
        if mode == "balance" and not enough:
            low.append(sub["username"])
            continue
        from_balance = mode == "balance" or (mode == "smart" and enough)
        res = activate_one(sub, plan, days, from_balance, "cash", 0.0 if from_balance else price, custom,
                           custom_time if custom else "", "Mass activation", notify=notify, label="Mass activation")
        if res["ok"]:
            commit()
            done.append(sub["username"])
        else:
            get_db().rollback()
            if res.get("seller"):
                stopped = res["error"]
                break
            other.append(sub["username"])
    audit("mass_activation", "%d subscribers" % len(done), "%s, %s" % (dict(MASS_METHODS)[mode],
                                                                         (chosen["label"] or chosen["name"]) if chosen else "current package"))
    commit()
    if done:
        flash("%d subscriber(s) activated." % len(done), "success")
    for rows, why in ((low, "balance lower than the package price"), (no_plan, "no package"),
                      (not_allowed, "package not allowed for you"), (bad_date, "custom date is not after their expiry"),
                      (other, "balance changed meanwhile, try again")):
        if rows:
            flash("Skipped %d (%s): %s." % (len(rows), why, _names(rows)), "warn")
    if stopped:
        flash("Stopped after %d: %s" % (len(done), stopped), "error")
    if not done and not stopped and not any((low, no_plan, not_allowed, bad_date, other)):
        flash("Nobody was activated.", "warn")
    return redirect(back)


MASS_PAY_METHODS = ("cash", "bKash", "Nagad", "bank", "other")


@route("/subscribers/mass-payment", module="subscribers", methods=["POST"])
def subscribers_mass_payment():
    """Mass Payment: add the same amount to the balance of every ticked customer (Ledger: Balance Topup). A seller pays
    it from their own balance, as with Add Balance on one customer. Activation can then take it from the balance."""
    f = request.form
    back = core.safe_next(f.get("next")) or url_for("subscribers")
    subs = _mass_subs(back)
    if subs is None:
        return redirect(back)
    method = f.get("method") if f.get("method") in MASS_PAY_METHODS else ""
    try:
        amount = round(float(f.get("amount") or 0), 2)
        assert 0 < amount < 10 ** 7
    except (ValueError, AssertionError):
        amount = 0
    if not method or not amount:
        flash("Choose a method and enter an amount above zero.", "error")
        return redirect(back)
    seller = g.me["role"] in core.SELLER_ROLES
    done, stopped = [], ""
    for sub in subs:
        if seller:
            ok, have = core.seller_pays(amount, "Mass payment to %s" % sub["username"])
            if not ok:
                get_db().rollback()
                stopped = "your balance (%.2f) is lower than %.2f" % (have, amount)
                break
        with get_db().cursor() as cur:
            cur.execute("UPDATE subscribers SET balance = balance + %s WHERE id = %s", (amount, sub["id"]))
            cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
            after = float(cur.fetchone()["balance"])
            cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                        (sub["id"], amount, after, ("Mass payment (%s)" % method)[:255], g.me["username"]))
        audit("balance_changed", sub["username"], "%+.2f -> %.2f (mass payment, %s)" % (amount, after, method))
        import appnotify
        with get_db().cursor() as cur:
            cur.execute("SELECT MAX(id) AS m FROM sub_ledger WHERE subscriber_id = %s", (sub["id"],))
            appnotify.balance(cur, sub["id"], amount, after, core.cfg().get("currency"),
                              "%s:%s" % (sub["id"], cur.fetchone()["m"]), g.me["username"])
        commit()
        done.append(sub["username"])
    if done:
        flash("%.2f added to the balance of %d subscriber(s)." % (amount, len(done)), "success")
    if stopped:
        flash("Stopped after %d: %s. Add balance to yourself first." % (len(done), stopped), "error")
    return redirect(back)


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
            custom_time = "" if time_hidden() else (f.get("expiry_time") or "").strip()
            if custom_time and not TIME_RE.match(custom_time):
                raise ValueError
        else:
            days = int(plan["validity_days"] or 30)
        received = 0.0 if from_balance else round(float(f.get("amount") or 0), 2)
        assert 0 <= received < 10 ** 8
    except (ValueError, AssertionError):
        flash("Pick an expiry date after %s (within 10 years) and enter a valid amount." % base.strftime("%d %b %Y"), "error")
        return redirect(back)
    res = activate_one(sub, plan, days, from_balance, method, received, custom,
                       custom_time if custom else "", f.get("note", "").strip())
    if not res["ok"]:
        flash(res["error"], "error")
        return redirect(back)
    commit()
    new_expiry, new_time = res["expiry"], res["time"]
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
