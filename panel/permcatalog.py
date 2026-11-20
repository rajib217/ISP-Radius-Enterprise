"""The function-level permission catalogue shown on Users > Roles > Permissions.

Every checkbox has a key like "sub.delete". Saving a role stores each key (table role_funcs) and also works
out the coarse per-module level (table role_perms) that the rest of the panel already enforces:

  * ENDPOINT_FN lists the pages / actions that are checked against one of these keys when someone opens them.
  * A key that is not listed there is only stored (it records the choice but nothing in the panel reads it yet).

Item flags:  v = needs the module at view level (default is edit)   g = the group's "Module" switch: when off,
nothing else in that group counts   n = does not change the module level   o = off unless ticked (default)
d = dangerous, off by default.
"""


def I(key, label, note="", flags=""):
    return {"key": key, "label": label, "note": note, "flags": flags}


def G(title, mod, *cols, wide=False):
    return {"title": title, "mod": mod, "cols": [list(c) for c in cols], "wide": wide}


TABS = [
    {"title": "Home", "icon": "home", "groups": [
        G("Reports & Statistics", None, [I("home.subs", "Subscriber Counter", "", "vn"), I("home.acc", "Accounting Counter", "", "vn"),
                                         I("home.users", "User Counter", "", "vn"), I("home.pred", "Predictions", "", "vn"),
                                         I("home.usage", "Usage", "", "vn")]),
        G("Expired & Expiring", None, [I("home.expiring", "Module", "", "vn")]),
        G("Graph Reports", None, [I("home.g_sales", "Sales/Invoice Reports", "", "vn"), I("home.g_pay", "Payments Reports", "", "vn")]),
        G("Notices", "notices", [I("home.notices", "Module", "", "vg"), I("home.notices_edit", "Add / Edit / Delete")]),
    ]},
    {"title": "My Profile", "icon": "user", "groups": [
        G("My Profile (Reseller)", None,
          [I("prof.module", "Module", "", "vn"), I("prof.edit", "Edit", "", "n"), I("prof.photo", "Change Photo", "", "n"),
           I("prof.password", "Change Password", "", "n"), I("prof.note", "Add Note", "", "n")],
          [I("prof.settings", "Profile Settings", "", "n"),
           I("prof.topup", "Balance Topup", "Allow Resellers To Topup Balance By Payment Gateways", "n"),
           I("prof.inv_module", "Invoice Template Module", "Enable Invoice Template On User Profile", "n"),
           I("prof.inv_settings", "Invoice Settings", "Invoice Template Settings Details On User Profile", "n")], wide=True),
        G("My Packages", "plans", [I("pkg.module", "Module", "", "vg"),
                                   I("pkg.assign", "Assign/Manage", "Allow To Assign New Packages Or Manage Existing Packages"),
                                   I("pkg.profit", "Manage Profit", "Allow To Manage Their Own Package Profit", "n"),
                                   I("pkg.delete", "Delete", "Allow To Delete Existing Packages If There Are No Users/Subscribers")]),
    ]},
    {"title": "User", "icon": "users", "groups": [
        G("User Role", None, [I("usr.role_module", "Module", "", "vn"), I("usr.role_add", "Add", "", "n"), I("usr.role_edit", "Edit", "", "n"),
                              I("usr.role_delete", "Delete", "", "n"),
                              I("usr.role_copy", "Copy", "Allow To Copy & Create Another Role With All The Permissions", "n")]),
        G("Reseller Staff", None, [I("usr.staff", "Module", "Resellers Can Create Their Own Staff", "vn")]),
        G("User Profile", None,
          [I("usr.module", "Module", "", "vn"), I("usr.add", "Add", "Resellers / sub-resellers: create the next tier below them", "no"), I("usr.view", "View", "", "vn"), I("usr.edit", "Edit", "Resellers / sub-resellers: edit, enable and disable the people directly below them", "no"),
           I("usr.delete", "Delete", "Resellers / sub-resellers: delete someone directly below them who has no users, subscribers or balance", "no"), I("usr.export", "Export", "", "n"), I("usr.photo", "Change Photo", "", "n"),
           I("usr.password", "Change Password", "", "no"), I("usr.role", "Change Role", "", "n"), I("usr.note", "Add Note", "", "n")],
          [I("usr.settings", "Profile Settings", "", "n"),
           I("usr.balance", "Manage/Add Balance", "Allow To Add Balance To Users", "n"),
           I("usr.inv_module", "Invoice Template Module", "Enable Invoice Template On User Profile", "n"),
           I("usr.inv_settings", "Invoice Settings", "Invoice Template Settings Details On User Profile", "n"),
           I("usr.transfer", "Transfer Subscribers", "Allow To Transfer Subscribers To Another Users", "n"),
           I("usr.ptype", "Update Profile Type", "Allow To Change User Profile Type (Only Admin Can Change It)", "n"),
           I("usr.limit", "Accounting/Balance Limit", "Allow To Set Users Accounting Transaction Limit", "n")],
          [I("usr.maclock", "MAC Lock", "Allow To Set MAC Lock For Users", "n"), I("usr.iplock", "IP Lock", "Allow To Set IP Lock For Users", "n"),
           I("usr.sublimit", "Subscriber Limitation", "Allow To Set Subscriber Number Limitation For Users", "n"),
           I("usr.reslimit", "Reseller Limitation", "Allow To Set Reseller Number Limitation For Admin", "n"),
           I("usr.subreslimit", "Subreseller Limitation", "Allow To Set Subreseller Number Limitation For Reseller", "n"),
           I("usr.retlimit", "Retailer Limitation", "Allow To Set Retailer Number Limitation For Subreseller", "n"),
           I("usr.switch", "Switch Profile", "Switch To User Profile As Login", "n"),
           I("usr.api", "API Access", "Allow User To Authenticate & Access REST API Endpoints", "no")], wide=True),
        G("User Packages", None, [I("usr.pkg_module", "Module", "", "vn"),
                                  I("usr.pkg_assign", "Assign/Manage", "Allow To Assign New Packages Or Manage Existing Packages", "no"),
                                  I("usr.pkg_delete", "Delete", "Allow To Delete Existing Packages If There Are No Users/Subscribers", "n")]),
    ]},
    {"title": "Subscriber", "icon": "users", "groups": [
        G("Subscriber Profile", "subscribers",
          [I("sub.module", "Module", "", "vg"), I("sub.add", "Add"), I("sub.view", "View", "", "v"), I("sub.edit", "Edit"),
           I("sub.delete", "Delete"), I("sub.import", "Import"), I("sub.export", "Export", "", "v"),
           I("sub.photo", "Change Photo"), I("sub.password", "Change Password"),
           I("sub.username", "Change Username", "Username Changes Are Irreversible. Recommended For Admin Only", "nd"),
           I("sub.toggle_pw", "Toggle Password", "", "v"), I("sub.note", "Add Note")],
          [I("sub.service", "Service Settings"),
           I("sub.activation", "Activation", "Allow To Activate/Renew Subscriber Connection"),
           I("sub.migration", "Package Migration", "Allow To Migrate Subscriber Package"),
           I("sub.mass_activation", "Mass Activation", "Allow To Mass Activate/Renew Subscribers By Current/Specific Package"),
           I("sub.custom_expiry", "Custom Expiry", "Allow To Use Custom Expiry Date On Activation/Renew Modal. Off for everyone except Admin until ticked", "o"),
           I("sub.custom_expiry_nt", "Custom Expiry - Disable Time", "Hide Time Selection, Show Date Only (Requires Custom Expiry Permission)", "n"),
           I("sub.expiry", "Expiration Date", "Allow To Change Subscriber Expiration Date"),
           I("sub.grace", "Grace Period", "Allow To Set Grace Period For Subscribers"),
           I("sub.mid_change", "Mid-Period Package Change", "Allow Changing Subscriber Package Before Current Expiry (Use Migration Otherwise)", "o"),
           I("sub.balance", "Manage/Add Balance", "Allow To Add/Withdraw Subscriber Balance/Payment"),
           I("sub.mass_payment", "Mass Payment", "Allow To Add Payment To Multiple Subscribers", "n"),
           I("sub.mass_delete", "Mass Delete", "Allow To Delete Multiple Subscribers"),
           I("sub.mass_settings", "Mass Settings", "Allow To Change Multiple Subscribers Service Settings")],
          [I("sub.disconnect", "Disconnect", "Allow To Disconnect Subscriber From Internet"), I("sub.document", "Add Document", "", "n"),
           I("sub.attr_add", "Add Attributes", "Allow To Add Radius Attributes"),
           I("sub.attr_view", "Radius Attributes", "Allow To View Radius Attributes List Of Subscriber", "v"),
           I("sub.net_on", "Enable Net"), I("sub.net_off", "Disable Net"), I("sub.profile_on", "Profile Enable"),
           I("sub.profile_off", "Profile Disable"), I("sub.discount", "Discount", "Allow To Set Discount For Subscriber"),
           I("sub.fee_view", "Extra Service Fee - View", "View extra service fees in subscriber profile", "vo"),
           I("sub.fee_add", "Extra Service Fee - Add", "Add extra service fees to subscribers", "o"),
           I("sub.fee_edit", "Extra Service Fee - Edit", "Edit extra service fees", "o"),
           I("sub.fee_delete", "Extra Service Fee - Delete", "Delete extra service fees", "o"),
           I("sub.fee_module", "Extra Service Module", "Access main extra service module to view all services", "vo"),
           I("sub.invoice", "Custom Invoice"), I("sub.revert", "Revert Last Invoice", "Allow To Subscriber Revert Last Invoice", "n"),
           I("sub.login_link", "Login Link", "Allow To View Subscriber's Login Link", "vn"),
           I("sub.reset_mac", "Reset MAC Address", "Allow To Reset Subscriber MAC Address & Force Disconnect")], wide=True),
        G("Service Settings", None,
          [I("svc.sms", "SMS Status", "Allow to view sms status", "vn"), I("svc.email", "Email Status", "Allow to view email status", "vn"),
           I("svc.maclock", "Auto Mac Lock", "", "n"), I("svc.skip_renew", "Skip Auto Renew", "", "n"),
           I("svc.profile_status", "Profile Status", "", "n"), I("svc.conn_type", "Connection Type", "", "n"), I("svc.nas", "NAS", "", "n"),
           I("svc.conn_pw", "Connection Password", "", "n"), I("svc.packages", "Packages", "", "n")],
          [I("svc.ip", "IP Pool/Address", "", "n"), I("svc.lock_vol", "Lock Volume Status", "", "n"),
           I("svc.vol_total", "Total Volume (GB)", "", "n"), I("svc.vol_used", "Used Volume (GB)", "", "n"),
           I("svc.lock_sess", "Lock Session Status", "", "n"), I("svc.sess_total", "Total Session (Min)", "", "n"),
           I("svc.sess_used", "Used Session (Min)", "", "n")], wide=True),
        G("Online Sessions", "online", [I("onl.module", "Module", "", "vg"), I("onl.disconnect", "Disconnect")]),
    ]},
    {"title": "Accounting", "icon": "card", "groups": [
        G("Manage Invoices", "accounting",
          [I("inv.module", "Module", "", "vg"), I("inv.view", "View", "", "v"), I("inv.add", "Add"),
           I("inv.mass_status", "Mass Status", "Allow To Change Multiple Invoice Status", "n"),
           I("inv.mass_delete", "Mass Delete", "Allow To Delete Multiple Invoice", "n"),
           I("inv.reverse", "Reverse", "Reverse Invoice And Update All Related Profit Distributions"),
           I("inv.mark_paid", "Mark As Paid (Due Invoices Only)", "Bulk Mark Multiple Invoices As Paid", "n"),
           I("inv.export", "Export", "", "v")]),
        # not tied to the Accounting module: ticking it opens only the ledger (never Payments, Dues or Expenses).
        # Resellers / sub-resellers / retailers: off until ticked; they then see their own account, their downline and
        # those customers. Staff with Accounting access always see the ledger.
        G("Ledger Reports", None, [I("led.module", "Module", "Resellers / sub-resellers / retailers: their own and their downline's ledger. Staff with Accounting access always have it.", "vgno"),
                                   I("led.export", "Export", "Excel and CSV download of the ledger", "vno")]),
        G("Payment Reports", "accounting", [I("pay.module", "Module", "", "vg"), I("pay.receipt", "View Receipt", "", "v"),
                                            I("pay.export", "Export", "", "v")]),
        G("Cashflow", "accounting", [I("cf.module", "Module", "", "vg"), I("cf.add", "Add"), I("cf.edit", "Edit"), I("cf.delete", "Delete"),
                                     I("cf.export", "Export", "", "v")]),
        G("Cashflow Category", None, [I("cfc.module", "Module", "", "vn"), I("cfc.add", "Add", "", "n"), I("cfc.edit", "Edit", "", "n"),
                                      I("cfc.delete", "Delete", "", "n"), I("cfc.export", "Export", "", "n")]),
    ]},
    {"title": "Network", "icon": "server", "groups": [
        G("Network", None, [I("net.live", "Subscriber Live Graph", "Allow To View NAS Live Graph", "vn"),
                            I("net.iface", "NAS Interface Graph", "View NAS Interface Traffic Graph", "vn")]),
        G("NAS, IP Pool and Graphs", "network", [I("nas.module", "Module", "", "vg"), I("nas.edit", "Add / Edit / Delete")]),
        G("OLT", "network", [I("olt.module", "Module", "", "vg"), I("olt.add", "Add"), I("olt.edit", "Edit"), I("olt.delete", "Delete"),
                             I("olt.sync", "Read ONUs / SNMP Test")]),
        G("ONU", "network", [I("onu.view", "View", "", "vg"), I("onu.edit", "Add / Edit / Link"), I("onu.delete", "Delete")]),
        G("Area", "area", [I("area.module", "Module", "", "vg"), I("area.edit", "Add / Edit / Delete")]),
        G("Department / Zone", "zones", [I("zone.module", "Module", "", "vg"), I("zone.edit", "Add / Edit / Delete")]),
    ]},
    {"title": "Inventory", "icon": "package", "groups": [
        G("Items", "inventory", [I("itm.module", "Module", "", "vg"), I("itm.add", "Add"), I("itm.edit", "Edit"), I("itm.delete", "Delete"),
                                 I("itm.move", "Stock In / Out"), I("itm.export", "Export", "", "v")]),
    ]},
    {"title": "Ticket", "icon": "ticket", "groups": [
        G("Tickets", "tickets", [I("tk.module", "Module", "", "vg"), I("tk.add", "Add"), I("tk.reply", "Reply"), I("tk.update", "Change Status"),
                                 I("tk.delete", "Delete")]),
    ]},
    {"title": "Note", "icon": "note", "groups": [
        G("Notes", "notes", [I("note.module", "Module", "", "vg"), I("note.add", "Add / Edit"), I("note.delete", "Delete")]),
    ]},
    {"title": "Logs", "icon": "file", "groups": [
        G("Activity Logs", "logs", [I("log.act", "Module", "", "vg"), I("log.act_x", "Export", "", "vn")]),
        G("Login Logs", "logs", [I("log.login", "Module", "", "vg"), I("log.login_x", "Export", "", "vn")]),
        G("Session Logs", "logs", [I("log.sess", "Module", "", "vg"), I("log.sess_x", "Export", "", "vn")]),
        G("CoA Requests", None, [I("log.coa", "Module", "", "vn"), I("log.coa_x", "Export", "", "vn")]),
        G("NAT Logs", None, [I("log.nat", "Module", "", "vno"), I("log.nat_x", "Export", "", "vno"), I("log.ipdr", "IPDR Report", "", "vno")]),
        G("PGW Transactions", None, [I("log.pgw", "Module", "", "vn"), I("log.pgw_x", "Export", "", "vn")]),
    ]},
    {"title": "Reports", "icon": "chart", "groups": [
        G("My Profit (Reseller)", None, [I("rep.myprofit", "Module", "Resellers / sub-resellers / retailers: see their own profit and that of the people below them", "vn")]),
        G("Reports", "reports", [I("rep.module", "Module", "", "vg"), I("rep.sales", "Sales Reports", "", "v"),
                                 I("rep.payment", "Payment Reports", "", "v"), I("rep.btrc", "BTRC Reports", "", "vn"),
                                 I("rep.bandwidth", "Bandwidth Usage Reports", "", "vn")]),
    ]},
    {"title": "Documents", "icon": "folder", "groups": [
        G("Documents", "documents", [I("doc.module", "Module", "", "vg"), I("doc.add", "Add"), I("doc.edit", "Edit", "", "n"),
                                     I("doc.delete", "Delete"), I("doc.export", "Export", "", "vn")]),
    ]},
    {"title": "SMS & Email", "icon": "mail", "groups": [
        G("SMS", "sms", [I("sms.module", "Module", "", "vg"), I("sms.send", "Send")]),
        G("Email", "email", [I("mail.module", "Module", "", "vg"), I("mail.send", "Send")]),
    ]},
]

