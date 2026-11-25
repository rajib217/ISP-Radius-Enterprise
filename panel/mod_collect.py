"""Bill Collector (বিল কালেক্টর): a person who goes door to door and takes the customers' payments.

  /collect                        the due list: customers who owe (expired package or unpaid charges), most urgent first
  /collect/<sid>                  take a payment (cash, the bank's own POS terminal, or bKash typed by hand)
  /collect/receipt/<cid>          the printable receipt
  /collect/report                 the collector's own day: what they took, cash in hand, deposits
  /collect/deposit                hand the cash in hand over to whoever hired them
  /collections                    for the hirer (administrator, or the reseller / sub-reseller / retailer who added the
                                  collector): their collectors' cash in hand, deposits to confirm, collections; admin voids

A collector belongs to whoever added them (panel_admins.parent_id): a reseller's collector sees only that reseller's
downline's customers (core.seller_boss / my_downline); an office collector (no reseller above) sees every customer.
Posting reuses the Activation rules (mod_subs.activate_one): an expired package is renewed when the money covers it
(a reseller's own cost comes out of the reseller's balance as usual), unpaid charges are paid, the rest goes into the
customer's balance (Ledger). A posted collection is never changed; only an administrator can void one (audited).
Card numbers are never stored: the POS is the bank's standalone machine, the panel keeps its approval code and the
card's last 4 digits only. No bank or wallet API is called."""
import re
import secrets
from datetime import date, datetime, timedelta

from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import audit, commit, get_db, insert, q, route, x

METHODS = (("cash", "Cash"), ("pos", "POS (card machine)"), ("bkash", "bKash"))
METHOD_KEY = {"cash": "col.collect_cash", "bkash": "col.collect_cash", "pos": "col.collect_pos"}
DEFAULT_MAX_TXN = 10000          # settings col_max_txn: the most one collection may be
DEFAULT_MAX_DAY = 100000         # settings col_max_day: the most one collector may take in a day
REPEAT_MIN = 10                  # same customer and amount again within this many minutes: ask first
TRX_RE = re.compile(r"^[A-Za-z0-9]{8,12}$")
APPROVAL_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
LAST4_RE = re.compile(r"^\d{4}$")
CARD_RE = re.compile(r"\d[\d \-]{11,}\d")       # 13+ digits in a row: looks like a whole card number, never kept


def is_collector():
    return g.me["role"] == "collector"


def _need_collector(key="col.module"):
    if not is_collector() or not core.can_fn("col.module") or not core.can_fn(key):
        abort(403)


def caps():
    s = core.cfg()

    def num(k, d):
        try:
            v = float(s.get(k) or d)
            return v if v > 0 else d
        except ValueError:
            return d
    return num("col_max_txn", DEFAULT_MAX_TXN), num("col_max_day", DEFAULT_MAX_DAY)


def _scope_sql(alias="s"):
    names = core.my_downline()
    if names is None:
        return "", []
    if not names:
        return " AND 1 = 0", []
    return " AND %s.created_by IN (%s)" % (alias, ",".join(["%s"] * len(names))), list(names)


def due_of(sub):
    """What a customer owes now: {'package', 'charges', 'total', 'plan', 'days', 'price'}. An expired (not suspended)
    package is owed at the customer's price less what is already in their balance, as Activation would charge it."""
    import mod_subs
    charges = float(q("SELECT COALESCE(SUM(amount), 0) AS t FROM sub_charges WHERE subscriber_id = %s AND paid = 0",
                      (sub["id"],), one=True)["t"] or 0)
    plan = mod_subs._plan(sub["plan_id"]) if sub.get("plan_id") else None
    days = int(plan["validity_days"] or 30) if plan else 0
    price = package = 0.0
    spare = float(sub.get("balance") or 0)
    if plan and sub.get("status") != "suspended" and sub.get("expiry_date") and sub["expiry_date"] < date.today():
        price = round(mod_subs.customer_price(sub, plan, days), 2)
        package = round(max(price - spare, 0), 2)
        spare = max(spare - price, 0)
    charges = round(max(charges - max(spare, 0), 0), 2)       # money already in the balance pays charges too
    return {"package": package, "charges": charges, "total": round(package + charges, 2), "plan": plan,
            "days": days, "price": price}


