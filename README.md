# ISP RADIUS Enterprise

A billing and RADIUS panel for PPPoE internet providers, built for MikroTik routers and sized for 20,000+ customers.
It installs FreeRADIUS, MariaDB and a web admin panel on one Ubuntu server, with automatic HTTPS from Let's Encrypt.

- Subscribers, packages, renewals and receipts; resellers, sub-resellers and retailers, each with their own balance
- Customer portal with bKash payments (Tokenized Checkout) and automatic reconnection after paying
- Live online sessions, one-click disconnect, router health (SNMP), Telegram alerts
- Accounting, inventory, tickets, SMS / email, OLT (ONU) monitoring, reports
- Safe updates: customers stay online while the panel is updated (GitHub Actions deploy included)

The full feature guide is in [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## 1. What you need

| | |
|---|---|
| Server | Ubuntu Server **24.04 LTS** (22.04 works but is less tested). A fresh VPS, or a machine in your own network. |
| Size | 2 cores / 4 GB RAM for a few thousand customers. **4+ cores / 8 GB RAM / SSD** for 20,000. |
| Access | `root`, or a user with `sudo`, over SSH |
| Network | A public IP. Ports **80** and **443** reachable from the internet (for HTTPS), and **UDP 1812/1813** reachable from your routers. |
| Domain (optional) | A subdomain such as `panel.yourdomain.com` for HTTPS, and optionally a second one such as `pay.yourdomain.com` for bKash. Without a domain the panel runs on `http://<server-ip>/`. |
| Routers | MikroTik with RouterOS **7.15.2 or newer** (needed for the Message-Authenticator check in new FreeRADIUS versions) |

If the server sits behind your MikroTik with a private LAN IP, first forward TCP 80 and 443 from the router's public IP
to the server. Otherwise Let's Encrypt cannot reach it.

## 2. Before you start: DNS records (for HTTPS)

Skip this step if you will use the panel by IP only.

At **Hostinger**, open hPanel > **Domains** > your domain > **DNS / Nameservers** > **DNS records**. Add one record for each subdomain:

| Type | Name | Points to | TTL |
|---|---|---|---|
| A | `panel` | your server's public IP | 300 |
| A | `pay` (optional) | your server's public IP | 300 |

Delete any other **A** or **AAAA** record with the same name. A stray IPv6 (AAAA) record makes Let's Encrypt fail.
Other DNS providers work the same way.

You do not need to wait for DNS before running the installer. It checks the records itself through public DNS
(1.1.1.1 and 8.8.8.8) and waits up to 15 minutes, showing what the internet currently sees.

## 3. Have these answers ready

The installer asks everything once, shows a summary, and changes nothing until you confirm. Press Enter to accept
a value shown in `[brackets]`.

| Question | Example | Notes |
|---|---|---|
| ISP company name, support phone, address | Dhaka Fiber Net, 01712345678, House 5, Uttara | Shown in the panel, customer portal and invoices |
| Super admin full name, email, username | Rafiqul Islam, rafiq@example.com, admin | Let's Encrypt sends certificate notices to this email |
| Super admin password | 10+ characters with upper case, lower case and a digit | Press Enter to get a generated one; it is shown once at the end |
| Public IP / LAN IP | 203.0.113.10 / 192.168.88.20 | Detected automatically; change them only if they are wrong |
| Panel subdomain, payment subdomain | panel.yourdomain.com, pay.yourdomain.com | Leave empty for plain HTTP by IP |
| First MikroTik: IP, name, RADIUS secret | 10.20.0.1, MT-Uttara | The IP the router uses to reach this server. Press Enter for a generated secret. You can also skip this and add routers later. |
| bKash (optional) | sandbox or live, App key, App secret, Username, Password | From your bKash merchant account. Can also be set later under Settings > bKash. |

## 4. Install

Connect to the server with SSH, then run:

```bash
sudo apt update && sudo apt install -y git
sudo git clone https://github.com/rajib217/ISP-Radius-Enterprise.git /opt/isp-panel/src
cd /opt/isp-panel/src
sudo bash install.sh
```

Answer the questions and type `Y` at the summary. The rest runs by itself and takes about 5 to 10 minutes:

1. installs FreeRADIUS, MariaDB, Python, Caddy and the firewall tools
2. checks the DNS records, and asks Let's Encrypt for certificates only once they are correct
3. tunes MariaDB for 20,000+ customers and creates the databases with random passwords (stored in `/etc/isp-panel/panel.env`)
4. configures FreeRADIUS to read customers, packages and routers from the database
5. installs the panel and its services, and saves your company, super admin, router and bKash details
6. sets up Caddy (HTTPS with automatic renewal) and the UFW firewall
7. tests HTTPS and prints the results

At the end it prints something like this:

```
==> Installation finished
   Panel     : https://panel.yourdomain.com/
   Portal    : https://panel.yourdomain.com/portal
   Username  : admin
   Password  : the one you typed

   https://panel.yourdomain.com  OK, trusted certificate (Let's Encrypt, valid until ...)

   ---------------- paste on the MikroTik (Terminal) ----------------
   /radius add service=ppp address=203.0.113.10 secret="..." authentication-port=1812 accounting-port=1813 timeout=3s comment="ISP RADIUS Panel"
   /radius incoming set accept=yes port=3799
   /ppp aaa set use-radius=yes accounting=yes interim-update=5m
```

The installer also runs without a terminal (for scripts). Then the answers come from environment variables such as
`ISP_NAME`, `ADMIN_EMAIL`, `DASH_DOMAIN` and `ROUTER_IP` (listed in the "setup wizard" part of `install.sh`).

## 5. Connect your MikroTik

Open the router's terminal (Winbox > New Terminal) and paste the three commands the installer printed:

```
/radius add service=ppp address=<server ip> secret="<secret>" authentication-port=1812 accounting-port=1813 timeout=3s
/radius incoming set accept=yes port=3799
/ppp aaa set use-radius=yes accounting=yes interim-update=5m
```

- `accounting=yes` and `interim-update` are required. Without them the Online page stays empty and the bandwidth chart has no data.
- `incoming accept=yes` lets the panel disconnect customers after a renewal, a suspension or the end of their package.
- For router health (CPU, RAM, uptime), also run `/snmp set enabled=yes`. Then set the community under **Logs > Logs & System Health > Settings**.
- For each additional router, first add it in the panel under **Network > Routers**. The firewall opens RADIUS for it within a minute, and the router's **MikroTik setup** button shows its commands.

**Split DNS (optional).** When the server is inside your network, the installer also prints `/ip dns static` commands.
With them, your customers reach the panel directly over the LAN and still see the same trusted certificate.

## 6. First steps in the panel

1. Sign in at the panel address with the super admin.
2. **Package**: create your speed packages (for example 10 Mbps, 500 Taka, 30 days).
3. **Subscribers > Add**, or **Subscribers > Import** your existing customers from CSV (download the sample file to see the columns).
4. **Network > Test login**: check that a customer is accepted.
5. Optional: **Settings > Subscriber > Customer Portal** (and **Self Activation** for bKash renewals), and **Settings > SMS**.

## 7. Updating

Customers stay online during updates. Only the panel itself is reloaded; FreeRADIUS, MariaDB and Caddy keep running.

```bash
cd /opt/isp-panel/src
sudo git pull
sudo bash update.sh
```

`update.sh` first checks the new code. Then it backs up the database to `/backups` (keeping the last 7), applies
database changes and switches to the new code. If the new code does not answer, it switches back by itself.
If an update needs system changes, it stops and asks you to run `sudo bash install.sh` once instead.
Running the installer again is always safe: it keeps your data and passwords and offers your last answers as defaults.

**Automatic deploys from GitHub.** `.github/workflows/deploy.yml` deploys every push to `main` once all checks pass.
To set it up, run `sudo bash deploy/setup-auto-deploy.sh` once on the server, then paste the values it prints into
GitHub (Settings > Secrets and variables > Actions). Details are in
[Updates and automatic deploys](docs/USER-GUIDE.md#updates-and-automatic-deploys).

## 8. Security and firewall

- UFW is switched on. SSH, TCP 80/443 and UDP 443 are open to everyone; UDP 1812/1813 only to the routers saved in the panel. Rules you add by hand are kept.
- HTTPS uses HSTS, and opening the server by its IP redirects to the HTTPS name.
- Secrets are in `/etc/isp-panel/panel.env`, readable only by root and the panel user. Your installer answers (without passwords) are in `/etc/isp-panel/install.conf`.
- If the installer generated the super admin password, change it after the first login.

## 9. Everyday commands

```bash
systemctl status freeradius isp-panel isp-panel-noc mariadb caddy   # is everything running?
journalctl -u isp-panel -n 50 --no-pager                             # panel errors
journalctl -u caddy -n 50 --no-pager                                 # HTTPS / certificate problems
sudo ufw status                                                      # firewall rules
ls -lh /backups                                                      # update backups
```

## 10. Troubleshooting

| Problem | What to do |
|---|---|
| The installer says a domain "points to nothing yet" | The DNS record is missing or not visible yet. Check it at Hostinger. Press Enter to check again, or `s` to continue on plain HTTP and run the installer again later. |
| HTTPS shows "NOT READY" at the end | Ports 80/443 are not reachable from the internet (provider firewall, missing port forward), or there is a wrong AAAA record. Check `journalctl -u caddy -n 50`, fix it, then run `sudo bash install.sh` again. |
| Customers cannot log in | In **Network > Routers**, the router's IP must be the address the router really sends from, and the secret must match. Try **Network > Test login**. **Logs > Logs & System Health** shows the reason for every refused login. |
| The Online page is empty | `accounting=yes` and `interim-update=5m` are missing on the MikroTik. |
| Disconnect does not work | `/radius incoming set accept=yes port=3799` is missing on the router, or a firewall between them blocks UDP 3799. |
| Forgot the admin password | `sudo bash -c 'cd /opt/isp-panel/app && set -a && . /etc/isp-panel/panel.env && python3 manage.py reset-password admin "NewPassw0rd"'` |

## Project layout

```
install.sh        installer and setup wizard (safe to run again)
update.sh         safe code update with backup and automatic rollback
panel/            the Flask web panel, NOC service and maintenance jobs
sql/              database schema (applied by install.sh and update.sh)
deploy/           Caddyfile template, firewall sync, GitHub deploy helper
docs/             user guide and design notes
tests/            page test run by GitHub Actions against a real MariaDB
.github/          GitHub Actions deploy workflow
```

## License

Apache License 2.0, see [LICENSE](LICENSE).
