"""bKash SMS auto-renew: an Android phone that holds the office bKash number forwards bKash's "You have received ..."
SMS to the panel (any SMS-forwarder app that can POST to a web address), and the panel renews the customer.

  Settings > bKash SMS  (/settings/bkash-sms)     switch, allowed senders, the forwarder address, the received
                                                   payments and the ones waiting for a hand match; the USB GSM
                                                   modem (SIM800C) on the server and its state (gsmd.py, gsmlib.py)
  /sms-in/<token>  (POST, no sign-in)              where the phone sends the SMS; the secret token is the only
                                                   thing that keeps others from faking payments

A message is used only when it came from an allowed sender (bKash), reads like a received payment and has a TrxID
not seen before (each TrxID is used once). The customer is found from:
  1. the Reference: their subscriber ID (the ID column of the subscriber list, shown in the portal too; "125", "#125"
     or "ID 125"), else their username or mobile number
  2. the payer's mobile number, when it is complete and belongs to exactly one customer
A masked number (0171XXXX583) is only a suggestion: thousands of numbers fit it, so it waits for one click.

Then, when the money plus the customer's balance covers the package price, the package is renewed (extra goes into
their balance, a shortfall comes out of it, as in the Activation window); otherwise the money goes into their balance.
A reseller's customer: the money is credited to the reseller first, and the reseller's package cost is taken from it,
as with a portal bKash payment. Suspended customers are never switched back on by a payment: it goes to the balance.
Nothing else in the SMS is stored; other SMS the phone forwards are dropped."""
import re
import secrets
from datetime import datetime

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

import core
from core import admin_required, audit, cfg, commit, get_db, q, route

KEYS = ("bksms_on", "bksms_token", "bksms_senders")
DEFAULT_SENDERS = "bKash"
TEXT_KEYS = ("text", "message", "msg", "body", "content", "sms", "smstext", "sms_text", "messagebody")
FROM_KEYS = ("from", "sender", "address", "phone", "number", "originatingaddress", "from_number", "msisdn")

RX_PAID = re.compile(r"received(?:\s+payment)?\s+Tk\.?\s*([\d,]+(?:\.\d{1,2})?)\s+from\s+([0-9Xx*+\-]{6,20})\.?"
                     r"(?:\s+Ref\.?\s*(.*?))?\.?\s+Fee\s+Tk", re.I | re.S)
RX_TRX = re.compile(r"\bTrxID\s*:?\s*([A-Z0-9]{6,20})\b", re.I)
RX_AT = re.compile(r"\bat\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2})")
RX_FROM_LINE = re.compile(r"^\s*From\s*:\s*(\S+)", re.I | re.M)
STATES = {"renewed": ("ok", "Renewed"), "balance": ("info", "Added to balance"), "unmatched": ("warn", "Needs a match"),
          "ignored": ("mute", "Ignored"), "working": ("mute", "Working")}


# --------------------------------------------------------------------------------------------- reading the SMS
def parse(text):
    """{'amount', 'payer', 'ref', 'trx', 'paid_at'} for a bKash "received" SMS, or None."""
    text = (text or "").strip()
    m, t = RX_PAID.search(text), RX_TRX.search(text)
    if not m or not t:
        return None
    try:
        amount = round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None
    if not 0 < amount < 10 ** 7:
        return None
    paid_at = None
    a = RX_AT.search(text)
    if a:
        try:
            paid_at = datetime.strptime("%s %s" % a.groups(), "%d/%m/%Y %H:%M")
        except ValueError:
            paid_at = None
    ref = re.sub(r"\s+", " ", (m.group(3) or "")).strip(" .")
    return {"amount": amount, "payer": m.group(2).strip(".-"), "ref": ref[:64], "trx": t.group(1).upper(),
            "paid_at": paid_at}


def _digits10(phone):
    """The last 10 digits of a Bangladeshi number (masked digits kept as X), or ""."""
    d = re.sub(r"[^0-9Xx*]", "", phone or "").upper().replace("*", "X")
    return d[-10:] if len(d) >= 10 else ""


