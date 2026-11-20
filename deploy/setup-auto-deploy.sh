#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  ISP RADIUS Panel - set up automatic deploys from GitHub (run once on the server)
#
#  Usage:   sudo bash setup-auto-deploy.sh [--repo URL] [--dir /opt/isp-panel/src] [--user deploy]
#
#  What it does (safe to run again; nothing here restarts FreeRADIUS, MariaDB or Caddy):
#    1. makes a root-owned git clone of the panel (default /opt/isp-panel/src) and points
#       /etc/isp-panel/deploy.conf at it, so  sudo /usr/local/sbin/isp-panel-deploy  updates from GitHub
#    2. creates the "deploy" user. It has no password, and its sudo rights cover exactly one command:
#       /usr/local/sbin/isp-panel-deploy
#    3. makes a new SSH key for GitHub. The key is locked to /usr/local/sbin/isp-panel-ssh-gate, so it can only
#       deploy or read the health check: no shell, no forwarding
#    4. prints the four values to paste into GitHub (Settings > Secrets and variables > Actions)
#  Each run makes a NEW key and removes the old one, so paste the new private key into GitHub again.
# ---------------------------------------------------------------------------
set -euo pipefail
trap 'echo "setup-auto-deploy.sh stopped at line $LINENO: $BASH_COMMAND" >&2' ERR

REPO_URL=""
DIR=/opt/isp-panel/src
USER_NAME=deploy
TAG="isp-panel-github-deploy"
SBIN=/usr/local/sbin
ETC=/etc/isp-panel
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c_ok()   { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()    { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_URL="$2"; shift 2 ;;
    --dir)  DIR="$2"; shift 2 ;;
    --user) USER_NAME="$2"; shift 2 ;;
    *) die "unknown option: $1 (use --repo URL, --dir PATH, --user NAME)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root:  sudo bash $0"
[[ -f "$ETC/panel.env" ]] || die "The panel is not installed on this server yet. Run install.sh first."
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_-]{1,31}$ ]] || die "Invalid user name: $USER_NAME"
command -v git >/dev/null || { apt-get install -y -qq git >/dev/null; }
command -v ssh-keygen >/dev/null || die "ssh-keygen is missing (install openssh-client)."

if [[ -z "$REPO_URL" ]]; then
  REPO_URL="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
  [[ -n "$REPO_URL" ]] || REPO_URL="https://github.com/rajib217/ISP-Radius-Enterprise.git"
fi

# ------------------------------------------------------------- 1. clone
if [[ -d "$DIR/.git" ]]; then
  c_ok "Using the existing clone in $DIR (bringing it up to date)"
  chown -R root:root "$DIR"
  git -C "$DIR" fetch --quiet origin main
  git -C "$DIR" checkout --quiet main 2>/dev/null || git -C "$DIR" checkout --quiet -B main origin/main
  # fast-forward only: files changed by hand in the clone are never overwritten
  git -C "$DIR" merge --quiet --ff-only origin/main \
    || die "$DIR has changes made on the server, so it cannot be updated. See: git -C $DIR status"
  echo "   now at $(git -C "$DIR" rev-parse --short=12 HEAD): $(git -C "$DIR" log -1 --format=%s)"
elif [[ -e "$DIR" && -n "$(ls -A "$DIR" 2>/dev/null)" ]]; then
  die "$DIR exists and is not a git clone (perhaps the copied install files). Use another folder:  --dir /opt/isp-panel/repo"
else
  c_ok "Cloning $REPO_URL into $DIR"
  mkdir -p "$(dirname "$DIR")"
  git clone --quiet --branch main "$REPO_URL" "$DIR" \
    || die "git clone failed. For a private repository add a read-only deploy key or use --repo with an access URL."
  chown -R root:root "$DIR"
fi
umask 022
printf 'DEPLOY_REPO=%q\nDEPLOY_BRANCH=main\n' "$DIR" > "$ETC/deploy.conf"
chmod 644 "$ETC/deploy.conf"
install -m 755 "$DIR/deploy/isp-panel-deploy" "$SBIN/isp-panel-deploy"
install -m 755 "$DIR/deploy/isp-panel-ssh-gate" "$SBIN/isp-panel-ssh-gate"

