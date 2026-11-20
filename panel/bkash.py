"""bKash Tokenized Checkout (v1.2.0-beta): the one place the panel talks to bKash.

Used by the customer portal (renewals, mod_portal.py) and Add Balance (reseller top-ups, mod_topup.py).

Money rules:
  * A payment is credited only by settle_portal() / settle_topup(), and only by the UPDATE that flips the row
    from 'pending' to 'completed' (the database row lock makes that happen once, even when the callback and the
    background check arrive together). The renewal or balance change runs in the same transaction.
  * confirm() never guesses. When execute times out or is refused, it asks bKash for the payment's status.
    When bKash cannot be reached at all, the row stays 'pending' and sweep() asks again later.
  * Every request and answer is written to gateway_logs, with secrets and tokens removed."""
import json
import time
import urllib.error
import urllib.request

from flask import g

import core
import db

BASE = {"sandbox": "https://tokenized.sandbox.bka.sh/v1.2.0-beta/tokenized/checkout/",
        "live": "https://tokenized.pay.bka.sh/v1.2.0-beta/tokenized/checkout/"}
TIMEOUT = 25                 # seconds for one bKash request
TOKEN_LIFE = 50 * 60         # bKash id_tokens last 60 minutes; reuse for 50
REFRESH_LIFE = 27 * 86400    # refresh_tokens last 28 days
SWEEP_AFTER = 15             # minutes: leave a payment alone while the customer may still be on the bKash page
SWEEP_EVERY = 10             # minutes between background checks of one payment
SWEEP_DAYS = 3               # stop asking after this; staff can still see the row
EXPIRE_WAITING = 60          # minutes: an unpaid ("Initiated") payment older than this is marked failed
SECRET_FIELDS = ("app_secret", "app_key", "password", "id_token", "refresh_token", "authorization")
TABLES = {"portal": "portal_payments", "topup": "topups"}


class BkashError(Exception):
    def __init__(self, message, lost=False):
        super().__init__(message)
        self.lost = lost      # True: the request may have reached bKash but its answer did not reach us


def configured():
    s = core.cfg()
    return s.get("bkash_on") == "1" and all(s.get(k) for k in ("bkash_key", "bkash_secret", "bkash_user", "bkash_pass"))


def fee_for(amount):
    s = core.cfg()
    try:
        fee = float(s.get("bkash_fee") or 0)
    except ValueError:
        fee = 0.0
    return round(amount * fee / 100.0 if s.get("bkash_fee_type") != "fixed" else fee, 2)


def base_url():
    return BASE["sandbox" if core.cfg().get("bkash_mode") == "sandbox" else "live"]


# ===================================================================== audit log
def redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in SECRET_FIELDS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _log(step, flow, ref, payment_id, http, ms, ok, body, answer, error=""):
    """Write one gateway call on its own connection, so the record survives even if the caller rolls back."""
    try:
        conn = db.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO gateway_logs (gateway, flow, ref_id, payment_id, step, http_status, ms, ok, "
                            "request, response, error) VALUES ('bkash', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (flow[:16], int(ref or 0), (payment_id or "")[:64], step[:32], http, ms, 1 if ok else 0,
                             json.dumps(redact(body or {}))[:8000], json.dumps(redact(answer or {}))[:8000], error[:255]))
            conn.commit()
        finally:
            conn.close()
    except Exception:          # the table may be missing until install.sh is re-run; never break a payment for a log
        pass