def cash_in_hand(collector_id):
    """Cash a collector took (posted) less the deposits their hirer confirmed; and what waits for confirmation."""
    got = float(q("SELECT COALESCE(SUM(amount), 0) AS t FROM collections WHERE collector_id = %s AND method = 'cash' "
                  "AND status = 'posted'", (collector_id,), one=True)["t"] or 0)
    dep = q("SELECT COALESCE(SUM(CASE WHEN status = 'confirmed' THEN amount END), 0) AS done, "
            "COALESCE(SUM(CASE WHEN status = 'pending' THEN amount END), 0) AS waiting FROM collection_deposits "
            "WHERE collector_id = %s", (collector_id,), one=True)
    return {"hand": round(got - float(dep["done"] or 0), 2), "pending": round(float(dep["waiting"] or 0), 2)}


def _sub(sid):
    sc, sp = _scope_sql()
    sub = q("SELECT s.*, p.price FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id WHERE s.id = %s" + sc,
            [sid] + sp, one=True)
    if not sub:
        abort(404)
    return sub


# ===================================================================== due list
@route("/collect", module="subscribers")
def collect():
    _need_collector()
    term = request.args.get("q", "").strip()[:64]
    sc, sp = _scope_sql()
    where = ("WHERE s.status <> 'suspended' AND ((s.expiry_date < CURDATE() AND s.plan_id IS NOT NULL) OR EXISTS "
             "(SELECT 1 FROM sub_charges c WHERE c.subscriber_id = s.id AND c.paid = 0))" + sc)
    params = list(sp)
    if term:
        where += " AND (s.username LIKE %s OR s.full_name LIKE %s OR s.phone LIKE %s OR s.area LIKE %s)"
        params += ["%" + term + "%"] * 4
    rows = q("SELECT s.*, p.price, COALESCE(p.label, p.name) AS plan_label FROM subscribers s LEFT JOIN plans p ON p.id = s.plan_id "
             + where + " ORDER BY s.expiry_date IS NULL, s.expiry_date, s.username LIMIT 300", params)
    every = []
    for s in rows:
        d = due_of(s)
        if d["total"] > 0:
            s["due"], s["code"] = d, core.sub_code(s["id"])
            s["late"] = (date.today() - s["expiry_date"]).days if d["package"] and s.get("expiry_date") else 0
            every.append(s)
    # filter chips (counted before the filter): all, an expired package, only unpaid charges; and an area
    show = request.args.get("show") if request.args.get("show") in ("expired", "charges") else "all"
    area = request.args.get("area", "").strip()[:64]
    areas = sorted({s["area"] for s in every if s.get("area")})
    counts = {"all": len(every), "expired": sum(1 for s in every if s["due"]["package"]),
              "charges": sum(1 for s in every if not s["due"]["package"])}
    out = [s for s in every if (show == "all" or (show == "expired") == bool(s["due"]["package"]))
           and (not area or s.get("area") == area)]
    me = g.me["id"]
    today = q("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS t FROM collections WHERE collector_id = %s AND status = 'posted' "
              "AND created_at >= CURDATE()", (me,), one=True)
    paid_today = {r["subscriber_id"] for r in q("SELECT DISTINCT subscriber_id FROM collections WHERE collector_id = %s "
                                                "AND status = 'posted' AND created_at >= CURDATE()", (me,))}
    return render_template("collect.html", rows=out, term=term, today=today, hand=cash_in_hand(me), show=show, area=area,
                           areas=areas, counts=counts, owed=round(sum(s["due"]["total"] for s in out), 2), paid_today=paid_today)