# ------------------------------------------------------------- 2. deploy user + one sudo command
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  c_ok "Creating user $USER_NAME (no password; it can only run the deploy)"
  useradd --create-home --shell /bin/sh "$USER_NAME"
fi
passwd -l "$USER_NAME" >/dev/null 2>&1 || true
SUDOERS=/etc/sudoers.d/isp-panel-deploy
printf '%s ALL=(root) NOPASSWD: %s/isp-panel-deploy\n' "$USER_NAME" "$SBIN" > "$SUDOERS.tmp"
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || { rm -f "$SUDOERS.tmp"; die "sudoers check failed"; }
mv "$SUDOERS.tmp" "$SUDOERS"

# ------------------------------------------------------------- 3. SSH key locked to the gate
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh"
AK="$HOME_DIR/.ssh/authorized_keys"
touch "$AK"
KEYDIR="$(mktemp -d)"; trap 'rm -rf "$KEYDIR"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "$TAG" -f "$KEYDIR/key"
{ grep -v " $TAG\$" "$AK" || true
  printf 'command="%s/isp-panel-ssh-gate",restrict %s\n' "$SBIN" "$(cat "$KEYDIR/key.pub")"; } > "$AK.new"
mv "$AK.new" "$AK"
chown "$USER_NAME:$USER_NAME" "$AK"; chmod 600 "$AK"
c_ok "SSH key for GitHub created (locked to deploying only)"

# ------------------------------------------------------------- 4. what to paste into GitHub
[[ -f "$ETC/install.conf" ]] && . "$ETC/install.conf"
HOST="${SAVED_PUBLIC_IP:-}"
[[ -n "$HOST" ]] || HOST="$(curl -4 -fsS --max-time 6 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
# sshd -T fails on Ubuntu 24.04 when ssh is socket-activated and /run/sshd is missing: then read sshd_config
PORT="$( { sshd -T 2>/dev/null || true; } | awk '$1 == "port" {print $2; exit}')"
[[ -n "$PORT" ]] || PORT="$(cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | awk 'tolower($1) == "port" {print $2; exit}' || true)"
[[ "$PORT" =~ ^[0-9]+$ ]] || PORT=22
HOSTPAT="$HOST"; [[ "$PORT" == 22 ]] || HOSTPAT="[$HOST]:$PORT"
KNOWN="$(for f in /etc/ssh/ssh_host_*_key.pub; do [[ -f "$f" ]] && printf '%s %s\n' "$HOSTPAT" "$(cut -d' ' -f1,2 "$f")"; done; true)"
[[ -n "$KNOWN" ]] || die "No SSH host keys in /etc/ssh (is openssh-server installed?)"
PANEL_URL=""
[[ -n "${SAVED_DASH_DOMAIN:-}" ]] && PANEL_URL="https://$SAVED_DASH_DOMAIN"

# self-check: the user may run exactly the deploy command with sudo
sudo -u "$USER_NAME" sudo -n -l "$SBIN/isp-panel-deploy" >/dev/null 2>&1 \
  || c_warn "sudo check failed for $USER_NAME; see $SUDOERS"

cat <<EOF

==> Done. Now add these in GitHub: repository > Settings > Secrets and variables > Actions > New repository secret

  Name: SERVER_HOST
  Value: $HOST

  Name: SERVER_USER
  Value: $USER_NAME

  Name: SERVER_KNOWN_HOSTS
  Value (all lines):
$KNOWN

  Name: SERVER_SSH_KEY
  Value (all lines, from -----BEGIN to END-----):
$(cat "$KEYDIR/key")

EOF
[[ "$PORT" == 22 ]] || printf '  Name: SERVER_PORT\n  Value: %s\n\n' "$PORT"
[[ -n "$PANEL_URL" ]] && printf '  Name: PANEL_URL   (optional: also checks the public HTTPS address after each deploy)\n  Value: %s\n\n' "$PANEL_URL"
cat <<EOF
  The private key above is shown only now and is not kept on this server. Copy it into GitHub, then clear the
  screen (clear). Running this script again makes a new key and the old one stops working.

  Test by hand any time:   sudo /usr/local/sbin/isp-panel-deploy
  After the secrets are in GitHub, every push to main deploys once all checks pass.
EOF