# ===================================================================== HTTP
def call(step, body, flow="", ref=0, payment_id="", token=None):
    """POST one Tokenized Checkout step and return bKash's JSON answer (a dict). Raises BkashError."""
    s = core.cfg()
    if step.startswith("token/"):
        headers = {"username": s["bkash_user"], "password": s["bkash_pass"]}
    else:
        headers = {"authorization": token, "x-app-key": s["bkash_key"]}
    headers.update({"Content-Type": "application/json", "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; ISP-RADIUS-Panel)"})
    req = urllib.request.Request(base_url() + step, data=json.dumps(body).encode(), method="POST", headers=headers)
    started = time.time()
    payment_id = payment_id or str(body.get("paymentID", ""))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode() or "{}"
            http = r.status
        try:
            answer = json.loads(raw)
        except ValueError:
            answer = {"_raw": raw[:500]}
        ms = int((time.time() - started) * 1000)
        ok = str(answer.get("statusCode", "0000")) == "0000"
        _log(step, flow, ref, payment_id or str(answer.get("paymentID", "")), http, ms, ok, body, answer)
        if "_raw" in answer:
            raise BkashError("bKash sent an answer that is not JSON.", lost=True)
        return answer
    except urllib.error.HTTPError as e:
        ms = int((time.time() - started) * 1000)
        try:
            answer = json.loads(e.read().decode() or "{}")
        except (ValueError, OSError):
            answer = {}
        detail = " ".join(str(v) for v in answer.values() if v and not isinstance(v, (dict, list)))[:160]
        _log(step, flow, ref, payment_id, e.code, ms, False, body, answer, "HTTP %s" % e.code)
        hint = ""
        if e.code in (401, 403):
            drop_token()
            hint = (" Common causes: Live mode with test credentials (or the reverse), a wrong Key/Secret/Username/"
                    "Password, or this server's IP address is not whitelisted by bKash.")
        raise BkashError("bKash refused the %s request (HTTP %s)%s.%s" % (step, e.code, (": " + detail) if detail else "", hint),
                         lost=e.code >= 500)
    except (urllib.error.URLError, OSError) as e:
        ms = int((time.time() - started) * 1000)
        _log(step, flow, ref, payment_id, None, ms, False, body, {}, "no answer: %s" % e)
        raise BkashError("Could not reach bKash. Try again in a moment.", lost=True)


# ===================================================================== token
def _cache():
    row = core.q("SELECT svalue FROM settings WHERE skey = 'bkash_token_cache'", one=True)
    try:
        return json.loads(row["svalue"]) if row else {}
    except (ValueError, TypeError):
        return {}


def drop_token():
    try:
        core.x("DELETE FROM settings WHERE skey = 'bkash_token_cache'")
        core.commit()
    except Exception:
        pass


def token():
    """An id_token: the cached one for 50 minutes, then a refresh with refresh_token, then a new grant."""
    s = core.cfg()
    sig = "%s|%s|%s" % (s.get("bkash_mode"), s.get("bkash_key"), s.get("bkash_user"))
    cached, now = _cache(), time.time()
    if cached.get("sig") != sig:
        cached = {}
    if cached.get("token") and cached.get("exp", 0) > now:
        return cached["token"]
    keys = {"app_key": s["bkash_key"], "app_secret": s["bkash_secret"]}
    data = None
    if cached.get("refresh") and cached.get("rexp", 0) > now:
        try:
            data = call("token/refresh", dict(keys, refresh_token=cached["refresh"]), flow="token")
        except BkashError:
            data = None
        if data and not data.get("id_token"):
            data = None
    fresh_grant = data is None
    if fresh_grant:
        data = call("token/grant", keys, flow="token")
    if not data.get("id_token"):
        reply = json.dumps({k: v for k, v in data.items() if "token" not in k.lower()})[:220]
        raise BkashError("bKash did not give a token. Its reply was: %s" % reply)
    new = {"sig": sig, "token": data["id_token"], "exp": now + TOKEN_LIFE,
           "refresh": data.get("refresh_token") or cached.get("refresh", ""),
           "rexp": (now + REFRESH_LIFE) if (fresh_grant or data.get("refresh_token")) else cached.get("rexp", 0)}
    with core.get_db().cursor() as cur:
        db.save_setting(cur, "bkash_token_cache", json.dumps(new))
    core.commit()
    return data["id_token"]


# ===================================================================== payment steps
def create(flow, ref, amount, payer, callback, invoice):
    """Start a payment. Returns bKash's answer, which always has paymentID and bkashURL. Raises BkashError."""
    data = call("create", {"mode": "0011", "payerReference": str(payer)[:50], "callbackURL": callback,
                           "amount": "%.2f" % amount, "currency": "BDT", "intent": "sale",
                           "merchantInvoiceNumber": invoice}, flow=flow, ref=ref, token=token())
    if not data.get("bkashURL") or not data.get("paymentID"):
        raise BkashError(str(data.get("statusMessage") or data.get("errorMessage") or "bKash did not start the payment."))
    return data


def query(payment_id, flow="", ref=0):
    return call("payment/status", {"paymentID": payment_id}, flow=flow, ref=ref, token=token())


def _msg(data, default):
    return str((data or {}).get("statusMessage") or (data or {}).get("errorMessage") or default)