# ===================================================================== take a payment
@route("/collect/<int:sid>", module="subscribers", need="view", methods=["GET", "POST"])   # col.* keys decide
def collect_one(sid):
    _need_collector()
    sub = _sub(sid)
    d = due_of(sub)
    if request.method == "GET":
        return _form(sub, d)
    f = request.form
    method = f.get("method", "")
    if method not in METHOD_KEY or not core.can_fn(METHOD_KEY[method]):
        abort(403)
    try:
        amount = round(float(f.get("amount") or 0), 2)
    except ValueError:
        amount = 0
    err = ""
    max_txn, max_day = caps()
    if amount <= 0:
        err = "Enter the amount received."
    elif d["total"] <= 0:
        err = "%s owes nothing now." % sub["username"]
    elif amount > d["total"] + 0.001:
        err = "The amount (%.2f) is more than the due (%.2f). Take only what is owed." % (amount, d["total"])
    elif amount > max_txn:
        err = "One collection may be at most %.2f." % max_txn
    pos_ref = trx = None
    if not err and method == "pos":
        appr, last4 = f.get("approval", "").strip(), f.get("last4", "").strip()
        if CARD_RE.search(appr) or CARD_RE.search(last4):
            err = "Never type the card number. Only the approval code from the slip and the card's last 4 digits."
        elif not APPROVAL_RE.match(appr) or not LAST4_RE.match(last4):
            err = "Enter the approval code from the POS slip (4-12 letters / digits) and the card's last 4 digits."
        else:
            pos_ref = "%s / ****%s" % (appr.upper(), last4)
    if not err and method == "bkash":
        trx = f.get("trx_id", "").strip().upper()
        if not TRX_RE.match(trx):
            err = "Enter the bKash TrxID (8-12 letters / digits)."
        elif q("SELECT 1 AS x FROM collections WHERE trx_id = %s", (trx,), one=True):
            err = "This bKash TrxID was already used for another collection."
    token = (f.get("form_token") or "").strip()[:48]
    if not err and (not token or q("SELECT receipt_no FROM collections WHERE form_token = %s", (token,), one=True)):
        err = "This payment form was already submitted (the receipt was made once). Open the customer again for a new one."
    if not err:
        day = float(q("SELECT COALESCE(SUM(amount), 0) AS t FROM collections WHERE collector_id = %s AND status = 'posted' "
                      "AND created_at >= CURDATE()", (g.me["id"],), one=True)["t"] or 0)
        if day + amount > max_day + 0.001:
            err = "Today's limit is %.2f; you have taken %.2f already." % (max_day, day)
    if not err and f.get("repeat_ok") != "1" and q(
            "SELECT 1 AS x FROM collections WHERE subscriber_id = %s AND amount = %s AND status = 'posted' "
            "AND created_at >= NOW() - INTERVAL %s MINUTE", (sid, amount, REPEAT_MIN), one=True):
        flash("%s paid %.2f in the last %d minutes already. If this is really a second payment, tick the box and save again."
              % (sub["username"], amount, REPEAT_MIN), "warn")
        return _form(sub, d, dict(f, repeat=True))
    if err:
        flash(err, "error")
        return _form(sub, d, f)
    res = post(sub, d, amount, method, pos_ref, trx, token)
    if not res["ok"]:
        get_db().rollback()
        flash(res["error"], "error")
        return _form(sub, d, f)
    commit()
    flash("Received %.2f from %s. Receipt %s." % (amount, sub["username"], res["receipt"]), "success")
    return redirect(url_for("collect_receipt", cid=res["id"]))


def _form(sub, d, f=None):
    f = f or {}
    charges = q("SELECT name, amount, created_at FROM sub_charges WHERE subscriber_id = %s AND paid = 0 ORDER BY id", (sub["id"],))
    recent = q("SELECT receipt_no, amount, method, status, created_at, id FROM collections WHERE subscriber_id = %s "
               "ORDER BY id DESC LIMIT 5", (sub["id"],))
    late = (date.today() - sub["expiry_date"]).days if d["package"] and sub.get("expiry_date") else 0
    return render_template("collect_form.html", sub=sub, due=d, methods=[m for m in METHODS if core.can_fn(METHOD_KEY[m[0]])],
                           f=f, token=secrets.token_urlsafe(24), code=core.sub_code(sub["id"]), cap=caps()[0], charges=charges,
                           recent=recent, late=late, labels=dict(METHODS), repeat_min=REPEAT_MIN,
                           plan_label=(d["plan"]["label"] or d["plan"]["name"]) if d["plan"] else "")


