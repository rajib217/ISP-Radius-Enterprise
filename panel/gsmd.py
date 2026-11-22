"""Reads the SMS of a USB GSM modem (SIM800C) on this server and books bKash payments from them. Runs as the
isp-panel-gsm systemd service, the only process that opens the modem's serial port.

Every 5 seconds: list the SMS on the SIM (PDU mode), join long SMS that came in parts, keep each one in gsm_inbox,
hand it to the bKash SMS auto-renew (mod_bksms.ingest: the same rules as the phone's forwarder app) and delete it
from the SIM, so the SIM never fills up. Every 30 seconds: signal, SIM, network. All of it goes into a small state
file that Settings > bKash SMS shows. Switched on and set up in Settings > bKash SMS (gsm_on, gsm_port, gsm_baud)."""
import os
import signal
import sys
import time
import traceback
from datetime import datetime

import gsmlib

RUNNING = True
POLL, IDLE, RETRY, STATUS_EVERY = 5, 10, 15, 30


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
