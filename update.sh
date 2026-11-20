#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  ISP RADIUS Panel - safe code update (no internet interruption)
#
#  Usage:   sudo bash update.sh            (from the updated project folder)
#  GitHub Actions runs it through /usr/local/sbin/isp-panel-deploy after "git pull".
#
#  What it touches: the panel code, the panel database schema, and the panel's own services
#  (isp-panel = web, isp-panel-sender, isp-panel-noc). It NEVER restarts FreeRADIUS, MariaDB or Caddy,
#  so PPPoE sessions, RADIUS logins and HTTPS certificates are not interrupted.
#
#  Order (every step must pass before the next one; a failure leaves the running panel as it was):
#    1. stage the new code next to the live one and test that it imports
#    2. back up the database to /backups (last 7 kept; session history tables: structure only)
#    3. apply sql/panel_schema.sql (idempotent, only adds tables / columns / indexes)
#    4. swap the code in, reload the web panel gracefully (old workers finish their requests)
#    5. health check on 127.0.0.1; if it fails, the previous code is put back automatically
#  A change that needs system setup (new service, FreeRADIUS config, Caddy) stops at step 0 and asks
#  for "sudo ./install.sh" instead.
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/isp-panel}"
APP_DIR="$INSTALL_DIR/app"
NEW_DIR="$INSTALL_DIR/app.new"
PREV_DIR="$INSTALL_DIR/app.prev"
ENV_FILE="${ENV_FILE:-/etc/isp-panel/panel.env}"
ETC_DIR="$(dirname "$ENV_FILE")"
DB_NAME=radius
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-7}"
# history tables: their rows are large and not needed to undo a code update (the nightly backup has them)
HISTORY_TABLES="radacct_archive radpostauth traffic_snapshots traffic_last gateway_logs stats_snapshots"