_PHONE_SQL = ("SELECT * FROM subscribers WHERE RIGHT(REPLACE(REPLACE(REPLACE(REPLACE(phone, ' ', ''), '-', ''), '+', ''), "
              "'.', ''), 10) LIKE %s ORDER BY id LIMIT 3")


def _by_phone(phone):
    """(subscriber or None, masked?) for a number; only a single exact owner counts."""
    d = _digits10(phone)
    if not d:
        return None, False
    masked = "X" in d
    rows = q(_PHONE_SQL, (d.replace("X", "_"),))
    return (rows[0] if len(rows) == 1 else None), masked


def find(p):
    """(subscriber, how, sure) for a parsed SMS; sure=False is only a suggestion. (None, why, False) when none."""
    ref = p["ref"]
    sid = re.fullmatch(r"(?:#|ID\s*[:#.-]?\s*)?(\d{1,9})", ref.strip(), re.I) if ref else None
    if sid:
        sub = q("SELECT * FROM subscribers WHERE id = %s", (int(sid.group(1)),), one=True)
        other = q("SELECT id FROM subscribers WHERE username = %s", (sid.group(1),), one=True)
        if sub and other and other["id"] != sub["id"]:
            return sub, "Reference %s is one customer's ID and another's username" % sid.group(1), False
        if sub:
            return sub, "Reference is the subscriber ID", True
    if ref:
        for name in dict.fromkeys((ref, ref.replace(" ", ""))):
            sub = q("SELECT * FROM subscribers WHERE username = %s", (name,), one=True)
            if sub:
                return sub, "Reference is the username", True
        if re.fullmatch(r"[+\d\s-]{10,16}", ref):
            sub, masked = _by_phone(ref)
            if sub and not masked:
                return sub, "Reference is the customer's mobile", True
    sub, masked = _by_phone(p["payer"])
    if sub and not masked:
        return sub, "paid from the customer's mobile", True
    if sub:
        return sub, "the masked number fits this customer's mobile", False
    return None, "no customer has this reference or mobile", False


# --------------------------------------------------------------------------------------------- crediting
def _credit_seller(cur, sub, amount, trx, by):
    owner = q("SELECT id, username, role FROM panel_admins WHERE username = %s", (sub.get("created_by") or "",), one=True)
    if not owner or owner["role"] not in core.SELLER_ROLES:
        return
    cur.execute("UPDATE panel_admins SET balance = balance + %s WHERE id = %s", (amount, owner["id"]))
    cur.execute("SELECT balance FROM panel_admins WHERE id = %s", (owner["id"],))
    after = cur.fetchone()["balance"]
    cur.execute("INSERT INTO user_ledger (user_id, amount, balance_after, method, note, created_by) "
                "VALUES (%s, %s, %s, 'bkash', %s, %s)",
                (owner["id"], amount, after, ("Customer %s paid by bKash (SMS), %s" % (sub["username"], trx))[:255], by[:64]))


def _to_balance(sub, amount, trx, by, why):
    with get_db().cursor() as cur:
        _credit_seller(cur, sub, amount, trx, by)
        cur.execute("UPDATE subscribers SET balance = balance + %s WHERE id = %s", (amount, sub["id"]))
        cur.execute("SELECT balance FROM subscribers WHERE id = %s", (sub["id"],))
        after = float(cur.fetchone()["balance"])
        cur.execute("INSERT INTO sub_ledger (subscriber_id, amount, balance_after, note, created_by) VALUES (%s, %s, %s, %s, %s)",
                    (sub["id"], amount, after, ("bKash %s (SMS)" % trx)[:255], by[:64]))
        try:
            import appnotify
            appnotify.balance(cur, sub["id"], amount, after, cfg().get("currency"), "bksms-" + trx, by)
        except Exception:          # the notice is a courtesy; the money must still be booked
            pass
    return "balance", "%.2f added to the balance (now %.2f): %s" % (amount, after, why)


