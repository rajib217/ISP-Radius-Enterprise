#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  ISP RADIUS Panel installer  -  Ubuntu Server 24.04
#  FreeRADIUS + MariaDB + web admin panel (Flask/gunicorn behind nginx)
#
#  Usage:   sudo ./install.sh
#  Options (environment variables):
#     ADMIN_USER=admin  ADMIN_PASS=...   first panel login (random if not set)
#     TIMEZONE=Asia/Dhaka                server time zone (set to "" to keep)
#  Safe to run again: it keeps existing passwords and data.
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/isp-panel
APP_DIR="$INSTALL_DIR/app"
ENV_DIR=/etc/isp-panel
ENV_FILE="$ENV_DIR/panel.env"
DB_NAME=radius
DB_RADIUS_USER=radius
DB_PANEL_USER=radius_panel
ADMIN_USER="${ADMIN_USER:-admin}"
TIMEZONE="${TIMEZONE-Asia/Dhaka}"

c_ok()   { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()    { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
rand()   { openssl rand -hex 16; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo ./install.sh"
[[ -f "$SRC_DIR/panel/app.py" ]] || die "Run this script from the unpacked isp-radius-panel folder."

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  c_warn "Built for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}. Continuing anyway."
fi

# ---------------------------------------------------------------- packages
c_ok "Installing packages (this takes a few minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y freeradius freeradius-mysql freeradius-utils \
  mariadb-server mariadb-client nginx openssl sudo \
  python3 python3-flask python3-pymysql python3-werkzeug gunicorn iputils-ping

command -v freeradius >/dev/null || die "freeradius binary not found after install."
FR_DIR="$(ls -d /etc/freeradius/*/ 2>/dev/null | head -n1 || true)"
FR_DIR="${FR_DIR%/}"
[[ -n "$FR_DIR" && -d "$FR_DIR" ]] || die "Cannot find /etc/freeradius/<version> directory."
c_ok "FreeRADIUS config directory: $FR_DIR"

if [[ -n "$TIMEZONE" && "$(timedatectl show -p Timezone --value 2>/dev/null)" != "$TIMEZONE" ]]; then
  c_ok "Setting server time zone to $TIMEZONE"
  timedatectl set-timezone "$TIMEZONE" || c_warn "Could not set time zone."
fi

# ---------------------------------------------------------------- secrets
mkdir -p "$ENV_DIR"
if [[ -f "$ENV_FILE" ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
PANEL_SECRET_KEY="${PANEL_SECRET_KEY:-$(rand)$(rand)}"
DB_PASS="${DB_PASS:-$(rand)}"
FR_DB_PASS="${FR_DB_PASS:-$(rand)}"
RADIUS_LOCAL_SECRET="${RADIUS_LOCAL_SECRET:-$(rand)}"
PANEL_COOKIE_SECURE="${PANEL_COOKIE_SECURE:-0}"

id -u isp-panel >/dev/null 2>&1 || useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin isp-panel
getent group freerad >/dev/null && usermod -aG freerad isp-panel || true

umask 027
cat > "$ENV_FILE" <<EOF
PANEL_SECRET_KEY=$PANEL_SECRET_KEY
PANEL_COOKIE_SECURE=$PANEL_COOKIE_SECURE
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=$DB_NAME
DB_USER=$DB_PANEL_USER
DB_PASS=$DB_PASS
FR_DB_PASS=$FR_DB_PASS
RADIUS_LOCAL_SECRET=$RADIUS_LOCAL_SECRET
EOF
chown root:isp-panel "$ENV_FILE"; chmod 640 "$ENV_FILE"
umask 022

# ---------------------------------------------------------------- MariaDB
c_ok "Tuning and preparing MariaDB for 20,000+ subscribers"
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
BUF_MB=$(( MEM_MB * 40 / 100 )); (( BUF_MB < 256 )) && BUF_MB=256
cat > /etc/mysql/mariadb.conf.d/99-isp-radius.cnf <<EOF
[mysqld]
character-set-server     = utf8mb4
collation-server         = utf8mb4_unicode_ci
skip-name-resolve
max_connections          = 500
innodb_buffer_pool_size  = ${BUF_MB}M
innodb_log_file_size     = 256M
innodb_flush_log_at_trx_commit = 2
innodb_flush_method      = O_DIRECT
innodb_io_capacity       = 1000
tmp_table_size           = 64M
max_heap_table_size      = 64M
EOF
systemctl enable --now mariadb
systemctl restart mariadb

mariadb <<SQL
CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$DB_RADIUS_USER'@'localhost'  IDENTIFIED BY '$FR_DB_PASS';
CREATE USER IF NOT EXISTS '$DB_RADIUS_USER'@'127.0.0.1'  IDENTIFIED BY '$FR_DB_PASS';
ALTER USER '$DB_RADIUS_USER'@'localhost'  IDENTIFIED BY '$FR_DB_PASS';
ALTER USER '$DB_RADIUS_USER'@'127.0.0.1'  IDENTIFIED BY '$FR_DB_PASS';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`$DB_NAME\`.* TO '$DB_RADIUS_USER'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE ON \`$DB_NAME\`.* TO '$DB_RADIUS_USER'@'127.0.0.1';
CREATE USER IF NOT EXISTS '$DB_PANEL_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
CREATE USER IF NOT EXISTS '$DB_PANEL_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_PANEL_USER'@'localhost' IDENTIFIED BY '$DB_PASS';
ALTER USER '$DB_PANEL_USER'@'127.0.0.1' IDENTIFIED BY '$DB_PASS';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_PANEL_USER'@'localhost';
GRANT ALL PRIVILEGES ON \`$DB_NAME\`.* TO '$DB_PANEL_USER'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

HAS_RADACCT=$(mariadb -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='radacct'")
if [[ "$HAS_RADACCT" == "0" ]]; then
  c_ok "Importing the FreeRADIUS schema"
  mariadb "$DB_NAME" < "$FR_DIR/mods-config/sql/main/mysql/schema.sql"
fi
c_ok "Importing the panel schema and indexes"
mariadb "$DB_NAME" < "$SRC_DIR/sql/panel_schema.sql"

# The panel joins its tables to FreeRADIUS's tables on username. MariaDB refuses to
# compare text across different collations, so make the panel tables match radcheck.
RC_COLL=$(mariadb -N -e "SELECT TABLE_COLLATION FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='radcheck'")
if [[ "$RC_COLL" =~ ^[a-z0-9_]+$ ]]; then
  for t in panel_admins plans subscribers payments audit_log stats_snapshots settings role_perms user_plans departments zones areas expenses inventory_items inventory_moves net_devices ip_pools tickets ticket_replies outbox message_templates notes documents fees sub_charges plan_fees nas_groups mac_vendors onus olt_snmp onu_metrics sub_ledger coa_log notices user_ledger topups role_funcs traffic_snapshots traffic_last; do
    CUR_COLL=$(mariadb -N -e "SELECT TABLE_COLLATION FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='$t'")
    if [[ "$CUR_COLL" != "$RC_COLL" ]]; then
      mariadb "$DB_NAME" -e "ALTER TABLE \`$t\` CONVERT TO CHARACTER SET utf8mb4 COLLATE $RC_COLL"
    fi
  done
fi

# ------------------------------------------------------------- FreeRADIUS
c_ok "Configuring FreeRADIUS to use MariaDB"
python3 - "$FR_DIR" "$FR_DB_PASS" "$RADIUS_LOCAL_SECRET" "$DB_NAME" "$DB_RADIUS_USER" <<'PY'
import os, re, shutil, sys
fr, dbpass, local_secret, dbname, dbuser = sys.argv[1:6]

def rd(p):
    with open(p) as f: return f.read()
def wr(p, s):
    with open(p, "w") as f: f.write(s)
BK = fr + "/isp-backups"            # FreeRADIUS never reads this folder (policy.d, mods-enabled ... are read)
os.makedirs(BK, exist_ok=True)
def backup(p):
    b = os.path.join(BK, os.path.relpath(p, fr).replace(os.sep, "__") + ".orig")
    if not os.path.exists(b): shutil.copy2(p, b)

# Files left by hand or by an older installer inside folders FreeRADIUS reads stop the service
# (a root-only copy cannot be opened by the freerad user). Move them out of the way.
for sub in ("policy.d", "sites-enabled", "mods-enabled", "conf.d"):
    d = os.path.join(fr, sub)
    if not os.path.isdir(d): continue
    for name in os.listdir(d):
        f = os.path.join(d, name)
        if os.path.islink(f) or not os.path.isfile(f): continue
        if re.search(r"(\.bak|\.orig|\.orig-isp|\.old|\.save|~|\.dpkg-\w+)$", name):
            dest = os.path.join(BK, "moved__" + sub + "__" + name)
            if os.path.exists(dest): os.remove(dest)
            shutil.move(f, dest)
            print("moved stray file out of " + sub + ": " + name)

def setv(s, key, val):
    pat = re.compile(r'^([ \t]*)#?[ \t]*' + re.escape(key) + r'[ \t]*=.*$', re.M)
    if not pat.search(s):
        raise SystemExit("setting not found in file: " + key)
    return pat.sub(lambda m: m.group(1) + key + " = " + val, s, count=1)

# 1. sql module
p = fr + "/mods-available/sql"; backup(p); s = rd(p)
s = setv(s, "driver", '"rlm_sql_mysql"')
s = setv(s, "dialect", '"mysql"')
s = setv(s, "server", '"127.0.0.1"')
s = setv(s, "port", "3306")
s = setv(s, "login", '"%s"' % dbuser)
s = setv(s, "password", '"%s"' % dbpass)
s = setv(s, "radius_db", '"%s"' % dbname)
s = setv(s, "read_clients", "yes")
s = setv(s, "client_table", '"nas"')

# FreeRADIUS 3.2 ships an active mysql { tls { ca_file = ... } } block that points at
# certificate files that do not exist. The database is local, so switch it off.
out, depth, in_tls = [], 0, False
for line in s.split("\n"):
    stripped = line.strip()
    if not in_tls and re.match(r"^tls[ \t]*\{", stripped):
        in_tls, depth = True, 1
        out.append(line)
        continue
    if in_tls:
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            in_tls = False
            out.append(line)
            continue
        if stripped and not stripped.startswith("#"):
            line = re.sub(r"^([ \t]*)", r"\1#", line, count=1)
    out.append(line)
s = "\n".join(out)
wr(p, s)
link = fr + "/mods-enabled/sql"
if not os.path.lexists(link): os.symlink("../mods-available/sql", link)

# 2. do not store attempted passwords in radpostauth
p = fr + "/mods-config/sql/main/mysql/queries.conf"; backup(p); s = rd(p)
s = re.sub(r"'%\{%\{User-Password\}:-[^']*'", "''", s)
wr(p, s)

# 3. default site: use SQL everywhere, drop flat-file accounting
p = fr + "/sites-available/default"; backup(p); s = rd(p)
s = re.sub(r'^([ \t]*)#[ \t]*-?sql[ \t]*$', r'\1-sql', s, flags=re.M)
for mod in ("radutmp", "unix", "detail"):
    s = re.sub(r'^([ \t]*)' + mod + r'[ \t]*$', r'\1#' + mod, s, flags=re.M)
wr(p, s)

# 4. server capacity for mass reconnects after a power cut
p = fr + "/radiusd.conf"; backup(p); s = rd(p)
for key, val in (("max_requests", "65536"), ("start_servers", "8"),
                 ("max_servers", "64"), ("min_spare_servers", "6"),
                 ("max_spare_servers", "20")):
    pat = re.compile(r'^([ \t]*)' + key + r'[ \t]*=.*$', re.M)
    if pat.search(s):
        s = pat.sub(lambda m: m.group(1) + key + " = " + val, s, count=1)
wr(p, s)

# 5. local test client secret
p = fr + "/clients.conf"; backup(p); s = rd(p)
s = re.sub(r'(secret[ \t]*=[ \t]*)testing123', lambda m: m.group(1) + local_secret, s)
wr(p, s)

# 6. accept usernames like name@realm even when the realm has no dot (the stock policy refuses them)
p = fr + "/policy.d/filter"
if os.path.exists(p):
    backup(p); s = rd(p)
    rule = re.compile(r'^([ \t]*)if \(\(&User-Name =~ /@/\) && \(&User-Name !~ /@\(\.\+\)\\\.\(\.\+\)\$/\)\)[ \t]*\{\n(?:.*\n)*?\1\}[ \t]*\n', re.M)
    s = rule.sub(lambda m: "".join("#" + ln if ln.strip() else ln for ln in m.group(0).splitlines(True)), s, count=1)
    wr(p, s)
    live = [ln for ln in s.splitlines() if "Realm does not have at least one dot" in ln and not ln.lstrip().startswith("#")]
    if live:
        print("WARNING: could not switch off the 'realm needs a dot' rule automatically; usernames such as name@realm may be refused.")
PY

c_ok "Checking the FreeRADIUS configuration"
# the service runs as the freerad user: give it read access to everything, exactly like the package default
if id freerad >/dev/null 2>&1; then
  chgrp -R freerad "$FR_DIR"
  find "$FR_DIR" -type d -exec chmod u+rwx,g+rx,o-rwx {} +
  find "$FR_DIR" -type f -exec chmod u+rw,g+r,o-rwx {} +
fi
# a leftover "freeradius -X" debug run keeps port 1812 busy
pkill -f '[f]reeradius.* -X' 2>/dev/null || true
fr_check() {
  if id freerad >/dev/null 2>&1; then
    runuser -u freerad -- "$(command -v freeradius)" -Cx -lstdout -d "$FR_DIR"
  else
    freeradius -Cx -lstdout -d "$FR_DIR"
  fi
}
if ! fr_check > /tmp/freeradius-check.log 2>&1; then
  tail -n 30 /tmp/freeradius-check.log
  die "FreeRADIUS config check failed (full log: /tmp/freeradius-check.log). Original files are saved in $FR_DIR/isp-backups/"
fi
systemctl enable freeradius
systemctl restart freeradius || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  systemctl is-active --quiet freeradius && break
  sleep 1
done
if ! systemctl is-active --quiet freeradius; then
  journalctl -u freeradius -n 25 --no-pager || true
  die "FreeRADIUS did not start. Fix the error above, then run this installer again."
fi
c_ok "FreeRADIUS is running"

# ------------------------------------------------------------- web panel
c_ok "Installing the web panel to $APP_DIR"
mkdir -p "$INSTALL_DIR"
rm -rf "$APP_DIR"; mkdir -p "$APP_DIR"
cp -a "$SRC_DIR/panel/." "$APP_DIR/"
chown -R root:root "$INSTALL_DIR"; chmod -R a+rX "$INSTALL_DIR"

cat > /etc/systemd/system/isp-panel.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel
After=network.target mariadb.service freeradius.service

[Service]
User=isp-panel
Group=isp-panel
SupplementaryGroups=freerad
WorkingDirectory=__APP__
EnvironmentFile=/etc/isp-panel/panel.env
ExecStart=/usr/bin/gunicorn --workers 4 --threads 8 --timeout 600 --bind 127.0.0.1:8000 --access-logfile - app:app
Restart=on-failure
RestartSec=3
PrivateTmp=yes
ProtectHome=yes
StateDirectory=isp-panel
StateDirectoryMode=0750

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/isp-panel-sender.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel SMS and email sender
After=network.target mariadb.service

[Service]
User=isp-panel
Group=isp-panel
WorkingDirectory=__APP__
EnvironmentFile=/etc/isp-panel/panel.env
ExecStart=/usr/bin/python3 __APP__/sender.py
Restart=always
RestartSec=5
PrivateTmp=yes
ProtectHome=yes

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/isp-panel-maint.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel maintenance (stale sessions, snapshots, cleanup)
After=mariadb.service

[Service]
Type=oneshot
User=isp-panel
Group=isp-panel
WorkingDirectory=__APP__
EnvironmentFile=/etc/isp-panel/panel.env
ExecStart=/usr/bin/python3 __APP__/maintenance.py
EOF

cat > /etc/systemd/system/isp-panel-maint.timer <<'EOF'
[Unit]
Description=Run ISP RADIUS Panel maintenance every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=isp-panel-maint.service

[Install]
WantedBy=timers.target
EOF
cat > /etc/systemd/system/isp-panel-backup.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel scheduled database backup
After=mariadb.service

[Service]
Type=oneshot
User=isp-panel
Group=isp-panel
WorkingDirectory=__APP__
EnvironmentFile=/etc/isp-panel/panel.env
ExecStart=/usr/bin/python3 __APP__/backuplib.py scheduled
StateDirectory=isp-panel
StateDirectoryMode=0750
TimeoutStartSec=6h
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/isp-panel-backup.timer <<'EOF'
[Unit]
Description=Check every 10 minutes whether the daily panel backup is due

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Unit=isp-panel-backup.service

[Install]
WantedBy=timers.target
EOF
sed -i "s#__APP__#$APP_DIR#g" /etc/systemd/system/isp-panel*.service

# allow the panel to reload FreeRADIUS after NAS changes (nothing else)
cat > /etc/sudoers.d/isp-panel <<'EOF'
isp-panel ALL=(root) NOPASSWD: /usr/bin/systemctl reload freeradius, /usr/bin/systemctl restart freeradius
EOF
chmod 440 /etc/sudoers.d/isp-panel
visudo -cf /etc/sudoers.d/isp-panel >/dev/null || { rm -f /etc/sudoers.d/isp-panel; die "sudoers check failed"; }

# first administrator
ADMIN_PASS_SHOWN=""
if [[ -z "${ADMIN_PASS:-}" ]]; then ADMIN_PASS="$(openssl rand -base64 12 | tr -d '=+/' | cut -c1-12)"; fi
RESULT=$(cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && python3 manage.py create-admin "$ADMIN_USER" "$ADMIN_PASS" admin) || die "Could not create the admin user: $RESULT"
[[ "$RESULT" == "created" ]] && ADMIN_PASS_SHOWN="$ADMIN_PASS"

systemctl daemon-reload
systemctl enable --now isp-panel.service isp-panel-sender.service isp-panel-maint.timer isp-panel-backup.timer

# ------------------------------------------------------------------ nginx
c_ok "Configuring nginx"
cat > /etc/nginx/sites-available/isp-panel <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 20m;

    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy same-origin always;

    location = /settings/backup/upload {
        client_max_body_size 4g;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900;
        proxy_send_timeout 900;
    }
    location /static/ {
        alias __APP__/static/;
        expires 7d;
        access_log off;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600;
    }
}
EOF
sed -i "s#__APP__#$APP_DIR#g" /etc/nginx/sites-available/isp-panel
ln -sf /etc/nginx/sites-available/isp-panel /etc/nginx/sites-enabled/isp-panel
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl restart nginx
systemctl restart isp-panel.service isp-panel-sender.service

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  c_ok "Opening firewall ports (RADIUS 1812/1813 udp, web 80/443 tcp)"
  ufw allow 1812/udp >/dev/null; ufw allow 1813/udp >/dev/null
  ufw allow 80/tcp >/dev/null;   ufw allow 443/tcp >/dev/null
fi

# ------------------------------------------------------------------ done
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
c_ok "Installation finished"
echo "   Panel URL : http://${IP:-your-server-ip}/"
echo "   Username  : $ADMIN_USER"
if [[ -n "$ADMIN_PASS_SHOWN" ]]; then
  echo "   Password  : $ADMIN_PASS_SHOWN     (change it after first login)"
else
  echo "   Password  : unchanged (admin already existed)"
fi
echo
echo "   Next: add your router under  Network > NAS / Routers,  then see README.md"
echo "   (MikroTik settings, HTTPS, backups)."
