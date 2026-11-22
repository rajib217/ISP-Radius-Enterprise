"""A USB GSM modem (SIM800C and other AT-command modems) on the panel server: talking to it over the serial port, and
decoding the SMS it holds (PDU mode, so Bangla / Unicode SMS and long SMS sent in several parts come out whole).

Used by gsmd.py (the isp-panel-gsm service, the only process that opens the port) and by Settings > bKash SMS,
which reads the state file the service writes. Standard library only (termios), Linux only for the serial part."""
import glob
import json
import os
import re
import select
import time
from datetime import datetime

import core

STATE_FILE = os.path.join(core.DATA_DIR, "gsm_state.json")
RECONNECT_FILE = os.path.join(core.DATA_DIR, "gsm_reconnect")
BAUDS = (9600, 19200, 38400, 57600, 115200)
STALE = 60                 # seconds: an older state file means the service is not running

# --------------------------------------------------------------------------------------------- GSM 03.38 alphabet
GSM7 = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\", 0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|",
            0x65: "€"}


def _septets(data, count):
    bits = int.from_bytes(data, "little")
    return [(bits >> (7 * i)) & 0x7F for i in range(count)]


def _gsm7(septets):
    out, esc = [], False
    for c in septets:
        if esc:
            out.append(GSM7_EXT.get(c, " "))
            esc = False
        elif c == 0x1B:
            esc = True
        else:
            out.append(GSM7[c])
    return "".join(out)


def _semi(data):
    """Swapped semi-octets as digits ('F' filler dropped)."""
    return "".join("%X%X" % (b & 0x0F, b >> 4) for b in data).rstrip("F")


def _address(pdu, i):
    """(address, next index) of an originating address."""
    digits, toa = pdu[i], pdu[i + 1]
    n = (digits + 1) // 2
    raw = pdu[i + 2:i + 2 + n]
    if toa & 0x70 == 0x50:                                  # alphanumeric sender, such as "bKash"
        addr = _gsm7(_septets(raw, digits * 4 // 7))
    else:
        addr = ("+" if toa & 0x70 == 0x10 else "") + _semi(raw)
    return addr.strip(), i + 2 + n


def _stamp(data):
    try:
        d = _semi(data[:6])
        return datetime(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]), int(d[6:8]), int(d[8:10]), int(d[10:12]))
    except (ValueError, IndexError):
        return None


def decode_pdu(hexpdu):
    """An SMS-DELIVER PDU (as AT+CMGL gives it, with the SMSC in front) as
    {'sender', 'time', 'text', 'part': (ref, total, seq) or None}; None for anything that is not a received SMS."""
    try:
        pdu = bytes.fromhex(hexpdu.strip())
        i = 1 + pdu[0]                                      # skip the SMS centre
        first = pdu[i]
        if first & 0x03 != 0:                               # 0 = SMS-DELIVER; sent copies and reports are skipped
            return None
        sender, i = _address(pdu, i + 1)
        dcs = pdu[i + 1]
        when = _stamp(pdu[i + 2:i + 9])
        udl = pdu[i + 9]
        ud = pdu[i + 10:]
        if dcs & 0xC0 == 0:
            alpha = (dcs >> 2) & 3
        elif dcs & 0xF0 == 0xF0:
            alpha = 1 if dcs & 4 else 0
        elif dcs & 0xF0 == 0xE0:
            alpha = 2
        else:
            alpha = 0
        part, head = None, 0
        if first & 0x40:                                    # user data header: long SMS in parts
            udhl = ud[0]
            h, j = ud[1:1 + udhl], 0
            while j + 1 < len(h):
                iei, ln = h[j], h[j + 1]
                v = h[j + 2:j + 2 + ln]
                if iei == 0x00 and ln == 3:
                    part = (v[0], v[1], v[2])
                elif iei == 0x08 and ln == 4:
                    part = ((v[0] << 8) | v[1], v[2], v[3])
                j += 2 + ln
            head = udhl + 1
        if alpha == 0:
            skip = (head * 8 + 6) // 7 if head else 0
            text = _gsm7(_septets(ud, udl)[skip:])
        elif alpha == 2:
            text = ud[head:udl].decode("utf-16-be", "replace")
        else:
            text = ud[head:udl].decode("latin-1", "replace")
        return {"sender": sender, "time": when, "text": text, "part": part}
    except (IndexError, ValueError):
        return None