def settle(row, sub, by):
    """Book one received payment for this customer. Commits. Returns (status, message)."""
    import mod_subs
    amount, trx = float(row["amount"]), row["trx_id"]
    old_me = g.get("me")
    g.me = {"username": by[:64], "role": "staff", "id": 0}       # "received by"; never a seller
    try:
        sub = q("SELECT * FROM subscribers WHERE id = %s", (sub["id"],), one=True)
        plan = mod_subs._plan(sub["plan_id"]) if sub.get("plan_id") else None
        status, msg = None, ""
        if not plan:
            why = "the customer has no package"
        elif sub["status"] == "suspended":
            why = "the customer is suspended, so the payment does not switch them back on"
        else:
            price = mod_subs.customer_price(sub, plan)
            have = round(float(sub["balance"] or 0), 2)
            why = "%.2f + balance %.2f is less than the package price %.2f" % (amount, have, price)
            if price > 0 and amount + have >= price - 0.001:
                with get_db().cursor() as cur:
                    _credit_seller(cur, sub, amount, trx, by)
                res = mod_subs.activate_one(sub, plan, int(plan["validity_days"] or 30), False, "bKash", amount,
                                            user_note=("bKash %s (SMS)" % trx), label="bKash SMS")
                if res["ok"]:
                    status = "renewed"
                    msg = "Renewed until %s, %.2f paid%s" % (res["expiry"].strftime("%d %b %Y"), res["price"],
                                                             (", balance %+.2f" % res["change"]) if res["change"] else "")
                else:
                    get_db().rollback()
                    why = res["error"].replace("Not activated: ", "").replace(" Nothing was changed.", "")
        if not status:
            status, msg = _to_balance(sub, amount, trx, by, why)
        core.x("UPDATE sms_payments SET status = %s, message = %s, subscriber_id = %s, username = %s, payment_id = %s, "
               "done_by = %s WHERE id = %s",
               (status, msg[:255], sub["id"], sub["username"], g.get("last_payment") if status == "renewed" else None,
                by[:64], row["id"]))
        audit("bkash_sms_" + status, sub["username"], "%.2f TrxID %s: %s" % (amount, trx, msg), who=by[:64])
        commit()
        return status, msg
    finally:
        g.pop("last_payment", None)
        if old_me is None:
            g.pop("me", None)
        else:
            g.me = old_me