c_ok()   { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()    { printf '\033[1;31mUPDATE STOPPED:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash update.sh"
[[ -f "$SRC_DIR/panel/app.py" && -f "$SRC_DIR/sql/panel_schema.sql" ]] || die "Run this from the project folder (the one with update.sh, panel/ and sql/)."
[[ -f "$ENV_FILE" && -d "$APP_DIR" ]] || die "The panel is not installed here yet. Run: sudo ./install.sh"

exec 9>"${LOCK_FILE:-/run/isp-panel-update.lock}"
flock -n 9 || die "Another update is running."

REV="$(git -C "$SRC_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo "local-$(date +%Y%m%d%H%M%S)")"
STAMP="$(date +%Y%m%d-%H%M%S)"
c_ok "Updating the panel to $REV"

# ------------------------------------------------------------ 0. system setup changed?
NEED="$(sed -n 's/^INSTALLER_VERSION=\([0-9]\+\).*/\1/p' "$SRC_DIR/install.sh" | head -n1)"
HAVE="$(cat "$ETC_DIR/installer.version" 2>/dev/null || echo 0)"
if (( ${NEED:-0} > ${HAVE:-0} )); then
  die "This update also changes the system setup (installer version $HAVE -> $NEED).
   Run it once by hand on the server:  cd $SRC_DIR && sudo ./install.sh
   (Nothing was changed. Customers stay online.)"
fi

# ------------------------------------------------------------ 1. stage and test
c_ok "Staging the new code"
rm -rf "$NEW_DIR"
cp -a "$SRC_DIR/panel/." "$NEW_DIR/"
find "$NEW_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
printf '%s\n' "$REV" > "$NEW_DIR/REVISION"
chown -R root:root "$NEW_DIR"; chmod -R a+rX "$NEW_DIR"

python3 -m py_compile "$NEW_DIR"/*.py 2>/tmp/isp-panel-compile.log \
  || { cat /tmp/isp-panel-compile.log; rm -rf "$NEW_DIR"; die "Python syntax error in the new code (see above)."; }
find "$NEW_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
# import the whole app as the panel user, with its real settings, without serving anything
if ! out="$(cd "$NEW_DIR" && set -a && . "$ENV_FILE" && set +a && \
            runuser -u isp-panel -- env PYTHONDONTWRITEBYTECODE=1 python3 -c 'import app' 2>&1)"; then
  printf '%s\n' "$out" | tail -n 20
  rm -rf "$NEW_DIR"
  die "The new code does not start (import error above)."
fi
[[ -f "$SRC_DIR/deploy/isp-panel-fwsync" ]] && python3 -m py_compile "$SRC_DIR/deploy/isp-panel-fwsync"

# ------------------------------------------------------------ 2. backup
c_ok "Backing up the database to $BACKUP_DIR"
install -d -m 700 "$BACKUP_DIR"
FREE_MB=$(df -Pm "$BACKUP_DIR" | awk 'NR == 2 {print $4}')
(( FREE_MB > 1024 )) || { rm -rf "$NEW_DIR"; die "Less than 1 GB free in $BACKUP_DIR. Free some space first."; }
DUMP="$(command -v mariadb-dump || command -v mysqldump)"
FILE="$BACKUP_DIR/isp-panel-$STAMP-$REV.sql.gz"
IGNORE=(); NODATA=()
for t in $HISTORY_TABLES; do
  if [[ "$(mariadb -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='$t'")" == 1 ]]; then
    IGNORE+=("--ignore-table=$DB_NAME.$t"); NODATA+=("$t")
  fi
done
umask 077
if ! {
  "$DUMP" --single-transaction --quick --routines --triggers --hex-blob "${IGNORE[@]}" "$DB_NAME"
  (( ${#NODATA[@]} )) && "$DUMP" --no-data --skip-add-drop-table "$DB_NAME" "${NODATA[@]}" | sed 's/^CREATE TABLE /CREATE TABLE IF NOT EXISTS /'
  true
} | gzip -6 > "$FILE.part"; then
  rm -f "$FILE.part"; rm -rf "$NEW_DIR"; die "The database backup failed. Nothing was changed."
fi
gzip -t "$FILE.part" || { rm -f "$FILE.part"; rm -rf "$NEW_DIR"; die "The backup file is damaged. Nothing was changed."; }
mv "$FILE.part" "$FILE"
umask 022
echo "   $FILE ($(du -h "$FILE" | cut -f1))"
# keep the newest $KEEP_BACKUPS update backups (the nightly backups live elsewhere and are not touched)
ls -1t "$BACKUP_DIR"/isp-panel-*.sql.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f --

# ------------------------------------------------------------ 3. schema
c_ok "Applying the database schema"
if ! mariadb "$DB_NAME" <"$SRC_DIR/sql/panel_schema.sql" 2>/tmp/isp-panel-schema.log; then
  tail -n 15 /tmp/isp-panel-schema.log
  rm -rf "$NEW_DIR"
  die "The schema update failed (see above). The running panel was not touched.
   The schema file only adds things, so the old code keeps working. Backup: $FILE"
fi
# new panel tables must use radcheck's collation, or joins on username fail (same rule as install.sh)
RC_COLL=$(mariadb -N -e "SELECT TABLE_COLLATION FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='radcheck'")
if [[ "$RC_COLL" =~ ^[a-z0-9_]+$ ]]; then
  for t in $(sed -n 's/^CREATE TABLE IF NOT EXISTS `\{0,1\}\([A-Za-z0-9_]*\).*/\1/p' "$SRC_DIR/sql/panel_schema.sql" | sort -u); do
    CUR=$(mariadb -N -e "SELECT TABLE_COLLATION FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='$t'")
    if [[ -n "$CUR" && "$CUR" != "$RC_COLL" ]]; then
      mariadb "$DB_NAME" -e "ALTER TABLE \`$t\` CONVERT TO CHARACTER SET utf8mb4 COLLATE $RC_COLL" || c_warn "Could not convert $t to $RC_COLL"
    fi
  done
fi

# ------------------------------------------------------------ 4. swap and reload
c_ok "Switching to the new code"
rm -rf "$PREV_DIR"
mv "$APP_DIR" "$PREV_DIR"
mv "$NEW_DIR" "$APP_DIR"
SBIN="${SBIN_DIR:-/usr/local/sbin}"
[[ -f "$SRC_DIR/deploy/isp-panel-fwsync" ]] && install -m 755 "$SRC_DIR/deploy/isp-panel-fwsync" "$SBIN/isp-panel-fwsync"
[[ -f "$SRC_DIR/deploy/isp-panel-deploy" ]] && install -m 755 "$SRC_DIR/deploy/isp-panel-deploy" "$SBIN/isp-panel-deploy"
[[ -f "$SRC_DIR/deploy/isp-panel-ssh-gate" && -f "$SBIN/isp-panel-ssh-gate" ]] && install -m 755 "$SRC_DIR/deploy/isp-panel-ssh-gate" "$SBIN/isp-panel-ssh-gate"

reload_panel() {
  if systemctl show -p ExecReload isp-panel.service | grep -q 'kill'; then
    systemctl reload isp-panel.service       # gunicorn HUP: new workers start, old ones finish their requests
  else
    systemctl restart isp-panel.service      # older unit without ExecReload: a few seconds of panel downtime
  fi
  systemctl try-restart isp-panel-sender.service isp-panel-noc.service
}
reload_panel

# ------------------------------------------------------------ 5. health check (rollback on failure)
c_ok "Checking that the new code answers"
healthy() {
  local body
  body="$(curl -fsS --max-time 5 http://127.0.0.1:8000/healthz 2>/dev/null)" || return 1
  [[ "$body" == *"\"ok\": true"* || "$body" == *'"ok":true'* ]] && [[ "$body" == *"$REV"* ]]
}
ok=0
for _ in $(seq 1 30); do
  if healthy; then ok=1; break; fi
  sleep 2
done
if (( ! ok )); then
  c_warn "The new code did not answer the health check. Putting the previous code back."
  journalctl -u isp-panel -n 20 --no-pager 2>/dev/null || true
  rm -rf "$NEW_DIR"; mv "$APP_DIR" "$NEW_DIR"; mv "$PREV_DIR" "$APP_DIR"
  reload_panel
  die "Rolled back to the previous code (the failed version is kept in $NEW_DIR for a look). Backup: $FILE"
fi

c_ok "Update finished: $REV is live. FreeRADIUS, MariaDB and Caddy were not restarted."
echo "REVISION=$REV"
