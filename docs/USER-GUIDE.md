# ISP RADIUS Panel

FreeRADIUS + MariaDB + a web admin panel for managing PPPoE subscribers, built for Ubuntu Server 24.04 and sized for 20,000+ customers.

**What you get**

- FreeRADIUS configured to read users, plans and routers from MariaDB (PAP, CHAP and MS-CHAPv2 all work)
- A web panel with automatic HTTPS (Caddy + Let's Encrypt), light and dark themes, usable on a phone
- Dashboard with live online count, 24-hour curve, plan mix, routers, expiring accounts
- Subscribers: search, filters, bulk suspend / activate / change plan / add days, CSV import and export, renew with payment record
- Online sessions with one-click disconnect (RADIUS Disconnect-Request to the router)
- Plans (speed, price, validity, IP pool, devices allowed), routers (NAS), login log, activity log, usage reports
- Maintenance timer that closes dead sessions after power cuts and archives old accounting data

**Version 2: the full menu**

The sidebar has these groups, and each one is a working feature:

| Menu | What it does |
|---|---|
| Home | Quick-action buttons, a "Needs attention" strip, and Reports & Statistics tabs: Subscriber (coloured cards and pie), Accounting, User, Predictions, Usage, Sales & Payment Graphs, Cashflow |
| My Profile | Your details, password, today's and this month's collection, recent activity |
| My Packages | The plans you may sell, with your own sales per plan this month |
| Users | Roles (permissions matrix), Users (everyone), Staffs, Resellers, Sub-resellers, Retailers. Each has phone, email, department, zone, allowed plans and, for sellers, who they belong to |
| Package | Plans, plan report (customers and monthly value per plan) |
| Subscribers | List with filters, online now, expiring, expired, suspended, add, CSV import/export |
| Accounting | Payments (with printable receipts and CSV), receive payment, expenses, income and expense (12 months), dues |
| Inventory | Items with minimum stock, stock in / out / assign to customer / return / count, full stock log |
| Network | Routers, IP pools (with usage bars and a ready MikroTik command), devices (ping check), test login |
| Area | Areas list (rename updates customers), area report |
| Tickets | Tickets with category, priority, department, assignee, replies, internal notes |
| Email / SMS | Compose to one, a group or typed numbers; templates; sent log with retry; automatic messages |
| Logs | Login attempts, panel activity, RADIUS sessions, messages |
| Notes | Private or shared notes, pinned, linked to a customer |
| Reports | Traffic, collection (CSV), customers |
| Documents | Upload NID copies, agreements, invoices; stored outside the web root |
| Department/Zone | Departments and zones for staff, tickets, areas and devices |
| Settings | General, Email (SMTP), SMS gateway, Automatic messages |

**Roles.** Administrator can do everything. Manager, Support, Accountant and Technician start with sensible defaults, and you can change each one under Users > Roles & permissions (none / view / view and change for every area). A user with "allowed plans" can only see and sell those plans.

**Resellers, sub-resellers and retailers** see only the customers they created and those created by people under them. Their home page shows statistics for those customers only. Give them only the permissions they need under Roles.

**SMS.** Settings > SMS gateway works with any provider that has an HTTP API. Paste the provider's sending address and put `{to}` and `{message}` where the number and text go (add `{sender}` if needed). Use "Send test SMS" before sending to customers.

**Email.** Settings > Email takes any SMTP account (for Gmail use an app password).

**Automatic messages.** Settings > Automatic messages: welcome and payment messages, and expiry reminders N days before (for example `3, 1, 0`). Their text is the `welcome`, `payment` and `expiry` templates, which you can edit under SMS > Templates and Email > Templates. Messages go through a queue, and a background service (`isp-panel-sender`) sends them at the speed you set, so sending to 20,000 customers does not freeze the panel.

**Upgrading.** See "Updates and automatic deploys" below. Data and passwords are always kept.

## Install

The step-by-step Linux install guide (DNS records, the setup wizard, HTTPS, MikroTik commands) is in the
[README](../README.md#4-install). In short, on a fresh Ubuntu 24.04 server:

```bash
sudo git clone https://github.com/rajib217/ISP-Radius-Enterprise.git /opt/isp-panel/src
cd /opt/isp-panel/src && sudo bash install.sh
```

The installer is safe to run again. It keeps passwords and data, and keeps backups of every FreeRADIUS file it edits
(in `/etc/freeradius/3.0/isp-backups/`).

## First steps in the panel

1. **Plans**: create your speed plans (for example `10Mbps`). The short name is what CSV import uses.
2. **Routers**: add each MikroTik with the IP it sends from and a shared secret.
3. **Import** your customers (Subscribers > Import). Download the sample file to see the columns.
4. **Server tools > Test a customer login** to confirm a customer is accepted.

## MikroTik settings

```
/radius add service=ppp address=<radius server ip> secret=<shared secret> timeout=3s
/radius incoming set accept=yes port=3799
/ppp aaa set use-radius=yes accounting=yes interim-update=5m
```

- `accounting=yes` and `interim-update` are required. Without them the Online page is empty and dead sessions are never cleaned up.
- `incoming accept=yes` lets the panel disconnect customers and apply suspensions immediately.
- Speed is sent as `Mikrotik-Rate-Limit` (upload/download as the router reads it). The plan's IP pool name is sent as `Framed-Pool`.
- Use RouterOS 7.15.2 or newer. Newer FreeRADIUS versions require Message-Authenticator to defend against the BlastRADIUS attack, and older RouterOS may not send it.

## How it maps to RADIUS

| Panel | FreeRADIUS table | Attribute |
|---|---|---|
| Password | `radcheck` | `Cleartext-Password` |
| Expiry date | `radcheck` | `Expiration` (works through the last day) |
| Suspended | `radcheck` | `Auth-Type := Reject` |
| Devices allowed | `radcheck` | `Simultaneous-Use` |
| Plan | `radusergroup` + `radgroupreply` | `Mikrotik-Rate-Limit`, `Framed-Pool`, `Acct-Interim-Interval` |
| Fixed IP | `radreply` | `Framed-IP-Address` |
| Routers | `nas` | read by FreeRADIUS at start and on reload |

Passwords must be stored in clear text so CHAP and MS-CHAPv2 can work. Protect the server accordingly (see Security).

## Capacity notes for 20,000 customers

- Recommended server: 4+ cores, 8 GB RAM or more, SSD. The installer sets the database buffer to 40% of RAM.
- Usage updates every 10 minutes (about 33 writes per second). Lower values cost more load; change it in Settings.
- After a power cut thousands of routers reconnect at once. The installer raises FreeRADIUS thread and request limits and uses a 64-connection database pool for this.
- Accounting rows older than 12 months move to `radacct_archive` each night. Login attempts older than 30 days are deleted. Both are adjustable in Settings.

## Security

- The installer switches on UFW: SSH, TCP 80/443 and UDP 443 for everyone, and UDP 1812/1813 (RADIUS) only for the
  routers saved under Network > Routers. A router added later is allowed within a minute (`isp-panel-fwsync.timer`).
  Rules you add by hand are left alone.
- HTTPS: give the installer a subdomain (for example `panel.yourdomain.com`, plus an optional `pay.yourdomain.com`
  for bKash). First add an **A record** for each at Hostinger (hPanel > Domains > DNS records) pointing to the server's
  public IP, and remove any AAAA record. The installer waits until public DNS shows the right IP, then Caddy gets
  Let's Encrypt certificates and renews them by itself. Without a subdomain the panel runs on plain HTTP by IP.
  To add HTTPS later, run `sudo ./install.sh` again and enter the subdomain.
- Secrets are generated at install in `/etc/isp-panel/panel.env` (readable by root and the panel user only).
- Login attempts are limited to 8 failures per 10 minutes per address. Every change is recorded under Logs > Panel activity.
- RADIUS does not log attempted passwords (the installer removes that from the stock `radpostauth` query).

## Backups

```bash
sudo mkdir -p /var/backups/radius
echo '30 2 * * * root mysqldump --single-transaction radius | gzip > /var/backups/radius/radius-$(date +\%F).sql.gz && find /var/backups/radius -mtime +14 -delete' | sudo tee /etc/cron.d/radius-backup
```

Copy these files off the server regularly.

## Updates and automatic deploys

Customers stay online during an update: `update.sh` changes only the panel (code, database schema, and its
own services). It never restarts FreeRADIUS, MariaDB or Caddy.

What one update does, stopping at the first problem with the running panel left as it was:

1. Copies the new code next to the live one and checks that it starts (syntax and imports).
2. Backs up the database to `/backups/isp-panel-<date>-<revision>.sql.gz` and keeps the last 7. Session history
   tables (`radacct_archive`, `radpostauth`, traffic and gateway logs) are saved without their rows; the nightly
   backup has those.
3. Applies `sql/panel_schema.sql` (it only adds tables, columns and indexes).
4. Swaps the code in and reloads the web panel gracefully: new workers start, old ones finish their requests.
5. Checks `http://127.0.0.1:8000/healthz`. If the new code does not answer, the previous code goes back automatically.

If an update also changes the system setup (a new service, FreeRADIUS or Caddy settings), it stops before step 1
and asks you to run `sudo ./install.sh` once by hand.

By hand on the server: `cd /opt/isp-panel/src && sudo git pull && sudo bash update.sh`

### One-time setup for GitHub Actions (`.github/workflows/deploy.yml`, runs on every push to `main`)

```bash
# 1. a root-owned clone, installed from there
sudo git clone https://github.com/<you>/ISP-Radius-Enterprise /opt/isp-panel/src
cd /opt/isp-panel/src && sudo ./install.sh

# 2. a deploy user that may run exactly one command as root
sudo adduser --disabled-password --gecos "" deploy
echo 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/isp-panel-deploy' | sudo tee /etc/sudoers.d/isp-panel-deploy
sudo chmod 440 /etc/sudoers.d/isp-panel-deploy
sudo -u deploy mkdir -m 700 /home/deploy/.ssh
# paste the PUBLIC half of a new key pair (ssh-keygen -t ed25519 -f deploy_key) into:
sudo -u deploy nano /home/deploy/.ssh/authorized_keys

# 3. the server's host key, to pin it in GitHub (check the fingerprint once)
ssh-keyscan -H <server-ip>
```

Then in GitHub, under Settings > Secrets and variables > Actions, add `SERVER_HOST`, `SERVER_USER` (`deploy`),
`SERVER_SSH_KEY` (the PRIVATE key) and `SERVER_KNOWN_HOSTS` (the ssh-keyscan output). Optionally add `SERVER_PORT`,
and `PANEL_URL` (for example `https://panel.yourdomain.com`) to also check the public HTTPS address after each deploy.
A private repository also needs a read-only deploy key on the server for `git pull`.

## Everyday commands

```bash
systemctl status freeradius isp-panel isp-panel-sender isp-panel-noc mariadb caddy
journalctl -u isp-panel -f                  # panel log
sudo systemctl stop freeradius && sudo freeradius -X     # RADIUS in debug mode, shows exactly why a login fails
radtest USER PASSWORD 127.0.0.1 0 $(grep RADIUS_LOCAL_SECRET /etc/isp-panel/panel.env | cut -d= -f2)
sudo -u root env $(cat /etc/isp-panel/panel.env | xargs) python3 /opt/isp-panel/app/manage.py reset-password admin 'NewPassword'
```

## Troubleshooting

- **Router gets no reply**: the router's source IP must be listed under Routers and the secrets must match. Check with `freeradius -X`.
- **Customer shows online after a power cut**: sessions with no update for 45 minutes are closed automatically; Settings > Close silent sessions controls this.
- **Disconnect button fails**: enable `/radius incoming` on the router, allow UDP 3799 from this server, and check the router is listed with the same secret.
- **Messages stay "Waiting"**: check Settings > SMS gateway or Email and use the test button, and make sure the sender is running: `systemctl status isp-panel-sender`, `journalctl -u isp-panel-sender -f`.
- **Speed not applied**: the plan's speed is read at login. Disconnect the customer so they reconnect with the new plan.

## Known limits

- Traffic reports count each session on the day it started. Long sessions make daily figures approximate. Monthly usage per customer is accurate over time but is not a quota system.
- Expired customers are refused at login. They are not moved to a "please renew" page.
- SMS and email delivery can only be confirmed as "accepted by the gateway". Delivery reports from the provider are not read.
- Uploaded documents live in `/var/lib/isp-panel/documents`. Include that folder in your backups.
- There is one MariaDB server. For a second RADIUS server, add database replication.

## Files

```
install.sh              one-step installer
sql/panel_schema.sql    panel tables and the indexes that matter at this size
panel/                  Flask application, templates, CSS and JS (no CDN, works offline)
panel/sender.py         background service that delivers queued SMS and email
/var/lib/isp-panel/     uploaded documents
/opt/isp-panel/app      where the panel is installed
/etc/isp-panel/         secrets (panel.env)
```


## Package menu

- **Policy**: lists RADIUS groups with every attribute (Reply or Check, operator, value). *Add Policy* creates a group with as many attribute rows as you need (Add More / Delete). Groups created by a plan are marked "Plan" and are changed on the Packages page. A policy that customers still use cannot be deleted.
- **Packages**: the plans page.
- **Allocation**: pick which packages each staff member, reseller or retailer may sell (empty = all).
- **Tax/Extra Fee**: percent or fixed charges. Active ones are added to the suggested amount in the Renew dialog.

Upgrade: copy the folder over the old one and run `sudo ./install.sh` again (adds the `fees` table).


## Subscribers menu

Sidebar lists: All, Online, Stale, Offline, Expired, Expired Online, Profile Disabled Online, Internet Disabled Online, Internet Disabled, Expiring 1 Day / 3 Days / 1 Week / 2 Weeks / 4 Weeks, MAC Binded, Grace Period On, Recent Auto Renew, Extra Service Fees. The (i) button on the page explains each one.

- The list shows ID, actions (renew, payment, suspend/activate), profile, status, expiry, password, package, salesperson, balance. View picks columns, Compact View tightens rows, Filter narrows by package, area, salesperson and expiry dates. Print/Copy/PDF/Excel/CSV export the page you see; Export and Download Subscribers Export give the full filtered list.
- Activation: type a username, days and amount. Mass Activation, Mass Payment (each customer pays their own plan price and gets the plan's days), Mass Settings (plan, grace, remove MAC) and Mass Delete work on the ticked rows.
- Grace period and MAC binding are set on the customer page. Grace moves the RADIUS expiry date later; MAC binding adds `Calling-Station-Id` to the login check. Use the format your router sends (MikroTik PPPoE: `AA:BB:CC:DD:EE:FF`).
- Balance is the total of unpaid Extra Service Fees. Recent Auto Renew shows customers renewed or paid in the last 7 days.

Upgrade: copy the folder over the old one and run `sudo ./install.sh` (adds two subscriber columns and the `sub_charges` table).


## Add / Edit Package

Package > Packages > Add Package has every option from the sections below. What each one does:

| Option | Effect |
|---|---|
| Name, Description, Invoice Description, Display Name, Price, speeds, IP pool, devices at once | Stored; speed, pool and devices go to the router through RADIUS |
| Duration + type (day / week / month = 30 days / year = 365 days) | Sets how many days a renewal adds |
| Fixed Expire Day | Every renewal ends on that day of the month |
| Fixed Expire Time | The login stops at that time on the expiry day |
| Tax/Extra Fee | Chosen fees are added to the suggested renewal amount |
| Policy | Extra RADIUS group every customer on the package joins |
| Allocation | Who may sell the package (nobody chosen = everyone) |
| Next Expired / Next Disabled Action | A policy that keeps expired or suspended customers online with restricted access; empty = they are refused |
| Data / Daily / Weekly quota | Checked every 5 minutes from accounting. When used up: disconnect, then refuse or move to a policy until the period restarts |
| Session quota | `Session-Timeout`: the router ends each session after that many minutes |
| Auto Renew, Require Balance, Billing Type, Auto Generate Invoice, Fixed Expire Day Acct. | Saved only. They need balances and invoices, which are not in this version |

Expired customers move into their "Next Expired Action" policy within 5 minutes of expiry; a live session keeps its old speed until the customer reconnects.

Upgrade: copy the folder over the old one and run `sudo ./install.sh` (adds package columns, `plan_fees` and `quota_hit`).


## Network menu

NAS, NAS Group, IP Pool, MAC Vendor, OLTs, ONUs, Graph, Diagram (and Test login).

- **NAS:** ID, IP, name, type, group, online subscribers and status (CoA badge, reachable / not answering after "Check connection"). Each row has an Actions menu: online users, edit, check connection, MikroTik setup commands (with the secret filled in), delete. Turning CoA off for a router stops the panel from sending disconnects to it.
- **NAS Group:** group routers; pick the group on the NAS form.
- **MAC Vendor:** look up a MAC and see online devices by vendor. The panel ships with no vendor list: upload the IEEE `oui.csv` (or paste `AA:BB:CC,Vendor` lines) once.
- **OLTs / ONUs:** OLTs are devices of type OLT; link each to its router. ONUs are a hand-kept list (OLT, PON port, serial, MAC, customer, Rx power, status). The panel does not read them from the OLT.
- **Graph:** customers and 7-day traffic per router, and online customers over 24 hours.
- **Diagram:** server, routers and the devices linked to them, with status dots.

Upgrade: copy the folder over the old one and run `sudo ./install.sh` (adds NAS columns, `nas_groups`, `mac_vendors`, `onus`).

## OLTs with SNMP (Network > OLTs)

Add an OLT with its SNMP settings (v1/v2c, community, port, timeout, retries). The panel reads the ONU list from the OLT:

- Automatically every 5 minutes (the maintenance timer), or on demand with **Sync ONUs** / **Sync all**.
- **Test SNMP** reads sysDescr / sysName / sysUpTime to check the settings.
- **SNMP walk** lists any OID subtree, to find the right OIDs for your OLT model.
- BDCOM, VSOL, C-Data and other EPON OLTs use the standard interface table (IF-MIB); ONU interfaces are matched with a regex (default `:\d+$`).
- Huawei and ZTE presets use vendor OIDs and are unverified starting points. Adjust them under "How ONUs are read" using the SNMP walk.
- SNMP v3 is not supported. The panel server must reach the OLT on UDP 161.

### ONUs page

Network > ONUs lists every ONU with filters (OLT, status, signal, mapped/unmapped), search, page size, Print / Copy / PDF / Excel / CSV export of the current page and a column chooser. Signal bands by Rx power: Excellent >= -14 dBm, Good >= -20, Fair >= -23, Poor >= -28, Critical below that. Slot/Card and PON : ONT are taken from the ONU name (for example `EPON0/1:3` or `0/4/8:63`). TX power and temperature are filled when you set their OIDs on the OLT (Advanced section). The row buttons show details, refresh the OLT, link the ONU to a subscriber by username, and delete.

### Hide the menu

The round menu button at the top left of every page hides or shows the left menu, so wide tables get the full screen. The choice is remembered in the browser.

### ONU detail page

The eye button on an ONU opens its page: details, current metrics (RX, TX, temperature, voltage, bias current, distance) and a Signal History chart (last hour, 6 hours, 24 hours, 7 days). History is saved at every OLT poll (every 5 minutes) and kept for 8 days. Voltage, current and distance appear once their OIDs are set on the OLT (Advanced section). After each sync the OLTs page shows how many ONUs returned RX, TX and temperature values, which tells you whether those OIDs are right.

## Settings

Settings has its own left menu: **System** (General, Software, Software Modules, Log Engine), **Customers and partners** (shortcuts to Subscribers, Receive payment, Users, Resellers), **Notifications** (SMS, Email, Automatic messages) and **Payment gateways**. The **Logo** and **Favicon** buttons upload your own images (PNG, JPG, GIF, WebP, ICO, up to 1 MB). Software Modules switches parts of the panel off for everyone. The payment gateway pages are placeholders: online payment is not built in, record payments under Accounting > Receive payment.

### Subscriber page

Open any customer from Subscribers > All. The page has action buttons (Activation = renew, Migration = change package, Add Balance, Edit Profile, Service Settings, Disconnect, Profile Disable, Disable Net, Sync, Usage Graph, New Invoice, Photo, Password, Toggle Password, Document, Attributes, Note, Grace Period, Reset MAC Address), KPI tiles (uptime, quota, balance, due, tickets) and tabs: Profile, Reports, Ledgers, Invoices, Payments, SMS Deliveries, Extra Service Fee, Attributes, MAC Address, Documents, Login Log, Session Log, Activities, Tickets.

- **Balance** is a prepaid wallet. Add Balance tops it up (a minus amount takes money off). Activation can take its amount from the balance. Every change is in the Ledgers tab.
- **Profile Disable** suspends the customer; **Disable Net** only refuses the internet login and keeps the profile active.
- **Service Settings:** SMS / Email switch off automatic messages for that customer. **Auto MAC lock** binds the first device seen (checked every 5 minutes). **Auto renew** renews from the balance when the package expires (checked every 5 minutes).
- **Not included:** Live Graph (needs router API polling), Login Link, Online Payments.

### Settings > Software

Phone / Identity format (digit count such as `[11-13]`, checked when saving a customer; the customer form has a new Identity (NID) field), Online page auto-refresh, default Table View Type, Google Maps (shows a map under Address Information), CoA Request Log (customer page > CoA Logs), Failed Auto Renew Log (customer page > Activities), Mac Lock Mode (Off / Lock = bind every customer's first device / Remove = clear all bindings in the background, then back to Off), NAS Mismatch Notification and NAS Auto Update (the panel learns each customer's router from their live session every 5 minutes). Import From Zalpro is shown but disabled: no importer exists yet.

## Settings > Software Modules
Six switches: Voucher, Prepaid Card, Inventory, Tickets, Notices, Department/Zone/POP. A module that is off disappears from the menu and its pages close for every user. Voucher and Prepaid Card are shown disabled because they are not built yet. **Notices** is new: add announcements under the Notices menu and they show as banners on the dashboard (optional start and end dates). It starts switched off on a fresh install; on an existing install turn it on here.

## Settings > Log Engine
Matches the NAT Log Engine screen: NAT Logs Module and Push switches, Push Status, Engine URL, API Token (eye button), export limits (days, rows), Log View Type, Test Connection and Submit. The log retention fields from before are kept at the bottom. Test Connection really calls the saved URL. **Push Now and the automatic push are not active**: they need your engine's API, which this panel does not have.

## Settings > Subscriber
Matches the Subscriber Settings screen. **Active:** Allow Duplicate Phone, Allow Duplicate NID/NIC and Identity Optional (checked when a subscriber is added or edited), Allow Document Upload (closes uploads on Documents), Grace Period days (default for the subscriber Grace Period action), Serial Number start (digits only; with 2001000 new subscribers and CSV imports get 2001001, 2001002, ...; empty gives random 8-digit numbers; shown on the subscriber page). **Saved only** (no customer portal exists yet): the twelve portal switches. Defaults are lenient (duplicates allowed, identity optional) so existing customers stay editable; the serial applies to new subscribers only.

## Settings > User
Five switches (Document/Phone/Email verification, Allow Duplicate Phone, Allow Duplicate NID/NIC). Only Allow Duplicate Phone is enforced (when adding or editing a panel user or your own profile); the rest are saved only.

## Settings > Partner Management
Tier switches for Reseller, Sub-reseller and Retailer with custom names and plural names. A tier that is OFF (or whose parent tier is OFF) blocks sign-in for its users and signs them out on their next click, stops auto-renew for their subscribers (maintenance job), and disappears from the Users menu and the add-user role list. Nothing is deleted. Names show in the menu, user lists, role pickers and role pages. Not built: Commission Sellers (switch disabled), General Name (saved only), renamed URLs (users/<plural>).

## Settings > Payment gateways > bKash
Only bKash is listed now. The page holds Status, Mode, Key, Secret, Username, Password, Transaction Fee and type, Merchant Number, Webhook Topic ARN(s) and the Webhook Payment Reference choice. **Saved only**: the panel does not receive bKash payments yet (no webhook receiver or payment API calls). Record payments under Accounting > Receive payment.

## Look and feel (premium theme)
One violet and ink-navy identity across every page: gradient sidebar with a glowing active item, refined buttons, fields with focus rings, calm tables, KPI cards, modals, toasts, a redesigned sign-in page and a matching dark mode. Inter and Inter Display are bundled in `static/fonts` (no internet needed; licence file included). Everything is in `static/css/app.css` (colour tokens at the top, "Premium layer" at the bottom).

## Add Package: speed comes from the Policy
There is no speed field on a package, as in your previous panel. The form has Bandwidth and Bandwidth Type (shown on lists and receipts only) and a required **Policy**. Put the speed in that policy as a `Mikrotik-Rate-Limit` reply attribute (for example `5M/10M`) under Package > Policy. Packages saved before this change keep their old speed until you edit and save them; the edit form warns about it. The Price, IP pool and Devices at once fields are kept because renewals and RADIUS need them.

## Why Refused?
Logs > Login attempts shows a "Why?" button next to every Refused line, and each subscriber page has a "Why Refused?" button (also at /diagnose). It checks: exact username, password row, suspension/quota/Disable Net, expiry, MAC lock, open sessions against Simultaneous-Use, groups and group check rules, and shows the last attempts.

## Usernames like name@realm
The stock FreeRADIUS policy (`policy.d/filter`) refuses `name@realm` when the realm has no dot (for example `@ahammedpur`) with "Realm does not have at least one dot separator". `install.sh` now comments that one rule out (backups of every FreeRADIUS file it changes go to `/etc/freeradius/3.0/isp-backups/`, a folder FreeRADIUS never reads), so these customers can log in after every install or upgrade.

## Installer safety checks (FreeRADIUS)
- Backups are written to `/etc/freeradius/3.0/isp-backups/`, never next to the originals.
- Stray `.bak`, `.orig`, `.orig-isp`, `.old`, `.save`, `~` files in `policy.d`, `sites-enabled`, `mods-enabled` and `conf.d` are moved into that folder (they can stop the service).
- Ownership is set to `root:freerad`, as the package does, so the service user can read every file.
- The configuration check runs as the `freerad` user, exactly like the service does, and a leftover `freeradius -X` debug run is stopped.
- After the restart the installer waits for FreeRADIUS to be active and prints the journal if it is not.

## Roles and Permissions pages

**Users → Roles** lists the built-in roles (Administrator, Manager, Support, Accountant, Technician, Reseller,
Sub-reseller, Retailer) with Role Type, Parent Role, user count, and a **Set Permission** button. The table has
Print / Copy / PDF / Excel / CSV and a live search.

**Set Permission** opens *Modules & Permissions (Role: …)*: pink pill tabs (Home, My Profile, User, Subscriber, Accounting, Network, Inventory, Ticket, Note, Logs, Reports, Documents, plus Package and SMS & Email) group the modules, and each module has
**View** and **Add / Edit** checkboxes (Add / Edit includes View). *Select All / Deselect All* work per tab, **Update**
saves, and *Reset to default* restores the built-in setting. Changes apply on the person's next page load.

Limits: permissions are enforced per module (none / view / change), not per individual button. The **Add Role**
button is shown disabled because custom roles are not supported; the eight built-in roles can be edited.

## Policy page layout

Package → Policy now lists only policies you created. The groups the panel makes for each package (named after the
package) are hidden, so they can no longer clutter the list. Each policy shows as a header row (name, number of
packages using it, number of subscribers) with its attributes underneath. Every row has an **Actions** menu: *Edit policy*,
*Delete policy* (blocked while customers still use it), and *Delete attribute* on attribute rows.

## Package duration: Day, Week, Month, Year

Duration Type is now just Day / Week / Month / Year. Month and Year renewals use real calendar months and years:
a 1-month package renewed on 15 March ends on 15 April, and one renewed on 31 January ends on 28 February
(29 in a leap year). This applies to Activation, mass payment and auto-renew. If you type a custom number of days in the
Activation box, that many plain days are added instead. The payment history records the real number of days added.

## Users page layout

Users (and Staffs / Resellers / Sub-resellers / Retailers) now use the table layout: ID, Actions menu, Profile,
Profile Type, Balance, Packages, Subscribers, Resellers, Role & Permission, and the Admin / Reseller / Subreseller
each person belongs to. Buttons on top: Add user, **Change Role**, **Filter** (role and status), **Compact View**.
The table has rows-per-page, Print / Copy / PDF / Excel / CSV, a View menu to show or hide columns, search and paging.
Balance is a per-user wallet figure (see below).

## User profile page

Click a name in Users to open the profile: buttons (Edit Profile, Assign Packages, Permissions, Add Note, Password,
Disable/Enable, Delete Profile), the header with role and status, counters (Packages, Subscribers, Subresellers,
Retailers, collected this month), an Actions menu (Change Profile Type / Change Role), and tabs: Profile, Reports,
Packages, Ledgers (payments this person received) and Subscribers. Photo, Invoice Settings and Switch Profile are not
included because the panel has no photo storage or sign-in-as feature.

## User balance

Every user has a Balance (shown green in the Users list and on the profile). An administrator changes it with the
wallet icon in the list or **Add Balance** on the profile (a minus amount takes balance away; it cannot go below zero).
Each change is saved in the Balance ledger on the Ledgers tab. The balance is a record you keep: it is **not** taken
automatically when a reseller activates customers. "Created By" is saved for users created from now on; the Admin
column falls back to your only administrator for older users.

Add Balance asks for a **Payment Method** (Cash, bKash, Nagad, Rocket, Bank, Other), the amount and a note. The
method is saved in the Balance ledger. The wallet icon in the Users list opens the same dialog for that user.

## Subscriber profile tabs

The tab row now reads: Profile, Reports, Ledgers, Invoices, Payments, Online Payments, SMS Deliveries, Services,
Extra Service Fee, Attributes, MAC Address, Documents, Login Log, Session Log, CoA Logs, Activities, Tickets.
*Services* shows the package, service switches and policies. *Online Payments* is empty until a payment gateway
(bKash) is connected, because no online payments are recorded yet.

## Add User form

Add User (Users page and the user profile's Edit Profile) has: Status, SMS Status and Email Status switches,
Profile Type (Admin, Staff, Reseller, Subreseller, Retailer) which filters the Role & Permission list, Full Name,
Username, Password, Identity, Phone, Email, Country, Province, City and Address, plus Belongs to, Department and Zone.
Which packages a person may sell is set with **Assign Packages** on their profile. Full name, identity, phone, email and password are required for a new user. Status really enables or
disables the sign-in. SMS Status and Email Status are saved and shown on the profile; the panel does not yet use them
to decide whether messages are sent to staff. Country, Province and City are typed text, not lists.

## Assign Packages (per user)

Users > profile > **Assign Packages** opens a page with a package list on the left (search and filter by
Ready / Not set / Needs attention) and an editor on the right: **Price** (what that person pays), **Subscriber profit**
and, for resellers, **Sub-reseller profit**, with live "Subscriber pays" and "Sub-reseller pays" totals. A sub-reseller or
retailer cannot be priced below the price of the reseller they belong to (shown as the Minimum). Leave the price empty
to not assign a package. If no package is priced, that person can use every package, as before.

Where it is used: when a customer created by that person is renewed, the suggested amount is their price plus the
subscriber profit (plus taxes/fees).

**The package price is taken from the seller's balance.** When a reseller, sub-reseller or retailer activates or renews a
customer (Activation window, the profile's Renew, Mass Activation, and the nightly Auto renew), their **Price** for that
package is subtracted from their balance and written in their Balance ledger (method "activation"). If their balance is
lower, nothing is changed and they are told to add balance first (Auto renew skips that customer and logs it). The
activation window shows the cost and the current balance. **When admin or staff activate or renew a customer that belongs
to a reseller, sub-reseller or retailer, that owner's balance is charged** (their price for the package) and the ledger line
says "(by admin)". If the owner's balance is too low, the activation is refused and the message names the owner. A customer
owned by admin or staff is not charged. If a seller has no price set for a package, nothing is charged for it. Only the person who activates pays: a reseller is not credited when a
sub-reseller under them activates.

## Who pays when balance is given
- **Admin** gives balance to anyone (users, resellers, sub-resellers, customers). The money is simply added; nothing is taken from anywhere.
- **A reseller** gives balance to a sub-reseller or retailer directly under them on the new **My Users** page (wallet icon), or to one of their customers with **Add Balance** on the customer's profile. The amount is **taken from the reseller's own balance**.
- **A sub-reseller or retailer** gives balance to their own customers the same way, taken from their own balance.
- If the giver's balance is too low, nothing is changed. A minus amount takes balance back: it returns to the giver's balance, and it cannot take more than the receiver holds.
- Every step is written in both ledgers (the giver's line is "Balance to <name>"). A seller can only give to people directly below them, and My Users shows only those people.
- The **My Users** menu and its Add Balance button follow the permissions *User > Module* and *User > Manage/Add Balance*. Staff accounts and admin are not charged.
- Not built: a seller creating, editing or disabling their own sub-resellers. That is still done by the admin under Users.

## Dashboard: expiring soon
The Subscriber tab of Reports & Statistics has three more tiles: **Expiring in 7 days**, **Expiring in 3 days** and **Expiring in 1 day** (active customers whose expiry date is from today up to that many days ahead, so the 7-day tile also contains the 3-day and 1-day ones). A reseller sees only their own customers. Clicking a tile opens the subscriber list with that filter.

## Subscriber list filter: Joined and Activated dates
The Filter window has two more date ranges. **Joined from / until** matches the day the customer was added. **Activated from / until** matches customers who were activated or renewed in that period (a payment that added days: Activation window, Renew, Mass Activation, Auto renew). Either end can be left empty. They combine with the other filters and also apply to Export. Extending expiry with the "Extend" bulk action or by editing the date is not counted as an activation.

## Reports > Server (CPU, RAM, storage)
Admin-only tab on the Reports page. Every 5 minutes the maintenance job records the server's CPU use, load average, RAM in use, main-disk use and the database size (read from /proc and the disk, no extra software). The tab shows the current values with the peak for the chosen dates, and four charts: CPU, RAM, Storage and Load. A period of up to 2 days shows every sample, up to a month shows hourly averages, longer shows daily averages. The first sample appears within 5 minutes of installing this version.

**Retention:** server samples older than **180 days are deleted automatically** (once a day, about 52,000 small rows at most). The number is the setting `server_stats_days` (default 180). The Traffic report is not deleted: customers' old accounting rows are *archived* (not erased) after `archive_months` (default 12), and login logs after `authlog_days` (default 30).
Notes: it measures the server the panel runs on, and the disk is the one holding `/`. If MariaDB or the backup folder is on another disk, that disk is not shown.

## Subscriber > Service Settings permissions
These boxes are now enforced (before, they were only saved). Without a box, the person keeps the saved value and the control is greyed out:
- **Packages** - no package change in Edit subscriber, Package Migration, the Mass Settings package and the "Change plan" bulk action, or in an Import file for existing customers. A new customer can still be created with a package. The package then changes only if an admin does it.
- **Profile Status** - Active/Suspended in Edit subscriber, Mass Settings and Import.
- **IP Pool/Address** - the fixed IP in Edit subscriber and Import.
- **Connection Password** - the password in Edit subscriber and Import (a new customer still needs one). The separate Change Password button follows *Change Password*.
- **SMS Status, Email Status** - hide the row on the profile and lock the switch; **Auto Mac Lock** and **Skip Auto Renew** lock their switches in Service settings.
- **SMS/Email/Auto MAC, Connection Type, NAS, Total/Used Volume, Total/Used Session** - the same fields in Mass Settings are skipped, and a notice lists what was not changed.
Not enforced (nothing in the panel uses them yet): Lock Volume Status, Lock Session Status, Custom Expiry, Mid-Period Package Change.
Changing a package does **not** charge the reseller. Only Activation and Renew do.

## Changing an expiry date
The permission **Subscriber > Expiration Date** is now enforced. Without it a seller cannot type or change the expiry date
when adding or editing a customer, in Mass Service Settings, in the Extend bulk action, or in an Import file (the expiry
column is ignored). A new customer added without it starts expired and becomes active only through Activation, which takes
the package price from the balance. **Custom Expiry** (a date picker in the activation window) does not exist yet, so that
box is still saved only.

## Reseller Add Balance (bKash)
Admin, reseller, sub-reseller and retailer panels show an **Add Balance** button. It opens the *Add Payment* page: Payment
Method (bKash only), Note (optional) and Amount (required). Submit starts a bKash **Tokenized Checkout** payment and sends
the reseller to bKash's own page. When bKash sends them back (to /bkash/callback/<id>/<key>, which has no "?" in it because
bKash adds ?paymentID=..&status=.. itself), the panel asks bKash to confirm the payment (execute) and only then adds the amount
to the balance, once, with an entry in the Balance ledger. A fee (Settings > Payment Gateway > bKash) is charged on top, and
only the entered amount is credited.

Needs: bKash switched on with Key, Secret, Username and Password for the **Tokenized Checkout** product (the credentials of
this panel's owner were checked: the Tokenized token request works, while the older popup "PGW Checkout" address answers
"Invalid app key and secret combination", so the popup version cannot be used with them). The panel must be reachable by the
reseller's browser at the address they use. If bKash answers 403 on "create", ask bKash to whitelist this server's IP and the
callback address (an https:// domain, set under Settings > Payment Gateway > Callback address, is safest). The create and
execute steps could not be run against bKash from the build environment, so test with a small amount first.

If the execute call fails after the reseller already paid, the balance is not added and the message shows the bKash payment ID;
check it in bKash and add the balance by hand (Admin profile > Add Balance).

Home, My Profile and User have no switches: the home page and a person's own profile are open to every role, and managing
users and roles is administrator-only, so those tabs only say so.

## Resellers and OLTs / ONUs

When an admin gives a reseller (or sub-reseller) the **Network** permission, that reseller can add their own OLTs and see the ONUs on them.

- Each OLT now stores who added it (`net_devices.created_by`; the installer adds the column).
- A reseller sees, edits, syncs, tests and deletes only OLTs added by themselves or their downline, and only the ONUs on those OLTs. Admin and staff see everything.
- A reseller can only add or edit an ONU on one of their own OLTs, and can only link an ONU to one of their own subscribers.
- OLTs added before this update have no owner, so only admin sees them. Admin can re-add them or set `created_by` to a reseller username in the database.
- Resellers cannot pick the "Connected to router" field, and the generic Devices page and Network diagram redirect them to their OLT list.
- NAS, NAS Group, IP Pool, MAC Vendor, Graph, Diagram, Test login and Devices have their own switch ("NAS, IP Pool and Graphs" on the Network permission tab). OLT / ONU access alone does not open them, and they disappear from that role's menu.
- Tested only against the fake-database harness, not a real MariaDB or a real OLT.

## Backup & Restore (Settings > Backup & Restore)

- **What is backed up:** the whole database (subscribers, packages, payments, RADIUS users and accounting, panel users, settings) as one `.sql.gz` file. It does not include server files such as the FreeRADIUS config or `/etc/isp-panel/panel.env`; the installer rebuilds those.
- **Automatic:** a systemd timer (`isp-panel-backup.timer`) checks every 10 minutes. Once a day, after the hour you choose (default 04:00), it makes a backup without locking the database (`--single-transaction`). The newest N backups are kept (default 7).
- **Back up now:** a button on the page. It runs in the background and the page reloads when finished.
- **Where files live:** `/var/lib/isp-panel/backups/`. You can download any backup, delete it, or copy a `.sql.gz` there with scp and it appears in the list.
- **Restore:** press Restore on a backup (made here, uploaded, or downloaded from Drive). You must type RESTORE and enter your own password. The panel first checks the whole file, then saves the current data as a "Before a restore" backup, then loads the file. If loading fails it puts the previous data back automatically. While it runs customers cannot log in and the panel may show errors for a minute or two; do it at a quiet time.
- **Upload a backup file:** the page accepts a `.sql.gz` made by this panel (up to 4 GB; Caddy allows this on that one URL only). Other upload pages stay limited to 20 MB. The file is checked but nothing changes until you press Restore.
- **Google Drive:** uses your own Google Cloud project, so no one else holds your data.
  1. console.cloud.google.com: create a project and enable the Google Drive API.
  2. OAuth consent screen: External, then **Publish app** (In production). In Testing mode Google makes the sign-in expire every 7 days.
  3. Credentials > OAuth client ID, type **TVs and Limited Input devices**. Paste the Client ID and Secret in the panel and Save.
  4. Press Connect Google Drive, open the Google link, enter the code, and press "I have approved it".
  Backups go to a folder named "ISP Panel Backups" in My Drive. The panel uses the `drive.file` permission, so it can only see files it created. The newest N are kept on Drive (default 14). "Show Drive backups" lists them and can download one to the server, so you can restore on a brand new server after reconnecting the same Google app.
- **Not encrypted:** backup files hold customer names, phone numbers and the bKash/Google settings. Keep them in a Google account you control.
- **Untested:** this was tested with a stand-in `mariadb`/`mariadb-dump` and stand-in Google replies, not against a real database or the real Google. Run Back up now, then Restore on a test copy, before relying on it.

## Function-level permissions (Users > Roles > Permissions)

The permission page now lists every function as its own checkbox, in the same tabs as the reference panel: Home, My Profile, User, Subscriber, Accounting, Network, Inventory, Ticket, Note, Logs, Reports, Documents, plus SMS & Email. Select/Deselect ticks or clears the open tab; Update saves all tabs.

- **Saved:** every checkbox is stored per role (table `role_funcs`). Saving also works out the older per-module level (None / View / Edit) from the boxes, so menus and pages keep following it. A group's "Module" box switches the whole group off.
- **Enforced (checked on the page or action):** subscriber add / edit / delete / import / export / activation / mass activation / package migration / balance / grace / password / photo / custom invoice / reset MAC / service settings / disconnect / enable-disable net / profile enable-disable / bulk actions; online disconnect; invoice add / reverse / export; cashflow (expenses) add-edit / delete; OLT add / edit / delete / read ONUs; ONU edit / link / delete; inventory add-edit / stock move / delete; ticket new / reply / status / delete; notes add / delete; documents add / delete; package save / delete; area, zone, SMS and email actions. The Add Subscriber, Import, Export and Add OLT buttons also hide when the box is off.
- **Saved only (nothing in the panel reads them yet):** the Home counters, My Profile and User tabs (Reseller staff, profile limits, API access, Switch Profile, invoice templates...), extra service fees, Service Settings field visibility, Cashflow Category, NAT Logs / CoA / PGW, BTRC and bandwidth reports, Change Username, Mass Payment, Mass Status / Mark As Paid. Ticking them records the choice for later.
- **Added beyond the reference:** OLT and ONU groups on the Network tab (for reseller OLT access), plus Inventory, Ticket, Note, Notices, Area, Zone and SMS & Email boxes. The Inventory, Ticket and Note layouts are my own, since no screenshots were given.
- A role that was never saved here starts from its old module levels: ticked where the module level allows the function, so nothing changes until you press Update.
- Admin always has everything. Custom roles (Add Role) are not built yet.

## Subscribers > Mass Settings

Tick subscribers, press **Mass Settings**, fill only what you want to change and press Submit. Empty fields are left alone. Fields: Profile Status, SMS / Email / Auto Mac Status, Connection Type, NAS, Salespersons, Packages, Expiration Date, Total / Used Volume (GB), Total / Used Session (Min), Discount type and amount, Box/POP number and address, MC/Switch/ONU board, Electricity type/socket, Cable type, Uplink port, Fiber code/ID, Fiber color.

- **Applied for real:** Profile Status (Suspended also disconnects live sessions), SMS / Email / Auto Mac status, Package, Expiration Date (RADIUS rewritten), Salespersons (changes the owner, so the subscriber moves to that person's list; a reseller can only pick people in their own downline), NAS (stored; the panel may overwrite it when it learns the router from accounting).
- **Saved only:** Connection Type, Total / Used Volume, Total / Used Session, Discount, and the Box/POP and fibre fields. They are stored and shown on the subscriber's Services tab (Connection & Infrastructure), but nothing in billing or RADIUS reads them yet (the discount is not subtracted from renewals).
- Needs the Subscriber "Mass Settings" permission. The installer adds the new subscriber columns.

## Routers (NAS): several at once

Add as many routers as you need under Network > NAS (each needs its own IP, short name and secret). FreeRADIUS only reads the router list from the database when it starts, so a plain reload does not activate a new router. Saving or deleting a router now **restarts** FreeRADIUS (about one second; connected customers stay online) so the change is active at once. On each MikroTik, add the panel server as the RADIUS server with the same secret (`/radius add service=ppp address=<panel IP> secret=<secret>`, CoA/incoming on port 3799).

## User profile buttons

The user profile (Users > click a user) now has: Edit Profile, Profile Settings, Assign Packages, Add Balance, Photo, Invoice Settings, Add Note, Password, Delete Profile, Switch Profile.

- **Profile Settings:** profile status (Active / Disabled) and SMS / email notification switches. This replaces the old Disable Profile button. The Permissions shortcut moved to Actions > Set permission.
- **Photo:** PNG or JPG up to 2 MB, shown on the profile.
- **Invoice Settings:** business name, address, phone and footer note. Payment receipts of that user's subscribers print these instead of the main ISP name; empty fields fall back to it.
- **Switch Profile:** an admin signs in as that user (active non-admin users only) to see exactly what they see. A yellow bar on every page has **Switch back**. Both steps are written to the activity log.

## Resellers managing their own sub-resellers / retailers

Under **Users > Roles > (a reseller or sub-reseller role) > User tab**, the admin can now tick:

| Box | What it lets the seller do (only for people directly below them) |
|---|---|
| User Profile > Add | Create the next tier: reseller creates sub-resellers, sub-reseller creates retailers |
| User Profile > Edit | Edit name, phone, email, identity, address; enable / disable |
| User Profile > Change Password | Set a new password for them |
| User Profile > Delete | Delete someone directly below them, only if they have no users below, no subscribers and a zero balance (otherwise disable them) |
| User Packages > Assign/Manage | Set the price each of them pays per package |

All four are **off by default**; nothing changes for existing sellers until the admin ticks them. They appear on the **My Users** page.

Rules that are enforced in code: a seller only sees and changes people whose parent is themselves; new people always get the next tier and the seller as parent (the seller cannot pick a role, parent or permissions); the tier switch in Settings > Partner Management is respected; package prices must be at least the seller's own price, and only packages the seller may use are shown.

**User limit (enforced).** Each reseller or sub-reseller can have a limit: the most people that may sit directly below them (empty = no limit). The admin sets it in the Add/Edit user dialog (field "User limit"). A reseller whose role has "Retailer Limitation" ticked can set it on their own sub-resellers, never above their own limit. It is checked when a seller creates someone, and when the admin creates a user or moves a user under a parent. Existing users are not removed if a limit is set lower than the number they already have; they simply cannot add more. Run `sql/panel_schema.sql` again (or the install script) to add the `max_children` column.

Not done: the admin has no limit on resellers (the "Reseller Limitation" and "Subreseller Limitation" boxes remain saved-only; the limit is set per user instead). Tested only against the fake database, not a real MariaDB.

## Custom roles (Users > Role > Add Role)

The administrator can create new roles with their own permission boxes, next to the built-in ones.

- **Add Role:** give a name and choose "Start from" an empty role or a copy of an existing office-staff role (manager, support, accountant, technician or another custom role). You land on the permission page to tick the boxes.
- **Use it:** the role then appears in the Role & Permission list when you add or edit a user (under type Staff), and in the Change role dialog.
- **Rename / Delete:** custom roles only. A role that is still given to a user cannot be deleted; change those users first. Built-in roles cannot be renamed or deleted.
- **Scope:** custom roles are **office-staff type**: they see all subscribers (like a manager or support), not a reseller's own downline, and they cannot hold a balance, a parent or a package price list. Reseller / sub-reseller / retailer stay the built-in tiers (rename them in Settings > Partner Management).
- **Database:** `panel_admins.role` becomes VARCHAR(32) and a new table `custom_roles` is added. Run `sql/panel_schema.sql` again (or the install script) once before using this, otherwise the Add Role button will show an error.
- Tested only against the fake database, not a real MariaDB.

## Customer portal (self-service)

Customers open **https://your-panel/portal**, sign in with their **PPPoE username and password**, and see:

- days left and the expiry date (red when expired or suspended), package, online / offline;
- **Renew with bKash** (see the rule below);
- usage this month (download, upload, sessions), a 14-day chart and the package volume bar when the package has a quota;
- their invoices (printable) and recent sessions;
- change their own password (this is also the PPPoE password, they must update their router).

**Switch on:** Settings > Subscriber > **Customer Portal** (off by default). In the same page, *Self Activation* controls the Pay button, *See Volume Info* the usage part, *Session Log* the sessions list and *Reset Password* the password form. Pay needs bKash switched on under Settings > Payment Gateway (the same bKash account the reseller Add Balance uses).

**Who can pay online.** Only customers who belong to the ISP itself (created by an administrator or staff). The amount is the package price, the days are the package validity, and the customer is renewed only after bKash confirms the payment (once, even if the page is reloaded; a wrong amount is refused). A payment is recorded like any other (Accounting > Payments, method "bkash", received by `portal:<username>`) and the usual payment SMS / email fires.
**Customers of a reseller, sub-reseller or retailer cannot pay online**; the portal shows who to pay instead. Reason: their money runs through the seller's own price and balance, and the portal does not touch a seller's balance. If you want online payments for them too, the rule for who receives the money must be decided first.

Safety: the portal has its own session (it never opens any admin page), 8 wrong sign-ins per address or username in 10 minutes lock sign-in for 10 minutes, and a customer can only see their own invoices and payments.

Not built: forgot-password by SMS/OTP, self-registration, two-factor, changing profile data, picking another package, and partial payments or advance balance. Run `sql/panel_schema.sql` again to add the `portal_payments` table. Tested only against the fake database; the real bKash create call is the same one that still needs your bKash whitelisting.

## Telegram alerts (Settings > Alerts)

`maintenance.py` (every 5 minutes) checks and sends a Telegram message once per problem, and once more when it is fixed:

- a router (NAS) does not answer ping (after 2 checks in a row, about 10 minutes) and when it is back;
- online customers drop by X% within 10 minutes (only when at least 20 were online);
- FreeRADIUS stops running (`systemctl is-active freeradius`);
- server CPU / RAM / disk above your limits (CPU must stay high for 2 samples);
- a morning summary (online, expiring today / tomorrow / 2-3 days, yesterday's new customers and collection).

**Set up:** create a bot with @BotFather, paste its token under Settings > Alerts, press Save, open the bot in Telegram, press Start and send any word, press **Find my chat ID**, then **Send test message**. A group works too (add the bot to the group, write there, press Find).

Notes: the server needs internet to api.telegram.org. The bot token is stored in the panel database (same as other gateway keys). Router checks use ICMP ping, so a router that blocks ping will look "down": allow ping from the panel server on it, or switch router alerts off. Only routers in the NAS list are checked (not OLTs or other devices). Not built: alerts for low reseller balance, failed bKash payments, OLT/ONU problems and repeated alerts while a problem lasts. Tested only with a simulated database and a simulated Telegram, not the real Telegram service. No database change is needed (settings only).

## Import: NAS and salesperson

On **Import subscribers** two dropdowns apply to every row of the file:
- **Salesperson**: the reseller, sub-reseller or retailer the imported customers belong to (default: you). They then appear in that seller's own list, and are limited by the usual downline rules (a seller can only pick themselves or their downline).
- **NAS**: saved on every imported customer (only for people allowed to set NAS, "Service Settings > NAS"). It is the same NAS field as in Mass Settings.

With "Update existing", the chosen salesperson and NAS are also written onto the customers that already exist. Importing does **not** charge the seller's balance (imports are for moving existing customers in). Tested only against the fake database.

## Reseller and sub-reseller profit report

Profit = what the customer paid minus the package price the seller paid from their balance (the price set under Users > Assign Packages).

- **Individual report per seller:** on both pages, the **seller drop-down** (next to the dates) lists every reseller, sub-reseller and retailer. Pick one and press Show to see only that seller: totals, profit-per-day chart and every sale (date, customer, days, method, customer paid, package cost, profit). Tick **Include people below** to add their sub-resellers and retailers; the sales list then shows which seller made each sale. The CSV follows the selection. A reseller can only pick themselves and people below them.
- **Admin / staff:** Reports > **Seller profit**. One row per reseller, sub-reseller and retailer with sales, customers paid, package cost, profit and margin, a profit-per-day chart and a CSV download.
- **Resellers, sub-resellers, retailers:** a **My Profit** menu item shows the same table for themselves and everyone below them. It needs no access to the Reports module. It can be switched off per role under Permissions > Reports > "My Profit (Reseller)".

What is **real and what is not:**
- The cost is saved on each payment at the moment of an activation, a renewal, a mass activation or an automatic renewal made for a seller's customer. So profit is exact for sales made **after this update**. Older payments have no cost saved and are left out (the page says so). Nothing is guessed back.
- Not counted: manual payments added on the Payments page, extra fees, and customers who pay online in the portal (they are not owned by a seller).
- Profit is shown per seller. A reseller's own mark-up on a sub-reseller is not tracked, because the panel charges only the seller who makes the sale.
- Run the SQL update once (`sql/panel_schema.sql` adds `payments.payer_id` and `payments.cost`), then restart the panel.
- Also changed: the Reports > Collection tab and its CSV now show a seller only their own and their downline's payments (before, a seller who was given the Reports module saw everyone's).
- Tested with a simulated database only, not on your real server.

## Subscriber Activation window

The **Activation** button on the Subscribers list opens a pop-up window with this form (same fields as below). **Activate & Print** activates the customer and opens the payment receipt ready to print. The **Activation** button on a customer's own profile opens the same form as a full page. On the profile page the customer is already chosen, and after you activate you are taken back to the profile. Both need the permission "Activation" (or "Mass Activation") under Users > Permissions > Subscriber Profile.

- **Subscriber:** type part of a username, name or phone number (at least 2 characters). A list under the box shows username, name, phone and status; pick with the mouse or with the arrow keys and Enter. If you type a full username and press Activate without picking, the panel looks it up for you. 20,000 customers are searched, not loaded. A reseller only finds their own customers and the ones below them.
- **Packages:** starts on the customer's current package. Choosing another one changes their package at activation, and the days come from that package. Sellers can only choose packages assigned to them. If the seller pays for the package from their balance, the cost and balance show under the box and the page warns when the balance is too low.
- **Custom Expiry** switch: only shown to people who have the permission **Custom Expiry** (Users > Permissions > Subscriber Profile). It is **off for everyone except Admin** until you tick it for a role, so resellers and sub-resellers cannot choose their own expiry date by default. Roles you saved before this update keep whatever was saved for them; check the box on each role you want. Pick the exact expiry date instead of the package length. The date is used as typed (no rounding to a whole month or a fixed expiry day).
- **Payment** switch: on = Payment Method, Amount received (filled from the package price) and Payment Note are recorded as a payment and the payment SMS goes out. Off = activated without a payment: no payment SMS, and the payment is saved as 0 with the method "none". A seller's package cost is still taken from their balance.
- The **Profile** and **Package Details** cards at the bottom update as you choose. Account status shows Active, Expired or Suspended, and the package shows its length in months/days and hours.
- Not tested on a real server or MariaDB, only with the simulated database and in a browser against it.

## Who sees which payments

A reseller, sub-reseller or retailer who is allowed to open **Payments** sees only: payments taken by themselves, payments taken by the people below them, and any payment of their own or their downline's customers. They cannot see another reseller's payments. Admin and staff roles still see everything. This is automatic (it follows the reseller tree); there is no extra switch.

To let a seller open Payments, give their role the **Accounting** module (View is enough) under Users > Permissions. Accounting is off for sellers by default. The same limit applies to the Payments list and its CSV, receipts, Receive Payment, manual payments and Dues. **Expenses and the Income summary are the company's own books and are refused for sellers even if Accounting is on.** Before this change, a seller who had been given Accounting saw every payment in the system.
