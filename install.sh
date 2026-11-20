#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  ISP RADIUS Panel installer  -  Ubuntu Server 24.04 (22.04 works but is less tested)
#  FreeRADIUS + MariaDB + web admin panel (Flask/gunicorn) behind Caddy (automatic Let's Encrypt HTTPS)
#
#  Usage:   sudo ./install.sh
#  A short setup wizard asks for the company, super admin, server IPs, HTTPS subdomains, the first MikroTik and
#  (optionally) bKash. Without a terminal it uses environment variables instead; see "setup wizard" below.
#     TIMEZONE=Asia/Dhaka                server time zone (set to "" to keep)
#  Safe to run again: it keeps existing passwords and data, and offers the last answers as defaults.
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
# Raise when a change needs this installer (new service, unit change, FreeRADIUS or Caddy config): update.sh then
# refuses to deploy and asks for "sudo ./install.sh" once, instead of half-applying it.
INSTALLER_VERSION=2
ADMIN_USER="${ADMIN_USER:-}"
TIMEZONE="${TIMEZONE-Asia/Dhaka}"

c_ok()   { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()    { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
rand()   { openssl rand -hex 16; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo ./install.sh"
[[ -f "$SRC_DIR/panel/app.py" ]] || die "Run this script from the project folder (the one with install.sh, panel/ and sql/)."

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  c_warn "Built for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}. Continuing anyway."
fi

# ================================================================ setup wizard
# Asks everything first, then the rest runs unattended. Answers (no passwords) are saved in $CONF_FILE and offered
# as the defaults next time. Without a terminal (or with NONINTERACTIVE=1) nothing is asked: the environment
# variables below and the saved answers are used, which is how scripted re-runs keep working.
#   ISP_NAME ISP_PHONE ISP_ADDRESS ADMIN_NAME ADMIN_EMAIL ADMIN_USER ADMIN_PASS PUBLIC_IP LAN_IP
#   DASH_DOMAIN PAY_DOMAIN ROUTER_NAME ROUTER_IP ROUTER_SECRET BKASH_MODE BKASH_KEY BKASH_SECRET BKASH_USER BKASH_PASS
#   TLS13_ONLY=1 (refuse TLS 1.2; old phones may then fail to open the portal)   DNS_WAIT_MIN=15
CONF_FILE="$ENV_DIR/install.conf"
mkdir -p "$ENV_DIR"; chmod 750 "$ENV_DIR"
# shellcheck disable=SC1090
[[ -f "$CONF_FILE" ]] && . "$CONF_FILE"
FIRST_RUN=1; [[ -n "${SAVED_ADMIN_USER:-}" ]] && FIRST_RUN=0
dbval() { mariadb -N -B -e "$1" "$DB_NAME" 2>/dev/null | head -n1 || true; }
if command -v mariadb >/dev/null 2>&1 && [[ "$(dbval "SELECT COUNT(*) FROM panel_admins")" =~ ^[1-9] ]]; then
  FIRST_RUN=0                                  # installed before the wizard existed: never reset its admin password unasked
  : "${SAVED_ISP_NAME:=$(dbval "SELECT svalue FROM settings WHERE skey = 'isp_name' AND svalue <> 'My ISP'")}"
  : "${SAVED_ISP_PHONE:=$(dbval "SELECT svalue FROM settings WHERE skey = 'support_phone'")}"
  : "${SAVED_ISP_ADDRESS:=$(dbval "SELECT svalue FROM settings WHERE skey = 'company_address'")}"
  : "${SAVED_ADMIN_EMAIL:=$(dbval "SELECT svalue FROM settings WHERE skey = 'support_email'")}"
fi
INTERACTIVE=0; [[ -t 0 && -z "${NONINTERACTIVE:-}" ]] && INTERACTIVE=1

trim() { local v="$*"; v="${v#"${v%%[![:space:]]*}"}"; printf '%s' "${v%"${v##*[![:space:]]}"}"; }
bad()  { printf '     \033[1;31m%s\033[0m\n' "$*"; }     # prints only; validators return 1 themselves

v_text()   { [[ -n "$1" ]] || { bad "Required."; return 1; }; [[ ${#1} -le 120 ]] || { bad "Too long (max 120 characters)."; return 1; }; [[ "$1" != *[[:cntrl:]]* ]] || { bad "Invalid characters."; return 1; }; }
v_opttext(){ [[ -z "$1" ]] || v_text "$1"; }
v_phone()  { [[ "$1" =~ ^\+?[0-9][0-9\ -]{5,19}$ ]] || { bad "Enter a phone number, e.g. 01712345678 or +8801712345678."; return 1; }; }
v_email()  { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$ ]] || { bad "Enter a valid email address (Let's Encrypt sends expiry notices here)."; return 1; }; }
v_user()   { [[ "$1" =~ ^[A-Za-z0-9_.-]{3,32}$ ]] || { bad "3-32 characters: letters, digits, . _ -"; return 1; }; }
v_ipv4()   {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || { bad "Enter an IPv4 address, e.g. 203.0.113.10"; return 1; }
  local o; for o in "${BASH_REMATCH[@]:1}"; do (( 10#$o <= 255 )) || { bad "Each part must be 0-255."; return 1; }; done
}
v_optip()  { [[ -z "$1" ]] || v_ipv4 "$1"; }
v_domain() {
  [[ -z "$1" ]] && return 0
  [[ "$1" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || { bad "Enter a name like panel.yourdomain.com (no http://, no slash)."; return 1; }
}
v_secret() { [[ "$1" =~ ^[A-Za-z0-9._@%+=:-]{8,60}$ ]] || { bad "8-60 characters: letters, digits and . _ @ % + = : -"; return 1; }; }
v_strong() {
  [[ ${#1} -ge 10 ]] || { bad "At least 10 characters."; return 1; }
  [[ "$1" =~ [a-z] && "$1" =~ [A-Z] && "$1" =~ [0-9] ]] || { bad "Use upper case, lower case and a digit."; return 1; }
  [[ "$1" =~ ^[[:graph:]]+$ ]] || { bad "No spaces."; return 1; }
}
v_token()  { [[ "$1" =~ ^[[:graph:]]{1,200}$ ]] || { bad "Required, no spaces."; return 1; }; }
clean_domain() { local d; d="$(trim "$1")"; d="${d,,}"; d="${d#*://}"; d="${d%%/*}"; printf '%s' "${d%.}"; }

# ask VAR "Question" "default" validator
ask() {
  local __v=$1 __q=$2 __d=${3:-} __c=${4:-} __a
  if (( ! INTERACTIVE )); then
    [[ "$__v" == *DOMAIN ]] && __d="$(clean_domain "$__d")"
    if [[ -n "$__d" && -n "$__c" ]] && ! $__c "$__d" >/dev/null; then die "$__v=\"$__d\" is not valid: $($__c "$__d")"; fi
    printf -v "$__v" '%s' "$__d"; return 0
  fi
  while :; do
    if [[ -n "$__d" ]]; then read -r -p "   $__q [$__d]: " __a || exit 1; else read -r -p "   $__q: " __a || exit 1; fi
    __a="$(trim "${__a:-$__d}")"
    [[ "$__v" == *DOMAIN ]] && __a="$(clean_domain "$__a")"
    if [[ -z "$__c" ]] || $__c "$__a"; then printf -v "$__v" '%s' "$__a"; return 0; fi
  done
}
# ask_secret VAR "Question" validator "hint when empty is allowed"   (hidden input, typed twice)
ask_secret() {
  local __v=$1 __q=$2 __c=$3 __empty=${4:-} __a __b
  if (( ! INTERACTIVE )); then return 0; fi
  while :; do
    read -r -s -p "   $__q${__empty:+ ($__empty)}: " __a || exit 1; echo
    if [[ -z "$__a" && -n "$__empty" ]]; then printf -v "$__v" '%s' ""; return 0; fi
    $__c "$__a" || continue
    read -r -s -p "   Type it again: " __b || exit 1; echo
    [[ "$__a" == "$__b" ]] || { bad "The two entries do not match."; continue; }
    printf -v "$__v" '%s' "$__a"; return 0
  done
}
# ask_yn VAR "Question" y|n
ask_yn() {
  local __v=$1 __q=$2 __d=$3 __a
  if (( ! INTERACTIVE )); then printf -v "$__v" '%s' "$__d"; return 0; fi
  while :; do
    read -r -p "   $__q [$( [[ $__d == y ]] && echo Y/n || echo y/N )]: " __a || exit 1
    __a="${__a:-$__d}"; __a="${__a,,}"
    case "$__a" in y|yes) printf -v "$__v" y; return 0;; n|no) printf -v "$__v" n; return 0;; esac
  done
}
section() { (( INTERACTIVE )) && printf '\n\033[1;36m[%s]\033[0m %s\n' "$1" "$2"; return 0; }
gen_secret() { openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | cut -c1-20; }

DET_PUBLIC="$(curl -4 -fsS --max-time 6 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 6 https://ifconfig.me 2>/dev/null || true)"
v_ipv4 "$DET_PUBLIC" >/dev/null 2>&1 || DET_PUBLIC=""
DET_LAN="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -n1)"

if (( INTERACTIVE )); then
  printf '\n\033[1m  ISP RADIUS Panel - setup\033[0m\n'
  printf '  Press Enter to accept the value in [brackets]. Nothing is changed until you confirm at the end.\n'
fi

section "1/6" "Your company (shown in the panel, customer portal and invoices)"
ask ISP_NAME    "ISP company name"  "${ISP_NAME:-${SAVED_ISP_NAME:-}}"    v_text
ask ISP_PHONE   "Support phone"     "${ISP_PHONE:-${SAVED_ISP_PHONE:-}}"  v_phone
ask ISP_ADDRESS "Address"           "${ISP_ADDRESS:-${SAVED_ISP_ADDRESS:-}}" v_opttext

section "2/6" "Super admin (the first panel login)"
ask ADMIN_NAME  "Full name"  "${ADMIN_NAME:-${SAVED_ADMIN_NAME:-Administrator}}" v_text
ask ADMIN_EMAIL "Email"      "${ADMIN_EMAIL:-${SAVED_ADMIN_EMAIL:-}}" v_email
ask ADMIN_USER  "Username"   "${ADMIN_USER:-${SAVED_ADMIN_USER:-admin}}" v_user
ADMIN_PASS="${ADMIN_PASS:-}"
if (( FIRST_RUN )); then
  ask_secret ADMIN_PASS "Password, 10+ characters with A-z and 0-9" v_strong "Enter = make a strong one for me"
else
  ask_secret ADMIN_PASS "New password" v_strong "Enter = keep the current password"
fi

section "3/6" "Server addresses"
ask PUBLIC_IP "Public IP of this server (detected)" "${PUBLIC_IP:-${SAVED_PUBLIC_IP:-$DET_PUBLIC}}" v_ipv4
ask LAN_IP    "LAN IP of this server (detected)"    "${LAN_IP:-${SAVED_LAN_IP:-$DET_LAN}}" v_optip

section "4/6" "Domains for HTTPS (create the DNS A records at Hostinger first; leave empty for plain HTTP by IP)"
ask DASH_DOMAIN "Panel subdomain, e.g. panel.yourdomain.com" "${DASH_DOMAIN:-${SAVED_DASH_DOMAIN:-}}" v_domain
PAY_DOMAIN="${PAY_DOMAIN:-${SAVED_PAY_DOMAIN:-}}"
if [[ -n "$DASH_DOMAIN" ]]; then
  while :; do
    ask PAY_DOMAIN "Payment / bKash subdomain, e.g. pay.yourdomain.com (optional)" "$PAY_DOMAIN" v_domain
    [[ "$PAY_DOMAIN" != "$DASH_DOMAIN" ]] && break
    (( INTERACTIVE )) || { PAY_DOMAIN=""; break; }
    bad "Use a different name than the panel subdomain, or leave it empty."; PAY_DOMAIN=""
  done
else
  PAY_DOMAIN=""
fi

section "5/6" "First MikroTik router (more can be added later under Network > Routers)"
ask ROUTER_IP "Router IP as RADIUS sees it (empty = add routers later)" "${ROUTER_IP:-${SAVED_ROUTER_IP:-}}" v_optip
ROUTER_NAME="${ROUTER_NAME:-${SAVED_ROUTER_NAME:-MikroTik-1}}"; ROUTER_SECRET="${ROUTER_SECRET:-}"
if [[ -n "$ROUTER_IP" ]]; then
  ask ROUTER_NAME "Router name" "$ROUTER_NAME" v_text
  ROUTER_NAME="${ROUTER_NAME:0:32}"
  if (( INTERACTIVE )); then
    ask_secret ROUTER_SECRET "RADIUS shared secret" v_secret "Enter = make one for me / keep the saved one"
  fi
fi

section "6/6" "bKash payment gateway (optional, can be set later under Settings > bKash)"
BKASH_ON=0; BKASH_MODE="${BKASH_MODE:-sandbox}"
if [[ -n "${BKASH_KEY:-}" && -n "${BKASH_SECRET:-}" && -n "${BKASH_USER:-}" && -n "${BKASH_PASS:-}" ]]; then BKASH_ON=1; fi
if (( INTERACTIVE )); then
  ask_yn want_bkash "Enter bKash credentials now?" n
  if [[ $want_bkash == y ]]; then
    while :; do
      read -r -p "   Mode, sandbox or live [sandbox]: " BKASH_MODE || exit 1
      BKASH_MODE="$(trim "${BKASH_MODE:-sandbox}")"; BKASH_MODE="${BKASH_MODE,,}"
      [[ $BKASH_MODE == sandbox || $BKASH_MODE == live ]] && break; bad "Type sandbox or live."
    done
    ask BKASH_KEY  "App key"  "" v_token
    ask_secret BKASH_SECRET "App secret" v_token
    ask BKASH_USER "Username" "" v_token
    ask_secret BKASH_PASS "Password" v_token
    BKASH_ON=1
  fi
fi

# generated values
ADMIN_PASS_SHOWN=""
if [[ -z "$ADMIN_PASS" ]] && (( FIRST_RUN )); then
  ADMIN_PASS="$(gen_secret)"; ADMIN_PASS_SHOWN="$ADMIN_PASS"
fi
ROUTER_SECRET_SHOWN=""
if [[ -n "$ROUTER_IP" && -z "$ROUTER_SECRET" ]]; then
  if [[ -n "${SAVED_ROUTER_IP:-}" && "$ROUTER_IP" == "$SAVED_ROUTER_IP" ]]; then
    : # same router as last time: keep the secret already in the database (read later)
  else
    ROUTER_SECRET="$(gen_secret)"
  fi
fi
CALLBACK_BASE=""
[[ -n "$DASH_DOMAIN" ]] && CALLBACK_BASE="https://${PAY_DOMAIN:-$DASH_DOMAIN}"

if (( INTERACTIVE )); then
  printf '\n\033[1m  Please check:\033[0m\n'
  printf '   %-18s %s\n' "Company" "$ISP_NAME, $ISP_PHONE${ISP_ADDRESS:+, $ISP_ADDRESS}" \
    "Super admin" "$ADMIN_USER ($ADMIN_NAME, $ADMIN_EMAIL)$( [[ -z $ADMIN_PASS ]] && echo ', password unchanged' )" \
    "Server" "public $PUBLIC_IP${LAN_IP:+, LAN $LAN_IP}" \
    "HTTPS" "$( [[ -n $DASH_DOMAIN ]] && echo "https://$DASH_DOMAIN${PAY_DOMAIN:+ and https://$PAY_DOMAIN}" || echo 'off (http://'"$PUBLIC_IP"'/)' )" \
    "Router" "$( [[ -n $ROUTER_IP ]] && echo "$ROUTER_NAME at $ROUTER_IP" || echo 'add later' )" \
    "bKash" "$( (( BKASH_ON )) && echo "$BKASH_MODE mode" || echo 'not changed' )"
  ask_yn go "Start the installation?" y
  [[ $go == y ]] || die "Stopped. Nothing was changed."
fi

umask 077
{
  echo "# install.sh answers (no passwords). Offered as defaults when the installer runs again."
  for k in ISP_NAME ISP_PHONE ISP_ADDRESS ADMIN_NAME ADMIN_EMAIL ADMIN_USER PUBLIC_IP LAN_IP DASH_DOMAIN PAY_DOMAIN ROUTER_NAME ROUTER_IP; do
    printf 'SAVED_%s=%q\n' "$k" "${!k}"
  done
} > "$CONF_FILE"
umask 022

# ---------------------------------------------------------------- packages
c_ok "Installing packages (this takes a few minutes)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y freeradius freeradius-mysql freeradius-utils \
  mariadb-server mariadb-client openssl sudo curl gnupg ca-certificates dnsutils ufw git \
  python3 python3-flask python3-pymysql python3-werkzeug gunicorn iputils-ping
if ! command -v caddy >/dev/null 2>&1; then
  c_ok "Installing Caddy (official Caddy apt repository)"
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  # The package would start Caddy at once, and fail while nginx still holds port 80 (that would stop this installer).
  # policy-rc.d tells the package scripts not to start services; Caddy starts below, once its Caddyfile is written.
  OWN_POLICY=0
  if [[ ! -e /usr/sbin/policy-rc.d ]]; then         # never replace one that the system already has
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d; OWN_POLICY=1
  fi
  if ! apt-get install -y caddy; then
    if (( OWN_POLICY )); then rm -f /usr/sbin/policy-rc.d; fi
    die "Could not install Caddy."
  fi
  if (( OWN_POLICY )); then rm -f /usr/sbin/policy-rc.d; fi
  systemctl stop caddy 2>/dev/null || true
fi
command -v caddy >/dev/null || die "caddy binary not found after install."

command -v freeradius >/dev/null || die "freeradius binary not found after install."
FR_DIR="$(ls -d /etc/freeradius/*/ 2>/dev/null | head -n1 || true)"
FR_DIR="${FR_DIR%/}"
[[ -n "$FR_DIR" && -d "$FR_DIR" ]] || die "Cannot find /etc/freeradius/<version> directory."
c_ok "FreeRADIUS config directory: $FR_DIR"

if [[ -n "$TIMEZONE" && "$(timedatectl show -p Timezone --value 2>/dev/null)" != "$TIMEZONE" ]]; then
  c_ok "Setting server time zone to $TIMEZONE"
  timedatectl set-timezone "$TIMEZONE" || c_warn "Could not set time zone."
fi


# ------------------------------------------------------------- DNS pre-flight
# Let's Encrypt allows only a few failed attempts per hour, so ask for a certificate only once both names already
# point at this server, as seen by two public resolvers (not this server's own cache).
HTTPS_ON=0
dns_state() {   # domain -> prints a short status, returns 0 when ready
  local d=$1 a1 a2 six mine bad6=""
  a1="$(dig +short +time=3 +tries=1 A "$d" @1.1.1.1 2>/dev/null | grep -E '^[0-9.]+$' | sort -u | tr '\n' ' ')"
  a2="$(dig +short +time=3 +tries=1 A "$d" @8.8.8.8 2>/dev/null | grep -E '^[0-9.]+$' | sort -u | tr '\n' ' ')"
  if [[ "$a1" != "$PUBLIC_IP " || "$a2" != "$PUBLIC_IP " ]]; then
    echo "points to: ${a1:-nothing yet} (1.1.1.1), ${a2:-nothing yet} (8.8.8.8); needs exactly $PUBLIC_IP"; return 1
  fi
  six="$(dig +short +time=3 +tries=1 AAAA "$d" @1.1.1.1 2>/dev/null | grep ':' || true)"
  if [[ -n "$six" ]]; then
    mine="$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/ {split($2, a, "/"); print a[1]}')"
    for x in $six; do grep -qxF "$x" <<<"$mine" || bad6="$bad6 $x"; done
    if [[ -n "$bad6" ]]; then
      echo "has an IPv6 (AAAA) record that is not this server:$bad6 - delete it at Hostinger"; return 1
    fi
  fi
  echo "OK ($PUBLIC_IP)"; return 0
}
if [[ -n "$DASH_DOMAIN" ]]; then
  c_ok "Checking DNS for the HTTPS domains"
  DEADLINE=$(( $(date +%s) + ${DNS_WAIT_MIN:-15} * 60 ))
  for d in $DASH_DOMAIN $PAY_DOMAIN; do
    while :; do
      if st="$(dns_state "$d")"; then echo "   $d: $st"; break; fi
      echo "   $d: $st"
      if (( $(date +%s) > DEADLINE )); then st="timeout"; break; fi
      cat <<EOF
     Fix it at Hostinger: hPanel > Domains > (your domain) > DNS / Nameservers > DNS records:
       Type A    Name ${d%%.*}    Points to $PUBLIC_IP    TTL 300   (delete any other A or AAAA record for ${d%%.*})
     Checking again in 20 seconds$( (( INTERACTIVE )) && echo ' (press s to skip HTTPS, Enter to check now)')...
EOF
      key=""
      if (( INTERACTIVE )); then read -r -t 20 -n 1 key || true; echo; else sleep 20; fi
      [[ "$key" == s || "$key" == S ]] && { st="skipped"; break; }
    done
    if [[ "$st" == timeout || "$st" == skipped ]]; then
      if [[ "$d" == "$DASH_DOMAIN" ]]; then
        c_warn "HTTPS is off for now: $d does not point to $PUBLIC_IP yet. The panel runs on http://$PUBLIC_IP/."
        c_warn "Fix the DNS record, then run this installer again; it asks for the certificate then."
        DASH_DOMAIN=""; PAY_DOMAIN=""; break
      fi
      c_warn "Leaving out $d (DNS not ready). Run the installer again once it points to $PUBLIC_IP."
      PAY_DOMAIN=""
    fi
  done
  [[ -n "$DASH_DOMAIN" ]] && HTTPS_ON=1
fi
CALLBACK_BASE=""
(( HTTPS_ON )) && CALLBACK_BASE="https://${PAY_DOMAIN:-$DASH_DOMAIN}"

# ---------------------------------------------------------------- secrets
mkdir -p "$ENV_DIR"
if [[ -f "$ENV_FILE" ]]; then
  set -a; . "$ENV_FILE"; set +a
fi
PANEL_SECRET_KEY="${PANEL_SECRET_KEY:-$(rand)$(rand)}"
DB_PASS="${DB_PASS:-$(rand)}"
FR_DB_PASS="${FR_DB_PASS:-$(rand)}"
RADIUS_LOCAL_SECRET="${RADIUS_LOCAL_SECRET:-$(rand)}"
PANEL_COOKIE_SECURE="$HTTPS_ON"          # secure cookies only work over HTTPS

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
  for t in panel_admins plans subscribers payments audit_log stats_snapshots settings role_perms user_plans departments zones areas expenses inventory_items inventory_moves net_devices ip_pools tickets ticket_replies outbox message_templates notes documents fees sub_charges plan_fees nas_groups mac_vendors onus olt_snmp onu_metrics sub_ledger coa_log notices user_ledger topups role_funcs traffic_snapshots traffic_last nas_health coa_queue portal_payments gateway_logs; do
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
#    Always start from the original file kept in isp-backups: every change to queries.conf is made here, so a re-run
#    also repairs a copy that an older version of this installer edited wrongly.
p = fr + "/mods-config/sql/main/mysql/queries.conf"; backup(p)
s = rd(os.path.join(BK, os.path.relpath(p, fr).replace(os.sep, "__") + ".orig"))
s = re.sub(r"'%\{%\{User-Password\}:-[^']*'", "''", s)

# 2b. the post-auth log also keeps why a login was refused, the router and the device MAC
#     (columns reason / nas_ip / mac, added by panel_schema.sql; shown on Logs & System Health).
def block_span(text, header, must_contain=None):
    """(start, end) of the body of the first `header { ... }` whose body contains `must_contain`, skipping braces
    inside quoted strings. queries.conf has two post-auth blocks: one inside the accounting queries (session start)
    and the real authentication log, which is the one that writes to ${..postauth_table}."""
    for m in re.finditer(r"^[ \t]*" + header + r"[ \t]*\{[ \t]*$", text, re.M):
        span = _body(text, m)
        if span and (must_contain is None or must_contain in text[span[0]:span[1]]):
            return span
    return None


def _body(text, m):
    i, depth, quote = m.end(), 1, False
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl
            continue
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return m.end(), i
        i += 1
    return None

POSTAUTH_QUERY = r'''	query =	"\
		INSERT INTO ${..postauth_table} \
			(username, pass, reply, authdate, reason, nas_ip, mac ${..class.column_name}) \
		VALUES ( \
			'%{SQL-User-Name}', \
			'', \
			'%{reply:Packet-Type}', \
			'%S.%M', \
			'%{%{Module-Failure-Message}:-%{reply:Reply-Message}}', \
			'%{NAS-IP-Address}', \
			'%{Calling-Station-Id}' \
			${..class.reply_xlat})"'''
span = block_span(s, "post-auth", must_contain="postauth_table")
if span and "(username, pass, reply, authdate, reason, nas_ip, mac)" not in s[span[0]:span[1]]:
    body = s[span[0]:span[1]]
    q = re.search(r'^[ \t]*query[ \t]*=[ \t]*"(?:[^"\\]|\\.)*"', body, re.M | re.S)
    if q:
        body = body[:q.start()] + POSTAUTH_QUERY + body[q.end():]
        s = s[:span[0]] + body + s[span[1]:]
    else:
        print("WARNING: post-auth query not found; refused logins will be logged without a reason.")
elif not span:
    print("WARNING: post-auth section not found; refused logins will be logged without a reason.")
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
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
git -C "$SRC_DIR" rev-parse --short=12 HEAD > "$APP_DIR/REVISION" 2>/dev/null || echo "manual" > "$APP_DIR/REVISION"
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
ExecStart=/usr/bin/gunicorn --chdir __APP__ --workers 4 --threads 8 --timeout 600 --graceful-timeout 120 --bind 127.0.0.1:8000 --access-logfile - app:app
# update.sh: HUP starts new workers on the new code while the old ones finish their requests (no downtime)
ExecReload=/bin/kill -s HUP $MAINPID
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

# NOC: router health every 30 s, Telegram alerts, RADIUS disconnects. Independent of FreeRADIUS and of the web
# panel: restarting either one (or this) never drops a customer's PPPoE session.
cat > /etc/systemd/system/isp-panel-noc.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel NOC (router health, alerts, disconnects)
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
User=isp-panel
Group=isp-panel
WorkingDirectory=__APP__
EnvironmentFile=/etc/isp-panel/panel.env
ExecStart=/usr/bin/python3 __APP__/noc.py
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

# firewall: keep RADIUS open exactly for the routers in the panel (a root job; the panel itself never runs as root)
install -m 755 "$SRC_DIR/deploy/isp-panel-fwsync" /usr/local/sbin/isp-panel-fwsync
cat > /etc/systemd/system/isp-panel-fwsync.service <<'EOF'
[Unit]
Description=ISP RADIUS Panel: open RADIUS in UFW for the routers saved in the panel
After=mariadb.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/isp-panel-fwsync
EOF
cat > /etc/systemd/system/isp-panel-fwsync.timer <<'EOF'
[Unit]
Description=Sync RADIUS firewall rules with the panel's routers every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Unit=isp-panel-fwsync.service

[Install]
WantedBy=timers.target
EOF
sed -i "s#__APP__#$APP_DIR#g" /etc/systemd/system/isp-panel*.service

# allow the panel to reload FreeRADIUS after NAS changes (nothing else)
cat > /etc/sudoers.d/isp-panel <<'EOF'
isp-panel ALL=(root) NOPASSWD: /usr/bin/systemctl reload freeradius, /usr/bin/systemctl restart freeradius, /usr/bin/systemctl start --no-block isp-panel-fwsync.service
EOF
chmod 440 /etc/sudoers.d/isp-panel
visudo -cf /etc/sudoers.d/isp-panel >/dev/null || { rm -f /etc/sudoers.d/isp-panel; die "sudoers check failed"; }

# ------------------------------------------------------------------ first data
c_ok "Saving the company profile, super admin and router"
# a username that does not exist yet needs a password, even on a re-run where "keep the current one" was chosen
if [[ -z "$ADMIN_PASS" && "$(mariadb -N -B -e "SELECT COUNT(*) FROM panel_admins WHERE username = '$ADMIN_USER'" "$DB_NAME")" == 0 ]]; then
  ADMIN_PASS="$(gen_secret)"; ADMIN_PASS_SHOWN="$ADMIN_PASS"
fi
SEED=$(cd "$APP_DIR" && set -a && . "$ENV_FILE" && set +a && \
  SEED_ISP_NAME="$ISP_NAME" SEED_ISP_PHONE="$ISP_PHONE" SEED_ISP_ADDRESS="$ISP_ADDRESS" SEED_CALLBACK_BASE="$CALLBACK_BASE" \
  SEED_ADMIN_USER="$ADMIN_USER" SEED_ADMIN_NAME="$ADMIN_NAME" SEED_ADMIN_EMAIL="$ADMIN_EMAIL" SEED_ADMIN_PASS="$ADMIN_PASS" \
  SEED_NAS_IP="$ROUTER_IP" SEED_NAS_NAME="$ROUTER_NAME" SEED_NAS_SECRET="$ROUTER_SECRET" \
  SEED_BKASH_ON="$BKASH_ON" SEED_BKASH_MODE="$BKASH_MODE" SEED_BKASH_KEY="${BKASH_KEY:-}" SEED_BKASH_SECRET="${BKASH_SECRET:-}" \
  SEED_BKASH_USER="${BKASH_USER:-}" SEED_BKASH_PASS="${BKASH_PASS:-}" \
  python3 manage.py seed-install) || die "Could not save the first data: $SEED"
echo "   $SEED"
unset BKASH_SECRET BKASH_PASS
[[ "$SEED" == *router-* ]] && systemctl restart freeradius    # FreeRADIUS reads its router list at start

systemctl daemon-reload
systemctl enable --now isp-panel.service isp-panel-sender.service isp-panel-noc.service isp-panel-maint.timer isp-panel-backup.timer \
  isp-panel-fwsync.timer

# ------------------------------------------------------------------ Caddy (HTTPS)
c_ok "Configuring Caddy$( (( HTTPS_ON )) && echo " with Let's Encrypt for $DASH_DOMAIN${PAY_DOMAIN:+ and $PAY_DOMAIN}" )"
if (( HTTPS_ON )); then
  SITES="$DASH_DOMAIN${PAY_DOMAIN:+, $PAY_DOMAIN}"
  TLS_LINE=""
  [[ "${TLS13_ONLY:-0}" == 1 ]] && TLS_LINE="tls {
		protocols tls1.3
	}"
  FALLBACK="
# opening the server by its IP address goes to the panel's HTTPS name (the domains get Caddy's own redirect)
http://$PUBLIC_IP${LAN_IP:+, http://$LAN_IP} {
	redir https://$DASH_DOMAIN{uri} 308
}"
  ACME_EMAIL="$ADMIN_EMAIL"
else
  SITES=":80"; TLS_LINE=""; FALLBACK=""; ACME_EMAIL=""
fi
SITES="$SITES" TLS_LINE="$TLS_LINE" FALLBACK="$FALLBACK" ACME_EMAIL="$ACME_EMAIL" APP_DIR="$APP_DIR" python3 - \
  "$SRC_DIR/deploy/Caddyfile.tmpl" /etc/caddy/Caddyfile.new <<'PY'
import os, re, sys
s = open(sys.argv[1]).read()
if not os.environ["ACME_EMAIL"]:            # no email: Let's Encrypt still works, it just sends no expiry warnings
    s = re.sub(r"^\s*email __ACME_EMAIL__\n", "", s, flags=re.M)
for key, env in (("__SITES__", "SITES"), ("__TLS__", "TLS_LINE"), ("__FALLBACK__", "FALLBACK"),
                 ("__ACME_EMAIL__", "ACME_EMAIL"), ("__APP__", "APP_DIR")):
    s = s.replace(key, os.environ[env])
open(sys.argv[2], "w").write(s)
PY
mkdir -p /var/log/caddy; chown caddy:caddy /var/log/caddy 2>/dev/null || true
caddy fmt --overwrite /etc/caddy/Caddyfile.new >/dev/null 2>&1 || true
caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new >/tmp/caddy-validate.log 2>&1 \
  || { tail -n 20 /tmp/caddy-validate.log; die "The generated Caddyfile is not valid (see above). The old one is unchanged."; }
chown -R caddy:caddy /var/log/caddy 2>/dev/null || true     # validate may have created the log file as root
[[ -f /etc/caddy/Caddyfile ]] && cp -a /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
# Caddy replaces nginx; the nginx package stays installed but stopped, so nothing else listens on 80/443
if systemctl list-unit-files nginx.service >/dev/null 2>&1 && systemctl is-enabled --quiet nginx 2>/dev/null; then
  c_ok "Stopping nginx (Caddy now serves the panel)"
  systemctl disable --now nginx || true
fi
systemctl enable caddy
systemctl restart caddy
systemctl restart isp-panel.service isp-panel-sender.service isp-panel-noc.service

# ------------------------------------------------------------------ firewall
c_ok "Configuring the firewall (UFW)"
SSH_PORTS="$( { sshd -T 2>/dev/null | awk '$1 == "port" {print $2}'; ss -Htlnp 2>/dev/null | awk '/"sshd"/ {n = split($4, a, ":"); print a[n]}'; } | sort -un)"
# UFW refuses a comment with an apostrophe ("Invalid syntax"): keep comments to letters, digits, spaces and hyphens
for p in ${SSH_PORTS:-22}; do
  [[ "$p" =~ ^[0-9]{1,5}$ ]] || continue
  ufw allow "$p/tcp" comment "ssh" >/dev/null                 # first, so enabling never locks you out
done
ufw allow 80/tcp  comment "http - lets encrypt and redirect" >/dev/null
ufw allow 443/tcp comment "https" >/dev/null
ufw allow 443/udp comment "https - http3" >/dev/null
# the older installer opened RADIUS to the whole internet; now only the routers in the panel may reach it
ufw delete allow 1812/udp >/dev/null 2>&1 || true
ufw delete allow 1813/udp >/dev/null 2>&1 || true
ufw --force enable >/dev/null
/usr/local/sbin/isp-panel-fwsync | sed 's/^/   /' || c_warn "Could not open RADIUS for the routers; run: sudo /usr/local/sbin/isp-panel-fwsync"

# ------------------------------------------------------------------ HTTPS check
HTTPS_RESULT=""
if (( HTTPS_ON )); then
  c_ok "Waiting for the Let's Encrypt certificates (up to 2 minutes)"
  for d in $DASH_DOMAIN $PAY_DOMAIN; do
    ok=""
    for _ in $(seq 1 24); do
      if out="$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' "https://$d/login" 2>&1)" && [[ "$out" =~ ^[23] ]]; then ok=1; break; fi
      sleep 5
    done
    if [[ -n "$ok" ]]; then
      info="$(echo | timeout 10 openssl s_client -connect "$d:443" -servername "$d" 2>/dev/null | openssl x509 -noout -issuer -enddate 2>/dev/null \
        | sed -e 's/^issuer=.*O *= *\([^,]*\).*/\1/' -e 's/^notAfter=/valid until /' | paste -sd ', ')"
      HTTPS_RESULT="$HTTPS_RESULT
   https://$d  OK, trusted certificate (${info:-issuer not read})"
    else
      HTTPS_RESULT="$HTTPS_RESULT
   https://$d  NOT READY: $out. See: journalctl -u caddy -n 50"
    fi
  done
fi

# ------------------------------------------------------------------ deploys (GitHub Actions)
install -m 755 "$SRC_DIR/deploy/isp-panel-deploy" /usr/local/sbin/isp-panel-deploy
REPO_TOP="$(git -C "$SRC_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$REPO_TOP" ]]; then
  printf 'DEPLOY_REPO=%q\nDEPLOY_BRANCH=main\n' "$REPO_TOP" > "$ENV_DIR/deploy.conf"; chmod 644 "$ENV_DIR/deploy.conf"
  if [[ "$(stat -c %u "$REPO_TOP")" != 0 ]]; then
    c_warn "$REPO_TOP is not owned by root. Deploys run it as root, so make it root-only:  sudo chown -R root:root $REPO_TOP"
  fi
fi
echo "$INSTALLER_VERSION" > "$ENV_DIR/installer.version"

# ------------------------------------------------------------------ done
[[ -z "$ROUTER_SECRET" && -n "$ROUTER_IP" ]] && ROUTER_SECRET="$(mariadb -N -B -e "SELECT secret FROM nas WHERE nasname = '$ROUTER_IP'" "$DB_NAME" 2>/dev/null || true)"
RADIUS_ADDR="$PUBLIC_IP"
SPLIT_DNS=0
if [[ -n "$LAN_IP" && "$LAN_IP" != "$PUBLIC_IP" && "$LAN_IP" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]; then
  RADIUS_ADDR="$LAN_IP"; SPLIT_DNS=1        # the server sits inside the ISP network: routers and customers reach it on the LAN
fi
PANEL_URL="http://$PUBLIC_IP/"; (( HTTPS_ON )) && PANEL_URL="https://$DASH_DOMAIN/"

echo
c_ok "Installation finished"
echo "   Panel     : $PANEL_URL"
echo "   Portal    : ${PANEL_URL}portal   (customers sign in with their PPPoE username and password)"
echo "   Username  : $ADMIN_USER"
if [[ -n "$ADMIN_PASS_SHOWN" ]]; then
  echo "   Password  : $ADMIN_PASS_SHOWN     (shown once; change it after the first login)"
elif [[ -n "$ADMIN_PASS" ]]; then
  echo "   Password  : the one you typed"
else
  echo "   Password  : unchanged"
fi
[[ -n "$HTTPS_RESULT" ]] && printf '%s\n' "$HTTPS_RESULT"
(( HTTPS_ON )) || echo "   HTTPS     : off. Point a subdomain at $PUBLIC_IP and run the installer again to switch it on."
(( BKASH_ON )) && echo "   bKash     : $BKASH_MODE mode, callback address ${CALLBACK_BASE:-the panel address}"
echo
echo "   ---------------- paste on the MikroTik (Terminal) ----------------"
if [[ -n "$ROUTER_IP" ]]; then
cat <<EOF
   /radius add service=ppp address=$RADIUS_ADDR secret="${ROUTER_SECRET:-<the secret saved for $ROUTER_IP>}" authentication-port=1812 accounting-port=1813 timeout=3s comment="ISP RADIUS Panel"
   /radius incoming set accept=yes port=3799
   /ppp aaa set use-radius=yes accounting=yes interim-update=5m
EOF
else
  echo "   (no router entered: add one under Network > Routers; the panel shows the commands for it)"
fi
if (( HTTPS_ON && SPLIT_DNS )); then
cat <<EOF
   # split DNS: customers on your network reach the panel directly on the LAN, with the same trusted certificate
   /ip dns static add name=$DASH_DOMAIN address=$LAN_IP ttl=5m comment="ISP panel (LAN)"
EOF
  [[ -n "$PAY_DOMAIN" ]] && echo "   /ip dns static add name=$PAY_DOMAIN address=$LAN_IP ttl=5m comment=\"ISP payments (LAN)\""
  cat <<EOF
   # only if customers use the router as their DNS server; keep port 53 closed on the internet side:
   /ip dns set allow-remote-requests=yes
   /ip firewall filter add chain=input in-interface-list=WAN protocol=udp dst-port=53 action=drop comment="no open DNS resolver"
   /ip firewall filter add chain=input in-interface-list=WAN protocol=tcp dst-port=53 action=drop comment="no open DNS resolver"
EOF
fi
echo "   -------------------------------------------------------------------"
echo "   RADIUS is open only for the routers saved in the panel (UFW). A router added later is allowed within a minute."
echo "   Answers saved in $CONF_FILE (no passwords). Run sudo ./install.sh again any time to change them or to upgrade."
