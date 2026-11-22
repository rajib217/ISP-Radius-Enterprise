"""Reads the SMS of a USB GSM modem (SIM800C) on this server and books bKash payments from them. Runs as the
isp-panel-gsm systemd service, the only process that opens the modem's serial port.

Every 5 seconds: list the SMS on the SIM (PDU mode), join long SMS that came in parts, keep each one in gsm_inbox,
hand it to the bKash SMS auto-renew (mod_bksms.ingest: the same rules as the phone's forwarder app) and delete it
from the SIM, so the SIM never fills up. Every 30 seconds: signal, SIM, network. All of it goes into a small state
file that Settings > bKash SMS shows. Switched on and set up in Settings > bKash SMS (gsm_on, gsm_port, gsm_baud).

When Settings > SMS sends "through the USB GSM modem" (sms_via = modem), this service is also the SMS gateway: it sends
the waiting customer SMS of the outbox (2 per round, at most 10 a minute) and isp-panel-sender leaves them alone."""
import os
import signal
import sys
import time
import traceback
from datetime import datetime

import gsmlib

RUNNING = True
POLL, IDLE, RETRY, STATUS_EVERY = 5, 10, 15, 30
MODEM_RATE = 10          # SMS per minute at most through a SIM: faster and the operator may bar it as spam
SEND_PER_ROUND = 2       # a round stays short, so incoming bKash SMS are still read every few seconds
MAX_TRIES = 3


def stop(*_):
    global RUNNING
    RUNNING = False