def confirm(payment_id, flow="", ref=0):
    """Finish a payment the customer approved. Returns (state, data):
    'completed' (money taken, data has trxID and amount), 'failed' (nothing taken), 'waiting' (the customer has not
    approved it yet) or 'unknown' (bKash could not be asked; try again later)."""
    answer, lost = None, False
    try:
        answer = call("execute", {"paymentID": payment_id}, flow=flow, ref=ref, token=token())
        if str(answer.get("statusCode")) == "0000" and str(answer.get("transactionStatus", "")) == "Completed" \
                and _paid(answer) > 0:
            return "completed", answer
        # Completed but without an amount: fall through and ask for the status (which carries the
        # amount) instead of crediting a payment whose amount was never verified.
    except BkashError:
        lost = True          # timeout or HTTP error: execute may or may not have gone through
    # already executed, refused, or the answer was lost: ask bKash what state the payment is in
    try:
        st = query(payment_id, flow, ref)
    except BkashError:
        return "unknown", answer or {}
    ts = str(st.get("transactionStatus", ""))
    if str(st.get("statusCode")) == "0000" and ts == "Completed":
        return "completed", st
    if ts in ("Initiated", "Authorized", "Pending Authorized"):
        if answer is not None and not lost:          # execute gave a clear refusal (wrong PIN, low balance, ...)
            return "failed", dict(st, statusMessage=_msg(answer, "bKash refused the payment."))
        return ("unknown" if lost else "waiting"), st
    if not ts and str(st.get("statusCode")) != "0000" and lost:
        return "unknown", st
    return "failed", dict(st, statusMessage=_msg(st, _msg(answer, "bKash did not complete the payment.")))