ALL_ITEMS = [(g["mod"], g, it) for t in TABS for g in t["groups"] for c in g["cols"] for it in c]
KEYS = [it["key"] for _, _, it in ALL_ITEMS]
KEY_INFO = {it["key"]: (g["mod"], it["flags"]) for _, g, it in ALL_ITEMS}
GROUP_OF = {it["key"]: g for _, g, it in ALL_ITEMS}
RANK = {"none": 0, "view": 1, "edit": 2}

# page / action  ->  the key (or any one of several keys) a role needs ticked to use it
ENDPOINT_FN = {
    "my_profit": "rep.myprofit", "my_profit_csv": "rep.myprofit",
    "subscriber_new": "sub.add", "subscriber_edit": "sub.edit", "subscriber_delete": "sub.delete",
    "import_csv": "sub.import", "import_sample": "sub.import", "subscribers_export": "sub.export",
    "subscriber_renew": "sub.activation", "subscriber_activate": ("sub.activation", "sub.mass_activation"), "subscriber_activation": ("sub.activation", "sub.mass_activation"),
    "subscriber_lookup": ("sub.activation", "sub.mass_activation"), "subscriber_charge_info": ("sub.activation", "sub.mass_activation"),
    "subscriber_migrate": "sub.migration", "subscriber_balance": "sub.balance", "subscriber_grace": "sub.grace",
    "subscriber_password": "sub.password", "subscriber_photo_save": "sub.photo", "subscriber_invoice": "sub.invoice",
    "subscriber_reset_mac": "sub.reset_mac", "subscriber_settings": "sub.service", "subscriber_kick": "sub.disconnect",
    "subscriber_net": ("sub.net_on", "sub.net_off"), "subscriber_toggle": ("sub.profile_on", "sub.profile_off"),
    "subscribers_bulk": ("sub.mass_settings", "sub.mass_delete", "sub.mass_activation"),
    "my_users": "usr.module", "user_transfer": "usr.balance",
    "my_user_save": ("usr.add", "usr.edit"), "my_user_toggle": "usr.edit", "my_user_password": "usr.password",
    "my_user_packages": "usr.pkg_assign", "my_user_delete": "usr.delete",
    "online_disconnect": "onl.disconnect",
    "payments_add": "inv.add", "payment_delete": "inv.reverse", "payments_export": "inv.export",
    "expense_save": ("cf.add", "cf.edit"), "expense_delete": "cf.delete",
    "olt_new": "olt.add", "olt_edit": "olt.edit", "olt_save": ("olt.add", "olt.edit"), "olt_delete": "olt.delete",
    "olt_sync": "olt.sync", "olt_sync_all": "olt.sync", "olt_test": "olt.sync", "olt_walk": "olt.sync",
    "onu_save": "onu.edit", "onu_map": "onu.edit", "onu_delete": "onu.delete",
    "inventory_save": ("itm.add", "itm.edit"), "inventory_move": "itm.move", "inventory_delete": "itm.delete",
    "ticket_new": "tk.add", "ticket_reply": "tk.reply", "ticket_update": "tk.update", "ticket_delete": "tk.delete",
    "note_save": "note.add", "note_delete": "note.delete",
    "document_upload": "doc.add", "document_delete": "doc.delete",
    "plan_save": "pkg.assign", "plan_delete": "pkg.delete",
    "area_save": "area.edit", "area_delete": "area.edit", "zone_save": "zone.edit", "zone_delete": "zone.edit",
    "sms_compose": "sms.send", "email_compose": "mail.send",
    # routers, pools, graphs: their own "NAS, IP Pool and Graphs" group, so OLT / ONU access does not open them
    "nas_list": "nas.module", "nas_groups": "nas.module", "ip_pools": "nas.module", "mac_vendors": "nas.module",
    "tools": "nas.module", "network_graph": "nas.module", "network_diagram": "nas.module", "devices": "nas.module",
    "nas_save": "nas.edit", "nas_delete": "nas.edit", "nas_ping": "nas.edit", "nas_ping_all": "nas.edit",
    "nas_group_save": "nas.edit", "nas_group_delete": "nas.edit", "ip_pool_save": "nas.edit",
    "ip_pool_delete": "nas.edit", "mac_vendor_import": "nas.edit", "mac_vendor_clear": "nas.edit",
    "tools_reload": "nas.edit", "device_save": "nas.edit", "device_delete": "nas.edit", "device_ping": "nas.edit",
    "device_ping_all": "nas.edit",
    "olts": "olt.module", "onus": "onu.view", "onu_view": "onu.view",
}