class Service:
    def __init__(self, app, opener=gsmlib.Modem, finder=gsmlib.find_port):
        self.app, self.opener, self.finder = app, opener, finder
        self.modem, self.seen, self.last_status, self.last_prune = None, {}, 0.0, 0.0
        self.state = {"on": False, "connected": False, "port": "", "error": "", "sms_read": 0,
                      "started_at": datetime.now().isoformat(timespec="seconds")}

    def close(self):
        if self.modem:
            self.modem.close()
        self.modem = None
        self.state["connected"] = False

    def settings(self):
        import core
        with self.app.test_request_context("/gsm-modem"):
            return dict(core.cfg())

    def handle(self, sender, text, when):
        """Keep the SMS and pass it to the bKash auto-renew. Returns the result word."""
        import core
        import mod_bksms
        with self.app.test_request_context("/gsm-modem"):
            iid = core.insert("INSERT INTO gsm_inbox (sender, text, sms_time, result) VALUES (%s, %s, %s, 'new')",
                              (sender[:64], text, when))
            core.commit()
            try:
                res = mod_bksms.ingest(text, sender, "modem")
                word = res.get("status", "")
                if word == "ignored":
                    word = "ignored: " + res.get("why", "")
            except Exception as exc:
                core.get_db().rollback()
                word = "error: %s" % exc
                traceback.print_exc()
            core.x("UPDATE gsm_inbox SET result = %s WHERE id = %s", (word[:64], iid))
            if time.time() - self.last_prune > 3600:
                core.x("DELETE FROM gsm_inbox WHERE received_at < NOW() - INTERVAL 7 DAY")
                self.last_prune = time.time()
            core.commit()
        return word

    def poll(self):
        now, items, keep = time.time(), [], set()
        for idx, pdu in self.modem.messages():
            d = gsmlib.decode_pdu(pdu)
            if d is None:                         # a sent copy or a delivery report: not ours to keep
                self.modem.delete(idx)
                continue
            key = (idx, pdu[:60])
            keep.add(key)
            items.append((idx, d, self.seen.setdefault(key, now)))
        self.seen = {k: v for k, v in self.seen.items() if k in keep}
        whole, _waiting = gsmlib.join_parts(items, now)
        for idxs, sender, when, text in whole:
            self.handle(sender, text, when)
            for i in idxs:
                self.modem.delete(i)
            self.state["sms_read"] = self.state.get("sms_read", 0) + 1
            self.state["last_sms_at"] = datetime.now().isoformat(timespec="seconds")
            self.state["last_sms_from"] = sender[:64]
        return len(whole)

    def send_round(self, s):
        """Customer SMS (the outbox) when Settings > SMS sends through the modem. Returns how many went out."""
        if s.get("sms_via") != "modem":
            return 0
        import core
        try:
            rate = max(1, min(int(s.get("sms_rate") or MODEM_RATE), MODEM_RATE))
        except ValueError:
            rate = MODEM_RATE
        now = time.time()
        self.sent_times = [t for t in getattr(self, "sent_times", []) if now - t < 60]
        room = min(rate - len(self.sent_times), SEND_PER_ROUND)
        if room <= 0:
            return 0
        cc = s.get("sms_country_code") or "880"
        sent = 0
        with self.app.test_request_context("/gsm-modem"):
            rows = core.q("SELECT id, to_addr, body, tries FROM outbox WHERE channel = 'sms' AND status = 'pending' "
                          "ORDER BY id LIMIT %s", (room,))
            for row in rows:
                try:
                    self.modem.send_sms(row["to_addr"], row["body"], cc)
                    core.x("UPDATE outbox SET status = 'sent', sent_at = NOW(), error = '' WHERE id = %s", (row["id"],))
                    sent += 1
                    self.sent_times.append(time.time())
                    self.state["sms_sent"] = self.state.get("sms_sent", 0) + 1
                    self.state["last_sent_at"] = datetime.now().isoformat(timespec="seconds")
                    self.state["send_error"] = ""
                except gsmlib.ModemError as exc:
                    tries = row["tries"] + 1
                    core.x("UPDATE outbox SET tries = %s, error = %s, status = %s WHERE id = %s",
                           (tries, ("Modem: %s" % exc)[:250], "failed" if tries >= MAX_TRIES else "pending", row["id"]))
                    self.state["send_error"] = str(exc)[:200]
                    core.commit()
                    if "went away" in str(exc):
                        raise
                core.commit()
        return sent

    def tick(self):
        """One round. Returns the seconds to wait before the next one."""
        s = self.settings()
        self.state["on"] = s.get("gsm_on") == "1"
        if not self.state["on"]:
            self.close()
            self.state.update(error="", port="")
            return IDLE
        if os.path.exists(gsmlib.RECONNECT_FILE):
            try:
                os.remove(gsmlib.RECONNECT_FILE)
            except OSError:
                pass
            self.close()
        try:
            if not self.modem:
                try:
                    baud = int(s.get("gsm_baud") or 115200)
                except ValueError:
                    baud = 115200
                port = (s.get("gsm_port") or "auto").strip()
                if port == "auto":
                    port = self.finder(baud)
                    if not port:
                        found = ", ".join(gsmlib.candidate_ports()) or "none"
                        raise gsmlib.ModemError("no modem answered on USB (serial devices: %s)" % found)
                self.state["port"] = port
                self.modem = self.opener(port, baud)
                self.modem.setup()
                self.last_status = 0.0
            if time.time() - self.last_status > STATUS_EVERY:
                self.state.update(self.modem.status())
                self.state["checked_at"] = datetime.now().isoformat(timespec="seconds")
                self.last_status = time.time()
            self.poll()
            self.send_round(s)
            self.state.update(connected=True, error="")
            return POLL
        except gsmlib.ModemError as exc:
            self.close()
            self.state["error"] = str(exc)[:300]
            return RETRY


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    from app import app
    svc = Service(app)
    while RUNNING:
        try:
            wait = svc.tick()
        except Exception as exc:          # database restarting and the like: report it and try again
            traceback.print_exc()
            svc.close()
            svc.state["error"] = ("%s: %s" % (type(exc).__name__, exc))[:300]
            wait = RETRY
        try:
            gsmlib.write_state(svc.state)
        except OSError as exc:
            print("cannot write %s: %s" % (gsmlib.STATE_FILE, exc), file=sys.stderr)
        end = time.time() + wait
        while RUNNING and time.time() < end:
            time.sleep(0.5)
    svc.close()


if __name__ == "__main__":
    main()