def post(sub, d, amount, method, pos_ref, trx, token):
    """Book one collection in the open transaction (the caller commits or rolls back): the collections row (its receipt
    number), then the money: renew an expired package it covers, pay unpaid charges, the rest into the balance."""
    import mod_subs
    me = g.me
    boss = core.seller_boss()
    cid = insert("INSERT INTO collections (receipt_no, subscriber_id, username, collector_id, parent_id, amount, method, pos_ref, "
                 "trx_id, form_token) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                 ("tmp-" + secrets.token_hex(8), sub["id"], sub["username"], me["id"], me.get("parent_id"), amount, method,
                  pos_ref, trx, token or None))
    receipt = "CR-%06d" % cid
    x("UPDATE collections SET receipt_no = %s WHERE id = %s", (receipt, cid))
    label = {"cash": "cash", "pos": "POS", "bkash": "bKash"}[method]
    note = "Collected by %s, receipt %s%s" % (me["username"], receipt, (" (%s)" % (pos_ref or trx)) if (pos_ref or trx) else "")
    left, done = amount, []
    # 1. the expired package, when the money (with what is in the balance) covers it
    if d["package"] > 0 and left + 0.001 >= d["package"] and d["plan"]:
        res = mod_subs.activate_one(sub, d["plan"], d["days"], False, label, d["package"], user_note=note,
                                    label="Collection")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        left = round(left - d["package"], 2)
        done.append("renewed to %s" % res["expiry"].strftime("%d %b %Y"))
    # 2. unpaid charges, oldest first, each one only when this money plus what waits in the balance covers it whole
    for c in q("SELECT id, name, amount FROM sub_charges WHERE subscriber_id = %s AND paid = 0 ORDER BY id", (sub["id"],)):
        amt = round(float(c["amount"] or 0), 2)
        spare = max(float(q("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],), one=True)["balance"] or 0), 0)
        if amt <= 0 or left + spare + 0.001 < amt:
            continue
        from_bal = round(max(amt - left, 0), 2)
        if from_bal:
            with get_db().cursor() as cur:
                cur.execute("UPDATE subscribers SET balance = balance - %s WHERE id = %s AND balance >= %s", (from_bal, sub["id"], from_bal))
                if cur.rowcount != 1:
                    return {"ok": False, "error": "The customer's balance changed meanwhile. Try again. Nothing was saved."}
                cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
                cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                            (sub["id"], -from_bal, float(cur.fetchone()["balance"]), ("Rest of %s from balance; %s" % (c["name"], note))[:255],
                             me["username"]))
        x("UPDATE sub_charges SET paid = 1, paid_at = NOW() WHERE id = %s AND paid = 0", (c["id"],))
        insert("INSERT INTO payments (subscriber_id, username, amount, days_added, method, note, received_by) "
               "VALUES (%s, %s, %s, 0, %s, %s, %s)", (sub["id"], sub["username"], amt, label, ("Extra fee: %s; %s" % (c["name"], note))[:255],
                                                      me["username"]))
        left = round(max(left - amt, 0), 2)
        done.append("paid %s" % c["name"])
    # 3. the rest waits in the customer's balance (Ledger)
    if left > 0:
        with get_db().cursor() as cur:
            cur.execute("UPDATE subscribers SET balance = balance + %s WHERE id = %s", (left, sub["id"]))
            cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
            after = float(cur.fetchone()["balance"])
            cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                        (sub["id"], left, after, note[:255], me["username"]))
        done.append("%.2f added to the balance" % left)
    result = "; ".join(done)
    x("UPDATE collections SET result = %s WHERE id = %s", (result[:255], cid))
    audit("collection", sub["username"], "%s %.2f %s%s: %s" % (receipt, amount, label, (" for " + boss["username"]) if boss else "", result))
    return {"ok": True, "id": cid, "receipt": receipt}


def _visible(row):
    """May the signed-in user see this collection / collector? The collector themselves, whoever hired them (an
    administrator sees every collector; a reseller tier the collectors they added) - never another collector."""
    if g.me["role"] == "admin":
        return True
    if is_collector():
        return row["collector_id"] == g.me["id"]
    if g.me["role"] in core.SELLER_ROLES:
        return row.get("parent_id") == g.me["id"]
    return False


@route("/collect/receipt/<int:cid>")
def collect_receipt(cid):
    c = q("SELECT c.*, s.full_name, s.phone, s.address, a.full_name AS collector_name, a.username AS collector_user "
          "FROM collections c LEFT JOIN subscribers s ON s.id = c.subscriber_id JOIN panel_admins a ON a.id = c.collector_id "
          "WHERE c.id = %s", (cid,), one=True) or abort(404)
    if not _visible(c) or (is_collector() and not core.can_fn("col.receipt")):
        abort(403)
    owner = q("SELECT created_by FROM subscribers WHERE id = %s", (c["subscriber_id"],), one=True) or {}
    inv = q("SELECT inv_name, inv_address, inv_phone, inv_footer FROM panel_admins WHERE username = %s",
            (owner.get("created_by") or "",), one=True) or {}       # a reseller's own header, as on payment receipts
    return render_template("collect_receipt.html", c=c, code=core.sub_code(c["subscriber_id"]), inv=inv,
                           method=dict(METHODS).get(c["method"], c["method"]))


# ===================================================================== reminder SMS (off unless ticked)
@route("/collect/<int:sid>/remind", module="subscribers", need="view", methods=["POST"])
def collect_remind(sid):
    _need_collector("col.remind_sms")
    sub = _sub(sid)
    d = due_of(sub)
    import msglib
    s = core.cfg()
    to = msglib.normalize_phone(sub.get("phone"), s.get("sms_country_code", "880"), s.get("sms_number_format", "intl"))
    if d["total"] <= 0 or not to or not sub.get("sms_on", 1):
        flash("No reminder sent: nothing is due, or the customer has no phone / SMS is off for them.", "warn")
        return redirect(url_for("collect_one", sid=sid))
    body = "%s, your bill of %s %.2f is due. Our collector %s will come to take it. %s" % (
        sub["full_name"] or sub["username"], s.get("currency") or "BDT", d["total"], g.me["full_name"] or g.me["username"],
        s.get("isp_name") or "")
    with get_db().cursor() as cur:
        msglib.enqueue(cur, "sms", to, body.strip(), subscriber_id=sid, dedupe="colrem:%d:%s" % (sid, date.today()),
                       by=g.me["username"])
    audit("collection_reminder", sub["username"], "%.2f" % d["total"])
    commit()
    flash("Reminder SMS queued for %s." % sub["username"], "success")
    return redirect(url_for("collect_one", sid=sid))


# ===================================================================== own report and deposits
@route("/collect/report")
def collect_report():
    _need_collector("col.own_report")
    start, end = core.date_range(0)
    lo, hi = datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())
    me = g.me["id"]
    rows = q("SELECT c.*, s.full_name FROM collections c LEFT JOIN subscribers s ON s.id = c.subscriber_id "
             "WHERE c.collector_id = %s AND c.created_at >= %s AND c.created_at < %s ORDER BY c.id DESC", (me, lo, hi))
    tot = {m: sum(float(r["amount"]) for r in rows if r["method"] == m and r["status"] == "posted") for m, _ in METHODS}
    cnt = {m: sum(1 for r in rows if r["method"] == m and r["status"] == "posted") for m, _ in METHODS}
    today = date.today()
    presets = [("Today", today, today), ("Yesterday", today - timedelta(days=1), today - timedelta(days=1)),
               ("7 days", today - timedelta(days=6), today), ("This month", today.replace(day=1), today)]
    deps = q("SELECT * FROM collection_deposits WHERE collector_id = %s ORDER BY id DESC LIMIT 30", (me,))
    return render_template("collect_report.html", rows=rows, tot=tot, cnt=cnt, presets=presets, total=sum(tot.values()), start=start, end=end,
                           deps=deps, hand=cash_in_hand(me), methods=dict(METHODS),
                           boss=core.seller_boss())