def default_on(key, perms, role_is_seller):
    """Whether a key is ticked for a role that has nothing saved yet, from the older per-module levels."""
    mod, flags = KEY_INFO[key]
    if "d" in flags or "o" in flags:
        return False
    if mod is None:
        return bool(role_is_seller)
    need = "view" if "v" in flags else "edit"
    return RANK.get(perms.get(mod, "none"), 0) >= RANK[need]


def module_levels(ticked):
    """Coarse module levels worked out from the ticked keys (what role_perms stores)."""
    out = {}
    for t in TABS:
        for g in t["groups"]:
            if not g["mod"]:
                continue
            items = [it for c in g["cols"] for it in c]
            gate = [it for it in items if "g" in it["flags"]]
            if gate and gate[0]["key"] not in ticked:
                continue
            n = 0
            for it in items:
                if it["key"] in ticked and "n" not in it["flags"]:
                    n = max(n, 1 if "v" in it["flags"] else 2)
            lvl = ("none", "view", "edit")[n]
            if lvl == "none" and gate:
                lvl = "view"
            if RANK[lvl] > RANK.get(out.get(g["mod"], "none"), 0):
                out[g["mod"]] = lvl
    return out


def effective(ticked, perms):
    """The keys a person may really use: ticked AND their module level is high enough."""
    ok = set()
    for k in ticked:
        if k not in KEY_INFO:
            continue
        mod, flags = KEY_INFO[k]
        if mod is None or RANK.get(perms.get(mod, "none"), 0) >= RANK["view" if "v" in flags else "edit"]:
            ok.add(k)
    return ok
