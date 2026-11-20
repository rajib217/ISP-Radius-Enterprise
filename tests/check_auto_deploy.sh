#!/usr/bin/env bash
# End-to-end check of deploy/setup-auto-deploy.sh on a throw-away Ubuntu machine (GitHub Actions job "autodeploy"):
# real sshd, real deploy user, the key the script prints, the SSH gate and the one sudo command. Needs root.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "FAIL: $*"; echo "::error file=deploy/setup-auto-deploy.sh::$*"; exit 1; }
ok() { echo "ok    $*"; }

# a server where the panel counts as installed, and a root-owned stand-in for GitHub
mkdir -p /etc/isp-panel; [[ -f /etc/isp-panel/panel.env ]] || echo "DB_PASS=ci" > /etc/isp-panel/panel.env
rm -rf /tmp/origin.git /opt/isp-panel/src
git init -q --bare /tmp/origin.git
git -c safe.directory="*" -C "$ROOT" push -q /tmp/origin.git HEAD:refs/heads/main
command -v sshd >/dev/null || apt-get install -y -qq openssh-server >/dev/null
systemctl start ssh 2>/dev/null || service ssh start

bash "$ROOT/deploy/setup-auto-deploy.sh" --repo /tmp/origin.git --dir /opt/isp-panel/src > /tmp/setup.out 2>&1 \
  || { cat /tmp/setup.out; fail "setup-auto-deploy.sh exited with an error"; }
ok "setup script finished"
grep -q "^DEPLOY_REPO=/opt/isp-panel/src" /etc/isp-panel/deploy.conf || fail "deploy.conf not written"
[[ "$(stat -c %U /opt/isp-panel/src)" == root ]] || fail "the clone is not root-owned"
ok "root-owned clone and deploy.conf"

# the private key exactly as the script printed it
awk '/-----BEGIN OPENSSH PRIVATE KEY-----/,/-----END OPENSSH PRIVATE KEY-----/' /tmp/setup.out > /tmp/k; chmod 600 /tmp/k
[[ -s /tmp/k ]] || fail "no private key in the output"
ssh-keygen -y -f /tmp/k > /tmp/k.pub || fail "the printed private key is not a valid key"
grep -q "$(cut -d' ' -f2 /tmp/k.pub)" /home/deploy/.ssh/authorized_keys || fail "printed key does not match authorized_keys"
grep -q 'command="/usr/local/sbin/isp-panel-ssh-gate",restrict ' /home/deploy/.ssh/authorized_keys || fail "key is not locked to the gate"
ok "printed private key matches the locked authorized key"
grep -q "$(cut -d' ' -f2 /etc/ssh/ssh_host_ed25519_key.pub)" /tmp/setup.out || fail "SERVER_KNOWN_HOSTS does not hold the host key"
ok "SERVER_KNOWN_HOSTS holds this server's host key"

S="ssh -i /tmp/k -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null deploy@127.0.0.1"
out="$($S 'id; cat /etc/shadow' 2>&1 || true)"
grep -q "isp-panel-ssh-gate" <<<"$out" && ! grep -q "uid=" <<<"$out" || fail "a command other than the deploy was not refused: $out"
ok "other commands are refused"
out="$($S 2>&1 </dev/null || true)"
grep -q "isp-panel-ssh-gate" <<<"$out" || fail "an interactive login was not refused: $out"
ok "no interactive shell"

# the deploy itself: git pull as root, then update.sh (which stops here: no panel app on this CI machine)
out="$($S 'sudo -n /usr/local/sbin/isp-panel-deploy' 2>&1 || true)"
echo "$out" | sed 's/^/      /'
grep -q "now at " <<<"$out" || fail "the deploy did not pull from the repository"
grep -qE "UPDATE STOPPED|Update finished" <<<"$out" || fail "update.sh was not reached"
ok "deploy key runs the pull and update.sh through sudo"
out="$($S 'curl -fsS --max-time 10 http://127.0.0.1:8000/healthz' 2>&1 || true)"
grep -q "isp-panel-ssh-gate" <<<"$out" && fail "the health check command was refused"
ok "health check command is allowed"

sudo -u deploy sudo -n -l /bin/bash >/dev/null 2>&1 && fail "the deploy user may run other commands with sudo"
ok "sudo allows only isp-panel-deploy"
echo "auto-deploy setup works"