# --------------------------------------------------------------------------------------------- the phone's webhook
def _incoming():
    """(text, sender) from whatever the forwarder app sent: JSON, a form, or the plain SMS as the body."""
    data = request.get_json(silent=True, force=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    flat = {}
    for k, v in (data or {}).items():
        if isinstance(v, dict):                      # {"data": {"text": ...}}
            flat.update({str(k2).lower(): v2 for k2, v2 in v.items() if not isinstance(v2, (dict, list))})
        elif not isinstance(v, list):
            flat[str(k).lower()] = v
    text = next((str(flat[k]) for k in TEXT_KEYS if flat.get(k)), "")
    sender = next((str(flat[k]) for k in FROM_KEYS if flat.get(k)), "")
    if not text:
        raw = request.get_data(as_text=True)[:4000]
        if "TrxID" in raw or "trxid" in raw.lower():
            text = raw
    if not sender:
        m = RX_FROM_LINE.search(text)
        sender = m.group(1) if m else ""
    return text[:2000], sender.strip()[:64]


def sender_ok(sender, allowed):
    names = [a.strip().lower() for a in (allowed or "").split(",") if a.strip()]
    return not names or (sender or "").strip().lower() in names


@route("/sms-in", methods=["POST"])
@route("/sms-in/<token>", methods=["POST"])
def sms_in(token=None):
    s = cfg()
    want = s.get("bksms_token") or ""
    sent = token or request.headers.get("X-Token", "") or request.headers.get("Authorization", "").replace("Bearer ", "", 1)
    if not want or not sent or not secrets.compare_digest(sent.strip(), want):
        abort(403)
    if s.get("bksms_on") != "1":
        return jsonify({"ok": False, "status": "off"})
    text, sender = _incoming()
    return jsonify(ingest(text, sender, "phone"))


def ingest(text, sender, source):
    """Use one SMS (from the phone's forwarder app, or from the USB modem through gsmd.py). Returns
    {'ok', 'status': off / ignored / duplicate / unmatched / renewed / balance, ...}. Needs a request context."""
    s = cfg()
    if s.get("bksms_on") != "1":
        return {"ok": False, "status": "off"}
    if not sender_ok(sender, s.get("bksms_senders", DEFAULT_SENDERS)):
        return {"ok": True, "status": "ignored", "why": "sender is not allowed"}
    p = parse(text)
    if not p:
        return {"ok": True, "status": "ignored", "why": "not a bKash received-payment SMS"}
    with get_db().cursor() as cur:
        cur.execute("INSERT IGNORE INTO sms_payments (trx_id, amount, payer, ref, sms_text, paid_at, status, source) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'working', %s)",
                    (p["trx"], p["amount"], p["payer"][:32], p["ref"], text[:500], p["paid_at"], source[:8]))
        new_id = cur.lastrowid if cur.rowcount == 1 else None
    commit()
    if not new_id:
        return {"ok": True, "status": "duplicate", "trx": p["trx"]}
    row = q("SELECT * FROM sms_payments WHERE id = %s", (new_id,), one=True)
    sub, how, sure = find(p)
    if not sure:
        core.x("UPDATE sms_payments SET status = 'unmatched', message = %s, subscriber_id = %s WHERE id = %s",
               (how[:255], sub["id"] if sub else None, new_id))
        audit("bkash_sms_unmatched", p["trx"], "%.2f from %s, Ref %s: %s" % (p["amount"], p["payer"], p["ref"], how),
              who="bkash-sms")
        commit()
        return {"ok": True, "status": "unmatched", "trx": p["trx"]}
    try:
        status, msg = settle(row, sub, "bkash-sms")
    except Exception as exc:
        get_db().rollback()
        core.x("UPDATE sms_payments SET status = 'unmatched', message = %s, subscriber_id = %s WHERE id = %s",
               (("Could not book it automatically (%s): %s" % (how, exc))[:255], sub["id"], new_id))
        commit()
        return {"ok": True, "status": "unmatched", "trx": p["trx"]}
    return {"ok": True, "status": status, "trx": p["trx"], "username": sub["username"]}


# --------------------------------------------------------------------------------------------- settings page
@route("/settings/bkash-sms", methods=["GET", "POST"])
@admin_required
def settings_bksms():
    s = cfg()
    if request.method == "POST" and request.form.get("part") == "gsm":
        return _save_gsm(request.form)
    if request.method == "POST":
        f = request.form
        import db
        token = s.get("bksms_token") or ""
        if f.get("new_token") == "1" or not token:
            token = secrets.token_urlsafe(24)
        with get_db().cursor() as cur:
            db.save_setting(cur, "bksms_on", "1" if f.get("bksms_on") == "1" else "0")
            db.save_setting(cur, "bksms_senders", (f.get("bksms_senders") or "").strip()[:200])
            db.save_setting(cur, "bksms_token", token)
        audit("settings_saved", "bKash SMS", ("on" if f.get("bksms_on") == "1" else "off") +
              (", new token" if f.get("new_token") == "1" else ""))
        commit()
        flash("Saved. A new address was made: put it in the phone's forwarder app, the old one no longer works."
              if f.get("new_token") == "1" else "Saved.", "success")
        return redirect(url_for("settings_bksms"))
    if request.args.get("show") in STATES:          # old links to the list, which is under Accounting now
        return redirect(url_for("bksms_payments", status=request.args["show"]))
    counts = {r["status"]: r["c"] for r in q("SELECT status, COUNT(*) AS c FROM sms_payments GROUP BY status")}
    token = s.get("bksms_token") or ""
    hook = url_for("sms_in", token=token, _external=True) if token else ""
    import gsmlib
    gsm = gsmlib.read_state()
    gsm["bars"] = gsmlib.bars(gsm.get("signal"))
    inbox = q("SELECT * FROM gsm_inbox ORDER BY id DESC LIMIT 20") if s.get("gsm_on") == "1" or gsm.get("sms_read") else []
    return render_template("settings_bksms.html", s=s, tab="bksms", counts=counts, hook=hook,
                           states=STATES, senders=s.get("bksms_senders", DEFAULT_SENDERS) if "bksms_senders" in s
                           else DEFAULT_SENDERS, gsm=gsm, ports=gsmlib.candidate_ports(), bauds=gsmlib.BAUDS, inbox=inbox,
                           gsm_port=s.get("gsm_port") or "auto", gsm_baud=int(s.get("gsm_baud") or 115200))


RX_PORT = re.compile(r"/dev/[A-Za-z0-9._/:+-]{1,120}")


def _save_gsm(f):
    """Settings > bKash SMS > USB GSM modem."""
    import db
    import gsmlib
    port = (f.get("gsm_port") or "auto").strip()
    if port != "auto" and (not RX_PORT.fullmatch(port) or ".." in port):
        flash("The port is a device such as /dev/ttyUSB0 or /dev/gsm-modem, or Auto. Nothing was saved.", "error")
        return redirect(url_for("settings_bksms"))
    try:
        baud = int(f.get("gsm_baud") or 115200)
        assert baud in gsmlib.BAUDS
    except (ValueError, AssertionError):
        baud = 115200
    on = "1" if f.get("gsm_on") == "1" else "0"
    with get_db().cursor() as cur:
        db.save_setting(cur, "gsm_on", on)
        db.save_setting(cur, "gsm_port", port)
        db.save_setting(cur, "gsm_baud", str(baud))
    audit("settings_saved", "GSM modem", "%s, %s at %d" % ("on" if on == "1" else "off", port, baud))
    commit()
    _ask_reconnect()
    flash(("Saved. The modem service connects within about 15 seconds; reload this page to see it."
           if on == "1" else "Saved. The modem is not read any more."), "success")
    return redirect(url_for("settings_bksms"))


def _ask_reconnect():
    import gsmlib
    try:
        with open(gsmlib.RECONNECT_FILE, "w") as fh:
            fh.write("1")
    except OSError:
        pass


@route("/settings/bkash-sms/modem/reconnect", methods=["POST"])
@admin_required
def bksms_modem_reconnect():
    _ask_reconnect()
    audit("gsm_reconnect", "GSM modem", "")
    flash("The modem service reconnects within about 15 seconds; reload this page to see it.", "success")
    return redirect(url_for("settings_bksms"))


def _row(pid, status="unmatched"):
    with get_db().cursor() as cur:
        cur.execute("UPDATE sms_payments SET status = 'working' WHERE id = %s AND status = %s", (pid, status))
        claimed = cur.rowcount == 1
    commit()
    return q("SELECT * FROM sms_payments WHERE id = %s", (pid,), one=True) if claimed else None


@route("/settings/bkash-sms/<int:pid>/apply", methods=["POST"])
@admin_required
def bksms_apply(pid):
    sub = q("SELECT * FROM subscribers WHERE username = %s", ((request.form.get("username") or "").strip(),), one=True)
    if not sub:
        flash("No customer has that username. Nothing was changed.", "error")
        return _back()
    row = _row(pid)
    if not row:
        flash("This payment was already booked or ignored.", "error")
        return _back()
    try:
        status, msg = settle(row, sub, g.me["username"])
    except Exception:
        get_db().rollback()
        core.x("UPDATE sms_payments SET status = 'unmatched' WHERE id = %s", (pid,))
        commit()
        raise
    flash("%s: %s" % (sub["username"], msg), "success")
    return _back()


@route("/settings/bkash-sms/<int:pid>/ignore", methods=["POST"])
@admin_required
def bksms_ignore(pid):
    row = _row(pid)
    if row:
        core.x("UPDATE sms_payments SET status = 'ignored', done_by = %s, message = %s WHERE id = %s",
               (g.me["username"], ("Ignored by %s" % g.me["username"])[:255], pid))
        audit("bkash_sms_ignored", row["trx_id"], "%.2f from %s" % (float(row["amount"]), row["payer"]))
        commit()
        flash("Ignored. TrxID %s will not be used again." % row["trx_id"], "success")
    return _back()


def _back():
    return redirect(core.safe_next(request.form.get("next")) or url_for("bksms_payments", status="unmatched"))
