"""The function-level permission catalogue shown on Users > Roles > Permissions.

Every checkbox has a key like "sub.delete". Saving a role stores each key (table role_funcs) and also works
out the coarse per-module level (table role_perms) that the rest of the panel already enforces:

  * ENDPOINT_FN lists the pages / actions that are checked against one of these keys when someone opens them.
  * A key that is not listed there is only stored (it records the choice but nothing in the panel reads it yet).

Item flags:  v = needs the module at view level (default is edit)   g = the group's "Module" switch: when off,
nothing else in that group counts   n = does not change the module level   o = off unless ticked (default)
d = dangerous, off by default   a = ticked by default for every role (office staff too, not only resellers).
"""


def I(key, label, note="", flags=""):
    return {"key": key, "label": label, "note": note, "flags": flags}


def G(title, mod, *cols, wide=False):
    return {"title": title, "mod": mod, "cols": [list(c) for c in cols], "wide": wide}


TABS = [
    {"title": "Home", "icon": "home", "groups": [
        G("Reports & Statistics", None, [I("home.subs", "Subscriber Counter", "", "vna"), I("home.acc", "Accounting Counter", "", "vna"),
                                         I("home.users", "User Counter", "", "vna"), I("home.pred", "Predictions", "", "vna"),
                                         I("home.usage", "Usage", "", "vna")]),
        G("Expired & Expiring", None, [I("home.expiring", "Module", "", "vna")]),
        G("Graph Reports", None, [I("home.g_sales", "Sales/Invoice Reports", "", "vna"), I("home.g_pay", "Payments Reports", "", "vna")]),
        G("Notices", "notices", [I("home.notices", "Module", "", "vg"), I("home.notices_edit", "Add / Edit / Delete")]),
    ]},
    {"title": "My Profile", "icon": "user", "groups": [
        G("My Profile (Reseller)", None,
          [I("prof.module", "Module", "", "vna"), I("prof.edit", "Edit", "", "na"), I("prof.photo", "Change Photo", "", "na"),
           I("prof.password", "Change Password", "", "na"), I("prof.note", "Add Note", "", "n")],
          [I("prof.settings", "Profile Settings", "", "n"),
           I("prof.topup", "Balance Topup", "Allow Resellers To Topup Balance By Payment Gateways", "na"),
           I("prof.inv_module", "Invoice Template Module", "Enable Invoice Template On User Profile", "n"),
           I("prof.inv_settings", "Invoice Settings", "Invoice Template Settings Details On User Profile", "n")], wide=True),
        G("My Packages", "plans", [I("pkg.module", "Module", "", "vg"),
                                   I("pkg.assign", "Assign/Manage", "Allow To Assign New Packages Or Manage Existing Packages"),
                                   I("pkg.profit", "Manage Profit", "Allow To Set The Subscriber / Sub-reseller Profit For The People Below Them", "vna"),
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
           I("usr.transfer", "Transfer Subscribers", "Move Customers To Another Reseller (Or The Office). Off Unless Ticked", "no"),
           I("usr.ptype", "Update Profile Type", "Allow To Change User Profile Type (Only Admin Can Change It)", "n"),
           I("usr.limit", "Accounting/Balance Limit", "Allow To Set Users Accounting Transaction Limit", "n")],
          [I("usr.maclock", "MAC Lock", "Allow To Set MAC Lock For Users", "n"), I("usr.iplock", "IP Lock", "Allow To Set IP Lock For Users", "n"),
           I("usr.sublimit", "Subscriber Limitation", "Set The Most Customers A Reseller / Sub-reseller / Retailer May Have. Off Unless Ticked", "no"),
           I("usr.reslimit", "Reseller Limitation", "Allow To Set Reseller Number Limitation For Admin", "n"),
           I("usr.subreslimit", "Subreseller Limitation", "Allow To Set Subreseller Number Limitation For Reseller", "n"),
           I("usr.retlimit", "Retailer Limitation", "Allow To Set Retailer Number Limitation For Subreseller", "n"),
           I("usr.switch", "Switch Profile", "Switch To User Profile As Login", "n"),
           I("usr.api", "API Access", "Allow User To Authenticate & Access REST API Endpoints", "no")], wide=True),
        G("User Packages", None, [I("usr.pkg_module", "Module", "", "vn"),
                                  I("usr.pkg_assign", "Assign/Manage", "Allow To Assign New Packages Or Manage Existing Packages", "no"),
                                  I("usr.pkg_delete", "Delete", "Remove A Package From A User When None Of Their Customers Uses It. Off Unless Ticked", "no")]),
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
           I("sub.custom_expiry_nt", "Custom Expiry - Disable Time", "Hide Time Selection, Show Date Only (Requires Custom Expiry Permission)", "no"),
           I("sub.expiry", "Expiration Date", "Allow To Change Subscriber Expiration Date"),
           I("sub.grace", "Grace Period", "Allow To Set Grace Period For Subscribers"),
           I("sub.mid_change", "Mid-Period Package Change", "Allow Changing Subscriber Package Before Current Expiry (Use Migration Otherwise)", "o"),
           I("sub.monthly_renew", "Monthly Renew", "Resellers: A 30-Day Package Renews To The Same Date Next Month (31 Or 28 Days Alike) For One Month's Price", "no"),
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
           I("sub.invoice", "Custom Invoice"), I("sub.revert", "Revert Last Invoice", "Undo The Customer's Last Renewal Within 48 Hours (Expiry, Package, Balances). Off Unless Ticked", "no"),
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
        G("bKash Payments", None, [I("bkp.module", "Module", "Resellers / sub-resellers / retailers: their own and their downline's bKash checkouts. Staff with Accounting access always have it.", "vgno"),
                                   I("bkp.export", "Export", "Excel and CSV download of bKash payments", "vno")]),
        # on for resellers / sub-resellers / retailers until unticked: each sees only their own (and downline's) customers
        G("bKash SMS Payments", None, [I("bks.module", "Module", "Resellers / sub-resellers / retailers: the bKash SMS payments of their own and their downline's customers. Staff with Accounting access always have it.", "vgn"),
                                       I("bks.export", "Export", "Excel and CSV download of bKash SMS payments", "vno")]),
        G("Payment Reports", "accounting", [I("pay.module", "Module", "", "vg"), I("pay.receipt", "View Receipt", "", "v"),
                                            I("pay.export", "Export", "", "v")]),
        G("Cashflow", "accounting", [I("cf.module", "Module", "", "vg"), I("cf.add", "Add"), I("cf.edit", "Edit"), I("cf.delete", "Delete"),
                                     I("cf.export", "Export", "", "v")]),
        G("Cashflow Category", None, [I("cfc.module", "Module", "", "vn"), I("cfc.add", "Add", "", "n"), I("cfc.edit", "Edit", "", "n"),
                                      I("cfc.delete", "Delete", "", "n"), I("cfc.export", "Export", "", "n")]),
    ]},
    {"title": "Network", "icon": "server", "groups": [
        G("Network", None, [I("net.live", "Subscriber Live Graph", "Allow To View NAS Live Graph", "vna"),
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
        G("Reports", "reports", [I("rep.module", "Module", "", "vg"), I("rep.sales", "Sales Reports", "", "v"),
                                 I("rep.payment", "Payment Reports", "", "v"), I("rep.btrc", "BTRC Reports", "Every customer's name, phone and address for BTRC. Office staff only, off until ticked", "vno"),
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
# the group's "Module" switch (flag g) for every other key of that group: switched off, the whole group is off
GATE_OF = {}
for _g in {id(g): g for _, g, _it in ALL_ITEMS}.values():
    _items = [it for c in _g["cols"] for it in c]
    _gate = next((it["key"] for it in _items if "g" in it["flags"]), None)
    if _gate:
        for it in _items:
            if it["key"] != _gate:
                GATE_OF[it["key"]] = _gate
RANK = {"none": 0, "view": 1, "edit": 2}

# page / action  ->  the key (or any one of several keys) a role needs ticked to use it
ENDPOINT_FN = {
    "subscriber_new": "sub.add", "subscriber_edit": "sub.edit", "subscriber_delete": "sub.delete",
    "import_csv": "sub.import", "import_sample": "sub.import", "subscribers_export": "sub.export",
    "subscriber_renew": "sub.activation", "subscriber_activate": ("sub.activation", "sub.mass_activation"), "subscriber_activation": ("sub.activation", "sub.mass_activation"),
    "subscriber_lookup": ("sub.activation", "sub.mass_activation"), "subscriber_charge_info": ("sub.activation", "sub.mass_activation"),
    "subscriber_migrate": "sub.migration", "subscriber_balance": "sub.balance", "subscriber_grace": "sub.grace",
    "subscriber_password": "sub.password", "subscriber_photo_save": "sub.photo", "subscriber_invoice": "sub.invoice",
    "subscriber_reset_mac": "sub.reset_mac", "subscriber_settings": "sub.service", "subscriber_kick": "sub.disconnect",
    "subscriber_net": ("sub.net_on", "sub.net_off"), "subscriber_toggle": ("sub.profile_on", "sub.profile_off"),
    "subscribers_bulk": ("sub.mass_settings", "sub.mass_delete", "sub.mass_activation"),
    "subscribers_mass_activate": "sub.mass_activation", "subscribers_mass_payment": "sub.mass_payment",
    "my_users": "usr.module", "user_transfer": "usr.balance",
    "my_user_save": ("usr.add", "usr.edit"), "my_user_toggle": "usr.edit", "my_user_password": "usr.password",
    "my_user_packages": "usr.pkg_assign", "my_user_delete": "usr.delete",
    "online_disconnect": "onl.disconnect",
    "payments_add": "inv.add", "payment_delete": "inv.reverse", "payments_export": "inv.export",
    "cashflow": "cf.module", "expenses": "cf.module", "cashflow_save": ("cf.add", "cf.edit"), "cashflow_delete": "cf.delete",
    "cashflow_csv": "cf.export", "cashflow_topup_correct": "cf.edit", "cashflow_xlsx": "cf.export",
    "cashflow_categories": "cfc.module", "cashflow_category_save": ("cfc.add", "cfc.edit"), "cashflow_category_delete": "cfc.delete",
    "olt_new": "olt.add", "olt_edit": "olt.edit", "olt_save": ("olt.add", "olt.edit"), "olt_delete": "olt.delete",
    "olt_sync": "olt.sync", "olt_sync_all": "olt.sync", "olt_test": "olt.sync", "olt_walk": "olt.sync",
    "onu_save": "onu.edit", "onu_map": "onu.edit", "onu_delete": "onu.delete",
    "inventory_save": ("itm.add", "itm.edit"), "inventory_move": "itm.move", "inventory_delete": "itm.delete",
    "ticket_new": "tk.add", "ticket_reply": "tk.reply", "ticket_update": "tk.update", "ticket_delete": "tk.delete",
    "subscriber_login_link": "sub.login_link", "subscriber_revert": "sub.revert",
    "follow_ups": "sub.note", "call_add": "sub.note", "call_done": "sub.note",
    "ticket_lookup": "tk.add", "tickets_bulk": "tk.update", "ticket_move": "tk.update", "ticket_to_office": "tk.reply",
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
    # Home, My Profile, Payments / receipts, reports, extra service fees, live graph (pages that were open to everyone
    # with the module until these ticks were checked)
    "payment_add": "inv.add", "payments": ("pay.module", "inv.view"),
    "receipt": ("pay.receipt", "inv.view", "sub.activation", "sub.invoice", "sub.balance"),
    "payments_export": ("inv.export", "pay.export"),
    "profile": "prof.module", "add_balance": "prof.topup",
    "charges": "sub.fee_module", "charge_save": ("sub.fee_add", "sub.fee_edit"), "charge_paid": "sub.fee_edit",
    "charge_delete": "sub.fee_delete", "subscriber_rate": "net.live",
    "collection_csv": "rep.payment", "profit_csv": "rep.sales", "btrc_report": "rep.btrc", "btrc_xlsx": "rep.btrc",
}

# Ticks that only started to count when the permission check of Nov 2026 wired them in. Before that, everyone with the
# module (or everyone) had these pages and buttons, so the first time the new code runs each role that could use one
# gets it ticked (core.upgrade_role_funcs): nothing changes for anyone until an administrator unticks it.
NOW_ENFORCED = ("home.subs", "home.acc", "home.users", "home.pred", "home.usage", "home.expiring", "home.g_sales",
                "home.g_pay", "prof.module", "prof.edit", "prof.password", "prof.topup", "pkg.module", "sub.toggle_pw",
                "sub.attr_view", "sub.note", "sub.document", "sub.fee_view", "sub.fee_module", "sub.fee_add",
                "sub.fee_edit", "sub.fee_delete", "inv.add", "inv.view", "pay.module", "pay.receipt", "pay.export",
                "net.live", "rep.sales", "rep.payment", "rep.bandwidth")
# ...and ticks that start to count but must not open anything by themselves: whatever was saved before (when they did
# nothing) is cleared once, so only a tick given from now on opens them
NOW_CLEARED = ("rep.btrc",)


# Built-in roles whose subscriber ticks do not simply follow the module level: on these key prefixes only the listed
# keys start ticked (Users > Roles can still change them). Technician: see customers, add new ones (they start
# expired: no activation), fix the address / map location, reset the MAC, disconnect, see who is online.
ROLE_KEYS = {
    "technician": (("sub.", "svc.", "onl."), frozenset((
        "sub.module", "sub.view", "sub.add", "sub.edit", "sub.disconnect", "sub.reset_mac",
        "onl.module", "onl.disconnect"))),
}
# ...and ticks such a role never gets, even when ticked on Users > Roles
ROLE_NEVER = {
    "technician": frozenset((
        "sub.password", "svc.conn_pw", "sub.username", "sub.delete", "sub.mass_delete", "sub.import", "sub.export",
        "sub.activation", "sub.mass_activation", "sub.migration", "sub.custom_expiry", "sub.expiry", "sub.grace",
        "sub.mid_change", "sub.balance", "sub.mass_payment", "sub.discount", "sub.invoice", "sub.revert",
        "sub.fee_add", "sub.fee_edit", "sub.fee_delete", "sub.fee_module", "sub.fee_view", "svc.packages",
        "home.acc", "home.pred", "home.usage", "home.g_sales", "home.g_pay", "pkg.module")),
}


def role_locked_keys(role, locked_modules=()):
    """Ticks this role can never have (ROLE_NEVER, and every key of a module locked for it): shown greyed out."""
    out = set(ROLE_NEVER.get(role, ()))
    for k, (mod, _flags) in KEY_INFO.items():
        if mod and mod in locked_modules:
            out.add(k)
    return out


def default_on(key, perms, role_is_seller, role=None):
    """Whether a key is ticked for a role that has nothing saved yet, from the older per-module levels."""
    mod, flags = KEY_INFO[key]
    special = ROLE_KEYS.get(role)
    if (special and key.startswith(special[0]) and key not in special[1]) or key in ROLE_NEVER.get(role, ()):
        return False
    if "d" in flags or "o" in flags:
        return False
    if "a" in flags:
        return True
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
    """The keys a person may really use: ticked, their group's Module switch ticked, AND their module level is high
    enough (e.g. Manage Invoices > View does nothing while Manage Invoices > Module is off)."""
    ok = set()
    for k in ticked:
        if k not in KEY_INFO:
            continue
        if k in GATE_OF and GATE_OF[k] not in ticked:
            continue
        mod, flags = KEY_INFO[k]
        if mod is None or RANK.get(perms.get(mod, "none"), 0) >= RANK["view" if "v" in flags else "edit"]:
            ok.add(k)
    return ok
