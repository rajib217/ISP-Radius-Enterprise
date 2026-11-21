"""Expired notice: a customer whose package ran out stays connected at a tiny speed in the RADIUS group
"expired-notice" (MikroTik address list "expired"), and every plain-HTTP page they open shows a page saying the bill
is unpaid, with the office phone number and (optionally) a bKash button. Nothing else on the internet opens.

  Settings > Expired notice  (/settings/expired-notice)   switch it on, phone, message, speed; the MikroTik commands
  /expired-notice                                         the page itself (no sign-in). Caddy serves it on port 8099,
                                                          where the routers send the expired customers' web requests

The customer is recognised by the IP address of their live PPPoE session. When the router hides that address
(the panel server is on the internet and the router masquerades), the page shows the general message only.
HTTPS sites cannot be redirected without a browser warning; phones and laptops test the connection over plain HTTP
when they join, and that test opens this page ("Sign in to network")."""
import ipaddress
import socket
from datetime import date, datetime, timedelta

from flask import Response, flash, g, redirect, render_template, request, session, url_for

import core
import radius
from core import admin_required, audit, cfg, commit, get_db, q, route

NOTICE_PORT = 8099
BKASH_HOSTS = ("payment.bkash.com", "sandbox.payment.bkash.com", "pay.bka.sh", "scripts.pay.bka.sh",
               "tokenized.pay.bka.sh")
DEFAULT_MSG = "আপনার ইন্টারনেট প্যাকেজের মেয়াদ শেষ হয়েছে এবং বিল এখনো পরিশোধ করা হয়নি। পরিশোধ করলেই কয়েক মিনিটের মধ্যে আবার ইন্টারনেট চালু হবে।"


def _customer_by_ip(ip):
    """(subscriber, plan) of the live session on this IP, or (None, None)."""
    try:
        ipaddress.ip_address(ip or "")
    except ValueError:
        return None, None
    row = q("SELECT username FROM radacct WHERE framedipaddress = %s AND acctstoptime IS NULL "
            "ORDER BY radacctid DESC LIMIT 1", (ip,), one=True)
    if not row:
        return None, None
    sub = q("SELECT * FROM subscribers WHERE username = %s", (row["username"],), one=True)
    if not sub:
        return None, None
    plan = q("SELECT * FROM plans WHERE id = %s", (sub["plan_id"],), one=True) if sub.get("plan_id") else None
    return sub, plan


def _pay_url(s):
    """The customer portal's sign-in page, when bKash renewals are possible from there."""
    base = (s.get("bkash_callback_base") or "").strip().rstrip("/")
    if s.get("expired_notice_bkash") == "1" and s.get("portal_on") == "1" and s.get("bkash_on") == "1" and base:
        return base + "/portal/login"
    return ""


@route("/expired-notice")
def expired_notice():
    s = cfg()
    sub, plan = _customer_by_ip(request.remote_addr)
    info = None
    if request.args.get("preview") == "1" and session.get("uid"):          # Settings > Expired notice: Preview
        info = {"name": "Labib Hasan", "username": "labib@ahammedpur", "plan": "10 Mbps Home",
                "ended": datetime.now() - timedelta(hours=5), "amount": 500.0, "balance": 0.0}
    elif sub:
        end = radius.account_end(sub, plan)
        if (end and end <= datetime.now()) or sub.get("status") == "expired":
            import mod_subs
            price = mod_subs.customer_price(sub, plan) if plan else 0.0
            bal = round(float(sub.get("balance") or 0), 2)
            info = {"name": sub.get("full_name") or "", "username": sub["username"],
                    "plan": (plan or {}).get("label") or (plan or {}).get("name") or "",
                    "ended": end, "amount": max(round(price - bal, 2), 0.0), "balance": bal}
    import mod_settings
    html = render_template("expired_notice.html", s=s, info=info, phone=s.get("expired_notice_phone") or s.get("support_phone") or "",
                           message=s.get("expired_notice_msg") or DEFAULT_MSG, pay_url=_pay_url(s),
                           isp=s.get("isp_name") or "", brand=mod_settings.brand_urls(), currency=s.get("currency") or "BDT")
    return Response(html, headers={"Cache-Control": "no-store, max-age=0"})


def _guess_server_ip():
    """The panel's address as the browser reached it, resolved to an IP; "" when that is only this machine
    (127.x / ::1), which the routers cannot use."""
    host = request.host.split(":")[0].strip("[]")
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return ""
    try:
        return "" if ipaddress.ip_address(ip).is_loopback else ip
    except ValueError:
        return ""