# ===================================================================== crediting (once)
def _paid(data):
    try:
        return float((data or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def mark(flow, row_id, status, message):
    """Close a pending row without crediting anything (only while it is still pending)."""
    core.x("UPDATE %s SET status = %%s, message = %%s, checked_at = NOW() WHERE id = %%s AND status = 'pending'" % TABLES[flow],
           (status, str(message)[:255], row_id))
    core.commit()


def _amount_ok(flow, row, data):
    """True only when bKash reports paying exactly amount + fee. Fails closed: a missing, unreadable
    or zero amount marks the row failed for a manual check instead of crediting an unverified payment."""
    want = float(row["amount"]) + float(row["fee"])
    paid = _paid(data)
    if paid <= 0:
        mark(flow, row["id"], "failed", "BKASH CONFIRMED THIS PAYMENT WITHOUT AN AMOUNT (trx %s, %.2f WAS EXPECTED): "
             "check this payment by hand." % ((data or {}).get("trxID", "?"), want))
        core.audit("bkash_amount_missing", row.get("payment_id", ""), "no amount in bKash answer, expected %.2f" % want, who="bkash")
        core.commit()
        return False
    if abs(paid - want) > 0.01:
        mark(flow, row["id"], "failed", "PAID %.2f BUT %.2f WAS EXPECTED (trx %s): check this payment by hand."
             % (paid, want, data.get("trxID", "?")))
        core.audit("bkash_amount_mismatch", row.get("payment_id", ""), "paid %.2f expected %.2f" % (paid, want), who="bkash")
        core.commit()
        return False
    return True


def settle_portal(row, data):
    """Renew the customer for a completed portal payment. Returns True when this call did the renewal."""
    import mod_subs
    if not _amount_ok("portal", row, data):
        return False
    trx = str(data.get("trxID", ""))[:64]
    with core.get_db().cursor() as cur:
        cur.execute("UPDATE portal_payments SET status = 'completed', trx_id = %s, message = '', checked_at = NOW() "
                    "WHERE id = %s AND status = 'pending'", (trx, row["id"]))
        first = cur.rowcount == 1
    if not first:
        core.commit()
        return False
    sub = core.q("SELECT * FROM subscribers WHERE id = %s", (row["subscriber_id"],), one=True)
    old_me = getattr(g, "me", None)
    g.me = {"username": "portal:" + sub["username"]}          # written as "received by"
    try:
        mod_subs.do_renew(sub, int(row["days"]), float(row["amount"]), "bkash", ("bKash %s" % trx)[:255])
        core.x("UPDATE portal_payments SET payment_ref = %s WHERE id = %s", (g.get("last_payment"), row["id"]))
        core.audit("portal_payment", sub["username"], "%.2f bKash %s" % (float(row["amount"]), trx), who=sub["username"])
    finally:
        if old_me is None:
            g.pop("me", None)
        else:
            g.me = old_me
    core.commit()
    return True


def settle_topup(row, data):
    """Add a completed top-up to the seller's balance. Returns True when this call added it."""
    if not _amount_ok("topup", row, data):
        return False
    trx = str(data.get("trxID", ""))[:64]
    with core.get_db().cursor() as cur:
        cur.execute("UPDATE topups SET status = 'completed', trx_id = %s, message = '', checked_at = NOW() "
                    "WHERE id = %s AND status = 'pending'", (trx, row["id"]))
        if cur.rowcount != 1:
            core.commit()
            return False
        cur.execute("UPDATE panel_admins SET balance = balance + %s WHERE id = %s", (row["amount"], row["user_id"]))
        cur.execute("SELECT username, balance FROM panel_admins WHERE id = %s", (row["user_id"],))
        who = cur.fetchone() or {"username": "?", "balance": 0}
        cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                    "VALUES (%s, %s, %s, 'bkash', %s, %s)",
                    (row["user_id"], row["amount"], who["balance"], ("bKash %s %s" % (trx, row["note"])).strip()[:255],
                     who["username"]))
    core.audit("balance_topup", who["username"], "%.2f bKash %s" % (float(row["amount"]), trx), who=who["username"])
    core.commit()
    return True


SETTLE = {"portal": settle_portal, "topup": settle_topup}


def check(flow, row):
    """Confirm one pending row with bKash and credit it if paid. Returns the row's state afterwards:
    'completed', 'failed', 'waiting' or 'unknown'."""
    if row["status"] != "pending":
        return row["status"]
    if not row.get("payment_id"):
        return "unknown"
    state, data = confirm(row["payment_id"], flow, row["id"])
    if state == "completed":
        SETTLE[flow](row, data)
        fresh = core.q("SELECT status FROM %s WHERE id = %%s" % TABLES[flow], (row["id"],), one=True)
        return fresh["status"] if fresh else "completed"
    if state == "failed":
        mark(flow, row["id"], "failed", _msg(data, "bKash did not complete the payment."))
        return "failed"
    msg = ("Waiting for bKash to confirm." if state == "unknown" else "Not paid on bKash yet.")
    core.x("UPDATE %s SET checked_at = NOW(), message = %%s WHERE id = %%s AND status = 'pending'" % TABLES[flow], (msg, row["id"]))
    core.commit()
    return state


def sweep(limit=20):
    """Background check of payments whose callback never arrived or could not be confirmed (run by maintenance.py
    inside a Flask request context). Returns how many rows changed state."""
    try:                       # keep 400 days of gateway history (disputes can come months later)
        core.x("DELETE FROM gateway_logs WHERE created_at < NOW() - INTERVAL 400 DAY LIMIT 5000")
        core.commit()
    except Exception:
        core.get_db().rollback()
    if not configured():
        return 0
    changed = 0
    for flow, table in TABLES.items():
        rows = core.q("SELECT * FROM %s WHERE status = 'pending' AND created_at < NOW() - INTERVAL %d MINUTE "
                      "AND created_at > NOW() - INTERVAL %d DAY "
                      "AND (checked_at IS NULL OR checked_at < NOW() - INTERVAL %d MINUTE) ORDER BY id LIMIT %d"
                      % (table, SWEEP_AFTER, SWEEP_DAYS, SWEEP_EVERY, limit))
        for row in rows:
            if not row.get("payment_id"):          # bKash never started it, so nothing can have been paid
                mark(flow, row["id"], "failed", "bKash never started this payment.")
                changed += 1
                continue
            try:
                state = check(flow, row)
            except Exception as exc:               # one broken row must not stop the others
                core.get_db().rollback()
                print("bKash check of %s #%s failed: %s" % (table, row["id"], exc))
                continue
            if state == "waiting":
                age = core.q("SELECT TIMESTAMPDIFF(MINUTE, created_at, NOW()) AS m FROM %s WHERE id = %%s" % table,
                             (row["id"],), one=True)
                if age and age["m"] >= EXPIRE_WAITING:
                    mark(flow, row["id"], "failed", "The customer did not finish paying on bKash.")
                    state = "failed"
            if state in ("completed", "failed"):
                changed += 1
    return changed