@route("/collect/deposit", methods=["POST"])
def collect_deposit():
    """The collector hands over cash: it waits until their hirer confirms they got it."""
    _need_collector("col.deposit")
    try:
        amount = round(float(request.form.get("amount") or 0), 2)
    except ValueError:
        amount = 0
    h = cash_in_hand(g.me["id"])
    room = round(h["hand"] - h["pending"], 2)
    if amount <= 0 or amount > room + 0.001:
        flash("Enter an amount up to your cash in hand that is not handed over yet (%.2f)." % max(room, 0), "error")
        return redirect(url_for("collect_report"))
    insert("INSERT INTO collection_deposits (collector_id, parent_id, amount, note) VALUES (%s, %s, %s, %s)",
           (g.me["id"], g.me.get("parent_id"), amount, request.form.get("note", "").strip()[:255]))
    audit("collection_deposit", g.me["username"], "%.2f" % amount)
    commit()
    flash("Deposit of %.2f sent. It counts once %s confirms getting the cash." % (
        amount, (core.seller_boss() or {}).get("username") or "the office"), "success")
    return redirect(url_for("collect_report"))


@route("/collect/deposit/confirm", methods=["POST"])
def collect_deposit_confirm():
    """Whoever hired the collector (or an administrator) confirms getting the cash, or turns the deposit down."""
    did = request.form.get("deposit_id", "")
    d = q("SELECT d.*, a.parent_id AS boss_id, a.username AS collector_user FROM collection_deposits d "
          "JOIN panel_admins a ON a.id = d.collector_id WHERE d.id = %s", (int(did) if did.isdigit() else 0,), one=True) or abort(404)
    if not (g.me["role"] == "admin" or (g.me["role"] in core.SELLER_ROLES and d["boss_id"] == g.me["id"])):
        abort(403)
    if d["status"] != "pending":
        flash("This deposit was already handled.", "warn")
        return redirect(url_for("collections"))
    ok = request.form.get("act") != "reject"
    x("UPDATE collection_deposits SET status = %s, confirmed_by = %s, confirmed_at = NOW() WHERE id = %s AND status = 'pending'",
      ("confirmed" if ok else "rejected", g.me["username"], d["id"]))
    audit("collection_deposit_" + ("confirmed" if ok else "rejected"), d["collector_user"], "%.2f" % float(d["amount"]))
    commit()
    flash(("Got %.2f from %s." if ok else "Deposit of %.2f from %s turned down.") % (float(d["amount"]), d["collector_user"]),
          "success" if ok else "warn")
    return redirect(url_for("collections"))


