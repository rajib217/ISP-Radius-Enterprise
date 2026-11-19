"""Reseller balance top-up through bKash (Tokenized Checkout, see bkash.py).

Flow: Add Balance form -> create payment -> browser goes to bKash -> bKash returns to
/bkash/callback/<id>/<key> -> execute payment -> only then is the balance credited (once).
A payment whose answer was lost is confirmed later by bkash.sweep() (maintenance timer)."""
import secrets

from flask import abort, flash, g, redirect, render_template, request, url_for

import bkash
import core
from bkash import BkashError, configured, fee_for  # noqa: F401  (older code imports these from here)
from core import commit, insert, q, route, x

MAX_AMOUNT = 500000


def callback_url(endpoint, **args):
    """bKash appends ?paymentID=..&status=.. itself, so the address must not carry its own "?"."""
    cb_base = (core.cfg().get("bkash_callback_base") or "").rstrip("/")
    return (cb_base + url_for(endpoint, **args)) if cb_base else url_for(endpoint, _external=True, **args)


@route("/add-balance", methods=["GET", "POST"])
def add_balance():
    if request.method == "POST":
        f = request.form
        try:
            amount = round(float(f.get("amount", "")), 2)
            assert 0 < amount <= MAX_AMOUNT
        except (ValueError, AssertionError):
            flash("Enter an amount between 1 and %s." % format(MAX_AMOUNT, ","), "error")
            return redirect(url_for("add_balance"))
        if f.get("method") != "bkash":
            flash("Select a payment method.", "error")
            return redirect(url_for("add_balance"))
        if not configured():
            flash("bKash is not switched on yet. Ask your administrator to set it up.", "error")
            return redirect(url_for("add_balance"))
        fee = fee_for(amount)
        token = secrets.token_hex(16)
        tid = insert("INSERT INTO topups (user_id, amount, fee, method, note, token) VALUES (%s, %s, %s, 'bkash', %s, %s)",
                     (g.me["id"], amount, fee, f.get("note", "").strip()[:255], token))
        commit()
        callback = "-"
        try:
            callback = callback_url("bkash_callback", tid=tid, key=token)
            data = bkash.create("topup", tid, amount + fee, g.me["username"], callback, "TOP%d" % tid)
        except BkashError as e:
            e = BkashError("%s [mode: %s, callback: %s]" % (e, core.cfg().get("bkash_mode"), callback))
            x("UPDATE topups SET status = 'failed', message = %s WHERE id = %s", (str(e)[:255], tid))
            commit()
            flash(str(e), "error")
            return redirect(url_for("add_balance"))
        x("UPDATE topups SET payment_id = %s WHERE id = %s", (data["paymentID"], tid))
        commit()
        return redirect(data["bkashURL"])
    s = core.cfg()
    history = q("SELECT amount, fee, status, trx_id, message, created_at FROM topups WHERE user_id = %s ORDER BY id DESC LIMIT 20",
                (g.me["id"],))
    return render_template("add_balance.html", history=history, ready=configured(), s=s)


@route("/bkash/callback")                          # older payments that were started with ?t=..&k=..
@route("/bkash/callback/<int:tid>/<key>")
def bkash_callback(tid=None, key=None):
    if tid is None:
        tid, key = request.args.get("t", ""), request.args.get("k", "")
    tid = str(tid)
    row = q("SELECT * FROM topups WHERE id = %s AND user_id = %s", (int(tid), g.me["id"]), one=True) if tid.isdigit() else None
    if not row or not secrets.compare_digest(str(row["token"]), str(key or "")):
        abort(404)
    if row["status"] != "pending":
        flash("This payment was already processed.", "info")
        return redirect(url_for("add_balance"))
    status = request.args.get("status", "")
    if status != "success" or request.args.get("paymentID", "") != row["payment_id"]:
        bkash.mark("topup", row["id"], "cancelled" if status == "cancel" else "failed", "bKash returned: %s" % (status or "no status"))
        flash("The bKash payment was %s. Nothing was added to your balance." % ("cancelled" if status == "cancel" else "not completed"), "error")
        return redirect(url_for("add_balance"))
    state = bkash.check("topup", row)
    if state == "completed":
        flash("Payment received. %s %.2f was added to your balance." % (core.cfg().get("currency", "BDT"), float(row["amount"])), "success")
    elif state == "failed":
        fresh = q("SELECT message FROM topups WHERE id = %s", (row["id"],), one=True)
        flash("bKash did not confirm the payment: %s" % ((fresh or {}).get("message") or "unknown reason"), "error")
    else:
        flash("bKash has not confirmed the payment yet. If money left your bKash, it is added to your balance automatically "
              "within about 15 minutes (payment ID %s)." % row["payment_id"], "info")
    return redirect(url_for("add_balance"))
