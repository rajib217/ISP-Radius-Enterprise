"""Sidebar menu: groups and links, filtered by what the signed-in user may open."""
from flask import g, request, url_for

import core


def _i(label, endpoint, match=None, args=None, none_of=None, module=None, admin=False, tier=None, fn=None, own=None, allow=None, office=False):
    # allow: the page decides who may open it (mod_coa.allowed), so the menu asks the same function
    # own: a function key that opens this one page on its own, even without the group's module (Ledger Reports and
    # bKash Payments: sellers get them only through that tick, staff through it or the Accounting module)
    return {"office": office, "allow": allow, "own": own, "fn": fn, "tier": tier, "admin": admin, "label": label, "endpoint": endpoint, "match": match or [endpoint], "args": args or {},
            "none_of": none_of or [], "module": module}


def _g(key, label, icon, items=None, endpoint=None, match=None, module=None, admin=False, sellers=False, fn=None, office=False):
    # office: never shown to resellers / sub-resellers / retailers (administrators and office staff, by the items' ticks)
    return {"office": office, "sellers": sellers, "fn": fn, "key": key, "label": label, "icon": icon, "items": items, "endpoint": endpoint,
            "match": match or ([endpoint] if endpoint else []), "module": module, "admin": admin}


STRUCTURE = [
    _g("home", "Home", "home", endpoint="dashboard"),
    _g("profile", "My Profile", "user", endpoint="profile", match=["profile", "account"], fn="prof.module"),
    _g("mypkg", "My Packages", "package", endpoint="my_packages"),
    _g("myusers", "My Users", "users", endpoint="my_users", match=["my_users", "user_transfer", "my_user_save", "my_user_packages", "my_user_delete"], sellers=True, fn="usr.module"),
    _g("mystmt", "Monthly Statement", "file", endpoint="statements", match=["statements"], sellers=True, fn="prof.module"),
    _g("users", "Users", "users", office=True, items=[
        _i("Roles", "roles", ["roles", "role_permissions"], fn="usr.role_module"),
        _i("Users", "admins", ["admins", "admin_save", "admin_toggle", "admin_delete", "admin_view", "admin_packages"], none_of=["group"], fn="usr.module"),
        _i("Staffs", "admins", args={"group": "staff"}, fn="usr.module"),
        _i("Resellers", "admins", args={"group": "reseller"}, tier="reseller", fn="usr.module"),
        _i("Sub-resellers", "admins", args={"group": "subreseller"}, tier="subreseller", fn="usr.module"),
        _i("Retailers", "admins", args={"group": "retailer"}, tier="retailer", fn="usr.module"),
        _i("Monthly statement", "statements", ["statements"], fn="usr.view"),
    ]),
    _g("package", "Package", "layers", module="plans", items=[
        _i("Policy", "policy", ["policy", "policy_save", "policy_delete"]),
        _i("Packages", "plans", ["plans", "plan_save", "plan_delete", "plan_new", "plan_edit"]),
        _i("Allocation", "allocation", admin=True),
        _i("Tax/Extra Fee", "fees", ["fees", "fee_save", "fee_delete"]),
        _i("Package report", "plan_report"),
    ]),
    _g("subs", "Subscribers", "users3", module="subscribers", items=[
        _i("All", "subscribers", ["subscribers", "subscriber_view", "subscriber_edit", "subscriber_new", "subscriber_activation"], none_of=["status"]),
        _i("Online", "subscribers", args={"status": "online"}),
        _i("Stale", "subscribers", args={"status": "stale"}),
        _i("Offline", "subscribers", args={"status": "offline"}),
        _i("Expired", "subscribers", args={"status": "expired"}),
        _i("Expired Online", "subscribers", args={"status": "expired_online"}),
        _i("Profile Disabled Online", "subscribers", args={"status": "profile_disabled_online"}),
        _i("Internet Disabled Online", "subscribers", args={"status": "internet_disabled_online"}),
        _i("Internet Disabled", "subscribers", args={"status": "internet_disabled"}),
        _i("Expiring 1 Day", "subscribers", args={"status": "exp1"}),
        _i("Expiring 3 Days", "subscribers", args={"status": "exp3"}),
        _i("Expiring 1 Week", "subscribers", args={"status": "exp7"}),
        _i("Expiring 2 Weeks", "subscribers", args={"status": "exp14"}),
        _i("Expiring 4 Weeks", "subscribers", args={"status": "exp28"}),
        _i("MAC Binded", "subscribers", args={"status": "mac"}),
        _i("Grace Period On", "subscribers", args={"status": "grace"}),
        _i("Recent Auto Renew", "subscribers", args={"status": "renewed"}),
        _i("Extra Service Fees", "charges", ["charges", "charge_save", "charge_paid", "charge_delete"], fn="sub.fee_module"),
        _i("Suspended", "subscribers", args={"status": "suspended"}),
        _i("Follow-ups", "follow_ups", ["follow_ups"]),
        _i("Map", "customer_map", ["customer_map"]),
        _i("Online sessions", "online", module="online"),
        _i("Import / Export", "import_csv", ["import_csv"]),
    ]),
    _g("acct", "Accounting", "dollar", module="accounting", items=[
        _i("Payments", "payments", ["payments", "receipt", "payment_add"]),
        _i("Receive payment", "receive"),
        _i("Cashflow", "cashflow", ["cashflow", "cashflow_categories", "expenses"], fn="cf.module"),
        _i("Income & expense", "acct_summary"),
        _i("Daily report", "daily_report", ["daily_report"], office=True),
        _i("Dues", "dues"),
        _i("Ledger Reports", "ledger", ["ledger", "ledger_csv", "ledger_xlsx"], own="led.module"),
        _i("bKash Payments", "bkash_payments", ["bkash_payments", "bkash_payments_csv", "bkash_payments_xlsx"], own="bkp.module"),
        _i("bKash SMS Payments", "bksms_payments", ["bksms_payments", "bksms_payments_csv", "bksms_payments_xlsx"], own="bks.module"),
    ]),
    _g("inv", "Inventory", "archive", module="inventory", items=[
        _i("Items", "inventory", none_of=["low"]),
        _i("Low stock", "inventory", args={"low": "1"}),
        _i("Stock log", "inventory_moves"),
    ]),
    _g("net", "Network", "globe", module="network", items=[
        _i("NAS", "nas_list", ["nas_list", "nas_save", "nas_delete", "nas_ping", "nas_ping_all"], fn="nas.module"),
        _i("NAS Group", "nas_groups", ["nas_groups", "nas_group_save", "nas_group_delete"], fn="nas.module"),
        _i("IP Pool", "ip_pools", ["ip_pools", "ip_pool_save", "ip_pool_delete"], fn="nas.module"),
        _i("MAC Vendor", "mac_vendors", ["mac_vendors", "mac_vendor_import", "mac_vendor_clear"], fn="nas.module"),
        _i("OLTs", "olts", ["olts", "olt_new", "olt_edit", "olt_save", "olt_delete", "olt_sync", "olt_sync_all", "olt_test", "olt_walk"], fn="olt.module"),
        _i("ONUs", "onus", ["onus", "onu_save", "onu_delete", "onu_view", "onu_map"], fn="onu.view"),
        _i("Graph", "network_graph", fn="nas.module"),
        _i("Diagram", "network_diagram", fn="nas.module"),
        _i("Test login", "tools", ["tools", "tools_reload"], fn="nas.module"),
        _i("CoA Request Log", "coa_log", allow="coa"),
    ]),
    _g("area", "Area", "pin", module="area", items=[
        _i("Areas", "areas"),
        _i("Area report", "area_report"),
    ]),
    _g("tickets", "Tickets", "ticket", module="tickets", items=[
        _i("All tickets", "tickets", ["tickets", "ticket_view"], none_of=["status", "mine", "view"]),
        _i("Open", "tickets", args={"status": "open"}),
        _i("Board", "ticket_board", ["ticket_board"], office=True),
        _i("My jobs", "jobs", ["jobs"], office=True),
        _i("My tickets", "tickets", args={"mine": "1"}, office=True),
        _i("New ticket", "ticket_new"),
        _i("Outages", "outages", ["outages"], office=True),
        _i("Report", "ticket_report", ["ticket_report"], office=True),
        _i("Settings", "ticket_settings", ["ticket_settings"], allow="ticket_admin"),
    ]),
    _g("email", "Email", "mail", module="email", items=[
        _i("Compose", "email_compose"),
        _i("Templates", "templates_page", args={"kind": "email"}),
        _i("Sent log", "messages", args={"channel": "email"}),
    ]),
    _g("sms", "SMS", "phone", module="sms", items=[
        _i("Compose", "sms_compose"),
        _i("Templates", "templates_page", args={"kind": "sms"}),
        _i("Sent log", "messages", args={"channel": "sms"}),
    ]),
    _g("appnotify", "App Notification", "bell", endpoint="notify_send", module="subscribers",
       match=["notify_send", "notify_batch_delete"]),
    _g("logs", "Logs", "file", module="logs", items=[
        _i("Logs & System Health", "noc", ["noc", "noc_settings"]),
        _i("Login attempts", "logs", none_of=["tab"]),
        _i("Panel activity", "logs", args={"tab": "activity"}),
        _i("Sessions", "logs_sessions"),
        _i("Messages", "messages", none_of=["channel"], module=("sms", "email")),
    ]),
    _g("notes", "Notes", "note", endpoint="notes", module="notes"),
    _g("notices", "Notices", "alert", endpoint="notices", module="notices", match=["notices", "notice_save", "notice_delete"]),
    _g("reports", "Reports", "chart", endpoint="reports", module="reports", fn=("rep.bandwidth", "rep.payment", "rep.sales")),
    _g("docs", "Documents", "folder", endpoint="documents", module="documents"),
    _g("zones", "Department/Zone", "briefcase", endpoint="org", module="zones"),
    _g("settings", "Settings", "sliders", endpoint="settings", admin=True,
       match=["settings", "settings_email", "settings_sms", "settings_automation", "settings_software", "settings_logs", "settings_modules", "settings_gateway", "settings_subscriber", "settings_subscriber_portal", "settings_user", "settings_partners", "settings_backup", "settings_expired"]),
]