def mikrotik_commands(server, bkash, panel_host=""):
    """The router commands (RouterOS 7) that send the expired customers' web requests to the notice page."""
    allow = [("ISP Panel", server)]
    if panel_host and panel_host != server:
        allow.append(("ISP Panel portal", panel_host))
    if bkash:
        allow += [("ISP Panel bKash", h) for h in BKASH_HOSTS]
    lines = ["# 1. sites an expired customer may still open (this server, and bKash for paying)",
             "/ip firewall address-list"]
    lines += ['add list=expired-allow address=%s comment="%s"' % (a, c) for c, a in allow]
    lines += ["",
              "# 2. their plain-HTTP web requests go to the notice page",
              "/ip firewall nat",
              'add chain=dstnat src-address-list=%s dst-address-list=!expired-allow protocol=tcp dst-port=80 '
              'action=dst-nat to-addresses=%s to-ports=%d comment="ISP Panel expired notice" place-before=0'
              % (radius.EXPIRED_LIST, server, NOTICE_PORT),
              "",
              "# 3. everything else is blocked for them (these go to the top of the forward rules)",
              "/ip firewall filter",
              'add chain=forward src-address-list=%s action=reject reject-with=icmp-admin-prohibited '
              'comment="ISP Panel expired: block the rest" place-before=0' % radius.EXPIRED_LIST,
              'add chain=forward src-address-list=%s dst-address-list=expired-allow action=accept '
              'comment="ISP Panel expired: allowed sites" place-before=0' % radius.EXPIRED_LIST,
              'add chain=forward src-address-list=%s protocol=udp dst-port=53 action=accept '
              'comment="ISP Panel expired: DNS" place-before=0' % radius.EXPIRED_LIST,
              'add chain=forward src-address-list=%s protocol=tcp dst-port=53 action=accept '
              'comment="ISP Panel expired: DNS" place-before=0' % radius.EXPIRED_LIST]
    return "\n".join(lines)


@route("/settings/expired-notice", methods=["GET", "POST"])
@admin_required
def settings_expired():
    s = cfg()
    if request.method == "POST":
        f = request.form
        rate = f.get("expired_notice_rate", "").strip()[:64]
        server = f.get("expired_notice_server", "").strip()[:64]
        if rate and not core.RATE_RE.match(rate):
            flash("The speed looks like 256k/256k. Nothing was saved.", "error")
            return redirect(url_for("settings_expired"))
        try:
            if server:
                ipaddress.ip_address(server)
        except ValueError:
            flash("Server address must be an IP address the routers can reach, such as 103.10.20.30. Nothing was saved.", "error")
            return redirect(url_for("settings_expired"))
        new = {"expired_notice_on": "1" if f.get("expired_notice_on") == "1" else "0",
               "expired_notice_bkash": "1" if f.get("expired_notice_bkash") == "1" else "0",
               "expired_notice_phone": f.get("expired_notice_phone", "").strip()[:32],
               "expired_notice_msg": f.get("expired_notice_msg", "").strip()[:500],
               "expired_notice_rate": rate, "expired_notice_server": server}
        import db
        with get_db().cursor() as cur:
            for k, v in new.items():
                db.save_setting(cur, k, v)
            radius.ensure_expired_group(cur, rate)
        audit("settings_saved", "expired notice", "on" if new["expired_notice_on"] == "1" else "off")
        commit()
        flash(("Expired notice is on. Expired customers move to it within 5 minutes. Paste the MikroTik commands below "
               "on every router.") if new["expired_notice_on"] == "1" else "Expired notice is off. Expired customers are "
              "refused again within 5 minutes.", "success")
        return redirect(url_for("settings_expired"))
    server = s.get("expired_notice_server") or _guess_server_ip() or "SERVER_IP"
    panel_host = ""
    base = (s.get("bkash_callback_base") or "").strip()
    if base:
        panel_host = base.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    in_group = q("SELECT COUNT(DISTINCT username) AS c FROM radusergroup WHERE groupname = %s", (radius.EXPIRED_GROUP,),
                 one=True)["c"]
    return render_template("settings_expired.html", s=s, tab="expired", server=server, default_msg=DEFAULT_MSG,
                           commands=mikrotik_commands(server, s.get("expired_notice_bkash") == "1", panel_host),
                           pay_url=_pay_url(s), in_group=in_group, port=NOTICE_PORT)
