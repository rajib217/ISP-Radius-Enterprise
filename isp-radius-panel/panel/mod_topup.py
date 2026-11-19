"""Reseller balance top-up through bKash (Tokenized Checkout).

Flow: Add Balance form -> grant token -> create payment -> browser goes to bKash -> bKash returns to
/bkash/callback/<id>/<key> -> execute payment -> only then is the balance credited (once)."""
import json
import secrets
import time
import urllib.error
import urllib.request

from flask import abort, flash, g, redirect, render_template, request, url_for

import core
import db
from core import audit, commit, insert, q, route, x

BASE = {"sandbox": "https://tokenized.sandbox.bka.sh/v1.2.0-beta/tokenized/checkout/",
        "live": "https://tokenized.pay.bka.sh/v1.2.0-beta/tokenized/checkout/"}
MAX_AMOUNT = 500000


class BkashError(Exception):
    pass


def _post(url, headers, body):
    step = url.rstrip("/").split("/")[-1]
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else b"", method="POST",
                                 headers=dict(headers, **{"Content-Type": "application/json", "Accept": "application/json",
                                                          "User-Agent": "Mozilla/5.0 (compatible; ISP-RADIUS-Panel)"}))
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = " ".join(str(v) for v in json.loads(e.read().decode()).values() if v and not isinstance(v, (dict, list)))[:160]
        except (ValueError, OSError):
            detail = ""
        hint = (" Common causes: Live mode with test credentials (or the reverse), a wrong Key/Secret/Username/Password, "
                "or this server's IP address is not whitelisted by bKash.") if e.code in (401, 403) else ""
        if e.code in (401, 403):
            try:
                x("DELETE FROM settings WHERE skey = 'bkash_token_cache'")
                commit()
            except Exception:
                pass
        raise BkashError("bKash refused the %s request (HTTP %s)%s.%s" % (step, e.code, (": " + detail) if detail else "", hint))
    except (urllib.error.URLError, OSError, ValueError):
        raise BkashError("Could not reach bKash. Try again in a moment.")


def configured():
    s = core.cfg()
    return s.get("bkash_on") == "1" and all(s.get(k) for k in ("bkash_key", "bkash_secret", "bkash_user", "bkash_pass"))


def _token():
    """Grant a token, and reuse it for up to 50 minutes (bKash limits how often tokens may be granted)."""
    s = core.cfg()
    base = BASE["sandbox" if s.get("bkash_mode") == "sandbox" else "live"]
    sig = "%s|%s|%s" % (s.get("bkash_mode"), s.get("bkash_key"), s.get("bkash_user"))
    row = q("SELECT svalue FROM settings WHERE skey = 'bkash_token_cache'", one=True)
    try:
        cached = json.loads(row["svalue"]) if row else {}
    except (ValueError, TypeError):
        cached = {}
    if cached.get("sig") == sig and cached.get("token") and cached.get("exp", 0) > time.time():
        return base, cached["token"]
    data = _post(base + "token/grant", {"username": s["bkash_user"], "password": s["bkash_pass"]},
                 {"app_key": s["bkash_key"], "app_secret": s["bkash_secret"]})
    if not data.get("id_token"):
        reply = json.dumps({k: v for k, v in data.items() if "token" not in k.lower()})[:220]
        raise BkashError("bKash did not give a token. Its reply was: %s" % reply)
    with core.get_db().cursor() as cur:
        db.save_setting(cur, "bkash_token_cache", json.dumps({"sig": sig, "token": data["id_token"], "exp": time.time() + 3000}))
    commit()
    return base, data["id_token"]


def _drop_token():
    x("DELETE FROM settings WHERE skey = 'bkash_token_cache'")
    commit()


