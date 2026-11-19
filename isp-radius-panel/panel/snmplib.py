"""A small SNMP v1 / v2c client (GET, GETNEXT, GETBULK, walk) with no third-party packages.

SNMPv3 needs encryption and is not supported. Values come back as Python ints or strings.
"""
import os
import socket
import time


class SnmpError(Exception):
    pass


# ----------------------------------------------------------------- BER encoding
def _len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(v):
    n = max((v.bit_length() + 8) // 8, 1)
    return _tlv(0x02, v.to_bytes(n, "big", signed=True))


def _oid(arcs):
    arcs = list(arcs)
    if len(arcs) < 2:
        raise SnmpError("OID too short")
    out = bytearray([40 * arcs[0] + arcs[1]])
    for a in arcs[2:]:
        chunk = [a & 0x7F]
        a >>= 7
        while a:
            chunk.append(0x80 | (a & 0x7F))
            a >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def parse_oid(text):
    try:
        arcs = tuple(int(p) for p in str(text).strip().strip(".").split("."))
    except ValueError:
        raise SnmpError("Not a valid OID: %r" % text)
    if len(arcs) < 2 or arcs[0] > 2:
        raise SnmpError("Not a valid OID: %r" % text)
    return arcs


def fmt_oid(arcs):
    return ".".join(str(a) for a in arcs)


# ----------------------------------------------------------------- BER decoding
def _read(buf, pos):
    tag = buf[pos]
    ln = buf[pos + 1]
    pos += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[pos:pos + k], "big")
        pos += k
    if pos + ln > len(buf):
        raise SnmpError("Truncated SNMP reply")
    return tag, buf[pos:pos + ln], pos + ln


def _decode_oid(body):
    if not body:
        return ()
    arcs = [body[0] // 40, body[0] % 40]
    v = 0
    for b in body[1:]:
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(v)
            v = 0
    return tuple(arcs)


def _decode_value(tag, body):
    if tag == 0x02:
        return int.from_bytes(body, "big", signed=True)
    if tag in (0x41, 0x42, 0x43, 0x46):
        return int.from_bytes(body, "big")
    if tag == 0x04 or tag == 0x44:
        return body
    if tag == 0x05:
        return None
    if tag == 0x06:
        return fmt_oid(_decode_oid(body))
    if tag == 0x40 and len(body) == 4:
        return ".".join(str(b) for b in body)
    if tag in (0x80, 0x81):
        return "NOSUCH"
    if tag == 0x82:
        return "END"
    return body


def _build(version, community, pdu_tag, reqid, a, b, oids):
    binds = b"".join(_tlv(0x30, _oid(o) + b"\x05\x00") for o in oids)
    pdu = _tlv(pdu_tag, _int(reqid) + _int(a) + _int(b) + _tlv(0x30, binds))
    return _tlv(0x30, _int(0 if version == "v1" else 1) + _tlv(0x04, community.encode()) + pdu)


def _parse(packet, reqid):
    tag, body, _ = _read(packet, 0)
    if tag != 0x30:
        raise SnmpError("Not an SNMP reply")
    pos = 0
    _, _, pos = _read(body, pos)                  # version
    _, _, pos = _read(body, pos)                  # community
    ptag, pdu, _ = _read(body, pos)
    if ptag != 0xA2:
        raise SnmpError("Unexpected SNMP packet type")
    pos = 0
    _, rid, pos = _read(pdu, pos)
    if int.from_bytes(rid, "big", signed=True) != reqid:
        return None                               # an old, late reply
    _, status, pos = _read(pdu, pos)
    _, _, pos = _read(pdu, pos)                   # error index
    status = int.from_bytes(status, "big")
    _, vb, _ = _read(pdu, pos)
    out, pos = [], 0
    while pos < len(vb):
        _, bind, pos = _read(vb, pos)
        t, o, p2 = _read(bind, 0)
        t2, v, _ = _read(bind, p2)
        out.append((_decode_oid(o), t2, _decode_value(t2, v)))
    return status, out


class Snmp:
    def __init__(self, host, community="public", version="v2c", port=161, timeout=5, retries=3):
        if version not in ("v1", "v2c"):
            raise SnmpError("Only SNMP v1 and v2c are supported.")
        self.host, self.community, self.version = host, community, version
        self.port, self.timeout, self.retries = int(port), max(float(timeout), 0.5), max(int(retries), 0)
        self.sock = None

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _ask(self, pdu_tag, a, b, oids):
        if self.sock is None:
            try:
                info = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM)[0]
                self.sock = socket.socket(info[0], socket.SOCK_DGRAM)
                self.sock.connect(info[4])
            except OSError as exc:
                raise SnmpError("Cannot reach %s: %s" % (self.host, exc))
        reqid = int.from_bytes(os.urandom(3), "big")
        packet = _build(self.version, self.community, pdu_tag, reqid, a, b, oids)
        for _ in range(self.retries + 1):
            try:
                self.sock.send(packet)
                self.sock.settimeout(self.timeout)
                end = time.time() + self.timeout
                while True:
                    reply = _parse(self.sock.recv(65535), reqid)
                    if reply is not None:
                        return reply
                    if time.time() > end:
                        break
            except socket.timeout:
                continue
            except ConnectionRefusedError:
                raise SnmpError("%s:%d refused the connection. Check the SNMP port and any firewall." % (self.host, self.port))
            except (OSError, IndexError, ValueError) as exc:
                raise SnmpError("SNMP error: %s" % exc)
        raise SnmpError("No answer from %s:%d. Check the IP, the SNMP port, the community and that SNMP is enabled "
                        "on the OLT." % (self.host, self.port))

    def get(self, oid):
        status, binds = self._ask(0xA0, 0, 0, [parse_oid(oid)])
        if status:
            raise SnmpError("The device refused that OID (error %d)." % status)
        o, t, v = binds[0]
        return None if v in ("NOSUCH", "END") else v

    def walk(self, base, max_rows=20000, bulk=25):
        """Yield (oid_tuple, value) for everything under base."""
        base = parse_oid(base)
        cur, seen = base, 0
        while seen < max_rows:
            if self.version == "v1":
                status, binds = self._ask(0xA1, 0, 0, [cur])
                if status == 2:
                    return
            else:
                status, binds = self._ask(0xA5, 0, bulk, [cur])
            if status:
                raise SnmpError("The device refused the walk (error %d)." % status)
            if not binds:
                return
            progressed = False
            for oid, tag, value in binds:
                if value == "END" or oid[:len(base)] != base or oid <= cur:
                    if not progressed and oid <= cur and value != "END" and oid[:len(base)] == base:
                        raise SnmpError("The device returned OIDs out of order.")
                    return
                yield oid, value
                cur, progressed = oid, True
                seen += 1
            if not progressed:
                return


def text(value):
    """Show an SNMP value as text; binary strings become hex (a MAC shows as AA:BB:...)."""
    if isinstance(value, bytes):
        try:
            s = value.decode("utf-8")
            if s.isprintable():
                return s.strip()
        except UnicodeDecodeError:
            pass
        return ":".join("%02X" % b for b in value)
    return "" if value is None else str(value)
