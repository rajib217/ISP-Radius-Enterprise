"""Read the ONU list from an OLT over SNMP and keep the `onus` table in step.

Works from a DB cursor (dict rows) so the web panel and the maintenance job share it.
How each vendor exposes ONUs differs, so a vendor only chooses *defaults*; every OID can be edited on the OLT.
  * interfaces mode: ONUs show up as interfaces in the standard IF-MIB (common on EPON OLTs); a regex picks them.
  * table mode: separate OID tables for status / name / serial / power that share the same index.
The vendor presets below are starting points. Use "SNMP walk" on the OLT page to confirm them.
"""
import re
from datetime import datetime

import snmplib

IF_DESCR, IF_OPER = "1.3.6.1.2.1.2.2.1.2", "1.3.6.1.2.1.2.2.1.8"
PRESETS = {
    "huawei": {"mode": "table", "oid_status": "1.3.6.1.4.1.2011.6.128.1.1.2.46.1.15",
               "oid_name": "1.3.6.1.4.1.2011.6.128.1.1.2.43.1.9", "oid_serial": "1.3.6.1.4.1.2011.6.128.1.1.2.43.1.3",
               "oid_temp": "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.1", "oid_tx": "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.3",
               "oid_rx": "1.3.6.1.4.1.2011.6.128.1.1.2.51.1.4", "rx_div": 100,
               "status_map": "1=online,2=offline"},
    "zte": {"mode": "table", "oid_status": "1.3.6.1.4.1.3902.1012.3.28.2.1.4",
            "oid_name": "1.3.6.1.4.1.3902.1012.3.28.1.1.2", "oid_serial": "1.3.6.1.4.1.3902.1012.3.28.1.1.5",
            "status_map": "4=online,2=los,1=offline,3=offline,5=offline,6=offline,7=offline"},
}
DEFAULT_REGEX = r":\d+$"      # EPON ONU interfaces are usually named like EPON0/1:3
VENDORS = [("bdcom", "BDCOM"), ("vsol", "VSOL"), ("cdata", "C-Data"), ("huawei", "Huawei"), ("zte", "ZTE"),
           ("nokia", "Nokia / Alcatel"), ("other", "Other / custom")]


def status_map(text):
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().lstrip("-").isdigit() and v.strip() in ("online", "offline", "los"):
                out[int(k)] = v.strip()
    return out


def profile(cfg):
    """Resolve the mode and OIDs from the saved settings plus the vendor preset."""
    pre = PRESETS.get(cfg["vendor"], {})
    custom = any(cfg.get(k) for k in ("oid_status", "oid_name", "oid_serial", "oid_rx", "oid_tx", "oid_temp", "oid_volt", "oid_curr", "oid_dist", "oid_mac"))
    if custom or pre.get("mode") == "table":
        p = dict(pre) if pre.get("mode") == "table" else {}
        for k in ("oid_status", "oid_name", "oid_serial", "oid_rx", "oid_tx", "oid_temp", "oid_volt", "oid_curr", "oid_dist", "oid_mac", "status_map"):
            p[k] = cfg.get(k) or p.get(k, "")
        if not p.get("oid_status"):
            raise snmplib.SnmpError("Enter the OID that holds the ONU status.")
        p["mode"] = "table"
        p["map"] = status_map(p.get("status_map") or "1=online,2=offline")
        p["rx_div"] = max(int(cfg["rx_div"] or 1), 1)
        if p["rx_div"] == 1 and pre.get("mode") == "table":
            p["rx_div"] = pre.get("rx_div", 1)
        p["temp_div"] = max(int(cfg.get("temp_div") or 1), 1)
        p["volt_div"] = max(int(cfg.get("volt_div") or 1), 1)
        p["curr_div"] = max(int(cfg.get("curr_div") or 1), 1)
        return p
    return {"mode": "interfaces", "regex": cfg["if_regex"] or DEFAULT_REGEX}


def fmt_serial(v):
    """GPON serials are 8 bytes: 4 letters of vendor id + 4 bytes. Show them like HWTC-B203D6CE."""
    if isinstance(v, (bytes, bytearray)) and len(v) == 8 and all(48 <= c <= 90 or 97 <= c <= 122 for c in v[:4]):
        return "%s-%s" % (bytes(v[:4]).decode(), bytes(v[4:]).hex().upper())
    return snmplib.text(v)[:64]