def fee_for(amount):
    s = core.cfg()
    try:
        fee = float(s.get("bkash_fee") or 0)
    except ValueError:
        fee = 0.0
    return round(amount * fee / 100.0 if s.get("bkash_fee_type") != "fixed" else fee, 2)


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
            base, id_token = _token()
            # No "?" in the address: bKash appends ?paymentID=..&status=.. itself, and a second "?" would break it.
            callback = url_for("bkash_callback", tid=tid, key=token)
            cb_base = (core.cfg().get("bkash_callback_base") or "").rstrip("/")
            callback = (cb_base + callback) if cb_base else url_for("bkash_callback", _external=True, tid=tid, key=token)
            data = _post(base + "create", {"authorization": id_token, "x-app-key": core.cfg()["bkash_key"]},
                         {"mode": "0011", "payerReference": g.me["username"][:50], "callbackURL": callback,
                          "amount": "%.2f" % (amount + fee), "currency": "BDT", "intent": "sale",
                          "merchantInvoiceNumber": "TOP%d" % tid})
            if not data.get("bkashURL") or not data.get("paymentID"):
                raise BkashError(str(data.get("statusMessage") or "bKash did not start the payment."))
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
        new = "cancelled" if status == "cancel" else "failed"
        x("UPDATE topups SET status = %s, message = %s WHERE id = %s AND status = 'pending'",
          (new, "bKash returned: %s" % (status or "no status"), row["id"]))
        commit()
        flash("The bKash payment was %s. Nothing was added to your balance." % ("cancelled" if new == "cancelled" else "not completed"), "error")
        return redirect(url_for("add_balance"))
    try:
        base, id_token = _token()
        hdr = {"authorization": id_token, "x-app-key": core.cfg()["bkash_key"]}
        data = _post(base + "execute", hdr, {"paymentID": row["payment_id"]})
        if str(data.get("statusCode", "")) != "0000":
            # already executed, or the answer was lost: ask bKash what the payment's state is
            data = _post(base + "payment/status", hdr, {"paymentID": row["payment_id"]})
    except BkashError as e:
        flash("%s If money left your bKash, contact your administrator with payment ID %s." % (e, row["payment_id"]), "error")
        return redirect(url_for("add_balance"))
    ok = str(data.get("statusCode", "")) == "0000" and str(data.get("transactionStatus", "Completed")) == "Completed"
    try:
        paid = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        paid = 0.0
    if ok and paid and abs(paid - (float(row["amount"]) + float(row["fee"]))) > 0.01:
        ok = False
        data["statusMessage"] = "The paid amount did not match."
    if not ok:
        x("UPDATE topups SET status = 'failed', message = %s WHERE id = %s AND status = 'pending'",
          (str(data.get("statusMessage") or "bKash did not confirm the payment.")[:255], row["id"]))
        commit()
        flash("bKash did not confirm the payment: %s" % (data.get("statusMessage") or "unknown reason"), "error")
        return redirect(url_for("add_balance"))
    trx = str(data.get("trxID", ""))[:64]
    # credit exactly once: only the request that flips pending -> completed adds the money
    with core.get_db().cursor() as cur:
        cur.execute("UPDATE topups SET status = 'completed', trx_id = %s, message = '' WHERE id = %s AND status = 'pending'",
                    (trx, row["id"]))
        if cur.rowcount == 1:
            cur.execute("UPDATE panel_admins SET balance = balance + %s WHERE id = %s", (row["amount"], g.me["id"]))
            cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (g.me["id"],))
            after = cur.fetchone()
            cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                        "VALUES (%s, %s, %s, 'bkash', %s, %s)",
                        (g.me["id"], row["amount"], list(after.values())[0] if after else 0,
                         ("bKash %s %s" % (trx, row["note"])).strip()[:255], g.me["username"]))
    audit("balance_topup", g.me["username"], "%.2f bKash %s" % (float(row["amount"]), trx))
    commit()
    flash("Payment received. %s %.2f was added to your balance." % (core.cfg().get("currency", "BDT"), float(row["amount"])), "success")
    return redirect(url_for("add_balance"))
