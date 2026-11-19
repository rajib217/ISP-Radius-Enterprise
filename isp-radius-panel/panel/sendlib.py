"""Send one email or one SMS. No Flask or database code, so it can run in the sender service and in tests."""
import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


class SendError(Exception):
    pass


class Mailer:
    """Keeps one SMTP connection open while a batch is sent."""

    def __init__(self, s):
        self.s = s
        self.server = None

    def _connect(self):
        host = (self.s.get("smtp_host") or "").strip()
        if not host:
            raise SendError("SMTP is not configured (Settings > Email).")
        port = int(self.s.get("smtp_port") or 587)
        security = self.s.get("smtp_security") or "starttls"
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=25)
                server.ehlo()
                if security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            if self.s.get("smtp_user"):
                server.login(self.s["smtp_user"], self.s.get("smtp_pass", ""))
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError("Cannot connect to the mail server: %s" % exc)
        self.server = server

    def send(self, to, subject, body):
        sender = (self.s.get("smtp_from") or self.s.get("smtp_user") or "").strip()
        if not sender:
            raise SendError("Set a From address in Settings > Email.")
        msg = EmailMessage()
        try:
            msg["From"] = formataddr((self.s.get("smtp_from_name") or "", sender))
            msg["To"] = to
            msg["Subject"] = subject or "(no subject)"
        except ValueError as exc:
            raise SendError("Bad header: %s" % exc)
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(body or "")
        for attempt in (1, 2):
            if self.server is None:
                self._connect()
            try:
                self.server.send_message(msg)
                return
            except smtplib.SMTPRecipientsRefused as exc:
                raise SendError("Recipient refused: %s" % exc)
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPException, OSError) as exc:
                self.close()
                if attempt == 2:
                    raise SendError("Mail server error: %s" % exc)

    def close(self):
        if self.server is not None:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None


def _fill(template, values, encode):
    for key, value in values.items():
        template = template.replace("{" + key + "}", encode(value))
    return template


def send_sms(s, to, body):
    """Call the configured HTTP gateway. The URL may contain {to}, {message} and {sender}."""
    url_t = (s.get("sms_url") or "").strip()
    if not url_t:
        raise SendError("The SMS gateway is not configured (Settings > SMS).")
    if not url_t.lower().startswith(("http://", "https://")):
        raise SendError("The gateway URL must start with http:// or https://")
    values = {"to": to, "message": body, "sender": s.get("sms_sender") or ""}
    url = _fill(url_t, values, urllib.parse.quote_plus)
    method = (s.get("sms_method") or "GET").upper()
    data, headers = None, {"User-Agent": "isp-panel/1.0"}
    if method == "POST":
        body_t = (s.get("sms_body") or "").strip()
        if body_t.startswith("{") or body_t.startswith("["):
            data = _fill(body_t, values, lambda v: json.dumps(str(v))[1:-1])
            headers["Content-Type"] = "application/json"
        else:
            data = _fill(body_t, values, urllib.parse.quote_plus)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read(3000).decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as exc:
        raise SendError("Gateway returned HTTP %s: %s" % (exc.code, exc.read(200).decode("utf-8", "replace")))
    except (urllib.error.URLError, OSError) as exc:
        raise SendError("Cannot reach the gateway: %s" % getattr(exc, "reason", exc))
    expect = (s.get("sms_success") or "").strip()
    ok = (expect.lower() in text.lower()) if expect else (200 <= code < 300)
    if not ok:
        raise SendError("Gateway answered: %s" % text.strip()[:200])
    return text.strip()[:200]