# ===================================================================== for the hirer
def my_collectors():
    """Collectors the signed-in user may see: administrators every one; a reseller tier those they added. None = no access."""
    cols = "SELECT a.id, a.username, a.full_name, a.phone, a.is_active, p.username AS boss FROM panel_admins a " \
           "LEFT JOIN panel_admins p ON p.id = a.parent_id WHERE a.role = 'collector'"
    if g.me["role"] == "admin":
        return q(cols + " ORDER BY a.is_active DESC, a.username")
    if g.me["role"] in core.SELLER_ROLES:
        return q(cols + " AND a.parent_id = %s ORDER BY a.is_active DESC, a.username", (g.me["id"],))
    return None


@route("/collections", methods=["GET", "POST"])
def collections():
    people = my_collectors()
    if people is None:
        abort(403)
    if request.method == "POST":                 # limits: administrators only
        if g.me["role"] != "admin":
            abort(403)
        vals = {}
        for k in ("col_max_txn", "col_max_day"):
            try:
                v = round(float(request.form.get(k) or 0), 2)
                assert 0 < v < 10 ** 8
                vals[k] = v
            except (ValueError, AssertionError):
                flash("Enter both limits as amounts above zero.", "error")
                return redirect(url_for("collections"))
        for k, v in vals.items():
            x("INSERT INTO settings (skey, svalue) VALUES (%s, %s) ON DUPLICATE KEY UPDATE svalue = VALUES(svalue)", (k, "%.2f" % v))
        audit("collection_limits", "", "per collection %.2f, per day %.2f" % (vals["col_max_txn"], vals["col_max_day"]))
        commit()
        flash("Collection limits saved.", "success")
        return redirect(url_for("collections"))
    start, end = core.date_range(6)
    lo, hi = datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())
    ids = [p["id"] for p in people]
    for p in people:
        p.update(cash_in_hand(p["id"]))
    pick = request.args.get("collector", type=int)
    if pick is not None and pick not in ids:
        abort(403)
    rows, deps = [], []
    if ids:
        mark = ",".join(["%s"] * len(ids))
        rows = q("SELECT c.*, a.username AS collector_user, s.full_name FROM collections c JOIN panel_admins a ON a.id = c.collector_id "
                 "LEFT JOIN subscribers s ON s.id = c.subscriber_id WHERE c.collector_id IN (" + mark + ") "
                 "AND c.created_at >= %s AND c.created_at < %s ORDER BY c.id DESC LIMIT 1000", ids + [lo, hi])
        for p in people:                         # what each one took in the period
            mine = [r for r in rows if r["collector_id"] == p["id"] and r["status"] == "posted"]
            p["took"], p["n"] = round(sum(float(r["amount"]) for r in mine), 2), len(mine)
            p["last"] = mine[0]["created_at"] if mine else None
        if pick:
            rows = [r for r in rows if r["collector_id"] == pick]
        deps = q("SELECT d.*, a.username AS collector_user FROM collection_deposits d JOIN panel_admins a ON a.id = d.collector_id "
                 "WHERE d.collector_id IN (" + mark + ") AND (d.status = 'pending' OR d.created_at >= %s) "
                 "ORDER BY d.status = 'pending' DESC, d.id DESC LIMIT 100", ids + [lo])
    total = sum(float(r["amount"]) for r in rows if r["status"] == "posted")
    max_txn, max_day = caps()
    pending = [d for d in deps if d["status"] == "pending"]
    return render_template("collections.html", people=people, rows=rows, deps=deps, total=total, start=start, end=end,
                           methods=dict(METHODS), max_txn=max_txn, max_day=max_day, pick=pick, pending=pending,
                           waiting=round(sum(float(d["amount"]) for d in pending), 2),
                           in_hand=round(sum(p["hand"] for p in people), 2))