def parse_cmgl(lines):
    """[(index, pdu hex)] from the lines of an AT+CMGL answer in PDU mode."""
    out, idx = [], None
    for line in lines:
        m = re.match(r"\+CMGL:\s*(\d+)", line)
        if m:
            idx = int(m.group(1))
        elif idx is not None and re.fullmatch(r"[0-9A-Fa-f]+", line.strip() or "x"):
            out.append((idx, line.strip()))
            idx = None
    return out


def join_parts(items, now=None, wait=600):
    """Group decoded SMS: [(indexes, sender, time, text)] of whole messages. The parts of a long SMS are joined in
    order; parts still missing after `wait` seconds (counted from `seen`) are given up on and joined as they are.
    items: [(index, decoded, seen_epoch)]. Returns (complete, waiting_indexes)."""
    now = now or time.time()
    whole, groups = [], {}
    for idx, d, seen in items:
        if not d["part"] or d["part"][1] <= 1:
            whole.append(([idx], d["sender"], d["time"], d["text"]))
        else:
            ref, total, seq = d["part"]
            groups.setdefault((d["sender"], ref, total), []).append((seq, idx, d, seen))
    waiting = []
    for (sender, _ref, total), parts in groups.items():
        parts.sort(key=lambda p: p[0])
        if len({p[0] for p in parts}) >= total or now - min(p[3] for p in parts) > wait:
            whole.append(([p[1] for p in parts], sender, parts[0][2]["time"], "".join(p[2]["text"] for p in parts)))
        else:
            waiting += [p[1] for p in parts]
    return whole, waiting



# --------------------------------------------------------------------------------------------- sending (SMS-SUBMIT)
_GSM7_INDEX = {ch: i for i, ch in enumerate(GSM7)}
_GSM7_EXT_INDEX = {ch: code for code, ch in GSM7_EXT.items()}
MAX_PARTS = 7                       # 7 parts = about 1000 GSM characters, the panel's SMS limit


def _to_septets(text):
    """GSM 7-bit codes for the text (an extension character takes two), or None when it needs Unicode."""
    out = []
    for ch in text:
        if ch in _GSM7_INDEX:
            out.append(_GSM7_INDEX[ch])
        elif ch in _GSM7_EXT_INDEX:
            out += [0x1B, _GSM7_EXT_INDEX[ch]]
        else:
            return None
    return out