# pages under the Subscribers menu outside its own items (a subscriber's profile and its sub-pages)
WIDE_EXTRA = ("subscriber_edit", "subscriber_new", "subscriber_activation", "subscriber_view", "diagnose_page")


def full_width():
    """Subscribers menu pages (every item and the pages under them) use the whole width, with the sidebar open too."""
    ep = request.endpoint or ""
    grp = next(gr for gr in STRUCTURE if gr["key"] == "subs")
    return ep in WIDE_EXTRA or any(ep in it["match"] for it in grp["items"])


def _active(item, endpoint):
    if endpoint not in item["match"]:
        return False
    if any(request.args.get(k) != v for k, v in item["args"].items()):
        return False
    return not any(request.args.get(k) for k in item["none_of"])


def _allowed(key):
    if key == "coa":
        import mod_coa
        return mod_coa.allowed()
    if key == "ticket_admin":
        return g.me["role"] in ("admin", "manager")
    return False


def build():
    endpoint = request.endpoint or ""
    is_admin = g.me["role"] == "admin"
    seller = g.me["role"] in core.SELLER_ROLES
    out = []
    for grp in STRUCTURE:
        if grp["admin"] and not is_admin:
            continue
        if grp.get("office") and seller:
            continue
        if grp["fn"] and not grp["sellers"] and not any(core.can_fn(k) for k in (
                (grp["fn"],) if isinstance(grp["fn"], str) else grp["fn"])):
            continue
        if grp["key"] == "mypkg" and (seller or g.me["role"] in core.ROLE_LOCKS) and not core.can_fn("pkg.module"):
            continue                      # Users > Roles > My Packages > Module
        if grp["sellers"] and (g.me["role"] not in core.SELLER_ROLES or not core.can_fn(grp["fn"])):
            continue
        mod_ok = not grp["module"] or core.can(grp["module"])
        if not mod_ok and not any(it.get("own") or it.get("allow") for it in grp["items"] or []):
            continue
        if grp["module"] == "plans" and g.me["role"] in core.SELLER_ROLES:
            continue                      # sellers see their packages under My Packages only
        node = {"key": grp["key"], "label": grp["label"], "icon": grp["icon"]}
        if grp["items"] is not None:
            kids = []
            off = core.tiers_off()
            for it in grp["items"]:
                if it.get("tier") in off:
                    continue
                if it.get("office") and seller:
                    continue
                if it.get("allow"):
                    if not _allowed(it["allow"]):
                        continue
                    kids.append({"label": it["label"], "url": url_for(it["endpoint"], **it["args"]), "active": _active(it, endpoint)})
                    continue
                if it.get("own"):              # same rule as the page itself (mod_ledger / mod_bkash_report _allowed)
                    if not (core.can_fn(it["own"]) if seller else (mod_ok or core.can_fn(it["own"]))):
                        continue
                elif not mod_ok:
                    continue
                if it["module"] and not core.can(it["module"]):
                    continue
                if it["fn"] and not core.can_fn(it["fn"]):
                    continue
                if it["admin"] and not is_admin:
                    continue
                kids.append({"label": core.tier_name(it["tier"], True) if it.get("tier") else it["label"], "url": url_for(it["endpoint"], **it["args"]),
                             "active": _active(it, endpoint)})
            if not kids:
                continue
            node["children"] = kids
            node["active"] = node["open"] = any(k["active"] for k in kids)
        else:
            node["url"] = url_for(grp["endpoint"])
            node["active"] = endpoint in grp["match"]
        out.append(node)
    return out