def huawei_pon(index):
    """Huawei ifIndex of a PON port: 0xFA000000 + slot * 8192 + port * 256 -> frame/slot/port."""
    off = index - 0xFA000000
    if off < 0:
        return ""
    return "0/%d/%d" % (off >> 13, (off >> 8) & 0x1F)


def pon_text(vendor, suffix):
    if vendor == "huawei" and len(suffix) >= 2 and huawei_pon(suffix[-2]):
        return "%s:%d" % (huawei_pon(suffix[-2]), suffix[-1])
    return ".".join(str(a) for a in suffix[:-1])[:32]


def p_mode_table(cfg):
    try:
        return profile(cfg)["mode"] == "table"
    except snmplib.SnmpError:
        return False


def client(device, cfg):
    return snmplib.Snmp(device["ip"], cfg["community"], cfg["version"], cfg["port"], cfg["timeout"], cfg["retries"])


def read_onus(device, cfg):
    """Return a list of dicts: key, name, serial, mac, pon, status, rx."""
    p = profile(cfg)
    c = client(device, cfg)
    out = []
    try:
        if p["mode"] == "interfaces":
            try:
                rx = re.compile(p["regex"])
            except re.error:
                raise snmplib.SnmpError("The ONU interface pattern is not a valid regular expression.")
            names = {o[-1]: snmplib.text(v) for o, v in c.walk(IF_DESCR)}
            oper = {o[-1]: v for o, v in c.walk(IF_OPER)}
            for idx, name in names.items():
                if not rx.search(name):
                    continue
                pon = name.rsplit(":", 1)[0] if ":" in name else ""
                out.append({"key": str(idx), "name": name, "serial": "", "mac": "", "pon": pon, "rx": None, "tx": None, "temp": None, "volt": None, "curr": None, "dist": None,
                            "status": "online" if oper.get(idx) == 1 else "offline"})
        else:
            base = {k: snmplib.parse_oid(p[k]) for k in ("oid_status", "oid_name", "oid_serial", "oid_rx", "oid_tx", "oid_temp", "oid_volt", "oid_curr", "oid_dist", "oid_mac")
                    if p.get(k)}
            cols = {}
            for k, b in base.items():
                try:
                    cols[k] = {o[len(b):]: v for o, v in c.walk(snmplib.fmt_oid(b))}
                except snmplib.SnmpError:
                    if k != "oid_status":
                        cols[k] = {}          # optional columns: a wrong OID must not stop the ONU list
                    else:
                        raise
            for suffix, raw in cols["oid_status"].items():
                key = ".".join(str(a) for a in suffix)
                rx = tx = temp = volt = curr = dist = None
                if suffix in cols.get("oid_rx", {}) and isinstance(cols["oid_rx"][suffix], int):
                    rx = round(cols["oid_rx"][suffix] / p["rx_div"], 2)
                    if not -60 <= rx <= 10:
                        rx = None
                if suffix in cols.get("oid_tx", {}) and isinstance(cols["oid_tx"][suffix], int):
                    tx = round(cols["oid_tx"][suffix] / p["rx_div"], 2)
                    if not -60 <= tx <= 40:
                        tx = None
                if suffix in cols.get("oid_temp", {}) and isinstance(cols["oid_temp"][suffix], int):
                    temp = round(cols["oid_temp"][suffix] / p["temp_div"], 1)
                    if not -40 <= temp <= 150:
                        temp = None
                for key, div, lo, hi in (("oid_volt", p["volt_div"], 0, 1000), ("oid_curr", p["curr_div"], 0, 10000),
                                         ("oid_dist", 1, 0, 200000)):
                    v = cols.get(key, {}).get(suffix)
                    if isinstance(v, int) and lo <= v / div <= hi:
                        v = round(v / div, 2)
                        if key == "oid_volt":
                            volt = v
                        elif key == "oid_curr":
                            curr = v
                        else:
                            dist = int(v)
                mac = cols.get("oid_mac", {}).get(suffix)
                out.append({"key": key[:64], "name": snmplib.text(cols.get("oid_name", {}).get(suffix))[:64],
                            "serial": fmt_serial(cols.get("oid_serial", {}).get(suffix)),
                            "mac": snmplib.text(mac)[:17] if isinstance(mac, bytes) and len(mac) == 6 else "",
                            "pon": pon_text(cfg["vendor"], suffix), "rx": rx, "tx": tx, "temp": temp, "volt": volt, "curr": curr, "dist": dist,
                            "volt": volt, "curr": curr, "dist": dist,
                            "status": p["map"].get(raw if isinstance(raw, int) else -1, "offline")})
    finally:
        c.close()
    return out