def _pack(septets, fill_bits=0):
    bits = 0
    for k, sv in enumerate(septets):
        bits |= sv << (fill_bits + 7 * k)
    return bits.to_bytes((fill_bits + 7 * len(septets) + 7) // 8, "little")


def _number(to, cc="880"):
    """(type of address, digits): international when it starts with the country code (or +), else national."""
    raw = (to or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
        raw = "+"
    if not 3 <= len(digits) <= 20:
        raise ModemError("%r is not a phone number" % to)
    intl = raw.startswith("+") or bool(cc and digits.startswith(cc) and len(digits) > len(cc) + 6)
    return (0x91 if intl else 0x81), digits


def encode_sms(to, text, ref=0, cc="880"):
    """SMS-SUBMIT PDUs (hex, with the SIM's own SMS centre) for one message: GSM alphabet when every character
    fits, otherwise Unicode (Bangla); split in parts with a joining header when it is too long for one SMS.
    Returns [(pdu hex, length for AT+CMGS)]."""
    toa, digits = _number(to, cc)
    addr = "%02X%02X" % (len(digits), toa) + bytes.fromhex(
        "".join(b + a for a, b in zip(*[iter(digits + ("F" if len(digits) % 2 else ""))] * 2))).hex().upper()
    sept = _to_septets(text)
    if sept is not None:
        one, each = 160, 153
        if len(sept) <= one:
            chunks = [sept]
        else:
            chunks, cur = [], []
            for k, sv in enumerate(sept):          # never split an escape pair across two parts
                if len(cur) >= each or (len(cur) == each - 1 and sv == 0x1B):
                    chunks.append(cur)
                    cur = []
                cur.append(sv)
            chunks.append(cur)
    else:
        units = text.encode("utf-16-be")
        one, each = 140, 134
        if len(units) <= one:
            chunks = [units]
        else:
            chunks, cur = [], b""
            for k in range(0, len(units), 2):        # whole UTF-16 units; a surrogate pair stays together
                u = units[k:k + 2]
                if len(cur) >= each or (len(cur) == each - 2 and 0xD8 <= u[0] <= 0xDB):
                    chunks.append(cur)
                    cur = b""
                cur += u
            chunks.append(cur)
    if len(chunks) > MAX_PARTS:
        raise ModemError("the message is too long (%d SMS parts, at most %d)" % (len(chunks), MAX_PARTS))
    out, total = [], len(chunks)
    for seq, chunk in enumerate(chunks, 1):
        udh = bytes([5, 0, 3, ref & 0xFF, total, seq]) if total > 1 else b""
        if sept is not None:
            fill = (7 - (len(udh) * 8) % 7) % 7 if udh else 0
            body = _pack(chunk, len(udh) * 8 + fill)
            ud = bytes(a | b for a, b in zip(udh + bytes(max(len(body) - len(udh), 0)), body)) if udh else body
            udl, dcs = (len(udh) * 8 + fill) // 7 + len(chunk), 0x00
        else:
            ud, dcs = udh + chunk, 0x08
            udl = len(ud)
        tpdu = "%02X00" % (0x41 if udh else 0x01) + addr + "00%02X" % dcs + "%02X" % udl + ud.hex().upper()
        out.append(("00" + tpdu, len(tpdu) // 2))
    return out

# --------------------------------------------------------------------------------------------- the serial port
def candidate_ports():
    """Serial devices a USB modem can be: the stable name from the installer's udev rule first."""
    seen, out = set(), []
    for pat in ("/dev/gsm-modem", "/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(pat)):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                out.append(p)
    return out


RX_FINAL = re.compile(r"\r\n(OK|ERROR|\+CMS ERROR[^\r]*|\+CME ERROR[^\r]*)\r\n")


class ModemError(Exception):
    pass


class Modem:
    def __init__(self, port, baud=115200):
        import termios
        import tty
        self.port = port
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise ModemError("cannot open %s: %s" % (port, e.strerror or e))
        try:
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            speed = getattr(termios, "B%d" % int(baud), termios.B115200)
            attrs[4] = attrs[5] = speed
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~getattr(termios, "CRTSCTS", 0)
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception as e:
            os.close(self.fd)
            raise ModemError("cannot set up %s: %s" % (port, e))

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def cmd(self, line, timeout=5.0):
        """Send one AT command; (ok, [answer lines]) without the echo, blank lines and unsolicited codes."""
        import termios
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
            os.write(self.fd, (line + "\r").encode("ascii"))
        except OSError as e:
            raise ModemError("the modem went away (%s)" % (e.strerror or e))
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError as e:
                    raise ModemError("the modem went away (%s)" % (e.strerror or e))
                if not chunk:
                    raise ModemError("the modem went away")
                buf += chunk
                lines = [x.strip() for x in buf.decode("latin-1").replace("\r", "\n").split("\n")]
                for x in lines:
                    if x == "OK" or x == "ERROR" or x.startswith("+CMS ERROR") or x.startswith("+CME ERROR"):
                        body = [y for y in lines if y and y != line and y != x and not y.startswith(("RING", "+CMTI", "+CRING"))]
                        return x == "OK", body if x == "OK" else body + [x]
        return False, ["no answer"]

    def value(self, line, prefix, timeout=5.0):
        ok, lines = self.cmd(line, timeout)
        for x in lines if ok else ():
            if x.startswith(prefix):
                return x[len(prefix):].strip()
        return ""

    def setup(self):
        """Wake the modem up and switch it to the mode the service reads SMS in."""
        for _ in range(3):
            if self.cmd("AT", 2)[0]:
                break
        else:
            raise ModemError("no answer to AT on %s (wrong port or speed, or the modem has no power)" % self.port)
        for c in ("ATE0", "AT+CMEE=2", "AT+CMGF=0", "AT+CNMI=0,0,0,0,0", 'AT+CPMS="SM","SM","SM"', "AT+COPS=3,0"):
            self.cmd(c, 5)

    def status(self):
        st = {"model": "", "sim": "", "operator": "", "signal": None, "registered": ""}
        ok, lines = self.cmd("AT+CGMM", 3)
        # SIM800C answers "SIM800"; ZTE / Huawei sticks answer "+CGMM: MF193A"
        st["model"] = re.sub(r"^\+CGMM:\s*", "", lines[0] if ok and lines else "")[:40]
        ok, lines = self.cmd("ATI", 3)
        if ok and lines and lines[0] and lines[0] not in st["model"] and not lines[0].startswith("Manufacturer"):
            st["model"] = ("%s · %s" % (st["model"], lines[0]) if st["model"] else lines[0])[:80]
        st["sim"] = self.sim_state()
        csq = self.value("AT+CSQ", "+CSQ:")
        try:
            rssi = int(csq.split(",")[0])
            st["signal"] = None if rssi == 99 else rssi
        except ValueError:
            pass
        reg = self.value("AT+CREG?", "+CREG:")
        code = reg.split(",")[1].strip() if "," in reg else ""
        st["registered"] = {"1": "home", "5": "roaming", "2": "searching", "3": "refused", "0": "not registered"}.get(code, "")
        cops = self.value("AT+COPS?", "+COPS:")
        m = re.search(r'"([^"]+)"', cops)
        st["operator"] = m.group(1)[:40] if m else ""
        return st

    def sim_state(self):
        """READY, SIM PIN, PH-NET PIN (a stick locked to another operator) ... or why the modem cannot read the SIM, in
        its own words (SIM not inserted, SIM failure, SIM busy). A SIM put in while the modem had power is only read
        after the modem is unplugged and plugged in again."""
        ok, lines = self.cmd("AT+CPIN?", 5)
        for x in lines if ok else ():
            if x.startswith("+CPIN:"):
                return x[6:].strip()
        why = next((re.sub(r"^\+CME ERROR:\s*", "", x) for x in reversed(lines) if x.startswith(("+CME ERROR", "ERROR"))), "")
        if why.isdigit():
            why = {"10": "SIM not inserted", "11": "SIM PIN required", "13": "SIM failure", "14": "SIM busy",
                   "15": "SIM wrong"}.get(why, "error " + why)
        return ("no SIM: " + why if why and why != "ERROR" else "no SIM")[:60]

    def messages(self):
        ok, lines = self.cmd("AT+CMGL=4", 15)
        if not ok:
            raise ModemError("could not list SMS: %s" % " ".join(lines)[:120])
        return parse_cmgl(lines)

    def delete(self, idx):
        return self.cmd("AT+CMGD=%d" % int(idx), 5)[0]

    def _wait_for(self, done, timeout):
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError as e:
                    raise ModemError("the modem went away (%s)" % (e.strerror or e))
                if not chunk:
                    raise ModemError("the modem went away")
                buf += chunk
                text = buf.decode("latin-1")
                hit = done(text)
                if hit:
                    return text
        return None

    def send_sms(self, to, text, cc="880"):
        """Send one message (in as many parts as it needs). Raises ModemError with the modem's reason."""
        import termios
        self._ref = (getattr(self, "_ref", 0) + 1) & 0xFF
        for pdu, length in encode_sms(to, text, self._ref, cc):
            try:
                termios.tcflush(self.fd, termios.TCIFLUSH)
                os.write(self.fd, ("AT+CMGS=%d\r" % length).encode("ascii"))
            except OSError as e:
                raise ModemError("the modem went away (%s)" % (e.strerror or e))
            got = self._wait_for(lambda s: ">" in s or "ERROR" in s, 10)
            if not got or ">" not in got:
                os.write(self.fd, b"\x1b")                       # ESC: drop the half-started message
                raise ModemError("the modem did not take the SMS: %s" % ((got or "no answer").strip()[:120]))
            os.write(self.fd, pdu.encode("ascii") + b"\x1a")      # Ctrl-Z: send
            got = self._wait_for(lambda s: RX_FINAL.search(s), 60)
            if not got or "+CMGS" not in got or RX_FINAL.search(got).group(1) != "OK":
                reason = re.search(r"\+CM[SE] ERROR:?\s*([^\r\n]*)", got or "")
                raise ModemError("the network refused the SMS: %s"
                                 % (reason.group(1) if reason else (got or "no answer").strip()[:120]))


def find_port(baud):
    """The first serial device that answers AT, or ""."""
    for p in candidate_ports():
        try:
            m = Modem(p, baud)
        except ModemError:
            continue
        try:
            if m.cmd("AT", 1.5)[0] or m.cmd("AT", 1.5)[0]:
                return p
        except ModemError:
            pass
        finally:
            m.close()
    return ""


# --------------------------------------------------------------------------------------------- state file
def write_state(st):
    st = dict(st, updated_at=time.time())
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, default=str)
    os.replace(tmp, STATE_FILE)


def read_state():
    """The service's last state, with 'running' (written within the last minute)."""
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"running": False}
    st["running"] = time.time() - float(st.get("updated_at") or 0) < STALE
    return st


def bars(rssi):
    """0-5 signal bars from AT+CSQ's 0-31."""
    if rssi is None:
        return 0
    return 0 if rssi < 2 else 1 if rssi < 8 else 2 if rssi < 13 else 3 if rssi < 18 else 4 if rssi < 23 else 5
