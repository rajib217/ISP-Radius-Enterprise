"""Sidebar menu: groups and links, filtered by what the signed-in user may open."""
from flask import g, request, url_for

import core


def _i(label, endpoint, match=None, args=None, none_of=None, module=None, admin=False, tier=None, fn=None):
    return {"fn": fn, "tier": tier, "admin": admin, "label": label, "endpoint": endpoint, "match": match or [endpoint], "args": args or {},
            "none_of": none_of or [], "module": module}


def _g(key, label, icon, items=None, endpoint=None, match=None, module=None, admin=False, sellers=False, fn=None):
    return {"sellers": sellers, "fn": fn, "key": key, "label": label, "icon": icon, "items": items, "endpoint": endpoint,
            "match": match or ([endpoint] if endpoint else []), "module": module, "admin": admin}


STRUCTURE = [
    _g("home", "Home", "home", endpoint="dashboard"),
    _g("profile", "My Profile", "user", endpoint="profile", match=["profile", "account"]),
    _g("mypkg", "My Packages", "package", endpoint="my_packages"),
    _g("myusers", "My Users", "users", endpoint="my_users", match=["my_users", "user_transfer", "my_user_save", "my_user_packages", "my_user_delete"], sellers=True, fn="usr.module"),
    _g("myprofit", "My Profit", "chart", endpoint="my_profit", match=["my_profit", "my_profit_csv"], sellers=True, fn="rep.myprofit"),
    _g("myledger", "Ledger", "file", endpoint="ledger", match=["ledger", "ledger_csv", "ledger_xlsx"], sellers=True, fn="led.mine"),
    _g("users", "Users", "users", admin=True, items=[
        _i("Roles", "roles"),
        _i("Users", "admins", ["admins", "admin_save", "admin_toggle", "admin_delete"], none_of=["group"]),
        _i("Staffs", "admins", args={"group": "staff"}),
        _i("Resellers", "admins", args={"group": "reseller"}, tier="reseller"),
        _i("Sub-resellers", "admins", args={"group": "subreseller"}, tier="subreseller"),
        _i("Retailers", "admins", args={"group": "retailer"}, tier="retailer"),
    ]),
    _g("package", "Package", "layers", module="plans", items=[
        _i("Policy", "policy", ["policy", "policy_save", "policy_delete"]),
        _i("Packages", "plans", ["plans", "plan_save", "plan_delete", "plan_new", "plan_edit"]),
        _i("Allocation", "allocation", admin=True),
        _i("Tax/Extra Fee", "fees", ["fees", "fee_save", "fee_delete"]),
        _i("Package report", "plan_report"),
    ]),
    _g("subs", "Subscribers", "users", module="subscribers", items=[
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
        _i("Extra Service Fees", "charges", ["charges", "charge_save", "charge_paid", "charge_delete"]),
        _i("Suspended", "subscribers", args={"status": "suspended"}),
        _i("Online sessions", "online", module="online"),
        _i("Import / Export", "import_csv", ["import_csv"]),
    ]),
    _g("acct", "Accounting", "dollar", module="accounting", items=[
        _i("Payments", "payments", ["payments", "receipt", "payment_add"]),
        _i("Receive payment", "receive"),
        _i("Expenses", "expenses"),
        _i("Income & expense", "acct_summary"),
        _i("Dues", "dues"),
        _i("Ledger Reports", "ledger", ["ledger", "ledger_csv", "ledger_xlsx"], fn="led.view"),
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
    ]),
    _g("area", "Area", "pin", module="area", items=[
        _i("Areas", "areas"),
        _i("Area report", "area_report"),
    ]),
    _g("tickets", "Tickets", "ticket", module="tickets", items=[
        _i("All tickets", "tickets", ["tickets", "ticket_view"], none_of=["status", "mine"]),
        _i("Open", "tickets", args={"status": "open"}),
        _i("My tickets", "tickets", args={"mine": "1"}),
        _i("New ticket", "ticket_new"),
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
    _g("logs", "Logs", "file", module="logs", items=[
        _i("Logs & System Health", "noc", ["noc", "noc_settings"]),
        _i("Login attempts", "logs", none_of=["tab"]),
        _i("Panel activity", "logs", args={"tab": "activity"}),
        _i("Sessions", "logs_sessions"),
        _i("Messages", "messages", none_of=["channel"], module=("sms", "email")),
    ]),
    _g("notes", "Notes", "note", endpoint="notes", module="notes"),
    _g("notices", "Notices", "alert", endpoint="notices", module="notices", match=["notices", "notice_save", "notice_delete"]),
    _g("reports", "Reports", "chart", endpoint="reports", module="reports"),
    _g("docs", "Documents", "folder", endpoint="documents", module="documents"),
    _g("zones", "Department/Zone", "briefcase", endpoint="org", module="zones"),
    _g("settings", "Settings", "sliders", endpoint="settings", admin=True,
       match=["settings", "settings_email", "settings_sms", "settings_automation", "settings_software", "settings_logs", "settings_modules", "settings_gateway", "settings_subscriber", "settings_user", "settings_partners", "settings_backup"]),
]


def _active(item, endpoint):
    if endpoint not in item["match"]:
        return False
    if any(request.args.get(k) != v for k, v in item["args"].items()):
        return False
    return not any(request.args.get(k) for k in item["none_of"])


def build():
    endpoint = request.endpoint or ""
    is_admin = g.me["role"] == "admin"
    out = []
    for grp in STRUCTURE:
        if grp["admin"] and not is_admin:
            continue
        if grp["sellers"] and (g.me["role"] not in core.SELLER_ROLES or not core.can_fn(grp["fn"])):
            continue
        if grp["module"] and not core.can(grp["module"]):
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