@route("/collections/<int:cid>/void", methods=["POST"])
def collection_void(cid):
    """Administrators only: void a posted collection and undo what it did - the renewal (as Revert Last Invoice: expiry
    back, the reseller's cost refunded), the charges it paid (unpaid again), the money it left in the balance - and drop
    its payments, so Payments and the reports no longer count it. Refused (nothing changes) when that is no longer safe."""
    if g.me["role"] != "admin":
        abort(403)
    c = q("SELECT * FROM collections WHERE id = %s", (cid,), one=True) or abort(404)
    reason = request.form.get("reason", "").strip()[:255]
    if c["status"] != "posted" or len(reason) < 3:
        flash("Write why the collection is void (a few words)." if c["status"] == "posted" else "Already void.", "error")
        return redirect(url_for("collections"))
    err = _undo(c)
    if err:
        get_db().rollback()
        flash("Receipt %s was not voided: %s Nothing was changed." % (c["receipt_no"], err), "error")
        return redirect(url_for("collections"))
    x("UPDATE collections SET status = 'void', void_by = %s, void_reason = %s, void_at = NOW() WHERE id = %s AND status = 'posted'",
      (g.me["username"], reason, cid))
    audit("collection_void", c["username"], "%s %.2f: %s" % (c["receipt_no"], float(c["amount"]), reason))
    commit()
    flash("Receipt %s is void and everything it did was undone." % c["receipt_no"], "success")
    return redirect(url_for("collections"))


def _undo(c):
    """Reverse one collection inside the open transaction. Returns '' or why it cannot be undone."""
    import mod_extras
    tag = r"receipt %s([^0-9]|$)" % re.escape(c["receipt_no"])
    sid = c["subscriber_id"]
    # 1. what it left in (or, paying a charge, took from) the balance
    net = float(q("SELECT COALESCE(SUM(amount), 0) AS t FROM sub_ledger WHERE subscriber_id = %s AND note REGEXP %s",
                  (sid, tag), one=True)["t"] or 0)
    if net:
        with get_db().cursor() as cur:
            cur.execute("UPDATE subscribers SET balance = balance - %s WHERE id = %s AND balance - %s >= 0", (net, sid, net))
            if cur.rowcount != 1:
                return "the %.2f it put into the customer's balance has already been spent." % net
            cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sid,))
            cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                        (sid, -net, float(cur.fetchone()["balance"]), "Void receipt %s" % c["receipt_no"], g.me["username"]))
    # 2. the charges it paid: unpaid again, their payments gone
    for p in q("SELECT id, note FROM payments WHERE subscriber_id = %s AND days_added = 0 AND note REGEXP %s", (sid, tag)):
        name = re.match(r"Extra fee: (.*?); ", p["note"] or "")
        if name:
            x("UPDATE sub_charges SET paid = 0, paid_at = NULL WHERE subscriber_id = %s AND name = %s AND paid = 1 "
              "ORDER BY paid_at DESC LIMIT 1", (sid, name.group(1)))
        x("DELETE FROM payments WHERE id = %s", (p["id"],))
    # 3. the renewal: only while it is still the customer's last one (as Revert Last Invoice)
    ren = q("SELECT id FROM payments WHERE subscriber_id = %s AND days_added > 0 AND note REGEXP %s", (sid, tag), one=True)
    if ren:
        last = mod_extras.revertable(sid)
        if not last or last["id"] != ren["id"] or last["why"]:
            return ("its renewal can no longer be undone (%s)." % (last["why"] if last and last["why"] and last["id"] == ren["id"]
                                                                   else "the customer was renewed or paid again after it"))
        sub = q("SELECT * FROM subscribers WHERE id = %s", (sid,), one=True)
        try:
            mod_extras.undo_renewal(sub, last)
        except ValueError as e:
            return str(e)
    return ""