# Phone bottom navigation (DESIGN.md B6): four shortcuts picked from build() output, so the
# permission filtering above is never bypassed, plus a "More" button that opens the full menu.
_BOTTOM = {
    "staff": [("dashboard", "Home", "home"), ("subscribers", "Subscribers", "users3"),
              ("online", "Online", "activity"), ("payments", "Payments", "dollar")],
    "tech": [("dashboard", "Home", "home"), ("jobs", "My jobs", "ticket"),
             ("customer_map", "Map", "pin"), ("subscribers", "Customers", "users3")],
    "seller": [("dashboard", "Home", "home"), ("subscribers", "Subscribers", "users3"),
               ("subscriber_activation", "Activate", "bolt"), ("add_balance", "Balance", "wallet")],
}


def bottom_nav(nodes=None):
    """Up to 4 {label, url, icon, active} shortcuts for the phone bottom bar, from build() nodes."""
    nodes = build() if nodes is None else nodes
    if not nodes:
        return []
    by_url, group_active = {}, {}
    for node in nodes:
        if node.get("url"):
            by_url[node["url"]] = node.get("active", False)
        for kid in node.get("children") or []:
            by_url.setdefault(kid["url"], kid.get("active", False))
            if kid.get("active"):
                group_active[node["key"]] = True
    # two seller shortcuts are pages, not menu items; gate them exactly as the dashboard does
    extra_ok = {"subscriber_activation": lambda: core.can("subscribers", "edit"),
                "add_balance": lambda: g.me["role"] in core.SELLER_ROLES}
    want = _BOTTOM["seller" if g.me["role"] in core.SELLER_ROLES else ("tech" if g.me["role"] == "technician" else "staff")]
    out = []
    for endpoint, label, icon in want:
        try:
            url = url_for(endpoint)
        except Exception:
            continue
        if url in by_url:
            active = by_url[url] or (endpoint == "subscribers" and group_active.get("subs", False))
        elif endpoint in extra_ok and extra_ok[endpoint]():
            active = (request.endpoint or "") == endpoint
        else:
            continue                       # not in this user's menu: skip the slot, never invent a link
        out.append({"label": label, "url": url, "icon": icon, "active": bool(active)})
    return out[:4]