def poll(cur, device, cfg):
    """Read the OLT and update `onus`. Returns (ok, message). Never raises for SNMP trouble."""
    now = datetime.now()
    try:
        found = read_onus(device, cfg)
        if not found:
            raise snmplib.SnmpError("The OLT answered but no ONUs were found. Open SNMP walk on the OLT page to find "
                                    "the right OID, or adjust the ONU interface pattern.")
    except (snmplib.SnmpError, ValueError) as exc:
        cur.execute("UPDATE olt_snmp SET last_poll = %s, last_ok = 0, last_msg = %s WHERE device_id = %s",
                    (now, str(exc)[:255], device["id"]))
        cur.execute("UPDATE net_devices SET last_status = 'down', last_checked = %s WHERE id = %s", (now, device["id"]))
        return False, str(exc)
    cur.execute("SELECT id, onu_key FROM onus WHERE olt_id = %s AND onu_key <> ''", (device["id"],))
    have = {r["onu_key"]: r["id"] for r in cur.fetchall()}
    seen = set()
    online = 0
    for o in found:
        seen.add(o["key"])
        online += o["status"] == "online"
        if o["key"] in have:
            cur.execute("UPDATE onus SET status = %s, rx_dbm = COALESCE(%s, rx_dbm), tx_dbm = COALESCE(%s, tx_dbm), "
                        "temp_c = COALESCE(%s, temp_c), volt_v = COALESCE(%s, volt_v), curr_ma = COALESCE(%s, curr_ma), "
                        "dist_m = COALESCE(%s, dist_m), pon_port = %s, last_seen = %s, "
                        "name = IF(source = 'snmp', %s, name), serial = IF(%s <> '', %s, serial), "
                        "mac = IF(%s <> '', %s, mac) WHERE id = %s",
                        (o["status"], o["rx"], o["tx"], o["temp"], o["volt"], o["curr"], o["dist"], o["pon"], now, o["name"], o["serial"], o["serial"], o["mac"], o["mac"],
                         have[o["key"]]))
        else:
            cur.execute("INSERT INTO onus (olt_id, onu_key, name, serial, mac, pon_port, rx_dbm, tx_dbm, temp_c, volt_v, "
                        "curr_ma, dist_m, status, last_seen, source) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'snmp')",
                        (device["id"], o["key"], o["name"], o["serial"], o["mac"], o["pon"], o["rx"], o["tx"],
                         o["temp"], o["volt"], o["curr"], o["dist"], o["status"], now))
    gone = [i for k, i in have.items() if k not in seen]
    for i in range(0, len(gone), 500):
        part = gone[i:i + 500]
        cur.execute("UPDATE onus SET status = 'offline' WHERE id IN (%s)" % ",".join(["%s"] * len(part)), part)
    cur.execute("INSERT INTO onu_metrics (onu_id, ts, rx_dbm, tx_dbm, temp_c) SELECT id, %s, rx_dbm, tx_dbm, temp_c "
                "FROM onus WHERE olt_id = %s AND source = 'snmp' AND status = 'online' AND last_seen = %s "
                "AND (rx_dbm IS NOT NULL OR tx_dbm IS NOT NULL OR temp_c IS NOT NULL)", (now, device["id"], now))
    msg = "%d ONUs read, %d online" % (len(found), online)
    if p_mode_table(cfg):
        msg += " (RX %d, TX %d, temp %d)" % (sum(o["rx"] is not None for o in found),
                                              sum(o["tx"] is not None for o in found),
                                              sum(o["temp"] is not None for o in found))
    cur.execute("UPDATE olt_snmp SET last_poll = %s, last_ok = 1, last_msg = %s WHERE device_id = %s",
                (now, msg, device["id"]))
    cur.execute("UPDATE net_devices SET last_status = 'up', last_checked = %s WHERE id = %s", (now, device["id"]))
    return True, msg
